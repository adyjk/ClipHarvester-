"""SQLite 存储 + FTS5 全文索引。

中文用 FTS5 的 trigram 分词，不需要结巴之类的分词器，支持子串匹配。
任何检索都在本地库完成，不产生网络请求。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  id             TEXT PRIMARY KEY,
  source         TEXT NOT NULL DEFAULT '',
  title          TEXT NOT NULL DEFAULT '',
  desc           TEXT NOT NULL DEFAULT '',
  tags           TEXT NOT NULL DEFAULT '',
  author         TEXT NOT NULL DEFAULT '',
  author_id      TEXT NOT NULL DEFAULT '',
  duration_s     INTEGER NOT NULL DEFAULT 0,
  pub_ts         INTEGER NOT NULL DEFAULT 0,
  play           INTEGER NOT NULL DEFAULT 0,
  like_count     INTEGER NOT NULL DEFAULT 0,
  fav            INTEGER NOT NULL DEFAULT 0,
  coin           INTEGER NOT NULL DEFAULT 0,
  danmaku        INTEGER NOT NULL DEFAULT 0,
  reply          INTEGER NOT NULL DEFAULT 0,
  cover_url      TEXT NOT NULL DEFAULT '',
  url            TEXT NOT NULL DEFAULT '',
  summary        TEXT NOT NULL DEFAULT '',
  summary_model  TEXT NOT NULL DEFAULT '',
  summarized_at  INTEGER NOT NULL DEFAULT 0,
  score          REAL NOT NULL DEFAULT 0,
  reasons        TEXT NOT NULL DEFAULT '[]',
  feedback       TEXT NOT NULL DEFAULT '',
  opened         INTEGER NOT NULL DEFAULT 0,
  fetched_at     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_videos_score ON videos(score DESC);
CREATE INDEX IF NOT EXISTS idx_videos_pub ON videos(pub_ts DESC);
CREATE INDEX IF NOT EXISTS idx_videos_fb ON videos(feedback);

CREATE TABLE IF NOT EXISTS feedback_log (
  id     TEXT NOT NULL,
  action TEXT NOT NULL,
  ts     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS fetch_runs (
  key     TEXT PRIMARY KEY,
  cursor  INTEGER NOT NULL DEFAULT 0,
  total   INTEGER NOT NULL DEFAULT 0,
  last_ts INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS daily_stat (
  day     TEXT PRIMARY KEY,
  fetched INTEGER NOT NULL DEFAULT 0
);

-- 去重索引：视频本体 24 小时后会被删，但这里记着「拉过什么」，避免第二天重复进货
CREATE TABLE IF NOT EXISTS seen (
  key      TEXT PRIMARY KEY,      -- id:xxx 或 t:归一化标题
  kind     TEXT NOT NULL,         -- id / title
  source   TEXT NOT NULL DEFAULT '',
  title    TEXT NOT NULL DEFAULT '',
  url      TEXT NOT NULL DEFAULT '',
  first_ts INTEGER NOT NULL,
  last_ts  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_kind ON seen(kind, source);

CREATE VIRTUAL TABLE IF NOT EXISTS videos_fts USING fts5(
  title, desc, tags, author, summary,
  content='videos', content_rowid='rowid', tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS videos_ai AFTER INSERT ON videos BEGIN
  INSERT INTO videos_fts(rowid, title, desc, tags, author, summary)
  VALUES (new.rowid, new.title, new.desc, new.tags, new.author, new.summary);
END;

CREATE TRIGGER IF NOT EXISTS videos_ad AFTER DELETE ON videos BEGIN
  INSERT INTO videos_fts(videos_fts, rowid, title, desc, tags, author, summary)
  VALUES ('delete', old.rowid, old.title, old.desc, old.tags, old.author, old.summary);
END;

CREATE TRIGGER IF NOT EXISTS videos_au AFTER UPDATE ON videos BEGIN
  INSERT INTO videos_fts(videos_fts, rowid, title, desc, tags, author, summary)
  VALUES ('delete', old.rowid, old.title, old.desc, old.tags, old.author, old.summary);
  INSERT INTO videos_fts(rowid, title, desc, tags, author, summary)
  VALUES (new.rowid, new.title, new.desc, new.tags, new.author, new.summary);
END;
"""

# 进货时允许被覆盖的字段（统计信息会变），摘要/反馈/分数绝不覆盖
UPSERT_FIELDS = [
    "title", "desc", "tags", "author", "author_id", "duration_s", "pub_ts",
    "play", "like_count", "fav", "coin", "danmaku", "reply",
    "cover_url", "url", "media_url",
]


