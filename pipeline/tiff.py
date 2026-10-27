"""東京国際映画祭 (TIFF) 板块：上映作品 + 排片 → DB → 小程序 JSON.

数据流:

  1. parse_list_html   解析 TIFF 官网「上映作品・イベント一覧」页面 HTML
                       (https://2026.tiff-jp.net/ja/lineup/list.html 浏览器另存)
                       → 作品清单 + 每场上映日期/开场时间/免费/事前申込。这是作品与场次的「真源」。
  2. import_films      逐片调官网背后的公开 JSON API (api-2026.tiff-jp.net/api/films/{code})
                       补 HTML 里没有的：会场、结束时间、字幕、票价、售票状态/购票链接、
                       英文/原题、制作年、国家、片长、导演/演员、简介。API 失败不致命，只用 HTML。
  3. match_eiga        用片名搜 eiga.com，按「导演一致 + 年份±2 / 同名 + 年份±1」校验候选，
                       确定 eiga movie_id，eiga 信息（评分/海报/导演/演员）写进主库 movies。
  4. match_douban      该 movie_id 在主库已有豆瓣匹配（step2 做过）→ 直接用；否则搜豆瓣
                       （原题/英题 → 日文片名，候选过 国家/年份/汉字导演 校验），结果写进
                       step2 同一套表 douban_matches / douban_details / douban_matches_history。
  5. export / upload   生成 tiff.json（作品 + 场次）+ tiff.meta.json（版本哈希），上传云存储 data/。

表（eiga / 豆瓣匹配信息与日常 step1/step2 共用一处，TIFF 表只外链 movie_id）:
  tiff_films       1 行 = 1 个上映作品/活动（film_code 主键，如 39005WFC15）。
                   movie_id → movies.movie_id：eiga.com 的 id；eiga 没登记但豆瓣有的片，
                   按 step1 惯例分配 99999xxx 临时 id（之后 eiga 登记了用 step2_api.py remap-id /
                   merge-id 换成正式 id，tiff_films 会一起级联）。
  tiff_screenings  1 行 = 1 场（film_code, show_date, start_time, venue）
"""
from __future__ import annotations

import hashlib
import html as html_lib
import json
import logging
import random
import re
import sqlite3
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DB_PATH = Path("data/eiga.db")

FESTIVAL_YEAR = 2026
FESTIVAL_NAME = "第39回東京国際映画祭"
FESTIVAL_START = "2026-10-26"
FESTIVAL_END = "2026-11-04"

SITE = "https://2026.tiff-jp.net"
API = "https://api-2026.tiff-jp.net/api"
# saling_status 1/2 时官网显示「オンラインチケット サイトへ」按钮，链接 = 前缀 + sales_identifier
TICKET_PREFIX = "https://reserve.smart-theater.com/projects/tiff2026-production/purchase/transaction/"

CLOUD_JSON = "tiff.json"
CLOUD_META = "tiff.meta.json"

MAX_DOUBAN_ATTEMPTS = 2
DOUBAN_NONE = 99  # douban_attempts=99：人工确认豆瓣没有该片，不再自动搜

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "ja,en;q=0.8",
    "Referer": f"{SITE}/ja/lineup/list.html",
}


# ── DB ────────────────────────────────────────────────────────────────────────

_FILMS_DDL = """
        CREATE TABLE IF NOT EXISTS tiff_films (
            film_code        TEXT PRIMARY KEY,  -- 官网作品编号，如 39005WFC15
            category         TEXT,              -- 部门（日文），如 ワールド・フォーカス
            category_en      TEXT,
            dept_subtitle    TEXT,              -- 部门内小专题，如 台湾電影ルネッサンス2026
            title_ja         TEXT NOT NULL,
            title_en         TEXT,
            title_original   TEXT,
            director_ja      TEXT,
            director_en      TEXT,
            director_original TEXT,
            cast_ja          TEXT,              -- " / " 连接
            synopsis_ja      TEXT,
            duration         INTEGER,
            year             INTEGER,
            country          TEXT,              -- 官网格式「台湾／日本」
            language         TEXT,
            premiere         TEXT,              -- ワールド・プレミア 等
            copyright        TEXT,
            image_url        TEXT,              -- 官网剧照 /img/lineup/cut/{code}_1.jpg (横版)
            video_id         TEXT,              -- YouTube 预告片 id
            is_event         INTEGER NOT NULL DEFAULT 0,  -- 1=讲座/仪式等非电影（不做 eiga/豆瓣匹配）
            api_ok           INTEGER NOT NULL DEFAULT 0,  -- 1=官网 API 补全成功
            -- 外链主库：eiga / 豆瓣信息都在 movies / douban_matches / douban_details（按 movie_id）
            movie_id         TEXT,              -- eiga.com id；eiga 未登记但有豆瓣 → 99999xxx 临时 id
            eiga_match       TEXT,              -- auto / manual / none（TIFF 侧的 eiga 匹配记账）
            douban_attempts  INTEGER NOT NULL DEFAULT 0,  -- 自动搜豆瓣失败次数；99=人工确认无
            updated_at       TEXT
        );
"""

# 旧版（TIFF 自带 eiga_* / douban_* 列）→ 新版迁移时删掉的列
_OLD_COLS = ("eiga_rating", "eiga_rating_count", "eiga_poster", "eiga_director", "eiga_cast",
             "douban_id", "douban_match", "douban_title_cn", "douban_score", "douban_votes",
             "douban_director", "douban_cast", "douban_genre", "douban_year", "douban_country",
             "douban_watched", "douban_wish", "douban_poster", "douban_reviews", "douban_checked_at")


def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_FILMS_DDL + """
        CREATE TABLE IF NOT EXISTS tiff_screenings (
            film_code        TEXT NOT NULL,
            show_date        TEXT NOT NULL,     -- 2026-10-26
            start_time       TEXT NOT NULL,     -- HH:MM，HTML 无时间（如授奖式）时为 ''
            end_time         TEXT,
            venue            TEXT NOT NULL DEFAULT '',  -- ヒューマントラストシネマ有楽町 シアター１
            venue_area       TEXT,              -- 有楽町 / 日比谷 …
            venue_theater    TEXT,              -- ヒューマントラストシネマ有楽町
            subtitle         TEXT,              -- 英語字幕・日本語字幕
            price            TEXT,
            is_free          INTEGER NOT NULL DEFAULT 0,
            pre_registration INTEGER NOT NULL DEFAULT 0,
            sale_status      INTEGER,           -- 官网 saling_status（1/2=可在线购票）
            sales_id         TEXT,
            PRIMARY KEY (film_code, show_date, start_time, venue),
            FOREIGN KEY (film_code) REFERENCES tiff_films(film_code)
        );
    """)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(tiff_films)")}
    if "eiga_movie_id" in cols:
        _migrate_to_shared(conn)
    conn.commit()
    return conn


