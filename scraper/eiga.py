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
