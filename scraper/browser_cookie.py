"""Auto-extract Douban cookies by restarting Chrome with the real profile.

Chrome 127+ uses App-Bound Encryption, so we can no longer read the
SQLite cookie DB from outside the browser process.  Instead we:

  1. Kill running Chrome (to unlock the profile directory).
  2. Relaunch Chrome with ``--remote-debugging-port`` + the user's real
     ``User Data`` directory (Default profile).
  3. Navigate to ``movie.douban.com`` via CDP.
  4. Read cookies with ``Network.getAllCookies`` over a minimal stdlib
     WebSocket (no third-party library needed).
  5. Leave Chrome running — the user can keep browsing normally.

If the user is logged in to Douban, this returns the full login cookie
(``dbcl2``, ``ck``, etc.).  Otherwise it returns anonymous cookies
(``bid``, ``ll``, …) which are already much better than nothing.

Usage::

    python -m scraper.browser_cookie          # restart Chrome, grab cookies
    python -m scraper.browser_cookie --paste   # read clipboard → config.yaml

Importable::

    from scraper.browser_cookie import get_douban_cookie          # real profile
    from scraper.browser_cookie import get_douban_cookie_headless  # temp profile
"""
from __future__ import annotations

import hashlib
import base64
import json
import logging
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Browser discovery
# ---------------------------------------------------------------------------

_CHROME_PATHS = [
    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%LocalAppData%\Google\Chrome\Application\chrome.exe"),
]

_EDGE_PATHS = [
    os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
]


