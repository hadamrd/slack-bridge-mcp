"""SQLite schema + helpers for the local Slack archive.

Single hot DB at SLACK_BRIDGE_ARCHIVE_DB_PATH (mode 600). FTS5 over
flattened message text. One writer at a time (WAL serializes); MCP tools
and both daemons (polling + WS watcher) all read.

Stable identity
---------------
`msg_id INTEGER PRIMARY KEY AUTOINCREMENT` is the canonical message id.
With `INTEGER PRIMARY KEY` it IS the rowid. With `AUTOINCREMENT` it is
never reused and is stable across `VACUUM` — useful for any future
out-of-band index that wants a durable foreign key into messages.

Edits + deletes
---------------
- Edits: a new row is appended with `edit_seq = max+1` at the same
  (channel_id, ts), and the previous row's `superseded_by` is set to the
  new row's `msg_id`. The "live" view filters `superseded_by IS NULL`.
- Deletes: soft. We set `deleted_at` (epoch s) on the live row. We never
  hard-delete from hot — compaction moves rows to cold, but tombstones
  travel along. The "live" view also filters `deleted_at IS NULL`.

Read tools should always include `WHERE superseded_by IS NULL AND
deleted_at IS NULL`. The partial index `idx_live` is sized for that.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from typing import Any

from ..config import settings

DB_PATH = settings().archive_db_path

SCHEMA_VERSION = 2

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS channels (
        id              TEXT PRIMARY KEY,
        name            TEXT,
        is_im           INTEGER NOT NULL DEFAULT 0,
        last_ts         TEXT,
        last_polled_at  INTEGER,
        name_resolved_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS messages (
        msg_id        INTEGER PRIMARY KEY AUTOINCREMENT,
        channel_id    TEXT NOT NULL,
        ts            TEXT NOT NULL,
        edit_seq      INTEGER NOT NULL DEFAULT 0,
        user          TEXT,
        user_label    TEXT,
        text          TEXT,
        thread_ts     TEXT,
        subtype       TEXT,
        raw_json      TEXT NOT NULL,
        recorded_at   INTEGER NOT NULL,
        ingested_via  TEXT,
        deleted_at    INTEGER,
        superseded_by INTEGER REFERENCES messages(msg_id),
        UNIQUE(channel_id, ts, edit_seq)
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chan_ts ON messages(channel_id, ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_thread  ON messages(channel_id, thread_ts)",
    "CREATE INDEX IF NOT EXISTS idx_user_ts ON messages(user, ts DESC)",
    """CREATE INDEX IF NOT EXISTS idx_live ON messages(channel_id, ts DESC)
       WHERE superseded_by IS NULL AND deleted_at IS NULL""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
        text,
        content=messages,
        content_rowid=msg_id,
        tokenize='unicode61 remove_diacritics 2'
    )""",
    """CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, text) VALUES (new.msg_id, new.text);
    END""",
    """CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.msg_id, old.text);
    END""",
    """CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE OF text ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, text) VALUES('delete', old.msg_id, old.text);
        INSERT INTO messages_fts(rowid, text) VALUES (new.msg_id, new.text);
    END""",
]


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), isolation_level=None, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    with contextlib.suppress(OSError):
        os.chmod(DB_PATH, 0o600)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    for stmt in _SCHEMA:
        conn.execute(stmt)
    cur = conn.execute("SELECT version FROM schema_version").fetchone()
    if not cur:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))


def schema_version(conn: sqlite3.Connection) -> int:
    """Returns 0 if pre-versioned (legacy) schema is detected, else the version."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if not cols:
        return SCHEMA_VERSION  # fresh DB
    if "msg_id" not in cols:
        return 1  # legacy: composite-PK schema
    row = (
        conn.execute("SELECT version FROM schema_version").fetchone()
        if (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_version'"
            ).fetchone()
        )
        else None
    )
    return int(row["version"]) if row else SCHEMA_VERSION


def ensure_channel(
    conn: sqlite3.Connection, channel_id: str, name: str | None, is_im: bool
) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO channels (id, name, is_im) VALUES (?, ?, ?)",
        (channel_id, name, 1 if is_im else 0),
    )
    if name:
        conn.execute(
            "UPDATE channels SET name=?, name_resolved_at=? WHERE id=?",
            (name, int(time.time()), channel_id),
        )


