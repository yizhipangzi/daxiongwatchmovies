"""统一的 HTTP 抓取层（eiga + 豆瓣的非浏览器请求共用）.

职责：
  * 节流：基础延时 + 随机抖动；每隔 N 次请求插入更长「喘息」停顿。
  * 反爬：轮换 User-Agent（仅无 session 的请求，如 eiga）、可选 HTTP 代理。
  * 退避/冷却：连续失败到阈值后冷却，冷却次数用尽则标记 exhausted，让调用方
    干净停下等下次续跑（opt-in，eiga/step1 用；豆瓣有自己的 tracker，不重复）。

配置来自 config.yaml 的 scraper 段（首次使用时惰性加载）。environment 只影响
基础延时（cloud 更长）与 is_cloud() 判断；请求一律直连（pipeline 跑在哪台机器，
就从那台机器的 IP 出去）。需要换出口 IP 时配 scraper.proxy（HTTP 代理）即可。

返回值：requests.Response。
"""
from __future__ import annotations

import logging
import random
import time
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# 内置 UA 池（与 douban 的池同源，覆盖主流浏览器）
_DEFAULT_UA = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:135.0) Gecko/20100101 Firefox/135.0",
]

_CONF: dict = {
    "environment": "local",
    "proxy": "",
    "delay_local": 0.3,
    "delay_cloud": 2.5,
    "jitter": (0.4, 1.5),
    "breath_every": 20,
    "breath_range": (12, 30),
    "cooldown_threshold": 4,
    "cooldown_range": (45, 90),
    "max_cooldowns": 3,
    "timeout": 20,
    "user_agents": _DEFAULT_UA,
}
_configured = False

# 运行期状态
_req_count = 0
_consec_fail = 0
_cooldowns_used = 0
_exhausted = False


# ── 配置 ──────────────────────────────────────────────────────────────────────

def configure(scraper_cfg: Optional[dict]) -> None:
    """用 config.yaml 的 scraper 段配置抓取层。"""
    global _configured
    cfg = scraper_cfg or {}
    for k in ("environment", "proxy"):
        if cfg.get(k) is not None:
            _CONF[k] = cfg[k]
    for k in ("delay_local", "delay_cloud", "breath_every",
              "cooldown_threshold", "max_cooldowns", "timeout"):
        if cfg.get(k) is not None:
            _CONF[k] = cfg[k]
    for k in ("jitter", "breath_range", "cooldown_range"):
        v = cfg.get(k)
        if isinstance(v, (list, tuple)) and len(v) == 2:
            _CONF[k] = (float(v[0]), float(v[1]))
    uas = cfg.get("user_agents")
    if uas:
        _CONF["user_agents"] = list(uas)
    _configured = True
    logger.info("Fetcher: environment=%s proxy=%s",
                _CONF["environment"], "set" if _CONF["proxy"] else "none")


def _ensure_configured() -> None:
    if _configured:
        return
    try:
        import yaml
        p = Path("config.yaml")
        if not p.exists():
            p = Path("config.yaml.example")
        if p.exists():
            cfg = (yaml.safe_load(p.read_text(encoding="utf-8")) or {}).get("scraper", {})
            configure(cfg)
            return
    except Exception as exc:
        logger.debug("Fetcher: 配置加载失败，用默认: %s", exc)
    configure({})


def base_delay() -> float:
    """按 environment 返回基础延时（供调用方需要时取用）。"""
    _ensure_configured()
    return float(_CONF["delay_cloud"] if _CONF["environment"] == "cloud"
                 else _CONF["delay_local"])


def is_cloud() -> bool:
    """当前是否 cloud 环境（用于决定是否跳过 MD/简报、浏览器等本地产物）。"""
    _ensure_configured()
    return _CONF.get("environment") == "cloud"


def _proxies() -> Optional[dict]:
    p = _CONF.get("proxy")
    return {"http": p, "https": p} if p else None


# ── 退避/冷却（opt-in） ───────────────────────────────────────────────────────

def exhausted() -> bool:
    return _exhausted


def reset_state() -> None:
    """新一轮开始时清零计数/冷却（供 step 入口调用）。"""
    global _req_count, _consec_fail, _cooldowns_used, _exhausted
    _req_count = _consec_fail = _cooldowns_used = 0
    _exhausted = False


def _cooldown_gate() -> bool:
    global _consec_fail, _cooldowns_used, _exhausted
    if _exhausted:
        return False
    if _consec_fail >= _CONF["cooldown_threshold"]:
        if _cooldowns_used < _CONF["max_cooldowns"]:
            cd = random.uniform(*_CONF["cooldown_range"])
            logger.warning("Fetcher: 连续失败 %d 次 — 冷却 %.0fs 后续跑",
                           _consec_fail, cd)
            time.sleep(cd)
            _consec_fail = 0
            _cooldowns_used += 1
            return True
        logger.warning("Fetcher: 冷却次数用尽 — 本轮停止抓取（已抓数据已落库，下次续跑）")
        _exhausted = True
        return False
    return True


def _record(success: bool) -> None:
    global _consec_fail
    _consec_fail = 0 if success else _consec_fail + 1


# ── 节流 ──────────────────────────────────────────────────────────────────────

def _pace(delay: float) -> None:
    global _req_count
    _req_count += 1
    time.sleep(max(0.0, delay) + random.uniform(*_CONF["jitter"]))
    if _CONF["breath_every"] and _req_count % int(_CONF["breath_every"]) == 0:
        br = random.uniform(*_CONF["breath_range"])
        logger.info("Fetcher: 第 %d 次请求后喘息 %.0fs", _req_count, br)
        time.sleep(br)


# ── 对外主入口 ────────────────────────────────────────────────────────────────

def get(url: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        session: Optional[requests.Session] = None,
        delay: float = 0.0,
        timeout: Optional[float] = None,
        cooldown: bool = False):
    """抓取一个 URL，返回 requests.Response（失败返回 None）。

    Args:
        session: 提供则用该 session（豆瓣，保留 cookie/curl_cffi）；不提供则裸 requests。
        delay: 基础延时（会再叠加随机抖动）。
        cooldown: True 时启用全局退避/冷却门控（eiga/step1 用；豆瓣传 False）。
    """
    _ensure_configured()
    if cooldown and not _cooldown_gate():
        return None

    h = dict(headers or {})
    # UA 轮换：仅对无 session 的裸请求（eiga）；豆瓣保留 session 自己的 UA 以保持一致。
    if session is None and "User-Agent" not in h:
        h["User-Agent"] = random.choice(_CONF["user_agents"] or _DEFAULT_UA)

    _pace(delay)
    timeout = timeout or _CONF["timeout"]
    try:
        if session is not None:
            resp = session.get(url, params=params, headers=h,
                               timeout=timeout, proxies=_proxies())
        else:
            resp = requests.get(url, params=params, headers=h,
                                timeout=timeout, proxies=_proxies())
        if cooldown:
            _record(resp is not None and getattr(resp, "status_code", 0) < 500)
        return resp
    except Exception as exc:
        logger.debug("Fetcher: 请求失败 %s: %s", url, exc)
        if cooldown:
            _record(False)
        return None
