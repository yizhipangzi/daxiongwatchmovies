"""Step 2: 豆瓣マッチング.

Read chain + indie movies from eiga.db, search Douban for each,
cross-validate year & country, save results with timestamp backup.
"""
from __future__ import annotations

import json
import logging
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path("data/eiga.db")

# ── 日文国名 → 中文国名 映射 ─────────────────────────────────────────────────
# eiga.com uses Japanese country names, Douban uses Chinese.
# We map JP → CN so we can check if at least one country overlaps.
_COUNTRY_JP_CN = {
    "日本": "日本",
    "アメリカ": "美国",
    "イギリス": "英国",
    "フランス": "法国",
    "ドイツ": "德国",
    "イタリア": "意大利",
    "スペイン": "西班牙",
    "カナダ": "加拿大",
    "オーストラリア": "澳大利亚",
    "韓国": "韩国",
    "中国": "中国",
    "香港": "中国香港",
    "台湾": "中国台湾",
    "インド": "印度",
    "ブラジル": "巴西",
    "メキシコ": "墨西哥",
    "ロシア": "俄罗斯",
    "スウェーデン": "瑞典",
    "デンマーク": "丹麦",
    "ノルウェー": "挪威",
    "フィンランド": "芬兰",
    "ベルギー": "比利时",
    "オランダ": "荷兰",
    "スイス": "瑞士",
    "オーストリア": "奥地利",
    "ポーランド": "波兰",
    "チェコ": "捷克",
    "チェコスロバキア": "捷克斯洛伐克",
    "ハンガリー": "匈牙利",
    "ルーマニア": "罗马尼亚",
    "ギリシャ": "希腊",
    "トルコ": "土耳其",
    "イスラエル": "以色列",
    "イラン": "伊朗",
    "タイ": "泰国",
    "ベトナム": "越南",
    "フィリピン": "菲律宾",
    "インドネシア": "印度尼西亚",
    "マレーシア": "马来西亚",
    "シンガポール": "新加坡",
    "アルゼンチン": "阿根廷",
    "コロンビア": "哥伦比亚",
    "チリ": "智利",
    "ニュージーランド": "新西兰",
    "南アフリカ": "南非",
    "エジプト": "埃及",
    "ナイジェリア": "尼日利亚",
    "アイルランド": "爱尔兰",
    "ポルトガル": "葡萄牙",
    "アイスランド": "冰岛",
    "ブルガリア": "保加利亚",
    "クロアチア": "克罗地亚",
    "セルビア": "塞尔维亚",
    "ウクライナ": "乌克兰",
    "ジョージア": "格鲁吉亚",
    "カザフスタン": "哈萨克斯坦",
    "モンゴル": "蒙古",
    "パキスタン": "巴基斯坦",
    "バングラデシュ": "孟加拉国",
    "スリランカ": "斯里兰卡",
    "ミャンマー": "缅甸",
    "カンボジア": "柬埔寨",
    "ラオス": "老挝",
    "ネパール": "尼泊尔",
    "ペルー": "秘鲁",
    "キューバ": "古巴",
    "ルクセンブルク": "卢森堡",
}

# Douban sometimes uses alternate names
_CN_ALIASES = {
    "英国": ["英国", "UK"],
    "美国": ["美国", "USA"],
    "中国香港": ["中国香港", "香港"],
    "中国台湾": ["中国台湾", "台湾"],
    "日本": ["日本"],
}


def _parse_jp_countries(eiga_country: str) -> list[str]:
    """Parse eiga.com country field into list of CN country names.

    eiga.com format: 'アメリカ・ブルガリア合作' or 'アメリカ' or 'フランス・イギリス・ドイツ合作'
    """
    if not eiga_country:
        return []
    # Remove '合作' suffix
    s = eiga_country.replace("合作", "").strip()
    # Split on ・
    parts = [p.strip() for p in s.split("・") if p.strip()]
    cn_list = []
    for p in parts:
        cn = _COUNTRY_JP_CN.get(p)
        if cn:
            cn_list.append(cn)
        else:
            # Keep original as fallback (won't match but at least logged)
            cn_list.append(p)
    return cn_list


def _check_country_match(eiga_country: str, douban_country: str) -> bool:
    """Check if at least one country from eiga matches douban's country list.

    Douban format: '美国 / 保加利亚' or '日本'
    """
    if not eiga_country or not douban_country:
        return True  # can't verify, assume OK

    jp_cn_list = _parse_jp_countries(eiga_country)
    # Douban countries: split on ' / '
    db_countries = [c.strip() for c in douban_country.split("/")]

    for cn in jp_cn_list:
        # Check against aliases
        aliases = _CN_ALIASES.get(cn, [cn])
        for alias in aliases:
            for db_c in db_countries:
                if alias in db_c or db_c in alias:
                    return True
    return False


def _check_year_match(eiga_year: int, douban_year: int) -> bool:
    """Year within ±2 is OK."""
    if not eiga_year or not douban_year:
        return True  # can't verify
    return abs(eiga_year - douban_year) <= 2


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_douban_table(conn: sqlite3.Connection):
    """Create douban_matches table + backup table."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS douban_matches (
            movie_id     TEXT PRIMARY KEY,
            douban_id    TEXT,
            title_cn     TEXT,
            douban_score REAL,
            douban_votes INTEGER,
            director     TEXT,
            cast         TEXT,
            genre        TEXT,
            douban_year  INTEGER,
            douban_country TEXT,
            douban_url   TEXT,
            year_ok      INTEGER,  -- 1=match, 0=mismatch
            country_ok   INTEGER,  -- 1=match, 0=mismatch
            verified     INTEGER,  -- 1=both OK, 0=suspect
            matched_at   TEXT,     -- ISO timestamp
            FOREIGN KEY (movie_id) REFERENCES movies(movie_id)
        );
        CREATE TABLE IF NOT EXISTS douban_matches_history (
            movie_id     TEXT,
            douban_id    TEXT,
            douban_score REAL,
            douban_votes INTEGER,
            snapshot_at  TEXT,
            PRIMARY KEY (movie_id, snapshot_at)
        );
        CREATE TABLE IF NOT EXISTS douban_details (
            movie_id TEXT PRIMARY KEY,
            douban_id TEXT,
            title_cn TEXT,
            douban_score REAL,
            douban_votes INTEGER,
            director TEXT,
            cast TEXT,
            genre TEXT,
            douban_year INTEGER,
            douban_country TEXT,
            douban_url TEXT,
            want_to_watch INTEGER,
            watched INTEGER,
            trailer_urls TEXT, -- JSON array
            short_reviews TEXT, -- JSON array of selected reviews
            updated_at TEXT
        );
        CREATE TABLE IF NOT EXISTS douban_skip_list (
            movie_id TEXT PRIMARY KEY,
            reason   TEXT,
            noted_at TEXT
        );
        -- Eiga.com short reviews for movies in the "电影日和" briefing section
        -- (chain releases with no Douban score but a Japanese-audience rating).
        -- Each row: one review. body_zh is the translated text (may be empty
        -- if translation isn't configured / failed).
        CREATE TABLE IF NOT EXISTS eiga_reviews (
            movie_id   TEXT NOT NULL,
            position   INTEGER NOT NULL,   -- 0..n-1, page order
            review_id  TEXT,
            rating     REAL,               -- reviewer's 1-5 score
            title_ja   TEXT,
            title_zh   TEXT,
            author     TEXT,
            date_str   TEXT,
            body_ja    TEXT,
            body_zh    TEXT,
            updated_at TEXT,
            PRIMARY KEY (movie_id, position)
        );
    """)
    # Add search_attempts column for existing DBs (idempotent)
    try:
        conn.execute("ALTER TABLE douban_matches ADD COLUMN search_attempts INTEGER DEFAULT 0")
        conn.commit()
    except Exception:
        pass
    # eiga.com 1-5 rating + review count, used by the 电影日和 section.
    # step1 also adds these, but step2 may be invoked standalone (e.g. for
    # briefing-only runs) so we re-assert here.
    for col_def in ("eiga_rating REAL", "eiga_rating_count INTEGER"):
        try:
            conn.execute(f"ALTER TABLE movies ADD COLUMN {col_def}")
            conn.commit()
        except Exception:
            pass
    conn.commit()


