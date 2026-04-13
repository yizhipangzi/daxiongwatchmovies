"""Briefing markdown generator for 大雄看点映.

Merges all scraped theater schedules, deduplicates movies, scores them,
and renders a ready-to-publish Markdown document.
"""
from __future__ import annotations

import logging
import math
import os
import re
from datetime import date, timedelta
from typing import Optional

from scraper.base import MovieInfo, TheaterSchedule

logger = logging.getLogger(__name__)


# ── Recommendation scoring ───────────────────────────────────────────────────

def calculate_recommendation_score(movie: MovieInfo, weights: dict) -> float:
    """Return a 0-100 recommendation score for a movie.

    Factors:
    - douban_score      (0-10 → scaled)
    - douban_votes      (log-normalized)
    - screening_count   (more showings = more popular / accessible)
    - is_new_release    (bonus for freshness)
    """
    w_score = weights.get("douban_score_weight", 0.5)
    w_votes = weights.get("douban_votes_weight", 0.2)
    w_screens = weights.get("screening_count_weight", 0.2)
    w_new = weights.get("new_release_weight", 0.1)

    # Douban score component (0-10 → 0-100)
    score_component = (movie.douban_score / 10.0) * 100 if movie.douban_score else 0.0

    # Votes component: log₁₀(votes+1) normalised against 10,000,000 votes → 100
    _LOG10_MAX_VOTES = 7.0  # log10(10_000_000) — 10M votes maps to a perfect score
    votes_component = (math.log10(movie.douban_votes + 1) / _LOG10_MAX_VOTES) * 100 \
        if movie.douban_votes else 0.0

    # Screening frequency component (cap at 20 showings)
    screens_component = min(movie.screening_count / 20.0, 1.0) * 100

    # New release bonus
    new_component = 100.0 if movie.is_new_release else 0.0

    total = (
        w_score * score_component
        + w_votes * votes_component
        + w_screens * screens_component
        + w_new * new_component
    )
    return round(total, 2)


# ── Deduplication ────────────────────────────────────────────────────────────

def _normalize_title(title: str) -> str:
    """Normalize a Japanese title for deduplication."""
    # Remove common suffixes like "(字幕版)" "(吹替版)"
    title = re.sub(r"[（(][^）)]*[）)]", "", title)
    # Collapse whitespace
    return re.sub(r"\s+", "", title).lower()


def merge_schedules(schedules: list[TheaterSchedule]) -> list[MovieInfo]:
    """Merge all theater schedules into a deduplicated list of MovieInfo."""
    seen: dict[str, MovieInfo] = {}
    for sched in schedules:
        for movie in sched.movies:
            key = _normalize_title(movie.title_jp)
            if key in seen:
                # Merge screenings
                seen[key].screenings.extend(movie.screenings)
                seen[key].screening_count = len(seen[key].screenings)
                # Keep richer data
                if not seen[key].poster_url and movie.poster_url:
                    seen[key].poster_url = movie.poster_url
            else:
                seen[key] = movie
    return list(seen.values())


# ── Markdown rendering ───────────────────────────────────────────────────────

def _stars(score: float) -> str:
    """Convert douban score to star string."""
    if score == 0:
        return "暂无评分"
    full = int(score / 2)
    half = 1 if (score / 2 - full) >= 0.5 else 0
    empty = 5 - full - half
    return "★" * full + "☆" * half + "　" * empty + f"  {score:.1f}"


def _theater_list(movie: MovieInfo) -> str:
    """Return a deduplicated, sorted list of theater names for a movie."""
    names = sorted({s.theater_name for s in movie.screenings})
    return "、".join(names)


