#!/usr/bin/env python3
"""Export / import Douban match data as CSV.

Export: all eiga.com movies with Douban match info and skip-list status.
Import: update or clear Douban URL for each row.
  - Non-empty douban_url  → upsert into douban_matches (verified=1), remove from skip list
  - Empty douban_url      → delete from douban_matches (triggers re-search next run)

Usage:
    python scripts/douban_csv.py export [output.csv]
    python scripts/douban_csv.py import  input.csv
"""
import csv
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path("data/eiga.db")

EXPORT_FIELDS = [
    "movie_id", "category", "rank", "title_jp", "title_original",
    "year", "country",
    "douban_id", "title_cn", "douban_url",
    "douban_score", "douban_votes", "verified", "search_attempts",
    "in_skip_list", "skip_reason",
]


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def export_csv(output_path: str = "douban_export.csv") -> int:
    conn = _get_db()
    rows = conn.execute("""
        SELECT
            m.movie_id, m.category, m.rank, m.title_jp, m.title_original,
            m.year, m.country,
            d.douban_id, d.title_cn, d.douban_url,
            d.douban_score, d.douban_votes, d.verified,
            COALESCE(d.search_attempts, 0) AS search_attempts,
            CASE WHEN s.movie_id IS NOT NULL THEN 1 ELSE 0 END AS in_skip_list,
            COALESCE(s.reason, '') AS skip_reason
        FROM movies m
        LEFT JOIN douban_matches d ON m.movie_id = d.movie_id
        LEFT JOIN douban_skip_list s ON m.movie_id = s.movie_id
        ORDER BY m.category, m.rank
    """).fetchall()
    conn.close()

    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=EXPORT_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row[k] for k in EXPORT_FIELDS})

    print(f"Exported {len(rows)} movies → {output_path}")
    return len(rows)


def import_csv(input_path: str) -> tuple[int, int]:
    """Read CSV and apply douban_url changes.

    Returns (updated, cleared).
    """
    conn = _get_db()
    now = datetime.now().isoformat(timespec="seconds")
    updated = 0
    cleared = 0
    skipped = 0

    with open(input_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            movie_id = (row.get("movie_id") or "").strip()
            if not movie_id:
                continue

            douban_url = (row.get("douban_url") or "").strip()

            if douban_url:
                m = re.search(r"subject/(\d+)", douban_url)
                if not m:
                    print(f"  SKIP {movie_id}: cannot parse douban_id from {douban_url!r}")
                    skipped += 1
                    continue
                douban_id = m.group(1)
                title_cn = (row.get("title_cn") or "").strip()
                canonical_url = f"https://movie.douban.com/subject/{douban_id}/"

                conn.execute("""
                    INSERT INTO douban_matches
                        (movie_id, douban_id, title_cn, douban_url, verified, search_attempts, matched_at)
                    VALUES (?, ?, ?, ?, 1, 0, ?)
                    ON CONFLICT(movie_id) DO UPDATE SET
                        douban_id       = excluded.douban_id,
                        title_cn        = excluded.title_cn,
                        douban_url      = excluded.douban_url,
                        verified        = 1,
                        search_attempts = 0,
                        matched_at      = excluded.matched_at
                """, (movie_id, douban_id, title_cn, canonical_url, now))

                conn.execute("DELETE FROM douban_skip_list WHERE movie_id = ?", (movie_id,))
                updated += 1

            else:
                # Empty URL → clear match so step2 will re-search next run
                conn.execute("DELETE FROM douban_matches WHERE movie_id = ?", (movie_id,))
                cleared += 1

    conn.commit()
    conn.close()
    print(f"Import done: {updated} updated, {cleared} cleared, {skipped} skipped")
    return updated, cleared


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    cmd = sys.argv[1].lower()
    if cmd == "export":
        out = sys.argv[2] if len(sys.argv) > 2 else "douban_export.csv"
        export_csv(out)
    elif cmd == "import":
        if len(sys.argv) < 3:
            print("Usage: python scripts/douban_csv.py import input.csv")
            sys.exit(1)
        import_csv(sys.argv[2])
    else:
        print(f"Unknown command: {cmd!r}. Use 'export' or 'import'.")
        sys.exit(1)