def _migrate_to_shared(conn: sqlite3.Connection) -> None:
    """一次性迁移：旧版把 eiga / 豆瓣匹配存在 tiff_films 自己的列里 → 写进主库
    movies / douban_matches / douban_details / douban_matches_history（与 step1/step2 同一处），
    tiff_films 只留 movie_id。主库里该 movie_id 已有豆瓣匹配的（step2 做过）保持不动。"""
    from pipeline.step2_douban import _ensure_douban_table
    _ensure_douban_table(conn)
    rows = [dict(r) for r in conn.execute("SELECT * FROM tiff_films")]
    conn.execute("ALTER TABLE tiff_films RENAME COLUMN eiga_movie_id TO movie_id")
    n_movie = n_douban = n_temp = 0
    for r in rows:
        if r["is_event"]:
            continue
        mid = r["eiga_movie_id"]
        if mid:
            n_movie += _upsert_movie(conn, mid, r, detail={
                "eiga_rating": r["eiga_rating"], "eiga_rating_count": r["eiga_rating_count"],
                "director": r["eiga_director"], "cast": r["eiga_cast"]}, poster=r["eiga_poster"])
        if r["douban_id"]:
            if not mid:
                mid = _new_temp_movie(conn, r)
                n_temp += 1
            if not _has_douban(conn, mid):
                try:
                    reviews = json.loads(r["douban_reviews"] or "[]")
                except Exception:
                    reviews = []
                _save_douban_dict(conn, mid, r, {
                    "douban_id": r["douban_id"], "title_cn": r["douban_title_cn"],
                    "score": r["douban_score"], "votes": r["douban_votes"],
                    "director": r["douban_director"], "cast": r["douban_cast"],
                    "genre": r["douban_genre"], "year": r["douban_year"],
                    "country": r["douban_country"], "watched": r["douban_watched"],
                    "wish": r["douban_wish"], "poster": r["douban_poster"], "reviews": reviews,
                }, manual=(r["douban_match"] == "manual"))
                n_douban += 1
        if r["douban_match"] == "manual-none":
            conn.execute("UPDATE tiff_films SET douban_attempts=? WHERE film_code=?",
                         (DOUBAN_NONE, r["film_code"]))
    for c in _OLD_COLS:
        try:
            conn.execute(f"ALTER TABLE tiff_films DROP COLUMN {c}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    logger.info("TIFF 迁移到主库：movies %d 行（新增/补全），豆瓣匹配 %d 条，临时 id %d 个",
                n_movie, n_douban, n_temp)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today_jst() -> str:
    from datetime import timezone, timedelta
    return datetime.now(timezone(timedelta(hours=9))).date().isoformat()


# ── 主库写入（movies / douban_*，与 step1 / step2 同一套表） ────────────────────

def _movie_country(tiff_country: Optional[str]) -> str:
    """官网「台湾／日本」→ movies.country 的 eiga 风格「台湾・日本」（step2 的国家校验按 ・ 拆）。"""
    return re.sub(r"\s*[／/]\s*", "・", tiff_country or "").strip()


def _is_temp(mid: Optional[str]) -> bool:
    return bool(mid) and str(mid).startswith("99999")


def _upsert_movie(conn, mid: str, r, title: Optional[str] = None,
                  detail: Optional[dict] = None, poster: Optional[str] = None) -> int:
    """eiga 信息写进主库 movies。新行直接插入；已有行（step1 抓过的）只补空字段，
    不覆盖 step1 的数据。category / rank 留空（只有 step1 按影院上映情况设置）。"""
    d = detail or {}
    vals = {
        "country": d.get("country") or _movie_country(r["country"]) or None,
        "title_original": d.get("title_original") or r["title_original"] or r["title_en"] or None,
        "year": d.get("year") or r["year"],
        "duration": d.get("duration") or r["duration"],
        "release_date": d.get("release_date"),
        # eiga 详情没抓到时用 TIFF 官网的日文导演 / 演员（与 eiga 同为日文片假名表记）
        "director": d.get("director") or r["director_ja"] or None,
        "eiga_rating": d.get("eiga_rating"),
        "eiga_rating_count": d.get("eiga_rating_count"),
        "script": d.get("script"),
        "cast_names": d.get("cast") or r["cast_ja"] or None,
        "poster_url": poster or None,
    }
    if conn.execute("SELECT 1 FROM movies WHERE movie_id=?", (mid,)).fetchone():
        conn.execute(
            "UPDATE movies SET " + ", ".join(f"{k}=COALESCE({k}, ?)" for k in vals)
            + " WHERE movie_id=?", list(vals.values()) + [mid])
        return 0
    cols = ["movie_id", "title_jp", "eiga_url"] + list(vals)
    conn.execute(
        f"INSERT INTO movies ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        [mid, title or clean_title(r["title_ja"]),
         None if _is_temp(mid) else f"https://eiga.com/movie/{mid}/"] + list(vals.values()))
    return 1


def _new_temp_movie(conn, r) -> str:
    """eiga 没登记但豆瓣有的 TIFF 片：按 step1 惯例分配 99999xxx 临时 id 建 movies 行。"""
    from pipeline.step1_eiga import _next_unlisted_id
    mid = _next_unlisted_id(conn)
    _upsert_movie(conn, mid, r, detail={"director": r["director_ja"], "cast": r["cast_ja"]})
    conn.execute("UPDATE tiff_films SET movie_id=? WHERE film_code=?", (mid, r["film_code"]))
    return mid


def _has_douban(conn, mid: Optional[str]) -> bool:
    if not mid:
        return False
    return conn.execute(
        "SELECT 1 FROM douban_matches WHERE movie_id=? AND COALESCE(douban_id,'')!=''",
        (mid,)).fetchone() is not None


def _save_douban_dict(conn, mid: str, r, d: dict, manual: bool = False, soup=None) -> None:
    """豆瓣匹配写进 step2 同一套表：douban_matches（基础行）+ douban_matches_history（基线）
    + douban_details。有页面 soup 时 details 交给 step2 的 _save_douban_details 解析（同一套逻辑，
    顺带 IMDb）；没有（迁移 / 页面抓取失败）时用 d 里已有的字段。"""
    from pipeline.step2_douban import (_ensure_douban_table, _save_douban_details,
                                       _check_year_match, _check_country_match)
    _ensure_douban_table(conn)
    now = _now()
    sid = d["douban_id"]
    year_ok = _check_year_match(r["year"] or 0, _int(d.get("year")) or 0)
    country_ok = _check_country_match(_movie_country(r["country"]), d.get("country") or "")
    conn.execute(
        """INSERT OR REPLACE INTO douban_matches
           (movie_id, douban_id, title_cn, douban_score, douban_votes, director, cast, genre,
            douban_year, douban_country, douban_url, year_ok, country_ok, verified, manual,
            matched_at, score_checked_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (mid, sid, d.get("title_cn") or "", d.get("score") or 0, d.get("votes") or 0,
         d.get("director") or "", d.get("cast") or "", d.get("genre") or "",
         _int(d.get("year")), d.get("country") or "",
         f"https://movie.douban.com/subject/{sid}/",
         1 if year_ok else 0, 1 if country_ok else 0, 1 if (year_ok and country_ok) else 0,
         1 if manual else 0, now, now[:10]))
    conn.execute(
        """INSERT OR IGNORE INTO douban_matches_history
           (movie_id, douban_id, douban_score, douban_votes, snapshot_at) VALUES (?,?,?,?,?)""",
        (mid, sid, d.get("score") or 0, d.get("votes") or 0, now))
    if soup is not None:
        _save_douban_details(conn, mid, sid, soup, r["year"], _movie_country(r["country"]), now)
        return
    if conn.execute("SELECT 1 FROM douban_details WHERE movie_id=?", (mid,)).fetchone():
        return
    conn.execute(
        """INSERT INTO douban_details
           (movie_id, douban_id, title_cn, douban_score, douban_votes, director, cast, genre,
            douban_year, douban_country, douban_url, want_to_watch, watched, trailer_urls,
            short_reviews, updated_at, poster_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (mid, sid, d.get("title_cn") or "", d.get("score") or 0, d.get("votes") or 0,
         d.get("director") or "", d.get("cast") or "", d.get("genre") or "",
         _int(d.get("year")), d.get("country") or "", f"https://movie.douban.com/subject/{sid}/",
         d.get("wish") or 0, d.get("watched") or 0, "[]",
         json.dumps([{"text": t} for t in (d.get("reviews") or []) if t], ensure_ascii=False),
         now, d.get("poster") or ""))


def _drop_douban(conn, mid: str) -> None:
    for t in ("douban_matches", "douban_details"):
        conn.execute(f"DELETE FROM {t} WHERE movie_id=?", (mid,))


# ── 1. 解析上传的列表 HTML ─────────────────────────────────────────────────────

_DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})")
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def _hidden(el) -> bool:
    return bool(el) and "display: none" in (el.get("style") or "").replace("display:none", "display: none")


