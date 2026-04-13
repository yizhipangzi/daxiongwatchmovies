"""TOHO Cinemas schedule scraper.

Fetches the weekly schedule from TOHOシネマズ for each configured Tokyo
theater and returns a list of :class:`TheaterSchedule` objects.

TOHO schedule page pattern:
  https://hlo.tohotheater.jp/net/movie/{THEATER_ID}/schedule/list.do
"""
from __future__ import annotations

import logging
import re
from datetime import date, timedelta
from typing import Optional

from .base import MovieInfo, ScreeningInfo, TheaterSchedule, clean_text, fetch_html

logger = logging.getLogger(__name__)

TOHO_BASE = "https://hlo.tohotheater.jp"
SCHEDULE_PATH = "/net/movie/{theater_id}/schedule/list.do"


def _parse_date(text: str, reference_year: int) -> Optional[date]:
    """Parse Japanese date strings like '4/13(日)' → date(2026, 4, 13)."""
    m = re.search(r"(\d{1,2})/(\d{1,2})", text)
    if not m:
        return None
    month, day = int(m.group(1)), int(m.group(2))
    today = date.today()
    year = reference_year
    # Handle year rollover: if we're in December and the schedule shows January
    if today.month == 12 and month == 1:
        year += 1
    try:
        return date(year, month, day)
    except ValueError:
        return None


def scrape_toho_theater(theater_id: str, theater_name: str,
                        base_url: str = TOHO_BASE,
                        target_dates: Optional[list[date]] = None
                        ) -> TheaterSchedule:
    """Scrape one TOHO theater and return its schedule."""
    schedule = TheaterSchedule(source="TOHO", theater_name=theater_name)
    url = base_url + SCHEDULE_PATH.format(theater_id=theater_id)
    soup = fetch_html(url, delay=1.0)
    if soup is None:
        logger.warning("Could not fetch TOHO schedule for %s", theater_name)
        return schedule

    today = date.today()
    year = today.year
    movies_seen: dict[str, MovieInfo] = {}

    # TOHO schedule page: each movie block has class "schedule-movie-detail"
    for block in soup.select(".schedule-movie-detail, .m-schedule-movie-detail"):
        # Movie title
        title_el = block.select_one(".schedule-movie-title, .m-schedule-movie-title, h3, h4")
        if title_el is None:
            continue
        title_jp = clean_text(title_el.get_text())
        if not title_jp:
            continue

        # Poster
        poster_url = ""
        img = block.select_one("img[src]")
        if img:
            src = img.get("src", "")
            poster_url = src if src.startswith("http") else base_url + src

        # Showtimes
        for row in block.select(".schedule-table tr, .m-schedule-table tr"):
            cells = row.select("td")
            if len(cells) < 2:
                continue
            show_date_text = cells[0].get_text(strip=True)
            show_date = _parse_date(show_date_text, year)
            if target_dates and show_date and show_date not in target_dates:
                continue

            for time_cell in cells[1:]:
                start_text = clean_text(time_cell.get_text())
                if not start_text or start_text in ("-", "―", "×"):
                    continue
                if title_jp not in movies_seen:
                    movies_seen[title_jp] = MovieInfo(
                        title_jp=title_jp,
                        poster_url=poster_url,
                        is_new_release=(show_date == today or
                                        (show_date and show_date >= today - timedelta(days=7))),
                    )
                movies_seen[title_jp].add_screening(ScreeningInfo(
                    theater_name=theater_name,
                    show_date=show_date,
                    start_time=start_text,
                ))

    # Fallback: even simpler selector — grab any movie title links
    if not movies_seen:
        for link in soup.select("a[href*='/net/movie/']"):
            title_jp = clean_text(link.get_text())
            if not title_jp or title_jp in movies_seen:
                continue
            movies_seen[title_jp] = MovieInfo(title_jp=title_jp)

    schedule.movies = list(movies_seen.values())
    logger.info("TOHO %s: found %d movies", theater_name, len(schedule.movies))
    return schedule


def scrape_all_toho(config: dict,
                    target_dates: Optional[list[date]] = None
                    ) -> list[TheaterSchedule]:
    """Scrape all configured TOHO theaters."""
    base_url = config.get("base_url", TOHO_BASE)
    schedules: list[TheaterSchedule] = []
    for loc in config.get("locations", []):
        s = scrape_toho_theater(
            theater_id=loc["id"],
            theater_name=loc["name"],
            base_url=base_url,
            target_dates=target_dates,
        )
        schedules.append(s)
    return schedules
