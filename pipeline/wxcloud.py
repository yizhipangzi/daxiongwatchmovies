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

# stable_token：多端/多次调用返回同一个有效 token，不会互相失效（cgi-bin/token
# 每次取都让上一个失效，cloud VM 与本地会互相踢掉对方的 token → 40001）。
STABLE_TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/stable_token"
UPLOAD_META_URL = "https://api.weixin.qq.com/tcb/uploadfile"
DOWNLOAD_META_URL = "https://api.weixin.qq.com/tcb/batchdownloadfile"
DELETE_URL = "https://api.weixin.qq.com/tcb/batchdeletefile"

# 海外 VM ↔ 腾讯云存储是跨境，慢且偶发卡断。给足超时 + 重试。
_RETRIES = 3
_META_TIMEOUT = 30        # 申请上传/下载地址、取 token
_TRANSFER_TIMEOUT = 300   # 实际传文件（几 MB 跨境）


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
            # 只认 stable_token 缓存；旧的 cgi-bin/token 缓存（无 kind）忽略并重取，
            # 这样换接口后各机器自动失效旧缓存，不用手动删。
            if (c.get("app_id") == app_id and c.get("kind") == "stable"
                    and c.get("expire_at", 0) - 300 > time.time()):
                return c["access_token"]
        except Exception:
            pass

    # 取 token 本身也走公网，跨境偶发抖动一样会打中它——之前这里没有重试，
    # 单次超时/连接失败就直接把整个同步（进而整个 step1）带崩。补上和
    # upload/download 一致的重试。
    last = None
    data = None
    for attempt in range(1, _RETRIES + 1):
        try:
            resp = requests.post(STABLE_TOKEN_URL, json={
                "grant_type": "client_credential",
                "appid": app_id,
                "secret": app_secret,
                "force_refresh": False,
            }, timeout=_META_TIMEOUT)
            data = resp.json()
            if not data.get("access_token"):
                raise RuntimeError(f"获取 access_token 失败: {data}")
            break
        except Exception as exc:
            last = exc
            data = None
            logger.warning("获取 access_token 第 %d/%d 次失败: %s", attempt, _RETRIES, exc)
            if attempt < _RETRIES:
                time.sleep(5 * attempt)
    if data is None:
        raise RuntimeError(f"获取 access_token 重试 {_RETRIES} 次仍失败: {last}")
    token = data["access_token"]
    try:
        cache_file.write_text(json.dumps({
            "app_id": app_id,
            "kind": "stable",
            "access_token": token,
            "expire_at": time.time() + int(data.get("expires_in", 7200)),
        }, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass
    return token


def upload(env: str, token: str, cloud_path: str, content: bytes) -> str:
    """上传单个文件到云存储，返回 cloud:// file_id。跨境慢/卡断时自动重试。"""
    last = None
    for attempt in range(1, _RETRIES + 1):
        try:
            # 1. 申请上传：拿到 COS 直传地址与鉴权
            meta = requests.post(
                UPLOAD_META_URL, params={"access_token": token},
                json={"env": env, "path": cloud_path}, timeout=_META_TIMEOUT,
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
            r = requests.post(meta["url"], files=form, timeout=_TRANSFER_TIMEOUT)
            if r.status_code not in (200, 204):
                raise RuntimeError(
                    f"COS 上传失败 ({cloud_path}) HTTP {r.status_code}: {r.text[:200]}")
            return meta["file_id"]
        except Exception as exc:
            last = exc
            logger.warning("云存储上传第 %d/%d 次失败 (%s): %s",
                           attempt, _RETRIES, cloud_path, exc)
            if attempt < _RETRIES:
                time.sleep(5 * attempt)
    raise RuntimeError(f"云存储上传重试 {_RETRIES} 次仍失败 ({cloud_path}): {last}")


def delete(env: str, token: str, fileid_list: list) -> dict:
    """批量删除云存储文件。不存在的 fileid 会在返回里标错误，但不影响整批。"""
    if not fileid_list:
        return {}
    r = requests.post(
        DELETE_URL, params={"access_token": token},
        json={"env": env, "fileid_list": list(fileid_list)}, timeout=_META_TIMEOUT,
    )
    return r.json()


def download(env: str, token: str, fileid: str,
             timeout: float = _TRANSFER_TIMEOUT) -> bytes:
    """按 fileID 从云存储下载，返回字节。跨境慢/卡断时自动重试。"""
    last = None
    for attempt in range(1, _RETRIES + 1):
        try:
            meta = requests.post(
                DOWNLOAD_META_URL, params={"access_token": token},
                json={"env": env, "file_list": [{"fileid": fileid, "max_age": 7200}]},
                timeout=_META_TIMEOUT,
            ).json()
            if meta.get("errcode"):
                raise RuntimeError(f"batchdownloadfile 失败: {meta}")
            fl = meta.get("file_list") or []
            if not fl or fl[0].get("status") != 0 or not fl[0].get("download_url"):
                raise RuntimeError(f"下载地址获取失败: {meta}")
            r = requests.get(fl[0]["download_url"], timeout=timeout)
            r.raise_for_status()
            return r.content
        except Exception as exc:
            last = exc
            logger.warning("云存储下载第 %d/%d 次失败: %s", attempt, _RETRIES, exc)
            if attempt < _RETRIES:
                time.sleep(5 * attempt)
    raise RuntimeError(f"云存储下载重试 {_RETRIES} 次仍失败: {last}")
