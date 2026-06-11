"""WeChat briefing markdown templates.

Edit this file to change wording, layout, emoji, section labels, or formatting
of the briefing output. The actual data loading and rendering logic lives in
``briefing.py`` — this file only defines the visible text.

Format strings use ``str.format()`` placeholders (``{name}``); make sure to
keep the placeholders intact when editing.

Available placeholders per template are listed in their docstrings below.
"""
from __future__ import annotations


# ── Date format used in the top heading ─────────────────────────────────────
# strftime spec, e.g. "%Y年%m月%d日" → "2026年05月11日"
DATE_FORMAT = "%Y年%m月%d日"


# ── Top of the briefing ────────────────────────────
BRIEFING_HEADER = """\
> 东京华人电影周报 · 连锁院线 / 独立影院
"""


# ── Section labels ──────────────────────────────────────────────────────────
# Order in this dict determines the order in which sections appear in the MD.
# Each value is (heading, subtitle).
SECTION_LABELS: dict[str, tuple[str, str]] = {
    "院线热映": ("🎬 院线热映", "最新院线大片推荐"),
    "电影日和": ("🌸 电影日和", "豆瓣暂无评分，日本观众好评的电影"),
    "院线经典": ("🎞️ 院线经典", "经典老片重映"),
    "小众佳片": ("🎨 小众佳片", "独立艺术影院的高分好片"),
}

# Section heading template.
# Placeholders: heading, subtitle
SECTION_HEADER = """\
---

## {heading}

*{subtitle}*
"""

# Shown in a section when no movies match the criteria
SECTION_EMPTY_PLACEHOLDER = "（暂无符合条件的电影）"


# ── Per-movie block ─────────────────────────────────────────────────────────
# Placeholders:
#   rank       — 1, 2, 3 ... within the section
#   name_link  — "[中文名](douban_url)" or plain "中文名" if no URL
#   orig_link  — "[原题](eiga_url)" or plain "原题" if no URL
#   stats_line — see STATS_* templates below
#   director, cast, genre — comma-separated strings (or "-")
#   theaters   — clickable theater list (or "-"))
MOVIE_BLOCK = """\

**{rank}. {name_link}（{orig_link}）**

⭐ {stats_line}
👤 导演 {director}
🎭 主演 {cast}
🏷️ 类型 {genre}
📍 上映影院
{theaters}\
"""

# Header for the reviews block — varies by data source.
REVIEW_HEADING_DOUBAN = "豆瓣短评"
REVIEW_HEADING_EIGA = "eiga.com 短评（自动翻译）"

# Per-review line variant for eiga 电影日和: shows the reviewer's 1-5 star
# rating and pairs the translated text with a short snippet of the original
# Japanese so readers who know JP can spot translation glitches.
#   Placeholders: rating, text_zh, text_ja, author
EIGA_REVIEW_LINE = " {rating}★ {text_zh} -{author}"
# When translation failed / wasn't configured — show original JP.
EIGA_REVIEW_LINE_JA_ONLY = " {rating}★ {text_ja} -{author}"


# ── Stats line variants (used inside MOVIE_BLOCK) ──────────────────────────
# When the movie has an aggregated Douban score.
#   Placeholders: score, watched, want
STATS_WITH_SCORE = "{score:.1f}分 / {watched}看过 / {want}想看"

# Fallback when there is no aggregated Douban score.
#   Placeholders: watched, want
STATS_NO_DATA = "-分 / {watched}看过 / {want}想看"

# 电影日和 板块：用 eiga.com 的日本观众评分代替豆瓣分。
#   Placeholders: rating, count
STATS_WITH_EIGA_RATING = "eiga.com {rating:.1f}/5（{count}人评价）"


# ── Short review lines (used inside MOVIE_BLOCK's reviews block) ───────────
# Per-review line.
#   Placeholders: text, author
REVIEW_LINE = " {text} -{author}"

# Shown when there are zero qualifying reviews.
REVIEW_NONE = " (暂无短评)"

# Default name when a reviewer has no display name.
REVIEW_ANONYMOUS = "匿名"


# ── Misc separators / placeholders ─────────────────────────────────────────
# Joins multiple theater links in the 影院 line
THEATER_SEP = " · "
# Shown when no theaters are recorded
NO_THEATERS = "-"
# Default placeholder when 导演/主演/类型 is missing
EMPTY_FIELD = "-"
