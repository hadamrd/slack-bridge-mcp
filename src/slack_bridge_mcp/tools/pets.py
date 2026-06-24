"""MCP control surface for Slack pets — list / status / enable / disable /
dry-run / logs / undo.

The pets themselves run inside the supervisor daemon (``slack_bridge_mcp.watcher``);
these tools are the human/operator interface to them. Enabling/disabling flips
``enabled`` in the pet's ``bot.yml``; the supervisor hot-reloads within ~50 events.
"""

from __future__ import annotations

from typing import Any

from mcp.types import Tool

from ..pets import audit, registry

TOOLS: list[Tool] = [
    Tool(
        name="slack_pet_list",
        description=(
            "List all Slack pets (AI helper agents) with their enabled/dry-run "
            "state, archetype, trigger, granted capabilities, and fire stats. "
            "This is the 'what bots do I have running' view."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="slack_pet_status",
        description=(
            "Detailed status for one pet (resolved allowed-tools, trigger, "
            "fire count, last fired) — or the whole fleet if name is omitted."
        ),
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_pet_enable",
        description="Enable a pet (sets enabled:true in its bot.yml). Supervisor hot-reloads.",
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_pet_disable",
        description=(
            "Disable ('kill') a pet (sets enabled:false in its bot.yml) so it "
            "stops reacting to events. Reversible with slack_pet_enable."
        ),
        inputSchema={
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_pet_dry_run",
        description=(
            "Toggle dry-run for a pet. In dry-run the pet decides + records what "
            "it WOULD do to its audit log but never mutates Slack. Use to vet a "
            "new/edited pet before letting it act live."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "on": {"type": "boolean", "default": True},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_pet_logs",
        description=(
            "Tail a pet's run log + its action audit trail (every Slack mutation "
            "it made or would have made, with the original text snapshotted)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "lines": {"type": "integer", "default": 40, "minimum": 1, "maximum": 500},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_pet_undo",
        description=(
            "Reverse a pet's mutation on a given message ts using the audit "
            "snapshot: restores edited text, reposts deleted text, or deletes a "
            "posted message. Looks up the most recent reversible action on that ts."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "ts": {"type": "string", "description": "The message ts the pet acted on"},
            },
            "required": ["name", "ts"],
            "additionalProperties": False,
        },
    ),
]


def _logs(name: str, lines: int) -> dict[str, Any]:
    spec = registry.load_one(name)
    if not spec:
        return {"ok": False, "error": f"no such pet: {name}"}
    run_log: list[str] = []
    if spec.log_path.exists():
        run_log = spec.log_path.read_text().splitlines()[-lines:]
    audit_tail = audit.read_all(spec.audit_path)[-lines:]
    return {
        "ok": True,
        "pet": name,
        "run_log": run_log,
        "audit": audit_tail,
    }


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    try:
        if name == "slack_pet_list":
            return registry.status()
        if name == "slack_pet_status":
            pet = args.get("name")
            return registry.one_status(pet) if pet else registry.status()
        if name == "slack_pet_enable":
            return registry.set_field(args["name"], "enabled", True)
        if name == "slack_pet_disable":
            return registry.set_field(args["name"], "enabled", False)
        if name == "slack_pet_dry_run":
            return registry.set_field(args["name"], "dry_run", bool(args.get("on", True)))
        if name == "slack_pet_logs":
            return _logs(args["name"], int(args.get("lines", 40)))
        if name == "slack_pet_undo":
            spec = registry.load_one(args["name"])
            if not spec:
                return {"ok": False, "error": f"no such pet: {args['name']}"}
            return audit.undo(spec.audit_path, args["ts"])
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return None
