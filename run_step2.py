"""Run Step 2: Douban matching for chain + indie movies."""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from scraper.logging_setup import setup as _setup_logging
_setup_logging()  # local=仅控制台；cloud=另写 logs/当天.log，只留 2 天

from pipeline.step2_douban import (
    match_movies,
    generate_step2_md,
)


def main():
    # Match movies (supports resume — already matched movies are skipped)
    results = match_movies(delay=2.0, resume=True)

    for cat in ("chain", "indie"):
        movies = results.get(cat, [])
        matched = [m for m in movies if m.get("douban_id")]
        print(f"\n{cat}: {len(matched)}/{len(movies)} matched")

    # cloud 环境只为小程序填库，不需要 MD 报告；local 才生成。
    from scraper import fetcher
    if fetcher.is_cloud():
        print("\n[cloud] 跳过 step2 MD 生成")
    else:
        md = generate_step2_md()
        print(f"\nMD report: {md}")


if __name__ == "__main__":
    main()
