"""Run Step 3: WeChat 公众号 briefing.

Fetches detailed Douban info (director, cast, genre, watched/want counts,
short reviews with reviewer IDs) for the top movies of each category, then
renders the final WeChat-ready briefing MD.
"""
import sys
import logging

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from pipeline.step2_douban import enrich_for_briefing
from generator.briefing import generate_wechat_briefing_md


def main():
    top_n = 5
    enriched = enrich_for_briefing(top_n=top_n, delay=2.0)
    print(f"\nBriefing enrich: {enriched} movies")

    md = generate_wechat_briefing_md(top_n=top_n)
    print(f"\nBriefing MD: {md}")


if __name__ == "__main__":
    main()
