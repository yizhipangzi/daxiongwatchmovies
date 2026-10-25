#!/usr/bin/env python3
"""一次性清理 douban_matches_history / imdb_score_history 里的冗余行.

修复前（见 pipeline/step2_douban.py）这两张表是「每天一行」，不管分数有没有变化
都写；而且抓取失败/撞到豆瓣验证页时 score=0 也被当成「今天的真实快照」写了进
去，混进了一堆假的"分数跌到 0"噪声。修复后（见 _history_unchanged）新数据只
在真的变化时才写一行，但历史上已经写进去的冗余行还在表里——这个脚本把它们
删掉，规则和 pipeline/step4_export.py 的 _score_trend_points() 完全一致：

  - score<=0 的行：视为抓取失败/未解析到评分的噪声，直接删（不是"真的跌到 0"）。
  - 同一部电影里，分数和前一条"保留下来的"行相同的，删（只留每次真变化的那一行）。
  - 每部电影永远保留第一条"有效"（score>0）行作为基线。

用法:
    python scripts/cleanup_score_history.py            # 先看看会删多少行（dry-run）
    python scripts/cleanup_score_history.py --apply     # 真正执行删除（自动先备份整个 db 文件）

幂等：删完之后再跑一次，两张表都应该是 0 行可删（因为新代码已经不再写冗余行）。
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

DB_PATH = Path("data") / "eiga.db"

# (表名, 分数列名)：两张表结构同构（movie_id, ..., <score_col>, ..., snapshot_at）。
TABLES = [
    ("douban_matches_history", "douban_score"),
    ("imdb_score_history", "imdb_score"),
]


def _rows_to_delete(conn: sqlite3.Connection, table: str, score_col: str) -> list[tuple]:
    """返回该表里要删的 (movie_id, snapshot_at) 主键对列表。"""
    rows = conn.execute(
        f"SELECT movie_id, {score_col} AS score, snapshot_at FROM {table} "
        f"ORDER BY movie_id, snapshot_at"
    ).fetchall()

    to_delete: list[tuple] = []
    prev_movie = None
    prev_score = None
    for movie_id, score, snapshot_at in rows:
        if movie_id != prev_movie:
            prev_movie = movie_id
            prev_score = None  # 换了一部电影，基线重新开始

        if not score or score <= 0:
            # 抓取失败/未解析到评分的噪声 —— 不参与基线比较，直接删。
            to_delete.append((movie_id, snapshot_at))
            continue

        if prev_score is not None and score == prev_score:
            # 和上一条"保留下来的"真实分数相同 —— 冗余，删。
            to_delete.append((movie_id, snapshot_at))
            continue

        # 保留：这是第一条有效行，或者分数真的变了。
        prev_score = score

    return to_delete


def main() -> None:
    apply = "--apply" in sys.argv
    if not DB_PATH.exists():
        print(f"DB not found: {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB_PATH))
    try:
        plan: dict[str, list[tuple]] = {}
        for table, score_col in TABLES:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            if not exists:
                print(f"{table}: 表不存在，跳过")
                continue
            total = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            dele = _rows_to_delete(conn, table, score_col)
            plan[table] = dele
            print(f"{table}: 共 {total} 行，计划删除 {len(dele)} 行，"
                  f"剩余 {total - len(dele)} 行")

        if not apply:
            print("\n(dry-run，没有真正删除。确认无误后加 --apply 执行)")
            return

        if not any(plan.values()):
            print("\n没有需要删除的行，无需执行。")
            return

        # 先备份整个 db 文件再动手，万一规则理解有误还能恢复。
        backup = DB_PATH.with_name(
            f"{DB_PATH.stem}.before-cleanup-{datetime.now():%Y%m%d%H%M%S}{DB_PATH.suffix}"
        )
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # 备份前把 WAL 合并进主文件
        shutil.copy2(DB_PATH, backup)
        print(f"\n已备份原库到: {backup}")

        for table, _ in TABLES:
            dele = plan.get(table) or []
            if not dele:
                continue
            conn.executemany(
                f"DELETE FROM {table} WHERE movie_id=? AND snapshot_at=?", dele
            )
        conn.commit()
        print("已执行删除并提交。")

        for table, _ in TABLES:
            if table in plan:
                remaining = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                print(f"{table}: 清理后剩余 {remaining} 行")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
