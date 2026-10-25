"""東京国際映画祭 (TIFF) 板块：上映作品 + 排片 → DB → 小程序 JSON.

数据流（独立于 step1~4，不碰 movies / douban_matches 这些以 eiga movie_id 为键的表）:

  1. parse_list_html   解析 TIFF 官网「上映作品・イベント一覧」页面 HTML
                       (https://2026.tiff-jp.net/ja/lineup/list.html 浏览器另存)
                       → 作品清单 + 每场上映日期/开场时间/免费/事前申込。这是作品与场次的「真源」。
  2. enrich_from_api   逐片调官网背后的公开 JSON API (api-2026.tiff-jp.net/api/films/{code})
                       补 HTML 里没有的：会场、结束时间、字幕、票价、售票状态/购票链接、
                       英文/原题、制作年、国家、片长、导演/演员、简介。API 失败不致命，只用 HTML。
  3. match_eiga        用片名搜 eiga.com，按「导演一致 / 年份±1」校验候选，记 eiga movie_id，
                       顺带抓 eiga 评分 / 海报 / 日文导演演员（复用 step1 的详情抓取）。
  4. match_douban      eiga 匹配到的片若主库已有豆瓣数据（douban_details / douban_matches）直接复用；
                       否则用 scraper.douban.search_douban 搜（原题/英题优先），抓页面取分数/短评。
  5. export / upload   生成 tiff.json（作品 + 场次）+ tiff.meta.json（版本哈希），上传云存储 data/。

表:
  tiff_films       1 行 = 1 个上映作品/活动（film_code 主键，如 39005WFC15）
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

_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
    "Accept-Language": "ja,en;q=0.8",
    "Referer": f"{SITE}/ja/lineup/list.html",
}


# ── DB ────────────────────────────────────────────────────────────────────────

def get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
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
            country          TEXT,
            language         TEXT,
            premiere         TEXT,              -- ワールド・プレミア 等
            copyright        TEXT,
            image_url        TEXT,              -- 官网剧照 /img/lineup/cut/{code}_1.jpg (横版)
            video_id         TEXT,              -- YouTube 预告片 id
            is_event         INTEGER NOT NULL DEFAULT 0,  -- 1=讲座/仪式等非电影（不做 eiga/豆瓣匹配）
            api_ok           INTEGER NOT NULL DEFAULT 0,  -- 1=官网 API 补全成功
            -- eiga.com 匹配
            eiga_movie_id    TEXT,
            eiga_match       TEXT,              -- auto / manual / none
            eiga_rating      REAL,
            eiga_rating_count INTEGER,
            eiga_poster      TEXT,
            eiga_director    TEXT,
            eiga_cast        TEXT,
            -- 豆瓣匹配
            douban_id        TEXT,
            douban_match     TEXT,              -- auto / reuse / manual / none
            douban_attempts  INTEGER NOT NULL DEFAULT 0,
            douban_title_cn  TEXT,
            douban_score     REAL,
            douban_votes     INTEGER,
            douban_director  TEXT,
            douban_cast      TEXT,
            douban_genre     TEXT,
            douban_year      INTEGER,
            douban_country   TEXT,
            douban_watched   INTEGER,
            douban_wish      INTEGER,
            douban_poster    TEXT,
            douban_reviews   TEXT,              -- JSON [text,...]
            douban_checked_at TEXT,
            updated_at       TEXT
        );
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
    conn.commit()
    return conn


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today_jst() -> str:
    from datetime import timezone, timedelta
    return datetime.now(timezone(timedelta(hours=9))).date().isoformat()


_TABLES = ("tiff_films", "tiff_screenings")


def snapshot_tables() -> dict:
    """pull_db 是整文件覆盖：云端库若还没有 TIFF 表（或较旧），本地 TIFF 数据会被冲掉。
    pull 前先抓下两张表，pull 后用 restore_tables 回灌（本地优先）。"""
    snap = {}
    if not DB_PATH.exists():
        return snap
    conn = get_db()
    try:
        for t in _TABLES:
            snap[t] = [dict(r) for r in conn.execute(f"SELECT * FROM {t}")]
    finally:
        conn.close()
    return snap


def restore_tables(snap: dict) -> int:
    conn = get_db()
    n = 0
    try:
        for t in _TABLES:
            rows = snap.get(t) or []
            if not rows:
                continue
            conn.execute(f"DELETE FROM {t}")
            cols = list(rows[0])
            sql = f"INSERT OR REPLACE INTO {t} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})"
            conn.executemany(sql, [[r[c] for c in cols] for r in rows])
            n += len(rows)
        conn.commit()
    finally:
        conn.close()
    return n


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


def import_films(html_path: Path, use_api: bool = True, delay: float = 0.3) -> dict:
    """HTML → tiff_films / tiff_screenings（API 补全）。匹配字段（eiga_* / douban_*）保留不动，
    可重复运行（官网排片有变动时重新另存 HTML 再跑一遍即可）。"""
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
                # 活动判定：列表里没有导演（官网对讲座/仪式隐藏导演栏）
                "is_event": 0 if (f["director"] or a.get("director_ja")) else 1,
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


def match_eiga(delay: float = 0.8, only_missing: bool = True, codes: Optional[list] = None,
               retry_none: bool = False) -> dict:
    """为每个非活动作品找 eiga movie_id，并抓评分/海报/日文导演演员。

    校验：候选详情页导演与 TIFF 导演一致即采用；导演缺失时要求「标题完全一致 + 年份±1」。
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
                stats["none"] += 1
                continue

            picked, detail = None, {}
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
                        picked, detail = mid, d
                        break
                    y = d.get("year") or cyear
                    same_title = _norm_name(clean_title(ctitle)) in (_norm_name(title), _norm_name(query))
                    if (not d.get("director") or not tiff_dir) and same_title and r["year"] and y \
                            and abs(int(y) - int(r["year"])) <= 1:
                        picked, detail = mid, d
                        break
                if picked or fetched >= 8:
                    break

            if picked:
                poster = _scrape_eiga_poster(picked, delay=delay)
                conn.execute(
                    """UPDATE tiff_films SET eiga_movie_id=?, eiga_match='auto', eiga_rating=?,
                       eiga_rating_count=?, eiga_poster=?, eiga_director=?, eiga_cast=?,
                       year=COALESCE(year, ?), duration=COALESCE(duration, ?),
                       country=CASE WHEN COALESCE(country,'')='' THEN ? ELSE country END
                       WHERE film_code=?""",
                    (picked, detail.get("eiga_rating"), detail.get("eiga_rating_count"), poster,
                     detail.get("director"), detail.get("cast"), detail.get("year"),
                     detail.get("duration"), detail.get("country") or "", r["film_code"]))
                stats["matched"] += 1
                logger.info("[eiga %d/%d] ✅ %s → %s", i, len(rows), r["title_ja"], picked)
            else:
                conn.execute("UPDATE tiff_films SET eiga_match='none' WHERE film_code=?", (r["film_code"],))
                stats["none"] += 1
                logger.info("[eiga %d/%d] ❌ %s", i, len(rows), r["title_ja"])
            conn.commit()
    finally:
        conn.close()
    logger.info("TIFF eiga 匹配: %s", stats)
    return stats


