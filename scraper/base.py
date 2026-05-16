"""Base data classes for movie scraping."""
from __future__ import annotations

import re
import time
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja,zh-CN;q=0.9,en;q=0.8",
}


@dataclass
class ScreeningInfo:
    """A single screening slot."""
    theater_name: str
    screen: str = ""
    show_date: Optional[date] = None
    start_time: str = ""
    end_time: str = ""


@dataclass
class MovieInfo:
    """All information about a movie showing in Tokyo this week."""
    title_jp: str                          # 日文原题
    title_cn: str = ""                     # 中文译名（从豆瓣获取）
    title_original: str = ""               # 拍摄国原语言标题
    title_en: str = ""                     # 英语标题
    country: str = ""                      # 制片国家/地区
    director: str = ""                     # 导演（中文）
    cast: str = ""                         # 主演（中文）
    year: int = 0                          # 年份
    genre: str = ""                        # 类型（中文）
    duration: int = 0                      # 时长（分钟）
    synopsis_jp: str = ""                  # 日文简介
    poster_url: str = ""                   # 海报 URL
    is_new_release: bool = False           # 本周新上映
    screenings: list[ScreeningInfo] = field(default_factory=list)

    # 上映期间
    screening_start: Optional[date] = None  # 最早上映日
    screening_end: Optional[date] = None    # 最晚上映日

    # 豆瓣数据
    douban_id: str = ""
    douban_url: str = ""
    douban_score: float = 0.0
    douban_votes: int = 0
    douban_want_to_watch: int = 0          # 想看人数
    douban_watched: int = 0                # 看过人数
    douban_short_reviews: list[str] = field(default_factory=list)

    # 宣传片链接
    trailer_douban_urls: list[str] = field(default_factory=list)
    trailer_youtube_url: str = ""          # YouTube 官方预告片

    # 影院提供的原始数据（用于多源交叉验证英文名）
    _hint_ename: str = ""                  # TOHO API ename 等影院侧英文名
    _hint_mcode: str = ""                  # TOHO 作品コード（用于详情页查询）

    # 计算字段
    recommendation_score: float = 0.0
    screening_count: int = 0              # 本周总场次数

    def add_screening(self, s: ScreeningInfo) -> None:
        self.screenings.append(s)
        self.screening_count = len(self.screenings)

    def compute_screening_period(self) -> None:
        """Compute screening_start / screening_end from screenings.

        Start: earliest show_date (if different across theaters, take earliest).
        End: per chain, if 2 consecutive days have no screenings treat as ended;
             across chains, take the latest end date.
        """
        dated = [s for s in self.screenings if s.show_date]
        if not dated:
            return
        all_dates = [s.show_date for s in dated]
        self.screening_start = min(all_dates)

        chain_dates: dict[str, set[date]] = {}
        for s in dated:
            chain = s.theater_name.split()[0] if s.theater_name else "other"
            chain_dates.setdefault(chain, set()).add(s.show_date)

        latest_end: Optional[date] = None
        for _chain, dates_set in chain_dates.items():
            sorted_dates = sorted(dates_set)
            end = sorted_dates[0]
            for i in range(1, len(sorted_dates)):
                gap = (sorted_dates[i] - sorted_dates[i - 1]).days
                if gap > 2:
                    break
                end = sorted_dates[i]
            if latest_end is None or end > latest_end:
                latest_end = end
        self.screening_end = latest_end


@dataclass
class TheaterSchedule:
    """Schedule output from a single theater / chain."""
    source: str                            # e.g. "TOHO", "United", "独立"
    theater_name: str
    movies: list[MovieInfo] = field(default_factory=list)


def fetch_html(url: str, *, delay: float = 0.5, timeout: int = 15,
               params: Optional[dict] = None) -> Optional[BeautifulSoup]:
    """Fetch a URL and return a BeautifulSoup object, or None on failure."""
    try:
        time.sleep(delay)
        resp = requests.get(url, headers=HEADERS, params=params,
                            timeout=timeout)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "utf-8"
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as exc:
        logger.warning("Failed to fetch %s: %s", url, exc)
        return None


def clean_text(text: str) -> str:
    """Strip whitespace and normalise full-width spaces."""
    return re.sub(r"\s+", " ", text.replace("\u3000", " ")).strip()