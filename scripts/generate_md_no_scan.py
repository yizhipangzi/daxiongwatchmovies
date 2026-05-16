#!/usr/bin/env python3
"""Generate Step1/Step2/Step3 MD from local DB without performing any scraping.

Usage: python scripts/generate_md_no_scan.py

Produces output/step1_YYYY-MM-DD.md, output/step2_YYYY-MM-DD.md, output/step3_YYYY-MM-DD.md
"""
from pathlib import Path
import sqlite3
from datetime import datetime
import sys

# Ensure repo root is on sys.path so pipeline/ and generator/ packages can be imported
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = Path("output")
OUT.mkdir(exist_ok=True)
DB = Path("data") / "eiga.db"
if not DB.exists():
    print("DB not found:", DB)
    raise SystemExit(1)

conn = sqlite3.connect(str(DB))
conn.row_factory = sqlite3.Row

# Determine latest snapshot date
row = conn.execute("SELECT snapshot_date FROM movie_snapshots ORDER BY snapshot_date DESC LIMIT 1").fetchone()
if not row:
    print("No movie snapshots found in DB. Nothing to generate.")
    conn.close()
    raise SystemExit(1)

snapshot_date = row[0]

def generate_step1():
    rows = conn.execute(
        "SELECT s.rank, s.movie_id, s.category, m.title_jp FROM movie_snapshots s JOIN movies m ON s.movie_id=m.movie_id WHERE s.snapshot_date=? ORDER BY s.rank",
        (snapshot_date,)
    ).fetchall()
    lines = [f"# Step1: 片单快照 ({snapshot_date})", "", "| 排名 | Movie ID | 标题(JP) | 类别 |", "|---|---:|---|---|"]
    for r in rows:
        lines.append(f"| {r['rank']} | {r['movie_id']} | {r['title_jp'].replace('|','')} | {r['category']} |")
    p = OUT / f"step1_{snapshot_date}.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    print("Step1 MD generated:", p)


def generate_step2():
    try:
        from pipeline.step2_douban import _load_results_from_db, generate_step2_md
    except Exception as e:
        print("Failed to import step2 generator:", e)
        return
    results = _load_results_from_db()
    p = generate_step2_md(results)
    print("Step2 MD generated:", p)


def generate_step3():
    try:
        from generator.briefing import generate_wechat_briefing_md
    except Exception as e:
        print("Failed to import generate_wechat_briefing_md:", e)
        return
    p = generate_wechat_briefing_md(top_n=5)
    print("Step3 (briefing) MD generated:", p)


if __name__ == '__main__':
    generate_step1()
    generate_step2()
    generate_step3()
    conn.close()
