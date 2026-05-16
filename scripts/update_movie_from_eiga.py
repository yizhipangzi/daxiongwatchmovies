#!/usr/bin/env python3
"""Update movies table from eiga.com detail pages.

Usage:
  python scripts/update_movie_from_eiga.py 103356
  python scripts/update_movie_from_eiga.py --all   # update all movies in DB

This script only updates the DB, it does not generate MD.
"""
from pathlib import Path
import sys
import sqlite3
import argparse

# ensure repo root on path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from pipeline.step1_eiga import _scrape_movie_detail, DB_PATH


def update_movie(conn: sqlite3.Connection, movie_id: str):
    detail = _scrape_movie_detail(movie_id, delay=0.5)
    if not detail:
        print(f"No detail fetched for movie_id={movie_id}")
        return False
    # Prepare values
    title_original = detail.get("title_original")
    country = detail.get("country")
    year = detail.get("year")
    duration = detail.get("duration")
    release_date = detail.get("release_date")

    # Check if movie exists
    cur = conn.execute("SELECT movie_id FROM movies WHERE movie_id=?", (movie_id,)).fetchone()
    if cur:
        conn.execute(
            "UPDATE movies SET title_original=?, country=?, year=?, duration=?, release_date=? WHERE movie_id=?",
            (title_original, country, year, duration, release_date, movie_id)
        )
        conn.commit()
        print(f"Updated movie {movie_id}: title_original={title_original!r}")
    else:
        # insert minimal record
        conn.execute(
            "INSERT INTO movies (movie_id, title_original, country, year, duration, release_date) VALUES (?,?,?,?,?,?)",
            (movie_id, title_original, country, year, duration, release_date)
        )
        conn.commit()
        print(f"Inserted movie {movie_id} with title_original={title_original!r}")
    return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("movie_ids", nargs="*", help="movie_id(s) to update")
    parser.add_argument("--all", action="store_true", help="Update all movies in DB")
    args = parser.parse_args()

    db = Path("data") / "eiga.db"
    if not db.exists():
        print("DB not found:", db)
        sys.exit(1)

    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    ids = []
    if args.all:
        rows = conn.execute("SELECT movie_id FROM movies").fetchall()
        ids = [r[0] for r in rows]
    else:
        if not args.movie_ids:
            parser.error("Provide movie_id(s) or --all")
        ids = args.movie_ids

    for mid in ids:
        try:
            update_movie(conn, mid)
        except Exception as e:
            print(f"Error updating {mid}: {e}")

    conn.close()

if __name__ == '__main__':
    main()
