"""Multi-source original / English title resolver.

Given a Japanese movie title, query several sources and cross-validate
to produce a reliable original-language title for Douban search.

Sources (in order of cost):
  1. TOHO API ``ename``   — free, already in MovieInfo._hint_ename
  2. TOHO detail page     — 1 request, ``h1 > span.en``
  3. eiga.com             — 2 requests, 「原題または英題」(most authoritative)

Cross-validation rules:
  - eiga.com says "原題" exists  →  the movie IS foreign.
  - If eiga title matches TOHO ename (case-insensitive) → high confidence.
  - If only TOHO ename exists and it's just romanised Japanese → skip it.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Optional

import requests
from bs4 import BeautifulSoup

from .base import MovieInfo

logger = logging.getLogger(__name__)

_HEADERS_TOHO = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://hlo.tohotheater.jp/",
}

# TOHO ename suffixes to strip (format variants, not part of the title)
_TOHO_SUFFIX_RE = re.compile(
    r"\s*/\s*(?:SUB|DUB|IMAXLASER|IMAX|MX4D|4DX|4K|DOLBY|ATMOS|"
    r"SCREENX|2D|3D|D-BOX|TCX|BESTIA|字幕版?|吹替版?)$",
    re.IGNORECASE,
)


def _clean_toho_ename(ename: str) -> str:
    """Strip format/version suffixes from a TOHO ename."""
    cleaned = _TOHO_SUFFIX_RE.sub("", ename).strip()
    # Repeat in case of chained suffixes like "/ IMAX / SUB"
    cleaned = _TOHO_SUFFIX_RE.sub("", cleaned).strip()
    return cleaned


def _is_valid_title(text: Optional[str]) -> bool:
    """Validate that a candidate title string looks like an actual movie title."""
    if not text:
        return False
    s = str(text).strip()
    if not s:
        return False
    if "\n" in s or "\r" in s or "\t" in s:
        return False
    if len(s) > 120:
        return False
    sentence_enders = "。.?!！？"
    if sum(s.count(ch) for ch in sentence_enders) > 1:
        return False
    if "http" in s or "www." in s:
        return False
    if re.search(r"[\W]{6,}", s):
        return False
    return True


# ── Source 2: TOHO detail page ───────────────────────────────────────────────

_TOHO_DETAIL_URL = (
    "https://hlo.tohotheater.jp/net/movie/TNPI3060J01.do?sakuhin_cd={mcode}"
)


def _fetch_toho_detail_en(mcode: str, delay: float = 1.0) -> Optional[str]:
    """Fetch the English title from a TOHO movie detail page."""
    if not mcode:
        return None
    url = _TOHO_DETAIL_URL.format(mcode=mcode)
    time.sleep(delay)
    try:
        resp = requests.get(url, headers=_HEADERS_TOHO, timeout=15)
        resp.raise_for_status()
    except Exception:
        return None
    soup = BeautifulSoup(resp.text, "lxml")
    h1 = soup.select_one("h1.info-title")
    if h1:
        en_span = h1.select_one("span.en")
        if en_span:
            return en_span.get_text(strip=True)
    return None


# ── Source 3: eiga.com (imported) ────────────────────────────────────────────

def _fetch_eiga_title(title_jp: str, delay: float = 1.0) -> Optional[str]:
    from .eiga import lookup_original_title
    return lookup_original_title(title_jp, delay=delay)


# ── Resolver ─────────────────────────────────────────────────────────────────

@dataclass
class _Candidate:
    source: str
    title: str
    is_authoritative: bool = False   # eiga.com "原題" is authoritative


def resolve_original_title(movie: MovieInfo,
                           delay: float = 1.0) -> Optional[str]:
    """Try multiple sources to find the original/English title.

    Returns the best candidate, or ``None`` if the movie appears to be
    originally Japanese (no foreign title found).
    """
    title_jp = movie.title_jp
    candidates: list[_Candidate] = []

    # ── Source 1: TOHO API ename (zero cost) ──
    if movie._hint_ename:
        clean = _clean_toho_ename(movie._hint_ename)
        if clean and _is_valid_title(clean):
            candidates.append(_Candidate("toho_api", clean))
            logger.debug("resolve: TOHO API ename -> %r", clean)

    # ── Source 2: eiga.com (most authoritative, tells us foreign vs JP) ──
    eiga_title = _fetch_eiga_title(title_jp, delay=delay)
    if eiga_title and _is_valid_title(eiga_title):
        candidates.append(_Candidate("eiga", eiga_title, is_authoritative=True))
        logger.debug("resolve: eiga.com -> %r", eiga_title)

    # ── Source 3: TOHO detail page (only if we have mcode and need more data) ──
    if movie._hint_mcode and not any(c.is_authoritative for c in candidates):
        toho_page_en = _fetch_toho_detail_en(movie._hint_mcode, delay=delay)
        if toho_page_en:
            clean = _clean_toho_ename(toho_page_en)
            if clean and _is_valid_title(clean):
                candidates.append(_Candidate("toho_page", clean))
                logger.debug("resolve: TOHO detail page -> %r", clean)

    if not candidates:
        logger.debug("resolve: %s -> no foreign title (Japanese movie)", title_jp)
        return None

    # ── Cross-validation: pick the best ──
    # If we have an authoritative source, prefer it
    authoritative = [c for c in candidates if c.is_authoritative]
    if authoritative:
        best = authoritative[0].title
        # Log if TOHO agrees
        toho_candidates = [c for c in candidates if c.source.startswith("toho")]
        if toho_candidates:
            toho_title = toho_candidates[0].title
            if toho_title.upper() == best.upper():
                logger.info(
                    "resolve: %s -> %r (eiga + TOHO agree)", title_jp, best
                )
            else:
                logger.info(
                    "resolve: %s -> %r (eiga authoritative; TOHO had %r)",
                    title_jp, best, toho_title,
                )
        else:
            logger.info("resolve: %s -> %r (eiga only)", title_jp, best)
        return best

    # No authoritative source — use TOHO but lower confidence
    best = candidates[0].title
    logger.info(
        "resolve: %s -> %r (TOHO only, no eiga confirmation)", title_jp, best
    )
    return best
