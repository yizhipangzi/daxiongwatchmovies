"""综合推荐度：融合豆瓣 / IMDb / eiga.com 三个评分来源为 0-100 分。

算法（贝叶斯收缩 + 加权平均，思路同 IMDb 经典 Top250 排序公式）：
  1. 各来源原始分先统一换算到 10 分制（eiga.com 满分 5 分 ×2）。
  2. 对每个来源做贝叶斯收缩：WR = v/(v+m)*R + m/(v+m)*C
     打分人数 v 越少，越向该来源的先验均值 C 收缩；v >> m 时约等于原始分 R。
     这避免"只有几个人打了 10 分"的冷门条目凭原始高分冲到榜首。
  3. 收缩后的分数按来源权重（豆瓣 > IMDb >>> eiga.com，见 config.yaml 的
     ranking 段）加权平均——只有真正拿到评分的来源参与平均，都没有则为 0。
  4. 结果 ×10 换算到 0-100 分制。

权重和收缩常数均可在 config.yaml 的 `ranking` 段调整，不改代码。
"""
from __future__ import annotations

from typing import Optional

DEFAULT_WEIGHTS: dict = {
    "douban_weight": 0.55,
    "imdb_weight": 0.35,
    "eiga_weight": 0.10,
    # 贝叶斯收缩的“半信区间”：打分人数达到该值时，原始分与先验各占一半权重。
    "douban_votes_m": 2000,
    "imdb_votes_m": 2000,
    "eiga_votes_m": 100,
    # 先验均值（已统一到 10 分制）：人数不足时分数向此值收缩。
    "douban_prior": 6.5,
    "imdb_prior": 6.5,
    "eiga_prior": 6.5,
}


def load_ranking_weights(config: Optional[dict]) -> dict:
    """从 config.yaml 的 `ranking` 段读取权重，缺项用 DEFAULT_WEIGHTS 补齐。"""
    cfg_weights = (config or {}).get("ranking") or {}
    return {**DEFAULT_WEIGHTS, **cfg_weights}


def _shrink(score10: float, votes: int, m: float, prior: float) -> float:
    votes = max(votes or 0, 0)
    if m <= 0:
        return score10
    return (votes / (votes + m)) * score10 + (m / (votes + m)) * prior


def compute_recommend_score(
    douban_score: float = 0.0, douban_votes: int = 0,
    imdb_score: float = 0.0, imdb_votes: int = 0,
    eiga_rating: float = 0.0, eiga_votes: int = 0,
    weights: Optional[dict] = None,
) -> float:
    """返回 0-100 综合推荐度；三个来源都没有评分时返回 0.0。"""
    w = weights or DEFAULT_WEIGHTS

    sources: list[tuple[float, float]] = []
    if douban_score:
        sources.append((
            _shrink(douban_score, douban_votes, w["douban_votes_m"], w["douban_prior"]),
            w["douban_weight"],
        ))
    if imdb_score:
        sources.append((
            _shrink(imdb_score, imdb_votes, w["imdb_votes_m"], w["imdb_prior"]),
            w["imdb_weight"],
        ))
    if eiga_rating:
        sources.append((
            _shrink(eiga_rating * 2, eiga_votes, w["eiga_votes_m"], w["eiga_prior"]),
            w["eiga_weight"],
        ))

    total_w = sum(wt for _, wt in sources)
    if total_w <= 0:
        return 0.0
    composite10 = sum(s * wt for s, wt in sources) / total_w
    return round(composite10 * 10, 1)
