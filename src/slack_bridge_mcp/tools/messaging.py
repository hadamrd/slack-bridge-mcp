"""DMs and threads — opening conversations, fetching threaded replies,
posting to a user by id/email.

Assistant-bot tooling (Glean, summarize, etc.) lives in `tools/assistant.py`
and reuses `_open_dm` from this module.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

from mcp.types import Tool

from .. import caches
from ..client import SlackError, call

TOOLS: list[Tool] = [
    Tool(
        name="slack_find_conversation",
        description=(
            "One-shot: find a person by name/email AND fetch your recent DM "
            "with them. Combines slack_find_user → conversations.open → "
            "conversations.history. Use this for queries like 'what did "
            "<person> just say to me'. Returns the resolved user, the DM "
            "channel id, and the last N messages."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Name, email, or user id"},
                "limit": {"type": "integer", "default": 15, "minimum": 1, "maximum": 200},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_open_dm",
        description=(
            "Open (or fetch existing) DM channel with a user. Accepts user "
            "ID (Uxxxxx) or email. Returns the channel id (Dxxxxx) which "
            "you can pass to slack_channel_history or slack_post_message."
        ),
        inputSchema={
            "type": "object",
            "properties": {"user": {"type": "string"}},
            "required": ["user"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_post_dm",
        description=(
            "Send a DM to a user by ID or email. Opens the DM if needed. "
            "Returns the DM channel id and message ts."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "user": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["user", "text"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_thread",
        description=(
            "Fetch all replies in a thread. Accepts a Slack permalink "
            "(https://*.slack.com/archives/<C>/p<TS>) OR (channel, thread_ts) "
            "explicitly. Returns the parent + replies."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "permalink": {"type": "string"},
                "channel": {"type": "string"},
                "thread_ts": {"type": "string"},
                "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 1000},
            },
            "additionalProperties": False,
        },
    ),
]


def _resolve_user_to_id(user: str) -> str:
    """Accept a Slack user id, an email, or a name (cache lookup)."""
    if user.startswith("U") and user.isalnum() and len(user) >= 9:
        return user
    if "@" in user:
        data = call("users.lookupByEmail", email=user)
        return data["user"]["id"]
    candidates = caches.find_users(user, limit=3)
    if not candidates:
        raise SlackError(
            f"user {user!r} not found in cache — try slack_find_user first, "
            f"or pass a Uxxx id / email directly"
        )
    return candidates[0]["id"]


def _open_dm(user: str) -> str:
    """Returns the DM channel id (Dxxxxx)."""
    uid = _resolve_user_to_id(user)
    data = call("conversations.open", users=uid)
    cid = (data.get("channel") or {}).get("id")
    if not cid:
        raise SlackError(f"conversations.open returned no channel for {user!r}")
    return cid


_PERMALINK_RE = re.compile(r"/archives/([A-Z0-9]+)/p(\d+)$")


def _parse_permalink(url: str) -> tuple[str, str]:
    """Extract (channel_id, thread_ts) from a Slack permalink."""
    path = urlparse(url).path
    m = _PERMALINK_RE.search(path)
    if not m:
        raise SlackError(f"can't parse Slack permalink: {url}")
    channel = m.group(1)
    ts_raw = m.group(2)
    ts = ts_raw[:10] + "." + ts_raw[10:]
    return channel, ts


def _thread(
    permalink: str | None, channel: str | None, thread_ts: str | None, limit: int
) -> dict[str, Any]:
    if permalink:
        channel, thread_ts = _parse_permalink(permalink)
    if not (channel and thread_ts):
        raise SlackError("need either permalink or (channel, thread_ts)")
    data = call("conversations.replies", channel=channel, ts=thread_ts, limit=limit)
    msgs = data.get("messages") or []
    return {
        "ok": True,
        "channel": channel,
        "thread_ts": thread_ts,
        "count": len(msgs),
        "messages": [
            {
                "ts": m.get("ts"),
                "user": m.get("user") or m.get("bot_id") or m.get("username"),
                "text": m.get("text"),
                "reactions": [
                    {"name": r.get("name"), "count": r.get("count")}
                    for r in (m.get("reactions") or [])
                ],
            }
            for m in msgs
        ],
        "has_more": data.get("has_more"),
    }


def _find_conversation(query: str, limit: int) -> dict[str, Any]:
    if query.startswith("U") and query.isalnum() and len(query) >= 9:
        uid = query
    elif "@" in query:
        data = call("users.lookupByEmail", email=query)
        u = data["user"]
        caches.cache_user_obj(u)
        uid = u["id"]
    else:
        candidates = caches.find_users(query, limit=5)
        if not candidates:
            raise SlackError(
                f"no cached user matches {query!r} — pass an email "
                f"(first.last@example.com), a Uxxx id, or seed the cache by "
                f"running slack_unread_summary first"
            )
        if len(candidates) > 1:
            return {
                "ok": False,
                "ambiguous": True,
                "candidates": [
                    {
                        "id": u["id"],
                        "label": caches.label(u),
                        "email": (u.get("profile") or {}).get("email"),
                    }
                    for u in candidates
                ],
            }
        uid = candidates[0]["id"]
    user_label = caches.label(caches.get_user(uid))
    cid = _open_dm(uid)
    data = call("conversations.history", channel=cid, limit=limit)
    return {
        "ok": True,
        "user": {"id": uid, "label": user_label},
        "channel_id": cid,
        "messages": [
            {
                "ts": m.get("ts"),
                "user": m.get("user") or m.get("bot_id") or m.get("username"),
                "text": m.get("text"),
                "thread_ts": m.get("thread_ts"),
                "reply_count": m.get("reply_count"),
            }
            for m in (data.get("messages") or [])
        ],
    }


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    try:
        if name == "slack_find_conversation":
            return _find_conversation(args["query"], int(args.get("limit", 15)))
        if name == "slack_open_dm":
            return {"ok": True, "channel_id": _open_dm(args["user"])}
        if name == "slack_post_dm":
            cid = _open_dm(args["user"])
            data = call("chat.postMessage", channel=cid, text=args["text"])
            return {
                "ok": True,
                "channel_id": cid,
                "ts": data.get("ts"),
                "permalink": data.get("permalink"),
            }
        if name == "slack_thread":
            return _thread(
                args.get("permalink"),
                args.get("channel"),
                args.get("thread_ts"),
                int(args.get("limit", 100)),
            )
    except SlackError as e:
        return {"ok": False, "error": str(e)}
    return None
