"""Run Step 3: 全量豆瓣详情抓取（云端定时任务用）.

默认无参数即跑**全量**：为所有东京院线 + 小众电影（= 小程序数据集）抓取豆瓣详情
（director / cast / genre / 想看 / 看过 / 短评 / trailers），写入 douban_details。
可多次运行 —— 按 refresh 窗口滚动刷新、断点续跑、遇限流自动退避，适合放云端 cron。

用法:
  python run_step3.py                   # 全量抓取（默认）
  python run_step3.py --limit 50        # 本次最多抓 50 部
  python run_step3.py --refresh-days 3  # 3 天内已刷新则跳过
  python run_step3.py --briefing        # 旧行为：仅 top-N briefing + 生成 MD
"""
import sys
import argparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from scraper.logging_setup import setup as _setup_logging
_setup_logging()  # local=仅控制台；cloud=另写 logs/当天.log，只留 2 天

from pipeline.step2_douban import enrich_for_briefing, enrich_all_movies
from generator.briefing import generate_wechat_briefing_md


def main():
    parser = argparse.ArgumentParser(description="Step 3: 全量豆瓣详情抓取")
    parser.add_argument("--briefing", action="store_true",
                        help="旧行为：仅抓 briefing top-N 并生成 WeChat MD")
    parser.add_argument("--top-n", type=int, default=5,
                        help="--briefing 模式下每板块的电影数（默认 5）")
    parser.add_argument("--limit", type=int, default=0,
                        help="全量模式下本次最多抓取的电影数（0=不限）")
    parser.add_argument("--refresh-days", type=int, default=7,
                        help="全量模式下，详情在该天数内已刷新则跳过（默认 7）")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="每次豆瓣请求的基础延迟秒数（默认 2.0）")
    args = parser.parse_args()

    if args.briefing:
        enriched = enrich_for_briefing(top_n=args.top_n, delay=args.delay)
        print(f"\nBriefing enrich: {enriched} movies")
        md = generate_wechat_briefing_md(top_n=args.top_n)
        print(f"\nBriefing MD: {md}")
        return

    # 默认：全量
    enriched = enrich_all_movies(
        delay=args.delay,
        refresh_days=args.refresh_days,
        limit=args.limit,
    )
    print(f"\n全量豆瓣详情抓取: {enriched} 部电影")


if __name__ == "__main__":
    main()
