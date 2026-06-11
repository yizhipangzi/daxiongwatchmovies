"""只重新生成 briefing MD（不抓取、不 enrich，从现有 DB 数据直接渲染）。

改了 briefing 模板 / 板块条件 / 拼图样式后，用这个快速重出简报，
不用重跑 step1/2/3。

注意：拼图按「板块+日期」缓存在 output/collages/，改了拼图样式要先删该目录
再跑，才会用新样式重新生成。
"""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from scraper.logging_setup import setup as _setup_logging
_setup_logging()

from generator.briefing import generate_wechat_briefing_md


def main():
    md = generate_wechat_briefing_md(top_n=5)
    print(f"\n[OK] briefing 已重新生成: {md}")


if __name__ == "__main__":
    main()
