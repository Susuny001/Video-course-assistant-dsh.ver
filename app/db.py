"""SQLite 持久化：课程、分段、字幕、任务、对话。"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any, Iterable

from .config import data_dir, new_id

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    path TEXT NOT NULL,
    duration REAL DEFAULT 0,
    width INTEGER DEFAULT 0,
    height INTEGER DEFAULT 0,
    size INTEGER DEFAULT 0,
    cover TEXT DEFAULT '',
    status TEXT DEFAULT 'new',
    created_at REAL,
    updated_at REAL,
    meta TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    start REAL, "end" REAL,
    title TEXT DEFAULT '',
    abstract TEXT DEFAULT '',
    keypoints TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_seg_vid ON segments(video_id, idx);
CREATE TABLE IF NOT EXISTS cues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    start REAL, "end" REAL,
    raw TEXT DEFAULT '',
    text TEXT DEFAULT '',
    translation TEXT DEFAULT '',
    terms TEXT DEFAULT '[]'
);
CREATE INDEX IF NOT EXISTS ix_cue_vid ON cues(video_id, idx);
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    video_id TEXT,
    kind TEXT,
    status TEXT DEFAULT 'running',
    progress REAL DEFAULT 0,
    message TEXT DEFAULT '',
    log TEXT DEFAULT '[]',
    created_at REAL,
    updated_at REAL
);
CREATE TABLE IF NOT EXISTS chats (
    id TEXT PRIMARY KEY,
    video_id TEXT,
    title TEXT DEFAULT '新对话',
    created_at REAL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT DEFAULT '',
    meta TEXT DEFAULT '{}',
    created_at REAL
);
CREATE INDEX IF NOT EXISTS ix_msg_chat ON messages(chat_id, id);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


@contextmanager
def connect():
    conn = sqlite3.connect(data_dir() / "app.db", timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init() -> None:
    with connect() as c:
        c.executescript(SCHEMA)


def rows(sql: str, args: Iterable = ()) -> list[dict]:
    with connect() as c:
        return [dict(r) for r in c.execute(sql, args).fetchall()]


def one(sql: str, args: Iterable = ()) -> dict | None:
    r = rows(sql, args)
    return r[0] if r else None


def exec(sql: str, args: Iterable = ()) -> int:
    with connect() as c:
        cur = c.execute(sql, args)
        return cur.lastrowid or cur.rowcount


# ---------------------------------------------------------------- videos
def add_video(path: str, title: str, **fields) -> dict:
    vid = new_id()
    now = time.time()
    data = {
        "id": vid,
        "title": title,
        "path": path,
        "duration": fields.get("duration", 0) or 0,
        "width": fields.get("width", 0) or 0,
        "height": fields.get("height", 0) or 0,
        "size": fields.get("size", 0) or 0,
        "cover": fields.get("cover", "") or "",
        "status": fields.get("status", "new"),
        "created_at": now,
        "updated_at": now,
        "meta": json.dumps(fields.get("meta", {}), ensure_ascii=False),
    }
    with connect() as c:
        c.execute(
            """INSERT INTO videos(id,title,path,duration,width,height,size,cover,status,created_at,updated_at,meta)
               VALUES(:id,:title,:path,:duration,:width,:height,:size,:cover,:status,:created_at,:updated_at,:meta)""",
            data,
        )
    return get_video(vid)  # type: ignore[return-value]


def get_video(vid: str) -> dict | None:
    v = one("SELECT * FROM videos WHERE id=?", (vid,))
    if v:
        v["meta"] = _json(v.get("meta"), {})
    return v


def list_videos() -> list[dict]:
    out = rows("SELECT * FROM videos ORDER BY updated_at DESC")
    for v in out:
        v["meta"] = _json(v.get("meta"), {})
        st = one("SELECT COUNT(*) n FROM segments WHERE video_id=?", (v["id"],))
        v["segment_count"] = st["n"] if st else 0
    return out


def update_video(vid: str, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = time.time()
    if "meta" in fields and not isinstance(fields["meta"], str):
        fields["meta"] = json.dumps(fields["meta"], ensure_ascii=False)
    keys = ", ".join(f"{k}=:{k}" for k in fields)
    fields["id"] = vid
    exec(f"UPDATE videos SET {keys} WHERE id=:id", fields)


def delete_video(vid: str) -> None:
    with connect() as c:
        for t in ("cues", "segments", "jobs", "messages"):
            c.execute(f"DELETE FROM {t} WHERE video_id=?", (vid,))
        c.execute("DELETE FROM chats WHERE video_id=?", (vid,))
        c.execute("DELETE FROM videos WHERE id=?", (vid,))


# ---------------------------------------------------------------- segments / cues
def replace_cues(vid: str, cues: list[dict]) -> None:
    with connect() as c:
        c.execute("DELETE FROM cues WHERE video_id=?", (vid,))
        c.executemany(
            """INSERT INTO cues(video_id,idx,start,"end",raw,text,translation,terms)
               VALUES(?,?,?,?,?,?,?,?)""",
            [
                (
                    vid, i, cu.get("start", 0), cu.get("end", 0), cu.get("raw", ""),
                    cu.get("text", ""), cu.get("translation", ""),
                    json.dumps(cu.get("terms", []), ensure_ascii=False),
                )
                for i, cu in enumerate(cues)
            ],
        )


def get_cues(vid: str) -> list[dict]:
    out = rows('SELECT * FROM cues WHERE video_id=? ORDER BY idx', (vid,))
    for r in out:
        r["terms"] = _json(r.get("terms"), [])
    return out


def replace_segments(vid: str, segs: list[dict]) -> None:
    with connect() as c:
        c.execute("DELETE FROM segments WHERE video_id=?", (vid,))
        c.executemany(
            'INSERT INTO segments(video_id,idx,start,"end",title,abstract,keypoints) VALUES(?,?,?,?,?,?,?)',
            [
                (
                    vid, i, s.get("start", 0), s.get("end", 0), s.get("title", ""),
                    s.get("abstract", ""),
                    json.dumps(s.get("keypoints", []), ensure_ascii=False),
                )
                for i, s in enumerate(segs)
            ],
        )


def get_segments(vid: str) -> list[dict]:
    out = rows('SELECT * FROM segments WHERE video_id=? ORDER BY idx', (vid,))
    for r in out:
        r["keypoints"] = _json(r.get("keypoints"), [])
    return out


# ---------------------------------------------------------------- jobs
def create_job(vid: str, kind: str = "analyze") -> str:
    jid = new_id("job")
    now = time.time()
    exec(
        "INSERT INTO jobs(id,video_id,kind,status,progress,message,log,created_at,updated_at)"
        " VALUES(?,?,?,'running',0,'准备中','[]',?,?)",
        (jid, vid, kind, now, now),
    )
    return jid


def update_job(jid: str, progress: float | None = None, message: str | None = None,
               status: str | None = None, log_line: str | None = None) -> None:
    with connect() as c:
        j = c.execute("SELECT log FROM jobs WHERE id=?", (jid,)).fetchone()
        log = _json(j["log"], []) if j else []
        if log_line:
            log.append({"t": time.time(), "msg": log_line})
            log = log[-200:]
        c.execute(
            """UPDATE jobs SET progress=COALESCE(?,progress), message=COALESCE(?,message),
               status=COALESCE(?,status), log=?, updated_at=? WHERE id=?""",
            (progress, message, status, json.dumps(log, ensure_ascii=False), time.time(), jid),
        )


def get_job(jid: str) -> dict | None:
    j = one("SELECT * FROM jobs WHERE id=?", (jid,))
    if j:
        j["log"] = _json(j.get("log"), [])
    return j


# ---------------------------------------------------------------- chats
def create_chat(vid: str, title: str = "新对话") -> str:
    cid = new_id("chat")
    exec("INSERT INTO chats(id,video_id,title,created_at) VALUES(?,?,?,?)",
         (cid, vid, title, time.time()))
    return cid


def list_chats(vid: str) -> list[dict]:
    return rows("SELECT * FROM chats WHERE video_id=? ORDER BY created_at DESC", (vid,))


def delete_chat(cid: str) -> None:
    exec("DELETE FROM messages WHERE chat_id=?", (cid,))
    exec("DELETE FROM chats WHERE id=?", (cid,))


def add_message(cid: str, role: str, content: str, meta: dict | None = None) -> int:
    return exec(
        "INSERT INTO messages(chat_id,role,content,meta,created_at) VALUES(?,?,?,?,?)",
        (cid, role, content, json.dumps(meta or {}, ensure_ascii=False), time.time()),
    )


def get_messages(cid: str) -> list[dict]:
    out = rows("SELECT * FROM messages WHERE chat_id=? ORDER BY id", (cid,))
    for m in out:
        m["meta"] = _json(m.get("meta"), {})
    return out


# ---------------------------------------------------------------- kv
def kv_get(key: str, default: Any = None) -> Any:
    r = one("SELECT value FROM kv WHERE key=?", (key,))
    return _json(r["value"], default) if r else default


def kv_set(key: str, value: Any) -> None:
    exec("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
         (key, json.dumps(value, ensure_ascii=False)))


def _json(v: Any, default: Any) -> Any:
    if v is None or v == "":
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except Exception:
        return default
