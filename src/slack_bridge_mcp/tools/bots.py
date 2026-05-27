"""Bot-identity lookup. Bot IDs (B…) are not resolvable via users.info;
Slack has a separate `bots.info` endpoint."""

from __future__ import annotations

from typing import Any

from mcp.types import Tool

from .. import caches
from ..client import SlackError

TOOLS: list[Tool] = [
    Tool(
        name="slack_bot_info",
        description=(
            "Resolve a bot ID (Bxxxxx) to its name + parent app. Cached per "
            "session. Used to label senders in messages from automation."
        ),
        inputSchema={
            "type": "object",
            "properties": {"bot_id": {"type": "string"}},
            "required": ["bot_id"],
            "additionalProperties": False,
        },
    ),
]


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    if name == "slack_bot_info":
        try:
            b = caches.get_bot(args["bot_id"])
            return {
                "ok": True,
                "id": b.get("id"),
                "name": b.get("name"),
                "app_id": b.get("app_id"),
                "deleted": b.get("deleted"),
                "icons": b.get("icons"),
            }
        except SlackError as e:
            return {"ok": False, "error": str(e)}
    return None
