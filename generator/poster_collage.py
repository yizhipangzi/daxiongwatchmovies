"""Poster collage generator for the WeChat briefing.

For each briefing section, fetches up to 5 movie posters from eiga.com and
arranges them in a simple horizontal row — no rotation, no overlap — at a
modest size.

Public API:
    fetch_eiga_poster(movie_id, ...) -> Optional[Path]
    make_section_collage(section_key, movie_ids, ...) -> Optional[Path]
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
from PIL import Image

logger = logging.getLogger(__name__)


EIGA_PHOTO_URL = "https://eiga.com/movie/{movie_id}/photo/"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,en;q=0.8",
}

POSTER_CACHE_DIR = Path("output") / "posters"
COLLAGE_DIR = Path("output") / "collages"

# Map Chinese section keys → ASCII filename slugs so collage paths stay
# URL-safe (no %-encoded CJK in the static file URL).
_SECTION_SLUGS: dict[str, str] = {
    "院线热映": "chain_hot",
    "院线经典": "chain_classic",
    "电影日和": "chain_eiga",
    "小众佳片": "indie",
}


# ── Poster fetching ─────────────────────────────────────────────────────────

def fetch_eiga_poster(movie_id: str,
                      delay: float = 0.5,
                      cache_dir: Path = POSTER_CACHE_DIR) -> Optional[Path]:
    """Fetch the main poster image for an eiga.com movie, cached locally.

    Returns the local path on success, ``None`` on failure (no movie ID,
    network error, no poster on the page).
    """
    if not movie_id:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{movie_id}.jpg"
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path

    page_url = EIGA_PHOTO_URL.format(movie_id=movie_id)
    try:
        time.sleep(delay)
        resp = requests.get(page_url, headers=_HEADERS, timeout=15)
        # 410 Gone means eiga.com retired this movie's photo page (common for
        # very old films). Surface this as a warning rather than silent skip.
        if resp.status_code == 410:
            logger.warning("poster: movie %s photo page is GONE (410)", movie_id)
            return None
        resp.raise_for_status()
    except Exception as exc:
        logger.warning("poster: failed to fetch photo page for %s: %s", movie_id, exc)
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    el = soup.select_one(".movie-photo img")
    if not el:
        logger.warning("poster: no .movie-photo img on movie %s", movie_id)
        return None
    img_url = el.get("src") or el.get("data-src")
    if not img_url:
        logger.warning("poster: .movie-photo img has no src on movie %s", movie_id)
        return None

    try:
        time.sleep(delay)
        ir = requests.get(img_url, headers=_HEADERS, timeout=20)
        ir.raise_for_status()
        cache_path.write_bytes(ir.content)
        return cache_path
    except Exception as exc:
        logger.warning("poster: failed to download poster for %s (%s): %s",
                       movie_id, img_url, exc)
        return None


def _looks_like_bot_check(soup) -> bool:
    """Heuristic: True when the page is Douban's JS proof-of-work challenge
    rather than a real subject page. Such pages have no #mainpic and no
    og:image, plus a tiny body and the form#sec used for the JS challenge."""
    if soup is None:
        return True
    if soup.select_one("#mainpic img"):
        return False
    if soup.find("meta", property="og:image"):
        return False
    return bool(soup.select_one("form#sec"))


def _fetch_douban_page_pw(douban_id: str) -> Optional[BeautifulSoup]:
    """Open the Douban subject page in Playwright (which executes the JS
    proof-of-work challenge served by HTTP-only requests) and return the
    parsed HTML. Returns None if the persistent Playwright context isn't
    available or the real page never renders.

    Intentionally does NOT call _handle_bot_check — that helper waits up
    to 3 minutes for human CAPTCHA solving, which is wrong for batch
    poster-fetching. The JS PoW page auto-submits its form in ~500ms and
    Playwright follows the redirect to the real subject page; we just
    wait for #mainpic img to appear.
    """
    try:
        from scraper.douban import _ensure_pw_context
    except Exception as exc:
        logger.debug("poster: Playwright unavailable: %s", exc)
        return None
    ctx = _ensure_pw_context()
    if ctx is None:
        logger.debug("poster: Playwright context unavailable — skipping")
        return None
    url = f"https://movie.douban.com/subject/{douban_id}/"
    try:
        page = ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                page.wait_for_selector("#mainpic img", timeout=15000)
            except Exception:
                pass
            html = page.content()
        finally:
            page.close()
        return BeautifulSoup(html, "lxml")
    except Exception as exc:
        logger.warning("poster: Playwright Douban fetch error for %s: %s",
                       douban_id, exc)
        return None