def set_eiga(code: str, eiga_id: str, delay: float = 0.5) -> None:
    """人工指定 eiga movie_id（eiga_id='none' 表示确认没有）。"""
    from pipeline.step1_eiga import _scrape_movie_detail, _scrape_eiga_poster
    conn = get_db()
    try:
        if eiga_id == "none":
            conn.execute("UPDATE tiff_films SET eiga_movie_id=NULL, eiga_match='none' WHERE film_code=?", (code,))
        else:
            d = _scrape_movie_detail(eiga_id, delay=delay)
            conn.execute(
                """UPDATE tiff_films SET eiga_movie_id=?, eiga_match='manual', eiga_rating=?,
                   eiga_rating_count=?, eiga_poster=?, eiga_director=?, eiga_cast=? WHERE film_code=?""",
                (eiga_id, d.get("eiga_rating"), d.get("eiga_rating_count"),
                 _scrape_eiga_poster(eiga_id, delay=delay), d.get("director"), d.get("cast"), code))
        conn.commit()
    finally:
        conn.close()


# ── 4. 豆瓣匹配 ───────────────────────────────────────────────────────────────

def _reuse_main_douban(conn, eiga_id: Optional[str]) -> Optional[dict]:
    """eiga 匹配到的片在主库已有豆瓣数据 → 直接复用（不发请求）。"""
    if not eiga_id:
        return None
    row = None
    try:
        row = conn.execute("SELECT * FROM douban_details WHERE movie_id=?", (eiga_id,)).fetchone()
    except sqlite3.OperationalError:
        pass
    m = None
    try:
        m = conn.execute("SELECT * FROM douban_matches WHERE movie_id=? AND COALESCE(douban_id,'')!=''",
                         (eiga_id,)).fetchone()
    except sqlite3.OperationalError:
        pass
    if not row and not m:
        return None
    d = dict(row) if row else {}
    mm = dict(m) if m else {}
    reviews = []
    try:
        from pipeline.step4_export import _review_texts
        reviews = _review_texts(d.get("short_reviews"), 3)
    except Exception:
        pass
    return {
        "douban_id": d.get("douban_id") or mm.get("douban_id"),
        "title_cn": d.get("title_cn") or mm.get("title_cn") or "",
        "score": d.get("douban_score") or mm.get("douban_score") or 0,
        "votes": d.get("douban_votes") or mm.get("douban_votes") or 0,
        "director": d.get("director") or mm.get("director") or "",
        "cast": d.get("cast") or mm.get("cast") or "",
        "genre": d.get("genre") or mm.get("genre") or "",
        "year": d.get("douban_year") or mm.get("douban_year"),
        "country": d.get("douban_country") or mm.get("douban_country") or "",
        "watched": d.get("watched") or 0,
        "wish": d.get("want_to_watch") or 0,
        "poster": d.get("poster_url") or "",
        "reviews": reviews,
    }


