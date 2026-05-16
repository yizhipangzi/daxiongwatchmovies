"""WeChat Official Account (微信公众号) publisher.

Implements the WeChat MP API workflow:
1. Obtain / refresh access_token
2. Upload referenced images (auto-uploads collages/posters from briefing MDs)
3. Convert Markdown → HTML and rewrite local img src to WeChat-hosted URLs
4. Create a draft article (草稿) — auto-picks a cover thumb if none configured
5. Optionally publish the draft

Docs: https://developers.weixin.qq.com/doc/offiaccount/Draft_Box/
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import markdown
import requests

logger = logging.getLogger(__name__)

TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/token"
DRAFT_ADD_URL = "https://api.weixin.qq.com/cgi-bin/draft/add"
DRAFT_GET_URL = "https://api.weixin.qq.com/cgi-bin/draft/get"
FREEPUBLISH_URL = "https://api.weixin.qq.com/cgi-bin/freepublish/submit"
# Permanent material (used for cover thumb, which becomes the article's
# preview image). thumb files have a 64KB hard limit on WeChat's side.
MATERIAL_ADD_URL = "https://api.weixin.qq.com/cgi-bin/material/add_material"
# Image upload for embedding inside article content; returns a wx URL.
CONTENT_IMG_UPLOAD_URL = "https://api.weixin.qq.com/cgi-bin/media/uploadimg"

# Refresh the access token this many seconds before it actually expires
TOKEN_EXPIRY_BUFFER_SECONDS = 60


class WeChatPublisher:
    """Wraps WeChat Official Account draft + publish API."""

    def __init__(self, config: dict) -> None:
        self.app_id = config.get("app_id", "")
        self.app_secret = config.get("app_secret", "")
        self.author = config.get("author", "大熊看点映")
        self.thumb_media_id = config.get("default_thumb_media_id", "")
        self._cache_file = Path(config.get("token_cache_file",
                                           ".wechat_token_cache.json"))
        self._access_token: str = ""
        self._token_expires_at: float = 0.0

    # ── Access Token ──────────────────────────────────────────────────────────

    def _load_cached_token(self) -> bool:
        """Load token from disk cache; return True if still valid."""
        if not self._cache_file.exists():
            return False
        try:
            data = json.loads(self._cache_file.read_text())
            if time.time() < data.get("expires_at", 0) - TOKEN_EXPIRY_BUFFER_SECONDS:
                self._access_token = data["access_token"]
                self._token_expires_at = data["expires_at"]
                return True
        except (json.JSONDecodeError, KeyError):
            pass
        return False

    def _save_token(self) -> None:
        self._cache_file.write_text(json.dumps({
            "access_token": self._access_token,
            "expires_at": self._token_expires_at,
        }))

    def get_access_token(self) -> str:
        """Return a valid access token, refreshing if necessary."""
        if self._access_token and time.time() < self._token_expires_at - TOKEN_EXPIRY_BUFFER_SECONDS:
            return self._access_token
        if self._load_cached_token():
            return self._access_token

        if not self.app_id or not self.app_secret:
            raise ValueError(
                "WeChat app_id and app_secret must be configured in config.yaml"
            )

        resp = requests.get(TOKEN_URL, params={
            "grant_type": "client_credential",
            "appid": self.app_id,
            "secret": self.app_secret,
        }, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if "errcode" in data and data["errcode"] != 0:
            raise RuntimeError(f"WeChat token error: {data}")

        self._access_token = data["access_token"]
        self._token_expires_at = time.time() + data.get("expires_in", 7200)
        self._save_token()
        logger.info("WeChat access token refreshed.")
        return self._access_token

    # ── Image upload ──────────────────────────────────────────────────────────

    def upload_content_image(self, image_path: Path) -> str:
        """Upload an image for embedding in article content; returns WeChat URL."""
        token = self.get_access_token()
        with image_path.open("rb") as f:
            files = {"media": (image_path.name, f, "image/jpeg")}
            resp = requests.post(
                CONTENT_IMG_UPLOAD_URL,
                params={"access_token": token},
                files=files,
                timeout=30,
            )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"WeChat content image upload failed: {data}")
        url = data.get("url", "")
        logger.info("Content image uploaded: %s → %s", image_path.name, url)
        return url

    def upload_thumb_material(self, image_path: Path) -> str:
        """Upload an image as a permanent thumb material; returns media_id.

        WeChat enforces a 64KB hard limit on thumb files, so the image is
        resized + recompressed via Pillow before upload.
        """
        token = self.get_access_token()
        thumb_bytes = self._resize_for_thumb(image_path)

        files = {"media": (image_path.stem + ".jpg", thumb_bytes, "image/jpeg")}
        resp = requests.post(
            MATERIAL_ADD_URL,
            params={"access_token": token, "type": "thumb"},
            files=files,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"WeChat thumb upload failed: {data}")
        media_id = data.get("media_id", "")
        logger.info("Thumb uploaded: %s → media_id=%s", image_path.name, media_id)
        return media_id

    @staticmethod
    def _resize_for_thumb(image_path: Path,
                          max_bytes: int = 60_000) -> bytes:
        """Resize/recompress an image to fit under WeChat's 64KB thumb limit."""
        try:
            from PIL import Image
        except ImportError as exc:
            raise RuntimeError(
                "Pillow is required to upload thumb images. pip install Pillow"
            ) from exc

        img = Image.open(image_path)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        # Start with a moderate size and reduce until under the limit.
        for max_dim, quality in [
            (900, 85), (720, 80), (600, 78), (480, 75), (360, 70), (300, 65),
        ]:
            work = img.copy()
            work.thumbnail((max_dim, max_dim), Image.LANCZOS)
            buf = io.BytesIO()
            work.save(buf, "JPEG", quality=quality, optimize=True)
            if len(buf.getvalue()) <= max_bytes:
                return buf.getvalue()
        # Last resort: aggressive shrink
        work = img.copy()
        work.thumbnail((240, 240), Image.LANCZOS)
        buf = io.BytesIO()
        work.save(buf, "JPEG", quality=60, optimize=True)
        return buf.getvalue()

    # ── Markdown → HTML ───────────────────────────────────────────────────────

    @staticmethod
    def markdown_to_html(md_content: str) -> str:
        """Convert Markdown to WeChat-compatible HTML."""
        html = markdown.markdown(
            md_content,
            extensions=["tables", "fenced_code", "nl2br"],
        )
        # WeChat does not support <h1>/<h2> well — convert to styled <p>
        html = re.sub(
            r"<h1>(.*?)</h1>",
            r'<p style="font-size:1.6em;font-weight:bold;text-align:center;">\1</p>',
            html,
        )
        html = re.sub(
            r"<h2>(.*?)</h2>",
            r'<p style="font-size:1.3em;font-weight:bold;border-left:4px solid #e8ac48;'
            r'padding-left:8px;margin-top:1em;">\1</p>',
            html,
        )
        html = re.sub(
            r"<h3>(.*?)</h3>",
            r'<p style="font-size:1.1em;font-weight:bold;">\1</p>',
            html,
        )
        # Wrap in a mobile-friendly container
        return (
            '<div style="font-family:-apple-system,BlinkMacSystemFont,'
            "'PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;"
            'font-size:15px;line-height:1.8;color:#333;max-width:680px;margin:0 auto;">'
            + html
            + "</div>"
        )

    # ── Draft API ─────────────────────────────────────────────────────────────

    @staticmethod
    def _truncate_utf8(text: str, max_bytes: int) -> str:
        """Truncate ``text`` so its UTF-8 byte length does not exceed
        ``max_bytes``. Cuts on a character boundary, never mid-codepoint."""
        if not text:
            return text
        encoded = text.encode("utf-8")
        if len(encoded) <= max_bytes:
            return text
        # Walk back until we find a clean codepoint boundary
        b = max_bytes
        while b > 0:
            try:
                return encoded[:b].decode("utf-8")
            except UnicodeDecodeError:
                b -= 1
        return ""

    def create_draft(self, title: str, html_content: str,
                     digest: str = "") -> str:
        """Upload a draft article; return the media_id of the draft."""
        token = self.get_access_token()
        # WeChat MP API enforces byte-length limits on these fields.
        # Empirical limits (UTF-8 bytes): author ≤ 8, title ≤ 64, digest ≤ 120.
        safe_author = self._truncate_utf8(self.author, 8)
        if safe_author != self.author:
            logger.warning(
                "WeChat author '%s' exceeds 8-byte limit — truncated to '%s'",
                self.author, safe_author,
            )
        safe_title = self._truncate_utf8(title, 64)
        safe_digest = self._truncate_utf8(digest or title, 120)

        article = {
            "title": safe_title,
            "author": safe_author,
            "digest": safe_digest,
            "content": html_content,
            "content_source_url": "",
            "need_open_comment": 0,
            "only_fans_can_comment": 0,
        }
        if self.thumb_media_id:
            article["thumb_media_id"] = self.thumb_media_id

        payload = {"articles": [article]}
        # IMPORTANT: WeChat MP API stores raw text. requests' default ``json=``
        # uses ``ensure_ascii=True`` which escapes every CJK char as ``\uXXXX``.
        # WeChat then saves those literal escape sequences as text instead of
        # decoding them. Send UTF-8 bytes with ``ensure_ascii=False`` instead.
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        resp = requests.post(
            DRAFT_ADD_URL,
            params={"access_token": token},
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"WeChat draft creation failed: {data}")

        media_id = data.get("media_id", "")
        logger.info("Draft created: media_id=%s", media_id)
        return media_id

    def publish_draft(self, media_id: str) -> str:
        """Submit a draft for free-publish; return publish_id."""
        token = self.get_access_token()
        body = json.dumps({"media_id": media_id}, ensure_ascii=False).encode("utf-8")
        resp = requests.post(
            FREEPUBLISH_URL,
            params={"access_token": token},
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"WeChat publish failed: {data}")

        publish_id = data.get("publish_id", "")
        logger.info("Article published: publish_id=%s", publish_id)
        return publish_id

    # ── Image-aware HTML preparation ──────────────────────────────────────────

    _LOCAL_IMG_RE = re.compile(
        r'(<img\b[^>]*\bsrc=")([^":/][^"]*)(")', flags=re.IGNORECASE,
    )

    def _resolve_local_image(self, src: str,
                             image_base_dir: Path) -> Optional[Path]:
        """Resolve a relative img src to a local Path within image_base_dir."""
        if not src or src.startswith(("http://", "https://", "data:", "//")):
            return None
        # Strip any leading "./" or "/"
        clean = src.lstrip("/").replace("\\", "/")
        candidate = (image_base_dir / clean).resolve()
        try:
            candidate.relative_to(image_base_dir.resolve())
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def upload_and_rewrite_html(self, html: str,
                                image_base_dir: Path) -> tuple[str, list[Path]]:
        """Upload every locally-referenced image in ``html`` to WeChat and
        rewrite each ``src`` to the WeChat-hosted URL.

        Returns ``(new_html, uploaded_local_paths)`` so the caller can pick a
        thumb candidate from the uploaded images.
        """
        uploaded: dict[str, str] = {}            # local src str → wx url
        uploaded_paths: list[Path] = []           # in order of first appearance

        def _sub(match: re.Match) -> str:
            prefix, src, suffix = match.group(1), match.group(2), match.group(3)
            if src in uploaded:
                return f"{prefix}{uploaded[src]}{suffix}"
            local = self._resolve_local_image(src, image_base_dir)
            if local is None:
                return match.group(0)
            try:
                wx_url = self.upload_content_image(local)
            except Exception as exc:
                logger.warning("Image upload failed for %s: %s", local, exc)
                return match.group(0)
            uploaded[src] = wx_url
            uploaded_paths.append(local)
            return f"{prefix}{wx_url}{suffix}"

        new_html = self._LOCAL_IMG_RE.sub(_sub, html)
        return new_html, uploaded_paths

    # ── High-level convenience ────────────────────────────────────────────────

    def publish_markdown(self, title: str, md_content: str,
                         digest: str = "",
                         as_draft_only: bool = True,
                         image_base_dir: Optional[Path] = None) -> dict:
        """Convert Markdown and publish to WeChat.

        Args:
            title: Article title.
            md_content: Full Markdown content.
            digest: Short summary (auto-generated from title if empty).
            as_draft_only: If True, only create a draft (no public publish).
            image_base_dir: Directory used to resolve relative ``<img src>``
                references in the briefing MD. When provided, every local
                image is uploaded to WeChat and the HTML src is rewritten;
                the first successfully uploaded image is also used as the
                article's cover thumb if ``default_thumb_media_id`` is not
                configured.

        Returns:
            dict with 'media_id' and optionally 'publish_id'.
        """
        html = self.markdown_to_html(md_content)

        uploaded_images: list[Path] = []
        if image_base_dir is not None:
            html, uploaded_images = self.upload_and_rewrite_html(html, image_base_dir)

        # WeChat requires a thumb_media_id on every draft. If the user didn't
        # configure one, auto-upload the first image we've already pulled.
        thumb_media_id = self.thumb_media_id
        if not thumb_media_id and uploaded_images:
            try:
                thumb_media_id = self.upload_thumb_material(uploaded_images[0])
            except Exception as exc:
                logger.warning("Auto thumb upload failed: %s", exc)
        if not thumb_media_id:
            raise RuntimeError(
                "WeChat 要求每篇草稿都有封面缩略图 (thumb_media_id)。"
                "请在 config.yaml 的 wechat.default_thumb_media_id 填一个有效 media_id，"
                "或确保 MD 中至少有一张本地图片（如 collages/...）。"
            )

        # Temporarily swap in the resolved thumb for this draft creation
        original_thumb = self.thumb_media_id
        self.thumb_media_id = thumb_media_id
        try:
            media_id = self.create_draft(title, html, digest=digest)
        finally:
            self.thumb_media_id = original_thumb

        result: dict = {"media_id": media_id, "status": "draft"}

        if not as_draft_only:
            publish_id = self.publish_draft(media_id)
            result["publish_id"] = publish_id
            result["status"] = "published"

        return result
