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
_RELEASE_RE = re.compile(r"劇場公開日\s*(\d{4}年\d{1,2}月\d{1,2}日)")


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
    for col_def in ("eiga_rating REAL", "eiga_rating_count INTEGER"):
        try:
            conn.execute(f"ALTER TABLE movies ADD COLUMN {col_def}")
        except Exception:
            pass
    conn.commit()
    return conn


def _fetch(url: str, delay: float = 0.5, max_retries: int = 3) -> Optional[BeautifulSoup]:
    for attempt in range(1, max_retries + 1):
        time.sleep(delay)
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=15)
            resp.raise_for_status()
            resp.encoding = "utf-8"
            return BeautifulSoup(resp.text, "lxml")
        except Exception as exc:
            logger.warning("fetch failed (attempt %d/%d) (%s): %s",
                           attempt, max_retries, url, exc)
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


def _scrape_movie_detail(movie_id: str, delay: float = 0.5) -> dict:
    """Fetch movie detail page, extract country/original title/year/duration/release/director."""
    url = f"https://eiga.com/movie/{movie_id}/"
    soup = _fetch(url, delay=delay)
    if soup is None:
        return {}

    text = soup.get_text()
    info: dict = {}

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

    # eiga.com user rating (X.Y on a 5-point scale) + total review count "全N件"
    rating_el = soup.select_one(".rating-star")
    if rating_el:
        try:
            info["eiga_rating"] = float(rating_el.get_text(strip=True))
        except ValueError:
            pass
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


def scrape_movies(delay: float = 0.5) -> dict:
    """操作2: Scrape ALL now-showing movies, classify as chain/indie/other.

    Returns dict with keys: "chain", "indie", "other" — each a list of movie dicts.
    """
    conn = _get_db()

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

    logger.info("Scraping now-showing movies...")
    movies = _scrape_now_showing(delay=delay)
    logger.info("Found %d movies in ranking", len(movies))

    results = {"chain": [], "indie": [], "other": []}


    for i, mov in enumerate(movies, 1):
        mid = mov["movie_id"]
        title_jp = mov["title_jp"]
        # Check if title_jp exists in DB, reuse fields if so
        row = conn.execute(
            "SELECT country, title_original, year, duration, release_date, director FROM movies WHERE title_jp=?",
            (title_jp,)
        ).fetchone()
        # Reuse DB row only when the key fields are populated. If country or
        # eiga_rating is missing (older step1 versions saved neither), re-fetch
        # the detail page instead of carrying the gap forward.
        existing_eiga_rating = None
        existing_eiga_rating_count = None
        try:
            extra = conn.execute(
                "SELECT eiga_rating, eiga_rating_count FROM movies WHERE title_jp=?",
                (title_jp,),
            ).fetchone()
            if extra:
                existing_eiga_rating, existing_eiga_rating_count = extra[0], extra[1]
        except Exception:
            pass

        # Re-fetch if country missing OR eiga_rating never recorded (NULL).
        # eiga_rating==0 means the page had no rating; that's a real value, not a gap.
        if row and row[0] and existing_eiga_rating is not None:
            mov.update({
                "country": row[0],
                "title_original": row[1],
                "year": row[2],
                "duration": row[3],
                "release_date": row[4],
                "director": row[5],
                "eiga_rating": existing_eiga_rating,
                "eiga_rating_count": existing_eiga_rating_count,
            })
            logger.info(f"[Step1] Skipped detail for {title_jp} (reuse DB)")
        else:
            if row:
                missing = ("country" if not (row and row[0]) else "eiga_rating")
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
                    eiga_rating, eiga_rating_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (mid, mov["title_jp"], mov.get("country"), mov.get("title_original"),
                 mov.get("year"), mov.get("duration"), mov.get("release_date"),
                 mov.get("director"), mov["rank"], category, mov["eiga_url"],
                 mov.get("eiga_rating"), mov.get("eiga_rating_count")),
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
                       eiga_rating_count=COALESCE(?, eiga_rating_count)
                     WHERE movie_id=?""",
                (category, mov["rank"],
                 mov.get("country"), mov.get("title_original"),
                 mov.get("year"), mov.get("duration"),
                 mov.get("release_date"), mov.get("director"),
                 mov.get("eiga_rating"), mov.get("eiga_rating_count"),
                 mid),
            )

        # Save screenings for matching theaters (all known showing theaters)
        relevant_tids = theater_ids_present
        for tid in relevant_tids:
            conn.execute(
                "INSERT OR IGNORE INTO screenings (movie_id, theater_id) VALUES (?, ?)",
                (mid, tid),
            )

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