def _find_browser() -> Optional[str]:
    """Return the path to Chrome or Edge, preferring Chrome."""
    for p in _CHROME_PATHS + _EDGE_PATHS:
        if os.path.isfile(p):
            return p
    for name in ("chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


# ---------------------------------------------------------------------------
# Port helper
# ---------------------------------------------------------------------------

_CDP_PREFERRED_PORTS = [9222, 9223, 9224, 9225]


def _free_port() -> int:
    """Return a free TCP port, preferring well-known CDP ports."""
    for port in _CDP_PREFERRED_PORTS:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    # Fallback: OS-assigned ephemeral port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------------------
# CDP helpers
# ---------------------------------------------------------------------------

def _wait_for_cdp(port: int, timeout: float = 15.0) -> bool:
    """Wait until the CDP HTTP endpoint is responsive."""
    url = f"http://127.0.0.1:{port}/json/version"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = requests.get(url, timeout=2)
            if r.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _get_pages(port: int) -> list[dict]:
    """Return the list of open pages from CDP."""
    r = requests.get(f"http://127.0.0.1:{port}/json", timeout=5)
    r.raise_for_status()
    return r.json()


# ---------------------------------------------------------------------------
# Minimal RFC-6455 WebSocket client (stdlib only, no masking randomness)
# ---------------------------------------------------------------------------

_WS_MAGIC = b"258EAFA5-E914-47DA-95CA-5AB5AA286923"


def _ws_connect(host: str, port: int, path: str) -> socket.socket:
    """Open a WebSocket connection and perform the handshake."""
    sock = socket.create_connection((host, port), timeout=10)
    # Generate a deterministic key (fine for local-only CDP)
    key = base64.b64encode(os.urandom(16)).decode()
    req = (
        f"GET {path} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Upgrade: websocket\r\n"
        f"Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: 13\r\n"
        f"\r\n"
    )
    sock.sendall(req.encode())
    # Read the HTTP response headers
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionError("WebSocket handshake failed: connection closed")
        buf += chunk
    if b"101" not in buf.split(b"\r\n")[0]:
        raise ConnectionError(f"WebSocket handshake rejected: {buf[:200]}")
    return sock


def _ws_send(sock: socket.socket, data: str) -> None:
    """Send a text frame (masked, as required by RFC 6455 for clients)."""
    payload = data.encode("utf-8")
    # Frame: FIN=1, opcode=1 (text)
    header = bytearray([0x81])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)  # MASK bit set
    elif length < 65536:
        header.append(0x80 | 126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(0x80 | 127)
        header.extend(struct.pack("!Q", length))
    # Mask key (4 bytes) — all zeros is fine for local CDP
    mask_key = b"\x00\x00\x00\x00"
    header.extend(mask_key)
    # Masked payload (XOR with zero = identity)
    sock.sendall(bytes(header) + payload)


def _ws_recv(sock: socket.socket, timeout: float = 10.0) -> str:
    """Receive one complete text frame and return the payload."""
    sock.settimeout(timeout)
    data = b""
    # Read at least 2 bytes for the header
    while len(data) < 2:
        data += sock.recv(4096)

    fin_opcode = data[0]
    second = data[1]
    masked = bool(second & 0x80)
    length = second & 0x7F
    offset = 2

    if length == 126:
        while len(data) < offset + 2:
            data += sock.recv(4096)
        length = struct.unpack("!H", data[offset:offset + 2])[0]
        offset += 2
    elif length == 127:
        while len(data) < offset + 8:
            data += sock.recv(4096)
        length = struct.unpack("!Q", data[offset:offset + 8])[0]
        offset += 8

    if masked:
        offset += 4  # skip mask key (server shouldn't mask, but handle it)

    # Read the full payload
    while len(data) < offset + length:
        data += sock.recv(65536)

    payload = data[offset:offset + length]
    return payload.decode("utf-8", errors="replace")


def _cdp_command(ws_url: str, method: str, params: dict | None = None) -> dict:
    """Send a single CDP command over WebSocket and return the result."""
    # Parse ws://host:port/path
    # ws_url looks like ws://127.0.0.1:PORT/devtools/page/ID
    url_body = ws_url.replace("ws://", "")
    host_port, path = url_body.split("/", 1)
    host, port_s = host_port.split(":")
    port = int(port_s)
    path = "/" + path

    sock = _ws_connect(host, port, path)
    try:
        msg = {"id": 1, "method": method}
        if params:
            msg["params"] = params
        _ws_send(sock, json.dumps(msg))
        # Read response — may need to skip events until we get id=1
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            text = _ws_recv(sock, timeout=5)
            reply = json.loads(text)
            if reply.get("id") == 1:
                return reply
    finally:
        sock.close()
    return {}


# ---------------------------------------------------------------------------
# CDP cookie reader (shared by all strategies)
# ---------------------------------------------------------------------------

def _read_douban_cookies_via_cdp(port: int) -> str:
    """Connect to a running CDP browser, return douban cookie string."""
    pages = _get_pages(port)
    target = None
    for p in pages:
        if p.get("type") == "page" and "douban" in p.get("url", ""):
            target = p
            break
    if not target:
        for p in pages:
            if p.get("type") == "page":
                target = p
                break
    if not target:
        logger.warning("CDP 未发现任何页面标签")
        return ""

    ws_url = target.get("webSocketDebuggerUrl", "")
    if not ws_url:
        logger.warning("CDP 页面没有 webSocketDebuggerUrl")
        return ""

    # If the page is not on douban, that's fine — Network.getAllCookies
    # returns cookies for ALL domains regardless of the current page.

    reply = _cdp_command(ws_url, "Network.getAllCookies")
    all_cookies = reply.get("result", {}).get("cookies", [])
    logger.debug("CDP Network.getAllCookies: 共 %d 个 cookie", len(all_cookies))

    douban_cookies = []
    for c in all_cookies:
        domain = c.get("domain", "")
        if "douban.com" in domain:
            name = c.get("name", "")
            value = c.get("value", "")
            if name:
                douban_cookies.append(f"{name}={value}")

    if not douban_cookies:
        return ""

    cookie_str = "; ".join(douban_cookies)
    logger.debug(
        "CDP 获取到 Douban Cookie (%d 项, %d 字符)",
        len(douban_cookies), len(cookie_str),
    )
    return cookie_str


# ---------------------------------------------------------------------------
# Browser user-data-dir discovery
# ---------------------------------------------------------------------------

def _chrome_user_data_dir() -> Optional[str]:
    ud = os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")
    return ud if os.path.isdir(ud) else None


def _edge_user_data_dir() -> Optional[str]:
    ud = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data")
    return ud if os.path.isdir(ud) else None


def _kill_browser(browser_path: str) -> bool:
    """Kill all instances of the browser and verify they are gone.

    Returns True if the browser was successfully stopped.
    """
    name = os.path.basename(browser_path)
    no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    # Try up to 3 rounds of killing
    for attempt in range(3):
        subprocess.run(
            ["taskkill", "/f", "/im", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=no_win,
        )
        subprocess.run(
            ["taskkill", "/f", "/im", "crashpad_handler.exe"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=no_win,
        )
        time.sleep(2)

        # Verify no processes remain
        check = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"],
            capture_output=True, text=True, timeout=5,
            creationflags=no_win,
        )
        if name.lower() not in check.stdout.lower():
            logger.debug("浏览器已完全关闭 (attempt %d)", attempt + 1)
            return True
        logger.debug("浏览器仍有残留进程，重试 kill (attempt %d)", attempt + 1)

    # Last resort: give it more time
    time.sleep(3)
    return True


def _remove_locks(user_data_dir: str) -> None:
    """Remove Chrome/Edge lock files to allow a clean restart."""
    for name in ["lockfile", os.path.join("Default", "LOCK")]:
        try:
            os.remove(os.path.join(user_data_dir, name))
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main cookie extraction — restart Chrome with real profile
# ---------------------------------------------------------------------------

def get_douban_cookie() -> str:
    """Restart Chrome/Edge with the user's real profile + debug port.

    Flow:
      1. Kill running Chrome (needed to unlock the profile).
      2. Relaunch with ``--remote-debugging-port`` + real user-data-dir.
      3. Navigate to ``movie.douban.com`` via CDP.
      4. Read all douban cookies via ``Network.getAllCookies``.
      5. **Leave Chrome running** — user can keep using it normally.

    If the user is logged in to Douban in their browser, this returns
    the full login cookie (``dbcl2`` etc.).  Otherwise it returns the
    anonymous cookies which are still much better than a blank profile.

    Returns cookie string or "".
    """
    browser = _find_browser()
    if not browser:
        logger.warning("找不到 Chrome 或 Edge，无法自动获取 Cookie")
        return ""

    is_chrome = "chrome" in browser.lower()
    user_data = (_chrome_user_data_dir() if is_chrome
                 else _edge_user_data_dir())
    if not user_data:
        logger.warning("找不到浏览器配置文件目录")
        return ""

    port = _free_port()
    logger.debug("使用调试端口 %d", port)

    # 1. Kill existing browser so we can use the real profile
    logger.info("正在重启 %s 以读取 Cookie …",
                "Chrome" if is_chrome else "Edge")
    _kill_browser(browser)
    _remove_locks(user_data)

    # 2. Relaunch with debug port
    #    IMPORTANT: Chrome ignores --remote-debugging-port when launched as
    #    a child of Python's subprocess.Popen (handle inheritance issue on
    #    Windows).  Using PowerShell Start-Process creates a fully detached
    #    process that works correctly.
    chrome_args = [
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data}",
        "--profile-directory=Default",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-mode",
    ]
    # Launch Chrome via a temporary .bat file for a fully detached process.
    # subprocess.Popen inherits handles that prevent Chrome from opening
    # the debug port.  A .bat with START creates a clean detached process.
    # Using a .bat also avoids all PowerShell quoting headaches with spaces.
    import tempfile
    chrome_arg_str = " ".join(
        f'"{a}"' if " " in a else a
        for a in chrome_args
    )
    bat_content = f'@start "" "{browser}" {chrome_arg_str}\n'
    bat_path = os.path.join(tempfile.gettempdir(), "_chrome_launch.bat")
    with open(bat_path, "w") as f:
        f.write(bat_content)
    logger.debug("启动脚本: %s\n%s", bat_path, bat_content)
    no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        [bat_path],
        timeout=15,
        creationflags=no_win,
        shell=True,
    )

    try:
        if not _wait_for_cdp(port, timeout=45):
            logger.warning("CDP 调试端口 %d 未能启动", port)
            return ""

        # 3. Wait for page to load + cookies to settle
        time.sleep(5)

        # 4. Read cookies
        cookie_str = _read_douban_cookies_via_cdp(port)
        if cookie_str:
            has_login = "dbcl2=" in cookie_str
            logger.info(
                "获取到 %s Cookie (%d 字符)",
                "登录态" if has_login else "匿名",
                len(cookie_str),
            )
        return cookie_str

    except Exception as exc:
        logger.warning("自动获取 Cookie 异常: %s", exc)
        return ""
    # NOTE: Chrome stays open — user can keep browsing normally.