def connect(db_path: str) -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    # 老库补列：视频内容素材（弹幕/热评文本）与摘要来源
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(videos)")}
    for name, decl in (
        ("content", "TEXT NOT NULL DEFAULT ''"),      # 弹幕+热评拼成的实况文本
        ("content_src", "TEXT NOT NULL DEFAULT ''"),  # danmaku / empty
        ("content_at", "INTEGER NOT NULL DEFAULT 0"),
        ("summary_src", "TEXT NOT NULL DEFAULT ''"),  # danmaku / asr / meta
        ("transcript", "TEXT NOT NULL DEFAULT ''"),   # whisper 语音转写全文
        ("media_url", "TEXT NOT NULL DEFAULT ''"),    # 抖音直链，转写时直接下载
        ("served", "INTEGER NOT NULL DEFAULT 0"),     # 已刷过（刷新后不再占位）
        ("author_id", "TEXT NOT NULL DEFAULT ''"),    # 作者唯一标识（抖音 sec_uid），用于串同作者视频
    ):
        if name not in cols:
            conn.execute(f"ALTER TABLE videos ADD COLUMN {name} {decl}")
    conn.commit()
    # seen 表补 url 列（老库没有），用于「历史记录」展示链接
    scols = {r["name"] for r in conn.execute("PRAGMA table_info(seen)")}
    if "url" not in scols:
        conn.execute("ALTER TABLE seen ADD COLUMN url TEXT NOT NULL DEFAULT ''")
        conn.commit()
    # 回填：还在 videos 库里的记录补上链接；更早被清理的只能留标题
    conn.execute(
        "UPDATE seen SET url = COALESCE((SELECT videos.url FROM videos WHERE 'id:'||videos.id = seen.key), '') "
        "WHERE kind='id' AND url=''"
    )
    conn.commit()
    # 首次建表时，把库里已有视频回填进去重索引
    if not conn.execute("SELECT 1 FROM seen LIMIT 1").fetchone():
        rows = conn.execute("SELECT id, source, title, url FROM videos").fetchall()
        remember_seen(conn, [{"id": r["id"], "source": r["source"], "title": r["title"], "url": r["url"]} for r in rows])


_NOISE_RE = re.compile(r"[\s\u200b\u200c\u200d\ufeff，。、！？；：,.!?;:\"'“”‘’（）()\[\]【】<>《》\-—_~·…|/\\]+")


def norm_title(title: str) -> str:
    """标题归一化：去空白、去标点、转小写，用于判断「标题一模一样」。"""
    return _NOISE_RE.sub("", (title or "").strip().lower())


def remember_seen(conn: sqlite3.Connection, videos: list[dict]) -> None:
    """把拉过的视频记进去重索引（含 id、归一化标题和链接），索引比视频本体活得久。"""
    now = int(time.time())
    for v in videos:
        vid = str(v.get("id") or "").strip()
        src = str(v.get("source") or "")
        title = str(v.get("title") or "")
        url = str(v.get("url") or "")
        if vid:
            conn.execute(
                "INSERT INTO seen(key, kind, source, title, url, first_ts, last_ts) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET title=?, url=?, last_ts=?",
                ("id:" + vid, "id", src, title, url, now, now, title, url, now),
            )
        nt = norm_title(title)
        if nt:
            conn.execute(
                "INSERT INTO seen(key, kind, source, title, url, first_ts, last_ts) "
                "VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET last_ts=?",
                ("t:" + nt, "title", src, nt, url, now, now, now),
            )
    conn.commit()


def existing_titles(conn: sqlite3.Connection, source: str = "") -> set[str]:
    """返回拉过的标题（归一化后）集合，来自去重索引，不受视频清理影响。"""
    sql = "SELECT title FROM seen WHERE kind='title'" + (" AND source=?" if source else "")
    args = (source,) if source else ()
    return {r[0] for r in conn.execute(sql, args) if r[0]}


