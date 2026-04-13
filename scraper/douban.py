"""Douban (豆瓣) movie data fetcher.

Uses Douban's subject suggest API and movie page scraping to enrich
MovieInfo objects with Chinese title, rating, vote count, and short reviews.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from .base import MovieInfo, HEADERS, clean_text

logger = logging.getLogger(__name__)

SUGGEST_URL = "https://movie.douban.com/j/subject_suggest"
MOVIE_BASE = "https://movie.douban.com/subject"


def _fetch_json(url: str, params: dict, delay: float = 1.5) -> Optional[list]:
    """Fetch JSON from Douban suggest API."""
    time.sleep(delay)
    try:
        resp = requests.get(url, params=params, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.warning("Douban JSON fetch failed (%s): %s", url, exc)
        return None


def _fetch_movie_page(subject_id: str, delay: float = 1.5) -> Optional[BeautifulSoup]:
    """Fetch a Douban movie subject page."""
    time.sleep(delay)
    url = f"{MOVIE_BASE}/{subject_id}/"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return BeautifulSoup(resp.text, "lxml")
    except Exception as exc:
        logger.warning("Douban page fetch failed (%s): %s", url, exc)
        return None


def search_douban(title_jp: str, year: int = 0,
                  delay: float = 1.5) -> Optional[dict]:
    """Search Douban by Japanese movie title; return the best match dict."""
    query = title_jp
    if year:
        query = f"{title_jp} {year}"

    results = _fetch_json(SUGGEST_URL, {"q": query}, delay=delay)
    if not results:
        # Try without the year
        results = _fetch_json(SUGGEST_URL, {"q": title_jp}, delay=delay)
    if not results:
        return None

    # Filter to movies (type == 'movie')
    movies = [r for r in results if r.get("type") == "movie"]
    if not movies:
        movies = results  # accept any

    # Prefer year match
    if year:
        year_matches = [m for m in movies if str(year) in m.get("year", "")]
        if year_matches:
            return year_matches[0]

    return movies[0] if movies else None


def _parse_rating(soup: BeautifulSoup) -> tuple[float, int]:
    """Parse rating score and vote count from a Douban movie page."""
    score = 0.0
    votes = 0

    rating_el = soup.select_one("strong.rating_num")
    if rating_el:
        try:
            score = float(rating_el.get_text(strip=True))
        except ValueError:
            pass

    votes_el = soup.select_one("span[property='v:votes']")
    if votes_el:
        try:
            votes = int(re.sub(r"\D", "", votes_el.get_text()))
        except ValueError:
            pass

    return score, votes


def _parse_meta(soup: BeautifulSoup) -> dict:
    """Parse director, cast, genre, year, duration from a Douban movie page."""
    info: dict = {}

    # Director
    directors = [a.get_text(strip=True) for a in soup.select("a[rel='v:directedBy']")]
    info["director"] = " / ".join(directors)

    # Cast
    cast_els = soup.select("a[rel='v:starring']")
    info["cast"] = " / ".join(a.get_text(strip=True) for a in cast_els[:5])

    # Genre
    genres = [el.get_text(strip=True) for el in soup.select("span[property='v:genre']")]
    info["genre"] = " / ".join(genres)

    # Year
    year_el = soup.select_one("span.year")
    if year_el:
        m = re.search(r"\d{4}", year_el.get_text())
        if m:
            info["year"] = int(m.group(0))

    # Duration
    dur_el = soup.select_one("span[property='v:runtime']")
    if dur_el:
        m = re.search(r"\d+", dur_el.get_text())
        if m:
            info["duration"] = int(m.group(0))

    # Chinese title
    title_el = soup.select_one("span[property='v:itemreviewed']")
    if not title_el:
        title_el = soup.select_one("h1 span")
    if title_el:
        info["title_cn"] = clean_text(title_el.get_text()).split()[0]

    return info


def _parse_short_reviews(soup: BeautifulSoup, n: int = 5) -> list[str]:
    """Extract top n short reviews from Douban movie page."""
    reviews: list[str] = []
    for el in soup.select(".comment-item .short"):
        text = clean_text(el.get_text())
        if text:
            reviews.append(text)
        if len(reviews) >= n:
            break
    return reviews


def enrich_movie_with_douban(movie: MovieInfo,
                             delay: float = 1.5,
                             review_count: int = 5) -> MovieInfo:
    """Fetch Douban data and attach it to a MovieInfo in-place."""
    match = search_douban(movie.title_jp, year=movie.year, delay=delay)
    if match is None:
        logger.debug("No Douban match for: %s", movie.title_jp)
        return movie

    subject_id = match.get("id", "")
    if not subject_id:
        return movie

    movie.douban_id = subject_id
    movie.douban_url = f"{MOVIE_BASE}/{subject_id}/"

    # Quick score from suggest API result
    if "rating" in match:
        try:
            movie.douban_score = float(match["rating"].get("value", 0) or 0)
        except (TypeError, ValueError):
            pass

    # Fetch full page for detailed info
    page_soup = _fetch_movie_page(subject_id, delay=delay)
    if page_soup is None:
        return movie

    score, votes = _parse_rating(page_soup)
    if score:
        movie.douban_score = score
    movie.douban_votes = votes

    meta = _parse_meta(page_soup)
    movie.title_cn = meta.get("title_cn", movie.title_cn)
    movie.director = meta.get("director", movie.director)
    movie.cast = meta.get("cast", movie.cast)
    movie.genre = meta.get("genre", movie.genre)
    if meta.get("year"):
        movie.year = meta["year"]
    if meta.get("duration"):
        movie.duration = meta["duration"]

    movie.douban_short_reviews = _parse_short_reviews(page_soup, n=review_count)

    logger.info(
        "Douban: %s → %s (%.1f分, %d人评价)",
        movie.title_jp, movie.title_cn or "?",
        movie.douban_score, movie.douban_votes
    )
    return movie


def enrich_all_movies(movies: list[MovieInfo],
                      config: dict) -> list[MovieInfo]:
    """Enrich all movies with Douban data, respecting rate limits."""
    delay = config.get("request_delay", 1.5)
    review_count = config.get("short_review_count", 5)
    for movie in movies:
        enrich_movie_with_douban(movie, delay=delay, review_count=review_count)
    return movies