def _douban_page(subject_id: str, delay: float) -> Optional[dict]:
    """抓豆瓣页面 → 分数/元数据/短评。拿不到（bot 页等）返回 None。"""
    from scraper.douban import _fetch_movie_page, _parse_rating, _parse_meta
    from pipeline.step2_douban import _select_reviews_from_soup
    soup = _fetch_movie_page(subject_id, delay=delay)
    if soup is None:
        return None
    score, votes = _parse_rating(soup)
    meta = _parse_meta(soup)
    if not score and not meta.get("title_cn") and not meta.get("director"):
        return None
    try:
        reviews = [x.get("text", "") if isinstance(x, dict) else str(x)
                   for x in _select_reviews_from_soup(soup, score_present=bool(score))]
    except Exception:
        reviews = []
    return {
        "douban_id": subject_id, "title_cn": meta.get("title_cn", ""),
        "score": score, "votes": votes, "director": meta.get("director", ""),
        "cast": meta.get("cast", ""), "genre": meta.get("genre", ""),
        "year": meta.get("year"), "country": meta.get("country", ""),
        "watched": meta.get("watched", 0) or 0, "wish": meta.get("want_to_watch", 0) or 0,
        "poster": meta.get("poster", ""), "reviews": [t for t in reviews if t][:3],
    }


def _save_douban(conn, code: str, d: dict, how: str) -> None:
    conn.execute(
        """UPDATE tiff_films SET douban_id=?, douban_match=?, douban_title_cn=?, douban_score=?,
           douban_votes=?, douban_director=?, douban_cast=?, douban_genre=?, douban_year=?,
           douban_country=?, douban_watched=?, douban_wish=?, douban_poster=?, douban_reviews=?,
           douban_checked_at=? WHERE film_code=?""",
        (d["douban_id"], how, d["title_cn"], d["score"] or 0, d["votes"] or 0, d["director"],
         d["cast"], d["genre"], d["year"], d["country"], d["watched"], d["wish"], d["poster"],
         json.dumps(d.get("reviews") or [], ensure_ascii=False), _now()[:10], code))


MAX_DOUBAN_ATTEMPTS = 2


def _douban_ok(r, d: dict) -> tuple[bool, str]:
    """豆瓣候选是否就是这部 TIFF 作品：国家至少一个对得上 + 年份 ±1（2000 年前的老片 ±2）。
    豆瓣没给国家/年份时视为无法否定（放行）。"""
    ty, dy = r["year"] or 0, _int(d.get("year")) or 0
    if ty and dy and abs(ty - dy) > (2 if ty < 2000 else 1):
        return False, f"年份 {ty}≠{dy}"
    from pipeline.step2_douban import _check_country_match
    tc = (r["country"] or "").replace("／", "・").replace("/", "・")
    if tc and d.get("country") and not _check_country_match(tc, d["country"]):
        return False, f"国家 {r['country']}≠{d['country']}"
    return True, ""


def revalidate_douban() -> list[str]:
    """用已存的豆瓣国家/年份重新校验所有自动匹配；不符的清掉、重置搜索次数，下次 match_douban 重搜。
    手动（manual）和复用主库（reuse，已在主库经人审过）的不动。返回被清掉的作品编号。"""
    conn = get_db()
    bad = []
    try:
        for r in conn.execute("SELECT * FROM tiff_films WHERE douban_match='auto' AND douban_id IS NOT NULL"):
            ok, why = _douban_ok(r, {"year": r["douban_year"], "country": r["douban_country"]})
            if not ok:
                bad.append(r["film_code"])
                logger.info("[douban 复核] ✖ %s ← %s %s（%s）", r["title_ja"], r["douban_id"],
                            r["douban_title_cn"], why)
        for code in bad:
            conn.execute(
                """UPDATE tiff_films SET douban_id=NULL, douban_match=NULL, douban_attempts=0,
                   douban_title_cn=NULL, douban_score=NULL, douban_votes=NULL, douban_director=NULL,
                   douban_cast=NULL, douban_genre=NULL, douban_year=NULL, douban_country=NULL,
                   douban_watched=NULL, douban_wish=NULL, douban_poster=NULL, douban_reviews=NULL
                   WHERE film_code=?""", (code,))
        conn.commit()
    finally:
        conn.close()
    return bad


