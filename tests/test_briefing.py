"""Tests for the briefing generator."""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from scraper.base import MovieInfo, ScreeningInfo, TheaterSchedule
from generator.briefing import (
    calculate_recommendation_score,
    merge_schedules,
    generate_briefing,
    _normalize_title,
    _stars,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def make_movie(title="テスト映画", score=8.0, votes=100000,
               screenings=2, is_new=False) -> MovieInfo:
    today = date.today()
    m = MovieInfo(
        title_jp=title,
        title_cn="测试电影",
        douban_score=score,
        douban_votes=votes,
        is_new_release=is_new,
    )
    for i in range(screenings):
        m.add_screening(ScreeningInfo(
            theater_name="テスト影院",
            show_date=today + timedelta(days=i),
            start_time="14:00",
        ))
    return m


def make_schedule(movies, source="TEST") -> TheaterSchedule:
    s = TheaterSchedule(source=source, theater_name="テスト")
    s.movies = movies
    return s


DEFAULT_WEIGHTS = {
    "douban_score_weight": 0.5,
    "douban_votes_weight": 0.2,
    "screening_count_weight": 0.2,
    "new_release_weight": 0.1,
}


# ── Unit tests: scoring ───────────────────────────────────────────────────────

class TestCalculateRecommendationScore:
    def test_perfect_movie_near_100(self):
        m = make_movie(score=10.0, votes=10_000_000, screenings=20, is_new=True)
        score = calculate_recommendation_score(m, DEFAULT_WEIGHTS)
        assert score > 90

    def test_no_douban_data(self):
        m = make_movie(score=0.0, votes=0, screenings=1, is_new=False)
        score = calculate_recommendation_score(m, DEFAULT_WEIGHTS)
        # No douban data → screens component only
        assert 0 <= score <= 20

    def test_new_release_bonus(self):
        m_old = make_movie(score=7.0, votes=50000, screenings=3, is_new=False)
        m_new = make_movie(score=7.0, votes=50000, screenings=3, is_new=True)
        s_old = calculate_recommendation_score(m_old, DEFAULT_WEIGHTS)
        s_new = calculate_recommendation_score(m_new, DEFAULT_WEIGHTS)
        assert s_new > s_old

    def test_higher_score_ranks_higher(self):
        m_hi = make_movie(score=9.0, votes=100000, screenings=5)
        m_lo = make_movie(score=5.0, votes=100000, screenings=5)
        assert (calculate_recommendation_score(m_hi, DEFAULT_WEIGHTS) >
                calculate_recommendation_score(m_lo, DEFAULT_WEIGHTS))

    def test_returns_float(self):
        m = make_movie()
        result = calculate_recommendation_score(m, DEFAULT_WEIGHTS)
        assert isinstance(result, float)

    def test_score_range(self):
        m = make_movie(score=7.5, votes=200000, screenings=4, is_new=True)
        result = calculate_recommendation_score(m, DEFAULT_WEIGHTS)
        assert 0.0 <= result <= 100.0


# ── Unit tests: deduplication ─────────────────────────────────────────────────

class TestMergeSchedules:
    def test_deduplication_same_title(self):
        m1 = make_movie("同じ映画", screenings=1)
        m2 = make_movie("同じ映画", screenings=2)
        s1 = make_schedule([m1], source="A")
        s2 = make_schedule([m2], source="B")
        merged = merge_schedules([s1, s2])
        assert len(merged) == 1
        assert merged[0].screening_count == 3

    def test_different_titles_kept(self):
        m1 = make_movie("映画A")
        m2 = make_movie("映画B")
        merged = merge_schedules([make_schedule([m1]), make_schedule([m2])])
        assert len(merged) == 2

    def test_empty_schedules(self):
        assert merge_schedules([]) == []

    def test_suffix_stripping_deduplication(self):
        """'映画A（字幕版）' and '映画A（吹替版）' should merge."""
        m1 = make_movie("映画A（字幕版）", screenings=1)
        m2 = make_movie("映画A（吹替版）", screenings=1)
        merged = merge_schedules([make_schedule([m1]), make_schedule([m2])])
        assert len(merged) == 1
        assert merged[0].screening_count == 2


# ── Unit tests: normalize_title ───────────────────────────────────────────────

class TestNormalizeTitle:
    def test_removes_brackets(self):
        assert _normalize_title("映画（字幕版）") == _normalize_title("映画（吹替版）")

    def test_collapses_spaces(self):
        assert _normalize_title("映画　タイトル") == "映画タイトル"

    def test_lowercased(self):
        assert _normalize_title("Movie") == "movie"


# ── Unit tests: stars ─────────────────────────────────────────────────────────

class TestStars:
    def test_zero_score(self):
        assert _stars(0) == "暂无评分"

    def test_five_stars(self):
        result = _stars(10.0)
        assert "★★★★★" in result

    def test_contains_score(self):
        result = _stars(8.5)
        assert "8.5" in result


# ── Integration test: generate_briefing ──────────────────────────────────────

class TestGenerateBriefing:
    def _config(self):
        return {
            "generator": {
                "output_dir": "output",
                "title_template": "大雄看点映 第{issue_number}期｜{week_start} ~ {week_end}",
                "subtitle": "东京华人电影周报",
                "top_n": 5,
                "include_next_week": False,
                "reviews_to_show": 2,
            },
            "ranking": DEFAULT_WEIGHTS,
        }

    def test_returns_markdown_and_movies(self):
        movies = [make_movie(f"映画{i}", score=float(i+5), votes=10000*i,
                             screenings=i+1) for i in range(3)]
        sched = make_schedule(movies)
        md, ranked = generate_briefing([sched], self._config(), issue_number=1)
        assert isinstance(md, str)
        assert isinstance(ranked, list)
        assert len(ranked) == 3

    def test_markdown_contains_title(self):
        sched = make_schedule([make_movie()])
        md, _ = generate_briefing([sched], self._config(), issue_number=7)
        assert "第7期" in md

    def test_ranking_order(self):
        low = make_movie("低分映画", score=4.0, votes=1000, screenings=1)
        high = make_movie("高分映画", score=9.5, votes=500000, screenings=10, is_new=True)
        sched = make_schedule([low, high])
        _, ranked = generate_briefing([sched], self._config(), issue_number=1)
        # High-scoring movie should be first
        assert ranked[0].title_jp == "高分映画"

    def test_markdown_contains_ranking_table(self):
        sched = make_schedule([make_movie()])
        md, _ = generate_briefing([sched], self._config(), issue_number=1)
        assert "推荐榜" in md or "排名" in md

    def test_empty_schedules(self):
        md, ranked = generate_briefing([], self._config(), issue_number=1)
        assert isinstance(md, str)
        assert ranked == []
