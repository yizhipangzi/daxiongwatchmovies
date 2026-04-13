"""United Cinemas schedule scraper.

Schedule page pattern (by theater):
  https://www.unitedcinemas.jp/{theater_id}/daily.php
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Optional

from .base import MovieInfo, ScreeningInfo, TheaterSchedule, clean_text, fetch_html

logger = logging.getLogger(__name__)

UNITED_BASE = "https://www.unitedcinemas.jp"
SCHEDULE_PATH = "/{theater_id}/daily.php"


def _parse_time(text: str) -> str:
    """Extract HH:MM from text."""
    m = re.search(r"\d{1,2}:\d{2}", text)
    return m.group(0) if m else text.strip()


def scrape_united_theater(theater_id: str, theater_name: str,
                          base_url: str = UNITED_BASE,
                          target_dates: Optional[list[date]] = None
                          ) -> TheaterSchedule:
    """Scrape one United Cinemas theater."""
    schedule = TheaterSchedule(source="United", theater_name=theater_name)
    url = base_url + SCHEDULE_PATH.format(theater_id=theater_id)
    soup = fetch_html(url, delay=1.0)
    if soup is None:
        logger.warning("Could not fetch United schedule for %s", theater_name)
        return schedule

    today = date.today()
    movies_seen: dict[str, MovieInfo] = {}

    # United Cinemas: each movie block with class "programBox" or similar
    for block in soup.select(".programBox, .movie-block, article.movie"):
        title_el = block.select_one("h2, h3, .movieTitle, .program-title")
        if title_el is None:
            continue
        title_jp = clean_text(title_el.get_text())
        if not title_jp:
            continue

        poster_url = ""
        img = block.select_one("img[src]")
        if img:
            src = img.get("src", "")
            poster_url = src if src.startswith("http") else base_url + src

        # Showtimes
        for time_el in block.select(".showtime, .time, time, .schedule-time"):
            start = _parse_time(time_el.get_text())
            if not start:
                continue
            if title_jp not in movies_seen:
                movies_seen[title_jp] = MovieInfo(
                    title_jp=title_jp,
                    poster_url=poster_url,
                    is_new_release=True,
                )
            movies_seen[title_jp].add_screening(ScreeningInfo(
                theater_name=theater_name,
                show_date=today,
                start_time=start,
            ))

    # Fallback: grab movie title links
    if not movies_seen:
        for link in soup.select("a[href*='/film/'], a[href*='/movie/']"):
            title_jp = clean_text(link.get_text())
            if not title_jp or len(title_jp) < 2:
                continue
            if title_jp not in movies_seen:
                movies_seen[title_jp] = MovieInfo(title_jp=title_jp)

    schedule.movies = list(movies_seen.values())
    logger.info("United %s: found %d movies", theater_name, len(schedule.movies))
    return schedule


def scrape_all_united(config: dict,
                      target_dates: Optional[list[date]] = None
                      ) -> list[TheaterSchedule]:
    """Scrape all configured United Cinemas theaters."""
    base_url = config.get("base_url", UNITED_BASE)
    schedules: list[TheaterSchedule] = []
    for loc in config.get("locations", []):
        s = scrape_united_theater(
            theater_id=loc["id"],
            theater_name=loc["name"],
            base_url=base_url,
            target_dates=target_dates,
        )
        schedules.append(s)
    return schedules
