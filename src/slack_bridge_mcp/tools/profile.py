"""User profile tools — set status, set display name (admin tools omitted)."""

from __future__ import annotations

import json
import time
from typing import Any

from mcp.types import Tool

from ..client import SlackError, call

TOOLS: list[Tool] = [
    Tool(
        name="slack_set_status",
        description=(
            "Update the authenticated user's Slack status. Pass empty `text` "
            "and `emoji` to clear it. `expires_in_minutes` (optional) sets "
            "auto-clear; omit for no expiry."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "emoji": {
                    "type": "string",
                    "description": "Emoji code with colons, e.g. ':spiral_calendar_pad:'",
                    "default": "",
                },
                "expires_in_minutes": {"type": "integer", "minimum": 1, "maximum": 10_080},
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_get_status",
        description="Read the authenticated user's current status (text, emoji, expiration).",
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
]


def _set_status(text: str, emoji: str, expires_in_minutes: int | None) -> dict[str, Any]:
    profile: dict[str, Any] = {"status_text": text, "status_emoji": emoji or ""}
    if expires_in_minutes is not None:
        profile["status_expiration"] = int(time.time()) + expires_in_minutes * 60
    else:
        profile["status_expiration"] = 0
    data = call("users.profile.set", profile=json.dumps(profile))
    p = data.get("profile") or {}
    return {
        "ok": True,
        "status_text": p.get("status_text"),
        "status_emoji": p.get("status_emoji"),
        "status_expiration": p.get("status_expiration"),
    }


def _get_status() -> dict[str, Any]:
    data = call("users.profile.get")
    p = data.get("profile") or {}
    return {
        "ok": True,
        "status_text": p.get("status_text"),
        "status_emoji": p.get("status_emoji"),
        "status_expiration": p.get("status_expiration"),
    }


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    try:
        if name == "slack_set_status":
            return _set_status(
                args["text"],
                args.get("emoji", ""),
                args.get("expires_in_minutes"),
            )
        if name == "slack_get_status":
            return _get_status()
    except SlackError as e:
        return {"ok": False, "error": str(e)}
    return None
