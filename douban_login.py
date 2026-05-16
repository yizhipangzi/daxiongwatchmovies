"""Douban login: open visible browser, auto-detect login, save to persistent profile."""
import sys, time, re
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from playwright.sync_api import sync_playwright
from pathlib import Path

PROFILE_DIR = str(Path(__file__).parent / ".playwright_profile")
CONFIG_PATH = Path(__file__).parent / "config.yaml"


def _save_cookie_to_config(cookie_str: str) -> bool:
    """Write the cookie string into config.yaml under the douban.cookie key."""
    if not CONFIG_PATH.exists():
        return False
    text = CONFIG_PATH.read_text(encoding="utf-8")
    new_text = re.sub(r"(cookie:\s*).*", rf"\g<1>'{cookie_str}'", text, count=1)
    if new_text == text:
        return False
    CONFIG_PATH.write_text(new_text, encoding="utf-8")
    return True

with sync_playwright() as p:
    # Persistent context = cookies/localStorage survive across runs
    ctx = p.chromium.launch_persistent_context(
        PROFILE_DIR,
        headless=False,
        locale="zh-CN",
        channel="chromium",
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()

    page.goto("https://accounts.douban.com/passport/login",
              wait_until="domcontentloaded", timeout=30000)
    print("请在浏览器窗口中登录豆瓣...")
    print("登录成功后程序会自动检测并继续。")

    # Poll for dbcl2 cookie (= logged in)
    for i in range(300):  # 5 min timeout
        cookies = ctx.cookies("https://www.douban.com")
        if any(c["name"] == "dbcl2" for c in cookies):
            print("✅ 检测到 dbcl2，登录成功！")
            break
        time.sleep(1)
        if i % 10 == 9:
            print(f"  等待登录中... ({i+1}s)")
    else:
        print("❌ 超时，未检测到登录。")
        ctx.close()
        sys.exit(1)

    # Extract all douban cookies from the Playwright context and save to config.yaml
    all_cookies = ctx.cookies("https://www.douban.com")
    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in all_cookies if c.get("name"))
    if cookie_str:
        has_dbcl2 = "dbcl2=" in cookie_str
        print(f"✅ 获取到 Cookie ({len(cookie_str)} 字符, 含dbcl2登录态: {'是' if has_dbcl2 else '否'})")
        if _save_cookie_to_config(cookie_str):
            print("✅ Cookie 已自动写入 config.yaml，下次运行 pipeline 时无需重新登录")
        else:
            print("⚠️  未能自动写入 config.yaml，请手动将以下 Cookie 粘贴到配置文件：")
            print(f"\ncookie: '{cookie_str}'")
    else:
        print("⚠️  未能提取 Cookie")

    # Verify suggest API works
    page.goto("https://movie.douban.com/", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)
    result = page.evaluate("""
        async () => {
            const resp = await fetch('/j/subject_suggest?q=银魂');
            return await resp.text();
        }
    """)
    import json
    data = json.loads(result)
    if data:
        print(f"✅ Suggest API 正常！返回 {len(data)} 条结果")
        for item in data[:3]:
            print(f"   {item.get('title','?')} ({item.get('year','?')})")
    else:
        print("⚠️ Suggest API 仍返回空（但登录已成功，后续可能需要等一下）")

    ctx.close()
    print(f"\n登录状态已保存到 {PROFILE_DIR}")
    print("后续 pipeline 会自动使用此登录状态，cookie 由浏览器自动管理不会失效。")
