#!/usr/bin/env python3
"""app.py — 大雄看点映 审核发布界面

提供一个本地 Flask Web 应用，用于：
1. 浏览已生成的简报 Markdown
2. 在线编辑内容
3. 一键发布到微信公众号（草稿或直接发布）

用法:
  python app.py              # 启动服务（默认 http://127.0.0.1:5000）
  python app.py --port 8080  # 指定端口
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import markdown
import yaml
from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify,
)

sys.path.insert(0, str(Path(__file__).parent))
from publisher.wechat import WeChatPublisher

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("app")

app = Flask(__name__)
app.secret_key = os.urandom(24)


# ── Config ────────────────────────────────────────────────────────────────────

def load_config(path: str = "config.yaml") -> dict:
    p = Path(path)
    if not p.exists():
        p = Path("config.yaml.example")
    if p.exists():
        with p.open(encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    return {}


CONFIG: dict = {}
OUTPUT_DIR: Path = Path("output")


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_filename(filename: str) -> str:
    """Validate and sanitize a briefing filename.

    Only filenames that match the expected pattern are accepted, preventing
    path-traversal attacks (e.g. '../../etc/passwd').
    Raises ValueError for invalid filenames.
    """
    # Allow only: briefing_NNN_YYYY-MM-DD.md (and nothing else)
    if not re.fullmatch(r"briefing_\d+_\d{4}-\d{2}-\d{2}\.md", filename):
        raise ValueError(f"Invalid briefing filename: {filename!r}")
    return filename


def list_briefings() -> list[dict]:
    """List all generated briefing files, newest first."""
    files = sorted(OUTPUT_DIR.glob("briefing_*.md"), reverse=True)
    result = []
    for f in files:
        stat = f.stat()
        result.append({
            "filename": f.name,
            "path": str(f),
            "size_kb": round(stat.st_size / 1024, 1),
            "modified": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return result


def read_briefing(filename: str) -> str:
    path = OUTPUT_DIR / filename
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def save_briefing(filename: str, content: str) -> None:
    path = OUTPUT_DIR / filename
    path.write_text(content, encoding="utf-8")


def extract_title(md_content: str) -> str:
    """Extract the H1 title from Markdown."""
    for line in md_content.splitlines():
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return "大雄看点映"


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    briefings = list_briefings()
    return render_template("index.html", briefings=briefings)


@app.route("/review/<filename>")
def review(filename: str):
    try:
        filename = _safe_filename(filename)
    except ValueError:
        flash("无效的文件名", "error")
        return redirect(url_for("index"))
    content = read_briefing(filename)
    if not content:
        flash(f"找不到文件: {filename}", "error")
        return redirect(url_for("index"))
    preview_html = markdown.markdown(
        content,
        extensions=["tables", "fenced_code", "nl2br"],
    )
    return render_template("review.html",
                           filename=filename,
                           content=content,
                           preview_html=preview_html)


@app.route("/save/<filename>", methods=["POST"])
def save(filename: str):
    try:
        filename = _safe_filename(filename)
    except ValueError:
        flash("无效的文件名", "error")
        return redirect(url_for("index"))
    content = request.form.get("content", "")
    save_briefing(filename, content)
    flash("✅ 保存成功！", "success")
    return redirect(url_for("review", filename=filename))


@app.route("/preview_html", methods=["POST"])
def preview_html():
    """AJAX endpoint: convert Markdown → HTML for live preview."""
    md_content = request.json.get("content", "")
    html = markdown.markdown(
        md_content,
        extensions=["tables", "fenced_code", "nl2br"],
    )
    return jsonify({"html": html})


@app.route("/publish/<filename>", methods=["GET", "POST"])
def publish(filename: str):
    try:
        filename = _safe_filename(filename)
    except ValueError:
        flash("无效的文件名", "error")
        return redirect(url_for("index"))
    content = read_briefing(filename)
    if not content:
        flash(f"找不到文件: {filename}", "error")
        return redirect(url_for("index"))

    wechat_cfg = CONFIG.get("wechat", {})
    is_configured = bool(
        wechat_cfg.get("app_id") and
        wechat_cfg.get("app_id") != "YOUR_WECHAT_APP_ID" and
        wechat_cfg.get("app_secret")
    )

    if request.method == "GET":
        title = extract_title(content)
        return render_template("publish.html",
                               filename=filename,
                               title=title,
                               is_configured=is_configured)

    # POST: perform publish
    title = request.form.get("title", extract_title(content))
    digest = request.form.get("digest", "")
    draft_only = request.form.get("draft_only", "true") == "true"

    if not is_configured:
        flash("❌ 微信公众号未配置，请先填写 config.yaml 中的 app_id 和 app_secret。", "error")
        return redirect(url_for("publish", filename=filename))

    try:
        publisher = WeChatPublisher(wechat_cfg)
        result = publisher.publish_markdown(
            title=title,
            md_content=content,
            digest=digest,
            as_draft_only=draft_only,
        )
        status = "草稿" if draft_only else "已发布"
        flash(
            f"✅ {status}成功！media_id: {result.get('media_id', '?')}"
            + (f"，publish_id: {result.get('publish_id', '')}" if not draft_only else ""),
            "success",
        )
        logger.info("Published %s → %s", filename, result)
    except Exception as exc:
        flash(f"❌ 发布失败: {exc}", "error")
        logger.exception("Publish failed for %s", filename)

    return redirect(url_for("publish", filename=filename))


@app.route("/delete/<filename>", methods=["POST"])
def delete(filename: str):
    try:
        filename = _safe_filename(filename)
    except ValueError:
        flash("无效的文件名", "error")
        return redirect(url_for("index"))
    path = OUTPUT_DIR / filename
    if path.exists():
        path.unlink()
        flash(f"已删除: {filename}", "info")
    return redirect(url_for("index"))


@app.route("/new")
def new_briefing():
    """Trigger a fresh scrape and redirect to the result."""
    flash("请使用命令行运行 python run_scraper.py 生成新简报，然后刷新此页面。", "info")
    return redirect(url_for("index"))


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    global CONFIG, OUTPUT_DIR

    parser = argparse.ArgumentParser(description="大雄看点映 审核发布界面")
    parser.add_argument("--port", type=int, default=0,
                        help="端口号（默认: 读取 config.yaml）")
    parser.add_argument("--host", default="",
                        help="主机地址（默认: 读取 config.yaml）")
    parser.add_argument("--config", default="config.yaml",
                        help="配置文件路径")
    args = parser.parse_args()

    CONFIG = load_config(args.config)
    OUTPUT_DIR = Path(CONFIG.get("generator", {}).get("output_dir", "output"))
    OUTPUT_DIR.mkdir(exist_ok=True)

    app_cfg = CONFIG.get("app", {})
    host = args.host or app_cfg.get("host", "127.0.0.1")
    port = args.port or app_cfg.get("port", 5000)
    debug = app_cfg.get("debug", False)

    # Use a configured secret key for session persistence across restarts
    configured_key = app_cfg.get("secret_key", "")
    app.secret_key = configured_key.encode() if configured_key else os.urandom(24)

    print(f"\n🎬  大雄看点映 审核发布界面")
    print(f"🌐  访问地址: http://{host}:{port}")
    print(f"📁  简报目录: {OUTPUT_DIR.resolve()}\n")

    app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
