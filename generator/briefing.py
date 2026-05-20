"""WeChat 公众号 briefing markdown generator.

Loads movie + douban data from data/eiga.db, applies the section filters
defined in ``_SECTION_SPECS``, and renders a publication-ready Markdown
file using the templates in ``briefing_template.py``.

Public entry points:
    generate_wechat_briefing_md(top_n, output_path)
    select_briefing_movie_ids(top_n)
"""
from __future__ import annotations

import logging
import re
import sqlite3
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Template imports ────────────────────────────────────────────────────────
#
# All visible text / layout lives in ``briefing_template.py`` — edit that file
# to change wording, emoji, section labels, ordering, etc. The code below only
# loads data from SQLite and substitutes it into those templates.

from .briefing_template import (
    DATE_FORMAT,
    BRIEFING_HEADER,
    SECTION_LABELS,
    SECTION_HEADER,
    SECTION_EMPTY_PLACEHOLDER,
    MOVIE_BLOCK,
    STATS_WITH_SCORE,
    STATS_WITH_NEW_RATING,
    STATS_NO_DATA,
    STATS_WITH_EIGA_RATING,
    REVIEW_LINE,
    REVIEW_NONE,
    REVIEW_ANONYMOUS,
    REVIEW_HEADING_DOUBAN,
    REVIEW_HEADING_EIGA,
    EIGA_REVIEW_LINE,
    EIGA_REVIEW_LINE_JA_ONLY,
    THEATER_SEP,
    NO_THEATERS,
    EMPTY_FIELD,
)


