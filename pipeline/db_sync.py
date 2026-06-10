"""eiga.db ↔ 微信云存储 文件级同步（同步中枢）.

把整个本地 sqlite (data/eiga.db) 当成一个文件放进云开发云存储，作为
VM / 本地 / step4 之间的同步中枢：

  VM(cron):  pull_db → step1/2/3（跳过今天已成功的）→ push_db
  本地:      pull_db → run_step2(resume，补浏览器匹配) → push_db
  step4:     pull_db → 导出 9 区 JSON → 上传 JSON（小程序读 JSON）

不碰云数据库 / 不映射 schema / 不吃数据库读写配额：一个文件上下传而已。
小程序消费的是 step4 导出的 JSON，不直接读 eiga.db。

复用 step4_export 里的云存储基建（access_token / 上传），这里补「下载」。

⚠️ 整文件同步是「最后写的赢」。VM cron 与本地尽量错峰，避免并发互相覆盖。
"""
from __future__ import annotations

import hashlib
import logging
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from pipeline import wxcloud

logger = logging.getLogger(__name__)

DB_PATH = Path("data/eiga.db")

# DB 里记录「cloud VM 当天成功跑完」的日期戳（run_state 表）。本地据此门控。
_STAMP_KEY = "cloud_last_success_date"


def today_jst() -> str:
    """东京日期 (YYYY-MM-DD)，与 step1 的 last_run_date / 快照保持一致。"""
    return datetime.now(timezone(timedelta(hours=9))).date().isoformat()


# ── 配置辅助 ──────────────────────────────────────────────────────────────────

def _cloud_db_path(config: dict) -> str:
    return (wxcloud.mp_cfg(config).get("db_path") or "db/eiga.db").strip("/")


def _db_fileid(config: dict) -> str:
    return wxcloud.mp_cfg(config).get("db_fileid") or ""


def _save_db_fileid_to_config(fileid: str, config_path: str = "config.yaml") -> bool:
    """把初始化得到的 file_id 写回 config.yaml 的 miniprogram.db_fileid。"""
    p = Path(config_path)
    if not p.exists():
        return False
    text = p.read_text(encoding="utf-8")
    new = re.sub(r'(\n\s*db_fileid:\s*).*', rf'\g<1>"{fileid}"', text, count=1)
    if new != text:
        p.write_text(new, encoding="utf-8")
        return True
    return False


# ── 上传 / 下载 ───────────────────────────────────────────────────────────────

def _checkpoint_wal(db_path: Path) -> None:
    """把 WAL 里的已提交数据合并进主 .db 文件。

    step1/2/3 用 WAL 模式，最近的提交可能还在 eiga.db-wal 里。只上传主 .db 会丢这部分，
    所以读字节上传前必须 checkpoint(TRUNCATE)。
    """
    if not Path(db_path).exists():
        return
    try:
        c = sqlite3.connect(str(db_path))
        c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        c.close()
    except Exception as exc:
        logger.debug("wal_checkpoint 跳过: %s", exc)


def _clear_wal_siblings(db_path: Path) -> None:
    """删除 -wal / -shm，避免覆盖主库后残留旧 WAL 造成不一致。"""
    for ext in ("-wal", "-shm"):
        sib = Path(str(db_path) + ext)
        if sib.exists():
            try:
                sib.unlink()
            except Exception:
                pass


def push_db(config: dict, db_path: Path = DB_PATH) -> str:
    """上传本地 eiga.db 到云存储，返回 file_id。"""
    mp = wxcloud.mp_cfg(config)
    env = mp.get("cloud_env", "")
    if not env:
        raise RuntimeError("miniprogram.cloud_env 未配置")
    if not Path(db_path).exists():
        raise RuntimeError(f"本地 DB 不存在: {db_path}")
    token = wxcloud.get_access_token(config)
    cloud_path = _cloud_db_path(config)
    _checkpoint_wal(db_path)  # 把 WAL 合并进主库，避免漏掉最近提交
    fid = wxcloud.upload(env, token, cloud_path, Path(db_path).read_bytes())
    # 同一路径上传 file_id 稳定；首次拿到就回写 config 方便以后 pull。
    if fid and fid != _db_fileid(config):
        _save_db_fileid_to_config(fid)
    logger.info("push_db: %s → %s (%d bytes)", db_path, cloud_path,
                Path(db_path).stat().st_size)
    return fid


def pull_db(config: dict, db_path: Path = DB_PATH) -> bool:
    """从云存储下载 eiga.db 覆盖本地。云上还没有则返回 False（首次需先 init/push）。"""
    mp = wxcloud.mp_cfg(config)
    env = mp.get("cloud_env", "")
    if not env:
        raise RuntimeError("miniprogram.cloud_env 未配置")
    fileid = _db_fileid(config)
    if not fileid:
        logger.warning("pull_db: 尚无 db_fileid（先在本地跑一次 push_db 播种云端），跳过下载")
        return False
    token = wxcloud.get_access_token(config)
    try:
        content = wxcloud.download(env, token, fileid)
    except Exception as exc:
        logger.warning("pull_db: 下载失败（可能云上还没有），跳过: %s", exc)
        return False
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # 先写临时文件再原子替换，避免写一半损坏。
    with tempfile.NamedTemporaryFile(dir=str(Path(db_path).parent), delete=False) as tf:
        tmp = Path(tf.name)
        tf.write(content)
    shutil.move(str(tmp), str(db_path))
    _clear_wal_siblings(db_path)  # 清掉旧 -wal/-shm，防止覆盖后不一致
    logger.info("pull_db: 云存储 → %s (%d bytes)", db_path, len(content))
    return True


# ── 日期戳 / 变化检测 ─────────────────────────────────────────────────────────

def _run_state_conn(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE IF NOT EXISTS run_state (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def set_cloud_stamp(date_str: str, db_path: Path = DB_PATH) -> None:
    """记录 cloud VM 当天成功跑完的日期戳（写进 DB 的 run_state，会随 push 同步）。"""
    conn = _run_state_conn(db_path)
    conn.execute("INSERT OR REPLACE INTO run_state (key, value) VALUES (?, ?)",
                 (_STAMP_KEY, date_str))
    conn.commit()
    conn.close()


def get_cloud_stamp(db_path: Path = DB_PATH) -> Optional[str]:
    """读取 cloud 成功日期戳；没有返回 None。"""
    if not Path(db_path).exists():
        return None
    conn = _run_state_conn(db_path)
    row = conn.execute("SELECT value FROM run_state WHERE key=?", (_STAMP_KEY,)).fetchone()
    conn.close()
    return row["value"] if row else None


def db_fingerprint(db_path: Path = DB_PATH) -> str:
    """主库文件内容的 sha256（先 checkpoint 把 WAL 合并进主库）。用于判断是否有变化。"""
    if not Path(db_path).exists():
        return ""
    _checkpoint_wal(db_path)
    h = hashlib.sha256()
    with open(db_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
