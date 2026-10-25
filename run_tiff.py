"""東京国際映画祭 (TIFF) 板块：导入 → eiga/豆瓣匹配 → 导出小程序 tiff.json 并上传.

数据源是 TIFF 官网「上映作品・イベント一覧」页（https://2026.tiff-jp.net/ja/lineup/list.html）
在浏览器里滚到底、全部加载后另存的 HTML（如 Downloads/eiga.txt）。详见 pipeline/tiff.py。

用法:
  python run_tiff.py all  <html>            # import + eiga + douban + export(上传)，一条龙
  python run_tiff.py import <html> [--no-api]
  python run_tiff.py eiga   [--all | --retry-none] [--code CODE ...]
  python run_tiff.py douban [--refresh] [--code CODE ...]
  python run_tiff.py export [--no-upload] [--out DIR]
  python run_tiff.py report                 # 匹配一览（人工核对）
  python run_tiff.py set-eiga   CODE EIGA_ID|none
  python run_tiff.py set-douban CODE DOUBAN_ID|none

  --sync   开始前 pull eiga.db（本地 TIFF 表先快照、pull 后回灌）、结束后 push
           （与 VM cron 错峰！整库同步是「最后写的赢」）
"""
import sys
import argparse
import logging
from pathlib import Path

import yaml

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from scraper.logging_setup import setup as _setup_logging
_setup_logging()
logger = logging.getLogger("run_tiff")

from pipeline import tiff


def _load_config(path: str = "config.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        p = Path("config.yaml.example")
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main():
    ap = argparse.ArgumentParser(description="TIFF 板块数据")
    ap.add_argument("cmd", choices=["all", "import", "eiga", "douban", "export", "report",
                                    "set-eiga", "set-douban"])
    ap.add_argument("args", nargs="*")
    ap.add_argument("--no-api", action="store_true", help="import 时不调官网 API（只用 HTML）")
    ap.add_argument("--all", action="store_true", help="eiga: 已匹配/已判定无的也重跑")
    ap.add_argument("--retry-none", action="store_true", help="eiga: 只重跑上次没匹配到的")
    ap.add_argument("--refresh", action="store_true", help="douban: 已匹配的重抓分数")
    ap.add_argument("--code", nargs="*", help="只处理这些作品编号")
    ap.add_argument("--no-upload", action="store_true")
    ap.add_argument("--out", default="output/miniprogram")
    ap.add_argument("--sync", action="store_true", help="前 pull / 后 push eiga.db")
    ap.add_argument("--config", default="config.yaml")
    a = ap.parse_args()
    config = _load_config(a.config)

    if a.sync:
        from pipeline import db_sync
        snap = tiff.snapshot_tables()
        db_sync.pull_db(config)
        n = tiff.restore_tables(snap)
        logger.info("pull 后回灌本地 TIFF 数据 %d 行", n)

    if a.cmd in ("all", "import"):
        if not a.args:
            ap.error("需要 HTML 文件路径")
        print(tiff.import_films(Path(a.args[0]), use_api=not a.no_api))
    if a.cmd in ("all", "eiga"):
        print(tiff.match_eiga(only_missing=not a.all, codes=a.code, retry_none=a.retry_none))
    if a.cmd in ("all", "douban"):
        print(tiff.match_douban(refresh_scores=a.refresh, codes=a.code))
    if a.cmd == "set-eiga":
        tiff.set_eiga(a.args[0], a.args[1])
    if a.cmd == "set-douban":
        tiff.set_douban(a.args[0], a.args[1])
    if a.cmd in ("all", "export", "set-eiga", "set-douban"):
        r = tiff.export(Path(a.out), upload=not a.no_upload, config=config)
        print(f"[OK] {r['file']} ver={r['ver']}")
        for k, v in r["file_ids"].items():
            print(f"  {k}: {v}")
    if a.cmd == "report":
        print(tiff.report())

    if a.sync:
        from pipeline import db_sync
        db_sync.push_db(config)


if __name__ == "__main__":
    main()
