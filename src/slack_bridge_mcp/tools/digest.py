"""High-level "what should I read next" tools.

These compose the lower-level primitives (client.counts, conversations.history,
users.info) into one-shot summaries — saves the agent from having to chain
4+ tool calls to answer a question like "what's unread?".
"""

from __future__ import annotations

import contextlib
from typing import Any

from mcp.types import Tool

from .. import caches
from ..client import SlackError, call

TOOLS: list[Tool] = [
    Tool(
        name="slack_my_mentions",
        description=(
            "List recent messages where the authenticated user was @mentioned. "
            "Backed by search.messages with the canonical `<@USERID>` query. "
            "By default filters out bot-mentions (opsgenie, jira, IAM portal, etc.) "
            "since those are usually noise. Returns channel, sender, text, "
            "permalink, ts."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "days_back": {"type": "integer", "default": 7, "minimum": 1, "maximum": 365},
                "count": {"type": "integer", "default": 30, "minimum": 1, "maximum": 100},
                "exclude_bots": {
                    "type": "boolean",
                    "default": True,
                    "description": "Drop matches where the sender is a bot/app (recommended).",
                },
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_unread_summary",
        description=(
            "One-shot summary of unread DMs and unread channels. For each "
            "unread DM, fetches the latest 1-3 messages and resolves the "
            "sender name. Channels return id + unread/mention counts only "
            "(no preview; some Slack workspaces block bulk channel lookup, "
            "use slack_resolve_channel for individual ones)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "dm_preview_messages": {
                    "type": "integer",
                    "default": 2,
                    "minimum": 1,
                    "maximum": 10,
                    "description": "How many recent messages to fetch per unread DM",
                },
                "include_read_dms": {
                    "type": "boolean",
                    "default": False,
                    "description": "Also include DMs with no unread (just the IDs).",
                },
            },
            "additionalProperties": False,
        },
    ),
]


def _unread_summary(dm_preview_messages: int, include_read_dms: bool) -> dict[str, Any]:
    counts = call("client.counts")

    # 1) Pull unread DMs
    ims = counts.get("ims") or []
    unread_ims = [d for d in ims if d.get("has_unreads")]

    # 2) Fetch preview for each unread DM (sequential — usually <10 DMs)
    dm_threads: list[dict[str, Any]] = []
    actor_ids: set[str] = set()
    for im in unread_ims:
        try:
            hist = call("conversations.history", channel=im["id"], limit=dm_preview_messages)
        except SlackError as e:
            dm_threads.append({"id": im["id"], "error": str(e)})
            continue
        msgs = hist.get("messages") or []
        for m in msgs:
            uid = m.get("user") or m.get("bot_id")
            if isinstance(uid, str) and (uid.startswith("U") or uid.startswith("B")):
                actor_ids.add(uid)
        dm_threads.append(
            {
                "id": im["id"],
                "mention_count": im.get("mention_count"),
                "messages": [
                    {
                        "ts": m.get("ts"),
                        "user": m.get("user") or m.get("bot_id") or m.get("username"),
                        "text": (m.get("text") or "")[:300],
                    }
                    for m in msgs
                ],
            }
        )

    # 3) Resolve all actor (user + bot) names. Bots use bots.info; users use
    #    users.info — caches.actor_label routes correctly and best-effort
    #    swallows individual failures.
    actors: dict[str, str] = {}
    for aid in sorted(actor_ids):
        try:
            actors[aid] = caches.actor_label(aid)
        except Exception:
            pass  # leave unresolved

    # 4) Stitch labels into the previews
    for thread in dm_threads:
        for m in thread.get("messages", []) or []:
            aid = m["user"]
            if aid in actors:
                m["sender"] = actors[aid]

    # 5) Optional: include all DM IDs even if read
    all_ims = ims if include_read_dms else None

    # 6) Channel side: just unread counts, IDs only
    channels = counts.get("channels") or []
    unread_channels = [
        {
            "id": c.get("id"),
            "mention_count": c.get("mention_count"),
            "last_read": c.get("last_read"),
            "latest": c.get("latest"),
        }
        for c in channels
        if c.get("has_unreads")
    ]

    return {
        "ok": True,
        "unread_dm_count": len(unread_ims),
        "unread_dms": dm_threads,
        "unread_channel_count": len(unread_channels),
        "unread_channels": unread_channels,
        "all_dm_ids": [d["id"] for d in all_ims] if all_ims else None,
        "tip": (
            "If you want channel names, call slack_resolve_channel on the IDs "
            "you care about; some Slack workspaces block bulk channel enumeration."
        ),
    }


