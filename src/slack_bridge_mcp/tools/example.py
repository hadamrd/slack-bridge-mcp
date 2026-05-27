"""Example tool — delete or replace once you have real ones."""

from __future__ import annotations

from typing import Any

from mcp.types import Tool

TOOLS: list[Tool] = [
    Tool(
        name="slack_bridge_ping",
        description="Health check. Returns {ok: true} so you can confirm the MCP is wired.",
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
]


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    if name == "slack_bridge_ping":
        return {"ok": True}
    return None
