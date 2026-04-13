"""WeChat Official Account (微信公众号) publisher.

Implements the WeChat MP API workflow:
1. Obtain / refresh access_token
2. Convert Markdown → HTML
3. Create a draft article (草稿)
4. Optionally publish the draft

Docs: https://developers.weixin.qq.com/doc/offiaccount/Draft_Box/
"""
from __future__ import annotations

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

# Refresh the access token this many seconds before it actually expires
TOKEN_EXPIRY_BUFFER_SECONDS = 60


class WeChatPublisher:
    """Wraps WeChat Official Account draft + publish API."""

    def __init__(self, config: dict) -> None:
        self.app_id = config.get("app_id", "")
        self.app_secret = config.get("app_secret", "")
        self.author = config.get("author", "大雄看点映")
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

    def create_draft(self, title: str, html_content: str,
                     digest: str = "") -> str:
        """Upload a draft article; return the media_id of the draft."""
        token = self.get_access_token()
        article = {
            "title": title,
            "author": self.author,
            "digest": digest or title,
            "content": html_content,
            "content_source_url": "",
            "need_open_comment": 0,
            "only_fans_can_comment": 0,
        }
        if self.thumb_media_id:
            article["thumb_media_id"] = self.thumb_media_id

        payload = {"articles": [article]}
        resp = requests.post(
            DRAFT_ADD_URL,
            params={"access_token": token},
            json=payload,
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
        resp = requests.post(
            FREEPUBLISH_URL,
            params={"access_token": token},
            json={"media_id": media_id},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("errcode", 0) != 0:
            raise RuntimeError(f"WeChat publish failed: {data}")

        publish_id = data.get("publish_id", "")
        logger.info("Article published: publish_id=%s", publish_id)
        return publish_id

    # ── High-level convenience ────────────────────────────────────────────────

    def publish_markdown(self, title: str, md_content: str,
                         digest: str = "",
                         as_draft_only: bool = True) -> dict:
        """Convert Markdown and publish to WeChat.

        Args:
            title: Article title.
            md_content: Full Markdown content.
            digest: Short summary (auto-generated from title if empty).
            as_draft_only: If True, only create a draft (no public publish).

        Returns:
            dict with 'media_id' and optionally 'publish_id'.
        """
        html = self.markdown_to_html(md_content)
        media_id = self.create_draft(title, html, digest=digest)
        result: dict = {"media_id": media_id, "status": "draft"}

        if not as_draft_only:
            publish_id = self.publish_draft(media_id)
            result["publish_id"] = publish_id
            result["status"] = "published"

        return result