def parse_list_html(path: Path) -> list[dict]:
    """解析「上映作品・イベント一覧」页 HTML → [{code, category, title, director,
    director_original, premiere, copyright, has_trailer, shows:[{date, time, free, prereg}]}]。

    页面结构：div.films > div.film（一片一个），内含
      .image a[href=film/{code}] / .info .category / .info .title span /
      ul.show li.item（.date "11/03[TUE]"、.time "17:45-"、.price_main 無料、.pre-registration）/
      .director「監督：xxx」（活动类 style=display:none）/ .original「[原文名]」/ .premiere / .copyright
    """
    soup = BeautifulSoup(Path(path).read_text(encoding="utf-8"), "lxml")
    films: list[dict] = []
    seen: set = set()
    for div in soup.select("div.films > div.film"):
        a = div.select_one(".title a[href]") or div.select_one(".image a[href]")
        if not a:
            continue
        m = re.search(r"film/([0-9A-Z]+)", a.get("href", ""))
        if not m:
            continue
        code = m.group(1)
        if code in seen:
            continue
        seen.add(code)

        dir_el = div.select_one(".director")
        director, director_orig = "", ""
        if dir_el and not _hidden(dir_el):
            orig_el = dir_el.select_one(".original")
            if orig_el:
                if not _hidden(orig_el):
                    director_orig = _text(orig_el).strip("[]［］ ")
                orig_el.extract()
            director = re.sub(r"^\s*監督[：:]\s*", "", _text(dir_el)).strip()

        shows = []
        for li in div.select("ul.show li.item"):
            dm = _DATE_RE.search(_text(li.select_one(".date")))
            if not dm:
                continue
            tm = _TIME_RE.search(_text(li.select_one(".time")))
            shows.append({
                "date": f"{FESTIVAL_YEAR}-{int(dm.group(1)):02d}-{int(dm.group(2)):02d}",
                "time": f"{int(tm.group(1)):02d}:{tm.group(2)}" if tm else "",
                "free": "無料" in _text(li.select_one(".price_main")),
                "prereg": li.select_one(".pre-registration") is not None,
            })

        films.append({
            "code": code,
            "category": _text(div.select_one(".category")),
            "title": _text(div.select_one(".title")),
            "director": director,
            "director_original": director_orig,
            "premiere": _text(div.select_one(".premiere")),
            "copyright": _text(div.select_one(".copyright")),
            "has_trailer": div.select_one(".trailer") is not None,
            "shows": shows,
        })
    logger.info("TIFF HTML: %d 个作品/活动, %d 场", len(films), sum(len(f["shows"]) for f in films))
    return films


# ── 2. 官网 API 补全 ───────────────────────────────────────────────────────────

def _api_get(path: str, retries: int = 3) -> Optional[dict]:
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(f"{API}/{path}", headers=_HEADERS, timeout=20)
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()
        except Exception as exc:
            last = exc
            time.sleep(1.5 * attempt)
    logger.warning("TIFF API %s 失败: %s", path, last)
    return None


def _clean_html(s: Optional[str]) -> str:
    if not s:
        return ""
    s = re.sub(r"<br\s*/?>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", html_lib.unescape(s)).strip()


def _venue_parts(venue: Optional[dict]) -> tuple[str, str, str]:
    """→ (屏幕名, 地区, 影院)。去掉「(車椅子スペースあり)」等括注。"""
    if not venue:
        return "", "", ""
    name = re.sub(r"\s*[（(][^）)]*[）)]\s*$", "", venue.get("name_ja") or "").strip()
    parts = (venue.get("full_name_ja") or "").split("/")
    area = parts[0].strip() if parts else ""
    theater = parts[1].strip() if len(parts) > 1 else ""
    return name, area, theater


def _int(v) -> Optional[int]:
    try:
        return int(str(v).strip()) if str(v).strip() else None
    except (TypeError, ValueError):
        return None


def _hhmm(v: Optional[str]) -> str:
    m = _TIME_RE.search(v or "")
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


