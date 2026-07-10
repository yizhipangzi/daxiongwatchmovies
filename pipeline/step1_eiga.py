"""Step 1

操作1: 登記首都圏映画館 (東京13/埼玉11/千葉12/神奈川14) — 月初のみフルスクレイプ
操作2: 影院为中心逐馆抓排片 + 分類 (院線 / 小众)
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

DB_PATH = Path("data/eiga.db")
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.8",
}

# 院線チェーン名の判定キーワード。chain列が非空 = 院線、空 = 小众（独立系）。
CHAIN_KEYWORDS = {
    "TOHO": ["TOHOシネマズ"],
    "シネマサンシャイン": ["シネマサンシャイン", "グランドシネマサンシャイン"],
    "テアトルシネマ": ["テアトル"],
}

# 首都圏映画館ページ — 東京(13) / 埼玉(11) / 千葉(12) / 神奈川(14)
THEATER_PREFS = ["13", "11", "12", "14"]
THEATER_LIST_URLS = {pref: f"https://eiga.com/theater/{pref}/" for pref in THEATER_PREFS}

# 映画詳細ページの製作情報regex
_PROD_RE = re.compile(
    r"(\d{4})年製作／(\d+)分／(?:[^／\n]{1,6}／)?([^\n原配劇／]{1,30})"
)
_ORIG_RE = re.compile(
    # Stop before distribution/release labels OR content sections (あらすじ, スタッフ).
    # "あらすじ" must be included: on some old-film pages eiga.com places the synopsis
    # in the same block element as the original title, causing one long "line" in
    # get_text() that the regex would otherwise capture in full.
    r"原題[^：:\n]*[：:]\s*(.+?)(?=\s*(?:配給|配信|配信開始日|劇場公開日|その他|その他の公開日|オフィシャル|公式|あらすじ|スタッフ)|\n|$)"
)
# 注意 label 和日期间常有全角/半角冒号「：」「:」——必须允许，否则会漏掉主上映日，
# .search 会跑去匹配侧栏「上映中/即将上映」列表里别的电影的日期（曾把鬼滅抓成别片的日期）。
_RELEASE_RE = re.compile(r"劇場公開日[\s：:]*(\d{4}年\d{1,2}月\d{1,2}日)")


# ── DB Setup ─────────────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS theaters (
            theater_id TEXT PRIMARY KEY,   -- eiga.com path e.g. "13/130501/3291"
            name       TEXT NOT NULL,
            chain      TEXT,              -- "TOHO" / "シネマサンシャイン" / "テアトルシネマ" / NULL。非NULL/非空 = 院線判定
            area       TEXT,              -- e.g. "池袋"
            area_code  TEXT,              -- e.g. "130501"
            url        TEXT,
            delete_flg INTEGER NOT NULL DEFAULT 0  -- 論理削除: 1=eiga.comから消えた映画館（scanから除外）
        );
        CREATE TABLE IF NOT EXISTS movies (
            movie_id   TEXT PRIMARY KEY,
            title_jp   TEXT NOT NULL,
            country    TEXT,
            title_original TEXT,
            year       INTEGER,
            duration   INTEGER,
            release_date TEXT,
            director   TEXT,
            rank       INTEGER,
            category   TEXT,              -- "chain" (院線映画) / "indie" (小众映画) / "other"
            eiga_url   TEXT
        );
        CREATE TABLE IF NOT EXISTS screenings (
            movie_id   TEXT NOT NULL,
            theater_id TEXT NOT NULL,
            last_seen  TEXT,               -- JST date this pair was last confirmed showing
            PRIMARY KEY (movie_id, theater_id),
            FOREIGN KEY (movie_id) REFERENCES movies(movie_id),
            FOREIGN KEY (theater_id) REFERENCES theaters(theater_id)
        );
        CREATE TABLE IF NOT EXISTS showtimes (
            movie_id   TEXT NOT NULL,
            theater_id TEXT NOT NULL,
            show_date  TEXT NOT NULL,   -- ISO date "2026-06-06"
            start_time TEXT NOT NULL,   -- "HH:MM" 24h, zero-padded
            end_time   TEXT,            -- "HH:MM" or NULL
            ticket_url TEXT,            -- 予約リンク or NULL (満席/上映終了 etc.)
            movie_type TEXT,            -- JSON [{type, type_txt}]: 字幕/吹替/IMAX 等格式标签
            PRIMARY KEY (movie_id, theater_id, show_date, start_time),
            FOREIGN KEY (movie_id) REFERENCES movies(movie_id),
            FOREIGN KEY (theater_id) REFERENCES theaters(theater_id)
        );
        CREATE TABLE IF NOT EXISTS movie_snapshots (
            snapshot_date TEXT NOT NULL,
            rank INTEGER NOT NULL,
            movie_id TEXT NOT NULL,
            category TEXT,
            PRIMARY KEY (snapshot_date, rank),
            FOREIGN KEY (movie_id) REFERENCES movies(movie_id)
        );
        CREATE TABLE IF NOT EXISTS run_state (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS theater_scan_log (
            scan_date  TEXT NOT NULL,   -- JST date, resets scan progress daily
            theater_id TEXT NOT NULL,
            PRIMARY KEY (scan_date, theater_id)
        );
    """)
    # Idempotent ALTER for eiga rating fields (added later).
    # eiga_rating is the 1-5 average shown at the top of each movie page;
    # eiga_rating_count is the 全N件 review count next to it.
    # script = 脚本 (screenwriters, " / " joined); cast_names = 出演者 (actor
    # names, " / " joined — `cast` is a SQL keyword so the column is cast_names).
    # director (監督) column already exists above.
    # poster_url = 海报图 URL（从电影海报页 /movie/{id}/photo/ 的 .movie-photo img
    # 抽取并升到高清 /640.jpg）。eiga 没有海报时留空，由 step4 回退到豆瓣海报。
    for col_def in ("eiga_rating REAL", "eiga_rating_count INTEGER",
                    "script TEXT", "cast_names TEXT", "poster_url TEXT"):
        try:
            conn.execute(f"ALTER TABLE movies ADD COLUMN {col_def}")
        except Exception:
            pass
    # Idempotent ALTER for showtimes.movie_type (added later).
    try:
        conn.execute("ALTER TABLE showtimes ADD COLUMN movie_type TEXT")
    except Exception:
        pass
    # Migration: theaters.category (chain/indie/other) 廃止 → chain列の有無で判定。
    # 代わりに論理削除フラグ delete_flg を追加。
    try:
        conn.execute("ALTER TABLE theaters ADD COLUMN delete_flg INTEGER NOT NULL DEFAULT 0")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE theaters DROP COLUMN category")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE screenings ADD COLUMN last_seen TEXT")
    except Exception:
        pass
    conn.commit()
    return conn


def _fetch(url: str, delay: float = 0.5, max_retries: int = 3) -> Optional[BeautifulSoup]:
    """抓取 eiga 页面。走统一 fetcher（节流/抖动/UA 轮换/可选 Oracle VM 中继/退避冷却）。

    fetcher 冷却用尽（exhausted）时直接返回 None，让调用方据此停下续跑。
    """
    from scraper import fetcher
    # delay=0：把基础延时交给 fetcher 按 environment 决定（local/cloud），
    # 但若调用方显式传了更大的 delay 则尊重之。
    base = max(delay, fetcher.base_delay())
    for attempt in range(1, max_retries + 1):
        if fetcher.exhausted():
            return None
        # 不传 User-Agent，让 fetcher 从 UA 池轮换（对付反爬）；只保留语言头。
        hdrs = {k: v for k, v in _HEADERS.items() if k.lower() != "user-agent"}
        resp = fetcher.get(url, headers=hdrs, delay=base if attempt == 1 else delay,
                           cooldown=True)
        if resp is not None:
            try:
                if resp.status_code < 400:
                    resp.encoding = "utf-8"
                    return BeautifulSoup(resp.text, "lxml")
                logger.warning("fetch %s -> HTTP %s (attempt %d/%d)",
                               url, resp.status_code, attempt, max_retries)
            except Exception as exc:
                logger.warning("fetch parse failed (%s): %s", url, exc)
        if attempt < max_retries:
            time.sleep(2 * attempt)
    return None