def upsert_videos(conn: sqlite3.Connection, videos: list[dict]) -> tuple[int, int, int]:
    """写入一批视频，返回 (新增数, 更新数, 标题重复跳过数)。

    按 id 去重；id 不同但归一化标题完全相同的，视为同一条内容，跳过不新增。
    """
    now = int(time.time())
    new_n = upd_n = skip_n = 0
    title_map: dict[str, str] = {}
    for r in conn.execute("SELECT id, title FROM videos"):
        t = norm_title(r[1])
        if t:
            title_map.setdefault(t, r[0])

    for v in videos:
        vid = str(v.get("id") or "").strip()
        if not vid:
            continue
        row = {k: v.get(k, "") for k in UPSERT_FIELDS}
        for k in ("duration_s", "pub_ts", "play", "like_count", "fav", "coin", "danmaku", "reply"):
            try:
                row[k] = int(row[k] or 0)
            except (TypeError, ValueError):
                row[k] = 0

        exists = conn.execute("SELECT 1 FROM videos WHERE id=?", (vid,)).fetchone()
        nt = norm_title(row.get("title", ""))
        if exists:
            sets = ", ".join(f"{k}=?" for k in UPSERT_FIELDS)
            conn.execute(f"UPDATE videos SET {sets} WHERE id=?", [row[k] for k in UPSERT_FIELDS] + [vid])
            upd_n += 1
        elif nt and nt in title_map:
            # id 不同但标题一模一样 → 同一条内容，跳过
            skip_n += 1
            continue
        else:
            conn.execute(
                "INSERT INTO videos (id, source, fetched_at, " + ", ".join(UPSERT_FIELDS) + ") "
                "VALUES (?,?,?, " + ", ".join("?" for _ in UPSERT_FIELDS) + ")",
                [vid, v.get("source", ""), now] + [row[k] for k in UPSERT_FIELDS],
            )
            new_n += 1
            if nt:
                title_map.setdefault(nt, vid)
    conn.commit()
    # 不管新增/更新/跳过，都记进长期去重索引
    remember_seen(conn, [
        {"id": v.get("id"), "source": v.get("source", ""), "title": v.get("title", ""), "url": v.get("url", "")}
        for v in videos
    ])
    return new_n, upd_n, skip_n


def add_daily_count(conn: sqlite3.Connection, n: int) -> None:
    day = time.strftime("%Y-%m-%d")
    conn.execute(
        "INSERT INTO daily_stat(day, fetched) VALUES(?,?) "
        "ON CONFLICT(day) DO UPDATE SET fetched = fetched + ?",
        (day, n, n),
    )
    conn.commit()


