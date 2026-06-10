"""Run Step 1: register theaters + scrape now-showing movies from eiga.com only."""
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from scraper.logging_setup import setup as _setup_logging
_setup_logging()  # local=仅控制台；cloud=另写 logs/当天.log，只留 2 天

from pipeline.step1_eiga import register_theaters, scrape_movies, generate_step1_md

def main():
    # 操作1: 映画館登記 (全東京)
    theaters = register_theaters(delay=0.3)
    chain_t = [t for t in theaters if t["category"] == "chain"]
    indie_t = [t for t in theaters if t["category"] == "indie"]
    print(f"\n=== Registered {len(theaters)} theaters (chain={len(chain_t)}, indie={len(indie_t)}) ===\n")
    for t in chain_t:
        print(f"  🎬 [{t['area']}] {t['name']} ({t['chain']})")
    for t in indie_t:
        print(f"  🎭 [{t['area']}] {t['name']}")

    # 操作2: 上映中映画抓取 + 分類
    results = scrape_movies(delay=0.3)
    print(f"\n=== chain={len(results['chain'])}, indie={len(results['indie'])}, other={len(results['other'])} ===\n")

    # MD output
    md = generate_step1_md(results, theaters)
    print(f"\nMD report: {md}")


if __name__ == "__main__":
    main()
