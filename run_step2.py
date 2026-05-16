"""Run Step 2: Douban matching for chain + indie movies."""
import sys
import logging

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from pipeline.step2_douban import (
    match_movies,
    enrich_new_movie_ratings,
    generate_step2_md,
)


def main():
    # Match movies (supports resume — already matched movies are skipped)
    results = match_movies(delay=2.0, resume=True)

    for cat in ("chain", "indie"):
        movies = results.get(cat, [])
        matched = [m for m in movies if m.get("douban_id")]
        print(f"\n{cat}: {len(matched)}/{len(movies)} matched")

    # For movies with a Douban entry but no aggregated score yet, fetch
    # short-review star ratings and store the average as 新片推荐度.
    enriched = enrich_new_movie_ratings(delay=2.0)
    print(f"\n新片推荐度 enriched: {enriched} movies")

    # Generate MD (load fresh from DB so new_movie_rating is included)
    md = generate_step2_md()
    print(f"\nMD report: {md}")


if __name__ == "__main__":
    main()