def today_count(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT fetched FROM daily_stat WHERE day=?", (time.strftime("%Y-%m-%d"),)).fetchone()
    return int(row["fetched"]) if row else 0


def get_cursor(conn: sqlite3.Connection, key: str) -> int:
    row = conn.execute("SELECT cursor FROM fetch_runs WHERE key=?", (key,)).fetchone()
    return int(row["cursor"]) if row else 0


def set_cursor(conn: sqlite3.Connection, key: str, cursor: int, total: int = 0) -> None:
    conn.execute(
        "INSERT INTO fetch_runs(key, cursor, total, last_ts) VALUES(?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET cursor=?, total=?, last_ts=?",
        (key, cursor, total, int(time.time()), cursor, total, int(time.time())),
    )
    conn.commit()


def pending_summary(conn: sqlite3.Connection, limit: int, force: bool = False) -> list[sqlite3.Row]:
    """待摘要：还没用「视频实况文本」总结过的。force=True 时全库重做。"""
    if force:
        return conn.execute(
            "SELECT * FROM videos ORDER BY score DESC LIMIT ?", (limit,)
        ).fetchall()
    return conn.execute(
        "SELECT * FROM videos WHERE summary_src NOT IN ('danmaku','meta','asr') "
        "ORDER BY score DESC LIMIT ?",
        (limit,),
    ).fetchall()


def save_transcript(conn: sqlite3.Connection, vid: str, text: str) -> None:
    conn.execute("UPDATE videos SET transcript=? WHERE id=?", (text, vid))
    conn.commit()


def save_content(conn: sqlite3.Connection, vid: str, text: str, src: str) -> None:
    conn.execute(
        "UPDATE videos SET content=?, content_src=?, content_at=? WHERE id=?",
        (text, src, int(time.time()), vid),
    )
    conn.commit()


def save_summary(conn: sqlite3.Connection, vid: str, payload: dict, model: str, src: str = "meta") -> None:
    conn.execute(
        "UPDATE videos SET summary=?, summary_model=?, summary_src=?, summarized_at=? WHERE id=?",
        (json.dumps(payload, ensure_ascii=False), model, src, int(time.time()), vid),
    )
    conn.commit()


def existing_ids(conn: sqlite3.Connection, source: str = "") -> set[str]:
    """返回拉过的 id 集合（含已从库里清掉的），source 为空时全平台。"""
    sql = "SELECT substr(key,4) FROM seen WHERE kind='id'"
    if source:
        sql += " AND source=?"
        return {r[0] for r in conn.execute(sql, (source,))}
    return {r[0] for r in conn.execute(sql)}


def mark_served(conn: sqlite3.Connection, ids: list[str]) -> int:
    """把已经刷过的视频标记为 served，之后默认不再出现在列表里。"""
    ids = [str(i) for i in ids if i]
    if not ids:
        return 0
    n = 0
    for i in range(0, len(ids), 400):
        chunk = ids[i:i + 400]
        ph = ",".join("?" for _ in chunk)
        cur = conn.execute(
            f"UPDATE videos SET served=1 WHERE id IN ({ph}) AND served=0", chunk
        )
        n += cur.rowcount
    conn.commit()
    return n


def purge_old(conn: sqlite3.Connection, hours: int = 24) -> int:
    """删除拉取超过 N 小时的视频（FTS 由触发器同步清理）。"""
    cutoff = int(time.time()) - int(hours * 3600)
    cur = conn.execute("DELETE FROM videos WHERE fetched_at < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


def purge_seen(conn: sqlite3.Connection, days: int = 30) -> int:
    """去重索引保留更久（默认 30 天），避免第二天又拉回同样的内容。"""
    cutoff = int(time.time()) - int(days * 86400)
    cur = conn.execute("DELETE FROM seen WHERE last_ts < ?", (cutoff,))
    conn.commit()
    return cur.rowcount


def set_feedback(conn: sqlite3.Connection, vid: str, action: str) -> None:
    """action: like / dislike / skip / clear / opened"""
    now = int(time.time())
    if action == "opened":
        conn.execute("UPDATE videos SET opened=1 WHERE id=?", (vid,))
    elif action == "clear":
        conn.execute("UPDATE videos SET feedback='' WHERE id=?", (vid,))
    else:
        conn.execute("UPDATE videos SET feedback=? WHERE id=?", (action, vid))
    conn.execute("INSERT INTO feedback_log(id, action, ts) VALUES(?,?,?)", (vid, action, now))
    conn.commit()


def all_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM videos").fetchall()


def update_scores(conn: sqlite3.Connection, scored: list[tuple[float, str, str]]) -> None:
    conn.executemany("UPDATE videos SET score=?, reasons=? WHERE id=?", scored)
    conn.commit()


def _fts_query(terms: list[str]) -> str:
    # trigram 分词下用引号包住做子串匹配，多词之间 AND
    return " AND ".join('"' + t.replace('"', "") + '"' for t in terms)


def search(
    conn: sqlite3.Connection,
    q: str = "",
    source: str = "",
    sort: str = "score",
    limit: int = 60,
    offset: int = 0,
    hide_disliked: bool = True,
    hide_opened: bool = False,
    hide_served: bool = True,
) -> list[sqlite3.Row]:
    where, params = [], []
    join = ""

    terms = [t for t in (q or "").split() if t]
    long_terms = [t for t in terms if len(t) >= 3]
    short_terms = [t for t in terms if len(t) < 3]

    if long_terms:
        join = "JOIN videos_fts ON videos_fts.rowid = videos.rowid"
        where.append("videos_fts MATCH ?")
        params.append(_fts_query(long_terms))
    for t in short_terms:
        # 少于 3 字 trigram 无法匹配，退化成长度很小的 LIKE
        where.append("(videos.title LIKE ? OR videos.tags LIKE ?)")
        params += [f"%{t}%", f"%{t}%"]

    if source:
        where.append("videos.source=?")
        params.append(source)
    if hide_disliked:
        where.append("videos.feedback NOT IN ('dislike','skip')")
    if hide_opened:
        where.append("videos.opened=0")
    if hide_served:
        where.append("videos.served=0")

    order = {
        "score": "videos.score DESC",
        "new": "videos.pub_ts DESC",
        "play": "videos.play DESC",
    }.get(sort, "videos.score DESC")

    sql = (
        "SELECT videos.* FROM videos " + join
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY " + order + " LIMIT ? OFFSET ?"
    )
    params += [limit, offset]
    return conn.execute(sql, params).fetchall()


def stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        "SELECT COUNT(*) total, SUM(summarized_at>0 AND summary_model!='fallback') summarized, "
        "SUM(summary_src NOT IN ('danmaku','meta','asr')) pending, "
        "SUM(feedback='like') likes, SUM(feedback='dislike') dislikes FROM videos"
    ).fetchone()
    return {
        "total": row["total"] or 0,
        "summarized": row["summarized"] or 0,
        "pending": row["pending"] or 0,
        "likes": row["likes"] or 0,
        "dislikes": row["dislikes"] or 0,
        "today_fetched": today_count(conn),
    }


def history(conn: sqlite3.Connection, limit: int = 200, offset: int = 0) -> dict:
    """历史记录：拉过/刷过的内容（标题 + 链接 + 来源 + 时间），按最近拉取倒序。"""
    where = "kind='id' AND url!=''"
    total = conn.execute(f"SELECT COUNT(*) FROM seen WHERE {where}").fetchone()[0]
    rows = conn.execute(
        f"SELECT source, title, url, last_ts FROM seen WHERE {where} "
        "ORDER BY last_ts DESC LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return {
        "total": total,
        "items": [
            {"source": r["source"], "title": r["title"], "url": r["url"], "ts": r["last_ts"]}
            for r in rows
        ],
    }