def _load_config(path: str = "config.yaml") -> dict:
    try:
        import yaml
        p = Path(path)
        if not p.exists():
            p = Path("config.yaml.example")
        if p.exists():
            with p.open(encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    except Exception as exc:
        logger.warning("Could not load config: %s", exc)
    return {}


class _FetchRetryTracker:
    """Shared retry / cool-down tracker for Douban page fetches.

    Used wherever we hit Douban in a loop and need to back off on transient
    failures (bot-check, empty/sorry pages, 403, network blips).

    Behaviour:
      - Caller invokes ``should_continue()`` before each attempt. If the
        consecutive-failure count has hit ``threshold`` for the first time,
        it sleeps for a random cool-down (50-90s by default), resets the
        counter, and returns True. After ``max_cooldowns`` such resets it
        returns False — caller should stop attempting for the rest of the run.
      - Caller invokes ``record_success()`` or ``record_failure()`` per attempt.
    """

    def __init__(self,
                 threshold: int = 3,
                 cooldown_seconds: tuple = (50.0, 90.0),
                 max_cooldowns: int = 1,
                 label: str = "fetch"):
        self._threshold = threshold
        self._cd_min, self._cd_max = cooldown_seconds
        self._max_cooldowns = max_cooldowns
        self._label = label
        self._failures = 0
        self._cooldowns = 0
        self._exhausted = False

    def should_continue(self) -> bool:
        if self._exhausted:
            return False
        if self._failures < self._threshold:
            return True
        if self._cooldowns < self._max_cooldowns:
            cd = random.uniform(self._cd_min, self._cd_max)
            logger.warning(
                "%s: %d consecutive failures — %.0fs cool-down, will retry",
                self._label, self._failures, cd,
            )
            time.sleep(cd)
            self._failures = 0
            self._cooldowns += 1
            return True
        logger.warning(
            "%s: %d consecutive failures after %d cool-down(s) — giving up for this run",
            self._label, self._failures, self._cooldowns,
        )
        self._exhausted = True
        return False

    def record_success(self) -> None:
        self._failures = 0

    def record_failure(self) -> None:
        self._failures += 1

    @property
    def exhausted(self) -> bool:
        return self._exhausted


def _select_reviews_from_soup(soup, score_present: bool) -> list[dict]:
    """Pick up to 3 short reviews (with author info) from a Douban movie page soup.

    Returns list of dicts: {"text", "author", "author_url", "status"}
    """
    from scraper.douban import _parse_short_reviews_labeled
    parsed = _parse_short_reviews_labeled(soup, n=15)
    primary = 'watched' if score_present else 'want'
    selected: list[dict] = []
    seen_texts: set = set()

    def _take(r):
        text = r.get('text') or ''
        if not text or text in seen_texts:
            return
        seen_texts.add(text)
        selected.append({
            "text": text,
            "author": r.get('author', ''),
            "author_url": r.get('author_url', ''),
            "status": r.get('status', 'unknown'),
        })

    for r in parsed:
        if r.get('status') == primary:
            _take(r)
            if len(selected) >= 3:
                break
    if len(selected) < 3:
        for r in parsed:
            _take(r)
            if len(selected) >= 3:
                break
    return selected


def match_movies(categories: tuple = ("chain", "indie"),
                 delay: float = 2.0,
                 resume: bool = True,
                 skip_existing_douban_fetch: bool = False) -> dict:
    """Search Douban for each movie in given categories.

    Returns dict with 'chain' and 'indie' lists of result dicts.
    """
    from scraper.douban import (
        search_douban, _fetch_movie_page, _parse_rating, _parse_meta,
        _parse_short_reviews_labeled, _find_youtube_trailer,
        init_douban_session,
    )

    cfg = _load_config()
    douban_cfg = cfg.get("douban", {})
    if douban_cfg:
        init_douban_session(douban_cfg)
        logger.info("Douban session initialised (cookie: %s)",
                    "yes" if douban_cfg.get("cookie") else "no")

    conn = _get_db()
    _ensure_douban_table(conn)

    now = datetime.now().isoformat(timespec="seconds")
    today_str = now[:10]

    # Delete wrong-match records (has douban_id but not verified — wrong movie, retry)
    bad_del = conn.execute(
        "DELETE FROM douban_matches WHERE verified = 0 AND douban_id IS NOT NULL AND douban_id != ''"
    ).rowcount
    # Drop all unmatched records so every run re-attempts them.
    # Skip-list entries live in douban_skip_list and are handled separately.
    stale_del = conn.execute(
        "DELETE FROM douban_matches WHERE (douban_id IS NULL OR douban_id = '')"
    ).rowcount
    if bad_del + stale_del:
        conn.commit()
    if bad_del:
        logger.info("Cleared %d wrong-match records for retry", bad_del)
    if stale_del:
        logger.info("Cleared %d unmatched records for retry", stale_del)

    # All verified matches are skipped silently on rerun — no log, no request
    done_ids = set()
    if resume:
        rows = conn.execute(
            "SELECT movie_id FROM douban_matches WHERE verified = 1"
        ).fetchall()
        done_ids = {r["movie_id"] for r in rows}
        if done_ids:
            logger.info("Resuming: %d already verified, skipping silently", len(done_ids))

    # Get movies
    movies = conn.execute(
        "SELECT * FROM movies WHERE category IN ({}) ORDER BY category, rank".format(
            ",".join("?" for _ in categories)),
        categories
    ).fetchall()
    logger.info("Total movies to match: %d (skipping %d already done)",
                len(movies), len([m for m in movies if m["movie_id"] in done_ids]))

    results = {c: [] for c in categories}
    score_tracker = _FetchRetryTracker(label="Score refresh")
    _search_count = 0  # counts new-movie searches (not score refreshes)
    _skipped_refreshed_today = 0  # verified matches whose score was already refreshed today

    for i, mov in enumerate(movies, 1):
        mid = mov["movie_id"]
        cat = mov["category"]

        # 1. Verified match — refresh today's score snapshot if not yet done
        if mid in done_ids:
            existing = conn.execute(
                "SELECT * FROM douban_matches WHERE movie_id=?", (mid,)
            ).fetchone()
            if existing and existing["douban_id"]:
                today_str = now[:10]
                # A successful page fetch today (even if score=0) counts as "done".
                # Failed fetches don't write a history row, so they retry automatically.
                refreshed_today = conn.execute(
                    "SELECT 1 FROM douban_matches_history WHERE movie_id=? AND snapshot_at>=?",
                    (mid, today_str)
                ).fetchone()
                if refreshed_today:
                    _skipped_refreshed_today += 1
                elif score_tracker.should_continue():
                    logger.info("[%d/%d] %s | %s — refreshing score",
                                i, len(movies), cat, mov["title_jp"])
                    try:
                        from scraper.douban import _fetch_movie_page, _parse_rating
                        time.sleep(delay + random.uniform(0.5, delay * 0.6))
                        soup = _fetch_movie_page(existing["douban_id"], delay=0)
                        if soup:
                            score, votes = _parse_rating(soup)
                            if score:
                                conn.execute(
                                    "UPDATE douban_matches "
                                    "SET douban_score=?, douban_votes=? "
                                    "WHERE movie_id=?",
                                    (score, votes, mid)
                                )
                                logger.info("[%d/%d] %s | %s 🔄 %.1f (%d votes)",
                                            i, len(movies), cat, mov["title_jp"], score, votes)
                            else:
                                logger.info("[%d/%d] %s | %s — page fetched, no rating yet",
                                            i, len(movies), cat, mov["title_jp"])
                            score_tracker.record_success()
                            # Record the successful fetch (score may be 0). Page fetch
                            # success is what marks the movie as "done today".
                            conn.execute(
                                """INSERT OR IGNORE INTO douban_matches_history
                                   (movie_id, douban_id, douban_score, douban_votes, snapshot_at)
                                   VALUES (?,?,?,?,?)""",
                                (mid, existing["douban_id"], score, votes, now)
                            )
                            conn.commit()
                        else:
                            score_tracker.record_failure()
                    except Exception as exc:
                        score_tracker.record_failure()
                        logger.warning("Score refresh failed for %s: %s", mid, exc)
            results[cat].append({**dict(mov), **dict(existing)} if existing else dict(mov))
            continue

        # 2. User-managed skip list — silent skip
        skip_row = conn.execute(
            "SELECT movie_id FROM douban_skip_list WHERE movie_id=?", (mid,)
        ).fetchone()
        if skip_row:
            results[cat].append(dict(mov))
            continue

        # Only movies that will actually be searched get logged
        title_jp = mov["title_jp"]
        title_jp_clean = re.sub(r'[（(]\d{4}[）)]$', '', title_jp).strip()
        title_orig = mov["title_original"] or ""
        year = mov["year"] or 0

        # Inter-movie pause: simulate a human pausing between searches
        _search_count += 1
        if _search_count > 1:
            inter_pause = random.uniform(1.5, 4.0)
            time.sleep(inter_pause)
        # Breathing break every 5 new searches
        if _search_count > 1 and (_search_count - 1) % 5 == 0:
            breath = random.uniform(12, 25)
            logger.info("Breathing pause: %.0fs after %d searches ...", breath, _search_count - 1)
            time.sleep(breath)

        logger.info("[%d/%d] %s | %s", i, len(movies), cat, title_jp)

        try:
            match = search_douban(title_jp_clean, year=year, delay=delay,
                                  orig_title=title_orig or None,
                                  country=mov["country"] or None)

            if not match:
                # Record the attempt so the unmatched table can show it;
                # the row is wiped at the start of the next run so it retries.
                conn.execute(
                    """INSERT INTO douban_matches (movie_id, matched_at, verified)
                       VALUES (?, ?, 0)
                       ON CONFLICT(movie_id) DO UPDATE SET
                           matched_at=excluded.matched_at,
                           verified=0""",
                    (mid, now)
                )
                conn.commit()
                logger.info("  ❌ No Douban match (will retry next run)")
                results[cat].append(dict(mov))
                continue

            subject_id = match.get("id", "")
            if not subject_id:
                logger.info("  ❌ No subject ID in match result")
                results[cat].append(dict(mov))
                continue

            # Score from suggest API (no page fetch required)
            rating = match.get("rating") or {}
            try:
                score = float(rating.get("value") or 0)
                votes = int(rating.get("count") or 0)
            except (TypeError, ValueError):
                score, votes = 0.0, 0
            try:
                suggest_year = int(match.get("year") or 0)
            except (TypeError, ValueError):
                suggest_year = 0

            year_ok = _check_year_match(year, suggest_year)

            result = {
                "movie_id": mid,
                "douban_id": subject_id,
                "title_cn": match.get("title", ""),
                "douban_score": score,
                "douban_votes": votes,
                "director": "",
                "cast": "",
                "genre": "",
                "douban_year": suggest_year,
                "douban_country": "",
                "douban_url": f"https://movie.douban.com/subject/{subject_id}/",
                "year_ok": 1 if year_ok else 0,
                "country_ok": 0,
                "verified": 1 if year_ok else 0,
                "matched_at": now,
            }

            conn.execute(
                """INSERT OR REPLACE INTO douban_matches
                   (movie_id, douban_id, title_cn, douban_score, douban_votes,
                    director, cast, genre, douban_year, douban_country, douban_url,
                    year_ok, country_ok, verified, matched_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (mid, subject_id, result["title_cn"], score, votes,
                 "", "", "", suggest_year, "", result["douban_url"],
                 result["year_ok"], 0, result["verified"], now)
            )
            # The suggest API returned a result — stamp history so this movie is
            # marked "done today" even if the suggest score is 0 (no ratings yet).
            conn.execute(
                """INSERT OR REPLACE INTO douban_matches_history
                   (movie_id, douban_id, douban_score, douban_votes, snapshot_at)
                   VALUES (?,?,?,?,?)""",
                (mid, subject_id, score, votes, now)
            )
            conn.commit()

            icon = "✅" if year_ok else "⚠️"
            logger.info("  %s id=%s score=%.1f votes=%d year:%s",
                        icon, subject_id, score, votes,
                        "OK" if year_ok else f"NG({year}→{suggest_year})")
            results[cat].append({**dict(mov), **result})

        except Exception as exc:
            from scraper.douban import CookieExpiredError
            if isinstance(exc, CookieExpiredError):
                logger.error("  🔒 Cookie blocked — stopping. %s", exc)
                conn.close()
                raise
            logger.error("  💥 Unexpected error processing %s: %s", title_jp, exc)
            results[cat].append(dict(mov))
            continue

    conn.close()

    # Close the persistent Playwright Chrome window (if it was used this run)
    try:
        from scraper.douban import close_pw_browser
        close_pw_browser()
    except Exception:
        pass

    matched = sum(len(v) for v in results.values())
    if _skipped_refreshed_today:
        logger.info("Score refresh: skipped %d movie(s) already refreshed today",
                    _skipped_refreshed_today)
    logger.info("=== Douban matching complete: %d/%d matched ===", matched, len(movies))
    return results


def enrich_top_movies(top_n: int = 15, delay: float = 2.0) -> int:
    """Fetch detail pages for the top N movies by douban_score.

    Step2 only saves IDs + scores from the suggest API (fast, no page fetch).
    Call this in step3/briefing generation to fill in director, cast, genre,
    country, reviews, and trailers for the movies that make the ranking.

    Returns the number of movies enriched.
    """
    from scraper.douban import (
        _fetch_movie_page, _parse_rating, _parse_meta, init_douban_session,
    )

    conn = _get_db()
    cfg = _load_config()
    douban_cfg = cfg.get("douban", {})
    if douban_cfg:
        init_douban_session(douban_cfg)

    rows = conn.execute(
        """SELECT m.movie_id, m.douban_id, m.douban_score,
                  mv.year AS movie_year, mv.country AS movie_country
           FROM douban_matches m
           JOIN movies mv ON m.movie_id = mv.movie_id
           WHERE m.douban_id IS NOT NULL AND m.douban_id != ''
           ORDER BY COALESCE(m.douban_score, 0) DESC
           LIMIT ?""",
        (top_n,)
    ).fetchall()

    enriched = 0
    now = datetime.now().isoformat(timespec="seconds")

    for row in rows:
        mid = row["movie_id"]
        subject_id = row["douban_id"]

        soup = _fetch_movie_page(subject_id, delay=delay)
        if not soup:
            logger.warning("enrich: could not fetch page for id=%s", subject_id)
            continue

        score, votes = _parse_rating(soup)
        meta = _parse_meta(soup)
        if meta.get("is_tv"):
            continue

        douban_year = meta.get("year", 0)
        douban_country = meta.get("country", "")
        year_ok = _check_year_match(row["movie_year"] or 0, douban_year)
        country_ok = _check_country_match(row["movie_country"] or "", douban_country)

        conn.execute(
            """UPDATE douban_matches SET
               title_cn=?, douban_score=?, douban_votes=?,
               director=?, cast=?, genre=?,
               douban_year=?, douban_country=?,
               year_ok=?, country_ok=?, verified=?
               WHERE movie_id=?""",
            (meta.get("title_cn", ""), score or row["douban_score"], votes,
             meta.get("director", ""), meta.get("cast", ""), meta.get("genre", ""),
             douban_year, douban_country,
             1 if year_ok else 0, 1 if country_ok else 0,
             1 if (year_ok and country_ok) else 0,
             mid)
        )

        try:
            sel_reviews = _select_reviews_from_soup(soup, score_present=bool(score))
            short_reviews_json = json.dumps(sel_reviews, ensure_ascii=False)
        except Exception:
            short_reviews_json = json.dumps([])
        trailers_json = json.dumps(meta.get("trailers", []) or [], ensure_ascii=False)

        conn.execute(
            """INSERT OR REPLACE INTO douban_details
               (movie_id, douban_id, title_cn, douban_score, douban_votes,
                director, cast, genre, douban_year, douban_country, douban_url,
                want_to_watch, watched, trailer_urls, short_reviews, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, subject_id, meta.get("title_cn", ""), score, votes,
             meta.get("director", ""), meta.get("cast", ""), meta.get("genre", ""),
             douban_year, douban_country,
             f"https://movie.douban.com/subject/{subject_id}/",
             meta.get("want_to_watch", 0) or 0, meta.get("watched", 0) or 0,
             trailers_json, short_reviews_json, now)
        )
        conn.commit()
        enriched += 1
        logger.info("enrich [%d/%d]: %s %.1f分", enriched, len(rows),
                    meta.get("title_cn", subject_id), score)

    conn.close()
    return enriched


def _save_douban_details(conn, mid: str, subject_id: str, soup,
                         movie_year, movie_country, now: str) -> dict:
    """Parse a fetched Douban movie page and upsert douban_matches + douban_details.

    Shared by the briefing enrich and the full-catalog enrich so the two paths
    can't drift. Returns the parsed meta dict augmented with 'score'/'votes'.
    Existing aggregated scores are never overwritten with a 0 from a miss.
    """
    from scraper.douban import _parse_rating, _parse_meta

    score, votes = _parse_rating(soup)
    meta = _parse_meta(soup)
    meta["score"] = score
    meta["votes"] = votes
    if meta.get("is_tv"):
        return meta

    # 空页面（bot/sorry 页）：没分、没中文名、没导演 → 没真正拿到数据。
    # 不写库（保持 updated_at 为 NULL/旧值），让下次 enrich 重试，确保最终取到，
    # step4 才有东西可输出。
    if not score and not meta.get("title_cn") and not meta.get("director"):
        meta["empty"] = True
        return meta

    douban_year = meta.get("year", 0)
    douban_country = meta.get("country", "")
    year_ok = _check_year_match(movie_year or 0, douban_year)
    country_ok = _check_country_match(movie_country or "", douban_country)

    existing_score = conn.execute(
        "SELECT douban_score FROM douban_matches WHERE movie_id=?", (mid,)
    ).fetchone()
    keep_score = score or (existing_score and existing_score["douban_score"]) or 0

    conn.execute(
        """UPDATE douban_matches SET
           title_cn=?, douban_score=?, douban_votes=?,
           director=?, cast=?, genre=?,
           douban_year=?, douban_country=?,
           year_ok=?, country_ok=?, verified=?
           WHERE movie_id=?""",
        (meta.get("title_cn", ""), keep_score, votes,
         meta.get("director", ""), meta.get("cast", ""), meta.get("genre", ""),
         douban_year, douban_country,
         1 if year_ok else 0, 1 if country_ok else 0,
         1 if (year_ok and country_ok) else 0,
         mid)
    )

    try:
        sel_reviews = _select_reviews_from_soup(soup, score_present=bool(score))
        short_reviews_json = json.dumps(sel_reviews, ensure_ascii=False)
    except Exception:
        short_reviews_json = json.dumps([])
    trailers_json = json.dumps(meta.get("trailers", []) or [], ensure_ascii=False)

    conn.execute(
        """INSERT OR REPLACE INTO douban_details
           (movie_id, douban_id, title_cn, douban_score, douban_votes,
            director, cast, genre, douban_year, douban_country, douban_url,
            want_to_watch, watched, trailer_urls, short_reviews, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (mid, subject_id, meta.get("title_cn", ""), score, votes,
         meta.get("director", ""), meta.get("cast", ""), meta.get("genre", ""),
         douban_year, douban_country,
         f"https://movie.douban.com/subject/{subject_id}/",
         meta.get("want_to_watch", 0) or 0, meta.get("watched", 0) or 0,
         trailers_json, short_reviews_json, now)
    )
    return meta


def enrich_all_movies(categories: tuple = ("chain", "indie"),
                      delay: float = 2.0,
                      refresh_days: int = 7,
                      limit: int = 0,
                      max_cooldowns: int = 3) -> int:
    """Fetch full Douban details for EVERY matched movie — not just the briefing top-5.

    Populates director / cast / genre / 想看 / 看过 / 短评 / trailers into
    ``douban_details`` (and refreshes ``douban_matches``) for all movies in the
    given categories that have a verified Douban id. This is the dataset the
    mini-program needs: chain + indie cover all 23-ku (9-district) theaters.

    Designed to grind through hundreds of movies across multiple days while
    surviving Douban rate-limiting:
      - Movies whose details were refreshed within ``refresh_days`` are skipped,
        so a daily run keeps the whole catalog fresh on a rolling window.
      - Never-enriched movies are processed first (NULL updated_at sorts first).
      - ``limit`` caps how many are fetched per run (0 = no cap).
      - On repeated Douban failures the run cools down up to ``max_cooldowns``
        times, then stops cleanly so the next run can resume.

    Returns the number of movies enriched this run.
    """
    from scraper.douban import _fetch_movie_page, init_douban_session, CookieExpiredError

    cfg = _load_config()
    douban_cfg = cfg.get("douban", {})
    if douban_cfg:
        init_douban_session(douban_cfg)

    conn = _get_db()
    _ensure_douban_table(conn)

    now = datetime.now().isoformat(timespec="seconds")
    cutoff = (datetime.now() - timedelta(days=refresh_days)).isoformat(timespec="seconds")

    cat_ph = ",".join("?" for _ in categories)
    # Verified matches in the target categories, with their last detail-fetch time.
    # NULL updated_at (never enriched) sorts first; then oldest first.
    rows = conn.execute(
        f"""SELECT m.movie_id, m.douban_id, mv.title_jp,
                   mv.year AS movie_year, mv.country AS movie_country,
                   d.updated_at AS details_at,
                   d.title_cn AS d_title_cn, d.director AS d_director
              FROM douban_matches m
              JOIN movies mv ON m.movie_id = mv.movie_id
              LEFT JOIN douban_details d ON d.movie_id = m.movie_id
             WHERE m.douban_id IS NOT NULL AND m.douban_id != ''
               AND m.verified = 1
               AND mv.category IN ({cat_ph})
             ORDER BY (d.updated_at IS NOT NULL), d.updated_at, mv.rank""",
        tuple(categories),
    ).fetchall()

    def _needs_fetch(r) -> bool:
        # 没详情 / 超过刷新窗口 → 要抓。
        if not (r["details_at"] and r["details_at"] >= cutoff):
            return True
        # 在窗口内但内容是空的（旧逻辑可能存过空行）→ 仍要重抓，确保取到真数据。
        empty = not (r["d_title_cn"] or "").strip() and not (r["d_director"] or "").strip()
        return empty

    targets = [r for r in rows if _needs_fetch(r)]
    skipped_fresh = len(rows) - len(targets)
    if limit > 0:
        targets = targets[:limit]

    if skipped_fresh:
        logger.info("Full enrich: %d movie(s) still fresh (<%dd), skipping",
                    skipped_fresh, refresh_days)
    if not targets:
        logger.info("Full enrich: nothing to do (%d matched movies all fresh)", len(rows))
        conn.close()
        return 0

    logger.info("Full enrich: %d movie(s) to fetch (of %d matched)", len(targets), len(rows))
    enriched = 0
    tracker = _FetchRetryTracker(label="Full enrich", max_cooldowns=max_cooldowns)
    try:
        for idx, t in enumerate(targets, 1):
            if idx > 1:
                time.sleep(random.uniform(1.5, 4.0))
                if (idx - 1) % 5 == 0:
                    breath = random.uniform(12, 25)
                    logger.info("Breathing pause: %.0fs after %d fetches", breath, idx - 1)
                    time.sleep(breath)

            if not tracker.should_continue():
                logger.info("Full enrich [%d/%d]: retry budget exhausted — stopping",
                            idx, len(targets))
                break

            mid = t["movie_id"]
            subject_id = t["douban_id"]
            try:
                soup = _fetch_movie_page(subject_id, delay=delay)
            except CookieExpiredError:
                logger.error("Full enrich: cookie blocked — stopping so it can resume later")
                break
            except Exception as exc:
                tracker.record_failure()
                logger.warning("Full enrich: fetch error for id=%s: %s", subject_id, exc)
                continue
            if not soup:
                tracker.record_failure()
                logger.warning("Full enrich: could not fetch page for id=%s", subject_id)
                continue

            meta = _save_douban_details(conn, mid, subject_id, soup,
                                        t["movie_year"], t["movie_country"], now)
            if meta.get("is_tv"):
                tracker.record_success()
                continue
            # 空页面（bot/sorry）：_save_douban_details 已判定并**未入库**，
            # 这里只记失败 + 跳过，下次重试（确保最终取到，step4 才有数据）。
            if meta.get("empty"):
                tracker.record_failure()
                logger.warning("Full enrich [%d/%d]: 空页面 id=%s — 未入库，下次重试",
                               idx, len(targets), subject_id)
                continue

            tracker.record_success()
            conn.commit()
            enriched += 1
            logger.info("Full enrich [%d/%d] %s | %s %.1f分 (%d看过/%d想看)",
                        idx, len(targets), t["title_jp"],
                        meta.get("title_cn", subject_id), meta.get("score") or 0,
                        meta.get("watched", 0) or 0, meta.get("want_to_watch", 0) or 0)
    finally:
        try:
            from scraper.douban import close_pw_browser
            close_pw_browser()
        except Exception:
            pass
        conn.close()

    logger.info("Full enrich: %d/%d enriched this run", enriched, len(targets))
    return enriched


def enrich_for_briefing(top_n: int = 5, delay: float = 2.0) -> int:
    """Enrich the movies that will be displayed in the WeChat briefing.

    Uses ``generator.briefing.select_briefing_movie_ids`` so that the SQL
    here can't drift from the filter rules in ``briefing.py``. Fetches
    director / cast / genre / watched / want / short reviews (with author
    info) from each Douban movie page and saves to ``douban_matches`` +
    ``douban_details``.

    Returns the number of movies enriched.
    """
    from scraper.douban import (
        _fetch_movie_page, _parse_rating, _parse_meta, init_douban_session,
    )
    from generator.briefing import select_briefing_movie_ids

    cfg = _load_config()
    douban_cfg = cfg.get("douban", {})
    if douban_cfg:
        init_douban_session(douban_cfg)

    conn = _get_db()
    _ensure_douban_table(conn)

    now = datetime.now().isoformat(timespec="seconds")
    today_str = now[:10]

    # Ask the briefing generator which movie_ids it would display, then
    # enrich exactly those.
    wanted_ids = select_briefing_movie_ids(top_n=top_n)
    if not wanted_ids:
        logger.info("Briefing enrich: no candidates")
        conn.close()
        return 0

    placeholders = ",".join("?" for _ in wanted_ids)
    rows = conn.execute(
        f"""SELECT m.movie_id, m.douban_id,
                   mv.year AS movie_year, mv.country AS movie_country,
                   SUBSTR(COALESCE(d.updated_at, ''), 1, 10) AS details_day
              FROM douban_matches m JOIN movies mv ON m.movie_id=mv.movie_id
              LEFT JOIN douban_details d ON d.movie_id=m.movie_id
             WHERE m.movie_id IN ({placeholders})""",
        tuple(wanted_ids),
    ).fetchall()
    targets: list[dict] = []
    skipped_today = 0
    for r in rows:
        if r["details_day"] == today_str:
            skipped_today += 1
            continue
        targets.append({
            "section": "briefing",
            "movie_id": r["movie_id"],
            "douban_id": r["douban_id"],
            "movie_year": r["movie_year"],
            "movie_country": r["movie_country"],
        })

    if skipped_today:
        logger.info("Briefing enrich: skipping %d movie(s) already enriched today",
                    skipped_today)

    if not targets:
        logger.info("Briefing enrich: no candidates")
        conn.close()
        return 0

    logger.info("Briefing enrich: %d unique movies across 4 sections", len(targets))
    enriched = 0
    tracker = _FetchRetryTracker(label="Briefing enrich")
    for idx, t in enumerate(targets, 1):
        # Inter-movie pause
        if idx > 1:
            time.sleep(random.uniform(1.5, 4.0))
            if (idx - 1) % 5 == 0:
                breath = random.uniform(12, 25)
                logger.info("Breathing pause: %.0fs after %d briefing fetches",
                            breath, idx - 1)
                time.sleep(breath)

        if not tracker.should_continue():
            logger.info("Briefing [%d/%d]: retry budget exhausted — stopping", idx, len(targets))
            break

        mid = t["movie_id"]
        subject_id = t["douban_id"]

        try:
            soup = _fetch_movie_page(subject_id, delay=delay)
        except Exception as exc:
            tracker.record_failure()
            logger.warning("Briefing enrich: fetch error for id=%s: %s", subject_id, exc)
            continue
        if not soup:
            tracker.record_failure()
            logger.warning("Briefing enrich: could not fetch page for id=%s", subject_id)
            continue

        score, votes = _parse_rating(soup)
        meta = _parse_meta(soup)
        if meta.get("is_tv"):
            tracker.record_success()
            continue
        # Empty page heuristic: this movie was selected because douban_score>0,
        # so getting nothing back means a bot/sorry page rather than real data.
        if not score and not meta.get("title_cn") and not meta.get("director"):
            tracker.record_failure()
            logger.warning(
                "Briefing [%d/%d]: empty page for id=%s (likely bot/sorry page) — retry later",
                idx, len(targets), subject_id,
            )
            continue
        tracker.record_success()

        douban_year = meta.get("year", 0)
        douban_country = meta.get("country", "")
        year_ok = _check_year_match(t["movie_year"] or 0, douban_year)
        country_ok = _check_country_match(t["movie_country"] or "", douban_country)

        # Don't overwrite an existing score with 0 if today's fetch missed it
        existing_score = conn.execute(
            "SELECT douban_score FROM douban_matches WHERE movie_id=?", (mid,)
        ).fetchone()
        keep_score = score or (existing_score and existing_score["douban_score"]) or 0
        conn.execute(
            """UPDATE douban_matches SET
               title_cn=?, douban_score=?, douban_votes=?,
               director=?, cast=?, genre=?,
               douban_year=?, douban_country=?,
               year_ok=?, country_ok=?, verified=?
               WHERE movie_id=?""",
            (meta.get("title_cn", ""), keep_score, votes,
             meta.get("director", ""), meta.get("cast", ""), meta.get("genre", ""),
             douban_year, douban_country,
             1 if year_ok else 0, 1 if country_ok else 0,
             1 if (year_ok and country_ok) else 0,
             mid)
        )

        try:
            sel_reviews = _select_reviews_from_soup(soup, score_present=bool(score))
            short_reviews_json = json.dumps(sel_reviews, ensure_ascii=False)
        except Exception:
            short_reviews_json = json.dumps([])
        trailers_json = json.dumps(meta.get("trailers", []) or [], ensure_ascii=False)

        conn.execute(
            """INSERT OR REPLACE INTO douban_details
               (movie_id, douban_id, title_cn, douban_score, douban_votes,
                director, cast, genre, douban_year, douban_country, douban_url,
                want_to_watch, watched, trailer_urls, short_reviews, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, subject_id, meta.get("title_cn", ""), score, votes,
             meta.get("director", ""), meta.get("cast", ""), meta.get("genre", ""),
             douban_year, douban_country,
             f"https://movie.douban.com/subject/{subject_id}/",
             meta.get("want_to_watch", 0) or 0, meta.get("watched", 0) or 0,
             trailers_json, short_reviews_json, now)
        )
        conn.commit()
        enriched += 1
        logger.info("Briefing [%d/%d] %s | %s %.1f分 (%d看过/%d想看)",
                    idx, len(targets), t["section"],
                    meta.get("title_cn", subject_id), score,
                    meta.get("watched", 0) or 0, meta.get("want_to_watch", 0) or 0)

    logger.info("Briefing enrich: %d/%d enriched", enriched, len(targets))
    conn.close()
    return enriched


# Per-review body translation is capped so DeepL Free's 500K char/month budget
# survives a few weeks of briefings. Empirically 200 chars covers the gist of
# most eiga short reviews.
_EIGA_REVIEW_TRANSLATE_CHARS = 200


def enrich_eiga_reviews_for_briefing(top_n: int = 5, delay: float = 1.0) -> int:
    """Fetch + translate the top-3 eiga.com reviews for 电影日和 candidates.

    Targets are the chain releases that the briefing's 电影日和 section would
    display: ``eiga_rating > 0``, ``douban_score`` empty, recent release.
    Already-enriched-today candidates are skipped. Review bodies are
    truncated to ``_EIGA_REVIEW_TRANSLATE_CHARS`` before translation so the
    DeepL quota doesn't blow up.
    """
    from scraper.eiga import scrape_eiga_reviews
    from scraper.translate import init_translator, translate_ja_to_zh, is_configured
    from generator.briefing import select_section_movie_ids

    cfg = _load_config()
    init_translator(cfg)

    conn = _get_db()
    _ensure_douban_table(conn)

    now = datetime.now().isoformat(timespec="seconds")
    today_str = now[:10]

    wanted_ids = select_section_movie_ids("电影日和", top_n=top_n)
    if not wanted_ids:
        logger.info("Eiga-review enrich: no 电影日和 candidates")
        conn.close()
        return 0

    title_lookup = {}
    placeholders = ",".join("?" for _ in wanted_ids)
    rows = conn.execute(
        f"SELECT movie_id, title_jp FROM movies WHERE movie_id IN ({placeholders})",
        tuple(wanted_ids),
    ).fetchall()
    title_lookup = {r["movie_id"]: r["title_jp"] for r in rows}

    if not is_configured():
        logger.warning(
            "DeepL not configured (config.yaml deepl.api_key empty) — "
            "fetching reviews but body_zh will be empty"
        )

    enriched = 0
    skipped_today = 0
    for mid in wanted_ids:
        # Skip if reviews already refreshed today (any row will do).
        last = conn.execute(
            "SELECT MAX(updated_at) FROM eiga_reviews WHERE movie_id=?", (mid,)
        ).fetchone()
        if last and last[0] and last[0][:10] == today_str:
            skipped_today += 1
            continue
        try:
            reviews = scrape_eiga_reviews(mid, n=3, delay=delay)
        except Exception as exc:
            logger.warning("Eiga reviews fetch failed for %s: %s", mid, exc)
            continue
        if not reviews:
            logger.info("Eiga reviews: %s has no reviews", title_lookup.get(mid, mid))
            continue

        # Replace prior reviews atomically per movie.
        conn.execute("DELETE FROM eiga_reviews WHERE movie_id=?", (mid,))
        translated = 0
        for pos, rev in enumerate(reviews):
            body_ja = rev.get("body_ja") or ""
            body_for_tx = body_ja[:_EIGA_REVIEW_TRANSLATE_CHARS]
            body_zh = translate_ja_to_zh(body_for_tx) if body_for_tx else None
            title_ja = rev.get("title") or ""
            title_zh = translate_ja_to_zh(title_ja) if title_ja else None
            if body_zh:
                translated += 1
            conn.execute(
                """INSERT INTO eiga_reviews
                   (movie_id, position, review_id, rating,
                    title_ja, title_zh, author, date_str,
                    body_ja, body_zh, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (mid, pos, rev.get("review_id", ""), rev.get("rating"),
                 title_ja, title_zh or "",
                 rev.get("author", ""), rev.get("date", ""),
                 body_ja, body_zh or "", now),
            )
        conn.commit()
        enriched += 1
        logger.info(
            "Eiga reviews: %s | %d reviews (%d translated)",
            title_lookup.get(mid, mid), len(reviews), translated,
        )

    if skipped_today:
        logger.info("Eiga-review enrich: skipped %d already-enriched today", skipped_today)
    logger.info("Eiga-review enrich: %d/%d movies enriched", enriched, len(wanted_ids))
    conn.close()
    return enriched

# ── MD出力 ───────────────────────────────────────────────────────────────────

def generate_step2_md(results: Optional[dict] = None) -> Path:
    """Generate step2 MD report. If results not provided, load from DB."""
    from datetime import date
    today = date.today().isoformat()
    outdir = Path("output")
    outdir.mkdir(exist_ok=True)

    if results is None:
        results = _load_results_from_db()

    lines = [
        f"# Step 2: 豆瓣マッチング結果 ({today})",
        "",
    ]

    for cat, label in [("chain", "院線映画"), ("indie", "小众映画")]:
        movies = results.get(cat, [])
        # Only count verified matches as real matches
        verified_count = sum(1 for m in movies
                            if m.get("douban_id") and m.get("verified"))

        lines += [
            f"## {label} ({verified_count}本マッチ / {len(movies)}本中)",
            "",
            "| # | Movie ID | Rank | タイトル | 原題 | 国(eiga) | 年 | 中文名 | 豆瓣分 | 評価数 | 監督 | 出演 |",
            "|---|----------|------|---------|------|----------|-----|--------|--------|--------|------|------|",
        ]
        for i, m in enumerate(movies, 1):
            mid = m.get("movie_id") or "-"
            title = m.get("title_jp", "")
            orig = m.get("title_original") or "-"
            country_e = m.get("country") or "?"
            year_e = m.get("year") or "?"
            rank = m.get("rank", "?")

            # Only show douban data for verified matches
            is_verified = m.get("douban_id") and m.get("verified")
            if is_verified:
                title_cn_raw = (m.get("title_cn") or "-").replace('|', '')
                score = m.get("douban_score", 0)
                votes = m.get("douban_votes", 0)
                director = m.get("director") or "-"
                cast = m.get("cast") or "-"
                douban_url = m.get("douban_url", "")
                title_cn = (f'<a href="{douban_url}" target="_blank">{title_cn_raw}</a>'
                            if douban_url else title_cn_raw)
                score_str = f"{score:.1f}" if score else "-"
            else:
                title_cn = ""
                score_str = ""
                votes = ""
                director = ""
                cast = ""

            # Escape pipe characters in all text fields for MD table
            _p = lambda s: str(s).replace('|', '') if s else s
            mid_cell = (f'<a href="https://eiga.com/movie/{mid}/" target="_blank">{mid}</a>'
                        if mid and mid != "-" else mid)
            lines.append(
                f"| {i} | {mid_cell} | {rank} | {_p(title)} | {_p(orig)} | {country_e} "
                f"| {year_e} | {title_cn} | {score_str} "
                f"| {votes} | {_p(director)} | {_p(cast)} |"
            )

        lines += ["", "---", ""]

    md_path = outdir / f"step2_{today}.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("MD saved: %s", md_path)
    # Also append a simple unmatched list for quick confirmation
    unmatched = []
    for cat in ("chain", "indie"):
        for m in results.get(cat, []):
            if not m.get("douban_id"):
                unmatched.append(dict(m))

    if unmatched:
        # Load skip-list entries so we can mark which unmatched movies are
        # permanently skipped vs. just temporarily un-resolvable.
        skip_map: dict = {}
        try:
            conn_sk = _get_db()
            for r in conn_sk.execute("SELECT movie_id, reason FROM douban_skip_list").fetchall():
                skip_map[r["movie_id"]] = r["reason"] or ""
            conn_sk.close()
        except Exception:
            skip_map = {}

        skip_rows = []
        retry_rows = []
        for m in unmatched:
            mid = m.get("movie_id") or "-"
            title = str(m.get("title_jp") or "-").replace('|', '')
            cat = m.get("category") or "-"
            if mid in skip_map:
                skip_rows.append((mid, title, cat, skip_map[mid].replace('|', '')))
            else:
                retry_rows.append((mid, title, cat))

        def _mid_link(mid: str) -> str:
            if not mid or mid == "-":
                return mid or "-"
            return f'<a href="https://eiga.com/movie/{mid}/" target="_blank">{mid}</a>'

        um_lines: list[str] = []
        if skip_rows:
            um_lines += ["", "---", "", "## Skip list", ""]
            um_lines.append("| # | Movie ID | タイトル | カテゴリ | 理由 |")
            um_lines.append("|---|----------|---------|----------|------|")
            for i, (mid, title, cat, reason) in enumerate(skip_rows, 1):
                um_lines.append(f"| {i} | {_mid_link(mid)} | {title} | {cat} | {reason} |")

        if retry_rows:
            um_lines += ["", "---", "", "## 未マッチ（再試行）", ""]
            um_lines.append("| # | Movie ID | タイトル | カテゴリ |")
            um_lines.append("|---|----------|---------|----------|")
            for i, (mid, title, cat) in enumerate(retry_rows, 1):
                um_lines.append(f"| {i} | {_mid_link(mid)} | {title} | {cat} |")

        if um_lines:
            with md_path.open("a", encoding="utf-8") as f:
                f.write("\n" + "\n".join(um_lines))

    return md_path


def manual_set_match(movie_id: str, douban_id: str, delay: float = 1.5) -> dict:
    """Manually set a Douban match: fetch the page, save to douban_matches + douban_details.

    Use when auto-search fails and you know the correct Douban subject ID.
    Returns a summary dict with the saved data.
    """
    from scraper.douban import (
        _fetch_movie_page, _parse_rating, _parse_meta, init_douban_session,
    )

    cfg = _load_config()
    douban_cfg = cfg.get("douban", {})
    if douban_cfg:
        init_douban_session(douban_cfg)

    conn = _get_db()
    _ensure_douban_table(conn)

    mov = conn.execute("SELECT * FROM movies WHERE movie_id=?", (movie_id,)).fetchone()
    if not mov:
        conn.close()
        raise ValueError(f"movie_id '{movie_id}' not found in movies table")

    logger.info("manual_set: fetching Douban page id=%s for %s", douban_id, mov["title_jp"])
    soup = _fetch_movie_page(douban_id, delay=delay)
    if not soup:
        conn.close()
        raise RuntimeError(f"Could not fetch Douban page for id={douban_id}")

    score, votes = _parse_rating(soup)
    meta = _parse_meta(soup)
    now = datetime.now().isoformat(timespec="seconds")

    douban_year = meta.get("year", 0)
    douban_country = meta.get("country", "")
    year_ok = _check_year_match(mov["year"] or 0, douban_year)
    country_ok = _check_country_match(mov["country"] or "", douban_country)

    conn.execute(
        """INSERT OR REPLACE INTO douban_matches
           (movie_id, douban_id, title_cn, douban_score, douban_votes,
            director, cast, genre, douban_year, douban_country, douban_url,
            year_ok, country_ok, verified, matched_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
        (movie_id, douban_id, meta.get("title_cn", ""), score, votes,
         meta.get("director", ""), meta.get("cast", ""), meta.get("genre", ""),
         douban_year, douban_country,
         f"https://movie.douban.com/subject/{douban_id}/",
         1 if year_ok else 0, 1 if country_ok else 0, now)
    )

    try:
        sel_reviews = _select_reviews_from_soup(soup, score_present=bool(score))
        short_reviews_json = json.dumps(sel_reviews, ensure_ascii=False)
    except Exception:
        short_reviews_json = json.dumps([])
    trailers_json = json.dumps(meta.get("trailers", []) or [], ensure_ascii=False)

    conn.execute(
        """INSERT OR REPLACE INTO douban_details
           (movie_id, douban_id, title_cn, douban_score, douban_votes,
            director, cast, genre, douban_year, douban_country, douban_url,
            want_to_watch, watched, trailer_urls, short_reviews, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (movie_id, douban_id, meta.get("title_cn", ""), score, votes,
         meta.get("director", ""), meta.get("cast", ""), meta.get("genre", ""),
         douban_year, douban_country,
         f"https://movie.douban.com/subject/{douban_id}/",
         meta.get("want_to_watch", 0) or 0, meta.get("watched", 0) or 0,
         trailers_json, short_reviews_json, now)
    )
    conn.commit()
    conn.close()

    return {
        "movie_id": movie_id,
        "title_jp": mov["title_jp"],
        "douban_id": douban_id,
        "title_cn": meta.get("title_cn", ""),
        "score": score,
        "votes": votes,
        "year_ok": year_ok,
        "country_ok": country_ok,
    }


def add_movie_to_skip_list(movie_id: str, reason: str = "no-douban-found") -> None:
    """Mark a movie to be skipped by Step2 (e.g., concerts without Douban entries)."""
    conn = _get_db()
    _ensure_douban_table(conn)
    from datetime import datetime
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("INSERT OR REPLACE INTO douban_skip_list (movie_id, reason, noted_at) VALUES (?,?,?)",
                 (movie_id, reason, now))
    conn.commit()
    conn.close()


def _load_results_from_db() -> dict:
    """Load results from DB for MD generation (when called standalone)."""
    conn = _get_db()
    _ensure_douban_table(conn)
    results = {"chain": [], "indie": []}

    for cat in ("chain", "indie"):
        movies = conn.execute(
            "SELECT m.*, d.douban_id, d.title_cn, d.douban_score, d.douban_votes, "
            "d.director, d.cast, d.genre, d.douban_year, d.douban_country, "
            "d.douban_url, d.year_ok, d.country_ok, d.verified "
            "FROM movies m LEFT JOIN douban_matches d ON m.movie_id = d.movie_id "
            "WHERE m.category = ? ORDER BY m.rank",
            (cat,)
        ).fetchall()
        results[cat] = [dict(r) for r in movies]

    conn.close()
    return results
