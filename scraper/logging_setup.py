"""统一日志配置.

  * local（默认）：只输出到控制台，不写日志文件。
  * cloud：除了控制台，再写 logs/YYYY-MM-DD.log；启动时删除更早的日志，
            只保留「当天 + 前一天」共 keep_days 天。

环境取自 config.yaml 的 scraper.environment。各 run_*.py 用 setup() 代替原来的
logging.basicConfig(...)。
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _read_environment(config: dict | None) -> str:
    if config is None:
        try:
            import yaml
            p = Path("config.yaml")
            if not p.exists():
                p = Path("config.yaml.example")
            config = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            config = {}
    return ((config.get("scraper", {}) or {}).get("environment") or "local").strip().lower()


def _prune_old_logs(log_dir: Path, keep_days: int) -> None:
    """删除日期早于「今天 -(keep_days-1)」的 *.log（文件名形如 2026-06-09.log）。"""
    cutoff = datetime.now().date() - timedelta(days=max(0, keep_days - 1))
    for f in log_dir.glob("*.log"):
        try:
            fdate = datetime.strptime(f.stem, "%Y-%m-%d").date()
        except ValueError:
            continue  # 非日期命名的日志不动
        if fdate < cutoff:
            try:
                f.unlink()
            except Exception:
                pass


def setup(level: int = logging.INFO,
          log_dir: str = "logs",
          keep_days: int = 2,
          config: dict | None = None) -> None:
    """配置 root logging。cloud 才加文件 handler 并清理旧日志。"""
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]  # 控制台恒有

    if _read_environment(config) == "cloud":
        d = Path(log_dir)
        d.mkdir(parents=True, exist_ok=True)
        _prune_old_logs(d, keep_days)
        today = datetime.now().strftime("%Y-%m-%d")
        handlers.append(logging.FileHandler(d / f"{today}.log", encoding="utf-8"))

    logging.basicConfig(level=level, format=_FMT, handlers=handlers, force=True)
