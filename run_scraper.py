#!/usr/bin/env python3
"""run_scraper.py — 大雄看点映 自动抓取脚本

用法:
  python run_scraper.py                   # 抓取本周电影，生成 Markdown 简报
  python run_scraper.py --demo            # 使用演示数据（不发起真实网络请求）
  python run_scraper.py --no-douban       # 跳过豆瓣评分抓取
  python run_scraper.py --output FILE     # 指定输出文件路径
  python run_scraper.py --issue N         # 手动指定期号
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

from scraper.toho import scrape_all_toho
from scraper.united import scrape_all_united
from scraper.independent import scrape_all_independent
from scraper.douban import enrich_all_movies
from scraper.base import TheaterSchedule
from generator.briefing import generate_briefing, merge_schedules

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


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="大雄看点映 — 电影简报自动抓取")
    parser.add_argument("--demo", action="store_true",
                        help="使用演示数据，不发起真实网络请求")
    parser.add_argument("--no-douban", action="store_true",
                        help="跳过豆瓣评分抓取")
    parser.add_argument("--output", default="",
                        help="输出文件路径（默认: output/briefing_N_YYYY-MM-DD.md）")
    parser.add_argument("--issue", type=int, default=0,
                        help="手动指定期号（默认: 自动检测）")
    parser.add_argument("--config", default="config.yaml",
                        help="配置文件路径")
    parser.add_argument("--json", action="store_true",
                        help="同时保存结构化 JSON 数据")
    args = parser.parse_args()

    config = _load_config(args.config)

    output_dir = Path(config.get("generator", {}).get("output_dir", "output"))
    output_dir.mkdir(exist_ok=True)

    issue_number = args.issue or _issue_number(output_dir)
    today = date.today()

    # ── Scrape ───────────────────────────────────────────────────────────────
    if args.demo:
        logger.info("=== 演示模式：使用本地演示数据 ===")
        schedules = _load_demo_data()
    else:
        schedules: list[TheaterSchedule] = []
        theaters_cfg = config.get("theaters", {})

        if theaters_cfg.get("toho", {}).get("enabled"):
            logger.info("正在抓取 TOHO 影院排期...")
            schedules.extend(scrape_all_toho(theaters_cfg["toho"]))

        if theaters_cfg.get("united", {}).get("enabled"):
            logger.info("正在抓取 United Cinemas 排期...")
            schedules.extend(scrape_all_united(theaters_cfg["united"]))

        if theaters_cfg.get("independent", {}).get("enabled"):
            logger.info("正在抓取独立影院排期...")
            schedules.extend(scrape_all_independent(theaters_cfg["independent"]))

        if not schedules:
            logger.warning("未抓取到任何排期数据。使用演示数据代替。")
            schedules = _load_demo_data()

        # ── Enrich with Douban ────────────────────────────────────────────
        if not args.no_douban:
            all_movies = merge_schedules(schedules)
            logger.info("正在从豆瓣获取评分数据（共 %d 部电影）...", len(all_movies))
            enrich_all_movies(all_movies, config.get("douban", {}))
            # Replace schedules with a single merged-and-enriched schedule
            merged = TheaterSchedule(source="merged", theater_name="all")
            merged.movies = all_movies
            schedules = [merged]

    # ── Generate ─────────────────────────────────────────────────────────────
    logger.info("正在生成简报 Markdown...")
    md_content, ranked_movies = generate_briefing(
        schedules=schedules,
        config=config,
        issue_number=issue_number,
    )

    # ── Save ─────────────────────────────────────────────────────────────────
    if args.output:
        out_path = Path(args.output)
    else:
        out_path = output_dir / f"briefing_{issue_number:03d}_{today.isoformat()}.md"

    out_path.write_text(md_content, encoding="utf-8")
    logger.info("简报已保存: %s", out_path)
    print(f"\n✅  简报已生成: {out_path}\n")

    # Optional JSON dump
    if args.json:
        json_path = out_path.with_suffix(".json")
        data = []
        for m in ranked_movies:
            data.append({
                "title_jp": m.title_jp,
                "title_cn": m.title_cn,
                "director": m.director,
                "cast": m.cast,
                "year": m.year,
                "genre": m.genre,
                "duration": m.duration,
                "douban_score": m.douban_score,
                "douban_votes": m.douban_votes,
                "douban_url": m.douban_url,
                "recommendation_score": m.recommendation_score,
                "screening_count": m.screening_count,
                "is_new_release": m.is_new_release,
                "theaters": list({s.theater_name for s in m.screenings}),
                "short_reviews": m.douban_short_reviews,
            })
        json_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("JSON 数据已保存: %s", json_path)
        print(f"📊  JSON 数据: {json_path}\n")

    print(f"📽️  本期共收录 {len(ranked_movies)} 部电影")
    print("🏆  推荐榜 Top 5:")
    for i, m in enumerate(ranked_movies[:5], 1):
        title = m.title_cn or m.title_jp
        score_str = f"豆瓣 {m.douban_score}" if m.douban_score else "暂无豆瓣评分"
        print(f"   {i}. {title} — {score_str}，推荐指数 {m.recommendation_score:.0f}/100")


if __name__ == "__main__":
    main()
