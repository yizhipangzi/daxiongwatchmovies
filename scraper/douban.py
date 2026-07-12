"""Douban (豆瓣) movie data fetcher.

Uses Douban's subject suggest API and movie page scraping to enrich
MovieInfo objects with Chinese title, rating, vote count, and short reviews.

Anti-scraping strategy (learned from open-source Douban crawlers):
  - ``requests.Session`` for cookie persistence across requests
  - Rotating User-Agent pool
  - Full browser-like headers (Accept, Referer, Cookie)
  - Configurable Cookie & proxy support
  - Exponential back-off on 403 / connection errors
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import random
import re
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote

import requests
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as _curl_requests
    _HAS_CURL_CFFI = True
except ImportError:
    _curl_requests = None  # type: ignore
    _HAS_CURL_CFFI = False

from .base import MovieInfo, clean_text

logger = logging.getLogger(__name__)

SUGGEST_URL = "https://movie.douban.com/j/subject_suggest"
MOVIE_BASE = "https://movie.douban.com/subject"

# Keywords to remove for fallback search (for Japanese movies)
# Can extend to include '劇場版' etc.
_JP_TITLE_CLEAN_KEYWORDS = ["映画"]


def _clean_query(q: str) -> str:
    """Normalize search query: keep only the title, strip trailing metadata.

    eiga.com sometimes bleeds staff/cast info into the original-title field,
    producing queries like "Mùi Phở オフィシャルサイト スタッフ・キャスト 監督 ...".
    If the query starts with non-Japanese text and then switches to Japanese,
    we keep only the non-Japanese prefix (the real title).
    """
    if not q:
        return ""
    q = re.sub(r"[\r\n\t]+", " ", str(q))
    q = re.sub(r"\s+", " ", q).strip()

    # Strip Japanese navigation/metadata that follows a non-Japanese title.
    # Only strip when the prefix already has a non-Japanese token; fully-Japanese
    # titles like "劇場版 クレヨンしんちゃん" must not be truncated to "劇場版".
    tokens = q.split()
    prefix: list[str] = []
    has_non_jp = False
    for tok in tokens:
        jp_chars = sum(
            1 for c in tok
            if '぀' <= c <= 'ヿ' or '一' <= c <= '鿿'
        )
        is_mostly_jp = jp_chars / max(len(tok), 1) > 0.5
        if is_mostly_jp and prefix and has_non_jp:
            break
        prefix.append(tok)
        if not is_mostly_jp:
            has_non_jp = True
    if prefix and len(prefix) < len(tokens):
        q = " ".join(prefix).rstrip("/・- ")

    if len(q) > 120:
        q = q[:120].rsplit(" ", 1)[0]
    return q

# Path for the enrichment checkpoint file (enables --resume)
CHECKPOINT_PATH = Path(".douban_checkpoint.json")


class CookieExpiredError(Exception):
    """Raised when Douban returns persistent 403 — cookie likely expired."""


class IPBlockedError(Exception):
    """Raised when Douban shows misc/sorry — IP fully blocked, no point retrying."""
    pass

# ── User-Agent pool (ref: JimSunJing/douban_crawler, Yun-cong/DoubanAPI) ────
_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) "
    "Gecko/20100101 Firefox/135.0",
]

# Common desktop viewport sizes
_VIEWPORTS = [
    {"width": 1280, "height": 800},
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1920, "height": 1080},
]

# Comprehensive stealth init script injected into every Playwright page
_STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [
    {name:'Chrome PDF Plugin', description:'Portable Document Format', filename:'internal-pdf-viewer', length:1},
    {name:'Chrome PDF Viewer', description:'', filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai', length:1},
    {name:'Native Client', description:'', filename:'internal-nacl-plugin', length:2},
]});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en-US', 'en']});
if (!window.chrome) window.chrome = {};
if (!window.chrome.runtime) window.chrome.runtime = {};
const _origPermQuery = navigator.permissions && navigator.permissions.query.bind(navigator.permissions);
if (_origPermQuery) {
    navigator.permissions.query = (p) =>
        p.name === 'notifications'
        ? Promise.resolve({state: 'prompt', onchange: null})
        : _origPermQuery(p);
}
"""

# Browser-like default headers (ref: Yun-cong/DoubanAPI base_setting.py)
_DOUBAN_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8,ja;q=0.7",
    "Referer": "https://movie.douban.com/",
    "Connection": "keep-alive",
}


# ── Multi-account token pool ─────────────────────────────────────────────────
# Optional douban_tokens.yaml (gitignored, see douban_tokens.yaml.example) lists
# several logged-in Douban accounts' cookie strings. When the active one gets
# blocked (403/429, or a Playwright bot-check), we mark it blocked with a
# cooldown timestamp (persisted, so it stays skipped across runs/days too) and
# rotate to the next available account instead of failing the whole step.
# With no douban_tokens.yaml present, behavior is unchanged: single cookie
# from config.yaml's douban.cookie.

_TOKENS_PATH = Path(__file__).resolve().parent.parent / "douban_tokens.yaml"
_TOKEN_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "douban_token_state.json"
_DEFAULT_COOLDOWN_HOURS = 24.0


def _load_token_pool() -> list[dict]:
    """Load [{name, cookie, debug_user_data_dir, debug_port}, ...] from
    douban_tokens.yaml. [] if absent/empty.

    debug_user_data_dir/debug_port are optional per-account fields for the
    Playwright path: a dedicated, already-logged-in Chrome directory for
    that specific account (see _connect_or_launch_debug_chrome). Without
    them, rotation still works for the plain requests session (_douban_get)
    but Playwright has nothing account-specific to switch to for that entry.
    """
    if not _TOKENS_PATH.exists():
        return []
    try:
        import yaml as _yaml
        data = _yaml.safe_load(_TOKENS_PATH.read_text(encoding="utf-8")) or {}
        tokens = data.get("tokens") or []
        out = []
        for i, t in enumerate(tokens):
            cookie = (t.get("cookie") or "").strip()
            if not cookie:
                continue
            out.append({
                "name": t.get("name") or f"token{i}",
                "cookie": cookie,
                "debug_user_data_dir": (t.get("chrome_debug_user_data_dir") or "").strip(),
                "debug_port": int(t.get("chrome_debug_port") or 9222),
            })
        return out
    except Exception as exc:
        logger.warning("douban_tokens.yaml: failed to load: %s", exc)
        return []


