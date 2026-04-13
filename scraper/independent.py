"""Independent / art-house theater scraper.

Each theater has its own website structure, so we use a generic heuristic
scraper that extracts movie titles from common HTML patterns, plus a set of
known selectors for popular Tokyo independent cinemas.
"""
from __future__ import annotations

import logging
import re
from datetime import date
from typing import Optional
from urllib.parse import urljoin

from .base import MovieInfo, ScreeningInfo, TheaterSchedule, clean_text, fetch_html

logger = logging.getLogger(__name__)

# Known CSS selector profiles per theater URL keyword
KNOWN_SELECTORS: dict[str, dict[str, str]] = {
    "uplink": {
        "movie_block": "article.movie, .schedule-item, .event-item",
        "title": "h2, h3, .title",
        "time": ".time, .showtime",
    },
    "imageforum": {
        "movie_block": ".movie-info, article, .nowShowing",
        "title": "h2, h3, .movie-title",
        "time": ".time, .showtime",
    },
    "cinemavera": {
        "movie_block": ".program, .movie-item, article",
        "title": "h2, h3",
        "time": ".time",
    },
    "pole2": {
        "movie_block": "article, .movie, .program",
        "title": "h2, h3, .title",
        "time": ".time, .showtime",
    },
    "ks-cinema": {
        "movie_block": "article, .movie, table tr",
        "title": "h2, h3, .title, td",
        "time": ".time, td",
    },
    "wasedashochiku": {
        "movie_block": "article, .movie, .program-item",
        "title": "h2, h3, .title",
        "time": ".showtime, .time",
    },
}

# Generic fallback selectors
GENERIC_TITLE_SELECTORS = [
    "h1.movie-title", "h2.movie-title", "h3.movie-title",
    "h1.title", "h2.title", "h3.title",
    ".movie-title", ".film-title", ".program-title",
    "h2", "h3",
]


def _get_selectors(url: str) -> dict[str, str]:
    """Return the best known selector set for a given URL."""
    for keyword, sels in KNOWN_SELECTORS.items():
        if keyword in url:
            return sels
    return {
        "movie_block": "article, .movie, .program, .film",
        "title": "h2, h3, .title, .movie-title",
        "time": ".time, .showtime",
    }


def _extract_titles_generic(soup, base_url: str) -> list[str]:
    """Last-resort generic title extraction."""
    titles: list[str] = []
    for sel in GENERIC_TITLE_SELECTORS:
        for el in soup.select(sel):
            text = clean_text(el.get_text())
            # Filter: must contain at least one Japanese character or be > 3 chars
            if text and len(text) > 2:
                if re.search(r"[\u3040-\u30ff\u4e00-\u9fff]", text) or len(text) > 5:
                    if text not in titles:
                        titles.append(text)
        if titles:
            break
    return titles[:20]  # cap at 20 per theater


def scrape_independent_theater(theater_config: dict,
                               target_dates: Optional[list[date]] = None
                               ) -> TheaterSchedule:
    """Scrape a single independent theater."""
    name = theater_config["name"]
    base_url = theater_config["url"]
    schedule_path = theater_config.get("schedule_path", "/")
    full_url = urljoin(base_url, schedule_path)

    schedule = TheaterSchedule(source="独立影院", theater_name=name)
    soup = fetch_html(full_url, delay=1.5)
    if soup is None:
        logger.warning("Could not fetch schedule for %s", name)
        return schedule

    selectors = _get_selectors(base_url)
    movies_seen: dict[str, MovieInfo] = {}
    today = date.today()

    for block in soup.select(selectors["movie_block"]):
        title_el = block.select_one(selectors["title"])
        if title_el is None:
            continue
        title_jp = clean_text(title_el.get_text())
        if not title_jp or len(title_jp) < 2:
            continue

        poster_url = ""
        img = block.select_one("img[src]")
        if img:
            src = img.get("src", "")
            poster_url = src if src.startswith("http") else urljoin(base_url, src)

        if title_jp not in movies_seen:
            movies_seen[title_jp] = MovieInfo(
                title_jp=title_jp,
                poster_url=poster_url,
            )

        for time_el in block.select(selectors["time"]):
            start = clean_text(time_el.get_text())
            m = re.search(r"\d{1,2}:\d{2}", start)
            if m:
                movies_seen[title_jp].add_screening(ScreeningInfo(
                    theater_name=name,
                    show_date=today,
                    start_time=m.group(0),
                ))

    # Generic fallback
    if not movies_seen:
        for title_jp in _extract_titles_generic(soup, base_url):
            movies_seen[title_jp] = MovieInfo(title_jp=title_jp)

    schedule.movies = list(movies_seen.values())
    logger.info("独立影院 %s: found %d movies", name, len(schedule.movies))
    return schedule


def scrape_all_independent(config: dict,
                           target_dates: Optional[list[date]] = None
                           ) -> list[TheaterSchedule]:
    """Scrape all configured independent theaters."""
    schedules: list[TheaterSchedule] = []
    for loc in config.get("locations", []):
        s = scrape_independent_theater(loc, target_dates=target_dates)
        schedules.append(s)
    return schedules
