"""Step 4: 导出小程序 JSON 并上传到微信云开发云存储.

从本地 data/eiga.db 生成 9 个按区拆分的 JSON（结构与小程序现有 data/{district}.js
一致，并补齐 README §四 预留的新字段：director / cast / doubanWatched /
doubanWish / doubanComment / showtimes），写到本地 output 目录，再通过云开发
服务端 HTTP API 上传到云存储。

step4 不在 run_scraper.py 内，单独运行（见 run_step4.py）。

云开发归属「小程序」这个 App，凭据与公众号不同，配置在 config.yaml 的
miniprogram 段（app_id / app_secret / cloud_env）。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = Path("data/eiga.db")

# 区 → eiga area_code(s)。区名/中文名沿用小程序现有 data/{district}.js。
# 顺序与 pages/district/district.js 的 LOADERS 一致。
DISTRICTS = [
    {"id": "shibuya",      "name": "渋谷",            "nameZh": "涩谷",            "areas": ["130301"]},
    {"id": "shinjuku",     "name": "新宿",            "nameZh": "新宿",            "areas": ["130201"]},
    {"id": "takadanobaba", "name": "高田馬場",        "nameZh": "高田马场",        "areas": ["130611"]},
    {"id": "ikebukuro",    "name": "池袋",            "nameZh": "池袋",            "areas": ["130501"]},
    {"id": "yurakucho",    "name": "有楽町・銀座・日本橋", "nameZh": "有乐町·银座·日本桥", "areas": ["130102", "130716", "130101"]},
    {"id": "roppongi",     "name": "六本木",          "nameZh": "六本木",          "areas": ["130401"]},
    {"id": "ebisu",        "name": "恵比寿・目黒",      "nameZh": "惠比寿·目黑",      "areas": ["130608", "130609"]},
    {"id": "suidobashi",   "name": "水道橋・飯田橋",     "nameZh": "水道桥·饭田桥",     "areas": ["130710"]},
    {"id": "ueno",         "name": "上野",            "nameZh": "上野",            "areas": ["130901"]},
]


# ── DB → JSON ─────────────────────────────────────────────────────────────────

def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _review_texts(short_reviews_json: Optional[str], n: int = 3) -> list[str]:
    """douban_details.short_reviews 是 [{text, author,...}] 的 JSON，取前 n 条文本。"""
    if not short_reviews_json:
        return []
    try:
        arr = json.loads(short_reviews_json)
    except Exception:
        return []
    out: list[str] = []
    if isinstance(arr, list):
        for item in arr:
            if isinstance(item, dict):
                t = (item.get("text") or "").strip()
            elif isinstance(item, str):
                t = item.strip()
            else:
                t = ""
            if t:
                out.append(t)
            if len(out) >= n:
                break
    return out


def _load_douban(conn: sqlite3.Connection) -> tuple[dict, dict]:
    """返回 (details_by_id, matches_by_id)，按 movie_id 索引。"""
    details: dict = {}
    if _table_exists(conn, "douban_details"):
        for r in conn.execute("SELECT * FROM douban_details"):
            details[r["movie_id"]] = dict(r)
    matches: dict = {}
    if _table_exists(conn, "douban_matches"):
        for r in conn.execute(
            "SELECT movie_id, title_cn, douban_score, douban_votes, "
            "director, \"cast\" AS cast, genre, verified FROM douban_matches"
        ):
            matches[r["movie_id"]] = dict(r)
    return details, matches


def _load_eiga_reviews(conn: sqlite3.Connection, n: int = 3) -> dict:
    """返回 {movie_id: [短评文本,...]}（每片最多 n 条）。豆瓣没匹配到时用作短评来源。

    优先译好的中文 (body_zh)，回退日文原文 (body_ja)，按 position 升序取前 n 条。
    """
    out: dict = {}
    if not _table_exists(conn, "eiga_reviews"):
        return out
    for r in conn.execute(
        "SELECT movie_id, body_zh, body_ja FROM eiga_reviews ORDER BY movie_id, position"
    ):
        mid = r["movie_id"]
        lst = out.setdefault(mid, [])
        if len(lst) >= n:
            continue
        txt = (r["body_zh"] or "").strip() or (r["body_ja"] or "").strip()
        if txt:
            lst.append(txt)
    return out


def _load_showtimes(conn: sqlite3.Connection) -> dict:
    """返回 {(movie_id, theater_id): [{time, movieType:[{type, typeTxt}]}, ...]}，
    只含系统当天(JST)的场次，时间升序。

    小程序按「今天看什么」展示，不处理日期维度，所以这里只抽取运行当天(JST)的
    场次。每个场次带上格式标签 movieType（字幕/吹替/IMAX 等，来自 showtimes.movie_type
    的 JSON）。showtimes 表里保留整周数据，过滤在此完成。
    """
    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone(timedelta(hours=9))).date().isoformat()
    out: dict = {}
    if not _table_exists(conn, "showtimes"):
        return out
    # movie_type 列是后加的：老库可能没有，按需探测，避免 SELECT 报错。
    cols = {r[1] for r in conn.execute("PRAGMA table_info(showtimes)")}
    has_type = "movie_type" in cols
    sel = ("SELECT movie_id, theater_id, start_time, ticket_url"
           + (", movie_type" if has_type else ""))
    for r in conn.execute(
        f"{sel} FROM showtimes WHERE show_date = ? ORDER BY start_time",
        (today,),
    ):
        movie_type: list = []
        if has_type and r["movie_type"]:
            try:
                raw = json.loads(r["movie_type"])
            except Exception:
                raw = []
            for x in raw if isinstance(raw, list) else []:
                if isinstance(x, dict) and x.get("type"):
                    movie_type.append({
                        "type": x["type"],
                        "typeTxt": x.get("type_txt") or x.get("typeTxt") or "",
                    })
        out.setdefault((r["movie_id"], r["theater_id"]), []).append({
            "time": r["start_time"],
            "movieType": movie_type,
            # 该场次是否可在线购票（有预约链接=可购）。名画座等无在线购票的场次为
            # false，小程序据此让该场次不可点击复制链接。
            "onlineTicket": bool(r["ticket_url"]),
        })
    return out


def _movie_obj(m: sqlite3.Row, theater_id: str,
               details: dict, matches: dict, showtimes: dict,
               eiga_reviews: dict) -> dict:
    # 安全取列：老库可能没有 cast_names / eiga_rating 等后加的列。
    cols = m.keys()
    def g(key):
        return m[key] if key in cols else None

    mid = m["movie_id"]
    dd = details.get(mid)
    dm = matches.get(mid)
    eiga_rating = g("eiga_rating")

    # 整体二选一：豆瓣匹配成功（有详情，或匹配到了 douban_id）→ 评分/想看/看过/
    # 类型/导演/演员/短评 全用豆瓣；没匹配到 → 全用 eiga 上的信息。
    # 一旦记下 douban_id 即视为已匹配（与 step2 的政策一致，不再以 verified 为准）。
    matched = bool(dd) or bool(dm and dm.get("douban_id"))

    if matched:
        d = dd or {}
        mt = dm or {}
        title_cn = d.get("title_cn") or mt.get("title_cn") or ""
        douban_score = d.get("douban_score") or mt.get("douban_score") or 0.0
        douban_votes = d.get("douban_votes") or mt.get("douban_votes") or 0
        director = d.get("director") or mt.get("director") or None
        cast = d.get("cast") or mt.get("cast") or None
        genre = d.get("genre") or mt.get("genre") or ""
        watched = d.get("watched") or 0
        wish = d.get("want_to_watch") or 0
        comments = _review_texts(d.get("short_reviews"), 3)  # 豆瓣短评，最多 3 条
    else:
        # 豆瓣没匹配到 → 用 eiga.com 信息（导演/演员为日文，短评为 eiga 短评，
        # 想看/看过/类型 eiga 无对应字段，留空）。评分走单独的 eigaRating 字段。
        title_cn = ""
        douban_score = 0.0
        douban_votes = 0
        director = g("director") or None       # eiga 日文导演
        cast = g("cast_names") or None          # eiga 日文演员
        genre = ""
        watched = 0
        wish = 0
        comments = eiga_reviews.get(mid, [])  # eiga 短评，最多 3 条

    # 海报：优先 eiga（movies.poster_url，step1 从 /movie/{id}/photo/ 抓的高清图），
    # eiga 没有时回退豆瓣海报（douban_details.poster_url，step2 从 #mainpic 抓）。
    poster = g("poster_url") or (dd.get("poster_url") if dd else None) or ""

    # sortScore：有豆瓣分用豆瓣分，否则用 eiga 评分(0-5)×2 折算到 10 分制。
    if douban_score and douban_score > 0:
        sort_score = round(float(douban_score), 1)
    elif eiga_rating:
        sort_score = round(float(eiga_rating) * 2, 1)
    else:
        sort_score = 0.0

    return {
        "id": mid,
        "titleJp": g("title_jp") or "",
        "titleCn": title_cn,
        "eigaRating": eiga_rating,
        "eigaRatingCount": g("eiga_rating_count"),
        "doubanScore": round(float(douban_score), 1) if douban_score else 0.0,
        "doubanVotes": douban_votes,
        "director": director,
        "cast": cast,
        "genre": genre,
        "duration": g("duration") or 0,
        "year": g("year") or 0,
        "country": g("country") or "",
        "doubanWatched": watched,
        "doubanWish": wish,
        "poster": poster,
        "doubanComments": comments,
        "showtimes": showtimes.get((mid, theater_id), []),
        "sortScore": sort_score,
    }


def build_district(conn: sqlite3.Connection, district: dict,
                   details: dict, matches: dict, showtimes: dict,
                   eiga_reviews: dict) -> dict:
    placeholders = ",".join("?" for _ in district["areas"])
    theaters_rows = conn.execute(
        f"SELECT theater_id, name FROM theaters WHERE area_code IN ({placeholders}) "
        f"ORDER BY name",
        district["areas"],
    ).fetchall()

    theaters_out = []
    for t in theaters_rows:
        movie_rows = conn.execute(
            "SELECT m.* FROM movies m JOIN screenings s ON s.movie_id = m.movie_id "
            "WHERE s.theater_id = ?",
            (t["theater_id"],),
        ).fetchall()
        if not movie_rows:
            continue  # 该影院当前无上映电影 → 不收录
        movies = [_movie_obj(m, t["theater_id"], details, matches, showtimes, eiga_reviews)
                  for m in movie_rows]
        # 今天(JST)在该影院没有场次的电影不输出（showtimes 已是当天一维数组）
        movies = [mv for mv in movies if mv["showtimes"]]
        if not movies:
            continue  # 该影院今天没有任何排片 → 不收录
        movies.sort(key=lambda x: x["sortScore"], reverse=True)
        theaters_out.append({
            "id": t["theater_id"],
            "name": t["name"],
            "movies": movies,
        })

    return {
        "id": district["id"],
        "name": district["name"],
        "nameZh": district["nameZh"],
        "theaterCount": len(theaters_out),
        "theaters": theaters_out,
    }


def build_all() -> dict:
    """返回 {district_id: district_json_dict}。"""
    conn = _get_db()
    try:
        details, matches = _load_douban(conn)
        showtimes = _load_showtimes(conn)
        eiga_reviews = _load_eiga_reviews(conn)
        out = {}
        for d in DISTRICTS:
            dj = build_district(conn, d, details, matches, showtimes, eiga_reviews)
            out[d["id"]] = dj
            logger.info("Step4 build: %s — %d theaters, %d movies",
                        d["id"], dj["theaterCount"],
                        sum(len(t["movies"]) for t in dj["theaters"]))
        return out
    finally:
        conn.close()


def write_local(data: dict, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for did, dj in data.items():
        p = out_dir / f"{did}.json"
        p.write_text(json.dumps(dj, ensure_ascii=False), encoding="utf-8")
        paths.append(p)
    logger.info("Step4: 已写本地 JSON %d 个 → %s", len(paths), out_dir)
    return paths


# ── 微信云开发云存储上传 ──────────────────────────────────────────────────────
# access_token / 上传 / 下载 的共用基建在 pipeline/wxcloud.py。

def upload_all(config: dict, local_files: list[Path]) -> dict:
    """把本地 JSON 上传到云存储。返回 {district_id: cloud_file_id}。"""
    from pipeline import wxcloud
    mp = wxcloud.mp_cfg(config)
    env = mp.get("cloud_env", "")
    if not env:
        raise RuntimeError("miniprogram.cloud_env 未配置（云开发环境 ID）")
    prefix = (mp.get("storage_prefix", "data") or "data").strip("/")

    token = wxcloud.get_access_token(config)
    file_ids: dict = {}
    for p in local_files:
        cloud_path = f"{prefix}/{p.name}"
        fid = wxcloud.upload(env, token, cloud_path, p.read_bytes())
        file_ids[p.stem] = fid
        logger.info("Step4 upload: %s → %s", cloud_path, fid)
    return file_ids


# ── 入口 ──────────────────────────────────────────────────────────────────────

def run(config: dict, out_dir: Path, upload: bool = True) -> dict:
    """生成 9 个区 JSON 写本地；upload=True 时再上传到云存储。

    返回 {"files": [...], "file_ids": {...}}。
    """
    data = build_all()
    files = write_local(data, out_dir)
    result = {"files": [str(p) for p in files], "file_ids": {}}
    if upload:
        result["file_ids"] = upload_all(config, files)
    return result