def _load_token_state() -> dict:
    if not _TOKEN_STATE_PATH.exists():
        return {}
    try:
        return json.loads(_TOKEN_STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_token_state(state: dict) -> None:
    try:
        _TOKEN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        logger.warning("douban_token_state.json: failed to save: %s", exc)


def _mark_token_blocked(name: str, reason: str = "", cooldown_hours: float = _DEFAULT_COOLDOWN_HOURS) -> None:
    from datetime import datetime, timedelta, timezone
    state = _load_token_state()
    until = (datetime.now(timezone.utc) + timedelta(hours=cooldown_hours)).isoformat()
    state[name] = {"blocked_until": until, "blocked_reason": reason}
    _save_token_state(state)
    logger.warning("Douban token pool: '%s' blocked until %s (%s)", name, until, reason or "?")


def _next_available_token(exclude: Optional[set] = None) -> Optional[dict]:
    """First pool token that's neither in `exclude` (tried already this call)
    nor still cooling down from a previous block. None if pool is empty/absent
    or every entry is currently blocked."""
    from datetime import datetime, timezone
    pool = _load_token_pool()
    if not pool:
        return None
    state = _load_token_state()
    now = datetime.now(timezone.utc)
    exclude = exclude or set()
    for t in pool:
        if t["name"] in exclude:
            continue
        entry = state.get(t["name"])
        if entry and entry.get("blocked_until"):
            try:
                if datetime.fromisoformat(entry["blocked_until"]) > now:
                    continue  # still cooling down
            except Exception:
                pass
        return t
    return None


def _set_session_cookie(sess: requests.Session, cookie_str: str) -> None:
    sess.cookies.clear()
    for pair in cookie_str.split(";"):
        pair = pair.strip()
        if "=" in pair:
            k, v = pair.split("=", 1)
            sess.cookies.set(k.strip(), v.strip())


def sync_session_cookie_to_pool(name: Optional[str] = None) -> bool:
    """Upsert the current requests session's cookie into douban_tokens.yaml.

    Meant to run once at the very end of a local run_scraper.py pass: only a
    local machine can refresh a login cookie (real Chrome / pang profile —
    the cloud VM has no interactive browser), so this is the "prepare" half
    of the token pool — keep the locally-refreshed account's entry current so
    the server side always has something live to rotate to if its own pool
    tokens get blocked. No-ops if there's no session or it isn't logged in.

    `name` defaults to "primary" if not given. Returns True if the pool file
    was written.
    """
    if _session is None:
        return False
    cookies = _session.cookies.get_dict()
    if "dbcl2" not in cookies:
        return False  # anonymous cookie — nothing worth pooling

    name = name or "primary"
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())

    try:
        import yaml as _yaml
        data = {}
        if _TOKENS_PATH.exists():
            data = _yaml.safe_load(_TOKENS_PATH.read_text(encoding="utf-8")) or {}
        tokens = data.get("tokens") or []
        for t in tokens:
            if t.get("name") == name:
                t["cookie"] = cookie_str
                break
        else:
            tokens.append({"name": name, "cookie": cookie_str})
        data["tokens"] = tokens
        _TOKENS_PATH.write_text(_yaml.dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        logger.info("Douban token pool: synced session cookie into '%s' (%s)", name, _TOKENS_PATH)
        return True
    except Exception as exc:
        logger.warning("Douban token pool: failed to sync session cookie into '%s': %s", name, exc)
        return False


# ── Session management ───────────────────────────────────────────────────────

_session: Optional[requests.Session] = None
_active_token_name: Optional[str] = None  # which pool entry _session's cookie came from ("" = config.yaml single cookie, not pool)


def _get_session(config: Optional[dict] = None) -> requests.Session:
    """Return (or create) a persistent Session with browser-like headers.

    Parameters from *config* (the ``douban`` section of config.yaml):
      - ``cookie``  – raw cookie string, or ``"auto"`` to read from browser
      - ``proxy``   – HTTP(S) proxy, e.g. ``http://127.0.0.1:7890``

    If douban_tokens.yaml has any account that isn't currently cooling down
    from a prior block, its cookie is used instead of config.douban.cookie —
    see the "Multi-account token pool" section above.
    """
    global _session, _active_token_name
    if _session is not None:
        return _session

    if _HAS_CURL_CFFI:
        _session = _curl_requests.Session(impersonate="chrome131")
        logger.debug("Douban session: using curl_cffi (Chrome TLS impersonation)")
    else:
        _session = requests.Session()
        logger.warning("curl_cffi not available — using plain requests (may get 403)")
    _session.headers.update(_DOUBAN_HEADERS)
    _session.headers["User-Agent"] = random.choice(_USER_AGENTS)

    pool_token = _next_available_token()
    if pool_token:
        _set_session_cookie(_session, pool_token["cookie"])
        _active_token_name = pool_token["name"]
        logger.info("Douban session: using token pool account '%s' (%d cookie items)",
                    pool_token["name"], len(_session.cookies))
    elif config:
        _active_token_name = None
        cookie_str = config.get("cookie", "")

        # "auto" or empty → restart browser with real profile to read cookies
        if not cookie_str or cookie_str.strip().lower() == "auto":
            try:
                from .browser_cookie import (
                    get_douban_cookie,
                    _save_cookie_to_config,
                )
                cookie_str = get_douban_cookie()
                if cookie_str:
                    _save_cookie_to_config(cookie_str)
                    has_login = "dbcl2=" in cookie_str
                    logger.info(
                        "Douban session: 已获取 %s Cookie (%d 字符)",
                        "登录态" if has_login else "匿名",
                        len(cookie_str),
                    )
                else:
                    logger.warning("Douban session: 浏览器 Cookie 获取失败")
            except Exception as exc:
                logger.debug("Douban session: 浏览器 Cookie 读取失败: %s", exc)
                cookie_str = ""

        if cookie_str and cookie_str.strip().lower() != "auto":
            _set_session_cookie(_session, cookie_str)
            logger.info("Douban session: loaded cookie (%d items)",
                        len(_session.cookies))

    if config:
        proxy = config.get("proxy", "")
        if proxy:
            _session.proxies.update({"http": proxy, "https": proxy})
            logger.info("Douban session: using proxy %s", proxy)

    # Warm up: visit the homepage first so Douban sets its own cookies
    try:
        _session.get("https://movie.douban.com/", timeout=10)
        logger.debug("Douban session: warm-up GET succeeded")
        time.sleep(random.uniform(1.5, 3.0))
    except Exception:
        logger.debug("Douban session: warm-up GET failed (non-critical)")

    return _session


def init_douban_session(config: dict) -> None:
    """Explicitly initialise the Douban session (call once at startup)."""
    global _session
    _session = None          # force re-creation
    _get_session(config)


def _refresh_session_cookie() -> bool:
    """On 403: read fresh cookies from .playwright_profile (headless) and reload session.

    Cloud: skip entirely — IP-level blocks can't be fixed by rotating cookies.
    Local: 403 means Playwright will handle the full login flow; this is just a
    quick attempt to load already-saved cookies before falling back to Playwright.

    Returns True if a logged-in cookie (dbcl2) was obtained.
    """
    global _session
    try:
        from scraper import fetcher
        if fetcher.is_cloud():
            logger.info("cloud: skipping cookie refresh (IP-level block, not a cookie issue)")
            return False
    except Exception:
        pass
    try:
        from .browser_cookie import (
            get_douban_cookie_from_playwright_profile,
            _save_cookie_to_config,
        )
        logger.info("403 — trying cookie refresh from playwright_profile ...")
        cookie_str = get_douban_cookie_from_playwright_profile()
        if not cookie_str:
            logger.info("playwright_profile: no cookies — Playwright fallback will handle login")
            return False

        try:
            _save_cookie_to_config(cookie_str)
        except Exception:
            pass

        sess = _session or requests.Session()
        sess.cookies.clear()
        for pair in cookie_str.split(";"):
            pair = pair.strip()
            if "=" in pair:
                k, v = pair.split("=", 1)
                sess.cookies.set(k.strip(), v.strip())
        _session = sess
        has_login = "dbcl2=" in cookie_str
        logger.info("Cookie refreshed (%s, %d chars)",
                    "logged-in" if has_login else "anonymous", len(cookie_str))
        return has_login
    except Exception as exc:
        logger.debug("Cookie refresh unavailable: %s", exc)
        return False



# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _douban_get(url: str, params: Optional[dict] = None,
                delay: float = 1.5, retries: int = 3) -> Optional[requests.Response]:
    """GET via the shared session; exponential back-off on network errors.

    On 403/429: if a token pool is configured, marks the active account
    blocked and rotates through every other available account before giving
    up. Otherwise (or once the pool is exhausted) tries a one-shot cookie
    refresh from .playwright_profile, then raises CookieExpiredError.
    """
    # 走统一 fetcher（节流/抖动/可选 Oracle VM 中继换 IP）。传 session 以保留 cookie /
    # curl_cffi TLS 指纹；cooldown=False，因为豆瓣各 enrich 函数已有自己的退避 tracker。
    from . import fetcher
    global _active_token_name
    sess = _get_session()
    wait = delay
    for attempt in range(1, retries + 1):
        base = wait if attempt > 1 else delay
        eff = max(base, fetcher.base_delay())
        try:
            resp = fetcher.get(url, params=params, session=sess, delay=eff)
            if resp is None:
                raise requests.RequestException("fetcher returned None")
            if resp.status_code in (403, 429):
                logger.warning("Douban %d for %s — attempting recovery",
                               resp.status_code, url)

                # Token pool: mark the current account blocked, rotate through
                # every other available one before falling back further.
                tried = set()
                while _active_token_name:
                    tried.add(_active_token_name)
                    _mark_token_blocked(_active_token_name, reason=f"HTTP {resp.status_code}")
                    nxt = _next_available_token(exclude=tried)
                    if not nxt:
                        break
                    _set_session_cookie(sess, nxt["cookie"])
                    _active_token_name = nxt["name"]
                    logger.info("Douban: rotated to token pool account '%s'", nxt["name"])
                    resp = fetcher.get(url, params=params, session=sess, delay=eff)
                    if resp is None:
                        raise requests.RequestException("fetcher returned None")
                    if resp.status_code not in (403, 429):
                        resp.raise_for_status()
                        return resp
                    logger.warning("Douban %d for %s (account '%s')",
                                   resp.status_code, url, nxt["name"])

                if _refresh_session_cookie():
                    sess = _get_session()        # updated cookies
                    resp2 = fetcher.get(url, params=params, session=sess, delay=eff)
                    if resp2 is not None and resp2.status_code not in (403, 429):
                        resp2.raise_for_status()
                        return resp2
                raise CookieExpiredError(
                    f"Douban {resp.status_code} — cookie blocked. "
                    "Re-run douban_login.py to refresh, or add more accounts to douban_tokens.yaml."
                )
            resp.raise_for_status()
            return resp
        except CookieExpiredError:
            raise
        except Exception as exc:
            logger.debug("Douban GET failed (attempt %d/%d) %s: %s",
                         attempt, retries, url, exc)
            if attempt < retries:
                wait *= 2
    return None


