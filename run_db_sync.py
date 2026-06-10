"""eiga.db ↔ 微信云存储 手动同步.

把整个本地 sqlite 当一个文件存在云存储当 master，VM/本地/step4 之间共享。

用法:
  python run_db_sync.py --push     # 上传本地 eiga.db 文件到云存储
                                   #   （首次会把返回的 file_id 记到 config.yaml，供 pull 用）
  python run_db_sync.py --pull     # 从云存储下载 eiga.db 覆盖本地

之后日常各 step 里会自动 pull/push，这个脚本主要用于首次上传和手动调试。
需先在 config.yaml 的 miniprogram 段填好 app_id / app_secret / cloud_env。
"""
import sys
import argparse
from pathlib import Path

import yaml

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from scraper.logging_setup import setup as _setup_logging
_setup_logging()

from pipeline import db_sync


def _load_config(path: str = "config.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        p = Path("config.yaml.example")
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main():
    parser = argparse.ArgumentParser(description="eiga.db ↔ 云存储 同步")
    parser.add_argument("--push", action="store_true", help="上传本地 eiga.db 文件到云存储")
    parser.add_argument("--pull", action="store_true", help="从云存储下载 eiga.db 覆盖本地")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    config = _load_config(args.config)
    mp = config.get("miniprogram", {}) or {}
    if not (mp.get("app_id") and mp.get("app_secret") and mp.get("cloud_env")):
        print("[!] 请先在 config.yaml 的 miniprogram 段填好 app_id / app_secret / cloud_env")
        return

    if args.pull:
        ok = db_sync.pull_db(config)
        print(f"\n[OK] pull: {'已下载覆盖本地' if ok else '云上暂无（先 --push 播种）'}")
    elif args.push:
        fid = db_sync.push_db(config)
        print(f"\n[OK] push: 已上传 eiga.db 文件到云存储\n     file_id: {fid}")
        print("     首次会把 file_id 记到 config.yaml 的 miniprogram.db_fileid（pull 下载用）；记得把 config 同步到 VM。")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
