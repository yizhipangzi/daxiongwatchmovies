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

import gzip
import hashlib
import json
import logging
import re
import shutil
import sqlite3
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from pipeline import wxcloud

logger = logging.getLogger(__name__)

DB_PATH = Path("data/eiga.db")

# DB 里记录「cloud VM 当天成功跑完」的日期戳（run_state 表）。本地据此门控。
_STAMP_KEY = "cloud_last_success_date"
# DB 里记录「本次推送版本戳」（run_state）。push 时写入并随库上传，同时单独存进
# 云端 meta 文件；pull 先读 meta 对比本地此值，一致说明云端没更新 → 跳过下载整库。
_PUSHED_KEY = "db_pushed_at"


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


def backup_db(config: dict, keep_days: int = 7, db_path: Path = DB_PATH) -> Optional[str]:
    """上传一份 db/backup/eiga-YYYYMMDD.db（gzip）作为当天备份，并删除超过 keep_days
    天的旧备份。供 cloud 每轮开始时（pull 之后）调用，保留最近 keep_days 天的滚动备份。

    云存储没有「列目录」接口，所以删除用确定性 fileid：按日期构造 keep_days..+30 天前的
    备份 fileid 批量删（不存在的会被忽略）。返回备份 file_id（失败/无 DB 返回 None）。
    """
    mp = wxcloud.mp_cfg(config)
    env = mp.get("cloud_env", "")
    if not env:
        raise RuntimeError("miniprogram.cloud_env 未配置")
    if not Path(db_path).exists():
        logger.warning("backup_db: 本地 DB 不存在，跳过备份")
        return None
    token = wxcloud.get_access_token(config)
    _checkpoint_wal(db_path)
    gz = gzip.compress(Path(db_path).read_bytes(), compresslevel=6)
    base = datetime.now(timezone(timedelta(hours=9))).date()  # JST
    backup_path = f"db/backup/eiga-{base.strftime('%Y%m%d')}.db"
    fid = wxcloud.upload(env, token, backup_path, gz)
    logger.info("backup_db: 已上传当天备份 %s (%d bytes)", backup_path, len(gz))

    # 删旧备份：用 db_fileid 推出云存储前缀，构造 keep_days..+30 天前的 fileid 批量删。
    db_fid = _db_fileid(config)
    cloud_path = _cloud_db_path(config)
    if db_fid and db_fid.endswith(cloud_path):
        prefix = db_fid[: -len(cloud_path)]  # cloud://{env}.{bucket}/
        old_ids = [
            prefix + f"db/backup/eiga-{(base - timedelta(days=ago)).strftime('%Y%m%d')}.db"
            for ago in range(keep_days, keep_days + 31)
        ]
        try:
            wxcloud.delete(env, token, old_ids)
            logger.info("backup_db: 已清理 >%d 天的旧备份（候选 %d 个）", keep_days, len(old_ids))
        except Exception as exc:
            logger.warning("backup_db: 清理旧备份失败（忽略）: %s", exc)
    return fid


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
    # 写入本次推送版本戳（随库上传 + 单独 meta 文件，供 pull 端先对比再决定是否下整库）
    pushed_at = repr(time.time())
    _set_run_state(db_path, _PUSHED_KEY, pushed_at)
    _checkpoint_wal(db_path)  # 把 WAL 合并进主库，避免漏掉最近提交
    # gzip 压缩再传：SQLite 压缩率高，几 MB 能压到几百 KB，避免 COS 因传太慢
    # 报 UserNetworkTooSlow（文件越小越快传完）。pull 端按 gzip 魔数自动解压。
    raw = Path(db_path).read_bytes()
    gz = gzip.compress(raw, compresslevel=6)
    fid = wxcloud.upload(env, token, cloud_path, gz)
    # 极小 meta：只放版本戳。pull 端先读它对比本地，没更新就不下载整库。
    try:
        wxcloud.upload(env, token, cloud_path + ".meta.json",
                       json.dumps({"pushed_at": pushed_at}).encode("utf-8"))
    except Exception as exc:
        logger.warning("push_db: meta 上传失败（不影响主库同步）: %s", exc)
    # 同一路径上传 file_id 稳定；首次拿到就回写 config 方便以后 pull。
    if fid and fid != _db_fileid(config):
        _save_db_fileid_to_config(fid)
    logger.info("push_db: %s → %s (%d → %d bytes, gzip, v=%s)",
                db_path, cloud_path, len(raw), len(gz), pushed_at)
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

    # 下载前先对比版本：读云端极小 meta（秒下），版本和本地一致说明云端没更新，
    # 跳过整库下载（省跨境流量/时间）。meta 读不到（旧库/首次）就回退照常下载。
    try:
        meta_bytes = wxcloud.download(env, token, fileid + ".meta.json")
        cloud_v = json.loads(meta_bytes).get("pushed_at")
        local_v = _get_run_state(db_path, _PUSHED_KEY)
        if cloud_v and local_v and str(cloud_v) == str(local_v):
            logger.info("pull_db: 云端未更新（版本一致 v=%s），跳过下载", cloud_v)
            return True
    except Exception as exc:
        logger.debug("pull_db: 读 meta 失败，照常下载整库: %s", exc)

    try:
        content = wxcloud.download(env, token, fileid)
    except Exception as exc:
        logger.warning("pull_db: 下载失败（可能云上还没有），跳过: %s", exc)
        return False
    # 按 gzip 魔数（1f 8b）自动解压；旧的未压缩文件（裸 sqlite）原样用，兼容过渡。
    if content[:2] == b"\x1f\x8b":
        content = gzip.decompress(content)
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


def _set_run_state(db_path: Path, key: str, value: str) -> None:
    conn = _run_state_conn(db_path)
    conn.execute("INSERT OR REPLACE INTO run_state (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def _get_run_state(db_path: Path, key: str) -> Optional[str]:
    if not Path(db_path).exists():
        return None
    conn = _run_state_conn(db_path)
    row = conn.execute("SELECT value FROM run_state WHERE key=?", (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def set_cloud_stamp(date_str: str, db_path: Path = DB_PATH) -> None:
    """记录 cloud VM 当天成功跑完的日期戳（写进 DB 的 run_state，会随 push 同步）。"""
    _set_run_state(db_path, _STAMP_KEY, date_str)


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
