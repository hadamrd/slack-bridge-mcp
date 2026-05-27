"""File listing + content fetch for messages and threads.

Slack files are referenced inline on a message (`message.files = [...]`) and
also addressable via `files.info`. The download URL (`url_private_download`)
needs the same cookie auth as the rest of the bridge — that's what
`client.fetch_url` provides.

Tools:
- `slack_message_files` — list files attached to a message + (optionally) its
  thread replies. Reads from local archive's `raw_json` first to skip API.
- `slack_file_content` — download a file by id and return inline text content
  (for text/json/yaml/etc.) or a saved temp-file path (for binaries).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from mcp.types import Tool

from ..archive import db as archive_db
from ..client import SlackError, call, fetch_url

_MAX_INLINE_BYTES = 2_000_000  # 2 MB cap on inline-text return
_TEXT_LIKE_MIMES = (
    "text/",
    "application/json",
    "application/x-yaml",
    "application/yaml",
    "application/xml",
    "application/javascript",
    "application/x-shellscript",
    "application/x-python",
)


TOOLS: list[Tool] = [
    Tool(
        name="slack_message_files",
        description=(
            "List files attached to a message. With include_replies=true (default), "
            "also walks the thread for files. Returns per-file: id (Fxxx), name, "
            "title, mimetype, filetype, size, permalink, and a short preview "
            "for text-y files. Reads from local archive's raw_json first; falls "
            "back to conversations.history if the message isn't archived yet. "
            "Pair with slack_file_content to fetch a file's body."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel id (Cxxx/Dxxx)"},
                "ts": {"type": "string", "description": "Slack ts of the parent message"},
                "include_replies": {"type": "boolean", "default": True},
            },
            "required": ["channel", "ts"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_file_content",
        description=(
            "Download a Slack file by id and return its content. For text-like "
            "mimes (text/*, application/json/yaml/xml/etc.) returns the body "
            "inline as `content` (capped at ~2 MB). For binaries (PDF, image, "
            "zip), saves to /tmp and returns `path` so a caller can open/OCR/parse "
            "it locally. The file is fetched with the bridge's cookie auth — "
            "the user's own permissions apply."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "file_id": {"type": "string", "description": "Slack file id (Fxxx)"},
                "max_bytes": {
                    "type": "integer",
                    "default": _MAX_INLINE_BYTES,
                    "minimum": 1,
                    "maximum": 50_000_000,
                    "description": "Cap on the inline body returned (text only).",
                },
            },
            "required": ["file_id"],
            "additionalProperties": False,
        },
    ),
]


def _file_summary(f: dict[str, Any]) -> dict[str, Any]:
    """Compact representation of a Slack file for listing."""
    return {
        "id": f.get("id"),
        "name": f.get("name"),
        "title": f.get("title"),
        "mimetype": f.get("mimetype"),
        "filetype": f.get("filetype"),
        "size": f.get("size"),
        "permalink": f.get("permalink"),
        "preview": (f.get("preview") or "")[:300] if f.get("preview") else None,
        "is_external": f.get("is_external", False),
    }


def _files_from_archive(channel: str, ts: str) -> list[dict[str, Any]] | None:
    """Pull files for (channel, ts) from local archive's raw_json. Returns None
    if message not archived."""
    conn = archive_db.open_db()
    archive_db.init_schema(conn)
    row = conn.execute(
        """SELECT raw_json FROM messages
           WHERE channel_id=? AND ts=? AND superseded_by IS NULL
           ORDER BY edit_seq DESC LIMIT 1""",
        (channel, ts),
    ).fetchone()
    if not row:
        return None
    try:
        raw = json.loads(row["raw_json"])
    except (json.JSONDecodeError, TypeError):
        return None
    return raw.get("files") or []


def _files_from_api(channel: str, ts: str) -> list[dict[str, Any]]:
    """Fall back to API for messages not in the archive."""
    # `latest=ts inclusive=true limit=1` returns just that message
    data = call("conversations.history", channel=channel, latest=ts, inclusive="true", limit=1)
    msgs = data.get("messages") or []
    if not msgs or msgs[0].get("ts") != ts:
        return []
    return msgs[0].get("files") or []


def _message_files(channel: str, ts: str, include_replies: bool) -> dict[str, Any]:
    parent_files = _files_from_archive(channel, ts)
    if parent_files is None:
        parent_files = _files_from_api(channel, ts)

    out: dict[str, Any] = {
        "ok": True,
        "channel_id": channel,
        "ts": ts,
        "parent_files": [_file_summary(f) for f in parent_files],
        "thread_files": [],
    }

    if include_replies:
        # Walk the thread once. Prefer archive; fall back to API.
        conn = archive_db.open_db()
        archive_db.init_schema(conn)
        rows = conn.execute(
            """SELECT msg_id, ts, raw_json FROM messages
               WHERE channel_id=? AND thread_ts=? AND ts != ?
                 AND superseded_by IS NULL AND deleted_at IS NULL""",
            (channel, ts, ts),
        ).fetchall()
        if rows:
            for r in rows:
                try:
                    raw = json.loads(r["raw_json"])
                except (json.JSONDecodeError, TypeError):
                    continue
                for f in raw.get("files") or []:
                    out["thread_files"].append({"reply_ts": r["ts"], **_file_summary(f)})
        else:
            try:
                replies = call("conversations.replies", channel=channel, ts=ts, limit=200)
                for m in replies.get("messages") or []:
                    if m.get("ts") == ts:
                        continue
                    for f in m.get("files") or []:
                        out["thread_files"].append({"reply_ts": m.get("ts"), **_file_summary(f)})
            except SlackError as e:
                out["thread_files_error"] = str(e)

    out["counts"] = {
        "parent": len(out["parent_files"]),
        "thread": len(out["thread_files"]),
    }
    return out


def _is_text_like(mimetype: str | None) -> bool:
    if not mimetype:
        return False
    return any(mimetype.startswith(prefix) for prefix in _TEXT_LIKE_MIMES)


def _file_content(file_id: str, max_bytes: int) -> dict[str, Any]:
    info = call("files.info", file=file_id)
    f = info.get("file") or {}
    url = f.get("url_private_download") or f.get("url_private")
    if not url:
        return {"ok": False, "error": f"file {file_id} has no download URL"}

    body, _headers = fetch_url(
        url, max_bytes=max_bytes if _is_text_like(f.get("mimetype")) else None
    )
    summary = _file_summary(f)

    if _is_text_like(f.get("mimetype")):
        try:
            content = body.decode("utf-8", errors="replace")
        except (UnicodeDecodeError, AttributeError):
            content = body.decode("latin-1", errors="replace")
        return {
            "ok": True,
            **summary,
            "content_kind": "text",
            "content": content,
            "truncated": len(body) >= max_bytes,
            "fetched_bytes": len(body),
        }

    # Binary: save to /tmp and return the path.
    suffix = ""
    if f.get("filetype"):
        suffix = "." + f["filetype"]
    elif f.get("name") and "." in f["name"]:
        suffix = "." + f["name"].rsplit(".", 1)[1]
    fd, path = tempfile.mkstemp(prefix=f"slack-{file_id}-", suffix=suffix)
    try:
        Path(path).write_bytes(body)
    finally:
        import os

        os.close(fd)
    return {
        "ok": True,
        **summary,
        "content_kind": "binary",
        "path": path,
        "fetched_bytes": len(body),
    }


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    try:
        if name == "slack_message_files":
            return _message_files(
                args["channel"], args["ts"], bool(args.get("include_replies", True))
            )
        if name == "slack_file_content":
            return _file_content(args["file_id"], int(args.get("max_bytes", _MAX_INLINE_BYTES)))
    except SlackError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return None