def _briefing_format_movie(rank: int, m: dict, eiga_url: Optional[str],
                           review_source: str = "douban") -> str:
    """Render one movie block.

    ``review_source`` is ``"douban"`` (default) or ``"eiga"``. When ``"eiga"``,
    the stats line uses the eiga.com rating and reviews come from
    ``m["eiga_reviews"]`` (already translated by step2 enrichment).
    """
    cn = (m.get("title_cn") or m.get("title_jp") or "").strip()
    title_jp = (m.get("title_jp") or "").strip()
    orig = (m.get("title_original") or "").strip()
    douban_url = m.get("douban_url") or ""

    name_link = f"[{cn}]({douban_url})" if douban_url else cn
    eiga_label = orig if orig else title_jp
    if eiga_url and eiga_label:
        orig_link = f"[{eiga_label}]({eiga_url})"
    else:
        orig_link = eiga_label or EMPTY_FIELD

    # Stats line — picked by review_source and what data is available.
    watched = m.get("watched") or 0
    want = m.get("want_to_watch") or 0
    if review_source == "eiga":
        eiga_rating = m.get("eiga_rating") or 0
        eiga_rating_count = m.get("eiga_rating_count") or 0
        if eiga_rating:
            stats_line = STATS_WITH_EIGA_RATING.format(
                rating=eiga_rating, count=eiga_rating_count,
            )
        else:
            stats_line = STATS_NO_DATA.format(watched=watched, want=want)
    else:
        score = m.get("douban_score") or 0
        if score:
            stats_line = STATS_WITH_SCORE.format(score=score, watched=watched, want=want)
        else:
            nm_rating = m.get("new_movie_rating")
            nm_count = m.get("new_movie_rating_count") or 0
            if nm_rating:
                stats_line = STATS_WITH_NEW_RATING.format(
                    rating=nm_rating, count=nm_count, watched=watched, want=want,
                )
            else:
                stats_line = STATS_NO_DATA.format(watched=watched, want=want)

    director = (m.get("director") or EMPTY_FIELD).strip() or EMPTY_FIELD
    cast = (m.get("cast") or EMPTY_FIELD).strip() or EMPTY_FIELD
    genre = (m.get("genre") or EMPTY_FIELD).strip() or EMPTY_FIELD

    # Theaters: clickable list joined by THEATER_SEP
    parts: list[str] = []
    for t in m.get("theaters") or []:
        if not isinstance(t, dict):
            continue
        name = t.get("name") or ""
        url = t.get("url") or ""
        if not name:
            continue
        parts.append(f"[{name}]({url})" if url else name)
    theaters_line = THEATER_SEP.join(parts) if parts else NO_THEATERS

    # Reviews
    review_lines: list[str] = []
    if review_source == "eiga":
        review_heading = REVIEW_HEADING_EIGA
        eiga_revs = m.get("eiga_reviews") or []
        for r in eiga_revs[:3]:
            body_zh = (r.get("body_zh") or "").strip()
            body_ja = (r.get("body_ja") or "").strip()
            author = (r.get("author") or "").strip() or REVIEW_ANONYMOUS
            rating = r.get("rating")
            rating_s = f"{rating:.1f}" if isinstance(rating, (int, float)) else "?"
            if body_zh:
                review_lines.append(EIGA_REVIEW_LINE.format(
                    rating=rating_s, text_zh=body_zh, author=author,
                ))
            elif body_ja:
                review_lines.append(EIGA_REVIEW_LINE_JA_ONLY.format(
                    rating=rating_s, text_ja=body_ja, author=author,
                ))
        if not review_lines:
            review_lines.append(REVIEW_NONE)
    else:
        review_heading = REVIEW_HEADING_DOUBAN
        reviews_data = m.get("short_reviews") or []
        watched_reviews = [
            r for r in reviews_data
            if isinstance(r, dict) and r.get("status") == "watched"
        ]
        chosen = watched_reviews[:3] if watched_reviews else reviews_data[:3]
        if not chosen:
            review_lines.append(REVIEW_NONE)
        else:
            for r in chosen:
                if isinstance(r, dict):
                    text = (r.get("text") or "").strip()
                    author = (r.get("author") or "").strip() or REVIEW_ANONYMOUS
                    if not text:
                        continue
                    review_lines.append(REVIEW_LINE.format(text=text, author=author))
                else:
                    review_lines.append(REVIEW_LINE.format(text=r, author=REVIEW_ANONYMOUS))
    reviews_block = "\n".join(review_lines)

    return MOVIE_BLOCK.format(
        rank=rank,
        name_link=name_link,
        orig_link=orig_link,
        stats_line=stats_line,
        director=director,
        cast=cast,
        genre=genre,
        review_heading=review_heading,
        theaters=theaters_line,
        reviews=reviews_block,
    )


def _load_theaters_for_movie(conn, movie_id: str) -> list[dict]:
    """Return deduplicated [{'name', 'url'}, ...] for a movie via screenings join."""
    try:
        rows = conn.execute(
            """SELECT DISTINCT t.name AS name, t.url AS url
                 FROM screenings s
                 JOIN theaters t ON s.theater_id = t.theater_id
                WHERE s.movie_id = ?
                ORDER BY t.name""",
            (movie_id,)
        ).fetchall()
    except Exception:
        return []
    out: list[dict] = []
    seen: set = set()
    for r in rows:
        name = r["name"] if r["name"] is not None else ""
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({"name": name, "url": r["url"] or ""})
    return out


# ── Section filters (Python predicates) ─────────────────────────────────────
#
# Edit the predicate / sort_key / hide_when_empty here to tweak briefing rules.
# The section ORDER and visible LABELS come from SECTION_LABELS in
# briefing_template.py — make sure keys stay in sync.

_RELEASE_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")