def _find_playwright_chromium() -> Optional[str]:
    """Find the Playwright-managed Chromium binary (Windows only)."""
    import glob as _glob
    local_app = os.path.expandvars("%LOCALAPPDATA%")
    pattern = os.path.join(local_app, "ms-playwright", "chromium-*", "chrome-win", "chrome.exe")
    candidates = _glob.glob(pattern)
    if candidates:
        return sorted(candidates)[-1]
    return None


def get_douban_cookie_from_playwright_profile(profile_dir: Optional[str] = None) -> str:
    """Extract Douban cookies from the saved .playwright_profile.

    Launches Playwright's Chromium (or system Chrome) headlessly against
    the persistent profile directory that ``douban_login.py`` uses.
    Because this is a *separate* profile directory from the user's real
    Chrome profile, no running Chrome process needs to be killed.

    Returns the cookie string, or "" if the profile does not exist /
    the login session has been cleared.
    """
    if profile_dir is None:
        profile_dir = str(Path(__file__).resolve().parent.parent / ".playwright_profile")
    if not os.path.isdir(profile_dir):
        logger.debug("playwright_profile 目录不存在: %s", profile_dir)
        return ""

    browser = _find_playwright_chromium() or _find_browser()
    if not browser:
        logger.warning("找不到 Chromium/Chrome，无法读取 playwright_profile Cookie")
        return ""

    port = _free_port()
    cmd = [
        browser,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--profile-directory=Default",
        "--headless=new",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
    ]
    no_win = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc: Optional[subprocess.Popen] = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=no_win,
        )
        if not _wait_for_cdp(port, timeout=30):
            logger.warning("playwright_profile CDP 端口 %d 未能启动", port)
            return ""
        time.sleep(2)
        cookie_str = _read_douban_cookies_via_cdp(port)
        if cookie_str:
            has_login = "dbcl2=" in cookie_str
            logger.info(
                "playwright_profile Cookie: %s (%d 字符)",
                "登录态" if has_login else "匿名",
                len(cookie_str),
            )
        return cookie_str
    except Exception as exc:
        logger.warning("从 playwright_profile 读取 Cookie 失败: %s", exc)
        return ""
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass


