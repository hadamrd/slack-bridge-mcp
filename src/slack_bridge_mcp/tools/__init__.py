"""Tool registry — aggregates each topical module's TOOLS + dispatch.

Every tools/<topic>.py module must export:
  - TOOLS: list[Tool]
  - dispatch(name: str, args: dict) -> dict | None  (None ⇒ "not mine, try next")
"""

from __future__ import annotations

from collections.abc import Callable
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
]


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    for fn in DISPATCHERS:
        result = fn(name, args)
        if result is not None:
            return result
    return {"error": f"unknown tool: {name}"}