def films_from_json(path: Path) -> list[dict]:
    """从导出的 tiff.json 还原 parse_list_html 的结果（HTML 原件找不到时的应急数据源）。
    director 留空 → import 时用 API 的 director_ja（JSON 里的 director 可能已换成豆瓣中文名）。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    films = []
    for f in data.get("films", []):
        films.append({
            "code": f["code"], "category": f.get("category") or "", "title": f.get("titleJp") or "",
            "director": "", "director_original": "", "premiere": f.get("premiere") or "",
            "copyright": f.get("copyright") or "", "has_trailer": bool(f.get("trailer")),
            "is_event": bool(f.get("isEvent")),
            "shows": [{"date": x["date"], "time": x.get("start") or "", "free": bool(x.get("free")),
                       "prereg": bool(x.get("prereg"))} for x in f.get("screenings") or []],
        })
    return films


def import_films(html_path: Optional[Path], use_api: bool = True, delay: float = 0.3,
                 films: Optional[list] = None) -> dict:
    """HTML → tiff_films / tiff_screenings（API 补全）。匹配字段（eiga_* / douban_*）保留不动，
    可重复运行（官网排片有变动时重新另存 HTML 再跑一遍即可）。films 可直接传入（见 films_from_json）。"""
    if films is None:
        films = parse_list_html(html_path)
    conn = get_db()
    now = _now()
    n_api = n_show = 0
    try:
        for i, f in enumerate(films, 1):
            code = f["code"]
            api = _api_get(f"films/{code}") if use_api else None
            if use_api:
                time.sleep(delay)
            if api:
                n_api += 1
            a = api or {}
            dept = a.get("department") or {}
            cast = [p.get("name_ja") for p in (a.get("performers") or []) if p.get("name_ja")]
            row = {
                "film_code": code,
                "category": f["category"] or dept.get("name_ja") or "",
                "category_en": dept.get("name_en") or "",
                "dept_subtitle": a.get("department_subtitle_ja") or "",
                "title_ja": f["title"] or a.get("title_ja") or code,
                "title_en": a.get("title_en") or "",
                "title_original": a.get("original_title") or "",
                "director_ja": f["director"] or a.get("director_ja") or "",
                "director_en": a.get("director_en") or "",
                "director_original": f["director_original"] or a.get("director_original") or "",
                "cast_ja": " / ".join(cast[:10]),
                "synopsis_ja": _clean_html(a.get("exp_ja")),
                "duration": _int(a.get("film_time_length")),
                "year": _int(a.get("made_year")),
                "country": a.get("made_in_ja") or "",
                "language": a.get("film_lang_ja") or "",
                "premiere": f["premiere"] or a.get("premier_ja") or "",
                "copyright": f["copyright"] or _clean_html(a.get("copyrights_ja")),
                "image_url": f"{SITE}/img/lineup/cut/{code}_1.jpg",
                "video_id": a.get("video_identifier") or "",
                # 活动判定：列表里没有导演（官网对讲座/仪式隐藏导演栏）；JSON 还原时沿用原判定
                "is_event": (1 if f["is_event"] else 0) if "is_event" in f
                            else (0 if (f["director"] or a.get("director_ja")) else 1),
                "api_ok": 1 if api else 0,
                "updated_at": now,
            }
            cols = list(row)
            conn.execute(
                f"INSERT INTO tiff_films ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}) "
                f"ON CONFLICT(film_code) DO UPDATE SET "
                + ",".join(f"{c}=excluded.{c}" for c in cols if c != "film_code"),
                [row[c] for c in cols],
            )

            # 场次：以 HTML 为准，API 的 act 按 (日期, 开场时间) 对上后补会场等细节。
            acts = {}
            for act in a.get("acts") or []:
                d = (act.get("act_date") or "")[:10]
                acts.setdefault((d, _hhmm(act.get("play_at"))), []).append(act)
            conn.execute("DELETE FROM tiff_screenings WHERE film_code=?", (code,))
            for s in f["shows"]:
                matched = acts.pop((s["date"], s["time"]), None) or [None]
                for act in matched:  # 同一时刻多个会场（如开幕片两厅同映）各一行
                    _insert_screening(conn, code, s["date"], s["time"], s["free"], s["prereg"], act)
                    n_show += 1
            # HTML 里没有、但 API 有的场次（HTML 另存后官网追加的）也收进来
            for (d, t), lst in acts.items():
                for act in lst:
                    _insert_screening(conn, code, d, t, False, False, act)
                    n_show += 1
            if i % 20 == 0:
                conn.commit()
                logger.info("TIFF import: %d/%d", i, len(films))
        conn.commit()
    finally:
        conn.close()
    logger.info("TIFF import: %d 作品（API 补全 %d），%d 场", len(films), n_api, n_show)
    return {"films": len(films), "api_ok": n_api, "screenings": n_show}


def _insert_screening(conn, code, date, start, free, prereg, act: Optional[dict]) -> None:
    act = act or {}
    venue, area, theater = _venue_parts(act.get("venue"))
    price = _clean_html(act.get("ticket_price_ja"))
    conn.execute(
        """INSERT OR REPLACE INTO tiff_screenings
           (film_code, show_date, start_time, end_time, venue, venue_area, venue_theater,
            subtitle, price, is_free, pre_registration, sale_status, sales_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (code, date, start, _hhmm(act.get("end_at")), venue, area, theater,
         act.get("subtitle_ja") or "", price,
         1 if (free or "無料" in price) else 0, 1 if prereg else 0,
         act.get("saling_status"), act.get("sales_identifier") or ""),
    )


# ── 3. eiga.com 匹配 ──────────────────────────────────────────────────────────

# 片名里只为映画祭加的修饰，搜 eiga 前去掉
_TITLE_NOISE = [
    r"［[^］]*(?:修復|復元|リマスター)[^］]*］", r"\[[^\]]*(?:修復|復元)[^\]]*\]",
    r"\s*4K\s*デジタルリマスター版", r"\s*4Kデジタルリマスター版", r"\s*デジタルリマスター版",
    r"\s*レストア版", r"\s*4K$", r"（原題）", r"（英題）", r"\(原題\)", r"\(英題\)",
    r"\s*-劇場特別版-", r"^映画「(.+)」$", r"^劇場版", r"^劇場アニメ",
]


def clean_title(t: str) -> str:
    s = (t or "").strip()
    for pat in _TITLE_NOISE:
        if pat.startswith("^映画"):
            s = re.sub(pat, r"\1", s)
        else:
            s = re.sub(pat, "", s)
    return s.strip(" 　")


def _norm_name(s: str) -> str:
    """人名归一：NFKC，去空格/中点/等号/点，小写。"""
    s = unicodedata.normalize("NFKC", s or "").lower()
    return re.sub(r"[\s・･=＝.\-‐/／,，、]", "", s)


def _names(s: str) -> list[str]:
    return [_norm_name(x) for x in re.split(r"[、,，/／]", s or "") if _norm_name(x)]


def _director_ok(tiff_dir: str, eiga_dir: str) -> bool:
    a, b = _names(tiff_dir), _names(eiga_dir)
    if not a or not b:
        return False
    return any(x == y or (len(x) >= 3 and (x in y or y in x)) for x in a for y in b)


def _eiga_candidates(query: str, delay: float) -> list[tuple[str, str, Optional[int]]]:
    """搜 eiga → [(movie_id, 标题, 年份)]（最多 12 个，去重，按页面顺序）。"""
    from pipeline.step1_eiga import _fetch
    soup = _fetch(f"https://eiga.com/search/{quote(query, safe='')}/", delay=delay)
    if soup is None:
        return []
    out, seen = [], set()
    for a in soup.select('a[href^="/movie/"]'):
        m = re.match(r"/movie/(\d+)/$", a.get("href", ""))
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        box = a.find_parent("li") or a.parent
        ym = re.search(r"((?:19|20)\d{2})年", _text(box))
        out.append((m.group(1), _text(a), int(ym.group(1)) if ym else None))
        if len(out) >= 12:
            break
    return out


# 旧字体 → 新字体（TIFF 用原片名的旧字，eiga 多登记为新字：赤線地帶→赤線地帯 等）
_KYUJI = str.maketrans("帶殘衹瀧黑國圓廣澤濱邊體點舊會變戀龍", "帯残祇滝黒国円広沢浜辺体点旧会変恋竜")


def _eiga_queries(r, title: str) -> list[str]:
    qs = [title]
    for q in (title.translate(_KYUJI),
              re.sub(r"[（(][^）)]*[）)]", "", title).strip(),
              re.split(r"[／/]", title)[0].strip()):
        if q and q not in qs:
            qs.append(q)
    en = (r["title_en"] or "").strip()
    if en and en not in qs and "日本" not in (r["country"] or ""):
        qs.append(en)
    return qs


def _find_existing_movie(conn, title: str) -> Optional[str]:
    """主库 movies 里已有同名片（正在影院上映的 TIFF 片 / 旧片重映）→ 直接用其 eiga id。"""
    row = conn.execute(
        "SELECT movie_id FROM movies WHERE title_jp=? AND movie_id NOT LIKE '99999%' LIMIT 1",
        (title,)).fetchone()
    return row["movie_id"] if row else None