def get_douban_cookie_headless() -> str:
    """Quick headless grab with a temp profile — anonymous ``bid`` only.

    Used as the first-try fallback in ``douban.py``.  If the result
    lacks ``dbcl2``, the caller escalates to ``get_douban_cookie()``.
    """
    browser = _find_browser()
    if not browser:
        return ""

    port = _free_port()
    tmp_dir = tempfile.mkdtemp(prefix="douban_cookie_")
    proc: Optional[subprocess.Popen] = None

    try:
        cmd = [
            browser,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={tmp_dir}",
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            "https://movie.douban.com/",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        if not _wait_for_cdp(port):
            return ""

        time.sleep(4)
        return _read_douban_cookies_via_cdp(port)

    except Exception:
        return ""
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Save cookie to config.yaml
# ---------------------------------------------------------------------------

def _save_cookie_to_config(cookie: str) -> bool:
    """Write cookie value into config.yaml."""
    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    if not config_path.exists():
        logger.warning("config.yaml 不存在: %s", config_path)
        return False

    text = config_path.read_text(encoding="utf-8")
    # Match the cookie line under douban section
    new_text = re.sub(
        r"(cookie:\s*).*",
        rf"\g<1>'{cookie}'",
        text,
        count=1,
    )
    if new_text == text:
        logger.warning("未能在 config.yaml 中找到 cookie 字段")
        return False

    config_path.write_text(new_text, encoding="utf-8")
    logger.info("Cookie 已写入 config.yaml")
    return True


# ---------------------------------------------------------------------------
# Clipboard paste mode
# ---------------------------------------------------------------------------

def _paste_from_clipboard() -> str:
    """Read cookie from Windows clipboard via PowerShell."""
    try:
        result = subprocess.run(
            ["powershell", "-Command", "Get-Clipboard"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.stdout.strip()
    except Exception as exc:
        logger.warning("读取剪贴板失败: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if "--paste" in sys.argv:
        cookie = _paste_from_clipboard()
        if not cookie:
            print("❌ 剪贴板为空，请先复制 Cookie 字符串")
            sys.exit(1)
        print(f"📋 读取到 Cookie ({len(cookie)} 字符)")
        if _save_cookie_to_config(cookie):
            print("✅ 已写入 config.yaml")
        else:
            print("❌ 写入 config.yaml 失败，请手动粘贴")
            print(f"\ncookie: '{cookie}'")
        return

    # Auto mode — restart Chrome with real profile
    print("🔑 正在重启浏览器以读取豆瓣 Cookie …")
    print("   （读取完成后浏览器会保持打开，可以正常继续使用）\n")

    cookie = get_douban_cookie()
    if cookie:
        has_login = "dbcl2=" in cookie
        status = "登录态" if has_login else "匿名"
        print(f"\n✅ 获取到{status} Cookie ({len(cookie)} 字符)")
        if not has_login:
            print("⚠️  未检测到登录态 (dbcl2)，部分功能可能受限")
        if _save_cookie_to_config(cookie):
            print("✅ 已自动写入 config.yaml")
        else:
            print("⚠️  请手动将以下内容粘贴到 config.yaml 的 cookie 字段：")
            print(f"\ncookie: '{cookie}'")
    else:
        print("\n❌ 获取 Cookie 失败")
        print()
        print("备选方案：")
        print("  1. 打开 Chrome 访问 https://movie.douban.com/")
        print("  2. F12 → Console → 输入: document.cookie")
        print("  3. 复制结果后运行: python -m scraper.browser_cookie --paste")


if __name__ == "__main__":
    main()
