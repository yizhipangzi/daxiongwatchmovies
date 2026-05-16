#!/usr/bin/env python3
"""run_scraper.py — 大熊看点映 自动抓取脚本

用法:
  python run_scraper.py                   # 抓取本周电影，生成 Markdown 简报
  python run_scraper.py --demo            # 使用演示数据（不发起真实网络请求）
  python run_scraper.py --no-douban       # 跳过豆瓣评分抓取
  python run_scraper.py --output FILE     # 指定输出文件路径
  python run_scraper.py --issue N         # 手动指定期号
  python run_scraper.py --resume          # Cookie 更新后继续上次中断的抓取
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import yaml

# Ensure repo root is on sys.path regardless of how the script is invoked
sys.path.insert(0, str(Path(__file__).parent))

# Ensure stdout can handle CJK characters on Windows (cp932 terminals)
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from scraper.base import TheaterSchedule, MovieInfo, ScreeningInfo
from generator.briefing import generate_wechat_briefing_md
from pipeline.step1_eiga import register_theaters, scrape_movies
from pipeline.step2_douban import enrich_for_briefing

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("run_scraper")


# ── Demo / mock data ─────────────────────────────────────────────────────────

def _load_demo_data() -> list[TheaterSchedule]:
    """Return hard-coded demo schedules so the tool works without the internet."""
    from scraper.base import MovieInfo, ScreeningInfo, TheaterSchedule

    today = date.today()

    demo_movies = [
        MovieInfo(
            title_jp="哀れなるものたち",
            title_cn="可怜的东西",
            director="ヨルゴス・ランティモス",
            cast="エマ・ストーン / マーク・ラファロ",
            year=2023,
            genre="剧情 / 喜剧 / 奇幻",
            duration=141,
            douban_score=8.0,
            douban_votes=320000,
            douban_url="https://movie.douban.com/subject/35606724/",
            douban_short_reviews=[
                "视觉奇观与女性自我意识的完美融合，艾玛·斯通的表演令人叹服。",
                "兰斯莫斯的风格延续，但比《宠儿》更天马行空。",
                "看完这部片，我决定重新审视自己的人生。",
            ],
            is_new_release=False,
            screenings=[
                ScreeningInfo("TOHOシネマズ 新宿", show_date=today, start_time="13:00"),
                ScreeningInfo("TOHOシネマズ 日比谷", show_date=today, start_time="16:30"),
                ScreeningInfo("TOHOシネマズ 六本木ヒルズ", show_date=today + timedelta(1), start_time="18:00"),
            ],
        ),
        MovieInfo(
            title_jp="関心領域",
            title_cn="利益区域",
            director="ジョナサン・グレイザー",
            cast="クリスティアン・フリーデル / ザンドラ・ヒュラー",
            year=2023,
            genre="剧情 / 战争 / 历史",
            duration=105,
            douban_score=8.3,
            douban_votes=180000,
            douban_url="https://movie.douban.com/subject/35431622/",
            douban_short_reviews=[
                "用反高潮的方式呈现高潮——这才是最令人不安的恐怖。",
                "画面的美丽与内容的残酷形成极致对比。",
            ],
            is_new_release=True,
            screenings=[
                ScreeningInfo("シアター・イメージフォーラム", show_date=today, start_time="14:00"),
                ScreeningInfo("シアター・イメージフォーラム", show_date=today, start_time="17:30"),
                ScreeningInfo("シアター・イメージフォーラム", show_date=today + timedelta(2), start_time="19:00"),
            ],
        ),
        MovieInfo(
            title_jp="オッペンハイマー",
            title_cn="奥本海默",
            director="クリストファー・ノーラン",
            cast="キリアン・マーフィー / エミリー・ブラント",
            year=2023,
            genre="剧情 / 传记 / 历史",
            duration=180,
            douban_score=8.8,
            douban_votes=1500000,
            douban_url="https://movie.douban.com/subject/35557727/",
            douban_short_reviews=[
                "诺兰最成熟的作品，三个小时一气呵成。",
                "IMAX画面的震撼与原子弹爆炸的隐喻完美契合。",
                "基里安·墨菲的眼睛里装着整个宇宙。",
            ],
            is_new_release=False,
            screenings=[
                ScreeningInfo("TOHOシネマズ 日比谷", show_date=today, start_time="10:30"),
                ScreeningInfo("TOHOシネマズ 日比谷", show_date=today, start_time="15:00"),
                ScreeningInfo("ユナイテッド・シネマ 豊洲", show_date=today + timedelta(1), start_time="12:00"),
            ],
        ),
        MovieInfo(
            title_jp="ゴジラ-1.0",
            title_cn="哥斯拉-1.0",
            director="山崎貴",
            cast="神木隆之介 / 浜辺美波",
            year=2023,
            genre="科幻 / 动作 / 冒险",
            duration=125,
            douban_score=7.9,
            douban_votes=450000,
            douban_url="https://movie.douban.com/subject/35743639/",
            douban_short_reviews=[
                "日本战败后的废墟与哥斯拉相遇，国内能看到真的太好了。",
                "特效惊艳，情感也在线，难得的佳作。",
            ],
            is_new_release=False,
            screenings=[
                ScreeningInfo("TOHOシネマズ 新宿", show_date=today, start_time="11:00"),
                ScreeningInfo("TOHOシネマズ 新宿", show_date=today, start_time="14:00"),
                ScreeningInfo("TOHOシネマズ 新宿", show_date=today, start_time="17:00"),
                ScreeningInfo("TOHOシネマズ 渋谷", show_date=today + timedelta(1), start_time="13:00"),
            ],
        ),
        MovieInfo(
            title_jp="ペルフェクト・デイズ",
            title_cn="完美的日子",
            director="ヴィム・ヴェンダース",
            cast="役所広司",
            year=2023,
            genre="剧情",
            duration=123,
            douban_score=8.5,
            douban_votes=260000,
            douban_url="https://movie.douban.com/subject/35906661/",
            douban_short_reviews=[
                "东京的厕所与人的尊严——维姆·文德斯拍出了一部东京情书。",
                "役所广司的沉默比任何台词都有力量。",
                "看完想去东京街头骑自行车，晒晒树影。",
            ],
            is_new_release=False,
            screenings=[
                ScreeningInfo("Uplink 吉祥寺", show_date=today, start_time="15:30"),
                ScreeningInfo("ポレポレ東中野", show_date=today + timedelta(3), start_time="17:00"),
            ],
        ),
        MovieInfo(
            title_jp="枯れ葉",
            title_cn="枯叶",
            director="アキ・カウリスマキ",
            cast="アルマ・ポウスティ / ユッシ・ヴァタネン",
            year=2023,
            genre="剧情 / 爱情",
            duration=81,
            douban_score=8.2,
            douban_votes=85000,
            douban_url="https://movie.douban.com/subject/35908804/",
            douban_short_reviews=[
                "卡里斯马基的极简主义依旧令人动容，芬兰式的忧郁爱情。",
                "81分钟，没有一秒多余。",
            ],
            is_new_release=True,
            screenings=[
                ScreeningInfo("シネマヴェーラ渋谷", show_date=today, start_time="13:00"),
                ScreeningInfo("シネマヴェーラ渋谷", show_date=today + timedelta(2), start_time="16:00"),
            ],
        ),
        MovieInfo(
            title_jp="フェラーリ",
            title_cn="法拉利",
            director="マイケル・マン",
            cast="アダム・ドライバー / ペネロペ・クルス",
            year=2023,
            genre="剧情 / 传记 / 运动",
            duration=130,
            douban_score=7.1,
            douban_votes=70000,
            douban_url="https://movie.douban.com/subject/26930490/",
            douban_short_reviews=[
                "迈克尔·曼的镜头下，赛车与商战同样惊心动魄。",
            ],
            is_new_release=True,
            screenings=[
                ScreeningInfo("TOHOシネマズ 六本木ヒルズ", show_date=today, start_time="18:30"),
                ScreeningInfo("ユナイテッド・シネマ 豊洲", show_date=today + timedelta(1), start_time="20:00"),
            ],
        ),
    ]

    sched = TheaterSchedule(source="DEMO", theater_name="全东京")
    sched.movies = demo_movies
    for m in demo_movies:
        m.screening_count = len(m.screenings)
    return [sched]


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load_config(path: str = "config.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        # Try the example file
        example = config_path.parent / (config_path.name + ".example")
        if example.exists():
            logger.warning(
                "config.yaml not found; using config.yaml.example. "
                "Copy it to config.yaml and fill in your credentials."
            )
            config_path = example
        else:
            logger.warning("No config file found; using default empty config.")
            return {}
    with config_path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _issue_number(output_dir: Path) -> int:
    """Auto-detect next issue number from existing output files."""
    existing = list(output_dir.glob("briefing_*.md"))
    if not existing:
        return 1
    nums = []
    for p in existing:
        m = re.search(r"briefing_(\d+)_", p.name)
        if m:
            nums.append(int(m.group(1)))
    return (max(nums) + 1) if nums else 1


# ── DB fallback helpers ───────────────────────────────────────────────────────

def _load_movies_from_db() -> list[MovieInfo]:
    """Load chain/indie movies from DB when step1 skip path returns empty."""
    import sqlite3
    db = Path("data/eiga.db")
    if not db.exists():
        return []
    try:
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM movies WHERE category IN ('chain','indie') ORDER BY category, rank"
        ).fetchall()
        conn.close()
        movies = []
        for r in rows:
            mi = MovieInfo(
                title_jp=r["title_jp"] or "",
                title_original=r["title_original"] or "",
                country=r["country"] or "",
                director=r["director"] or "",
                year=r["year"] or 0,
                duration=r["duration"] or 0,
            )
            movies.append(mi)
        return movies
    except Exception as exc:
        logger.warning("Failed to load movies from DB: %s", exc)
        return []


def _apply_step2_results(movies: list[MovieInfo], results: dict) -> None:
    """Populate douban fields in MovieInfo objects from step2 match results."""
    by_title: dict[str, dict] = {}
    for cat_movies in results.values():
        for m in cat_movies:
            if m.get("title_jp") and m.get("douban_id"):
                by_title[m["title_jp"]] = m
    for mi in movies:
        m = by_title.get(mi.title_jp)
        if not m:
            continue
        mi.title_cn = m.get("title_cn") or ""
        mi.douban_id = str(m.get("douban_id") or "")
        mi.douban_url = m.get("douban_url") or (
            f"https://movie.douban.com/subject/{m['douban_id']}/" if m.get("douban_id") else ""
        )
        try:
            mi.douban_score = float(m.get("douban_score") or 0)
        except (TypeError, ValueError):
            mi.douban_score = 0.0
        try:
            mi.douban_votes = int(m.get("douban_votes") or 0)
        except (TypeError, ValueError):
            mi.douban_votes = 0


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="大熊看点映 — 电影简报自动抓取")
    parser.add_argument("--demo", action="store_true",
                        help="使用演示数据，不发起真实网络请求")
    parser.add_argument("--no-douban", action="store_true",
                        help="跳过豆瓣评分抓取")
    parser.add_argument("--no-scan", action="store_true",
                        help="不进行网络扫描，只从本地 DB 生成 MD（快速预览）")
    parser.add_argument("--output", default="",
                        help="输出文件路径（默认: output/briefing_N_YYYY-MM-DD.md）")
    parser.add_argument("--issue", type=int, default=0,
                        help="手动指定期号（默认: 自动检测）")
    parser.add_argument("--config", default="config.yaml",
                        help="配置文件路径")
    parser.add_argument("--resume", action="store_true",
                        help="从上次中断处继续豆瓣抓取（Cookie 更新后使用）")
    args = parser.parse_args()

    config = _load_config(args.config)

    output_dir = Path(config.get("generator", {}).get("output_dir", "output"))
    output_dir.mkdir(exist_ok=True)

    issue_number = args.issue or _issue_number(output_dir)
    today = date.today()

    # ── Scrape ───────────────────────────────────────────────────────────────
    if args.no_scan:
        # Run quick MD generation from local DB without scraping
        logger.info("--no-scan: 直接从本地 DB 生成 MD（不抓取）")
        # Use the helper script to generate step1/step2/step3
        try:
            import subprocess, sys
            subprocess.run([sys.executable, "scripts/generate_md_no_scan.py"], check=True)
        except Exception as exc:
            logger.error("生成 MD 失败: %s", exc)
        return

    if args.demo:
        logger.info("=== 演示模式：使用本地演示数据 ===")
        schedules = _load_demo_data()
    else:
        # Use eiga.com pipeline only: register theaters and scrape now-showing movies
        theaters = register_theaters(delay=0.3)
        results = scrape_movies(delay=0.3)

        # Convert scraped results into MovieInfo list
        all_movies: list[MovieInfo] = []
        for cat in ("chain", "indie", "other"):
            for m in results.get(cat, []):
                mi = MovieInfo(
                    title_jp=m.get("title_jp", ""),
                    title_original=m.get("title_original", "") or "",
                    country=m.get("country", "") or "",
                    director=m.get("director", "") or "",
                    year=m.get("year") or 0,
                    duration=m.get("duration") or 0,
                )
                # screenings may be absent; keep theater names if present
                for tn in m.get("_theater_names", []) if isinstance(m.get("_theater_names"), list) else []:
                    mi.add_screening(ScreeningInfo(theater_name=tn))
                all_movies.append(mi)

        # Step1 skip path may return empty if today's snapshot is missing.
        # Fall back to loading all movies from the DB directly.
        if not all_movies:
            logger.info("Step1 returned empty results; loading movies from DB directly")
            all_movies = _load_movies_from_db()

        # Run step2 unconditionally — it reads the DB directly and is independent
        # of whether step1 scraped or skipped.
        step2_results = None
        if not args.no_douban:
            from pipeline.step2_douban import match_movies, generate_step2_md
            step2_results = match_movies(categories=("chain", "indie"), delay=5.0, resume=True)
            generate_step2_md(step2_results)

        if not all_movies:
            logger.warning("未抓取到任何排期数据。使用演示数据代替。")
            schedules = _load_demo_data()
        else:
            if step2_results:
                _apply_step2_results(all_movies, step2_results)
            # Compute screening period for each movie
            for movie in all_movies:
                movie.compute_screening_period()
            merged = TheaterSchedule(source="merged", theater_name="all")
            merged.movies = all_movies
            schedules = [merged]

    # ── Generate ─────────────────────────────────────────────────────────────
    # WeChat 公众号 briefing: 院線映画 + 小众映画 (top 5 each, douban_score > 0).
    # Enrich first so douban_details has director/cast/genre/want/watched/reviews
    # with reviewer IDs, then render the final MD.
    logger.info("正在抽取豆瓣详细信息 (top 5 院线 + top 5 小众)...")
    enrich_for_briefing(top_n=5, delay=2.0)

    logger.info("正在生成简报 Markdown...")
    if args.output:
        out_path = Path(args.output)
    else:
        out_path = output_dir / f"briefing_{issue_number:03d}_{today.isoformat()}.md"
    generate_wechat_briefing_md(top_n=5, output_path=out_path)

    logger.info("简报已保存: %s", out_path)
    print(f"\n[OK] 简报已生成: {out_path}\n")


if __name__ == "__main__":
    main()
