"""Tests for the Flask review and publish application."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import app as app_module
from app import app as flask_app, _safe_filename


@pytest.fixture
def client(tmp_path):
    """Configure app with a temporary output directory."""
    app_module.OUTPUT_DIR = tmp_path
    flask_app.config["TESTING"] = True
    flask_app.secret_key = b"test-secret"
    with flask_app.test_client() as c:
        yield c


class TestSafeFilename:
    def test_valid_filename(self):
        assert _safe_filename("briefing_001_2026-04-13.md") == "briefing_001_2026-04-13.md"

    def test_path_traversal_rejected(self):
        with pytest.raises(ValueError):
            _safe_filename("../../etc/passwd")

    def test_arbitrary_md_rejected(self):
        with pytest.raises(ValueError):
            _safe_filename("secrets.md")

    def test_missing_extension_rejected(self):
        with pytest.raises(ValueError):
            _safe_filename("briefing_001_2026-04-13")

    def test_extra_path_separator_rejected(self):
        with pytest.raises(ValueError):
            _safe_filename("subdir/briefing_001_2026-04-13.md")


class TestIndexRoute:
    def test_index_200(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert "大雄看点映" in resp.data.decode()

    def test_index_shows_briefings(self, client, tmp_path):
        (tmp_path / "briefing_001_2026-04-13.md").write_text("# Test", encoding="utf-8")
        resp = client.get("/")
        assert b"briefing_001_2026-04-13.md" in resp.data


class TestReviewRoute:
    def test_review_existing_file(self, client, tmp_path):
        (tmp_path / "briefing_001_2026-04-13.md").write_text("# Hello", encoding="utf-8")
        resp = client.get("/review/briefing_001_2026-04-13.md")
        assert resp.status_code == 200
        assert b"Hello" in resp.data

    def test_review_invalid_filename_redirects(self, client):
        resp = client.get("/review/../../etc/passwd")
        assert resp.status_code in (301, 302, 404)

    def test_review_missing_file_redirects(self, client):
        resp = client.get("/review/briefing_999_2026-04-13.md")
        assert resp.status_code in (302, 404)


class TestSaveRoute:
    def test_save_creates_file(self, client, tmp_path):
        (tmp_path / "briefing_001_2026-04-13.md").write_text("# Original", encoding="utf-8")
        resp = client.post(
            "/save/briefing_001_2026-04-13.md",
            data={"content": "# Updated"},
        )
        assert resp.status_code in (302, 200)
        assert (tmp_path / "briefing_001_2026-04-13.md").read_text() == "# Updated"

    def test_save_invalid_filename_rejected(self, client):
        resp = client.post("/save/../../evil.md", data={"content": "evil"})
        assert resp.status_code in (301, 302, 404)


class TestPreviewRoute:
    def test_preview_returns_html(self, client):
        resp = client.post(
            "/preview_html",
            json={"content": "# 标题\n\n段落文字"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert "html" in data
        assert "<h1>" in data["html"]


class TestDeleteRoute:
    def test_delete_removes_file(self, client, tmp_path):
        f = tmp_path / "briefing_001_2026-04-13.md"
        f.write_text("# Delete me", encoding="utf-8")
        resp = client.post("/delete/briefing_001_2026-04-13.md")
        assert resp.status_code in (302, 200)
        assert not f.exists()

    def test_delete_invalid_filename_rejected(self, client):
        resp = client.post("/delete/../config.yaml")
        assert resp.status_code in (301, 302, 404)
