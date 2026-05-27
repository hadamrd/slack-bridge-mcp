"""MCP entry point. Thin orchestrator — should never grow past ~100 lines."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from . import tools
from .context import log

app = Server("slack-bridge")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return tools.TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    # Surface every error to the caller — Exception is intentional, not BLE001.
    try:
        result = tools.dispatch(name, arguments or {})
    except Exception as e:
        result = {"error": f"{type(e).__name__}: {e}"}
    return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]


async def main() -> None:
    log.info("slack-bridge-mcp starting (%d tools)", len(tools.TOOLS))
    async with stdio_server() as (reader, writer):
        await app.run(reader, writer, app.create_initialization_options())


def main_sync() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    main_sync()