def _link_eiga(conn, r, eiga_id: str, how: str, title: Optional[str], detail: dict,
               poster: Optional[str]) -> None:
    """把 TIFF 作品挂到 eiga id：eiga 信息写进 movies；之前挂的是 99999 临时 id（只有豆瓣）
    → 用 step1 的 merge_movie_ids 把豆瓣数据等并到正式 id（tiff_films 一起级联）。"""
    _upsert_movie(conn, eiga_id, r, title=title, detail=detail, poster=poster)
    old = r["movie_id"]
    conn.execute("UPDATE tiff_films SET eiga_match=? WHERE film_code=?", (how, r["film_code"]))
    if _is_temp(old) and old != eiga_id:
        conn.commit()
        from pipeline.step1_eiga import merge_movie_ids
        merge_movie_ids(old, eiga_id)
    else:
        conn.execute("UPDATE tiff_films SET movie_id=? WHERE film_code=?", (eiga_id, r["film_code"]))
    conn.commit()


def match_eiga(delay: float = 0.8, only_missing: bool = True, codes: Optional[list] = None,
               retry_none: bool = False) -> dict:
    """为每个非活动作品找 eiga movie_id，eiga 信息写进主库 movies。

    校验：候选详情页导演与 TIFF 导演一致（且年份±2）即采用；导演缺失时要求「标题完全一致 + 年份±1」。
    """
    from pipeline.step1_eiga import _scrape_movie_detail, _scrape_eiga_poster
    conn = get_db()
    q = "SELECT * FROM tiff_films WHERE is_event=0"
    if retry_none:
        q += " AND eiga_match='none'"
    elif only_missing:
        q += " AND (eiga_match IS NULL OR eiga_match='')"
    rows = conn.execute(q).fetchall()
    if codes:
        rows = [r for r in rows if r["film_code"] in codes]
    stats = {"matched": 0, "none": 0}
    try:
        for i, r in enumerate(rows, 1):
            title = clean_title(r["title_ja"])
            tiff_dir = " / ".join(x for x in (r["director_ja"], r["director_original"]) if x)
            # 选集类（「監督：A『x』／B『y』」）导演字段很长，不做匹配
            if len(r["director_ja"] or "") > 60:
                conn.execute("UPDATE tiff_films SET eiga_match='none' WHERE film_code=?", (r["film_code"],))
                conn.commit()
                stats["none"] += 1
                continue

            picked, ptitle, detail = None, None, {}
            existing = _find_existing_movie(conn, r["title_ja"]) or _find_existing_movie(conn, title)
            tried: set = set()
            fetched = 0
            for qi, query in enumerate(_eiga_queries(r, title)):
                cands = [(existing, title, None)] if (existing and qi == 0) else []
                cands += _eiga_candidates(query, delay)
                # 年份近的先查详情（「生きる」这类常见片名，1952 年版不一定排在前面）
                if r["year"]:
                    cands.sort(key=lambda c: abs(c[2] - r["year"]) if c[2] else 50)
                for mid, ctitle, cyear in cands:
                    if mid in tried or fetched >= 8:
                        continue
                    tried.add(mid)
                    # 搜索结果的年份是日本公开日：差太远的不浪费请求（旧片重映除外，可能差几十年）
                    if r["year"] and cyear and abs(cyear - r["year"]) > 3 and r["year"] >= 2015:
                        continue
                    fetched += 1
                    d = _scrape_movie_detail(mid, delay=delay)
                    if not d:
                        continue
                    if _director_ok(tiff_dir, d.get("director", "")):
                        # 同一导演的别的作品（DAU 项目的「DAU. ナターシャ」等）：年份差 >2 不要
                        ey = d.get("year") or cyear
                        if r["year"] and ey and abs(int(ey) - int(r["year"])) > 2:
                            continue
                        picked, ptitle, detail = mid, ctitle, d
                        break
                    y = d.get("year") or cyear
                    same_title = _norm_name(clean_title(ctitle)) in (_norm_name(title), _norm_name(query))
                    if (not d.get("director") or not tiff_dir) and same_title and r["year"] and y \
                            and abs(int(y) - int(r["year"])) <= 1:
                        picked, ptitle, detail = mid, ctitle, d
                        break
                if picked or fetched >= 8:
                    break

            if picked:
                poster = _scrape_eiga_poster(picked, delay=delay)
                _link_eiga(conn, r, picked, "auto", ptitle, detail, poster)
                stats["matched"] += 1
                logger.info("[eiga %d/%d] ✅ %s → %s", i, len(rows), r["title_ja"], picked)
            else:
                conn.execute("UPDATE tiff_films SET eiga_match='none' WHERE film_code=?", (r["film_code"],))
                conn.commit()
                stats["none"] += 1
                logger.info("[eiga %d/%d] ❌ %s", i, len(rows), r["title_ja"])
    finally:
        conn.close()
    logger.info("TIFF eiga 匹配: %s", stats)
    return stats


def _eiga_title(eiga_id: str, delay: float) -> Optional[str]:
    from pipeline.step1_eiga import _fetch
    soup = _fetch(f"https://eiga.com/movie/{eiga_id}/", delay=delay)
    h1 = soup.select_one("h1") if soup else None
    return h1.get_text(strip=True) if h1 else None


def set_eiga(code: str, eiga_id: str, delay: float = 0.5) -> None:
    """人工指定 eiga movie_id（eiga_id='none' 表示确认 eiga 没有该片：解除挂接，
    豆瓣之后按 99999 临时 id 重新匹配）。"""
    from pipeline.step1_eiga import _scrape_movie_detail, _scrape_eiga_poster
    conn = get_db()
    try:
        r = conn.execute("SELECT * FROM tiff_films WHERE film_code=?", (code,)).fetchone()
        if not r:
            raise ValueError(f"未知作品编号 {code}")
        if eiga_id == "none":
            conn.execute("UPDATE tiff_films SET movie_id=CASE WHEN movie_id LIKE '99999%' "
                         "THEN movie_id END, eiga_match='none', douban_attempts=0 WHERE film_code=?", (code,))
            conn.commit()
        else:
            d = _scrape_movie_detail(eiga_id, delay=delay)
            _link_eiga(conn, r, eiga_id, "manual", _eiga_title(eiga_id, delay), d,
                       _scrape_eiga_poster(eiga_id, delay=delay))
    finally:
        conn.close()


# ── 4. 豆瓣匹配 ───────────────────────────────────────────────────────────────

def _douban_fetch(subject_id: str, delay: float) -> tuple:
    """抓豆瓣页面 → (soup, 校验用字段 dict)。拿不到（bot 页等）返回 (None, None)。"""
    from scraper.douban import _fetch_movie_page, _parse_rating, _parse_meta
    soup = _fetch_movie_page(subject_id, delay=delay)
    if soup is None:
        return None, None
    score, votes = _parse_rating(soup)
    meta = _parse_meta(soup)
    if not score and not meta.get("title_cn") and not meta.get("director"):
        return None, None
    return soup, {
        "douban_id": subject_id, "title_cn": meta.get("title_cn", ""),
        "score": score, "votes": votes, "director": meta.get("director", ""),
        "cast": meta.get("cast", ""), "genre": meta.get("genre", ""),
        "year": meta.get("year"), "country": meta.get("country", ""),
    }


