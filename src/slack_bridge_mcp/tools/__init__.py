"""Tool registry — aggregates each topical module's TOOLS + dispatch.

Every tools/<topic>.py module must export:
  - TOOLS: list[Tool]
  - dispatch(name: str, args: dict) -> dict | None  (None ⇒ "not mine, try next")

Pet mutation guard
------------------
When a tool runs inside a pet subprocess (the runner sets SLACK_BRIDGE_PET_*
env vars), every *mutating* Slack call is funnelled through ``_guarded_dispatch``
which snapshots the original content, records the action to the pet's audit
JSONL, and — under dry-run — simulates the mutation instead of executing it.
This is the single chokepoint that makes pets transparent + reversible.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mcp.types import Tool

from . import (
    actions,
    archive,
    assistant,
    auth,
    bots,
    digest,
    example,
    files,
    messaging,
    pets,
    profile,
    users,
    watcher,
)

TOOLS: list[Tool] = [
    *example.TOOLS,
    *auth.TOOLS,
    *actions.TOOLS,
    *users.TOOLS,
    *bots.TOOLS,
    *messaging.TOOLS,
    *assistant.TOOLS,
    *profile.TOOLS,
    *digest.TOOLS,
    *archive.TOOLS,
    *files.TOOLS,
    *watcher.TOOLS,
    *pets.TOOLS,
]

DISPATCHERS: list[Callable[[str, dict[str, Any]], dict[str, Any] | None]] = [
    example.dispatch,
    auth.dispatch,
    actions.dispatch,
    users.dispatch,
    bots.dispatch,
    messaging.dispatch,
    assistant.dispatch,
    profile.dispatch,
    digest.dispatch,
    archive.dispatch,
    files.dispatch,
    watcher.dispatch,
    pets.dispatch,
]

# Slack tools that mutate workspace state — audited + dry-run-guarded for pets.
_MUTATING: frozenset[str] = frozenset(
    {
        "slack_post_message",
        "slack_post_dm",
        "slack_update_message",
        "slack_delete_message",
        "slack_react",
        "slack_unreact",
    }
)


def _raw_dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    for fn in DISPATCHERS:
        result = fn(name, args)
        if result is not None:
            return result
    return {"error": f"unknown tool: {name}"}


def _pet_context() -> dict[str, Any] | None:
    """Pet audit context from env (set by pets.runner), or None outside a pet."""
    audit_dir = os.environ.get("SLACK_BRIDGE_PET_AUDIT_DIR")
    if not audit_dir:
        return None
    return {
        "name": os.environ.get("SLACK_BRIDGE_PET_NAME", "pet"),
        "audit_path": Path(audit_dir) / "actions.jsonl",
        "dry_run": os.environ.get("SLACK_BRIDGE_PET_DRYRUN", "") in ("1", "true", "True"),
    }


def _guarded_dispatch(name: str, args: dict[str, Any], pet: dict[str, Any]) -> dict[str, Any]:
    from ..pets import audit

    audit_path: Path = pet["audit_path"]
    dry: bool = pet["dry_run"]

    # Resolve channel + snapshot original content for reversibility.
    channel_id: str | None = args.get("channel")
    if channel_id:
        try:
            from .actions import _resolve_channel

            channel_id = _resolve_channel(channel_id)
        except Exception:
            pass  # keep raw value; snapshot may just be empty
    original = None
    if name in ("slack_update_message", "slack_delete_message") and channel_id and args.get("ts"):
        original = audit.fetch_message_text(channel_id, args["ts"])

    record = audit.build_record(pet["name"], name, args, channel_id, original, dry)

    if dry:
        record["would_have"] = {"tool": name, "args": args}
        audit.append(audit_path, record)
        return {"ok": True, "dry_run": True, "pet": pet["name"], "would_have": record["would_have"]}

    result = _raw_dispatch(name, args)
    record["result"] = result
    if isinstance(result, dict):
        record["result_ts"] = result.get("ts")
    audit.append(audit_path, record)
    return result


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    pet = _pet_context()
    if pet and name in _MUTATING:
        return _guarded_dispatch(name, args, pet)
    return _raw_dispatch(name, args)