def _my_mentions(days_back: int, count: int, exclude_bots: bool) -> dict[str, Any]:
    import datetime

    # Resolve own user_id via auth.test (single call; cheap)
    me = call("auth.test")
    my_id = me.get("user_id")
    if not my_id:
        return {"ok": False, "error": "auth.test did not return user_id"}

    after = (datetime.date.today() - datetime.timedelta(days=days_back)).isoformat()
    # Slack search treats <@USERID> as "messages mentioning that user".
    # Most @-mentions are bot noise (opsgenie / IAM / jira); we page through
    # results until we have enough human mentions or hit the cap.
    max_pages = 5 if exclude_bots else 1
    out: list[dict[str, Any]] = []
    bots_dropped = 0
    total = None
    for page in range(1, max_pages + 1):
        data = call(
            "search.messages",
            query=f"<@{my_id}> after:{after}",
            count=100,
            page=page,
            sort="timestamp",
        )
        if total is None:
            total = (data.get("messages") or {}).get("total")
        matches = (data.get("messages") or {}).get("matches") or []
        if not matches:
            break
        for m in matches:
            sender_id = m.get("user") or m.get("bot_id") or m.get("username")
            is_bot = (
                bool(m.get("username"))
                or bool(m.get("bot_id"))
                or (isinstance(m.get("user"), str) and m["user"].startswith("B"))
            )
            # Also catch U-prefixed app-bots (IncidentBot, DX, etc.) by
            # checking users.info `is_bot` once cached.
            sender_label = sender_id
            if isinstance(sender_id, str) and sender_id.startswith("U"):
                try:
                    user_obj = caches.get_user(sender_id)
                    if user_obj.get("is_bot"):
                        is_bot = True
                    sender_label = caches.label(user_obj)
                except Exception:
                    pass
            elif isinstance(sender_id, str) and sender_id.startswith("B"):
                with contextlib.suppress(Exception):
                    sender_label = caches.actor_label(sender_id)
            if exclude_bots and is_bot:
                bots_dropped += 1
                continue
            out.append(
                {
                    "ts": m.get("ts"),
                    "channel": (m.get("channel") or {}).get("name"),
                    "channel_id": (m.get("channel") or {}).get("id"),
                    "sender_id": sender_id,
                    "sender": sender_label,
                    "is_bot": is_bot,
                    "text": m.get("text"),
                    "permalink": m.get("permalink"),
                }
            )
            if len(out) >= count:
                break
        if len(out) >= count:
            break
        # No more pages?
        paging = (data.get("messages") or {}).get("paging") or {}
        if page >= int(paging.get("pages", page)):
            break

    return {
        "ok": True,
        "total": (data.get("messages") or {}).get("total"),
        "count": len(out),
        "since": after,
        "bots_dropped": bots_dropped,
        "matches": out,
    }


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    try:
        if name == "slack_unread_summary":
            return _unread_summary(
                int(args.get("dm_preview_messages", 2)),
                bool(args.get("include_read_dms", False)),
            )
        if name == "slack_my_mentions":
            return _my_mentions(
                int(args.get("days_back", 7)),
                int(args.get("count", 30)),
                bool(args.get("exclude_bots", True)),
            )
    except SlackError as e:
        return {"ok": False, "error": str(e)}
    return None
