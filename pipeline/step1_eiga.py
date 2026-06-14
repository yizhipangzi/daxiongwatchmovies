"""Step 1

操作1: 登記全東京映画館 from /theater/13/ page
操作2: 抓取上映中映画 + 分類 (院線 / 小众 / other)
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

# 院線対象エリア (東京23区内) — chain theaters here are 院線
CHAIN_AREAS = {"池袋", "上野", "渋谷", "新宿", "日本橋", "目黒", "有楽町", "六本木"}

# 院線チェーン名の判定キーワード
CHAIN_KEYWORDS = {
    "TOHO": ["TOHOシネマズ"],
    "シネマサンシャイン": ["シネマサンシャイン", "グランドシネマサンシャイン"],
    "テアトルシネマ": ["テアトル"],
}

# 小众映画対象エリア (indie theaters)
INDIE_AREAS = {
    "池袋", "恵比寿", "飯田橋", "大塚", "上野",
    "銀座",
    "渋谷", "新宿", "神保町", "水道橋",
    "高田馬場", "田端",
    "日本橋",
    "有楽町",
    "六本木",
}

# 全東京映画館ページ
THEATER_LIST_URL = "https://eiga.com/theater/13/"

# 上映中映画ランキング (東京都)
NOW_SHOWING_URL = "https://eiga.com/now/q/?title=&region=3&pref=13&area=&genre=on&sort=rank"

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
            chain      TEXT,              -- "TOHO" / "シネマサンシャイン" / "テアトルシネマ" / NULL
            area       TEXT,              -- e.g. "池袋"
            area_code  TEXT,              -- e.g. "130501"
            category   TEXT,              -- "chain" (院線) / "indie" / "other"
            url        TEXT
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
    """)
    # Idempotent ALTER for eiga rating fields (added later).
    # eiga_rating is the 1-5 average shown at the top of each movie page;
    # eiga_rating_count is the 全N件 review count next to it.
    # script = 脚本 (screenwriters, " / " joined); cast_names = 出演者 (actor
    # names, " / " joined — `cast` is a SQL keyword so the column is cast_names).
    # director (監督) column already exists above.
    for col_def in ("eiga_rating REAL", "eiga_rating_count INTEGER",
                    "script TEXT", "cast_names TEXT"):
        try:
            conn.execute(f"ALTER TABLE movies ADD COLUMN {col_def}")
        except Exception:
            pass
    # Idempotent ALTER for showtimes.movie_type (added later).
    try:
        conn.execute("ALTER TABLE showtimes ADD COLUMN movie_type TEXT")
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
    """Scrape /theater/13/ page to get ALL Tokyo theaters."""
    soup = _fetch(THEATER_LIST_URL, delay=delay)
    if soup is None:
        return []

    theaters = []
    # The page has sections by area, each theater is a link /theater/13/AREA_CODE/ID/
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        m = re.search(r"/theater/13/(\d+)/(\d+)/", href)
        if not m:
            continue

        area_code = m.group(1)
        theater_num = m.group(2)
        theater_id = f"13/{area_code}/{theater_num}"
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
        area = _detect_area(name)

        # Determine category
        if chain and area in CHAIN_AREAS:
            category = "chain"
        elif area in INDIE_AREAS:
            category = "indie"
        else:
            category = "other"

        theaters.append({
            "theater_id": theater_id,
            "name": name,
            "chain": chain,
            "area": area,
            "area_code": area_code,
            "category": category,
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


def register_theaters(delay: float = 0.5) -> list[dict]:
    """操作1: Scrape ALL Tokyo theaters, diff against DB, insert/delete/update."""
    conn = _get_db()

    existing = {
        row["theater_id"]: dict(row)
        for row in conn.execute("SELECT * FROM theaters").fetchall()
    }

    scraped = _scrape_theater_list_page(delay=delay)
    scraped_map = {t["theater_id"]: t for t in scraped}

    added = [t for tid, t in scraped_map.items() if tid not in existing]
    removed = [t for tid, t in existing.items() if tid not in scraped_map]
    updated = [
        t for tid, t in scraped_map.items()
        if tid in existing and (
            t["name"] != existing[tid]["name"] or
            t["chain"] != existing[tid]["chain"] or
            t["category"] != existing[tid]["category"]
        )
    ]

    for t in added:
        conn.execute(
            """INSERT INTO theaters (theater_id, name, chain, area, area_code, category, url)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (t["theater_id"], t["name"], t["chain"],
             t["area"], t["area_code"], t["category"], t["url"]),
        )
    for t in removed:
        conn.execute("DELETE FROM screenings WHERE theater_id=?", (t["theater_id"],))
        conn.execute("DELETE FROM theaters WHERE theater_id=?", (t["theater_id"],))
    for t in updated:
        conn.execute(
            """UPDATE theaters SET name=?, chain=?, area=?, area_code=?, category=?, url=?
               WHERE theater_id=?""",
            (t["name"], t["chain"], t["area"], t["area_code"], t["category"], t["url"],
             t["theater_id"]),
        )

    conn.commit()

    if added:
        logger.info("Theaters added (%d): %s", len(added),
                    ", ".join(f"[{t['area']}] {t['name']}" for t in added))
    if removed:
        logger.info("Theaters removed (%d): %s", len(removed),
                    ", ".join(f"[{t['area']}] {t['name']}" for t in removed))
    if updated:
        logger.info("Theaters updated (%d): %s", len(updated),
                    ", ".join(f"[{t['area']}] {t['name']}" for t in updated))
    if not added and not removed and not updated:
        logger.info("Theaters unchanged (%d total)", len(scraped))

    chain_theaters = [t for t in scraped if t["category"] == "chain"]
    indie_theaters = [t for t in scraped if t["category"] == "indie"]
    logger.info("  院線 (chain): %d  小众 (indie): %d  total: %d",
                len(chain_theaters), len(indie_theaters), len(scraped))

    conn.close()
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


# ── 操作2: 上映中映画抓取 ────────────────────────────────────────────────────

def _scrape_now_showing(delay: float = 0.5) -> list[dict]:
    """Scrape all pages of now-showing movies (Tokyo, by rank)."""
    movies = []
    page = 1
    rank = 0
    base = NOW_SHOWING_URL

    while True:
        url = base if page == 1 else f"{base}&page={page}"
        soup = _fetch(url, delay=delay)
        if soup is None:
            break

        found_any = False
        for h2 in soup.select("h2"):
            a = h2.select_one("a[href*='/movie/']")
            if not a:
                continue
            href = a.get("href", "")
            m = re.match(r"(?:https://eiga\.com)?/movie/(\d+)/", href)
            if not m:
                continue

            found_any = True
            rank += 1
            movie_id = m.group(1)
            title = a.get_text(strip=True)
            # Remove pipe characters that break MD tables
            title = title.replace("|", "")

            movies.append({
                "movie_id": movie_id,
                "title_jp": title,
                "rank": rank,
                "eiga_url": f"https://eiga.com/movie/{movie_id}/",
            })

        if not found_any:
            break
        page += 1

    return movies


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


def _scrape_theater_list(movie_id: str, delay: float = 0.5) -> list[str]:
    """Fetch '映画館を探す' page for a movie in Tokyo (pref=13), return theater_ids."""
    url = f"https://eiga.com/movie-pref/{movie_id}/13/"
    soup = _fetch(url, delay=delay)
    if soup is None:
        return []

    theater_ids = []
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        m = re.search(r"/movie-theater/\d+/(\d+/\d+/\d+)/", href)
        if m:
            tid = m.group(1)
            if tid not in theater_ids:
                theater_ids.append(tid)
    return theater_ids


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
            for btn in td.select(".btn"):
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


def scrape_movies(delay: float = 0.5, scrape_showtimes: bool = True) -> dict:
    """操作2: Scrape ALL now-showing movies, classify as chain/indie/other.

    Args:
        delay: per-request delay (seconds).
        scrape_showtimes: also fetch each movie's per-theater weekly schedule and
            store it in the `showtimes` table. Adds one request per (movie, theater),
            so it can be disabled for a fast metadata-only run.

    Returns dict with keys: "chain", "indie", "other" — each a list of movie dicts.
    """
    conn = _get_db()

    # 清理过期排片：删除「处理日前一天」之前的旧场次（JST）。在映电影的场次每轮会
    # 先删后写保持最新，但下线电影（不再被抓取）的旧场次会残留，这里统一清掉。
    from datetime import datetime as _dt2, timezone as _tz2, timedelta as _td2
    _cutoff = (_dt2.now(_tz2(_td2(hours=9))).date() - _td2(days=1)).isoformat()
    _purged = conn.execute("DELETE FROM showtimes WHERE show_date < ?", (_cutoff,)).rowcount
    conn.commit()
    if _purged:
        logger.info("Step1: 清理 %d 条 %s 之前的过期排片", _purged, _cutoff)

    # Get theater sets by chain membership. Definition: any theater with a non-empty
    # 'chain' field is considered a chain theater. Any theater not in chain_tids is
    # treated as non-chain (indie candidate). This means 'indie' is defined as
    # "not in chain theaters" regardless of geographic area.
    chain_tids = {row["theater_id"] for row in
                  conn.execute("SELECT theater_id FROM theaters WHERE chain IS NOT NULL AND chain<>''").fetchall()}
    all_tids = {row["theater_id"] for row in
                conn.execute("SELECT theater_id FROM theaters").fetchall()}
    # indie_tids are any known theaters that are not chain theaters
    indie_tids = all_tids - chain_tids

    logger.info("Theater counts — chain: %d, indie: %d, total: %d",
                len(chain_tids), len(indie_tids), len(all_tids))

    # Scrape movie listing
    # Check last run date (Tokyo timezone) and skip if already run today
    from datetime import datetime, timezone, timedelta
    try:
        last = _get_run_state("last_run_date")
        if last:
            # Stored as ISO date in Japan local date
            last_date = datetime.fromisoformat(last).date()
            # Current date in JST
            JST = timezone(timedelta(hours=9))
            today_jst = datetime.now(JST).date()
            if last_date == today_jst:
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
                    cat = s["category"] or "other"
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
                    # Snapshots exist but movies table is empty — fall through to re-scrape.
                    logger.warning(
                        "Step1 skip: %d snapshots exist but movies table is empty. "
                        "Re-scraping to repopulate movies table.", len(snaps)
                    )
    except Exception:
        # If state check fails, proceed normally
        pass

    # 新一轮：清零 fetcher 的请求计数/冷却状态
    from scraper import fetcher
    fetcher.reset_state()

    logger.info("Scraping now-showing movies...")
    movies = _scrape_now_showing(delay=delay)
    logger.info("Found %d movies in ranking", len(movies))

    # 断点续跑：本轮（今天 JST）已写过快照的电影视为「已抓完」，直接跳过重抓。
    # 每部抓完会立刻 commit + 写快照（见循环末尾），所以被中途杀掉后重跑只补未完成的。
    from datetime import datetime as _dt, timezone as _tz, timedelta as _tdj
    today_jst = _dt.now(_tz(_tdj(hours=9))).date().isoformat()
    done_today = {
        r["movie_id"] for r in conn.execute(
            "SELECT movie_id FROM movie_snapshots WHERE snapshot_date=?", (today_jst,)
        ).fetchall()
    }
    if done_today:
        logger.info("Step1 续跑：今天已抓完 %d 部，将跳过", len(done_today))

    results = {"chain": [], "indie": [], "other": []}


    for i, mov in enumerate(movies, 1):
        mid = mov["movie_id"]
        title_jp = mov["title_jp"]

        # 已在本轮抓完 → 从 DB 取回分类结果，跳过重抓（断点续跑，不重复已完成操作）。
        if mid in done_today:
            db_row = conn.execute("SELECT * FROM movies WHERE movie_id=?", (mid,)).fetchone()
            if db_row:
                m = dict(db_row)
                m["rank"] = mov["rank"]
                cat = m.get("category") or "other"
                m["_theaters"] = [
                    r["theater_id"] for r in conn.execute(
                        "SELECT theater_id FROM screenings WHERE movie_id=?", (mid,)
                    ).fetchall()
                ]
                m["_theater_names"] = [
                    r["name"] for r in conn.execute(
                        "SELECT t.name FROM screenings s JOIN theaters t "
                        "ON t.theater_id=s.theater_id WHERE s.movie_id=?", (mid,)
                    ).fetchall()
                ]
                results.setdefault(cat, []).append(m)
                logger.info("[%d/%d] %s | %s（已抓，跳过）", i, len(movies), cat, title_jp)
                continue

        # fetcher 冷却用尽 → 干净停下，已抓数据已落库，下次续跑补完
        if fetcher.exhausted():
            logger.warning("Step1：抓取冷却用尽，本轮提前停止（已抓 %d 部，下次续跑）", i - 1)
            break
        # Check if title_jp exists in DB, reuse fields if so. director/script/cast
        # were added later, so include them to detect (and backfill) old rows.
        row = conn.execute(
            "SELECT country, title_original, year, duration, release_date, director, "
            "eiga_rating, eiga_rating_count, script, cast_names FROM movies WHERE title_jp=?",
            (title_jp,)
        ).fetchone()
        existing_eiga_rating = row["eiga_rating"] if row else None
        existing_eiga_rating_count = row["eiga_rating_count"] if row else None

        # Re-fetch the detail page if any key field is missing: country, eiga_rating
        # (NULL only — 0 is a real "no rating" value), director, or cast. This
        # backfills staff/cast for rows scraped before those columns existed.
        # eiga_rating==0 means the page had no rating; that's a real value, not a gap.
        reuse = bool(
            row and row["country"] and existing_eiga_rating is not None
            and row["director"] and row["cast_names"]
        )
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
            detail = _scrape_movie_detail(mid, delay=delay)
            mov.update(detail)

        # Get all theaters showing this movie
        theater_ids = _scrape_theater_list(mid, delay=delay)

        # Determine category based on whether any of the showing theaters is
        # classified as chain or indie. "other" theaters (outside Tokyo 23-ku
        # target areas, e.g. CINEMA NEKO in 東あきる野) must NOT promote a movie
        # to indie — they should make the movie "other" and excluded from briefing.
        # Rules:
        #   - chain: at least one showing theater is a chain theater
        #   - indie: showing theaters include indie (but no chain) theaters
        #   - other: only "other" theaters or no Tokyo theaters at all
        relevant_tids = chain_tids | indie_tids
        theater_ids_present = [tid for tid in theater_ids if tid in relevant_tids]
        chain_matches = [tid for tid in theater_ids_present if tid in chain_tids]

        if chain_matches:
            category = "chain"
        elif theater_ids_present:
            category = "indie"
        else:
            category = "other"

        # For display and MD, show all known theaters where the movie is playing
        # (do not restrict by area). For DB screenings we also record all known
        # theatre matches.
        mov["_theaters"] = theater_ids_present

        mov["category"] = category

        # Save movie to master movies DB. Core metadata (country, title_original
        # etc.) is preserved from the first run, but `category` and `rank` must be
        # refreshed every run — otherwise a movie that started in chain theaters
        # and later moved to indie/other would forever stay classified as chain.
        existing = conn.execute("SELECT movie_id FROM movies WHERE movie_id=?", (mid,)).fetchone()
        if not existing:
            conn.execute(
                """INSERT INTO movies
                   (movie_id, title_jp, country, title_original, year,
                    duration, release_date, director, rank, category, eiga_url,
                    eiga_rating, eiga_rating_count, script, cast_names)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (mid, mov["title_jp"], mov.get("country"), mov.get("title_original"),
                 mov.get("year"), mov.get("duration"), mov.get("release_date"),
                 mov.get("director"), mov["rank"], category, mov["eiga_url"],
                 mov.get("eiga_rating"), mov.get("eiga_rating_count"),
                 mov.get("script"), mov.get("cast")),
            )
        else:
            # Full UPDATE so re-fetched detail (country / eiga_rating / etc.)
            # actually lands in DB. COALESCE preserves existing values when the
            # new fetch returned nothing for a column.
            conn.execute(
                """UPDATE movies SET
                       category=?, rank=?,
                       country=COALESCE(?, country),
                       title_original=COALESCE(?, title_original),
                       year=COALESCE(?, year),
                       duration=COALESCE(?, duration),
                       release_date=COALESCE(?, release_date),
                       director=COALESCE(?, director),
                       eiga_rating=COALESCE(?, eiga_rating),
                       eiga_rating_count=COALESCE(?, eiga_rating_count),
                       script=COALESCE(?, script),
                       cast_names=COALESCE(?, cast_names)
                     WHERE movie_id=?""",
                (category, mov["rank"],
                 mov.get("country"), mov.get("title_original"),
                 mov.get("year"), mov.get("duration"),
                 mov.get("release_date"), mov.get("director"),
                 mov.get("eiga_rating"), mov.get("eiga_rating_count"),
                 mov.get("script"), mov.get("cast"),
                 mid),
            )

        # Save screenings for matching theaters (all known showing theaters)
        relevant_tids = theater_ids_present
        for tid in relevant_tids:
            conn.execute(
                "INSERT OR IGNORE INTO screenings (movie_id, theater_id) VALUES (?, ?)",
                (mid, tid),
            )

        # Save real showtimes per theater. Showtimes change daily, so wipe this
        # movie's previous showtimes and re-store the fresh weekly schedule.
        if scrape_showtimes:
            conn.execute("DELETE FROM showtimes WHERE movie_id=?", (mid,))
            total_slots = 0
            for tid in relevant_tids:
                slots = _scrape_showtimes(mid, tid, delay=delay)
                for s in slots:
                    conn.execute(
                        """INSERT OR REPLACE INTO showtimes
                           (movie_id, theater_id, show_date, start_time, end_time, ticket_url, movie_type)
                           VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (mid, tid, s["show_date"], s["start_time"],
                         s["end_time"], s["ticket_url"],
                         json.dumps(s.get("types") or [], ensure_ascii=False)),
                    )
                total_slots += len(slots)
            if total_slots:
                logger.info("[Step1] %s: %d showtimes across %d theaters",
                            title_jp, total_slots, len(relevant_tids))

        # Insert a daily snapshot row (snapshot_date, rank, movie_id)
        # Use JST date to stay consistent with last_run_date which is also stored as JST.
        from datetime import datetime, timezone, timedelta as _td
        snapshot_date = datetime.now(timezone(_td(hours=9))).date().isoformat()
        conn.execute(
            "INSERT OR REPLACE INTO movie_snapshots (snapshot_date, rank, movie_id, category) VALUES (?,?,?,?)",
            (snapshot_date, mov["rank"], mid, category),
        )

        # Get theater names for display
        theater_names = []
        for tid in mov["_theaters"]:
            row = conn.execute("SELECT name FROM theaters WHERE theater_id=?", (tid,)).fetchone()
            if row:
                theater_names.append(row["name"])
        mov["_theater_names"] = theater_names

        icon = {"chain": "🎬", "indie": "🎭", "other": "⏭️"}[category]
        logger.info(
            "[%d/%d] #%d %s %s | %s | %s",
            i, len(movies), mov["rank"], icon, category,
            mov["title_jp"],
            mov.get("country") or "?",
        )

        results[category].append(mov)

        # 每部抓完立刻落库：免费云中途被杀也已保存，重跑跳过本部。
        conn.commit()

    conn.commit()
    conn.close()

    logger.info("=== Result: chain=%d, indie=%d, other=%d (total=%d) ===",
                len(results["chain"]), len(results["indie"]),
                len(results["other"]), len(movies))
    # Record last successful run date in JST
    try:
        from datetime import datetime, timezone, timedelta
        JST = timezone(timedelta(hours=9))
        today_jst = datetime.now(JST).date().isoformat()
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


# ── MD出力 ───────────────────────────────────────────────────────────────────

def generate_step1_md(results: dict, theaters: list[dict]) -> Path:
    """Generate step1 MD report with chain and indie sections."""
    from datetime import date
    today = date.today().isoformat()
    outdir = Path("output")
    outdir.mkdir(exist_ok=True)

    chain_theaters = [t for t in theaters if t["category"] == "chain"]
    indie_theaters = [t for t in theaters if t["category"] == "indie"]
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