def match_douban(delay: float = 2.0, refresh_scores: bool = False, codes: Optional[list] = None) -> dict:
    """为非活动作品匹配豆瓣。已匹配的跳过（refresh_scores=True 时重抓分数）；
    搜不到的记 douban_attempts，满 MAX_DOUBAN_ATTEMPTS 次后不再自动搜。"""
    from scraper.douban import search_douban, init_douban_session, CookieExpiredError
    from pipeline.step2_douban import _load_config

    cfg = _load_config()
    if cfg.get("douban"):
        init_douban_session(cfg["douban"])

    revalidate_douban()  # 先清掉校验不过的旧自动匹配，本轮重搜
    conn = get_db()
    rows = conn.execute("SELECT * FROM tiff_films WHERE is_event=0 AND COALESCE(douban_match,'')!='manual-none'").fetchall()
    if codes:
        rows = [r for r in rows if r["film_code"] in codes]
    stats = {"reuse": 0, "auto": 0, "refreshed": 0, "none": 0, "skipped": 0}
    searches = 0
    try:
        for i, r in enumerate(rows, 1):
            code = r["film_code"]
            if r["douban_id"]:
                if refresh_scores and r["douban_checked_at"] != _now()[:10]:
                    d = _douban_page(r["douban_id"], delay)
                    if d:
                        _save_douban(conn, code, d, r["douban_match"] or "auto")
                        conn.commit()
                        stats["refreshed"] += 1
                continue

            reused = _reuse_main_douban(conn, r["eiga_movie_id"])
            if reused and reused.get("douban_id"):
                _save_douban(conn, code, reused, "reuse")
                conn.commit()
                stats["reuse"] += 1
                logger.info("[douban %d/%d] ♻️ %s → %s", i, len(rows), r["title_ja"], reused["douban_id"])
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
            # 搜索词依次：原题 / 英题 → 日文片名。候选必须过 _douban_ok（国家 + 年份）校验，
            # 否则换下一个搜索词。英题容易撞到别的同名片（記憶ゼロ = "Arrested Memory"
            # 曾被匹配到法国纪录片「童年记忆」），所以不能「搜到就算」。
            # orig_title 传日文片名本身 = 只按日文搜（不传会让 search_douban 再去 eiga 查原题）。
            queries = []
            for q in ((r["title_original"] or "").strip(), (r["title_en"] or "").strip(), title):
                if q and q not in queries:
                    queries.append(q)
            d, rejected = None, []
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
                cand = _douban_page(sid, delay)
                if not cand:
                    # 页面拿不到：用 suggest 结果（只有年份可校验），分数下次 refresh 补
                    rating = match.get("rating") or {}
                    cand = {"douban_id": sid, "title_cn": match.get("title", ""),
                            "score": float(rating.get("value") or 0), "votes": int(rating.get("count") or 0),
                            "director": "", "cast": "", "genre": "", "year": _int(match.get("year")),
                            "country": "", "watched": 0, "wish": 0, "poster": "", "reviews": []}
                ok, why = _douban_ok(r, cand)
                if ok:
                    d = cand
                    break
                rejected.append(sid)
                logger.info("[douban] ⚠️ %s 候选 %s %s 不符（%s），换搜索词",
                            r["title_ja"], sid, cand.get("title_cn"), why)

            if not d:
                conn.execute("UPDATE tiff_films SET douban_attempts=COALESCE(douban_attempts,0)+1, "
                             "douban_match='none' WHERE film_code=?", (code,))
                conn.commit()
                stats["none"] += 1
                logger.info("[douban %d/%d] ❌ %s", i, len(rows), r["title_ja"])
                continue
            sid = d["douban_id"]
            _save_douban(conn, code, d, "auto")
            conn.commit()
            stats["auto"] += 1
            logger.info("[douban %d/%d] ✅ %s → %s %s %.1f", i, len(rows), r["title_ja"], sid,
                        d["title_cn"], d["score"] or 0)
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
    """人工指定豆瓣 subject id（douban_id='none' 表示确认没有，之后不再自动搜）。"""
    conn = get_db()
    try:
        if douban_id == "none":
            conn.execute("UPDATE tiff_films SET douban_id=NULL, douban_match='manual-none' WHERE film_code=?", (code,))
        else:
            from scraper.douban import init_douban_session
            from pipeline.step2_douban import _load_config
            cfg = _load_config()
            if cfg.get("douban"):
                init_douban_session(cfg["douban"])
            d = _douban_page(douban_id, delay) or {
                "douban_id": douban_id, "title_cn": "", "score": 0, "votes": 0, "director": "",
                "cast": "", "genre": "", "year": None, "country": "", "watched": 0, "wish": 0,
                "poster": "", "reviews": []}
            _save_douban(conn, code, d, "manual")
        conn.commit()
    finally:
        conn.close()


