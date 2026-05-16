#!/usr/bin/env python3
import sqlite3
import sys
from pathlib import Path

if len(sys.argv) < 3:
    print('Usage: python scripts/set_title_original.py MOVIE_ID "TITLE"')
    sys.exit(1)
mid = sys.argv[1]
title = sys.argv[2]

db = Path('data') / 'eiga.db'
if not db.exists():
    print('DB not found:', db)
    sys.exit(1)

conn = sqlite3.connect(str(db))
cur = conn.cursor()
cur.execute("UPDATE movies SET title_original=? WHERE movie_id=?", (title, mid))
conn.commit()
row = cur.execute("SELECT movie_id, title_jp, title_original FROM movies WHERE movie_id=?", (mid,)).fetchone()
print(row)
conn.close()