def _week_range(start: date) -> tuple[date, date]:
    """Return (monday, sunday) of the ISO week containing `start`."""
    monday = start - timedelta(days=start.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def _format_date_jp(d: date) -> str:
    return f"{d.month}月{d.day}日"


TEMPLATE = """\
# {title}
> {subtitle}

---

## 📅 本周上映（{week_start} ～ {week_end}）

{this_week_section}

---

## 🏆 本周推荐榜 Top {top_n}

{ranking_section}

---

{next_week_section}\
## 🎬 关于大雄看点映

> 面向东京华人社区的每周电影情报，涵盖 TOHO、United 等主流连锁及独立艺术影院。  
> 数据来源：各影院官网 + 豆瓣电影  
> 如有疑问或投稿欢迎留言 🎞
"""

MOVIE_BLOCK = """\
### {rank}. {display_title}

| 项目 | 内容 |
|------|------|
| 🎬 原题 | {title_jp} |
| 🌐 豆瓣 | [{douban_score_str}]({douban_url}) |
| 👤 导演 | {director} |
| 🎭 主演 | {cast} |
| 🏷️ 类型 | {genre} |
| 📍 上映影院 | {theaters} |

{reviews_section}\
"""

RANKING_ROW = "| {rank} | {display_title} | {score_str} | {douban_score_str} | {theaters} |"


def _movie_section(movies: list[MovieInfo],
                   reviews_to_show: int = 3,
                   ranked: bool = False) -> str:
    lines: list[str] = []
    for idx, movie in enumerate(movies, 1):
        display_title = movie.title_cn if movie.title_cn else movie.title_jp
        if movie.title_cn and movie.title_cn != movie.title_jp:
            display_title = f"{movie.title_cn}（{movie.title_jp}）"

        douban_url = movie.douban_url or "https://movie.douban.com"
        douban_score_str = _stars(movie.douban_score)

        reviews_section = ""
        if movie.douban_short_reviews:
            review_lines = [
                f"> 💬 {r}" for r in movie.douban_short_reviews[:reviews_to_show]
            ]
            reviews_section = "\n".join(review_lines) + "\n"

        rank_prefix = f"{idx}" if ranked else "◆"
        block = MOVIE_BLOCK.format(
            rank=rank_prefix,
            display_title=display_title,
            title_jp=movie.title_jp,
            douban_score_str=douban_score_str,
            douban_url=douban_url,
            director=movie.director or "—",
            cast=movie.cast or "—",
            genre=movie.genre or "—",
            theaters=_theater_list(movie) or "—",
            reviews_section=reviews_section,
        )
        lines.append(block)
    return "\n".join(lines)


def _ranking_section(top_movies: list[MovieInfo], top_n: int) -> str:
    header = (
        "| 排名 | 电影 | 推荐指数 | 豆瓣评分 | 上映影院 |\n"
        "|------|------|----------|----------|----------|\n"
    )
    rows: list[str] = []
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for idx, movie in enumerate(top_movies[:top_n], 1):
        display = movie.title_cn if movie.title_cn else movie.title_jp
        if movie.title_cn and movie.title_cn != movie.title_jp:
            display = f"{movie.title_cn}"
        medal = medals.get(idx, str(idx))
        score_str = f"{movie.recommendation_score:.0f}/100"
        douban_str = f"{movie.douban_score:.1f}" if movie.douban_score else "—"
        theaters = _theater_list(movie) or "—"
        rows.append(RANKING_ROW.format(
            rank=medal,
            display_title=display,
            score_str=score_str,
            douban_score_str=douban_str,
            theaters=theaters,
        ))
    return header + "\n".join(rows)


def generate_briefing(
    schedules: list[TheaterSchedule],
    config: dict,
    target_week_start: Optional[date] = None,
    issue_number: int = 1,
) -> tuple[str, list[MovieInfo]]:
    """Generate a Markdown briefing from all theater schedules.

    Returns:
        (markdown_string, ranked_movies)
    """
    gen_cfg = config.get("generator", {})
    ranking_cfg = config.get("ranking", {})

    top_n = gen_cfg.get("top_n", 10)
    reviews_to_show = gen_cfg.get("reviews_to_show", 3)
    include_next_week = gen_cfg.get("include_next_week", True)
    title_template = gen_cfg.get(
        "title_template",
        "大雄看点映 第{issue_number}期｜{week_start} ~ {week_end}"
    )
    subtitle = gen_cfg.get("subtitle", "东京华人电影周报")

    today = date.today()
    if target_week_start is None:
        # Use the Monday of the current week
        target_week_start = today - timedelta(days=today.weekday())
    week_start, week_end = _week_range(target_week_start)

    # Merge and deduplicate
    all_movies = merge_schedules(schedules)

    # Score
    for movie in all_movies:
        movie.recommendation_score = calculate_recommendation_score(movie, ranking_cfg)

    # Sort by recommendation score
    ranked = sorted(all_movies, key=lambda m: m.recommendation_score, reverse=True)

    # Split this week / next week
    next_week_start = week_start + timedelta(weeks=1)
    next_week_end = next_week_start + timedelta(days=6)

    def _is_this_week(movie: MovieInfo) -> bool:
        dates = {s.show_date for s in movie.screenings if s.show_date}
        if not dates:
            return True  # assume current if no date info
        return any(week_start <= d <= week_end for d in dates)

    def _is_next_week(movie: MovieInfo) -> bool:
        dates = {s.show_date for s in movie.screenings if s.show_date}
        if not dates:
            return False
        return any(next_week_start <= d <= next_week_end for d in dates)

    this_week = [m for m in ranked if _is_this_week(m)]
    next_week = [m for m in ranked if _is_next_week(m)]

    # Build sections
    title = title_template.format(
        issue_number=issue_number,
        week_start=_format_date_jp(week_start),
        week_end=_format_date_jp(week_end),
    )

    this_week_section = _movie_section(this_week, reviews_to_show=reviews_to_show)
    ranking_section = _ranking_section(ranked, top_n)

    if include_next_week and next_week:
        next_week_section = (
            f"## 📌 下周预告（{_format_date_jp(next_week_start)}"
            f" ～ {_format_date_jp(next_week_end)}）\n\n"
            + _movie_section(next_week[:5], reviews_to_show=1)
            + "\n\n---\n\n"
        )
    else:
        next_week_section = ""

    md = TEMPLATE.format(
        title=title,
        subtitle=subtitle,
        week_start=_format_date_jp(week_start),
        week_end=_format_date_jp(week_end),
        this_week_section=this_week_section,
        top_n=top_n,
        ranking_section=ranking_section,
        next_week_section=next_week_section,
    )
    return md, ranked
