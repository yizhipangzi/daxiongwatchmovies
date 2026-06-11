"""重新抓取电影的上映日期（修复 release_date）。

历史上 step1 的「劇場公開日」正则漏了全角冒号「：」，导致很多电影的 release_date
抓错（跳过主上映日、抓成页面侧栏里别的电影的日期）。正则已修；这个脚本重抓详情页、
用修正后的正则更新 release_date。只动 release_date，其它字段不变。

用法:
  python run_backfill_release.py                  # 全部电影
  python run_backfill_release.py --cat chain      # 只 chain
  python run_backfill_release.py --cat chain indie
  python run_backfill_release.py --limit 50       # 本次最多 50 部（可分批续跑）
  python run_backfill_release.py --delay 1.0      # 每次请求基础延时（秒）
"""
import sys
import argparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from scraper.logging_setup import setup as _setup_logging
_setup_logging()

from pipeline.step1_eiga import backfill_release_dates


def main():
    parser = argparse.ArgumentParser(description="重抓电影上映日期，修复 release_date")
    parser.add_argument("--cat", nargs="*", default=None,
                        help="限定 category（如 chain indie）；省略 = 全部电影")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="每次请求基础延时（秒，默认 0.5）")
    parser.add_argument("--limit", type=int, default=0,
                        help="本次最多处理多少部（0 = 不限）")
    args = parser.parse_args()

    cats = tuple(args.cat) if args.cat else None
    scanned, fixed = backfill_release_dates(categories=cats, delay=args.delay, limit=args.limit)
    print(f"\n[OK] 扫描 {scanned} 部，修正 {fixed} 部 release_date")


if __name__ == "__main__":
    main()