# 日文汉字 → 简体（只收导演名里常见的字）。用于豆瓣中文导演名 vs TIFF 日文/原文导演名的汉字比对。
_JP2CN = str.maketrans(
    "黒澤沢溝島嶋紀貴龍瀬渕進彌邉邊辺實実細宮鈴綾橋広廣長東間関陽輝達義樹頼鳥齋斎恵岡鯨識錦織愛尋謙鳳儀"
    "國圓瀧濱體點會變戀覺勝邦條萬與壽榮聖觀劉陳張楊趙黃鄭謝韓馮蕭鍾葉蘇鄧潘賴錢顧孫譚賈",
    "黑泽泽沟岛岛纪贵龙濑渊进弥边边边实实细宫铃绫桥广广长东间关阳辉达义树赖鸟斋斋惠冈鲸识锦织爱寻谦凤仪"
    "国圆泷滨体点会变恋觉胜邦条万与寿荣圣观刘陈张杨赵黄郑谢韩冯萧钟叶苏邓潘赖钱顾孙谭贾")
_CJK_RE = re.compile(r"[㐀-䶿一-鿿]")


def _cjk_set(s: str) -> set:
    return set(_CJK_RE.findall((s or "").translate(_JP2CN)))


def _director_conflict(r, douban_director: str) -> bool:
    """两边导演名都有汉字时，TIFF 导演的汉字在豆瓣导演名里出现不到 1/3 → 判定不是同一人。
    （片假名 / 拉丁字母转写对不上汉字译名，这种情况不判，返回 False。）"""
    k = _cjk_set(f"{r['director_ja'] or ''}{r['director_original'] or ''}")
    d = _cjk_set(douban_director)
    if not k or not d:
        return False
    return len(k & d) / len(k) < 0.34


def _douban_ok(r, d: dict) -> tuple[bool, str]:
    """豆瓣候选是否就是这部 TIFF 作品：年份 ±1（2000 年前的老片 ±2）+ 国家至少一个对得上
    + 汉字导演名对得上（同国同年的别的片，如续集 / 同名 TV 剧 / 同季动画）。
    豆瓣没给对应字段时视为无法否定（放行）。"""
    ty, dy = r["year"] or 0, _int(d.get("year")) or 0
    if ty and dy and abs(ty - dy) > (2 if ty < 2000 else 1):
        return False, f"年份 {ty}≠{dy}"
    if _director_conflict(r, d.get("director") or ""):
        return False, f"导演 {r['director_ja']}≠{d.get('director')}"
    from pipeline.step2_douban import _check_country_match
    tc = _movie_country(r["country"])
    if tc and d.get("country") and not _check_country_match(tc, d["country"]):
        return False, f"国家 {r['country']}≠{d['country']}"
    return True, ""


def _main_scope_ids(conn) -> set:
    """正在东京影院上映、归日常 step2 管的 movie_id（TIFF 不去动它们的豆瓣匹配）。"""
    try:
        from pipeline.step2_douban import _in_scope_movie_ids
        return _in_scope_movie_ids(conn)
    except Exception:
        return set()


def revalidate_douban() -> list[str]:
    """用主库里存的豆瓣国家/年份/导演，重新校验 TIFF 作品挂着的自动匹配；不符的删掉、
    重置搜索次数，下次 match_douban 重搜。人工匹配（manual=1）和正在影院上映（归 step2 管）
    的不动。返回被清掉的作品编号。"""
    conn = get_db()
    bad = []
    try:
        scope = _main_scope_ids(conn)
        for r in conn.execute(
                """SELECT f.*, m.douban_id AS d_id, m.title_cn AS d_title,
                          m.douban_year AS d_year, m.douban_country AS d_country,
                          COALESCE(NULLIF(dd.director,''), m.director) AS d_director
                     FROM tiff_films f
                     JOIN douban_matches m ON m.movie_id = f.movie_id
                LEFT JOIN douban_details dd ON dd.movie_id = f.movie_id
                    WHERE COALESCE(m.douban_id,'')!='' AND COALESCE(m.manual,0)=0"""):
            if r["movie_id"] in scope:
                continue
            ok, why = _douban_ok(r, {"year": r["d_year"], "country": r["d_country"],
                                     "director": r["d_director"]})
            if not ok:
                bad.append((r["film_code"], r["movie_id"]))
                logger.info("[douban 复核] ✖ %s ← %s %s（%s）", r["title_ja"], r["d_id"],
                            r["d_title"], why)
        for code, mid in bad:
            _drop_douban(conn, mid)
            conn.execute("UPDATE tiff_films SET douban_attempts=0 WHERE film_code=?", (code,))
        conn.commit()
    finally:
        conn.close()
    return [c for c, _ in bad]


def match_douban(delay: float = 2.0, refresh_scores: bool = False, codes: Optional[list] = None) -> dict:
    """为非活动作品匹配豆瓣，结果写进主库 douban_matches / douban_details（同 step2）。
    movie_id 在主库已有豆瓣匹配的（step2 做过 / 之前匹配过）直接用，不再搜（refresh_scores=True
    时重抓分数，正在影院上映的交给 step2 不重复抓）。搜不到的记 tiff_films.douban_attempts，
    满 MAX_DOUBAN_ATTEMPTS 次后不再自动搜。"""
    from scraper.douban import search_douban, init_douban_session, CookieExpiredError
    from pipeline.step2_douban import _load_config, _save_douban_details

    cfg = _load_config()
    if cfg.get("douban"):
        init_douban_session(cfg["douban"])

    revalidate_douban()  # 先清掉校验不过的旧自动匹配，本轮重搜
    conn = get_db()
    rows = conn.execute("SELECT * FROM tiff_films WHERE is_event=0").fetchall()
    if codes:
        rows = [r for r in rows if r["film_code"] in codes]
    stats = {"linked": 0, "auto": 0, "refreshed": 0, "none": 0, "skipped": 0}
    searches = 0
    today = _now()[:10]
    scope = _main_scope_ids(conn) if refresh_scores else set()
    try:
        for i, r in enumerate(rows, 1):
            code, mid = r["film_code"], r["movie_id"]
            if _has_douban(conn, mid):
                stats["linked"] += 1
                if refresh_scores and mid not in scope:
                    m = conn.execute("SELECT douban_id, score_checked_at FROM douban_matches "
                                     "WHERE movie_id=?", (mid,)).fetchone()
                    if m["score_checked_at"] != today:
                        soup, d = _douban_fetch(m["douban_id"], delay)
                        if soup is not None:
                            _save_douban_details(conn, mid, m["douban_id"], soup, r["year"],
                                                 _movie_country(r["country"]), _now())
                            conn.execute("UPDATE douban_matches SET score_checked_at=? WHERE movie_id=?",
                                         (today, mid))
                            conn.commit()
                            stats["refreshed"] += 1
                continue

            if (r["douban_attempts"] or 0) >= MAX_DOUBAN_ATTEMPTS or len(r["director_ja"] or "") > 60:
                stats["skipped"] += 1
                continue

            searches += 1
            if searches > 1:
                time.sleep(random.uniform(1.5, 4.0))
            if searches > 1 and (searches - 1) % 5 == 0:
                time.sleep(random.uniform(10, 20))

            title = clean_title(r["title_ja"])
            # 搜索词依次：原题 / 英题 → 日文片名。候选必须过 _douban_ok（年份 + 国家 + 汉字导演）校验，
            # 否则换下一个搜索词。英题容易撞到别的同名片（記憶ゼロ = "Arrested Memory"
            # 曾被匹配到法国纪录片「童年记忆」），所以不能「搜到就算」。
            # orig_title 传日文片名本身 = 只按日文搜（不传会让 search_douban 再去 eiga 查原题）。
            queries = []
            for q in ((r["title_original"] or "").strip(), (r["title_en"] or "").strip(), title):
                if q and q not in queries:
                    queries.append(q)
            found, rejected = None, []
            for q in queries:
                try:
                    match = search_douban(title, year=r["year"] or 0, delay=delay,
                                          orig_title=q, country=r["country"] or None)
                except CookieExpiredError:
                    logger.error("豆瓣 cookie 失效，停止。")
                    raise
                except Exception as exc:
                    logger.warning("[douban] %s 搜索异常: %s", r["title_ja"], exc)
                    match = None
                sid = (match or {}).get("id")
                if not sid or sid in rejected:
                    continue
                soup, cand = _douban_fetch(sid, delay)
                if cand is None:
                    # 页面拿不到：用 suggest 结果（只有年份可校验），分数下次 refresh 补
                    rating = match.get("rating") or {}
                    cand = {"douban_id": sid, "title_cn": match.get("title", ""),
                            "score": float(rating.get("value") or 0),
                            "votes": int(rating.get("count") or 0),
                            "year": _int(match.get("year"))}
                ok, why = _douban_ok(r, cand)
                if ok:
                    found = (soup, cand)
                    break
                rejected.append(sid)
                logger.info("[douban] ⚠️ %s 候选 %s %s 不符（%s），换搜索词",
                            r["title_ja"], sid, cand.get("title_cn"), why)

            if not found:
                conn.execute("UPDATE tiff_films SET douban_attempts=COALESCE(douban_attempts,0)+1 "
                             "WHERE film_code=?", (code,))
                conn.commit()
                stats["none"] += 1
                logger.info("[douban %d/%d] ❌ %s", i, len(rows), r["title_ja"])
                continue
            soup, cand = found
            if not mid:
                mid = _new_temp_movie(conn, r)
            _save_douban_dict(conn, mid, r, cand, soup=soup)
            conn.commit()
            stats["auto"] += 1
            logger.info("[douban %d/%d] ✅ %s → %s %s %.1f", i, len(rows), r["title_ja"],
                        cand["douban_id"], cand.get("title_cn"), cand.get("score") or 0)
    finally:
        conn.close()
        try:
            from scraper.douban import close_pw_browser
            close_pw_browser()
        except Exception:
            pass
    logger.info("TIFF 豆瓣匹配: %s", stats)
    return stats


