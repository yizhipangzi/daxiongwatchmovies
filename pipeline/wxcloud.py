"""微信云开发云存储 —— 共用基建（access_token / 上传 / 下载）.

step4_export（上传区 JSON）和 db_sync（上传/下载 eiga.db）都用这里，避免重复。
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
UPLOAD_META_URL = "https://api.weixin.qq.com/tcb/uploadfile"
DOWNLOAD_META_URL = "https://api.weixin.qq.com/tcb/batchdownloadfile"


def mp_cfg(config: dict) -> dict:
    return (config or {}).get("miniprogram", {}) or {}


def get_access_token(config: dict) -> str:
    """取小程序 access_token（带本地文件缓存，提前 5 分钟过期）。"""
    mp = mp_cfg(config)
    app_id = mp.get("app_id", "")
    app_secret = mp.get("app_secret", "")
    if not (app_id and app_secret):
        raise RuntimeError("miniprogram.app_id / app_secret 未配置")

    cache_file = Path(mp.get("token_cache_file", ".miniprogram_token_cache.json"))
    if cache_file.exists():
        try:
            c = json.loads(cache_file.read_text(encoding="utf-8"))
            if c.get("app_id") == app_id and c.get("expire_at", 0) - 300 > time.time():
                return c["access_token"]
        except Exception:
            pass

    resp = requests.get(TOKEN_URL, params={
        "grant_type": "client_credential",
        "appid": app_id,
        "secret": app_secret,
    }, timeout=15)
    data = resp.json()
    token = data.get("access_token")
    if not token:
        raise RuntimeError(f"获取 access_token 失败: {data}")
    try:
        cache_file.write_text(json.dumps({
            "app_id": app_id,
            "access_token": token,
            "expire_at": time.time() + int(data.get("expires_in", 7200)),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return token


def upload(env: str, token: str, cloud_path: str, content: bytes) -> str:
    """上传单个文件到云存储，返回 cloud:// file_id。"""
    # 1. 申请上传：拿到 COS 直传地址与鉴权
    meta = requests.post(
        UPLOAD_META_URL, params={"access_token": token},
        json={"env": env, "path": cloud_path}, timeout=20,
    ).json()
    if meta.get("errcode"):
        raise RuntimeError(f"uploadfile 申请失败 ({cloud_path}): {meta}")

    # 2. 直传 COS（multipart，file 字段必须放最后）
    form = [
        ("key", (None, cloud_path)),
        ("Signature", (None, meta["authorization"])),
        ("x-cos-security-token", (None, meta["token"])),
        ("x-cos-meta-fileid", (None, meta["cos_file_id"])),
        ("file", ("file", content)),
    ]
    r = requests.post(meta["url"], files=form, timeout=60)
    if r.status_code not in (200, 204):
        raise RuntimeError(f"COS 上传失败 ({cloud_path}) HTTP {r.status_code}: {r.text[:200]}")
    return meta["file_id"]


def download(env: str, token: str, fileid: str, timeout: float = 120) -> bytes:
    """按 fileID 从云存储下载，返回字节。"""
    meta = requests.post(
        DOWNLOAD_META_URL, params={"access_token": token},
        json={"env": env, "file_list": [{"fileid": fileid, "max_age": 7200}]},
        timeout=20,
    ).json()
    if meta.get("errcode"):
        raise RuntimeError(f"batchdownloadfile 失败: {meta}")
    fl = meta.get("file_list") or []
    if not fl or fl[0].get("status") != 0 or not fl[0].get("download_url"):
        raise RuntimeError(f"下载地址获取失败: {meta}")
    r = requests.get(fl[0]["download_url"], timeout=timeout)
    r.raise_for_status()
    return r.content
