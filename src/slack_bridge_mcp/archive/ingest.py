"""Shared message-ingest entry point used by both the polling daemon and the
WS watcher. Handles plain messages, edits (`message_changed`), and deletes
(`message_deleted`). Idempotent — both writers can race; the new schema's
UNIQUE(channel_id, ts, edit_seq) plus the live-row predicate make collisions
no-ops.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from typing import Any

from .. import caches
from . import db

log = logging.getLogger("slack-archive-ingest")

_LINK_RE = re.compile(r"<https?://[^|>]+\|([^>]+)>")
_RAWLINK_RE = re.compile(r"<(https?://[^>]+)>")
_USERMENT_RE = re.compile(r"<@(U[A-Z0-9]+)>")
_CHANMENT_RE = re.compile(r"<#(C[A-Z0-9]+)\|([^>]*)>")


def flatten_text(msg: dict[str, Any]) -> str:
    """Combine text + blocks + attachments into one searchable plaintext."""
    parts: list[str] = []
    if msg.get("text"):
        parts.append(msg["text"])
    for blk in msg.get("blocks") or []:
        if blk.get("type") == "section":
            t = (blk.get("text") or {}).get("text")
            if t:
                parts.append(t)
        for f in blk.get("fields") or []:
            t = f.get("text")
            if t:
                parts.append(t)
        for el in blk.get("elements") or []:
            for sub in el.get("elements") or []:
                t = sub.get("text")
                if t:
                    parts.append(t)
    for att in msg.get("attachments") or []:
        for k in ("title", "text", "pretext", "fallback"):
            v = att.get(k)
            if v:
                parts.append(v)
        for fld in att.get("fields") or []:
            v = fld.get("value")
            if v:
                parts.append(f"{fld.get('title', '')}: {v}")
    s = " ".join(parts)
    s = _LINK_RE.sub(r"\1", s)
    s = _RAWLINK_RE.sub("", s)
    s = _USERMENT_RE.sub(r"@\1", s)
    s = _CHANMENT_RE.sub(r"#\2", s)
    return s.strip()


def _resolve_actor_label(actor: str | None) -> str | None:
    if not actor:
        return None
    try:
        return caches.actor_label(actor)
    except Exception:
        return None


def ingest_message(
    conn: sqlite3.Connection,
    channel_id: str,
    msg: dict[str, Any],
    *,
    via: str,
) -> str:
    """Insert a plain message (subtype is normal text or a non-edit/delete subtype).
    Returns 'inserted' or 'noop'."""
    user = msg.get("user") or msg.get("bot_id") or msg.get("username")
    text = flatten_text(msg)
    new_id = db.insert_message(
        conn,
        channel_id=channel_id,
        ts=msg["ts"],
        user=user,
        user_label=_resolve_actor_label(user),
        text=text,
        thread_ts=msg.get("thread_ts"),
        subtype=msg.get("subtype"),
        raw=msg,
        via=via,
    )
    return "inserted" if new_id is not None else "noop"


def ingest_edit(
    conn: sqlite3.Connection,
    channel_id: str,
    new_msg: dict[str, Any],
    *,
    via: str,
) -> str:
    """Apply a message edit. `new_msg` is the post-edit message body
    (keyed by the original ts)."""
    user = new_msg.get("user") or new_msg.get("bot_id") or new_msg.get("username")
    text = flatten_text(new_msg)
    new_id = db.apply_edit(
        conn,
        channel_id=channel_id,
        ts=new_msg["ts"],
        user=user,
        user_label=_resolve_actor_label(user),
        text=text,
        thread_ts=new_msg.get("thread_ts"),
        subtype=new_msg.get("subtype"),
        raw=new_msg,
        via=via,
    )
    return "edited" if new_id is not None else "noop"


def ingest_delete(
    conn: sqlite3.Connection,
    channel_id: str,
    ts: str,
) -> str:
    """Soft-delete the message at (channel_id, ts). Returns 'deleted' or 'noop'."""
    n = db.mark_deleted(conn, channel_id, ts)
    return "deleted" if n else "noop"


def ingest_event(conn: sqlite3.Connection, event: dict[str, Any], *, via: str) -> str:
    """Top-level dispatcher for a Slack `message`-class event from either WS or HTTP."""
    cid = event.get("channel")
    if not cid:
        return "noop"
    subtype = event.get("subtype")
    if subtype == "message_deleted":
        ts = event.get("deleted_ts") or (event.get("previous_message") or {}).get("ts")
        if not ts:
            return "noop"
        return ingest_delete(conn, cid, ts)
    if subtype == "message_changed":
        new_msg = event.get("message") or {}
        if not new_msg.get("ts"):
            return "noop"
        return ingest_edit(conn, cid, new_msg, via=via)
    if not event.get("ts"):
        return "noop"
    return ingest_message(conn, cid, event, via=via)
