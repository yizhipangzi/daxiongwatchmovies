import sys
import logging
from pathlib import Path
import argparse
from datetime import date, datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

from pipeline.step2_douban import match_movies, generate_step2_md, add_movie_to_skip_list
from pipeline.step2_douban import _get_db, _ensure_douban_table


def run_step2(categories=("chain", "indie"), delay=2.0, output_md=True):
    """Run step2 Douban matching (single pass)."""
    results = match_movies(categories=categories, delay=delay, resume=True)

    unmatched = sum(1 for cat in results for m in results[cat] if not m.get("douban_id"))
    print(f"Step2 done: unmatched={unmatched}")

    if output_md:
        md = generate_step2_md(results)
        print(f"MD saved: {md}")
    return results


def cmd_skip_add(args):
    movie_id = args.movie_id
    reason = args.reason or "manual-skip"
    conn = _get_db()
    _ensure_douban_table(conn)
    row = conn.execute("SELECT title_jp FROM movies WHERE movie_id=?", (movie_id,)).fetchone()
    title = row[0] if row else None
    now = datetime.now().isoformat(timespec="seconds")
    conn.execute("INSERT OR REPLACE INTO douban_skip_list (movie_id, reason, noted_at) VALUES (?,?,?)",
                 (movie_id, reason, now))
    conn.commit()
    conn.close()
    print(f"Added movie_id {movie_id} to skip list (reason='{reason}')" + (f" title='{title}'" if title else ""))


def cmd_skip_remove(args):
    movie_id = args.movie_id
    conn = _get_db()
    _ensure_douban_table(conn)
    cur = conn.execute("DELETE FROM douban_skip_list WHERE movie_id=?", (movie_id,))
    conn.commit()
    conn.close()
    if cur.rowcount:
        print(f"Removed movie_id {movie_id} from skip list")
    else:
        print(f"movie_id {movie_id} not found in skip list")


def cmd_skip_list(args):
    conn = _get_db()
    _ensure_douban_table(conn)
    rows = conn.execute(
        "SELECT s.movie_id, s.reason, s.noted_at, m.title_jp "
        "FROM douban_skip_list s LEFT JOIN movies m ON s.movie_id = m.movie_id "
        "ORDER BY s.noted_at DESC"
    ).fetchall()
    conn.close()
    if not rows:
        print("Skip list is empty")
        return
    for r in rows:
        mid, reason, noted_at, title = r
        print(f"{mid}\t{noted_at}\t{reason}\t{title or '-'}")


_NULL_SENTINELS = {"null", "none", "-", ""}


def cmd_manual_set(args):
    from pipeline.step2_douban import manual_set_match, add_movie_to_skip_list

    if (args.douban_id or "").strip().lower() in _NULL_SENTINELS:
        # "this movie has no Douban entry" — drop any wrong match and put it
        # on the skip list so future runs stop trying to re-match it.
        movie_id = args.movie_id
        conn = _get_db()
        _ensure_douban_table(conn)
        row = conn.execute("SELECT title_jp FROM movies WHERE movie_id=?", (movie_id,)).fetchone()
        if not row:
            conn.close()
            print(f"❌ movie_id '{movie_id}' not found in movies table")
            sys.exit(1)
        title = row[0]
        deleted = conn.execute(
            "DELETE FROM douban_matches WHERE movie_id=?", (movie_id,)
        ).rowcount
        conn.commit()
        conn.close()
        reason = args.reason or "no-douban-entry"
        add_movie_to_skip_list(movie_id, reason=reason)
        print(f"✅ Marked {movie_id} ({title}) as having no Douban entry")
        print(f"   cleared {deleted} match row | added to skip list (reason='{reason}')")
        return

    try:
        r = manual_set_match(args.movie_id, args.douban_id, delay=args.delay)
        print(f"✅ {r['title_jp']}  →  {r['title_cn']}  (id={r['douban_id']})")
        print(f"   score={r['score']}  votes={r['votes']}")
        print(f"   year_ok={r['year_ok']}  country_ok={r['country_ok']}")
    except Exception as exc:
        print(f"❌ {exc}")
        sys.exit(1)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Step2 (Douban) runner and utilities")
    sub = parser.add_subparsers(dest="cmd")

    p_run = sub.add_parser("run", help="Run step2 Douban matching")
    p_run.add_argument("--no-md", action="store_true", help="Do not regenerate MD")
    p_run.add_argument("--delay", type=float, default=2.0, help="Delay between requests")

    p_ms = sub.add_parser(
        "manual-set",
        help="Manually match a movie to a Douban subject ID, or pass 'null' if Douban has no entry",
    )
    p_ms.add_argument("movie_id", help="movie_id from the movies table")
    p_ms.add_argument(
        "douban_id",
        help="Douban subject ID, or 'null'/'none'/'-' to mark 'no Douban entry'",
    )
    p_ms.add_argument("--delay", type=float, default=1.5, help="Request delay (default 1.5s)")
    p_ms.add_argument(
        "--reason",
        help="Skip-list reason when douban_id is null (default: no-douban-entry)",
    )

    p_skip = sub.add_parser("skip", help="Manage douban skip list")
    skip_sub = p_skip.add_subparsers(dest="op")

    p_add = skip_sub.add_parser("add", help="Add movie_id to skip list")
    p_add.add_argument("movie_id", help="movie_id to skip")
    p_add.add_argument("--reason", help="Reason for skipping (optional)")

    p_rm = skip_sub.add_parser("remove", help="Remove movie_id from skip list")
    p_rm.add_argument("movie_id", help="movie_id to remove from skip list")

    skip_sub.add_parser("list", help="List skip entries")

    args = parser.parse_args(argv)
    if args.cmd is None:
        return run_step2()

    if args.cmd == "run":
        return run_step2(output_md=not args.no_md, delay=args.delay)
    if args.cmd == "manual-set":
        return cmd_manual_set(args)
    if args.cmd == "skip":
        if args.op == "add":
            return cmd_skip_add(args)
        if args.op == "remove":
            return cmd_skip_remove(args)
        if args.op == "list":
            return cmd_skip_list(args)
        parser.error("skip requires one of: add, remove, list")


if __name__ == "__main__":
    main()