def fetch_douban_poster(douban_id: str,
                        delay: float = 1.0,
                        cache_dir: Path = POSTER_CACHE_DIR) -> Optional[Path]:
    """Fetch the Douban subject page main poster (``#mainpic img``).

    Used as a fallback when eiga.com has no usable photo page (e.g. very old
    films returning 410 Gone). Tries the existing HTTP session in
    ``scraper.douban`` first; if that returns Douban's JS proof-of-work
    bot-check page, escalates to Playwright (which executes the challenge).
    """
    if not douban_id:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"douban_{douban_id}.jpg"
    if cache_path.exists() and cache_path.stat().st_size > 0:
        return cache_path

    try:
        from scraper.douban import _fetch_movie_page
        soup = _fetch_movie_page(str(douban_id), delay=delay)
    except Exception as exc:
        logger.warning("poster: Douban fetch error for subject %s: %s",
                       douban_id, exc)
        soup = None

    if _looks_like_bot_check(soup):
        logger.info("poster: HTTP fetch returned bot-check for %s — trying Playwright",
                    douban_id)
        soup = _fetch_douban_page_pw(str(douban_id))

    if soup is None:
        logger.warning("poster: Douban page unreachable for subject %s", douban_id)
        return None

    el = soup.select_one("#mainpic img")
    img_url = (el.get("src") or el.get("data-src")) if el else None
    if not img_url:
        # Final fallback: og:image meta tag (often present even when
        # #mainpic structure varies between Douban page versions).
        meta = soup.find("meta", property="og:image")
        if meta and meta.get("content"):
            img_url = meta["content"]
    if not img_url:
        logger.warning("poster: no poster image on Douban subject %s", douban_id)
        return None

    # Reuse the Douban session (cookies + Chrome TLS fingerprint via curl_cffi)
    # and set Referer to the subject page so img server doesn't return 418/403.
    try:
        from scraper.douban import _get_session
        sess = _get_session()
        prev_referer = sess.headers.get("Referer", "https://movie.douban.com/")
        sess.headers["Referer"] = f"https://movie.douban.com/subject/{douban_id}/"
        try:
            time.sleep(delay)
            ir = sess.get(img_url, timeout=20)
            ir.raise_for_status()
            cache_path.write_bytes(ir.content)
        finally:
            sess.headers["Referer"] = prev_referer
        logger.info("poster: Douban fallback used for subject %s", douban_id)
        return cache_path
    except Exception as exc:
        logger.warning("poster: failed to download Douban poster %s (%s): %s",
                       douban_id, img_url, exc)
        return None


# ── Collage assembly ────────────────────────────────────────────────────────

def _safe_filename(name: str) -> str:
    """Sanitize a section key for use as a filename component."""
    return re.sub(r'[\\/:*?"<>| ]+', "_", name).strip("_") or "section"


def _open_image(path: Path) -> Optional[Image.Image]:
    try:
        return Image.open(path).convert("RGBA")
    except Exception as exc:
        logger.debug("poster: failed to open %s: %s", path, exc)
        return None


