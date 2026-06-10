"""Run Step 4: 导出小程序 JSON 并上传到微信云开发云存储.

step4 单独运行，不在 run_scraper.py 内。从本地 data/eiga.db 生成 9 个按区
JSON（含 director/cast/想看/看过/短评/showtimes 等新字段），写到 output 目录，
再上传到云开发云存储。

用法:
  python run_step4.py                  # 生成 + 上传
  python run_step4.py --no-upload      # 只生成本地 JSON，不上传
  python run_step4.py --out DIR        # 指定本地输出目录（默认 output/miniprogram）

需先在 config.yaml 的 miniprogram 段填好 app_id / app_secret / cloud_env，
并在微信开发者工具里开通云开发。
"""
import sys
import argparse
import logging
from pathlib import Path

import yaml

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from scraper.logging_setup import setup as _setup_logging
_setup_logging()  # local=仅控制台；cloud=另写 logs/当天.log，只留 2 天
logger = logging.getLogger("run_step4")

from pipeline import step4_export


def _load_config(path: str = "config.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        p = Path("config.yaml.example")
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def main():
    parser = argparse.ArgumentParser(description="Step 4: 导出小程序 JSON + 上传云存储")
    parser.add_argument("--no-upload", action="store_true",
                        help="只生成本地 JSON，不上传云存储")
    parser.add_argument("--out", default="output/miniprogram",
                        help="本地输出目录（默认 output/miniprogram）")
    parser.add_argument("--config", default="config.yaml")
    args = parser.parse_args()

    config = _load_config(args.config)
    result = step4_export.run(config, Path(args.out), upload=not args.no_upload)

    print(f"\n[OK] 已生成 {len(result['files'])} 个 JSON → {args.out}")
    if result["file_ids"]:
        print("[OK] 已上传到云存储：")
        for did, fid in result["file_ids"].items():
            print(f"  {did}: {fid}")
    elif not args.no_upload:
        print("[!] 未上传（检查 miniprogram.cloud_env / app_secret 配置）")


if __name__ == "__main__":
    main()
