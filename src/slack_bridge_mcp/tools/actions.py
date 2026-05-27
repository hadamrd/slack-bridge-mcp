"""Slack action tools — list channels, read history, post messages, search.

All tools authenticate via the bridge's xoxc+xoxd tokens (same source of
truth as slack_refresh_tokens writes). On `invalid_auth`, advise the caller
to call slack_refresh_tokens (and slack_login if the SSO session is dead).
"""

from __future__ import annotations

from typing import Any

from mcp.types import Tool

from ..client import SlackError, call

TOOLS: list[Tool] = [
    Tool(
        name="slack_channel_metadata",
        description=(
            "Bulk-fetch rich metadata for a list of public channels in ONE "
            "call (vs N×conversations.info). Returns per-channel: name, "
            "purpose, member_count, last_message_ts, file_count, "
            "recent_post_count, member_avatar_hashes. Works for channels "
            "you're NOT a member of. NOTE: only C-prefix IDs (DMs and "
            "groups will be rejected by server-side regex)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of channel IDs (C-prefix only)",
                },
            },
            "required": ["channel_ids"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_recent_activity",
        description=(
            "Bulk 'what's recent' digest for N channels in one call. "
            "Returns the last message + attachments per channel. Replaces "
            "doing N separate conversations.history(limit=1) calls. Great "
            "for 'show me activity in these channels' workflows."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of channel IDs",
                },
            },
            "required": ["channel_ids"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_my_channels",
        description=(
            "List channels the user is a member of, via the internal "
            "`client.counts` endpoint (Enterprise-Grid-friendly; "
            "`conversations.list` may be blocked in some workspaces). Returns id, "
            "has_unreads, mention_count, last_read, latest. Names are NOT "
            "included — call slack_resolve_channel or slack_search_messages "
            "to map ids ↔ names."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="slack_resolve_channel",
        description=(
            "Resolve a channel name (with or without leading '#') to its "
            "Slack ID. Useful before slack_channel_history when you only know "
            "the human name."
        ),
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_channel_history",
        description=(
            "Fetch recent messages from a channel. `channel` accepts either "
            "the channel ID (Cxxxxx / Gxxxxx) or a human name like "
            "'#pre-platform-alerts'. Default: 100 most recent messages, "
            "newest first. Returns ts, user, text, thread_ts, reply_count, "
            "and reaction counts."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "limit": {"type": "integer", "default": 100, "minimum": 1, "maximum": 1000},
                "oldest": {
                    "type": "string",
                    "description": "Inclusive lower bound (Slack ts, e.g. '1715000000.000000').",
                },
                "latest": {
                    "type": "string",
                    "description": "Inclusive upper bound (Slack ts).",
                },
            },
            "required": ["channel"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_search_messages",
        description=(
            "Search Slack messages with the same query syntax as the web UI "
            "search bar. Supports e.g. 'in:#channel keyword', 'from:@user', "
            "'after:2026-04-01'. Returns top matches with permalinks."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "count": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
                "sort": {"type": "string", "enum": ["score", "timestamp"], "default": "timestamp"},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_post_message",
        description=(
            "Post a message to a channel as the authenticated user. `channel` "
            "accepts ID or '#name'. Optional `thread_ts` to reply in a thread."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "text": {"type": "string"},
                "thread_ts": {"type": "string"},
            },
            "required": ["channel", "text"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_join_channel",
        description=(
            "Join a public channel as the authenticated user (conversations.join). "
            "Accepts ID or '#name'. Idempotent — already-joined channels return "
            "ok. Fails on private channels (need an invite via "
            "slack_invite_to_channel from a current member). After joining, "
            "the WS watcher sees that channel's events live and the archive "
            "starts populating."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID (Cxxx) or '#name'"},
            },
            "required": ["channel"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_leave_channel",
        description=(
            "Leave a channel you're currently a member of (conversations.leave). "
            "Idempotent. Note: private channels can't be re-joined unless someone "
            "invites you back — be careful."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID (Cxxx) or '#name'"},
            },
            "required": ["channel"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_invite_to_channel",
        description=(
            "Invite one or more users into a channel you are a member of "
            "(conversations.invite). Accepts user IDs or emails for the "
            "users to invite. Use to bring a colleague into a channel — for "
            "private channels this is the canonical way for them to join."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel ID or '#name'"},
                "users": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "User IDs (Uxxx) or emails to invite",
                    "minItems": 1,
                    "maxItems": 30,
                },
            },
            "required": ["channel", "users"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_react",
        description=(
            "Add an emoji reaction to a message (reactions.add). `name` is the "
            "emoji code without colons, e.g. 'eyes', 'white_check_mark', 'fire'. "
            "Idempotent at Slack's side — re-adding returns already_reacted."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "ts": {"type": "string", "description": "Slack ts of the message"},
                "name": {"type": "string", "description": "Emoji code without colons"},
            },
            "required": ["channel", "ts", "name"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_unreact",
        description="Remove an emoji reaction you added (reactions.remove).",
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "ts": {"type": "string"},
                "name": {"type": "string"},
            },
            "required": ["channel", "ts", "name"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_message_reactions",
        description=(
            "Read all reactions on a message (reactions.get). Returns each "
            "reaction's name + count + list of users. Use to detect approvals, "
            "ack signals, blocked markers, etc."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "ts": {"type": "string"},
            },
            "required": ["channel", "ts"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_mark_read",
        description=(
            "Mark a channel as read up to (and including) a given message ts "
            "(conversations.mark). Use after processing an unread mention/DM "
            "so it disappears from your inbox. If `ts` is omitted, marks "
            "everything currently visible as read."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "ts": {
                    "type": "string",
                    "description": "Mark read up to this ts; default = latest",
                },
            },
            "required": ["channel"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_update_message",
        description=(
            "Edit a message you previously posted (chat.update). Only your "
            "own messages — Slack rejects edits to others. Returns the new "
            "ts (sometimes unchanged) and updated text."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "ts": {"type": "string"},
                "text": {"type": "string"},
            },
            "required": ["channel", "ts", "text"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_delete_message",
        description=(
            "Delete a message you previously posted (chat.delete). Only your "
            "own messages. The local archive will mark it `deleted_at` via "
            "the WS feed but retain audit history."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "ts": {"type": "string"},
            },
            "required": ["channel", "ts"],
            "additionalProperties": False,
        },
    ),
]


def _channel_metadata(channel_ids: list[str]) -> dict[str, Any]:
    """conversations.bulkFetchMetadata — one call returns rich metadata
    for up to N C-prefix channels. Fails if any non-C-prefix ID included."""
    import json as _j

    bad = [c for c in channel_ids if not (c and c.startswith("C"))]
    if bad:
        return {
            "ok": False,
            "error": f"only C-prefix channels supported by this endpoint; rejected: {bad[:3]}",
        }
    r = call("conversations.bulkFetchMetadata", channel_ids=_j.dumps(channel_ids))
    return {"ok": True, "count": len(r.get("channels") or []), "channels": r.get("channels") or []}


def _recent_activity(channel_ids: list[str]) -> dict[str, Any]:
    """conversations.recentSummary — one call returns the last activity
    summary across N channels (last message + attachments per channel)."""
    import json as _j

    r = call("conversations.recentSummary", channel_ids=_j.dumps(channel_ids))
    return {
        "ok": True,
        "count": len(r.get("recent_summaries") or []),
        "channels": r.get("recent_summaries") or [],
    }


def _my_channels() -> dict[str, Any]:
    """List all channels + DMs the user is in — with FULL metadata (names,
    topics, purposes) merged with read-state (has_unreads, mention_count).

    Recipe (cracked 2026-05-09):
    - `client.channels` (with limit=1000) returns all 77+ channels with
      names — Enterprise-Grid-friendly, not the blocked conversations.list.
    - `client.counts` adds the unread/mention state per channel.

    Cache side-effect: also populates the archive `channels.name` column
    so future searches benefit from name resolution.
    """
    rich_resp = call("client.channels", limit=1000)
    counts = call("client.counts")

    # Index counts by id for fast merge
    counts_by_id = {c["id"]: c for c in counts.get("channels", []) if c.get("id")}

    # The rich response nests as channels.channels (yes, twice)
    raw_channels = rich_resp.get("channels") or {}
    channel_list = raw_channels.get("channels", []) if isinstance(raw_channels, dict) else []

    # Persist to archive — this populates the channel-name lookup for every
    # other read tool (slack_archive_search, _thread, _resume, etc.)
    try:
        from ..archive import db

        conn = db.open_db()
        for c in channel_list:
            if c.get("id"):
                db.ensure_channel(conn, c["id"], c.get("name"), is_im=bool(c.get("is_im")))
                if c.get("name"):
                    conn.execute(
                        "UPDATE channels SET name=? WHERE id=?",
                        (c["name"], c["id"]),
                    )
        conn.commit()
    except Exception:
        pass

    out_channels = []
    for c in channel_list:
        cid = c.get("id")
        merged = {
            "id": cid,
            "name": c.get("name"),
            "is_private": c.get("is_private"),
            "is_archived": c.get("is_archived"),
            "is_general": c.get("is_general"),
            "topic": (c.get("topic") or {}).get("value") or None,
            "purpose": (c.get("purpose") or {}).get("value") or None,
            "creator": c.get("creator"),
            "created": c.get("created"),
            "context_team_id": c.get("context_team_id"),
        }
        if cid in counts_by_id:
            cs = counts_by_id[cid]
            merged.update(
                {
                    "has_unreads": cs.get("has_unreads"),
                    "mention_count": cs.get("mention_count"),
                    "last_read": cs.get("last_read"),
                    "latest": cs.get("latest"),
                }
            )
        out_channels.append(merged)

    return {
        "ok": True,
        "channels": out_channels,
        "ims": [
            {
                "id": d.get("id"),
                "has_unreads": d.get("has_unreads"),
                "mention_count": d.get("mention_count"),
            }
            for d in counts.get("ims", [])
        ],
        "channels_total": len(out_channels),
        "ims_total": len(counts.get("ims") or []),
    }


def _resolve_channel(name: str) -> str:
    """Resolve a channel name → ID via cascading lookup:
    1. Already-an-ID? Return as-is.
    2. Local archive `channels.name` column (populated by `client.channels`
       on every `slack_my_channels` call). Instant, free.
    3. Fresh `client.channels(limit=1000)` if cache miss. One API call,
       returns all member channels with names.
    4. `search.messages in:<name>` as last resort (works for channels the
       user can SEE messages in even without membership).
    """
    name = name.lstrip("#")
    if (
        name.startswith(("C", "G", "D"))
        and name.isalnum()
        and name == name.upper()
        and len(name) >= 9
    ):
        return name

    # Tier 2: local archive
    try:
        from ..archive import db

        conn = db.open_db()
        row = conn.execute("SELECT id FROM channels WHERE name = ?", (name,)).fetchone()
        if row:
            return row["id"]
    except Exception:
        pass

    # Tier 3: fresh client.channels — populates the archive while we're at it
    try:
        rich = call("client.channels", limit=1000)
        outer = rich.get("channels") or {}
        chans = outer.get("channels", []) if isinstance(outer, dict) else []
        if chans:
            # Persist all of them to the archive for future hits
            try:
                from ..archive import db

                conn = db.open_db()
                for c in chans:
                    if c.get("id"):
                        db.ensure_channel(conn, c["id"], c.get("name"), is_im=bool(c.get("is_im")))
                        if c.get("name"):
                            conn.execute(
                                "UPDATE channels SET name=? WHERE id=?",
                                (c["name"], c["id"]),
                            )
                conn.commit()
            except Exception:
                pass
            for c in chans:
                if c.get("name") == name:
                    return c["id"]
    except SlackError:
        pass

    # Tier 4: search.messages — works for non-member channels we can see
    data = call("search.messages", query=f"in:{name}", count=1)
    matches = (data.get("messages") or {}).get("matches") or []
    for m in matches:
        ch = m.get("channel") or {}
        if ch.get("name") == name:
            return ch["id"]
    raise SlackError(
        f"channel '{name}' not found in local cache, client.channels, or via "
        f"search.messages. Either the name is wrong, you have no access, or "
        f"the channel is hidden from your workspace view."
    )


def _channel_history(
    channel: str, limit: int, oldest: str | None, latest: str | None
) -> dict[str, Any]:
    cid = _resolve_channel(channel)
    data = call("conversations.history", channel=cid, limit=limit, oldest=oldest, latest=latest)
    msgs = []
    for m in data.get("messages", []):
        msgs.append(
            {
                "ts": m.get("ts"),
                "user": m.get("user") or m.get("bot_id") or m.get("username"),
                "text": m.get("text"),
                "thread_ts": m.get("thread_ts"),
                "reply_count": m.get("reply_count"),
                "reactions": [
                    {"name": r.get("name"), "count": r.get("count")}
                    for r in (m.get("reactions") or [])
                ],
                "subtype": m.get("subtype"),
            }
        )
    return {
        "ok": True,
        "channel_id": cid,
        "count": len(msgs),
        "messages": msgs,
        "has_more": data.get("has_more"),
    }


def _search(query: str, count: int, sort: str) -> dict[str, Any]:
    data = call("search.messages", query=query, count=count, sort=sort)
    matches = (data.get("messages") or {}).get("matches") or []
    return {
        "ok": True,
        "total": (data.get("messages") or {}).get("total"),
        "count": len(matches),
        "matches": [
            {
                "ts": m.get("ts"),
                "channel": (m.get("channel") or {}).get("name"),
                "channel_id": (m.get("channel") or {}).get("id"),
                "user": m.get("username") or m.get("user"),
                "text": m.get("text"),
                "permalink": m.get("permalink"),
            }
            for m in matches
        ],
    }


def _post(channel: str, text: str, thread_ts: str | None) -> dict[str, Any]:
    cid = _resolve_channel(channel)
    data = call("chat.postMessage", channel=cid, text=text, thread_ts=thread_ts)
    return {"ok": True, "channel_id": cid, "ts": data.get("ts"), "permalink": data.get("permalink")}


def _join(channel: str) -> dict[str, Any]:
    cid = _resolve_channel(channel)
    data = call("conversations.join", channel=cid)
    ch = data.get("channel") or {}
    return {
        "ok": True,
        "channel_id": cid,
        "name": ch.get("name"),
        "num_members": ch.get("num_members"),
        "is_member": ch.get("is_member"),
        "already_in_channel": data.get("already_in_channel", False),
        "warning": data.get("warning"),
    }


def _leave(channel: str) -> dict[str, Any]:
    cid = _resolve_channel(channel)
    data = call("conversations.leave", channel=cid)
    return {"ok": True, "channel_id": cid, "not_in_channel": data.get("not_in_channel", False)}


def _resolve_user_id(user: str) -> str:
    """Slack user id (Uxxx) or email → Uxxx. Mirrors messaging._resolve_user_to_id
    minimally for the invite flow."""
    from .. import caches

    if user.startswith("U") and user.isalnum() and len(user) >= 9:
        return user
    if "@" in user:
        data = call("users.lookupByEmail", email=user)
        return data["user"]["id"]
    cands = caches.find_users(user, limit=3)
    if not cands:
        raise SlackError(f"user {user!r} not found in cache; pass a Uxxx id or email")
    return cands[0]["id"]


def _invite(channel: str, users: list[str]) -> dict[str, Any]:
    cid = _resolve_channel(channel)
    uids = [_resolve_user_id(u) for u in users]
    # conversations.invite accepts comma-separated ids
    data = call("conversations.invite", channel=cid, users=",".join(uids))
    ch = data.get("channel") or {}
    return {
        "ok": True,
        "channel_id": cid,
        "name": ch.get("name"),
        "invited": uids,
        "errors": data.get("errors") or [],  # per-user failure list (e.g. already_in_channel)
    }


def _react(channel: str, ts: str, name_: str) -> dict[str, Any]:
    cid = _resolve_channel(channel)
    call("reactions.add", channel=cid, timestamp=ts, name=name_.strip(":"))
    return {"ok": True, "channel_id": cid, "ts": ts, "name": name_}


def _unreact(channel: str, ts: str, name_: str) -> dict[str, Any]:
    cid = _resolve_channel(channel)
    call("reactions.remove", channel=cid, timestamp=ts, name=name_.strip(":"))
    return {"ok": True, "channel_id": cid, "ts": ts, "name": name_}


def _message_reactions(channel: str, ts: str) -> dict[str, Any]:
    cid = _resolve_channel(channel)
    data = call("reactions.get", channel=cid, timestamp=ts, full="true")
    msg = data.get("message") or {}
    return {
        "ok": True,
        "channel_id": cid,
        "ts": ts,
        "reactions": [
            {
                "name": r.get("name"),
                "count": r.get("count"),
                "users": r.get("users") or [],
            }
            for r in (msg.get("reactions") or [])
        ],
    }


def _mark_read(channel: str, ts: str | None) -> dict[str, Any]:
    cid = _resolve_channel(channel)
    if not ts:
        # Default to latest message in the channel
        hist = call("conversations.history", channel=cid, limit=1)
        msgs = hist.get("messages") or []
        if not msgs:
            return {"ok": True, "channel_id": cid, "ts": None, "noop": True}
        ts = msgs[0]["ts"]
    call("conversations.mark", channel=cid, ts=ts)
    return {"ok": True, "channel_id": cid, "ts": ts}


def _update_message(channel: str, ts: str, text: str) -> dict[str, Any]:
    cid = _resolve_channel(channel)
    data = call("chat.update", channel=cid, ts=ts, text=text)
    return {"ok": True, "channel_id": cid, "ts": data.get("ts", ts), "text": data.get("text")}


def _delete_message(channel: str, ts: str) -> dict[str, Any]:
    cid = _resolve_channel(channel)
    call("chat.delete", channel=cid, ts=ts)
    return {"ok": True, "channel_id": cid, "ts": ts}


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    try:
        if name == "slack_my_channels":
            return _my_channels()
        if name == "slack_channel_metadata":
            return _channel_metadata(args["channel_ids"])
        if name == "slack_recent_activity":
            return _recent_activity(args["channel_ids"])
        if name == "slack_resolve_channel":
            return {"ok": True, "id": _resolve_channel(args["name"])}
        if name == "slack_channel_history":
            return _channel_history(
                args["channel"], int(args.get("limit", 100)), args.get("oldest"), args.get("latest")
            )
        if name == "slack_search_messages":
            return _search(args["query"], int(args.get("count", 20)), args.get("sort", "timestamp"))
        if name == "slack_post_message":
            return _post(args["channel"], args["text"], args.get("thread_ts"))
        if name == "slack_join_channel":
            return _join(args["channel"])
        if name == "slack_leave_channel":
            return _leave(args["channel"])
        if name == "slack_invite_to_channel":
            return _invite(args["channel"], args["users"])
        if name == "slack_react":
            return _react(args["channel"], args["ts"], args["name"])
        if name == "slack_unreact":
            return _unreact(args["channel"], args["ts"], args["name"])
        if name == "slack_message_reactions":
            return _message_reactions(args["channel"], args["ts"])
        if name == "slack_mark_read":
            return _mark_read(args["channel"], args.get("ts"))
        if name == "slack_update_message":
            return _update_message(args["channel"], args["ts"], args["text"])
        if name == "slack_delete_message":
            return _delete_message(args["channel"], args["ts"])
    except SlackError as e:
        msg = str(e)
        hint = ""
        if "invalid_auth" in msg or "not_authed" in msg or "token_revoked" in msg:
            hint = " — try slack_refresh_tokens (or slack_login if SSO expired)"
        return {"ok": False, "error": msg + hint}
    return None