def insert_message(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    ts: str,
    user: str | None,
    user_label: str | None,
    text: str,
    thread_ts: str | None,
    subtype: str | None,
    raw: dict[str, Any],
    via: str,
) -> int | None:
    """Insert a brand-new message at edit_seq=0. Idempotent: if (channel_id, ts, 0)
    already exists, returns None. Otherwise returns the msg_id assigned."""
    now = int(time.time())
    cur = conn.execute(
        """INSERT OR IGNORE INTO messages
           (channel_id, ts, edit_seq, user, user_label, text, thread_ts, subtype,
            raw_json, recorded_at, ingested_via)
           VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            channel_id,
            ts,
            user,
            user_label,
            text,
            thread_ts,
            subtype,
            json.dumps(raw, separators=(",", ":")),
            now,
            via,
        ),
    )
    if not cur.rowcount or cur.lastrowid is None:
        return None
    return int(cur.lastrowid)


def apply_edit(
    conn: sqlite3.Connection,
    *,
    channel_id: str,
    ts: str,
    user: str | None,
    user_label: str | None,
    text: str,
    thread_ts: str | None,
    subtype: str | None,
    raw: dict[str, Any],
    via: str,
) -> int | None:
    """Record an edit. Looks up the current live row at (channel_id, ts).
    If text matches, no-op. Otherwise: insert a new row at edit_seq=max+1
    and stamp the old row's superseded_by. Returns new msg_id, or None on no-op."""
    live = conn.execute(
        """SELECT msg_id, edit_seq, text FROM messages
           WHERE channel_id=? AND ts=? AND superseded_by IS NULL AND deleted_at IS NULL
           ORDER BY edit_seq DESC LIMIT 1""",
        (channel_id, ts),
    ).fetchone()
    if live and live["text"] == text:
        return None
    next_seq = (live["edit_seq"] + 1) if live else 0
    now = int(time.time())
    cur = conn.execute(
        """INSERT OR IGNORE INTO messages
           (channel_id, ts, edit_seq, user, user_label, text, thread_ts, subtype,
            raw_json, recorded_at, ingested_via)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            channel_id,
            ts,
            next_seq,
            user,
            user_label,
            text,
            thread_ts,
            subtype,
            json.dumps(raw, separators=(",", ":")),
            now,
            via,
        ),
    )
    new_id = int(cur.lastrowid) if cur.rowcount and cur.lastrowid is not None else None
    if new_id and live:
        conn.execute(
            "UPDATE messages SET superseded_by=? WHERE msg_id=?",
            (new_id, live["msg_id"]),
        )
    return new_id


def mark_deleted(conn: sqlite3.Connection, channel_id: str, ts: str) -> int:
    """Soft-delete the live row at (channel_id, ts). Returns rowcount (0 or 1)."""
    cur = conn.execute(
        """UPDATE messages SET deleted_at=? WHERE channel_id=? AND ts=?
           AND superseded_by IS NULL AND deleted_at IS NULL""",
        (int(time.time()), channel_id, ts),
    )
    return cur.rowcount


def get_channel_last_ts(conn: sqlite3.Connection, channel_id: str) -> str | None:
    row = conn.execute("SELECT last_ts FROM channels WHERE id=?", (channel_id,)).fetchone()
    return row["last_ts"] if row else None


def update_channel_checkpoint(
    conn: sqlite3.Connection, channel_id: str, last_ts: str | None
) -> None:
    if last_ts:
        conn.execute(
            "UPDATE channels SET last_ts=?, last_polled_at=? WHERE id=?",
            (last_ts, int(time.time()), channel_id),
        )
    else:
        conn.execute(
            "UPDATE channels SET last_polled_at=? WHERE id=?",
            (int(time.time()), channel_id),
        )


def stats(conn: sqlite3.Connection) -> dict[str, Any]:
    msg_count = conn.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
    live_count = conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE superseded_by IS NULL AND deleted_at IS NULL"
    ).fetchone()["c"]
    deleted_count = conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE deleted_at IS NOT NULL"
    ).fetchone()["c"]
    edited_count = conn.execute(
        "SELECT COUNT(*) AS c FROM messages WHERE superseded_by IS NOT NULL"
    ).fetchone()["c"]
    via_breakdown = {
        r["via"]: r["c"]
        for r in conn.execute(
            "SELECT COALESCE(ingested_via, 'legacy') AS via, COUNT(*) AS c FROM messages GROUP BY via"
        ).fetchall()
    }
    chan_count = conn.execute("SELECT COUNT(*) AS c FROM channels").fetchone()["c"]
    chan_polled = conn.execute(
        "SELECT COUNT(*) AS c FROM channels WHERE last_polled_at IS NOT NULL"
    ).fetchone()["c"]
    last_msg = conn.execute("SELECT MAX(recorded_at) AS m FROM messages").fetchone()["m"]
    db_size = DB_PATH.stat().st_size if DB_PATH.exists() else 0
    return {
        "db_path": str(DB_PATH),
        "db_size_bytes": db_size,
        "schema_version": SCHEMA_VERSION,
        "channels_known": chan_count,
        "channels_polled": chan_polled,
        "messages_total": msg_count,
        "messages_live": live_count,
        "messages_edited": edited_count,
        "messages_deleted": deleted_count,
        "ingested_via": via_breakdown,
        "last_recorded_at": last_msg,
    }