def _fetch_movie_page(subject_id: str, delay: float = 1.5,
                      max_retries: int = 3) -> Optional[BeautifulSoup]:
    """Fetch a Douban movie subject page.

    Tries requests first; if the response looks like a bot/sorry page
    (div#content absent), falls back to the same Playwright browser
    context used for search — handles bot checks and saves fresh cookies.
    """
    try:
        from scraper import fetcher as _fetcher
        _is_cloud = _fetcher.is_cloud()
    except Exception:
        _is_cloud = False

    url = f"{MOVIE_BASE}/{subject_id}/"
    sess = _get_session()
    prev_referer = sess.headers.get("Referer", "https://movie.douban.com/")
    sess.headers["Referer"] = "https://movie.douban.com/search?cat=1002"
    try:
        resp = _douban_get(url, delay=delay, retries=max_retries)
    except CookieExpiredError:
        sess.headers["Referer"] = prev_referer
        if not _is_cloud:
            logger.info(
                "_fetch_movie_page: 403/cookie blocked for id=%s — Playwright fallback",
                subject_id,
            )
            return _fetch_movie_page_playwright(subject_id, delay=delay)
        raise
    sess.headers["Referer"] = prev_referer
    if resp is None:
        return None
    soup = BeautifulSoup(resp.text, "lxml")
    # Real movie pages always have div#content; absence means bot/sorry page.
    # Playwright fallback is local-only (cloud has no browser, and IP is blocked anyway).
    if soup.select_one("div#content") is None:
        if not _is_cloud:
            logger.info(
                "_fetch_movie_page: requests got bot/empty page for id=%s — Playwright fallback",
                subject_id,
            )
            return _fetch_movie_page_playwright(subject_id, delay=delay)
    return soup


def _search_suggest(q: str, year: int = 0,
                    delay: float = 1.5) -> Optional[dict]:
    """Query Douban subject_suggest JSON API; return best movie match."""
    resp = _douban_get(SUGGEST_URL, params={"q": q}, delay=delay)
    if resp is None:
        return None
    try:
        results = resp.json()
    except Exception:
        return None
    if not isinstance(results, list):
        return None
    movies = [r for r in results if r.get("type") == "movie"]
    if not movies:
        return None
    if year:
        year_matches = [m for m in movies if str(year) in m.get("year", "")]
        if year_matches:
            return year_matches[0]
    return movies[0]


def _parse_web_search_html(html: str, year: int = 0) -> Optional[dict]:
    """Parse a Douban search results page (shared by requests and Playwright paths)."""
    soup = BeautifulSoup(html, "lxml")
    for result_el in soup.select(".result"):
        type_el = result_el.select_one("h3 span")
        if type_el and "电影" not in type_el.get_text():
            continue
        link_el = result_el.select_one("h3 a")
        if not link_el:
            continue
        href = unquote(link_el.get("href", ""))
        m = re.search(r"movie\.douban\.com/subject/(\d+)", href)
        if not m:
            continue
        subject_id = m.group(1)
        title = link_el.get_text(strip=True)
        result_year = ""
        cast_el = result_el.select_one(".subject-cast")
        if cast_el:
            ym = re.search(r"(\d{4})", cast_el.get_text())
            if ym:
                result_year = ym.group(1)
        if year and result_year:
            try:
                if abs(year - int(result_year)) > 2:
                    continue
            except ValueError:
                pass
        logger.info("Web search found: id=%s title=%s year=%s", subject_id, title, result_year)
        return {"id": subject_id, "title": title, "year": result_year, "type": "movie"}
    return None


def _search_web(q: str, year: int = 0,
                delay: float = 1.5) -> Optional[dict]:
    """Search via Douban's search page using requests."""
    search_url = f"https://www.douban.com/search?q={quote(q)}&cat=1002"
    logger.info("Douban web search: %s", q)
    resp = _douban_get(search_url, delay=delay)
    if resp is None:
        return None
    result = _parse_web_search_html(resp.text, year)
    if result is None:
        logger.info("Douban web search: no movie match for %r", q)
    return result


# ── Persistent Playwright browser (kept open for the whole step2 session) ────

_pw_state: dict = {}  # keys: "pw", "browser", "context"
# Token-pool bookkeeping for the Playwright cookie-injection path. Kept
# separate from _pw_state (which _close_pw_state() wipes on every context
# recreation) so a rotation sequence remembers which accounts it already
# tried across several _ensure_pw_context() calls.
_pw_active_token_name: Optional[str] = None
_pw_tried_tokens: set = set()


def _load_cookie_from_config() -> str:
    try:
        import yaml as _yaml
        _cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
        if _cfg_path.exists():
            with _cfg_path.open(encoding="utf-8") as _f:
                _cfg = _yaml.safe_load(_f) or {}
            return _cfg.get("douban", {}).get("cookie", "") or ""
    except Exception:
        pass
    return ""


def _inject_cookies_into_ctx(ctx, cookie_str: str) -> None:
    if not cookie_str:
        return
    cookies = []
    for pair in cookie_str.split(";"):
        pair = pair.strip()
        if "=" in pair:
            name, value = pair.split("=", 1)
            cookies.append({"name": name.strip(), "value": value.strip(),
                            "domain": ".douban.com", "path": "/"})
    if cookies:
        ctx.add_cookies(cookies)


def _load_chrome_debug_dir_from_config() -> str:
    """config.yaml douban.chrome_debug_user_data_dir — a DEDICATED, non-default
    Chrome user-data-dir for automation (e.g. "C:\\...\\work\\chrome").

    Chrome refuses to open --remote-debugging-port on your real/default
    profile as a security measure (confirmed empirically: no launch method —
    direct launch, kill+relaunch, even the user's own Chrome shortcut — gets
    a debug port to actually listen when pointed at the real profile). A
    separate directory isn't subject to that restriction, so logging into
    Douban there once (stays logged in indefinitely) and always launching
    Chrome against that directory is the approach that actually works.
    """
    try:
        import yaml as _yaml
        _cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
        if _cfg_path.exists():
            with _cfg_path.open(encoding="utf-8") as _f:
                _cfg = _yaml.safe_load(_f) or {}
            return _cfg.get("douban", {}).get("chrome_debug_user_data_dir", "") or ""
    except Exception:
        pass
    return ""