# ── 操作1: 映画館登記 ────────────────────────────────────────────────────────

# Area name detection from eiga.com section headers & theater names
_AREA_MAP = {
    "130101": "千代田区",
    "130201": "新宿",
    "130301": "渋谷",
    "130401": "品川",
    "130501": "池袋",
    "130601": "銀座",
    "130701": "有楽町",
    "130801": "六本木",
    "130901": "目黒",
    "131001": "上野",
    "131101": "日本橋",
}

# Finer area detection from theater name / address
_AREA_KEYWORDS = {
    "池袋": ["池袋"],
    "上野": ["上野"],
    "渋谷": ["渋谷"],
    "新宿": ["新宿", "歌舞伎町"],
    "日本橋": ["日本橋"],
    "目黒": ["目黒"],
    "有楽町": ["有楽町", "日比谷"],
    "六本木": ["六本木"],
    "恵比寿": ["恵比寿", "YEBISU", "エビス"],
    "飯田橋": ["飯田橋", "アンスティチュ・フランセ"],
    "大塚": ["大塚"],
    "銀座": ["銀座"],
    "神保町": ["神保町"],
    "水道橋": ["水道橋", "アテネ・フランセ"],
    "高田馬場": ["高田馬場", "早稲田"],
    "田端": ["田端", "TABATA"],
}


def _detect_chain(name: str) -> Optional[str]:
    """Detect if theater belongs to a chain. Returns chain name or None."""
    for chain, keywords in CHAIN_KEYWORDS.items():
        for kw in keywords:
            if kw in name:
                return chain
    return None


def _detect_area(name: str) -> str:
    """Detect area from theater name."""
    for area, keywords in _AREA_KEYWORDS.items():
        for kw in keywords:
            if kw in name:
                return area
    return ""


def _scrape_theater_list_page(delay: float = 0.5) -> list[dict]:
    """Scrape /theater/{pref}/ pages for all 首都圏 prefectures (東京/埼玉/千葉/神奈川)."""
    theaters = []
    for pref, url in THEATER_LIST_URLS.items():
        soup = _fetch(url, delay=delay)
        if soup is None:
            continue

        # The page has sections by area, each theater is a link /theater/PREF/AREA_CODE/ID/
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            m = re.search(rf"/theater/{pref}/(\d+)/(\d+)/", href)
            if not m:
                continue

            area_code = m.group(1)
            theater_num = m.group(2)
            theater_id = f"{pref}/{area_code}/{theater_num}"
            name = a.get_text(strip=True)

            # Skip if name looks like a count or empty
            if not name or re.match(r"^\(\d+\)$", name) or len(name) < 2:
                continue
            # Remove trailing (N) showing count
            name = re.sub(r"\s*\(\d+\)\s*$", "", name)
            # Remove pipe characters that break MD tables
            name = name.replace("|", "")

            if not name:
                continue

            chain = _detect_chain(name)
            # Area-name keyword lookup only covers Tokyo (23-ku) wards, so
            # theaters in the other 3 prefectures naturally fall through to "" —
            # that's expected, not a bug (see theaters_export.csv for the manually
            # curated broad-region names used for those 3 prefectures).
            area = _detect_area(name)

            theaters.append({
                "theater_id": theater_id,
                "name": name,
                "chain": chain,
                "area": area,
                "area_code": area_code,
                "url": f"https://eiga.com/theater/{theater_id}/",
            })

    # Deduplicate by theater_id
    seen = set()
    unique = []
    for t in theaters:
        if t["theater_id"] not in seen:
            seen.add(t["theater_id"])
            unique.append(t)

    return unique


def register_theaters(delay: float = 0.5, force: bool = False) -> list[dict]:
    """操作1: 映画館マスタ整備（東京/埼玉/千葉/神奈川の4都県）。DBと差分をとってinsert/delete/update。

    フルスクレイプは4都県ぶんの劇場一覧ページを読むため、月初（JST基準で月が
    変わった最初の実行）だけ行う。それ以外の日は前回スクレイプ結果をDBから
    そのまま返し、ネットワークアクセスをスキップする。force=True で強制実行可能。
    """
    conn = _get_db()

    from datetime import datetime, timezone, timedelta
    JST = timezone(timedelta(hours=9))
    current_month = datetime.now(JST).strftime("%Y-%m")
    last_month = _get_run_state("theater_master_last_month")
    if not force and last_month == current_month:
        logger.info("Theater master: already maintained this month (%s), skipping scrape.",
                    current_month)
        existing_theaters = [
            dict(row) for row in conn.execute("SELECT * FROM theaters WHERE delete_flg=0").fetchall()
        ]
        conn.close()
        return existing_theaters

    # existing includes logically-deleted rows too, so a theater that reappears
    # on eiga.com can be revived instead of hitting the PK conflict on INSERT.
    existing = {
        row["theater_id"]: dict(row)
        for row in conn.execute("SELECT * FROM theaters").fetchall()
    }

    scraped = _scrape_theater_list_page(delay=delay)
    scraped_map = {t["theater_id"]: t for t in scraped}

    added = [t for tid, t in scraped_map.items() if tid not in existing]
    revived = [t for tid, t in scraped_map.items() if tid in existing and existing[tid]["delete_flg"]]
    removed = [t for tid, t in existing.items() if tid not in scraped_map and not t["delete_flg"]]
    updated = [
        t for tid, t in scraped_map.items()
        if tid in existing and not existing[tid]["delete_flg"] and (
            t["name"] != existing[tid]["name"] or
            t["chain"] != existing[tid]["chain"]
        )
    ]

    for t in added:
        conn.execute(
            """INSERT INTO theaters (theater_id, name, chain, area, area_code, url, delete_flg)
               VALUES (?, ?, ?, ?, ?, ?, 0)""",
            (t["theater_id"], t["name"], t["chain"], t["area"], t["area_code"], t["url"]),
        )
    for t in revived:
        conn.execute(
            """UPDATE theaters SET name=?, chain=?, area=?, area_code=?, url=?, delete_flg=0
               WHERE theater_id=?""",
            (t["name"], t["chain"], t["area"], t["area_code"], t["url"], t["theater_id"]),
        )
    for t in removed:
        # 論理削除: 行は残す（screenings/showtimesの履歴を保持）。日次scanは
        # delete_flg=0 でフィルタするので、以後この映画館は対象外になる。
        conn.execute("UPDATE theaters SET delete_flg=1 WHERE theater_id=?", (t["theater_id"],))
    for t in updated:
        conn.execute(
            """UPDATE theaters SET name=?, chain=?, area=?, area_code=?, url=?
               WHERE theater_id=?""",
            (t["name"], t["chain"], t["area"], t["area_code"], t["url"], t["theater_id"]),
        )

    conn.commit()

    if added:
        logger.info("Theaters added (%d): %s", len(added),
                    ", ".join(f"[{t['area']}] {t['name']}" for t in added))
    if revived:
        logger.info("Theaters revived (%d): %s", len(revived),
                    ", ".join(f"[{t['area']}] {t['name']}" for t in revived))
    if removed:
        logger.info("Theaters logically deleted (%d): %s", len(removed),
                    ", ".join(f"[{t['area']}] {t['name']}" for t in removed))
    if updated:
        logger.info("Theaters updated (%d): %s", len(updated),
                    ", ".join(f"[{t['area']}] {t['name']}" for t in updated))
    if not added and not revived and not removed and not updated:
        logger.info("Theaters unchanged (%d total)", len(scraped))

    chain_theaters = [t for t in scraped if t["chain"]]
    indie_theaters = [t for t in scraped if not t["chain"]]
    logger.info("  院線 (chain): %d  小众 (indie): %d  total: %d",
                len(chain_theaters), len(indie_theaters), len(scraped))

    conn.close()
    _set_run_state("theater_master_last_month", current_month)
    return scraped


