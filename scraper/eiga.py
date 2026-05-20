"""eiga.com movie lookup — resolve Japanese titles to original/English titles.

Workflow:
  1. Search ``eiga.com/search/<日文片名>/``
  2. Take the first ``/movie/<id>/`` result
  3. Fetch the movie page and look for 「原題または英題：XXX」
  4. If found, return the original title (used to search Douban)
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

from .base import clean_text

logger = logging.getLogger(__name__)

EIGA_SEARCH = "https://eiga.com/search/{query}/"
EIGA_MOVIE = "https://eiga.com/movie/{movie_id}/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.8",
}

# Regex: capture original title from "原題または英題：XXX".
# Stop before content sections (あらすじ, スタッフ) or end of line.
# On old-film pages eiga.com sometimes puts the synopsis in the same block element,
# so we need both the keyword lookahead and a post-match truncation guard.
_ORIG_TITLE_RE = re.compile(
    r"原題[^：:\n]*[：:]\s*(.+?)(?=\s*(?:あらすじ|スタッフ|配給|配信)|\n|$)",
    re.MULTILINE,
)


def _fetch(url: str, delay: float = 1.0) -> Optional[BeautifulSoup]:
    time.sleep(delay)
    try:
        resp = requests.get(url, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return BeautifulSoup(resp.text, "lxml")
    except Exception as exc:
        logger.debug("eiga.com fetch failed (%s): %s", url, exc)
        return None


def _search_eiga(title_jp: str, delay: float = 1.0) -> Optional[str]:
    """Search eiga.com and return the first ``/movie/<id>/`` path, or None."""
    url = EIGA_SEARCH.format(query=quote(title_jp, safe=""))
    soup = _fetch(url, delay=delay)
    if soup is None:
        return None
    for a in soup.select("a[href]"):
        href = a.get("href", "")
        m = re.match(r"/movie/(\d+)/", href)
        if m:
            return m.group(1)
    return None


def _get_original_title(movie_id: str, delay: float = 1.0) -> Optional[str]:
    """Fetch eiga.com movie page and extract the original/English title."""
    url = EIGA_MOVIE.format(movie_id=movie_id)
    soup = _fetch(url, delay=delay)
    if soup is None:
        return None
    # separator="\n" ensures block elements are separated by newlines,
    # so the regex $-stop keeps the match within a single line.
    text = soup.get_text(separator="\n")
    m = _ORIG_TITLE_RE.search(text)
    if not m:
        return None
    raw = m.group(1).strip()
    # Safety net: truncate at 3+ consecutive spaces (same-line synopsis separation)
    raw = re.split(r'\s{3,}', raw)[0].strip()
    return raw if raw else None


def lookup_original_title(title_jp: str,
                          delay: float = 1.0) -> Optional[str]:
    """Given a Japanese movie title, return its original/English title via
    eiga.com, or ``None`` if the movie is originally Japanese (no 原題).
    """
    movie_id = _search_eiga(title_jp, delay=delay)
    if movie_id is None:
        logger.debug("eiga.com: no result for %r", title_jp)
        return None
    orig = _get_original_title(movie_id, delay=delay)
    if orig:
        logger.info("eiga.com: %s -> %r", title_jp, orig)
    else:
        logger.debug("eiga.com: %s has no original title (Japanese movie)", title_jp)
    return orig


EIGA_ALL_RATING = "https://eiga.com/movie/{movie_id}/review/all-rating/"


def scrape_eiga_reviews(movie_id: str, n: int = 3,
                        delay: float = 1.0) -> list[dict]:
    """Fetch the top ``n`` reviews from eiga.com's all-rating page.

    Returns a list of dicts: ``{review_id, rating, title, author, date, body_ja}``.
    Order matches the page (eiga's default sort = newest-first / featured first).
    """
    url = EIGA_ALL_RATING.format(movie_id=movie_id)
    soup = _fetch(url, delay=delay)
    if soup is None:
        return []

    results: list[dict] = []
    for rv in soup.select(".user-review")[:n]:
        rating_el = rv.select_one("h2.review-title .rating-star, .rating-star")
        rating: Optional[float] = None
        if rating_el:
            try:
                rating = float(rating_el.get_text(strip=True))
            except ValueError:
                rating = None
        title_a = rv.select_one("h2.review-title a")
        title_txt = title_a.get_text(strip=True) if title_a else ""
        review_id = ""
        if title_a and title_a.get("href"):
            m = re.search(r"/review/(\d+)/?", title_a["href"])
            if m:
                review_id = m.group(1)
        author_el = rv.select_one(".user-name")
        author = author_el.get_text(strip=True) if author_el else ""
        time_el = rv.select_one(".time")
        date_str = time_el.get_text(strip=True) if time_el else ""
        # Body lives in .txt-block p (typically p.short). Use separator='\n' so
        # <br/> tags in the source become line breaks in the output.
        body_el = rv.select_one(".txt-block p") or rv.select_one(".txt-block")
        body_ja = ""
        if body_el:
            body_ja = clean_text(body_el.get_text(separator="\n"))
        if not body_ja:
            # Last-resort fallback: grab any non-trivial text in the inner div
            inner = rv.select_one(".user-review-inner")
            if inner:
                body_ja = clean_text(inner.get_text(separator="\n"))[:2000]

        results.append({
            "review_id": review_id,
            "rating": rating,
            "title": title_txt,
            "author": author,
            "date": date_str,
            "body_ja": body_ja,
        })
    return results