def _connect_or_launch_debug_chrome(user_data_dir: str, pw, proxy_url: str, port: int = 9222):
    """Attach over CDP to a Chrome instance running on `user_data_dir` with
    remote debugging enabled; if none is running yet, launch one (real
    system Chrome, not Playwright's bundled Chromium — the user validated
    ``chrome.exe --user-data-dir=<dir> --remote-debugging-port=9222
    --remote-allow-origins=*`` manually first). Left running afterward so
    subsequent calls (this run or a later one) just reattach instantly.

    Returns (browser, context) or None.
    """
    import requests as _req

    def _try_attach():
        try:
            r = _req.get(f"http://127.0.0.1:{port}/json/version", timeout=1.5)
            if not r.ok:
                return None
        except Exception:
            return None
        try:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            pages_ctx = [c for c in browser.contexts if c.pages]
            ctx = pages_ctx[0] if pages_ctx else (
                browser.contexts[0] if browser.contexts
                else browser.new_context(locale="zh-CN", viewport=random.choice(_VIEWPORTS))
            )
            return browser, ctx
        except Exception as exc:
            logger.debug("Playwright: CDP attach on port %d failed: %s", port, exc)
            return None

    result = _try_attach()
    if result is not None:
        logger.info("Playwright: attached to already-running debug Chrome (port=%d, dir=%s)",
                    port, user_data_dir)
        return result

    from scraper.browser_cookie import _find_browser, _wait_for_cdp
    browser_path = _find_browser()
    if not browser_path:
        logger.warning("Playwright: no Chrome/Edge executable found to launch debug profile")
        return None

    Path(user_data_dir).mkdir(parents=True, exist_ok=True)
    args = [
        f"--user-data-dir={user_data_dir}",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if proxy_url:
        args.append(f"--proxy-server={proxy_url}")

    # Launched via a detached .bat (not subprocess.Popen directly): on Windows,
    # Popen's inherited handles can stop Chrome from actually opening the
    # debug port — same fix as scraper.browser_cookie.get_douban_cookie().
    import subprocess
    arg_str = " ".join(f'"{a}"' if " " in a else a for a in args)
    bat_path = os.path.join(tempfile.gettempdir(), "_douban_pw_debug_chrome_launch.bat")
    with open(bat_path, "w") as f:
        f.write(f'@start "" "{browser_path}" {arg_str}\n')
    subprocess.run([bat_path], timeout=15, shell=True,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    if not _wait_for_cdp(port, timeout=30):
        logger.warning("Playwright: launched debug Chrome but port %d never came up", port)
        return None
    time.sleep(1)
    result = _try_attach()
    if result is not None:
        logger.info("Playwright: launched and attached to debug Chrome (port=%d, dir=%s)",
                    port, user_data_dir)
    return result


def _close_pw_state() -> None:
    global _pw_state
    is_cdp = _pw_state.get("cdp", False)
    # Close the page (tab) we opened — always safe
    try:
        page = _pw_state.get("page")
        if page:
            page.close()
    except Exception:
        pass
    # For CDP connections don't close context/browser — that would kill the user's Chrome
    if not is_cdp:
        for key in ("context", "browser"):
            try:
                obj = _pw_state.get(key)
                if obj:
                    obj.close()
            except Exception:
                pass
    try:
        pw = _pw_state.get("pw")
        if pw:
            pw.stop()
    except Exception:
        pass
    _pw_state = {}


def close_pw_browser() -> None:
    """Close the persistent Playwright browser (auto-called at process exit)."""
    _close_pw_state()


def _ensure_pw_context():
    """Return a live Playwright BrowserContext, creating one if needed.

    Launch order:
      1. Dedicated automation Chrome profile — a NON-default user-data-dir,
         either the active token-pool account's own
         chrome_debug_user_data_dir (enables real account rotation on the
         Playwright side, see _rotate_pw_on_block) or, with no pool / no
         per-account dir, the single config.yaml
         douban.chrome_debug_user_data_dir fallback. NOT your real/default
         Chrome profile, so it isn't subject to Chrome's "no remote
         debugging on the default profile" restriction (confirmed
         empirically: no launch method gets around that restriction on the
         real profile — direct launch, kill+relaunch, even the user's own
         Chrome shortcut all fail). Attaches if already running, otherwise
         launches it with --remote-debugging-port + --remote-allow-origins=*.
      2. .playwright_profile/ (created by douban_login.py — has real Douban session)
      3. Fresh system Chrome context + config.yaml cookie injection
      4. Bundled Playwright Chromium (always available as last resort)
    """
    global _pw_state

    if _pw_state.get("context"):
        try:
            _pw_state["context"].pages  # raises if context already closed
            return _pw_state["context"]
        except Exception:
            _close_pw_state()

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    pw_token = _next_available_token(exclude=_pw_tried_tokens)
    cookie_str = pw_token["cookie"] if pw_token else _load_cookie_from_config()
    pw_token_name = pw_token["name"] if pw_token else None

    try:
        pw = sync_playwright().start()
    except Exception as exc:
        logger.warning("Playwright: failed to start: %s", exc)
        return None

    ctx = None
    browser = None
    is_cdp = False
    ctx_token_name = None  # which pool token (if any) this context corresponds to

    # Read proxy from config.yaml (same field used by the requests session)
    proxy_url = ""
    try:
        import yaml as _yaml
        _cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
        proxy_url = (_yaml.safe_load(_cfg_path.read_text(encoding="utf-8")) or {}).get(
            "douban", {}).get("proxy", "") or ""
    except Exception:
        pass

    _ANTI_DETECT_ARGS = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if proxy_url:
        _ANTI_DETECT_ARGS.append(f"--proxy-server={proxy_url}")
        logger.info("Playwright: using proxy %s", proxy_url)
    else:
        _ANTI_DETECT_ARGS.append("--no-proxy-server")

    # Playwright proxy kwarg (used for new_context in the non-persistent path)
    _pw_proxy = {"server": proxy_url} if proxy_url else None

    # Option 1: dedicated automation Chrome profile with its own debug port —
    # NOT your real/default Chrome profile, so it isn't subject to Chrome's
    # "no remote debugging on the real profile" restriction. Attaches if
    # already running, otherwise launches it. Logging into Douban there once
    # keeps it logged in indefinitely, independent of your daily Chrome.
    #
    # Prefers the current token-pool account's OWN debug directory (so
    # rotating accounts on a block also switches which dedicated Chrome
    # Playwright drives); falls back to the single config.yaml
    # douban.chrome_debug_user_data_dir when there's no pool, or the active
    # pool token doesn't have one configured.
    if ctx is None:
        if pw_token and pw_token.get("debug_user_data_dir"):
            debug_dir = pw_token["debug_user_data_dir"]
            debug_port = pw_token["debug_port"]
            candidate_token_name = pw_token_name
        else:
            debug_dir = _load_chrome_debug_dir_from_config()
            debug_port = 9222
            candidate_token_name = None
        if debug_dir:
            result = _connect_or_launch_debug_chrome(debug_dir, pw, proxy_url, port=debug_port)
            if result is not None:
                browser, ctx = result
                is_cdp = True
                ctx_token_name = candidate_token_name
            else:
                logger.warning("Playwright: could not attach/launch debug Chrome at '%s' — falling back",
                               debug_dir)

    # Option 2: use .playwright_profile — logged-in Douban session from douban_login.py
    if ctx is None:
        pw_profile = Path(__file__).resolve().parent.parent / ".playwright_profile"
        if pw_profile.is_dir():
            # Remove stale lock files before launching
            for lock_name in ("LOCK", "SingletonLock", "SingletonCookie", "SingletonSocket"):
                for search_dir in (pw_profile, pw_profile / "Default"):
                    lf = search_dir / lock_name
                    if lf.exists():
                        try:
                            lf.unlink()
                        except Exception:
                            pass
            try:
                ctx = pw.chromium.launch_persistent_context(
                    str(pw_profile),
                    channel="chrome",
                    headless=False,
                    args=_ANTI_DETECT_ARGS,
                    locale="zh-CN",
                    viewport=random.choice(_VIEWPORTS),
                )
                logger.info("Playwright: using .playwright_profile")
            except Exception as exc:
                logger.warning("Playwright: .playwright_profile failed: %s — falling back to fresh context", exc)
                ctx = None

    # Option 3: fresh system Chrome context + inject config.yaml cookies
    if ctx is None and browser is None:
        try:
            browser = pw.chromium.launch(
                headless=False,
                channel="chrome",
                args=_ANTI_DETECT_ARGS,
            )
            logger.info("Playwright: using system Chrome (fresh context)")
        except Exception as exc:
            logger.warning("Playwright: channel=chrome failed: %s — trying bundled Chromium", exc)

    # Option 4: bundled Playwright Chromium (independent of system Chrome, always works)
    if ctx is None and browser is None:
        try:
            browser = pw.chromium.launch(
                headless=False,
                args=_ANTI_DETECT_ARGS,
            )
            logger.info("Playwright: using bundled Chromium (system Chrome unavailable)")
        except Exception as exc:
            pw.stop()
            logger.warning("Playwright: all launch attempts failed: %s", exc)
            return None

    # Build a context from whichever browser launched (Options 2 or 3)
    if ctx is None and browser is not None:
        ctx = browser.new_context(
            locale="zh-CN",
            proxy=_pw_proxy,
            viewport=random.choice(_VIEWPORTS),
            user_agent=random.choice(_USER_AGENTS),
        )
        logger.info("Playwright: fresh context")

    # For CDP-connected contexts (user's real Chrome):
    # - skip stealth init script (already fingerprinted as a real Chrome profile)
    # - skip cookie injection (user already has login cookies in their profile)
    if not is_cdp:
        ctx.add_init_script(_STEALTH_INIT_SCRIPT)
        if cookie_str:
            _inject_cookies_into_ctx(ctx, cookie_str)
            logger.info("Playwright: injected config cookies (dbcl2=%s)",
                        "yes" if "dbcl2=" in cookie_str else "no")

    _pw_state.update({"pw": pw, "browser": browser, "context": ctx, "cdp": is_cdp})
    global _pw_active_token_name
    # ctx_token_name is set precisely by Option 1 when it used a pool
    # account's own debug Chrome; for the cookie-injection paths (Options
    # 2/3, non-CDP) the pool token (if any) drove `cookie_str` instead, so
    # fall back to that. Anything else (Option 0's generic catch-all, or no
    # pool at all) stays untracked — _rotate_pw_on_block won't try to rotate
    # a session it doesn't recognize as a specific pool account.
    _pw_active_token_name = ctx_token_name or (pw_token_name if not is_cdp else None)
    atexit.register(close_pw_browser)
    return ctx


def _handle_bot_check(page) -> None:
    """Detect a Douban bot-check/login-wall page and abort immediately.

    With a real logged-in Chrome profile (config.yaml douban.chrome_profile),
    hitting this page means the IP is rate-limited/blocked, not "not logged
    in" — no amount of waiting or manual solving in this run fixes that, it
    just needs to cool down. So any detection raises IPBlockedError right
    away so the caller (enrich_all_movies) stops the whole step cleanly
    instead of retrying movie by movie into the same block.
    """
    # Only texts unique to bot-verification / full-page-blocked pages.
    # Do NOT add generic login prompts like "需要先登录" — those appear on
    # normal movie pages too and cause false positives that block data extraction.
    _BOT_TEXTS = ("证明你是人类", "像机器人程序")

    def _is_bot_page() -> bool:
        try:
            # sec.douban.com / misc/sorry are unambiguous — check URL first.
            url = page.url
            if "sec.douban.com" in url or "misc/sorry" in url:
                return True
            # For everything else, use page content as the ground truth.
            # After a JS-based login redirect page.url can be stale (still shows
            # accounts/login) even though the browser has already moved to the
            # movie page. Checking id="content" (present on every normal Douban
            # page, absent on login/bot/sorry pages) avoids that false positive.
            content = page.content()
            if 'id="content"' in content:
                return False
            # No id="content" — definitely a blocking page. Determine type.
            return ("accounts/login" in url or
                    "accounts/login" in content or
                    any(t in content for t in _BOT_TEXTS))
        except Exception:
            return False

    if not _is_bot_page():
        return

    try:
        current_url = page.url
    except Exception:
        current_url = ""
    logger.warning("Playwright: bot-check/blocked page detected (url=%s) — aborting step", current_url)
    raise IPBlockedError(f"Douban bot-check/blocked page (url={current_url})")


def _search_web_playwright(q: str, year: int = 0) -> Optional[dict]:
    """Playwright fallback: uses the movie.douban.com search bar (human-like).

    Navigates to the movie homepage, types the query into the search box
    (#inp-query) and presses Enter — avoids directly hitting the search URL
    which is more likely to trigger bot detection. On a bot-check, rotates
    through the token pool the same way _fetch_movie_page_playwright does.
    """
    return _rotate_pw_on_block(q, lambda: _search_web_playwright_once(q, year))


def _search_web_playwright_once(q: str, year: int = 0) -> Optional[dict]:
    ctx = _ensure_pw_context()
    if ctx is None:
        logger.debug("Playwright context unavailable — skipping browser fallback")
        return None

    logger.info("Douban Playwright search: %s", q)
    try:
        page = ctx.new_page()
        page.bring_to_front()
        try:
            logger.debug("Playwright: navigating to movie.douban.com ...")
            page.goto("https://movie.douban.com/", wait_until="domcontentloaded", timeout=45000)
            logger.info("Playwright: page loaded, url=%s", page.url)
            _handle_bot_check(page)

            # Human-like pause after page load before interacting
            time.sleep(random.uniform(1.0, 2.5))

            # Hover over search box first, then click
            search = page.locator("#inp-query")
            search.hover()
            time.sleep(random.uniform(0.2, 0.6))
            search.click()
            time.sleep(random.uniform(0.4, 0.8))

            # Type with randomised per-session keystroke delay
            search.press_sequentially(q, delay=random.randint(60, 130))
            time.sleep(random.uniform(0.3, 0.7))
            search.press("Enter")

            # Wait for search results page to load
            page.wait_for_load_state("domcontentloaded", timeout=20000)
            _handle_bot_check(page)
            try:
                page.wait_for_selector(".result", timeout=15000)
            except Exception:
                pass

            html = page.content()
        finally:
            page.close()

        result = _parse_web_search_html(html, year)
        if result is None:
            logger.info("Douban Playwright search: no movie match for %r", q)
        return result
    except IPBlockedError:
        raise  # propagate so enrich_all_movies can break the loop
    except TimeoutError as exc:
        # Playwright navigation timed out (not a bot-check — that raises
        # IPBlockedError above) — close Chrome so next call starts fresh.
        logger.warning("Playwright: navigation timed out for %r — closing context (%s)", q, exc)
        _close_pw_state()
        return None
    except Exception as exc:
        logger.warning("Playwright search error for %r: %s", q, exc)
        return None


def _rotate_pw_on_block(label: str, fn):
    """Call `fn()`; on IPBlockedError, if a token-pool account was active for
    the current Playwright context, mark it blocked and retry with the next
    available one (closing the context first so _ensure_pw_context() picks a
    fresh token). Bounded by the pool size. Re-raises once the pool (or the
    single non-pool session) is exhausted, so the caller can still stop the
    whole step as before.
    """
    global _pw_active_token_name
    pool_size = len(_load_token_pool())
    attempts = max(pool_size, 1)
    for _ in range(attempts):
        try:
            return fn()
        except IPBlockedError:
            if not _pw_active_token_name:
                raise  # not using the pool (pang/.playwright_profile/config cookie) — no rotation possible
            _mark_token_blocked(_pw_active_token_name, reason="playwright bot-check")
            _pw_tried_tokens.add(_pw_active_token_name)
            _pw_active_token_name = None
            _close_pw_state()  # force a fresh context (and thus a fresh token) next call
            nxt = _next_available_token(exclude=_pw_tried_tokens)
            if not nxt:
                raise  # pool exhausted
            logger.info("Playwright: rotating to token pool account '%s' after block (%s)", nxt["name"], label)
    raise IPBlockedError(f"Douban token pool exhausted after {attempts} attempt(s) ({label})")


def _fetch_movie_page_playwright(subject_id: str, delay: float = 1.5) -> Optional[BeautifulSoup]:
    """Fetch a Douban movie detail page via Playwright, reusing the same browser
    tab across calls. On a bot-check (IPBlockedError), rotates through every
    other available token-pool account before giving up — see
    _rotate_pw_on_block for the shared retry loop."""
    return _rotate_pw_on_block(subject_id, lambda: _fetch_movie_page_playwright_once(subject_id, delay))


def _fetch_movie_page_playwright_once(subject_id: str, delay: float = 1.5) -> Optional[BeautifulSoup]:
    ctx = _ensure_pw_context()
    if ctx is None:
        logger.debug("Playwright context unavailable — skipping movie page fallback")
        return None

    url = f"{MOVIE_BASE}/{subject_id}/"
    logger.info("Douban Playwright enrich: %s", url)
    try:
        # Reuse the persistent page; create a new one only on first call or if it died.
        page = _pw_state.get("page")
        is_new_page = False
        if page is not None:
            try:
                page.url  # raises PlaywrightError if the page has been closed
            except Exception:
                page = None
                _pw_state.pop("page", None)
        if page is None:
            page = ctx.new_page()
            _pw_state["page"] = page
            is_new_page = True

        page.bring_to_front()

        if is_new_page:
            # First call: visit homepage to establish session / warm up Referer.
            logger.debug("Playwright enrich: new page — loading homepage first ...")
            page.goto("https://movie.douban.com/", wait_until="domcontentloaded", timeout=45000)
            _handle_bot_check(page)
            time.sleep(random.uniform(1.0, 2.0))

        # Navigate to the movie page (previous page acts as natural Referer).
        logger.debug("Playwright enrich: navigating to subject page ...")
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        _handle_bot_check(page)
        time.sleep(random.uniform(delay * 0.5, delay))
        html = page.content()
        # Do NOT close the page — keep it open for the next movie.
        return BeautifulSoup(html, "lxml")
    except IPBlockedError:
        raise  # handled by _rotate_pw_on_block
    except TimeoutError as exc:
        # Playwright navigation timed out (not a bot-check — that raises
        # IPBlockedError above) — close the browser so next call starts fresh.
        logger.warning("Playwright: navigation timed out for id=%s — closing context (%s)", subject_id, exc)
        _close_pw_state()
        return None
    except Exception as exc:
        logger.warning("Playwright: enrich error for id=%s: %s", subject_id, exc)
        # Page may be in a broken state; drop it so the next call gets a fresh one.
        _pw_state.pop("page", None)
        return None


def _search_douban_by_query(query: str, year: int = 0,
                            delay: float = 1.5) -> Optional[dict]:
    """Suggest API first (lightweight), web search as fallback."""
    q = _clean_query(query)
    if not q:
        return None
    result = _search_suggest(q, year=year, delay=delay)
    if result:
        return result
    return _search_web(q, year=year, delay=delay)


def _split_jp_title(title: str) -> list[str]:
    """Split a Japanese movie title into meaningful keyword segments.

    Splits on spaces, ``・``, ``／``, ``〜``, ``～``, ``―``, parentheses,
    brackets, and common series-subtitle separators.  Returns segments
    sorted longest-first (longer = more specific).
    """
    # Normalise separators
    t = re.sub(r"[・／〜～―–—]+", " ", title)
    # Split on spaces, brackets, and parentheses
    t = re.sub(r"[（）()【】「」『』《》\[\]]+", " ", t)
    parts = [p.strip() for p in t.split() if p.strip()]
    # Drop tiny segments (single kana / punctuation) — not useful as queries
    parts = [p for p in parts if len(p) >= 2]
    # Drop noise words that never help Douban search
    parts = [p for p in parts if p not in _JP_STOPWORDS]
    # Deduplicate while keeping order
    seen: set[str] = set()
    unique: list[str] = []
    for p in parts:
        if p not in seen:
            seen.add(p)
            unique.append(p)
    # Longest first (more specific)
    unique.sort(key=len, reverse=True)
    return unique


# Japanese title fragments that carry no search value on Douban.
_JP_STOPWORDS: set[str] = {
    "劇場版", "映画", "特別編", "特別版", "総集編", "完結編",
    "前編", "後編", "前篇", "後篇", "上巻", "下巻",
    "アニメ", "実写版", "新作", "リバイバル",
    "字幕版", "吹替版", "日本語吹替版",
    "IMAX版", "4DX版", "MX4D版", "ドルビー版",
    "THE", "MOVIE", "FILM", "FINAL", "PART",
}



# ── Match helpers (module-level so tests and step2_douban can import them) ───

def _norm(s: str) -> str:
    return re.sub(r"[\s\W]+", "", s or "").lower()


def _year_ok(year: int, result: dict) -> bool:
    if not year or not result.get("year"):
        return True
    try:
        return abs(year - int(result["year"])) <= 2
    except (ValueError, TypeError):
        return True


def _title_ok(result: dict, target_title: str) -> bool:
    tn = _norm(target_title)
    if not tn:
        return True
    main = result.get("title", "")
    candidates = [main]
    # Douban format: "中文名 外文名" — extract foreign part after leading CJK word
    parts = main.split(None, 1)
    if len(parts) == 2:
        first = parts[0]
        if all('一' <= ch <= '鿿' or '぀' <= ch <= 'ヿ' for ch in first if ch.strip()):
            candidates.append(parts[1])
    candidates.append(result.get("title_cn", ""))
    return any(
        cn and (tn == cn or tn in cn or cn in tn)
        for cn in (_norm(c) for c in candidates)
    )


def _country_ok(result: dict, target_country: str) -> bool:
    rc = result.get("country", "")
    return not target_country or not rc or target_country in rc or rc in target_country


def _validate_via_page(subject_id: str, orig_title: str,
                       year: int, country: Optional[str],
                       delay: float) -> bool:
    """Fetch the movie page and verify title/year/country. Returns True if match."""
    try:
        soup = _fetch_movie_page(subject_id, delay)
        if not soup:
            return False
        meta = _parse_meta(soup)
        aka_field = meta.get("aka") or []
        aka_list = (
            [x.strip() for x in re.split(r"[/、,]", aka_field) if x.strip()]
            if isinstance(aka_field, str) else list(aka_field)
        )
        candidates = [
            meta.get("title_original") or "",
            meta.get("title_en") or "",
            meta.get("title_cn") or "",
        ] + aka_list
        on = _norm(orig_title)
        title_matched = any(
            on == _norm(c) or on in _norm(c) or _norm(c) in on
            for c in candidates if c
        )
        # Soft fallback: substring fails when eiga's title is missing articles
        # ("Chaplin: Spirit of the Tramp" vs Douban "Charlie Chaplin: The Spirit
        # of the Tramp"). Token-subset is safe here because year+country below
        # still gate the match.
        if not title_matched and on:
            title_matched = _title_token_overlap(orig_title, candidates)
        if not title_matched and on:
            title_matched = on in _norm(soup.get_text(separator=" "))
        if not title_matched:
            return False
        page_year = meta.get("year")
        if year and page_year and abs(year - page_year) > 2:
            logger.debug("Douban: page title matched but year failed (%d vs %d)", year, page_year)
            return False
        if country and meta.get("country"):
            try:
                from pipeline.step2_douban import _check_country_match
                if not _check_country_match(country, meta["country"]):
                    logger.debug("Douban: page title matched but country failed (%r vs %r)",
                                 country, meta["country"])
                    return False
            except Exception:
                pass
        logger.info("Douban: page-level match accepted: %r -> id=%s year=%s country=%s",
                    orig_title, subject_id, page_year, meta.get("country"))
        return True
    except Exception:
        logger.debug("Douban: failed to fetch/inspect full page for title heuristic")
        return False


def _strip_diacritics(s: str) -> str:
    """'Kék Pelikán' → 'Kek Pelikan' — helps Douban's search index find European titles."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


_TITLE_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "of", "and", "or", "in", "on", "to", "for", "with",
    "de", "la", "le", "el", "und", "et",
})


def _title_tokens(s: str) -> set[str]:
    """Significant lowercase tokens from a Latin/mixed title (accents stripped)."""
    s = _strip_diacritics(s).lower()
    return {t for t in re.split(r"[^a-z0-9]+", s) if t and t not in _TITLE_STOPWORDS}


def _title_token_overlap(orig_title: str, candidates: list[str]) -> bool:
    """Soft title match: every content token of orig_title appears in a candidate.

    Lets eiga's shorter foreign title ("Chaplin: Spirit of the Tramp") match a
    Douban entry with a richer name ("Charlie Chaplin: The Spirit of the Tramp")
    without admitting unrelated films. Requires ≥2 content tokens so a single
    shared word can't trigger a false positive.
    """
    orig_toks = _title_tokens(orig_title)
    if len(orig_toks) < 2:
        return False
    for c in candidates:
        if c and orig_toks.issubset(_title_tokens(c)):
            return True
    return False


def search_douban(title_jp: str, year: int = 0,
                  delay: float = 1.5,
                  movie: Optional[MovieInfo] = None,
                  orig_title: Optional[str] = None,
                  country: Optional[str] = None) -> Optional[dict]:
    """Search Douban by Japanese movie title; return the best match dict.

    Search order:
      - Pure foreign movies: orig_title only (resolved via eiga.com / TOHO).
      - Japanese movies (incl. co-productions like "アメリカ・日本合作"):
        title_jp first, then orig_title.
    Each query tries suggest API first, web search as fallback.
    Year +/-2 and title substring match are required; page-level validation
    is used as a fallback when the fast title check fails.
    """
    if not orig_title:
        from .title_resolver import resolve_original_title
        if movie is not None:
            orig_title = resolve_original_title(movie, delay=1.0)
        else:
            from .eiga import lookup_original_title
            orig_title = lookup_original_title(title_jp, delay=1.0)

    # Country: explicit kwarg takes priority, then fall back to movie.country
    if country is None and movie is not None:
        if hasattr(movie, "country"):
            country = movie.country
        elif isinstance(movie, dict):
            country = movie.get("country")

    is_jp = bool(country and ("日本" in country or country.lower() == "japan"))
    # If country is unknown or orig_title is None, always include title_jp so we
    # never end up with an all-None query list (which produces zero searches).
    if is_jp or not orig_title:
        queries = [title_jp, orig_title]
    else:
        queries = [orig_title]

    tried: set[str] = set()

    def _try_queries(qs: list) -> Optional[dict]:
        for q in qs:
            if not q or q in tried:
                continue
            tried.add(q)
            r = _search_douban_by_query(q, year, delay)
            if r and _year_ok(year, r):
                return r
        if year:
            for q in qs:
                if not q:
                    continue
                fq = f"{q} {year}"
                if fq in tried:
                    continue
                tried.add(fq)
                r = _search_douban_by_query(fq, year, delay)
                if r and _year_ok(year, r):
                    return r
        return None

    result = _try_queries(queries)
    if result and _year_ok(year, result):
        if orig_title and not _title_ok(result, orig_title) and not is_jp:
            logger.debug("Douban: fast title check failed (got %r, want %r), fetching page",
                         result.get("title"), orig_title)
            if _validate_via_page(result.get("id", ""), orig_title, year, country, delay):
                return result
        elif country and not _country_ok(result, country):
            logger.warning("Douban: result country mismatch: got %r, want %r",
                           result.get("country"), country)
        else:
            return result

    # Fallback: strip JP-specific keywords
    if is_jp and any(k in title_jp for k in _JP_TITLE_CLEAN_KEYWORDS):
        cleaned = title_jp
        for k in _JP_TITLE_CLEAN_KEYWORDS:
            cleaned = cleaned.replace(k, "")
        cleaned = cleaned.strip()
        if cleaned and cleaned != title_jp:
            logger.info("Douban: fallback search with cleaned JP title %r", cleaned)
            result = _try_queries([cleaned])
            if result and _year_ok(year, result):
                return result

    # Fallback: strip diacritics (e.g. "Kék Pelikán" → "Kek Pelikan")
    if orig_title:
        normalized = _strip_diacritics(orig_title)
        if normalized and normalized != orig_title:
            logger.info("Douban: diacritic-normalized fallback %r (was %r)", normalized, orig_title)
            result = _try_queries([normalized])
            if result and _year_ok(year, result):
                return result

    # Final fallback: Playwright-rendered browser search.
    # ⚠️ cloud（无头 VM、cron 无人值守）下**禁用**：Playwright 会开可见浏览器且
    # 遇豆瓣人机验证要人工通过——VM 上必然卡住超时。这类硬匹配留给本地有人时跑。
    try:
        from scraper import fetcher
        if fetcher.is_cloud():
            logger.info("Douban: cloud 环境跳过 Playwright 搜索（硬匹配留给本地）")
            return None
    except Exception:
        pass

    # Douban's search page is JS-rendered; requests gets empty HTML for some
    # queries. Playwright actually executes the JS and sees real results.
    # Only triggered once per movie after all requests paths fail.
    # Use orig_title for foreign movies, title_jp for Japanese or when orig_title
    # resolution failed (orig_title=None means eiga lookup returned nothing).
    pw_query = (orig_title if (not is_jp and orig_title) else title_jp)
    if pw_query:
        # Note: do NOT skip queries that are in `tried` — those were tried via
        # requests (which gets empty JS-rendered HTML). Playwright actually
        # executes JS and sees real results, so the same query must be retried.
        pw_queries = [pw_query]
        stripped = _strip_diacritics(pw_query)
        if stripped != pw_query:
            pw_queries.append(stripped)
        for pq in pw_queries:
            result = _search_web_playwright(pq, year=year)
            if result and _year_ok(year, result):
                return result

    return None




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
    """Parse director, cast, genre, year, duration, country, titles, etc.

    All text (director, cast, genre) comes from the Douban page in Chinese.
    """
    info: dict = {}

    # Director (Chinese names from Douban page)
    directors = [a.get_text(strip=True) for a in soup.select("a[rel='v:directedBy']")]
    info["director"] = " / ".join(directors)

    # Cast (Chinese names from Douban page)
    cast_els = soup.select("a[rel='v:starring']")
    info["cast"] = " / ".join(a.get_text(strip=True) for a in cast_els[:5])

    # Genre (Chinese from Douban page)
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

    # Chinese title — prefer "XXX的剧情简介" heading (pure CN name)
    synopsis_h2 = soup.find("h2", string=re.compile(r".+的剧情简介"))
    if not synopsis_h2:
        # Sometimes the text is inside an <i> child
        for h2 in soup.find_all("h2"):
            i_tag = h2.find("i")
            if i_tag and "的剧情简介" in i_tag.get_text():
                synopsis_h2 = h2
                break
    if synopsis_h2:
        txt = synopsis_h2.get_text()
        m_cn = re.match(r"(.+?)的剧情简介", txt)
        if m_cn:
            info["title_cn"] = clean_text(m_cn.group(1))
    if "title_cn" not in info:
        # Fallback: h1 span (less reliable for mixed-language titles)
        title_el = soup.select_one("span[property='v:itemreviewed']")
        if not title_el:
            title_el = soup.select_one("h1 span")
        if title_el:
            info["title_cn"] = clean_text(title_el.get_text()).split()[0]

    # --- Country / region ---
    info_div = soup.select_one("#info")
    if info_div:
        info_text = info_div.get_text()
        cm = re.search(r"制片国家/地区:\s*(.+)", info_text)
        if cm:
            info["country"] = clean_text(cm.group(1).split("\n")[0])
        am = re.search(r"又名:\s*(.+)", info_text)
        if am:
            info["aka"] = [clean_text(x) for x in am.group(1).split("\n")[0].split("/")]
        # Detect TV series: pages with "集数:" are not movies
        if re.search(r"集数:\s*\d+", info_text):
            info["is_tv"] = True

    # --- Original title (from page h1) ---
    # Douban h1 usually: "中文名 OriginalTitle"
    h1_title_el = soup.select_one("span[property='v:itemreviewed']") or soup.select_one("h1 span")
    if h1_title_el:
        full = clean_text(h1_title_el.get_text())
        parts = full.split(None, 1)
        if len(parts) == 2:
            info["title_original"] = parts[1]

    # --- English title ---
    # Heuristic: scan aka list for a Latin-only title
    _latin_re = re.compile(r"^[A-Za-z0-9\s:;,.'\-!?&()]+$")
    for aka in info.get("aka", []):
        if aka and _latin_re.match(aka):
            info["title_en"] = aka
            break
    # Fallback: if original title is Latin-only, use it as English title too
    if not info.get("title_en") and info.get("title_original"):
        if _latin_re.match(info["title_original"]):
            info["title_en"] = info["title_original"]

    # --- Want to watch / Watched counts ---
    for a_tag in soup.select("a[href*='wish'] span, .subject-others-interests-ft a"):
        text = a_tag.get_text()
        wm = re.search(r"(\d+)人想看", text)
        if wm:
            info["want_to_watch"] = int(wm.group(1))
    for a_tag in soup.select("a[href*='collect'] span, .subject-others-interests-ft a"):
        text = a_tag.get_text()
        wm = re.search(r"(\d+)人看过", text)
        if wm:
            info["watched"] = int(wm.group(1))
    # Broader fallback: scan #interest_sectl
    interest = soup.select_one("#interest_sectl")
    if interest:
        itxt = interest.get_text()
        if not info.get("want_to_watch"):
            wm = re.search(r"(\d+)人想看", itxt)
            if wm:
                info["want_to_watch"] = int(wm.group(1))
        if not info.get("watched"):
            wm = re.search(r"(\d+)人看过", itxt)
            if wm:
                info["watched"] = int(wm.group(1))

    # --- Trailer URLs from Douban ---
    trailers: list[str] = []
    for link in soup.select("a[href*='trailer'], a[href*='/video/']"):
        href = link.get("href", "")
        if href and href not in trailers:
            trailers.append(href)
    info["trailers"] = trailers[:5]

    # --- Poster (海报) ---
    # 豆瓣条目页左上角 #mainpic 里的 <img> 就是海报，src 形如
    # https://img9.doubanio.com/view/photo/s_ratio_poster/public/p2879886456.jpg
    # 作为 eiga.com 没有海报时的回退来源。
    poster_el = soup.select_one("#mainpic img")
    if poster_el and poster_el.get("src"):
        info["poster"] = poster_el["src"].strip()

    return info


def _find_youtube_trailer(soup: BeautifulSoup) -> str:
    """Look for a YouTube trailer link on the Douban page."""
    for link in soup.select("a[href]"):
        href = link.get("href", "")
        if "youtube.com/watch" in href or "youtu.be/" in href:
            return href
    return ""


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


def _parse_short_reviews_labeled(soup: BeautifulSoup, n: int = 5) -> list[dict]:
    """Extract top n short reviews with inferred author status and author info.

    Returns list of dicts:
        {"text": str, "status": "watched"|"want"|"unknown",
         "author": str, "author_url": str}
    Heuristics:
      - If comment meta contains '看过' or a rating element is present -> 'watched'
      - If comment meta contains '想看' -> 'want'
      - Otherwise -> 'unknown'
    """
    out: list[dict] = []
    for item in soup.select(".comment-item"):
        short_el = item.select_one(".short")
        if not short_el:
            continue
        text = clean_text(short_el.get_text())
        status = "unknown"
        author_name = ""
        author_url = ""
        # Look for comment meta area
        meta_el = item.select_one(".comment-info")
        meta_text = meta_el.get_text() if meta_el else ""
        if meta_el:
            # First <a href> in .comment-info points to the reviewer's profile.
            a_tag = meta_el.select_one("a[href]")
            if a_tag:
                author_name = clean_text(a_tag.get_text())
                author_url = a_tag.get("href", "") or ""
        if "看过" in meta_text:
            status = "watched"
        elif "想看" in meta_text:
            status = "want"
        else:
            # If there is an element indicating a rating (class contains 'rating'), treat as watched
            if item.select_one("span[class*='rating'], i[class*='rating']"):
                status = "watched"
        out.append({
            "text": text,
            "status": status,
            "author": author_name,
            "author_url": author_url,
        })
        if len(out) >= n:
            break
    return out


def enrich_movie_with_douban(movie: MovieInfo,
                             delay: float = 1.5,
                             review_count: int = 5) -> MovieInfo:
    """Fetch Douban data and attach it to a MovieInfo in-place."""
    match = search_douban(movie.title_jp, year=movie.year, delay=delay,
                          movie=movie)
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
    movie.title_original = meta.get("title_original", movie.title_original)
    movie.title_en = meta.get("title_en", movie.title_en)
    movie.country = meta.get("country", movie.country)
    movie.director = meta.get("director", movie.director)
    movie.cast = meta.get("cast", movie.cast)
    movie.genre = meta.get("genre", movie.genre)
    if meta.get("year"):
        movie.year = meta["year"]
    if meta.get("duration"):
        movie.duration = meta["duration"]
    movie.douban_want_to_watch = meta.get("want_to_watch", 0)
    movie.douban_watched = meta.get("watched", 0)
    movie.trailer_douban_urls = meta.get("trailers", [])

    # Try to find YouTube official trailer
    movie.trailer_youtube_url = _find_youtube_trailer(page_soup)

    movie.douban_short_reviews = _parse_short_reviews(page_soup, n=review_count)

    logger.info(
        "Douban: %s → %s (%.1f分, %d人评价, 想看%d, 看过%d)",
        movie.title_jp, movie.title_cn or "?",
        movie.douban_score, movie.douban_votes,
        movie.douban_want_to_watch, movie.douban_watched
    )
    return movie


def enrich_all_movies(movies: list[MovieInfo],
                      config: dict,
                      resume: bool = False) -> list[MovieInfo]:
    """Enrich all movies with Douban data, respecting rate limits.

    If *resume* is ``True``, load previously saved progress from the
    checkpoint file and skip already-enriched movies.

    On :class:`CookieExpiredError`, saves a checkpoint and re-raises so
    the caller can tell the user to update the cookie and ``--resume``.
    """
    delay = config.get("request_delay", 1.5)
    review_count = config.get("short_review_count", 5)
    # Playwright handles sessions automatically — no cookie needed

    # ── Resume: load enriched data from checkpoint ──
    done_map: dict[str, dict] = {}
    if resume and CHECKPOINT_PATH.exists():
        try:
            saved = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
            done_map = {item["title_jp"]: item for item in saved}
            logger.info("从断点恢复：已加载 %d 部电影的豆瓣数据", len(done_map))
        except Exception as exc:
            logger.warning("读取断点文件失败，将重新抓取: %s", exc)

    enriched_count = 0
    for i, movie in enumerate(movies, 1):
        # Skip if already enriched in a previous run
        if movie.title_jp in done_map:
            _apply_checkpoint(movie, done_map[movie.title_jp])
            logger.debug("跳过（已缓存）: %s", movie.title_jp)
            enriched_count += 1
            continue

        enrich_movie_with_douban(movie, delay=delay, review_count=review_count)
        enriched_count += 1
        logger.info("[%d/%d] %s 完成", i, len(movies), movie.title_jp)

    # All done — clean up checkpoint
    if CHECKPOINT_PATH.exists():
        CHECKPOINT_PATH.unlink()
        logger.info("全部完成，已清理断点文件")

    return movies


def _save_checkpoint(movies: list[MovieInfo]) -> None:
    """Save enrichment progress to a JSON checkpoint file."""
    data = []
    for m in movies:
        if not m.douban_id:
            continue  # not yet enriched
        data.append({
            "title_jp": m.title_jp,
            "douban_id": m.douban_id,
            "douban_url": m.douban_url,
            "douban_score": m.douban_score,
            "douban_votes": m.douban_votes,
            "title_cn": m.title_cn,
            "title_en": m.title_en,
            "title_original": m.title_original,
            "country": m.country,
            "director": m.director,
            "cast": m.cast,
            "genre": m.genre,
            "year": m.year,
            "duration": m.duration,
            "douban_want_to_watch": m.douban_want_to_watch,
            "douban_watched": m.douban_watched,
            "trailer_douban_urls": m.trailer_douban_urls,
            "trailer_youtube_url": m.trailer_youtube_url,
            "douban_short_reviews": m.douban_short_reviews,
        })
    CHECKPOINT_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("断点已保存到 %s（%d 部电影）", CHECKPOINT_PATH, len(data))


def _apply_checkpoint(movie: MovieInfo, saved: dict) -> None:
    """Restore enriched fields from a checkpoint dict onto a MovieInfo."""
    movie.douban_id = saved.get("douban_id", "")
    movie.douban_url = saved.get("douban_url", "")
    movie.douban_score = saved.get("douban_score", 0.0)
    movie.douban_votes = saved.get("douban_votes", 0)
    movie.title_cn = saved.get("title_cn", "")
    movie.title_en = saved.get("title_en", "")
    movie.title_original = saved.get("title_original", "")
    movie.country = saved.get("country", "")
    movie.director = saved.get("director", "")
    movie.cast = saved.get("cast", "")
    movie.genre = saved.get("genre", "")
    if saved.get("year"):
        movie.year = saved["year"]
    if saved.get("duration"):
        movie.duration = saved["duration"]
    movie.douban_want_to_watch = saved.get("douban_want_to_watch", 0)
    movie.douban_watched = saved.get("douban_watched", 0)
    movie.trailer_douban_urls = saved.get("trailer_douban_urls", [])
    movie.trailer_youtube_url = saved.get("trailer_youtube_url", "")
    movie.douban_short_reviews = saved.get("douban_short_reviews", [])