def set_douban(code: str, douban_id: str, delay: float = 1.5) -> None:
    """人工指定豆瓣 subject id → 走 step2 的 manual_set_match（manual=1，日常 step2 也不会动它）。
    douban_id='none' 表示确认豆瓣没有：删掉挂着的匹配，之后不再自动搜。"""
    conn = get_db()
    try:
        r = conn.execute("SELECT * FROM tiff_films WHERE film_code=?", (code,)).fetchone()
        if not r:
            raise ValueError(f"未知作品编号 {code}")
        mid = r["movie_id"]
        if douban_id == "none":
            if mid:
                _drop_douban(conn, mid)
            conn.execute("UPDATE tiff_films SET douban_attempts=? WHERE film_code=?", (DOUBAN_NONE, code))
            conn.commit()
            return
        if not mid:
            mid = _new_temp_movie(conn, r)
        conn.commit()
    finally:
        conn.close()
    from pipeline.step2_douban import manual_set_match
    manual_set_match(mid, douban_id, delay=delay)


# ── 5. 导出 / 上传 ─────────────────────────────────────────────────────────────

def _split(s: Optional[str], n: int) -> str:
    parts = [p.strip() for p in re.split(r"\s*/\s*", s or "") if p.strip()]
    return " / ".join(parts[:n])


def _linked(conn) -> tuple[dict, dict, dict]:
    """→ (movies, douban_details, douban_matches)，各按 movie_id 索引，只取 TIFF 挂着的。"""
    sub = "SELECT movie_id FROM tiff_films WHERE movie_id IS NOT NULL"
    movies = {r["movie_id"]: dict(r) for r in conn.execute(f"SELECT * FROM movies WHERE movie_id IN ({sub})")}
    details, matches = {}, {}
    try:
        details = {r["movie_id"]: dict(r) for r in
                   conn.execute(f"SELECT * FROM douban_details WHERE movie_id IN ({sub})")}
        matches = {r["movie_id"]: dict(r) for r in
                   conn.execute(f"SELECT * FROM douban_matches WHERE movie_id IN ({sub}) "
                                "AND COALESCE(douban_id,'')!=''")}
    except sqlite3.OperationalError:
        pass  # 新库还没有豆瓣表
    return movies, details, matches


def build_json() -> dict:
    """→ 小程序 tiff.json:
    {festival, dates:[...], films:[{code, titleJp, titleCn, ..., screenings:[...]}]}
    场次按 film 挂载，小程序按日期 tab 自行分组。eiga / 豆瓣信息按 movie_id 从主库取，
    与 step4（区 JSON）同一份数据、同一个「豆瓣匹配到就全用豆瓣」规则。"""
    from pipeline.step4_export import _review_texts
    conn = get_db()
    try:
        shows: dict = {}
        for s in conn.execute("SELECT * FROM tiff_screenings ORDER BY show_date, start_time, venue"):
            st = s["sale_status"]
            shows.setdefault(s["film_code"], []).append({
                "date": s["show_date"],
                "start": s["start_time"],
                "end": s["end_time"] or "",
                "venue": s["venue"] or "",
                "area": s["venue_area"] or "",
                "theater": s["venue_theater"] or "",
                "subtitle": s["subtitle"] or "",
                "price": s["price"] or "",
                "free": bool(s["is_free"]),
                "prereg": bool(s["pre_registration"]),
                "saleStatus": st,
                "ticketUrl": (TICKET_PREFIX + s["sales_id"]) if s["sales_id"] and st in (1, 2) else "",
            })
        movies, details, matches = _linked(conn)
        films = []
        for r in conn.execute("SELECT * FROM tiff_films ORDER BY film_code"):
            mid = r["movie_id"]
            m = movies.get(mid) or {}
            dd = details.get(mid) or {}
            dm = matches.get(mid) or {}
            douban_id = dm.get("douban_id") or dd.get("douban_id") or ""
            matched = bool(douban_id)
            tiff_dir = " / ".join(x for x in (r["director_ja"], r["director_original"] and
                                              f"({r['director_original']})") if x)
            films.append({
                "code": r["film_code"],
                "category": r["category"] or "",
                "categoryEn": r["category_en"] or "",
                "deptSubtitle": r["dept_subtitle"] or "",
                "titleJp": r["title_ja"],
                "titleEn": r["title_en"] or "",
                "titleOriginal": r["title_original"] or "",
                "titleCn": (dd.get("title_cn") or dm.get("title_cn") or "") if matched else "",
                "isEvent": bool(r["is_event"]),
                "premiere": r["premiere"] or "",
                "duration": r["duration"] or m.get("duration") or 0,
                "year": r["year"] or m.get("year") or 0,
                "country": r["country"] or "",
                "language": r["language"] or "",
                # 豆瓣有就用豆瓣（中文），没有用 TIFF 日文
                "director": ((dd.get("director") or dm.get("director")) if matched else "") or tiff_dir,
                "cast": (_split(dd.get("cast") or dm.get("cast"), 5) if matched else "")
                        or _split(r["cast_ja"], 6),
                "genre": (dd.get("genre") or dm.get("genre") or "") if matched else "",
                "synopsis": r["synopsis_ja"] or "",
                "image": r["image_url"] or "",
                "poster": m.get("poster_url") or dd.get("poster_url") or "",
                "copyright": r["copyright"] or "",
                "trailer": f"https://www.youtube.com/watch?v={r['video_id']}" if r["video_id"] else "",
                "tiffUrl": f"{SITE}/ja/lineup/film/{r['film_code']}",
                # 临时 99999 id 不是 eiga 页面，不输出
                "eigaId": mid if (mid and not _is_temp(mid)) else "",
                "eigaRating": m.get("eiga_rating") or 0,
                "eigaRatingCount": m.get("eiga_rating_count") or 0,
                "doubanId": douban_id,
                "doubanScore": round(float(dd.get("douban_score") or dm.get("douban_score") or 0), 1),
                "doubanVotes": dd.get("douban_votes") or dm.get("douban_votes") or 0,
                "doubanWatched": dd.get("watched") or 0,
                "doubanWish": dd.get("want_to_watch") or 0,
                "doubanComments": _review_texts(dd.get("short_reviews"), 3),
                "screenings": shows.get(r["film_code"], []),
            })
    finally:
        conn.close()
    dates = sorted({s["date"] for f in films for s in f["screenings"]})
    return {
        "festival": {"name": FESTIVAL_NAME, "start": FESTIVAL_START, "end": FESTIVAL_END,
                     "site": f"{SITE}/ja/"},
        "generatedAt": _now(),
        # 数据日期（JST）：小程序 TIFF 页默认选中这一天的 tab，也用来判断数据新旧
        "dataDate": _today_jst(),
        "dates": dates,
        "films": films,
    }


