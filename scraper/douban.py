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
import random
import re
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


# ── Session management ───────────────────────────────────────────────────────

_session: Optional[requests.Session] = None


def _get_session(config: Optional[dict] = None) -> requests.Session:
    """Return (or create) a persistent Session with browser-like headers.

    Parameters from *config* (the ``douban`` section of config.yaml):
      - ``cookie``  – raw cookie string, or ``"auto"`` to read from browser
      - ``proxy``   – HTTP(S) proxy, e.g. ``http://127.0.0.1:7890``
    """
    global _session
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

    if config:
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
            for pair in cookie_str.split(";"):
                pair = pair.strip()
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    _session.cookies.set(k.strip(), v.strip())
            logger.info("Douban session: loaded cookie (%d items)",
                        len(_session.cookies))

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
    """On 403: try to grab a fresh cookie from the local browser, update session.

    Tries playwright_profile first (headless, no Chrome kill), then falls
    back to reading from the real Chrome profile.

    Returns True if a logged-in cookie (dbcl2) was obtained.
    """
    global _session
    try:
        from .browser_cookie import (
            get_douban_cookie_from_playwright_profile,
            get_douban_cookie,
            _save_cookie_to_config,
        )

        # 1. Try the saved playwright profile (fast, headless, no browser kill)
        logger.info("403 — trying cookie refresh from playwright_profile ...")
        cookie_str = get_douban_cookie_from_playwright_profile()
        if not cookie_str or "dbcl2=" not in cookie_str:
            # 2. Fall back to real Chrome (kills and restarts Chrome)
            logger.info("playwright_profile has no dbcl2 — trying real Chrome profile ...")
            cookie_str = get_douban_cookie()

        if not cookie_str:
            logger.warning("Cookie refresh failed — no cookie obtained")
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
        return has_login  # Only True if we got a real login token
    except Exception as exc:
        logger.debug("Cookie refresh unavailable: %s", exc)
        return False



# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _douban_get(url: str, params: Optional[dict] = None,
                delay: float = 1.5, retries: int = 3) -> Optional[requests.Response]:
    """GET via the shared session; exponential back-off on network errors.

    On 403/429: tries a one-shot cookie refresh, then raises CookieExpiredError
    immediately — no point burning more retries with a blocked session.
    """
    sess = _get_session()
    wait = delay
    for attempt in range(1, retries + 1):
        base = wait if attempt > 1 else delay
        time.sleep(base + random.uniform(0.4, max(1.0, base * 0.5)))
        try:
            resp = sess.get(url, params=params, timeout=15)
            if resp.status_code in (403, 429):
                logger.warning("Douban %d for %s — attempting cookie refresh",
                               resp.status_code, url)
                if _refresh_session_cookie():
                    sess = _get_session()        # updated cookies
                    resp2 = sess.get(url, params=params, timeout=15)
                    if resp2.status_code not in (403, 429):
                        resp2.raise_for_status()
                        return resp2
                raise CookieExpiredError(
                    f"Douban {resp.status_code} — cookie blocked. "
                    "Re-run douban_login.py to refresh."
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
    """Fetch a Douban movie subject page via requests."""
    url = f"{MOVIE_BASE}/{subject_id}/"
    sess = _get_session()
    prev_referer = sess.headers.get("Referer", "https://movie.douban.com/")
    # Simulate navigating from search results to movie page
    sess.headers["Referer"] = "https://movie.douban.com/search?cat=1002"
    resp = _douban_get(url, delay=delay, retries=max_retries)
    sess.headers["Referer"] = prev_referer
    if resp is None:
        return None
    return BeautifulSoup(resp.text, "lxml")


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


def _load_chrome_profile_from_config() -> str:
    try:
        import yaml as _yaml
        _cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
        if _cfg_path.exists():
            with _cfg_path.open(encoding="utf-8") as _f:
                _cfg = _yaml.safe_load(_f) or {}
            return _cfg.get("douban", {}).get("chrome_profile", "") or ""
    except Exception:
        pass
    return ""


def _close_pw_state() -> None:
    global _pw_state
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
      1. .playwright_profile/ (created by douban_login.py — has real Douban session)
      2. Fresh Chrome context + config.yaml cookie injection
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

    cookie_str = _load_cookie_from_config()

    try:
        pw = sync_playwright().start()
    except Exception as exc:
        logger.warning("Playwright: failed to start: %s", exc)
        return None

    ctx = None
    browser = None

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

    # Option 1: use .playwright_profile — logged-in Douban session from douban_login.py
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

    # Option 2: fresh Chrome context + inject config.yaml cookies
    if ctx is None:
        try:
            browser = pw.chromium.launch(
                headless=False,
                channel="chrome",
                args=_ANTI_DETECT_ARGS,
            )
        except Exception as exc:
            pw.stop()
            logger.warning("Playwright: failed to launch Chrome: %s", exc)
            return None
        ctx = browser.new_context(
            locale="zh-CN",
            proxy=_pw_proxy,
            viewport=random.choice(_VIEWPORTS),
            user_agent=random.choice(_USER_AGENTS),
        )
        logger.info("Playwright: fresh context")

    ctx.add_init_script(_STEALTH_INIT_SCRIPT)

    # Inject config.yaml cookies into whichever context we got
    if cookie_str:
        _inject_cookies_into_ctx(ctx, cookie_str)
        logger.info("Playwright: injected config cookies (dbcl2=%s)",
                    "yes" if "dbcl2=" in cookie_str else "no")

    _pw_state.update({"pw": pw, "browser": browser, "context": ctx})
    atexit.register(close_pw_browser)
    return ctx


def _save_pw_cookies_to_config(page) -> None:
    """Extract Douban cookies from Playwright page, save to config.yaml,
    and reinitialize the requests session so it immediately benefits."""
    try:
        all_cookies = page.context.cookies("https://www.douban.com")
        cookie_str = "; ".join(
            f"{c['name']}={c['value']}" for c in all_cookies if c.get("name")
        )
        if not cookie_str or "dbcl2=" not in cookie_str:
            return
        cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
        if not cfg_path.exists():
            return
        text = cfg_path.read_text(encoding="utf-8")
        new_text = re.sub(r"(cookie:\s*).*", rf"\g<1>'{cookie_str}'", text, count=1)
        if new_text != text:
            cfg_path.write_text(new_text, encoding="utf-8")
            logger.info("Cookies saved to config.yaml (%d chars)", len(cookie_str))
        # Reinitialize the requests session immediately with the new cookies
        global _session
        _session = None
        try:
            import yaml as _yaml
            _cfg_path = Path(__file__).resolve().parent.parent / "config.yaml"
            with _cfg_path.open(encoding="utf-8") as _f:
                _cfg = _yaml.safe_load(_f) or {}
            _get_session(_cfg.get("douban", {}))
            logger.info("Requests session reinitialized with fresh cookies")
        except Exception as exc:
            logger.debug("Failed to reinitialize requests session: %s", exc)
    except Exception as exc:
        logger.debug("Failed to save cookies: %s", exc)


def _handle_bot_check(page) -> None:
    """Detect Douban bot-check page; pause and wait for the user to pass it.

    After the user solves the verification, extracts fresh cookies and saves
    them back to config.yaml so the requests session also benefits.
    """
    _BOT_TEXTS = ("证明你是人类", "像机器人程序")

    def _is_bot_page() -> bool:
        try:
            url = page.url
            if "sec.douban.com" in url:
                return True
            if "misc/sorry" in url:
                return True
            return any(t in page.content() for t in _BOT_TEXTS)
        except Exception:
            return False

    if not _is_bot_page():
        return

    # Immediately refresh the requests session token on bot detection
    logger.info("Bot-check detected — refreshing session token ...")
    try:
        _refresh_session_cookie()
    except Exception as exc:
        logger.debug("Session refresh on bot-check failed: %s", exc)

    print(
        "\n⚠️  豆瓣检测到机器人 — 请在浏览器窗口中完成验证，通过后程序自动继续（最多等 3 分钟）...\n",
        flush=True,
    )
    logger.warning("Douban bot-check detected — waiting for manual verification (3 min timeout)")

    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(2)
        if not _is_bot_page():
            _save_pw_cookies_to_config(page)
            print("✅ 验证通过，继续搜索...\n", flush=True)
            logger.info("Douban bot-check passed")
            return

    raise TimeoutError("Douban bot-check: manual verification timed out (3 min)")


def _search_web_playwright(q: str, year: int = 0) -> Optional[dict]:
    """Playwright fallback: uses the movie.douban.com search bar (human-like).

    Navigates to the movie homepage, types the query into the search box
    (#inp-query) and presses Enter — avoids directly hitting the search URL
    which is more likely to trigger bot detection.
    """
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
    except TimeoutError as exc:
        # Bot-check timed out — close Chrome so next call starts fresh
        logger.warning("Playwright: bot-check timed out for %r — closing context (%s)", q, exc)
        _close_pw_state()
        return None
    except Exception as exc:
        logger.warning("Playwright search error for %r: %s", q, exc)
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

    return info


def _find_youtube_trailer(soup: BeautifulSoup) -> str:
    """Look for a YouTube trailer link on the Douban page."""
    for link in soup.select("a[href]"):
        href = link.get("href", "")
        if "youtube.com/watch" in href or "youtu.be/" in href:
            return href
    return ""


def fetch_short_review_ratings(subject_id: str, delay: float = 1.5) -> list[int]:
    """Fetch star ratings from the 'want to watch' (status=P) comments page.

    Used for movies that have a Douban entry but no aggregated score yet —
    early viewer star ratings (1-5) give a recommendation signal.

    Returns a list of integer ratings (1-5) found on the first page. Empty list
    if the page cannot be fetched or no rated reviews are present.
    """
    url = f"{MOVIE_BASE}/{subject_id}/comments?status=P"
    sess = _get_session()
    prev_referer = sess.headers.get("Referer", "https://movie.douban.com/")
    sess.headers["Referer"] = f"{MOVIE_BASE}/{subject_id}/"
    try:
        resp = _douban_get(url, delay=delay)
    finally:
        sess.headers["Referer"] = prev_referer
    if resp is None:
        return []
    soup = BeautifulSoup(resp.text, "lxml")
    ratings: list[int] = []
    # Each comment block has <span class="comment-info">…<span class="allstarN0 rating">…
    for info in soup.select(".comment-info"):
        rating_span = info.select_one("span[class*='allstar']")
        if not rating_span:
            continue
        for cls in rating_span.get("class") or []:
            m = re.match(r"allstar([1-5])0$", cls)
            if m:
                ratings.append(int(m.group(1)))
                break
    return ratings


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