# ── 5. 导出 / 上传 ─────────────────────────────────────────────────────────────

def _split(s: Optional[str], n: int) -> str:
    parts = [p.strip() for p in re.split(r"\s*/\s*", s or "") if p.strip()]
    return " / ".join(parts[:n])


def build_json() -> dict:
    """→ 小程序 tiff.json:
    {festival, dates:[...], films:[{code, titleJp, titleCn, ..., screenings:[...]}]}
    场次按 film 挂载，小程序按日期 tab 自行分组。"""
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
        films = []
        for r in conn.execute("SELECT * FROM tiff_films ORDER BY film_code"):
            reviews = []
            try:
                reviews = json.loads(r["douban_reviews"] or "[]")
            except Exception:
                pass
            matched = bool(r["douban_id"])
            films.append({
                "code": r["film_code"],
                "category": r["category"] or "",
                "categoryEn": r["category_en"] or "",
                "deptSubtitle": r["dept_subtitle"] or "",
                "titleJp": r["title_ja"],
                "titleEn": r["title_en"] or "",
                "titleOriginal": r["title_original"] or "",
                "titleCn": r["douban_title_cn"] or "",
                "isEvent": bool(r["is_event"]),
                "premiere": r["premiere"] or "",
                "duration": r["duration"] or 0,
                "year": r["year"] or 0,
                "country": r["country"] or "",
                "language": r["language"] or "",
                # 豆瓣有就用豆瓣（中文），没有用 TIFF 日文
                "director": (r["douban_director"] if matched and r["douban_director"] else
                             " / ".join(x for x in (r["director_ja"], r["director_original"] and f"({r['director_original']})") if x)),
                "cast": _split(r["douban_cast"], 5) if matched and r["douban_cast"] else _split(r["cast_ja"], 6),
                "genre": r["douban_genre"] or "",
                "synopsis": r["synopsis_ja"] or "",
                "image": r["image_url"] or "",
                "poster": r["eiga_poster"] or r["douban_poster"] or "",
                "copyright": r["copyright"] or "",
                "trailer": f"https://www.youtube.com/watch?v={r['video_id']}" if r["video_id"] else "",
                "tiffUrl": f"{SITE}/ja/lineup/film/{r['film_code']}",
                "eigaId": r["eiga_movie_id"] or "",
                "eigaRating": r["eiga_rating"] or 0,
                "eigaRatingCount": r["eiga_rating_count"] or 0,
                "doubanId": r["douban_id"] or "",
                "doubanScore": round(float(r["douban_score"] or 0), 1),
                "doubanVotes": r["douban_votes"] or 0,
                "doubanWatched": r["douban_watched"] or 0,
                "doubanWish": r["douban_wish"] or 0,
                "doubanComments": reviews[:3],
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
    """匹配情况一览（供人工核对 / 手动修正）。"""
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM tiff_films WHERE is_event=0 ORDER BY film_code").fetchall()
        lines = [f"{'code':<12} {'eiga':<8} {'douban':<10} {'score':>5}  title / douban title"]
        for r in rows:
            lines.append(f"{r['film_code']:<12} {(r['eiga_movie_id'] or '-'):<8} "
                         f"{(r['douban_id'] or '-'):<10} {(r['douban_score'] or 0):>5.1f}  "
                         f"{r['title_ja']}  /  {r['douban_title_cn'] or ''}")
        n = len(rows)
        ne = sum(1 for r in rows if r["eiga_movie_id"])
        nd = sum(1 for r in rows if r["douban_id"])
        lines.append(f"\n作品 {n}：eiga 匹配 {ne}，豆瓣匹配 {nd}")
        return "\n".join(lines)
    finally:
        conn.close()
