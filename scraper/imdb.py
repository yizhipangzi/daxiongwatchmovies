"""IMDb 评分获取 — 走 OMDb API（omdbapi.com），按 imdb_id 换结构化数据.

不直接爬 imdb.com：那边是重度反爬 + 前端渲染，OMDb 是免费的 IMDb 数据镜像，
用 imdb_id 就能拿到 imdbRating/imdbVotes，稳定不用维护 HTML 解析。免费额度
每天 1000 次，覆盖每日在映电影的评分刷新绰绰有余（见 config.yaml 的 omdb.api_key）。
"""
from __future__ import annotations

import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

OMDB_URL = "https://www.omdbapi.com/"
_TIMEOUT = 10.0


def fetch_imdb_rating(imdb_id: str, api_key: str,
                       retries: int = 2, delay: float = 1.0) -> Optional[dict]:
    """按 imdb_id 查 OMDb，返回 {"imdb_score": float, "imdb_votes": int}。

    查不到 / 还没有评分人数 / 请求失败（重试后仍失败）都返回 None，调用方按
    "这次没拿到，下次再试" 处理，不落库。
    """
    if not imdb_id or not api_key:
        return None
    last_exc: Optional[Exception] = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                OMDB_URL,
                params={"i": imdb_id, "apikey": api_key},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("Response") == "False":
                logger.info("OMDb %s: %s", imdb_id, data.get("Error"))
                return None
            score_raw = data.get("imdbRating") or "N/A"
            votes_raw = data.get("imdbVotes") or "N/A"
            if score_raw == "N/A":
                return None
            score = float(score_raw)
            votes = int(votes_raw.replace(",", "")) if votes_raw != "N/A" else 0
            return {"imdb_score": score, "imdb_votes": votes}
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                time.sleep(delay * attempt)
    logger.warning("OMDb fetch failed for %s: %s", imdb_id, last_exc)
    return None