def _parse_jp_release_date(s: Optional[str]) -> Optional[date]:
    """Parse eiga.com 劇場公開日 (e.g. '2026年4月10日') into a date object."""
    if not s:
        return None
    m = _RELEASE_DATE_RE.search(s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _is_recent_release(m: dict, today: date, days: int = 365) -> bool:
    """Return True if eiga.com 劇場公開日 is within `days` days of today.
    Future releases (negative delta) also count as recent."""
    rd = m.get("_release_date_obj")
    if rd is None:
        return False
    return (today - rd).days < days


def _year_diff(m: dict, today: date) -> int:
    """Years between today and the Douban-recorded year (fall back to eiga year)."""
    y = m.get("douban_year") or m.get("year") or 0
    if not y:
        return 0
    return today.year - int(y)


# Each spec: predicate(movie, today) -> bool, sort_key(movie) -> sortable,
#            hide_when_empty (bool).
# Keys must match SECTION_LABELS in briefing_template.py.
_SECTION_SPECS: dict[str, dict] = {
    # 院线热映: chain + recent release + douban_score>0 + NOT classic.
    # (year filter prevents recent re-releases of old films from doubling up
    # with 院线经典.)
    "院线热映": {
        "predicate": lambda m, today: (
            m.get("category") == "chain"
            and (m.get("douban_score") or 0) > 0
            and _is_recent_release(m, today, days=365)
            and _year_diff(m, today) <= 3
        ),
        "sort_key": lambda m: -(m.get("douban_score") or 0),
        "hide_when_empty": False,
    },
    # 院线经典: chain + classic (today_year - douban_year > 3) + score>0.
    "院线经典": {
        "predicate": lambda m, today: (
            m.get("category") == "chain"
            and (m.get("douban_score") or 0) > 0
            and _year_diff(m, today) > 3
        ),
        "sort_key": lambda m: -(m.get("douban_score") or 0),
        "hide_when_empty": False,
    },
    # 小众佳片: indie + score>0.
    "小众佳片": {
        "predicate": lambda m, today: (
            m.get("category") == "indie"
            and (m.get("douban_score") or 0) > 0
        ),
        "sort_key": lambda m: -(m.get("douban_score") or 0),
        "hide_when_empty": False,
    },
    # 电影日和: chain + recent release + no Douban score + has eiga rating,
    # sorted by eiga rating desc. Replaces the old 院线新片 (which used
    # Douban's 新片推荐度) so Japanese-audience signal is used when Douban
    # hasn't yet aggregated a score.
    "电影日和": {
        "predicate": lambda m, today: (
            m.get("category") == "chain"
            and (m.get("douban_score") or 0) == 0
            and (m.get("eiga_rating") or 0) > 0
            and _is_recent_release(m, today, days=365)
            and _year_diff(m, today) <= 3
        ),
        "sort_key": lambda m: -(m.get("eiga_rating") or 0),
        "hide_when_empty": True,
    },
}


def _load_briefing_candidates(conn, top_n: int = 5) -> list[dict]:
    """Load all chain+indie movies with the fields needed for section filtering."""
    import json as _json

    # LEFT JOIN douban_matches so 电影日和 candidates without a Douban entry
    # still surface. Their predicate (douban_score == 0 + has eiga_rating)
    # gates them in; everything else is filtered by per-section predicates.
    rows = conn.execute(
        "SELECT mv.movie_id, mv.title_jp, mv.title_original, mv.eiga_url, "
        "       mv.year, mv.category, mv.release_date, "
        "       mv.eiga_rating, mv.eiga_rating_count, "
        "       m.douban_id, m.douban_url, m.title_cn AS m_title_cn, "
        "       m.douban_score AS m_score, m.douban_year, "
        "       m.new_movie_rating, m.new_movie_rating_count, "
        "       d.title_cn AS d_title_cn, d.douban_score AS d_score, "
        "       d.douban_votes, d.director, d.cast, d.genre, "
        "       d.want_to_watch, d.watched, d.short_reviews "
        "  FROM movies mv "
        "  LEFT JOIN douban_matches m ON m.movie_id = mv.movie_id "
        "  LEFT JOIN douban_details d ON d.movie_id = mv.movie_id "
        " WHERE mv.category IN ('chain', 'indie')"
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        # Prefer detail values when present
        d["title_cn"] = d.get("d_title_cn") or d.get("m_title_cn") or ""
        d["douban_score"] = d.get("d_score") if d.get("d_score") else d.get("m_score") or 0
        try:
            d["short_reviews"] = _json.loads(d["short_reviews"]) if d["short_reviews"] else []
        except Exception:
            d["short_reviews"] = []
        d["_release_date_obj"] = _parse_jp_release_date(d.get("release_date"))
        out.append(d)
    return out


def _load_eiga_reviews(conn, movie_id: str) -> list[dict]:
    """Pull cached eiga.com reviews (with translations) for one movie."""
    try:
        rows = conn.execute(
            """SELECT position, review_id, rating,
                       title_ja, title_zh, author, date_str,
                       body_ja, body_zh
                  FROM eiga_reviews
                 WHERE movie_id = ?
                 ORDER BY position""",
            (movie_id,),
        ).fetchall()
    except Exception:
        return []
    return [dict(r) for r in rows]


def _select_briefing_sections(all_movies: list[dict], today: date, top_n: int,
                              extra_n: int = 10) -> list[tuple[str, list[dict], list[dict]]]:
    """Apply each section predicate to ``all_movies`` and return triples of
    ``(key, primary_movies, extras)`` in SECTION_LABELS order.

    - ``primary_movies`` are the top ``top_n`` by sort_key (used in the text).
    - ``extras`` are the next ``extra_n`` candidates (used as poster fallback
      when one of the primary movies has no fetchable eiga.com photo page).

    Sections marked ``hide_when_empty`` are dropped when ``primary_movies``
    is empty.
    """
    result: list[tuple[str, list[dict], list[dict]]] = []
    for section_key in SECTION_LABELS.keys():
        spec = _SECTION_SPECS.get(section_key)
        if spec is None:
            continue
        matched = [m for m in all_movies if spec["predicate"](m, today)]
        matched.sort(key=spec["sort_key"])
        primary = matched[:top_n]
        extras = matched[top_n:top_n + extra_n]
        if not primary and spec["hide_when_empty"]:
            continue
        result.append((section_key, primary, extras))
    return result


def generate_wechat_briefing_md(top_n: int = 5,
                                output_path: Optional[Path] = None) -> Path:
    """Generate the WeChat-ready briefing MD.

    Sections are defined by ``SECTION_LABELS`` in ``briefing_template.py`` and
    their data filters by ``_SECTION_SPECS`` above. Per-movie data is read from
    ``douban_details`` + ``screenings``/``theaters``. Run ``enrich_for_briefing``
    first to populate the source tables.
    """
    db = Path("data") / "eiga.db"
    today = date.today()
    today_s = today.isoformat()

    outdir = Path("output")
    outdir.mkdir(exist_ok=True)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    all_movies = _load_briefing_candidates(conn, top_n=top_n)
    # Theater lookups need the same conn — do them lazily per displayed movie.
    sections = _select_briefing_sections(all_movies, today, top_n)

    # Poster collage support is optional — silently disable if Pillow / network
    # is unavailable so the briefing can still be generated text-only.
    try:
        from .poster_collage import make_section_collage, cleanup_poster_cache
    except Exception as exc:
        logger.warning("Poster collage disabled: %s", exc)
        make_section_collage = None  # type: ignore[assignment]
        cleanup_poster_cache = None  # type: ignore[assignment]

    # Initialise the Douban session up-front so the poster-collage fallback
    # (via #mainpic) can reuse it without paying the cold-start cost mid-run.
    try:
        from scraper.douban import init_douban_session
        import yaml as _yaml
        _cfg_path = Path("config.yaml")
        if not _cfg_path.exists():
            _cfg_path = Path("config.yaml.example")
        if _cfg_path.exists():
            with _cfg_path.open(encoding="utf-8") as _f:
                _cfg = _yaml.safe_load(_f) or {}
            init_douban_session(_cfg.get("douban", {}))
    except Exception as exc:
        logger.debug("Douban session init skipped: %s", exc)

    parts: list[str] = [BRIEFING_HEADER.format(date_str=today.strftime(DATE_FORMAT))]

    for section_key, movies, extras in sections:
        heading, subtitle = SECTION_LABELS[section_key]
        parts.append(SECTION_HEADER.format(heading=heading, subtitle=subtitle))

        # Section poster collage. Pass (eiga_id, douban_id) pairs ordered by
        # priority (primary top-N first, then extras as fallback for missing
        # posters). For each pair, eiga.com is tried first and Douban's
        # ``#mainpic`` is used as fallback when eiga fails (e.g. 410 Gone).
        if make_section_collage is not None and movies:
            try:
                collage_path = make_section_collage(
                    section_key=section_key,
                    movie_specs=(
                        [(m["movie_id"], m.get("douban_id")) for m in movies]
                        + [(m["movie_id"], m.get("douban_id")) for m in extras]
                    ),
                    date_str=today_s,
                )
            except Exception as exc:
                logger.warning("Collage failed for %s: %s", section_key, exc)
                collage_path = None
            if collage_path is not None:
                try:
                    rel = collage_path.relative_to(outdir).as_posix()
                except ValueError:
                    rel = collage_path.as_posix()
                parts.append(f"![{heading}]({rel})\n")

        if not movies:
            parts.append(SECTION_EMPTY_PLACEHOLDER + "\n")
            continue
        review_source = "eiga" if section_key == "电影日和" else "douban"
        for idx, m in enumerate(movies, 1):
            m["theaters"] = _load_theaters_for_movie(conn, m["movie_id"])
            eiga_url = m.get("eiga_url") or (
                f"https://eiga.com/movie/{m['movie_id']}/" if m.get("movie_id") else None
            )
            if review_source == "eiga":
                m["eiga_reviews"] = _load_eiga_reviews(conn, m["movie_id"])
            parts.append(
                _briefing_format_movie(idx, m, eiga_url, review_source=review_source) + "\n"
            )

    conn.close()

    # All collages assembled — the per-movie poster cache is no longer needed.
    if cleanup_poster_cache is not None:
        try:
            cleanup_poster_cache()
        except Exception as exc:
            logger.debug("Poster cache cleanup failed: %s", exc)

    outp = output_path or outdir / f"briefing_{today_s}.md"
    outp.write_text("\n".join(parts), encoding="utf-8")
    logger.info("WeChat briefing saved: %s", outp)
    return outp


def select_briefing_movie_ids(top_n: int = 5) -> set[str]:
    """Return the set of movie_ids that would be displayed in the briefing.

    Used by ``enrich_for_briefing`` to figure out which movies need their
    detail page fetched (director/cast/want/watched/reviews). Only the
    primary top-N per section is returned — fallback "extras" for the
    poster collage don't need full enrichment.
    """
    db = Path("data") / "eiga.db"
    today = date.today()
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        all_movies = _load_briefing_candidates(conn, top_n=top_n)
    finally:
        conn.close()
    selected: set[str] = set()
    for _key, movies, _extras in _select_briefing_sections(all_movies, today, top_n):
        for m in movies:
            selected.add(m["movie_id"])
    return selected


def select_section_movie_ids(section_key: str, top_n: int = 5) -> list[str]:
    """Return movie_ids that the named section would display, in display order.

    Used by step2's eiga-review enrichment so it only translates reviews for
    movies the 电影日和 section will actually render.
    """
    spec = _SECTION_SPECS.get(section_key)
    if spec is None:
        return []
    db = Path("data") / "eiga.db"
    today = date.today()
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    try:
        all_movies = _load_briefing_candidates(conn, top_n=top_n)
    finally:
        conn.close()
    matched = [m for m in all_movies if spec["predicate"](m, today)]
    matched.sort(key=spec["sort_key"])
    return [m["movie_id"] for m in matched[:top_n]]
