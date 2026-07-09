"""Import output/theaters_export.csv into the theaters table.

Upserts by theater_id (insert new rows, update name/chain/area/area_code/url
for existing ones, revive logically-deleted rows). Any active theater_id
present in the DB but missing from the CSV is logically deleted (delete_flg=1)
rather than hard-deleted, so screenings/showtimes history is preserved.

A `category` column in the CSV (from older exports) is ignored — chain/indie
is now derived from whether `chain` is non-empty.

Usage: python scripts/import_theaters_csv.py [path/to/theaters_export.csv]
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pipeline.step1_eiga import _get_db  # noqa: E402


def import_csv(csv_path: Path) -> None:
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))

    conn = _get_db()
    existing = {
        row["theater_id"]: dict(row)
        for row in conn.execute("SELECT * FROM theaters").fetchall()
    }

    csv_ids = set()
    added = updated = revived = 0
    for r in rows:
        tid = r["theater_id"]
        csv_ids.add(tid)
        fields = (r["name"], r["chain"] or None, r["area"], r["area_code"], r["url"])

        if tid not in existing:
            conn.execute(
                """INSERT INTO theaters (theater_id, name, chain, area, area_code, url, delete_flg)
                   VALUES (?, ?, ?, ?, ?, ?, 0)""",
                (tid, *fields),
            )
            added += 1
            continue

        row = existing[tid]
        if row["delete_flg"]:
            revived += 1
        elif (row["name"], row["chain"] or None, row["area"], row["area_code"], row["url"]) == fields:
            continue
        else:
            updated += 1
        conn.execute(
            """UPDATE theaters SET name=?, chain=?, area=?, area_code=?, url=?, delete_flg=0
               WHERE theater_id=?""",
            (*fields, tid),
        )

    removed = 0
    for tid, row in existing.items():
        if tid not in csv_ids and not row["delete_flg"]:
            conn.execute("UPDATE theaters SET delete_flg=1 WHERE theater_id=?", (tid,))
            removed += 1

    conn.commit()
    conn.close()
    print(f"Imported {len(rows)} rows from {csv_path}: "
          f"added={added} updated={updated} revived={revived} logically_deleted={removed}")


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output/theaters_export.csv")
    import_csv(path)