def _set_run_state(key: str, value: str) -> None:
    conn = _get_db()
    conn.execute("INSERT OR REPLACE INTO run_state (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def _get_run_state(key: str) -> Optional[str]:
    conn = _get_db()
    row = conn.execute("SELECT value FROM run_state WHERE key=?", (key,)).fetchone()
    conn.close()
    return row[0] if row else None


def _staff_names(soup: BeautifulSoup, label: str) -> list[str]:
    """Return the people listed under a given <dt> label in <dl class="movie-staff">.

    The staff block is a definition list: each <dt> is a role (監督 / 脚本 / 撮影 …)
    followed by one or more <dd> entries until the next <dt>.
    """
    dl = soup.select_one("dl.movie-staff")
    if dl is None:
        return []
    names: list[str] = []
    current: Optional[str] = None
    for child in dl.find_all(["dt", "dd"]):
        if child.name == "dt":
            current = child.get_text(strip=True)
        elif current == label:
            name = child.get_text(strip=True)
            if name and name not in names:
                names.append(name)
    return names


def _scrape_movie_detail(movie_id: str, delay: float = 0.5) -> dict:
    """Fetch movie detail page, extract country/original title/year/duration/
    release/director/script/cast."""
    url = f"https://eiga.com/movie/{movie_id}/"
    soup = _fetch(url, delay=delay)
    if soup is None:
        return {}

    text = soup.get_text()
    info: dict = {}

    # 監督 (director) / 脚本 (script) from the staff definition list, and 出演者
    # (cast) from <ul class="movie-cast">. Cast actor name lives in the <span>
    # of each <li> (the surrounding text is the character/role name).
    director = _staff_names(soup, "監督")
    if director:
        info["director"] = " / ".join(director)
    script = _staff_names(soup, "脚本")
    if script:
        info["script"] = " / ".join(script)
    cast = [
        li.select_one("span").get_text(strip=True)
        for li in soup.select("ul.movie-cast > li")
        if li.select_one("span")
    ]
    if cast:
        # Cap at 10 to keep the field compact for the briefing / mini-program popup.
        info["cast"] = " / ".join(cast[:10])

    pm = _PROD_RE.search(text)
    if pm:
        info["year"] = int(pm.group(1))
        info["duration"] = int(pm.group(2))
        country = pm.group(3).strip()
        # Cap at 50 chars: long enough for 5+ country co-productions
        # (e.g. "スペイン・オランダ・イギリス・フランス合作" is 21), but still
        # rejects regex over-matches that bleed into adjacent page sections.
        if len(country) <= 50:
            info["country"] = country

    # eiga.com user rating (X.Y on a 5-point scale) + total rating count.
    # The rating count comes from the .all-reviews-link tag (e.g.
    # "全123件") — this is the number of users who actually rated the
    # film, which the briefing uses as a credibility threshold. The
    # bare-text regex is a fallback for pages where the link's markup varies.
    rating_el = soup.select_one(".rating-star")
    if rating_el:
        try:
            info["eiga_rating"] = float(rating_el.get_text(strip=True))
        except ValueError:
            pass
    link_el = soup.select_one(".all-reviews-link")
    if link_el:
        lm = re.search(r"(\d+)", link_el.get_text())
        if lm:
            try:
                info["eiga_rating_count"] = int(lm.group(1))
            except ValueError:
                pass
    if "eiga_rating_count" not in info:
        rcm = re.search(r"全(\d+)件", text)
        if rcm:
            try:
                info["eiga_rating_count"] = int(rcm.group(1))
            except ValueError:
                pass

    om = _ORIG_RE.search(text)
    if om:
        raw = om.group(1).strip()
        # Safety net: truncate at 3+ consecutive spaces, which eiga.com uses to
        # separate the title from the synopsis when they land on the same text line.
        raw = re.split(r'\s{3,}', raw)[0].strip()
        if raw:
            info["title_original"] = raw

    rm = _RELEASE_RE.search(text)
    if rm:
        info["release_date"] = rm.group(1)

    return info


# 海报 URL 高清化：
#   .../photo/{hash}.jpg        → .../photo/{hash}/640.jpg
#   .../photo/{hash}/{size}.jpg → .../photo/{hash}/640.jpg
_POSTER_SIZED_RE = re.compile(r"(/photo/[0-9a-f]+)/\d+\.(?:jpg|png|webp)")
_POSTER_PLAIN_RE = re.compile(r"(/photo/[0-9a-f]+)\.jpg")


def _hi_res_poster(src: str) -> str:
    """把 eiga 海报 URL 升到高清 /640.jpg（约 100KB，适配小程序浮层封面）。"""
    m = _POSTER_SIZED_RE.search(src)
    if m:
        return src[:m.start()] + m.group(1) + "/640.jpg"
    m = _POSTER_PLAIN_RE.search(src)
    if m:
        return src[:m.start()] + m.group(1) + "/640.jpg"
    return src


def _scrape_eiga_poster(movie_id: str, delay: float = 0.5) -> Optional[str]:
    """抓电影海报页 /movie/{id}/photo/，取 <div class="movie-photo"> 下 img 的 src，
    升到高清 /640.jpg 返回。eiga 没有海报（占位 noimg）或页面取不到时返回 None，
    交给 step4 回退到豆瓣海报。"""
    url = f"https://eiga.com/movie/{movie_id}/photo/"
    soup = _fetch(url, delay=delay)
    if soup is None:
        return None
    el = soup.select_one(".movie-photo img")
    src = (el.get("src") or "").strip() if el else ""
    if not src or "noimg" in src:
        return None
    return _hi_res_poster(src)


def _ensure_movie_detail(conn: sqlite3.Connection, movie_id: str, title_jp: str,
                         delay: float = 0.5) -> None:
    """Insert or refresh a movie's metadata row (country/original title/director/
    cast/rating/poster/etc — NOT category or rank; the caller sets those once all
    theaters showing it today are known, in a second pass over every theater).

    Reuse-if-complete: looks up any existing row by title_jp (not movie_id — the
    same film may have first appeared under a temp 99999xxx id before eiga.com
    registered it), and only re-fetches the detail/poster pages if key fields
    are still missing.
    """
    row = conn.execute(
        "SELECT country, title_original, year, duration, release_date, director, "
        "eiga_rating, eiga_rating_count, script, cast_names, poster_url FROM movies WHERE title_jp=?",
        (title_jp,)
    ).fetchone()
    existing_eiga_rating = row["eiga_rating"] if row else None
    existing_eiga_rating_count = row["eiga_rating_count"] if row else None
    existing_poster = row["poster_url"] if row else None

    # eiga_rating==0 is a real "no rating" value, not a gap — only None counts as missing.
    reuse = bool(
        row and row["country"] and existing_eiga_rating is not None
        and row["director"] and row["cast_names"]
    )
    mov: dict = {}
    if reuse:
        mov.update({
            "country": row["country"],
            "title_original": row["title_original"],
            "year": row["year"],
            "duration": row["duration"],
            "release_date": row["release_date"],
            "director": row["director"],
            "eiga_rating": existing_eiga_rating,
            "eiga_rating_count": existing_eiga_rating_count,
            "script": row["script"],
            "cast": row["cast_names"],
        })
        logger.info(f"[Step1] Skipped detail for {title_jp} (reuse DB)")
    else:
        if row:
            if not row["country"]:
                missing = "country"
            elif existing_eiga_rating is None:
                missing = "eiga_rating"
            elif not row["director"]:
                missing = "director"
            else:
                missing = "cast"
            logger.info(f"[Step1] Re-fetching detail for {title_jp} ({missing} missing)")
        detail = _scrape_movie_detail(movie_id, delay=delay)
        mov.update(detail)

    # 海报：DB 已有就复用（不重抓）；否则抓一次海报页 /movie/{id}/photo/。
    # eiga 没有海报时返回 None → 留空，由 step4 回退到豆瓣海报。
    mov["poster_url"] = existing_poster or _scrape_eiga_poster(movie_id, delay=delay)

    existing_movie = conn.execute(
        "SELECT movie_id FROM movies WHERE movie_id=?", (movie_id,)
    ).fetchone()
    if not existing_movie:
        conn.execute(
            """INSERT INTO movies
               (movie_id, title_jp, country, title_original, year,
                duration, release_date, director, eiga_url,
                eiga_rating, eiga_rating_count, script, cast_names, poster_url)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (movie_id, title_jp, mov.get("country"), mov.get("title_original"),
             mov.get("year"), mov.get("duration"), mov.get("release_date"),
             mov.get("director"), f"https://eiga.com/movie/{movie_id}/",
             mov.get("eiga_rating"), mov.get("eiga_rating_count"),
             mov.get("script"), mov.get("cast"), mov.get("poster_url")),
        )
    else:
        # COALESCE preserves existing values when the new fetch returned nothing
        # for a column (e.g. reused-but-still-missing fields stay as they were).
        conn.execute(
            """UPDATE movies SET
                   country=COALESCE(?, country),
                   title_original=COALESCE(?, title_original),
                   year=COALESCE(?, year),
                   duration=COALESCE(?, duration),
                   release_date=COALESCE(?, release_date),
                   director=COALESCE(?, director),
                   eiga_rating=COALESCE(?, eiga_rating),
                   eiga_rating_count=COALESCE(?, eiga_rating_count),
                   script=COALESCE(?, script),
                   cast_names=COALESCE(?, cast_names),
                   poster_url=COALESCE(?, poster_url)
                 WHERE movie_id=?""",
            (mov.get("country"), mov.get("title_original"),
             mov.get("year"), mov.get("duration"),
             mov.get("release_date"), mov.get("director"),
             mov.get("eiga_rating"), mov.get("eiga_rating_count"),
             mov.get("script"), mov.get("cast"),
             mov.get("poster_url"),
             movie_id),
        )


_TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})")


def _norm_time(raw: str) -> Optional[str]:
    """Normalize a leading 'H:MM' / 'HH:MM' to zero-padded 'HH:MM'."""
    m = _TIME_RE.match(raw or "")
    if not m:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _scrape_showtimes(movie_id: str, theater_id: str, delay: float = 0.5) -> list[dict]:
    """Fetch the per-theater schedule page and parse the weekly showtimes.

    URL: /movie-theater/{movie_id}/{theater_id}/ where theater_id is the
    "pref/area/theater" path (e.g. "13/130201/3318").

    The page groups screenings by format: each <div class="movie-schedule"> holds
    one <div class="movie-type"> (the format/audio tags) plus one
    <table class="weekly-schedule">. Every tag is a <span class="type-XXX">…</span>
    (type-subtitled 字幕 / type-dubbed 吹替 / type-imax / type-screenx / …) and
    applies to ALL showtimes in that group's table.

    Each <td data-date="YYYYMMDD"> cell holds one day's screenings. Every showtime
    is a `.btn` element:
      - <a class="btn ticket…" href="…">14:55<small>～23:35</small></a>  (bookable)
      - <span class="btn off">9:40</span>                               (no link)
    The leading text is the start time; the optional <small> holds the end time.

    Returns a list of dicts: {show_date, start_time, end_time, ticket_url, types}
    where types = [{"type": "type-imax", "type_txt": "IMAX"}, …]. The same
    (date, start) appearing in multiple format groups is merged into one slot with
    the union of its tags (dedup by type class, in first-seen order).
    """
    url = f"https://eiga.com/movie-theater/{movie_id}/{theater_id}/"
    soup = _fetch(url, delay=delay)
    if soup is None:
        return []

    by_slot: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for table in soup.select("table.weekly-schedule"):
        # Tags live in the enclosing group's <div class="movie-type">.
        grp = table.find_parent("div", class_="movie-schedule")
        types: list[dict] = []
        if grp:
            for span in grp.select("div.movie-type span"):
                cls = next((c for c in span.get("class", []) if c.startswith("type-")), None)
                if not cls:
                    continue
                txt = span.get_text(strip=True)
                types.append({"type": cls, "type_txt": txt})

        for td in table.select("td[data-date]"):
            d = td.get("data-date", "")
            if len(d) != 8 or not d.isdigit():
                continue
            show_date = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
            # Normal theaters render each showtime as a `.btn` (bookable <a> or
            # unbookable <span class="btn off">). 名画座/revival houses without
            # online ticketing instead list bare <span>HH:MM</span> as direct
            # children of the <td> (the date label is inside <p class="date">, so
            # recursive=False excludes it). Fall back to those so these theaters
            # don't end up with zero showtimes.
            slot_els = td.select(".btn")
            if not slot_els:
                slot_els = [sp for sp in td.find_all("span", recursive=False)
                            if _TIME_RE.match(sp.get_text(strip=True))]
            # 一部の映画館は <a class="official" data-time="...">HH:MM～HH:MM</a>
            # 形式で排片を表示する（.btn ではない）。
            if not slot_els:
                slot_els = td.select("a.official")
            for btn in slot_els:
                # The start time is the leading text node, before any <small> end time.
                lead = btn.find(string=True)
                start = _norm_time(lead.strip() if lead else "")
                if not start:
                    continue
                key = (show_date, start)
                rec = by_slot.get(key)
                if rec is None:
                    end = None
                    small = btn.select_one("small")
                    if small:
                        end = _norm_time(re.sub(r"^[～~\s]+", "", small.get_text(strip=True)))
                    # a.official のテキストは "HH:MM～HH:MM" 形式
                    if end is None:
                        em = re.search(r"[～~](\d{1,2}:\d{2})", btn.get_text(strip=True))
                        if em:
                            end = _norm_time(em.group(1))
                    href = btn.get("href")
                    rec = {
                        "show_date": show_date,
                        "start_time": start,
                        "end_time": end,
                        "ticket_url": href if href else None,
                        "types": [],
                    }
                    by_slot[key] = rec
                    order.append(key)
                # Merge this group's tags into the slot, dedup by type class.
                seen_types = {t["type"] for t in rec["types"]}
                for t in types:
                    if t["type"] not in seen_types:
                        rec["types"].append(t)
                        seen_types.add(t["type"])
    return [by_slot[k] for k in order]


# ── 操作3: 映画館ページから未登録映画を検出・登記 ─────────────────────────

def _next_unlisted_id(conn: sqlite3.Connection) -> str:
    """Return the next available 99999xxx movie ID for unlisted movies."""
    row = conn.execute(
        "SELECT MAX(CAST(movie_id AS INTEGER)) FROM movies WHERE movie_id GLOB '99999*'"
    ).fetchone()
    base = row[0] if (row and row[0]) else 999990000
    return str(base + 1)


_MOVIE_HREF_RE = re.compile(r"(?:https://eiga\.com)?/movie/(\d+)/")


def _scrape_theater_schedule(theater_id: str, delay: float = 0.5) -> list[dict]:
    """Scrape a theater's own schedule page: every movie section showing there
    today/this week, both eiga.com-registered and unlisted.

    On eiga.com's theater page each movie section starts with an h2:
      - <h2 class="title-xlarge ..."><a href="/movie/ID/">title</a></h2>  ← in DB
      - <h2 class="title-xlarge ...">title</h2>                           ← NOT in DB

    Walks the page in document order, grouping div.movie-schedule blocks under
    their preceding h2. Returns [{title_jp, movie_id, showtimes}] for every
    section — movie_id is the eiga.com id (str) if the h2 links to /movie/ID/,
    else None (unlisted title; caller resolves/registers a temp id).
    showtimes format is identical to _scrape_showtimes output.
    """
    url = f"https://eiga.com/theater/{theater_id}/"
    soup = _fetch(url, delay=delay)
    if soup is None:
        return []

    # Walk page in document order, bucket schedule-divs under their h2.
    # soup.select() returns elements in document order.
    sections: list[tuple] = []   # [(h2_el, [sched_div, ...])]
    current_h2 = None
    current_scheds: list = []
    for el in soup.select("h2.title-xlarge, div.movie-schedule"):
        if el.name == "h2":
            if current_h2 is not None:
                sections.append((current_h2, list(current_scheds)))
            current_h2 = el
            current_scheds = []
        else:
            current_scheds.append(el)
    if current_h2 is not None:
        sections.append((current_h2, current_scheds))

    results = []
    for h2, scheds in sections:
        a = h2.select_one("a[href*='/movie/']")
        movie_id = None
        if a:
            m = _MOVIE_HREF_RE.match(a.get("href", ""))
            if m:
                movie_id = m.group(1)
        title = h2.get_text(strip=True)
        if not title or len(title) < 2:
            continue

        # Parse showtimes — same structure as _scrape_showtimes but sourced from
        # the theater overview page instead of the per-movie-theater page.
        by_slot: dict[tuple[str, str], dict] = {}
        order: list[tuple[str, str]] = []
        for sched in scheds:
            types: list[dict] = []
            for span in sched.select("div.movie-type span"):
                cls = next(
                    (c for c in span.get("class", []) if c.startswith("type-")), None
                )
                if not cls:
                    continue
                types.append({"type": cls, "type_txt": span.get_text(strip=True)})

            for table in sched.select("table.weekly-schedule"):
                for td in table.select("td[data-date]"):
                    d = td.get("data-date", "")
                    if len(d) != 8 or not d.isdigit():
                        continue
                    show_date = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
                    slot_els = td.select(".btn")
                    if not slot_els:
                        slot_els = [
                            sp for sp in td.find_all("span", recursive=False)
                            if _TIME_RE.match(sp.get_text(strip=True))
                        ]
                    # 未登録映画は <a class="official" data-time="...">HH:MM～HH:MM</a>
                    # 形式で排片が表示される（.btn ではない）。
                    if not slot_els:
                        slot_els = td.select("a.official")
                    for btn in slot_els:
                        lead = btn.find(string=True)
                        start = _norm_time(lead.strip() if lead else "")
                        if not start:
                            continue
                        key = (show_date, start)
                        rec = by_slot.get(key)
                        if rec is None:
                            end = None
                            small = btn.select_one("small")
                            if small:
                                end = _norm_time(
                                    re.sub(r"^[～~\s]+", "", small.get_text(strip=True))
                                )
                            # a.official のテキストは "HH:MM～HH:MM" 形式
                            if end is None:
                                em = re.search(r"[～~](\d{1,2}:\d{2})", btn.get_text(strip=True))
                                if em:
                                    end = _norm_time(em.group(1))
                            href = btn.get("href")
                            rec = {
                                "show_date": show_date,
                                "start_time": start,
                                "end_time": end,
                                "ticket_url": href if href else None,
                                "types": [],
                            }
                            by_slot[key] = rec
                            order.append(key)
                        seen = {t["type"] for t in rec["types"]}
                        for t in types:
                            if t["type"] not in seen:
                                rec["types"].append(t)
                                seen.add(t["type"])

        results.append({"title_jp": title, "movie_id": movie_id,
                         "showtimes": [by_slot[k] for k in order]})
    return results


def scrape_movies(delay: float = 0.5, scrape_showtimes: bool = True) -> dict:
    """操作2: 影院为中心扫描 — 逐个非删除影院直接抓其排片页，一次拿到该馆全部电影
    （已登记 + 未登记）及其今日排片，覆盖首都圏4都県全部活跃影院。

    Args:
        delay: per-request delay (seconds).
        scrape_showtimes: also fetch each theater's weekly schedule and store it
            in the `showtimes` table. Disable for a fast metadata-only run.

    Returns dict with keys: "chain", "indie", "other" — each a list of movie dicts
    ("other" is always empty now: every discovered movie is at a known theater,
    which is either chain or indie by construction — kept for caller compat).
    """
    conn = _get_db()
    from datetime import datetime, timezone, timedelta
    JST = timezone(timedelta(hours=9))

    # 清理过期排片：删除「处理日前一天」之前的旧场次（JST）。在映电影的场次每轮会
    # 先删后写保持最新，但下线电影（不再被抓取）的旧场次会残留，这里统一清掉。
    _cutoff = (datetime.now(JST).date() - timedelta(days=1)).isoformat()
    _purged = conn.execute("DELETE FROM showtimes WHERE show_date < ?", (_cutoff,)).rowcount
    conn.commit()
    if _purged:
        logger.info("Step1: 清理 %d 条 %s 之前的过期排片", _purged, _cutoff)

    # Get theater sets by chain membership. Definition: any theater with a non-empty
    # 'chain' field is considered a chain theater. Any theater not in chain_tids is
    # treated as non-chain (indie candidate). This means 'indie' is defined as
    # "not in chain theaters" regardless of geographic area.
    # delete_flg=0 のみ対象 — 論理削除された映画館はscan対象外。
    chain_tids = {row["theater_id"] for row in
                  conn.execute("SELECT theater_id FROM theaters "
                               "WHERE delete_flg=0 AND chain IS NOT NULL AND chain<>''").fetchall()}
    all_tids = {row["theater_id"] for row in
                conn.execute("SELECT theater_id FROM theaters WHERE delete_flg=0").fetchall()}
    indie_tids = all_tids - chain_tids

    logger.info("Theater counts — chain: %d, indie: %d, total: %d",
                len(chain_tids), len(indie_tids), len(all_tids))

    # Check last run date (Tokyo timezone) and skip if already run today
    try:
        last = _get_run_state("last_run_date")
        if last:
            last_date = datetime.fromisoformat(last).date()
            today_jst_date = datetime.now(JST).date()
            if last_date == today_jst_date:
                logger.info("Step1: already ran today (JST=%s), skipping scrape_movies.", last)
                # Load today's snapshot from movie_snapshots and assemble results
                conn2 = _get_db()
                snaps = conn2.execute(
                    "SELECT rank, movie_id, category FROM movie_snapshots WHERE snapshot_date=? ORDER BY rank",
                    (last,)
                ).fetchall()
                out = {"chain": [], "indie": [], "other": []}
                for s in snaps:
                    mid = s["movie_id"]
                    rank = s["rank"]
                    cat = s["category"] or "indie"
                    row = conn2.execute("SELECT * FROM movies WHERE movie_id=?", (mid,)).fetchone()
                    if not row:
                        continue
                    m = dict(row)
                    m["rank"] = rank
                    out.setdefault(cat, []).append(m)
                conn2.close()
                total = sum(len(v) for v in out.values())
                if total > 0:
                    return out
                if snaps:
                    logger.warning(
                        "Step1 skip: %d snapshots exist but movies table is empty. "
                        "Re-scraping to repopulate movies table.", len(snaps)
                    )
    except Exception:
        pass

    # 新一轮：清零 fetcher 的请求计数/冷却状态
    from scraper import fetcher
    fetcher.reset_state()

    today_jst = datetime.now(JST).date().isoformat()

    # 断点续跑：本轮（今天 JST）已扫过的影院直接跳过，改为按「馆」而不是按
    # 「片」记录进度——一馆一请求就能拿到该馆全部电影+排片，粒度更粗更省请求。
    done_theaters = {
        r["theater_id"] for r in conn.execute(
            "SELECT theater_id FROM theater_scan_log WHERE scan_date=?", (today_jst,)
        ).fetchall()
    }
    if done_theaters:
        logger.info("Step1 续跑：今天已扫 %d 家影院，将跳过", len(done_theaters))

    touched_movie_ids: set = set()
    all_active = sorted(all_tids)
    for i, tid in enumerate(all_active, 1):
        if tid in done_theaters:
            continue
        if fetcher.exhausted():
            logger.warning("Step1：抓取冷却用尽，本轮提前停止（已扫 %d/%d 家影院，下次续跑）",
                           i - 1, len(all_active))
            break

        theater_row = conn.execute(
            "SELECT name FROM theaters WHERE theater_id=?", (tid,)
        ).fetchone()
        theater_name = theater_row["name"] if theater_row else tid

        sections = _scrape_theater_schedule(tid, delay=delay)
        for sec in sections:
            title_jp = sec["title_jp"]
            if sec["movie_id"]:
                mid = sec["movie_id"]
                if mid not in touched_movie_ids:
                    touched_movie_ids.add(mid)
                    _ensure_movie_detail(conn, mid, title_jp, delay=delay)
            else:
                # 未登録映画：按 title_jp 找已注册的临时ID，否则新建 99999xxx。
                existing_m = conn.execute(
                    "SELECT movie_id FROM movies WHERE title_jp=?", (title_jp,)
                ).fetchone()
                if existing_m:
                    mid = existing_m["movie_id"]
                else:
                    mid = _next_unlisted_id(conn)
                    conn.execute(
                        "INSERT INTO movies (movie_id, title_jp, eiga_url) VALUES (?,?,NULL)",
                        (mid, title_jp),
                    )
                    logger.info("[unlisted] %s → %s | id=%s", theater_name, title_jp, mid)
                touched_movie_ids.add(mid)

            # upsert screenings with today's date so the post-loop pass can
            # reconstruct "which theaters show which movie today" even across
            # a resumed (multi-invocation) run — see done_theaters above.
            conn.execute(
                """INSERT INTO screenings (movie_id, theater_id, last_seen) VALUES (?, ?, ?)
                   ON CONFLICT(movie_id, theater_id) DO UPDATE SET last_seen=excluded.last_seen""",
                (mid, tid, today_jst),
            )
            if scrape_showtimes:
                conn.execute("DELETE FROM showtimes WHERE movie_id=? AND theater_id=?", (mid, tid))
                for s in sec["showtimes"]:
                    conn.execute(
                        """INSERT OR REPLACE INTO showtimes
                           (movie_id, theater_id, show_date, start_time, end_time, ticket_url, movie_type)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (mid, tid, s["show_date"], s["start_time"],
                         s["end_time"], s["ticket_url"],
                         json.dumps(s.get("types") or [], ensure_ascii=False)),
                    )

        # 该馆抓完立刻落库 + 记录扫描进度：云端中途被杀也不丢已完成的馆。
        conn.execute(
            "INSERT OR REPLACE INTO theater_scan_log (scan_date, theater_id) VALUES (?, ?)",
            (today_jst, tid),
        )
        conn.commit()
        logger.info("[%d/%d] %s (%s): %d movie section(s)",
                    i, len(all_active), theater_name, tid, len(sections))

    # 二遍统一算 category/rank/movie_snapshots：从 screenings 里今天被确认过
    # (last_seen=today) 的 (movie, theater) 对重建「今天谁在哪些馆上映」，这样
    # 不管本轮是一口气跑完还是分多次续跑完成，结果都是完整、一致的。
    today_pairs = conn.execute(
        "SELECT movie_id, theater_id FROM screenings WHERE last_seen=?", (today_jst,)
    ).fetchall()
    movie_theaters: dict = {}
    for r in today_pairs:
        movie_theaters.setdefault(r["movie_id"], set()).add(r["theater_id"])

    # rank: 廃止した東京榜単の代わりに、上映館数が多い順（同数は movie_id 昇順で
    # 安定ソート）で付け直す。無料で使える人気の目安であり、movie_snapshots の
    # PRIMARY KEY (snapshot_date, rank) の一意性も自然に保てる。
    ranked_ids = sorted(movie_theaters.keys(),
                        key=lambda mid: (-len(movie_theaters[mid]), mid))

    results = {"chain": [], "indie": [], "other": []}
    for rank, mid in enumerate(ranked_ids, 1):
        tids_for_movie = movie_theaters[mid]
        category = "chain" if (tids_for_movie & chain_tids) else "indie"

        conn.execute("UPDATE movies SET category=?, rank=? WHERE movie_id=?", (category, rank, mid))
        conn.execute(
            "INSERT OR REPLACE INTO movie_snapshots (snapshot_date, rank, movie_id, category) VALUES (?,?,?,?)",
            (today_jst, rank, mid, category),
        )
        row = conn.execute("SELECT * FROM movies WHERE movie_id=?", (mid,)).fetchone()
        if not row:
            continue
        m = dict(row)
        m["rank"] = rank
        m["_theaters"] = sorted(tids_for_movie)
        ph = ",".join("?" for _ in m["_theaters"])
        m["_theater_names"] = [
            r["name"] for r in conn.execute(
                f"SELECT name FROM theaters WHERE theater_id IN ({ph})", m["_theaters"]
            ).fetchall()
        ] if m["_theaters"] else []
        results[category].append(m)

    conn.commit()
    conn.close()

    for cat in ("chain", "indie"):
        results[cat].sort(key=lambda x: x["rank"])

    logger.info("=== Result: chain=%d, indie=%d (total=%d) ===",
                len(results["chain"]), len(results["indie"]), len(movie_theaters))
    # Record last successful run date in JST (unconditionally, even if the theater
    # loop above stopped early on fetcher exhaustion — matches prior behavior:
    # exhaustion is a deliberate "budget for today is spent" signal, not a crash;
    # resumability is for actual process kills, via theater_scan_log above).
    try:
        _set_run_state("last_run_date", today_jst)
    except Exception:
        pass
    return results


def backfill_movie_details(category: str = "chain",
                            only_missing: str = "eiga_rating",
                            delay: float = 0.6,
                            limit: int = 0) -> int:
    """Re-scrape eiga.com detail pages for movies already in DB that are
    missing a specific field.

    Step1's main loop only touches movies that are currently in eiga.com's
    "now showing" ranking. Movies that dropped off the ranking but still
    showing in theaters stay in DB with whatever fields they had at scrape
    time — useful here as a one-shot migration after new columns are added
    (e.g. eiga_rating).

    Args:
        category: which category to backfill ("chain", "indie", or "all").
        only_missing: column name to filter on; rows where this column IS NULL
                      are re-scraped. Defaults to "eiga_rating".
        delay: per-request delay (seconds).
        limit: 0 = no limit, otherwise cap the number of movies processed.

    Returns the number of movies updated.
    """
    conn = _get_db()
    cat_filter = "" if category == "all" else " AND category = ?"
    params: tuple
    if category == "all":
        params = ()
    else:
        params = (category,)
    sql = (
        f"SELECT movie_id, title_jp FROM movies "
        f"WHERE {only_missing} IS NULL{cat_filter} ORDER BY rank"
    )
    if limit > 0:
        sql += f" LIMIT {int(limit)}"
    rows = conn.execute(sql, params).fetchall()
    logger.info("Backfill %s: %d movies in %s with NULL %s",
                only_missing, len(rows), category, only_missing)
    updated = 0
    for r in rows:
        mid = r["movie_id"]
        title_jp = r["title_jp"]
        try:
            detail = _scrape_movie_detail(mid, delay=delay)
        except Exception as exc:
            logger.warning("Backfill: detail fetch failed for %s (%s): %s",
                           mid, title_jp, exc)
            continue
        if not detail:
            continue
        # COALESCE keeps existing data when the new fetch returned nothing.
        conn.execute(
            """UPDATE movies SET
                   country=COALESCE(?, country),
                   title_original=COALESCE(?, title_original),
                   year=COALESCE(?, year),
                   duration=COALESCE(?, duration),
                   release_date=COALESCE(?, release_date),
                   director=COALESCE(?, director),
                   eiga_rating=COALESCE(?, eiga_rating),
                   eiga_rating_count=COALESCE(?, eiga_rating_count)
                 WHERE movie_id=?""",
            (detail.get("country"), detail.get("title_original"),
             detail.get("year"), detail.get("duration"),
             detail.get("release_date"), detail.get("director"),
             detail.get("eiga_rating"), detail.get("eiga_rating_count"),
             mid),
        )
        conn.commit()
        updated += 1
        logger.info("[backfill %d/%d] %s | rating=%s",
                    updated, len(rows), title_jp, detail.get("eiga_rating"))
    conn.close()
    logger.info("Backfill done: %d movies updated", updated)
    return updated


def backfill_release_dates(categories: Optional[tuple] = None,
                           delay: float = 0.5,
                           limit: int = 0) -> tuple[int, int]:
    """重抓详情页，用修正后的正则更新电影的 release_date（劇場公開日）。

    修复历史上「劇場公開日 正则漏全角冒号」导致的错误上映日（曾把主上映日跳过、
    抓成页面侧栏别的电影的日期）。只更新 release_date，其它字段不动。

    Args:
        categories: 限定 category（如 ("chain","indie")）；None = 全部电影。
        delay: 每次请求的基础延时。
        limit: 0 = 不限，否则本次最多处理多少部。

    可断点续跑：fetcher 冷却用尽会提前停止。返回 (扫描数, 修正数)。
    """
    conn = _get_db()
    if categories:
        ph = ",".join("?" for _ in categories)
        rows = conn.execute(
            f"SELECT movie_id, title_jp, release_date FROM movies "
            f"WHERE category IN ({ph}) ORDER BY rank",
            tuple(categories),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT movie_id, title_jp, release_date FROM movies ORDER BY rank"
        ).fetchall()
    if limit > 0:
        rows = rows[:limit]

    from scraper import fetcher
    fetcher.reset_state()
    scanned = fixed = 0
    for r in rows:
        if fetcher.exhausted():
            logger.warning("backfill_release_dates: 抓取冷却用尽，提前停止（已扫 %d 部，下次续跑）",
                           scanned)
            break
        scanned += 1
        detail = _scrape_movie_detail(r["movie_id"], delay=delay)
        rd = detail.get("release_date")
        if rd and rd != r["release_date"]:
            conn.execute("UPDATE movies SET release_date=? WHERE movie_id=?",
                         (rd, r["movie_id"]))
            conn.commit()
            fixed += 1
            logger.info("[backfill release %d] %s: %s -> %s",
                        fixed, r["title_jp"], r["release_date"], rd)
    conn.close()
    logger.info("backfill_release_dates: 扫描 %d 部，修正 %d 部", scanned, fixed)
    return scanned, fixed


def backfill_showtime_types(delay: float = 0.5, limit: int = 0) -> tuple[int, int]:
    """回填 showtimes.movie_type：对当前已有排片的 (movie, theater) 重抓场次页，
    把格式标签（字幕/吹替/IMAX/ScreenX 等）写到对应场次行的 movie_type 列。

    movie_type 列是后加的，老库里的场次行该列为 NULL。step1 主循环有「今天已跑」
    防抓守卫，当天不会重抓，所以用这个独立回填做一次性迁移。

    只挑 movie_type 仍为 NULL 的 (movie, theater) 对，所以可反复运行/断点续跑
    （fetcher 冷却用尽会提前停）。只更新 movie_type 列，不动场次时间本身。

    返回 (扫描的 movie-theater 对数, 更新的场次行数)。
    """
    conn = _get_db()
    pairs = conn.execute(
        "SELECT DISTINCT movie_id, theater_id FROM showtimes "
        "WHERE movie_type IS NULL ORDER BY movie_id, theater_id"
    ).fetchall()
    if limit > 0:
        pairs = pairs[:limit]
    logger.info("backfill_showtime_types: %d 对 (movie,theater) 待回填", len(pairs))

    from scraper import fetcher
    fetcher.reset_state()
    scanned = updated = 0
    for p in pairs:
        if fetcher.exhausted():
            logger.warning("backfill_showtime_types: 抓取冷却用尽，提前停止"
                           "（已扫 %d 对，下次续跑）", scanned)
            break
        scanned += 1
        mid, tid = p["movie_id"], p["theater_id"]
        slots = _scrape_showtimes(mid, tid, delay=delay)
        for s in slots:
            cur = conn.execute(
                "UPDATE showtimes SET movie_type=? "
                "WHERE movie_id=? AND theater_id=? AND show_date=? AND start_time=?",
                (json.dumps(s.get("types") or [], ensure_ascii=False),
                 mid, tid, s["show_date"], s["start_time"]),
            )
            updated += cur.rowcount
        conn.commit()
        if scanned % 20 == 0:
            logger.info("backfill_showtime_types: %d/%d 对，已更新 %d 行",
                        scanned, len(pairs), updated)
    conn.close()
    logger.info("backfill_showtime_types: 扫描 %d 对，更新 %d 行", scanned, updated)
    return scanned, updated


def remap_unlisted_id(old_id: str, new_id: str, delay: float = 0.5) -> dict:
    """Promote a 99999xxx temp ID to its real eiga.com movie ID.

    Call this once eiga.com adds the movie to their database.  The function:
      1. Verifies old_id exists and looks like a 99999xxx entry.
      2. Fetches fresh metadata from eiga.com using new_id.
      3. Inserts a new movies row (new_id) with the real metadata, preserving
         category / screening / showtime / douban data from the old row.
      4. Re-parents all child rows (screenings, showtimes, snapshots,
         douban_matches, douban_details, douban_matches_history,
         douban_skip_list, eiga_reviews) to new_id.
      5. Deletes the old 99999xxx row.

    Returns a summary dict.  Raises ValueError on bad input, RuntimeError if
    the eiga.com detail page cannot be fetched.
    """
    conn = _get_db()

    old_row = conn.execute("SELECT * FROM movies WHERE movie_id=?", (old_id,)).fetchone()
    if not old_row:
        conn.close()
        raise ValueError(f"movie_id '{old_id}' not found in movies table")
    if not str(old_id).startswith("99999"):
        conn.close()
        raise ValueError(f"'{old_id}' does not look like a 99999xxx temp ID")
    if conn.execute("SELECT 1 FROM movies WHERE movie_id=?", (new_id,)).fetchone():
        conn.close()
        raise ValueError(f"new_id '{new_id}' already exists in movies table")

    # Fetch real metadata from eiga.com
    detail = _scrape_movie_detail(new_id, delay=delay)
    poster = _scrape_eiga_poster(new_id, delay=delay)

    old = dict(old_row)
    conn.execute(
        """INSERT INTO movies
           (movie_id, title_jp, country, title_original, year, duration,
            release_date, director, rank, category, eiga_url,
            eiga_rating, eiga_rating_count, script, cast_names, poster_url)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (new_id,
         old["title_jp"],
         detail.get("country") or old.get("country"),
         detail.get("title_original") or old.get("title_original"),
         detail.get("year") or old.get("year"),
         detail.get("duration") or old.get("duration"),
         detail.get("release_date") or old.get("release_date"),
         detail.get("director") or old.get("director"),
         old.get("rank"),
         old.get("category"),
         f"https://eiga.com/movie/{new_id}/",
         detail.get("eiga_rating") or old.get("eiga_rating"),
         detail.get("eiga_rating_count") or old.get("eiga_rating_count"),
         detail.get("script") or old.get("script"),
         detail.get("cast") or old.get("cast_names"),
         poster or old.get("poster_url")),
    )

    # Re-parent all child tables (SQLite FKs are advisory; update manually).
    for table in ("screenings", "showtimes", "movie_snapshots",
                  "douban_matches", "douban_details", "douban_matches_history",
                  "douban_skip_list", "eiga_reviews"):
        try:
            conn.execute(f"UPDATE {table} SET movie_id=? WHERE movie_id=?", (new_id, old_id))
        except Exception:
            pass  # table may not exist in older DBs

    conn.execute("DELETE FROM movies WHERE movie_id=?", (old_id,))
    conn.commit()
    conn.close()

    logger.info("remap_unlisted_id: %s → %s (%s)", old_id, new_id, old["title_jp"])
    return {
        "old_id": old_id,
        "new_id": new_id,
        "title_jp": old["title_jp"],
        "eiga_url": f"https://eiga.com/movie/{new_id}/",
        "detail_fetched": bool(detail),
    }


def merge_movie_ids(old_id: str, new_id: str) -> dict:
    """Merge a 99999xxx temp movie into an *already-existing* real movie row.

    Unlike remap_unlisted_id (which promotes a temp ID once eiga.com registers
    it — new_id must NOT exist yet), this handles the case where the same film
    was independently discovered twice: once as an unlisted temp ID and once
    with its real eiga_id (e.g. a different theater listed it with a link).
    Both rows may already carry their own screenings/showtimes/douban data.

    Conflict rule for every child table keyed (partly) by movie_id: new_id's
    existing row wins; old_id's row is only adopted where new_id has nothing
    for that key. movies.* columns follow the same rule via COALESCE — old_id
    never overwrites a value new_id already has.

    Returns a summary dict. Raises ValueError on bad input.
    """
    conn = _get_db()

    old_row = conn.execute("SELECT * FROM movies WHERE movie_id=?", (old_id,)).fetchone()
    if not old_row:
        conn.close()
        raise ValueError(f"movie_id '{old_id}' not found in movies table")
    if not str(old_id).startswith("99999"):
        conn.close()
        raise ValueError(f"'{old_id}' does not look like a 99999xxx temp ID")
    new_row = conn.execute("SELECT * FROM movies WHERE movie_id=?", (new_id,)).fetchone()
    if not new_row:
        conn.close()
        raise ValueError(
            f"new_id '{new_id}' does not exist — use remap_unlisted_id instead "
            f"if eiga.com just registered this movie under a fresh id"
        )

    old, new = dict(old_row), dict(new_row)

    # movies.*: fill only what new_id is missing, never overwrite.
    cols = [c for c in old.keys() if c != "movie_id"]
    set_clause = ", ".join(f"{c}=COALESCE({c}, ?)" for c in cols)
    conn.execute(
        f"UPDATE movies SET {set_clause} WHERE movie_id=?",
        [old[c] for c in cols] + [new_id],
    )

    # Child tables keyed by (movie_id, ...other pk cols...): move rows that
    # don't collide with something new_id already has; drop the rest.
    _CHILD_PKS = {
        "screenings": ["theater_id"],
        "showtimes": ["theater_id", "show_date", "start_time"],
        "eiga_reviews": ["position"],
        "douban_matches_history": ["snapshot_at"],
    }
    moved = {}
    for table, pk_rest in _CHILD_PKS.items():
        try:
            cond = " AND ".join(f"o.{c}=n.{c}" for c in pk_rest)
            # Table alias on the DELETE target (SQLite 3.39+) so the subquery's
            # unqualified columns can't shadow-resolve to its own "n" rows —
            # without it, "col=n.col" inside the subquery binds to n itself
            # (always true) and wipes out every old_id row, not just conflicts.
            conn.execute(
                f"""DELETE FROM {table} AS o WHERE o.movie_id=? AND EXISTS (
                        SELECT 1 FROM {table} n WHERE n.movie_id=? AND {cond}
                    )""",
                (old_id, new_id),
            )
            cur = conn.execute(f"UPDATE {table} SET movie_id=? WHERE movie_id=?", (new_id, old_id))
            moved[table] = cur.rowcount
        except Exception:
            pass  # table may not exist in older DBs

    # movie_snapshots: movie_id isn't part of the PK (snapshot_date, rank), so
    # re-parenting can't collide — just move everything over.
    try:
        conn.execute("UPDATE movie_snapshots SET movie_id=? WHERE movie_id=?", (new_id, old_id))
    except Exception:
        pass

    # Single-row-per-movie tables (PK = movie_id): keep new_id's row if it has
    # one, otherwise adopt old_id's.
    for table in ("douban_matches", "douban_details"):
        has_new = conn.execute(f"SELECT 1 FROM {table} WHERE movie_id=?", (new_id,)).fetchone()
        if not has_new:
            conn.execute(f"UPDATE {table} SET movie_id=? WHERE movie_id=?", (new_id, old_id))
        else:
            conn.execute(f"DELETE FROM {table} WHERE movie_id=?", (old_id,))

    # Skip-list status doesn't carry over onto a real, already-registered movie.
    try:
        conn.execute("DELETE FROM douban_skip_list WHERE movie_id=?", (old_id,))
    except Exception:
        pass

    conn.execute("DELETE FROM movies WHERE movie_id=?", (old_id,))
    conn.commit()
    conn.close()

    logger.info("merge_movie_ids: %s → %s (%s)", old_id, new_id, old["title_jp"])
    return {
        "old_id": old_id,
        "new_id": new_id,
        "title_jp": new.get("title_jp") or old["title_jp"],
        "moved": moved,
    }


# ── MD出力 ───────────────────────────────────────────────────────────────────

def generate_step1_md(results: dict, theaters: list[dict]) -> Path:
    """Generate step1 MD report with chain and indie sections."""
    from datetime import date
    today = date.today().isoformat()
    outdir = Path("output")
    outdir.mkdir(exist_ok=True)

    chain_theaters = [t for t in theaters if t["chain"]]
    indie_theaters = [t for t in theaters if not t["chain"]]
    chain_movies = results.get("chain", [])
    indie_movies = results.get("indie", [])

    lines = [
        f"# Step 1: 上映中映画一覧 ({today})",
        "",
        "## 院線映画館 (チェーン)",
        "",
        "| エリア | 映画館 | チェーン |",
        "|--------|--------|----------|",
    ]
    for t in sorted(chain_theaters, key=lambda x: x["area"]):
        lines.append(f"| {t['area']} | {t['name']} | {t['chain']} |")

    lines += [
        "",
        f"## 院線映画 ({len(chain_movies)}本)",
        "",
        "| # | Rank | タイトル | 国 | 原題 | 公開日 | 上映館 |",
        "|---|------|---------|-----|------|--------|--------|",
    ]
    for i, m in enumerate(chain_movies, 1):
        country = m.get("country") or "?"
        orig = m.get("title_original") or "-"
        rel = m.get("release_date") or "-"
        tnames = ", ".join(m.get("_theater_names", []))
        title_safe = m['title_jp'].replace('|', '')
        orig_safe = orig.replace('|', '')
        lines.append(
            f"| {i} | {m['rank']} | {title_safe} | {country} | {orig_safe} | {rel} | {tnames} |"
        )

    lines += [
        "",
        "---",
        "",
        "## 小众映画館 (独立系)",
        "",
        "| エリア | 映画館 |",
        "|--------|--------|",
    ]
    for t in sorted(indie_theaters, key=lambda x: x["area"]):
        lines.append(f"| {t['area']} | {t['name']} |")

    lines += [
        "",
        f"## 小众映画 ({len(indie_movies)}本)",
        "",
        "| # | Rank | タイトル | 国 | 原題 | 公開日 | 上映館 |",
        "|---|------|---------|-----|------|--------|--------|",
    ]
    for i, m in enumerate(indie_movies, 1):
        country = m.get("country") or "?"
        orig = m.get("title_original") or "-"
        rel = m.get("release_date") or "-"
        tnames = ", ".join(m.get("_theater_names", []))
        title_safe = m['title_jp'].replace('|', '')
        orig_safe = orig.replace('|', '')
        lines.append(
            f"| {i} | {m['rank']} | {title_safe} | {country} | {orig_safe} | {rel} | {tnames} |"
        )

    lines += [
        "",
        "---",
        "",
        f"## その他 ({len(results.get('other', []))}本 — 対象エリア外)",
        "",
    ]

    md_path = outdir / f"step1_{today}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("MD saved: %s", md_path)
    # Also write a short metadata file to indicate successful run time (JST)
    try:
        from datetime import datetime, timezone, timedelta
        JST = timezone(timedelta(hours=9))
        run_ts = datetime.now(JST).isoformat(timespec="seconds")
        meta = {
            "last_run_jst": run_ts,
            "generated_md": str(md_path)
        }
        (outdir / "step1_run_state.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return md_path
