"""Process-local caches shared across tool modules.

The users cache is persisted to disk (`SLACK_BRIDGE_USERS_CACHE_PATH`)
so name lookups are instant across MCP restarts. Bots are cached in-memory
only — they're cheap to refetch and don't accumulate the same way.

Cache writes are best-effort and atomic-ish (write to .tmp, rename). A
corrupted file just costs us one cold-start refetch.
"""

from __future__ import annotations

import json
import os
from typing import Any

from .client import call
from .config import settings

_USERS_CACHE = settings().users_cache_path


def _load_users_cache() -> dict[str, dict[str, Any]]:
    if not _USERS_CACHE.exists():
        return {}
    try:
        return json.loads(_USERS_CACHE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_users_cache() -> None:
    try:
        _USERS_CACHE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = _USERS_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(_users, separators=(",", ":")))
        os.replace(tmp, _USERS_CACHE)
        os.chmod(_USERS_CACHE, 0o600)
    except OSError:
        pass  # cache is best-effort; never fail a request because of it


_users: dict[str, dict[str, Any]] = _load_users_cache()
_bots: dict[str, dict[str, Any]] = {}


def get_user(user_id: str) -> dict[str, Any]:
    """Return cached user object or fetch via users.info. Persists on miss."""
    if user_id not in _users:
        data = call("users.info", user=user_id)
        _users[user_id] = data["user"]
        _save_users_cache()
    return _users[user_id]


def find_users(query: str, limit: int = 10) -> list[dict[str, Any]]:
    """Substring-match against cached users on label / name / email / title.
    Local-only — call get_user / users.lookupByEmail for cache misses.
    """
    q = query.lower().strip()
    if not q:
        return []
    hits: list[tuple[int, dict[str, Any]]] = []
    for u in _users.values():
        if u.get("deleted"):
            continue
        haystack = " ".join(
            filter(
                None,
                [
                    label(u),
                    u.get("name") or "",
                    (u.get("profile") or {}).get("email") or "",
                    (u.get("profile") or {}).get("title") or "",
                ],
            )
        ).lower()
        if q in haystack:
            # crude scoring: exact label > startswith > contains
            score = 0 if label(u).lower() == q else 1 if haystack.startswith(q) else 2
            hits.append((score, u))
    hits.sort(key=lambda x: x[0])
    return [u for _, u in hits[:limit]]


def cache_user_obj(user: dict[str, Any]) -> None:
    """External callers may pass a fully-formed user object (e.g. from
    users.lookupByEmail) and have it merged into the cache."""
    if user.get("id"):
        _users[user["id"]] = user
        _save_users_cache()


def get_bot(bot_id: str) -> dict[str, Any]:
    """Return cached bot object or fetch via bots.info."""
    if bot_id not in _bots:
        data = call("bots.info", bot=bot_id)
        _bots[bot_id] = data["bot"]
    return _bots[bot_id]


def actor_label(actor_id: str) -> str:
    """Resolve any U… or B… to a human label, falling back to the raw id."""
    try:
        if actor_id.startswith("U"):
            return label(get_user(actor_id))
        if actor_id.startswith("B"):
            return get_bot(actor_id).get("name") or actor_id
    except Exception:
        pass
    return actor_id


def get_users_bulk(user_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Resolve many user_ids → user objects, sharing the cache."""
    return {uid: get_user(uid) for uid in dict.fromkeys(user_ids)}  # dedupe, preserve order


def label(user: dict[str, Any]) -> str:
    """Human-readable label: real name → display name → name → id."""
    profile = user.get("profile") or {}
    return (
        profile.get("real_name_normalized")
        or profile.get("real_name")
        or profile.get("display_name_normalized")
        or profile.get("display_name")
        or user.get("name")
        or user.get("id", "?")
    )