def make_row_collage(image_paths: list[Path],
                     output_path: Path,
                     total_width: int = 1000) -> Optional[Path]:
    """把最多 5 张海报水平排成一行：不旋转、不重叠。

    每张海报宽度 = ``total_width / 5``，海报按这个宽度等比缩小（高度随比例变化）。
    海报横向并排（5 张时整图宽 ≈ total_width），高度不一时垂直居中、上下留底色。
    返回输出路径；没有可用图片返回 ``None``。
    """
    images: list[Image.Image] = []
    for p in image_paths[:5]:
        img = _open_image(p)
        if img is not None:
            images.append(img)
    if not images:
        return None

    cell_w = max(total_width // 5, 1)   # 每张海报宽度 = 最终图宽 / 5
    sized: list[Image.Image] = []
    max_h = 1
    for src in images:
        aspect = src.width / max(src.height, 1)
        h = max(int(round(cell_w / aspect)), 1)
        sized.append(src.convert("RGB").resize((cell_w, h), Image.LANCZOS))
        max_h = max(max_h, h)

    n = len(sized)
    canvas = Image.new("RGB", (cell_w * n, max_h), (245, 240, 230))
    for i, im in enumerate(sized):
        y = (max_h - im.height) // 2   # 高度不一时垂直居中
        canvas.paste(im, (i * cell_w, y))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, "JPEG", quality=88, optimize=True)
    return output_path


# ── High-level helper used by briefing.py ───────────────────────────────────

def make_section_collage(section_key: str,
                         movie_specs: list[tuple],
                         date_str: str,
                         target_count: int = 5,
                         out_dir: Path = COLLAGE_DIR,
                         poster_delay: float = 0.5) -> Optional[Path]:
    """Fetch posters and assemble a scattered collage for a briefing section.

    ``movie_specs`` is a list of ``(eiga_movie_id, douban_id)`` tuples ordered
    by priority (primary top-N first, then fallback candidates). For each
    spec, eiga.com is tried first; if its photo page is unavailable, the
    Douban subject's ``#mainpic`` is used as a fallback. Iteration stops
    once ``target_count`` valid posters have been collected.

    Returns the collage path on success, ``None`` if no posters could be
    obtained.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = _SECTION_SLUGS.get(section_key) or _safe_filename(section_key)
    out_path = out_dir / f"{slug}_{date_str}.jpg"

    # Reuse existing collage for the same section/date
    if out_path.exists() and out_path.stat().st_size > 0:
        return out_path

    posters: list[Path] = []
    obtained_log: list[str] = []
    missing_log: list[str] = []
    for spec in movie_specs:
        if len(posters) >= target_count:
            break
        # Allow callers to pass bare eiga ids for backward compatibility
        if isinstance(spec, (tuple, list)):
            eiga_id = str(spec[0]) if spec[0] is not None else ""
            douban_id = str(spec[1]) if len(spec) > 1 and spec[1] is not None else ""
        else:
            eiga_id = str(spec) if spec is not None else ""
            douban_id = ""
        if not eiga_id and not douban_id:
            continue

        p = fetch_eiga_poster(eiga_id, delay=poster_delay) if eiga_id else None
        source = "eiga"
        if p is None and douban_id:
            p = fetch_douban_poster(douban_id, delay=poster_delay)
            source = "douban"
        if p is not None:
            posters.append(p)
            obtained_log.append(f"{eiga_id or '-'}({source})")
        else:
            missing_log.append(eiga_id or douban_id or "?")

    if len(posters) < target_count:
        logger.warning(
            "collage [%s]: %d/%d posters obtained (missing: %s)",
            section_key, len(posters), target_count,
            ", ".join(missing_log) or "none",
        )
    else:
        logger.info("collage [%s]: %d posters obtained — %s",
                    section_key, len(posters), ", ".join(obtained_log))

    if not posters:
        return None

    return make_row_collage(posters, out_path)


def cleanup_poster_cache(cache_dir: Path = POSTER_CACHE_DIR) -> int:
    """Delete the individual-poster cache directory. Called after all collages
    are generated so the temporary files don't accumulate.

    Returns the number of files removed.
    """
    if not cache_dir.exists():
        return 0
    removed = 0
    for p in cache_dir.iterdir():
        if p.is_file():
            try:
                p.unlink()
                removed += 1
            except Exception as exc:
                logger.debug("cleanup: failed to remove %s: %s", p, exc)
    try:
        cache_dir.rmdir()
    except OSError:
        pass  # directory not empty (errors above) — leave it
    logger.info("Poster cache cleanup: removed %d files", removed)
    return removed
