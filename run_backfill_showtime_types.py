"""回填 showtimes.movie_type（场次格式标签：字幕/吹替/IMAX/ScreenX 等）。

movie_type 列是后加的，老库里已有的场次行该列为 NULL，小程序里 movieType 就空了。
step1 主循环有「今天已跑」防抓守卫，当天不会重抓，所以用这个独立脚本一次性回填。

只挑 movie_type 仍为 NULL 的 (movie, theater) 对，可反复运行 / 分批续跑。
跑完后再跑 run_step4.py 重新生成并上传 JSON。

用法:
  python run_backfill_showtime_types.py              # 全部待回填
  python run_backfill_showtime_types.py --limit 100  # 本次最多 100 对（可分批续跑）
  python run_backfill_showtime_types.py --delay 1.0  # 每次请求基础延时（秒）
"""
import sys
import argparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from scraper.logging_setup import setup as _setup_logging
_setup_logging()

from pipeline.step1_eiga import backfill_showtime_types


def main():
    parser = argparse.ArgumentParser(description="回填场次格式标签 movie_type")
    parser.add_argument("--delay", type=float, default=0.5,
                        help="每次请求基础延时（秒，默认 0.5）")
    parser.add_argument("--limit", type=int, default=0,
                        help="本次最多处理多少对 (movie,theater)（0 = 不限）")
    args = parser.parse_args()

    scanned, updated = backfill_showtime_types(delay=args.delay, limit=args.limit)
    print(f"\n[OK] 扫描 {scanned} 对 (movie,theater)，更新 {updated} 行 movie_type")


if __name__ == "__main__":
    main()
