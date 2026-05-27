"""User-identity tools: resolve Slack user IDs to human-readable info."""

from __future__ import annotations

from typing import Any

from mcp.types import Tool

from .. import caches
from ..client import SlackError, call

TOOLS: list[Tool] = [
    Tool(
        name="slack_find_user",
        description=(
            "Fast fuzzy lookup against the persistent users cache "
            "(SLACK_BRIDGE_USERS_CACHE_PATH). Substring match on "
            "real name, display name, handle, email, title. Returns 0-N "
            "candidates. If no cache hit and query looks like an email, "
            "falls back to users.lookupByEmail and seeds the cache. "
            "Cache fills automatically as other tools resolve user IDs — "
            "after a few sessions, most colleagues are findable instantly."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_seed_cache",
        description=(
            "Warm the persistent users cache by fetching the sender of the "
            "most recent message in each of your DMs. After running this once, "
            "slack_find_user can resolve any DM partner by name instantly. "
            "Safe to run repeatedly — only fetches uids not already cached."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_user_info",
        description=(
            "Look up a Slack user by ID (Uxxxxx) or email. Returns id, name, "
            "real_name, display_name, email (if visible), title, tz, "
            "is_admin, is_deleted. Cached in-process per session."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "user": {"type": "string", "description": "User ID (Uxxxxx) or email"},
            },
            "required": ["user"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_users_lookup",
        description=(
            "Bulk resolve multiple user IDs to {id, label, real_name, "
            "email}. Useful before rendering message lists with sender "
            "names. Shares the same cache as slack_user_info."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "user_ids": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["user_ids"],
            "additionalProperties": False,
        },
    ),
]


def _project(user: dict[str, Any]) -> dict[str, Any]:
    profile = user.get("profile") or {}
    return {
        "id": user.get("id"),
        "label": caches.label(user),
        "name": user.get("name"),
        "real_name": profile.get("real_name"),
        "display_name": profile.get("display_name"),
        "email": profile.get("email"),
        "title": profile.get("title"),
        "tz": user.get("tz"),
        "is_admin": user.get("is_admin"),
        "is_bot": user.get("is_bot"),
        "deleted": user.get("deleted"),
    }


def _user_info(user: str) -> dict[str, Any]:
    if "@" in user:
        data = call("users.lookupByEmail", email=user)
        u = data["user"]
        caches._users[u["id"]] = u  # warm cache
        return {"ok": True, "user": _project(u)}
    return {"ok": True, "user": _project(caches.get_user(user))}


def _users_lookup(user_ids: list[str]) -> dict[str, Any]:
    resolved = caches.get_users_bulk(user_ids)
    return {
        "ok": True,
        "count": len(resolved),
        "users": {uid: _project(u) for uid, u in resolved.items()},
    }


def _find_user(query: str, limit: int) -> dict[str, Any]:
    hits = caches.find_users(query, limit=limit)
    if not hits and "@" in query:
        # email-shaped miss: try the lookup endpoint and seed cache
        data = call("users.lookupByEmail", email=query)
        u = data["user"]
        caches.cache_user_obj(u)
        hits = [u]
    return {
        "ok": True,
        "query": query,
        "count": len(hits),
        "candidates": [_project(u) for u in hits],
        "hint": (
            ""
            if hits
            else "no match in cache — seed it by running slack_unread_summary, "
            "slack_channel_history on a busy channel, or pass an email like "
            "'first.last@example.com'."
        ),
    }


def _seed_cache() -> dict[str, Any]:
    counts = call("client.counts")
    seen_uids: set[str] = set()
    fetched: list[str] = []
    for im in counts.get("ims") or []:
        try:
            hist = call("conversations.history", channel=im["id"], limit=1)
        except SlackError:
            continue
        for m in hist.get("messages") or []:
            uid = m.get("user")
            if uid and uid.startswith("U") and uid not in seen_uids:
                seen_uids.add(uid)
                if uid not in caches._users:
                    try:
                        caches.get_user(uid)
                        fetched.append(uid)
                    except SlackError:
                        pass
    return {
        "ok": True,
        "dm_count": len(counts.get("ims") or []),
        "newly_cached": len(fetched),
        "total_cached_users": len(caches._users),
    }


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    try:
        if name == "slack_find_user":
            return _find_user(args["query"], int(args.get("limit", 10)))
        if name == "slack_seed_cache":
            return _seed_cache()
        if name == "slack_user_info":
            return _user_info(args["user"])
        if name == "slack_users_lookup":
            return _users_lookup(args["user_ids"])
    except SlackError as e:
        return {"ok": False, "error": str(e)}
    return None
