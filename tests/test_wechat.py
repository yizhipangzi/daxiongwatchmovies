"""Tests for the WeChat publisher (no real API calls)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from publisher.wechat import WeChatPublisher

SAMPLE_MD = """# 大雄看点映 第1期｜4月13日 ~ 4月19日

## 本周上映

### 1. 可怜的东西

豆瓣评分：8.0
"""


class TestMarkdownToHtml:
    def test_basic_conversion(self):
        html = WeChatPublisher.markdown_to_html("# Hello\n\n**World**")
        assert "Hello" in html
        assert "World" in html

    def test_table_conversion(self):
        md = "| A | B |\n|---|---|\n| 1 | 2 |"
        html = WeChatPublisher.markdown_to_html(md)
        assert "<table" in html

    def test_wrapped_in_div(self):
        html = WeChatPublisher.markdown_to_html("Hello")
        assert html.startswith("<div")

    def test_h1_replaced(self):
        html = WeChatPublisher.markdown_to_html("# 大标题")
        # Should NOT contain plain <h1> tag
        assert "<h1>" not in html
        assert "大标题" in html

    def test_h2_replaced(self):
        html = WeChatPublisher.markdown_to_html("## 小节")
        assert "<h2>" not in html
        assert "小节" in html


class TestWeChatPublisher:
    def _publisher(self):
        return WeChatPublisher({
            "app_id": "test_app_id",
            "app_secret": "test_secret",
            "author": "大雄看点映",
            "default_thumb_media_id": "",
            "token_cache_file": "/tmp/test_wechat_token.json",
        })

    @patch("publisher.wechat.requests.get")
    def test_get_access_token(self, mock_get):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "access_token": "test_token_12345",
            "expires_in": 7200,
        }
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        pub = self._publisher()
        token = pub.get_access_token()
        assert token == "test_token_12345"

    @patch("publisher.wechat.requests.post")
    @patch("publisher.wechat.requests.get")
    def test_create_draft(self, mock_get, mock_post):
        # Mock token fetch
        token_resp = MagicMock()
        token_resp.json.return_value = {"access_token": "tok", "expires_in": 7200}
        token_resp.raise_for_status = MagicMock()
        mock_get.return_value = token_resp

        # Mock draft creation
        draft_resp = MagicMock()
        draft_resp.json.return_value = {"media_id": "MEDIA_123", "errcode": 0}
        draft_resp.raise_for_status = MagicMock()
        mock_post.return_value = draft_resp

        pub = self._publisher()
        media_id = pub.create_draft("测试文章", "<p>内容</p>")
        assert media_id == "MEDIA_123"

    @patch("publisher.wechat.requests.post")
    @patch("publisher.wechat.requests.get")
    def test_publish_markdown_draft_only(self, mock_get, mock_post):
        token_resp = MagicMock()
        token_resp.json.return_value = {"access_token": "tok", "expires_in": 7200}
        token_resp.raise_for_status = MagicMock()
        mock_get.return_value = token_resp

        draft_resp = MagicMock()
        draft_resp.json.return_value = {"media_id": "MEDIA_456", "errcode": 0}
        draft_resp.raise_for_status = MagicMock()
        mock_post.return_value = draft_resp

        pub = self._publisher()
        result = pub.publish_markdown("标题", SAMPLE_MD, as_draft_only=True)
        assert result["status"] == "draft"
        assert result["media_id"] == "MEDIA_456"
        assert "publish_id" not in result

    def test_missing_credentials_raises(self):
        pub = WeChatPublisher({"app_id": "", "app_secret": ""})
        with pytest.raises(ValueError, match="app_id"):
            pub.get_access_token()

    @patch("publisher.wechat.requests.post")
    @patch("publisher.wechat.requests.get")
    def test_api_error_raises(self, mock_get, mock_post):
        token_resp = MagicMock()
        token_resp.json.return_value = {"access_token": "tok", "expires_in": 7200}
        token_resp.raise_for_status = MagicMock()
        mock_get.return_value = token_resp

        draft_resp = MagicMock()
        draft_resp.json.return_value = {"errcode": 40001, "errmsg": "invalid credential"}
        draft_resp.raise_for_status = MagicMock()
        mock_post.return_value = draft_resp

        pub = self._publisher()
        with pytest.raises(RuntimeError, match="40001"):
            pub.create_draft("标题", "<p>内容</p>")