def export(out_dir: Path, upload: bool, config: Optional[dict] = None) -> dict:
    data = build_json()
    out_dir.mkdir(parents=True, exist_ok=True)
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    p = out_dir / CLOUD_JSON
    p.write_bytes(body)
    # 版本号只取内容（去掉 generatedAt），内容不变 → 版本不变 → 小程序继续用缓存
    ver_src = dict(data)
    ver_src.pop("generatedAt", None)
    ver = hashlib.md5(json.dumps(ver_src, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    meta = out_dir / CLOUD_META
    meta.write_text(json.dumps({"ver": ver}), encoding="utf-8")
    logger.info("TIFF export: %d films, %d 场, %d bytes → %s", len(data["films"]),
                sum(len(f["screenings"]) for f in data["films"]), len(body), p)
    result = {"file": str(p), "ver": ver, "file_ids": {}}
    if upload:
        from pipeline import wxcloud
        mp = wxcloud.mp_cfg(config or {})
        env = mp.get("cloud_env", "")
        if not env:
            raise RuntimeError("miniprogram.cloud_env 未配置")
        prefix = (mp.get("storage_prefix", "data") or "data").strip("/")
        token = wxcloud.get_access_token(config)
        # 先传数据再传版本号：小程序看到新版本号时数据一定已就位
        result["file_ids"][CLOUD_JSON] = wxcloud.upload(env, token, f"{prefix}/{CLOUD_JSON}", body)
        result["file_ids"][CLOUD_META] = wxcloud.upload(env, token, f"{prefix}/{CLOUD_META}",
                                                        meta.read_bytes())
    return result


def report() -> str:
    """匹配情况一览（供人工核对 / 手动修正）。eiga 列 = movie_id（99999 开头为 eiga 未登记的临时 id）。"""
    conn = get_db()
    try:
        movies, details, matches = _linked(conn)
        rows = conn.execute("SELECT * FROM tiff_films WHERE is_event=0 ORDER BY film_code").fetchall()
        lines = [f"{'code':<12} {'eiga':<10} {'douban':<10} {'score':>5}  title / douban title"]
        ne = nd = 0
        for r in rows:
            mid = r["movie_id"]
            dm = matches.get(mid) or {}
            dd = details.get(mid) or {}
            did = dm.get("douban_id") or ""
            ne += 1 if (mid and not _is_temp(mid)) else 0
            nd += 1 if did else 0
            flag = "*" if dm.get("manual") else " "
            lines.append(f"{r['film_code']:<12} {(mid or '-'):<10} {(did or '-'):<9}{flag} "
                         f"{float(dd.get('douban_score') or dm.get('douban_score') or 0):>5.1f}  "
                         f"{r['title_ja']}  /  {dd.get('title_cn') or dm.get('title_cn') or ''}")
        lines.append(f"\n作品 {len(rows)}：eiga 匹配 {ne}，豆瓣匹配 {nd}（* = 人工匹配）")
        return "\n".join(lines)
    finally:
        conn.close()


def restore_matches_from_json(path: Path) -> dict:
    """应急：从导出的 tiff.json 还原（tiff_films 为空时先用 JSON 的作品/场次清单重建），
    再把 eiga / 豆瓣匹配灌回主库 movies / douban_*。主库已有的不覆盖。"""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    conn = get_db()
    empty = conn.execute("SELECT COUNT(*) FROM tiff_films").fetchone()[0] == 0
    conn.close()
    if empty:
        logger.info("tiff_films 为空 → 从 %s 重建作品与场次", path)
        import_films(None, films=films_from_json(path))
    conn = get_db()
    stats = {"eiga": 0, "douban": 0, "missing": 0}
    try:
        for f in data.get("films", []):
            r = conn.execute("SELECT * FROM tiff_films WHERE film_code=?", (f["code"],)).fetchone()
            if not r:
                stats["missing"] += 1
                continue
            if f.get("isEvent"):
                continue
            mid = r["movie_id"]
            eid = f.get("eigaId") or None
            if eid and not mid:
                _upsert_movie(conn, eid, r, detail={"eiga_rating": f.get("eigaRating") or None,
                                                    "eiga_rating_count": f.get("eigaRatingCount") or None},
                              poster=f.get("poster"))
                conn.execute("UPDATE tiff_films SET movie_id=?, eiga_match='auto' WHERE film_code=?",
                             (eid, f["code"]))
                mid = eid
                stats["eiga"] += 1
            elif not eid and not r["eiga_match"]:
                conn.execute("UPDATE tiff_films SET eiga_match='none' WHERE film_code=?", (f["code"],))
            did = f.get("doubanId")
            if did and not _has_douban(conn, mid):
                if not mid:
                    mid = _new_temp_movie(conn, r)
                _save_douban_dict(conn, mid, r, {
                    "douban_id": did, "title_cn": f.get("titleCn"), "score": f.get("doubanScore"),
                    "votes": f.get("doubanVotes"), "director": f.get("director"),
                    "cast": f.get("cast"), "genre": f.get("genre"), "watched": f.get("doubanWatched"),
                    "wish": f.get("doubanWish"), "poster": None if eid else f.get("poster"),
                    "reviews": f.get("doubanComments") or []})
                stats["douban"] += 1
            elif not did and not (r["douban_attempts"] or 0):
                conn.execute("UPDATE tiff_films SET douban_attempts=1 WHERE film_code=?", (f["code"],))
        conn.commit()
    finally:
        conn.close()
    return stats
