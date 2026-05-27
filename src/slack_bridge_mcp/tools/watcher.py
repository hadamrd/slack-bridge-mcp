"""MCP tools to manage the slack-watcher daemon's rules.

Rules live at SLACK_BRIDGE_WATCHER_RULES_PATH. The daemon
reloads them automatically every 50 events (or on SIGHUP). These tools
just expose CRUD-on-disk + status probes; the daemon is its own process.
"""

from __future__ import annotations

import contextlib
import subprocess
from typing import Any

import yaml
from mcp.types import Tool

from ..config import settings

RULES_PATH = settings().watcher_rules_path
LOG_PATH = settings().watcher_log_path

TOOLS: list[Tool] = [
    Tool(
        name="slack_watcher_status",
        description=(
            "Check if the slack-watcher daemon is running, count active rules, "
            "show the last 20 log lines."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="slack_watcher_rules_list",
        description="List all rules from SLACK_BRIDGE_WATCHER_RULES_PATH.",
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="slack_watcher_rules_set",
        description=(
            "Replace the entire rules file with the given list. Validates "
            "YAML serialisation, writes atomically. Daemon picks up changes "
            "on next event (≤50 events lag) or via signal."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "rules": {"type": "array", "description": "Full rules list (replaces file)"},
            },
            "required": ["rules"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_watcher_rules_add",
        description="Append a single rule to the rules file. Convenient for scripted setup.",
        inputSchema={
            "type": "object",
            "properties": {"rule": {"type": "object"}},
            "required": ["rule"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_watcher_logs_tail",
        description="Return the last N lines of the watcher log.",
        inputSchema={
            "type": "object",
            "properties": {
                "lines": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500}
            },
            "additionalProperties": False,
        },
    ),
]


def _load_rules() -> list[dict[str, Any]]:
    if not RULES_PATH.exists():
        return []
    return yaml.safe_load(RULES_PATH.read_text()) or []


def _save_rules(rules: list[dict[str, Any]]) -> None:
    RULES_PATH.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = RULES_PATH.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(rules, sort_keys=False, default_flow_style=False))
    import os

    os.replace(tmp, RULES_PATH)
    os.chmod(RULES_PATH, 0o600)


def _status() -> dict[str, Any]:
    pgrep = subprocess.run(
        ["pgrep", "-fa", "slack_bridge_mcp.watcher"],
        capture_output=True,
        text=True,
    )
    pids = [ln.split()[0] for ln in pgrep.stdout.strip().splitlines() if ln.split()]
    rules = _load_rules()
    enabled = [r for r in rules if r.get("enabled", True)]
    log_tail = []
    if LOG_PATH.exists():
        with contextlib.suppress(OSError):
            log_tail = LOG_PATH.read_text().splitlines()[-20:]
    return {
        "ok": True,
        "running": bool(pids),
        "pids": pids,
        "rules_total": len(rules),
        "rules_enabled": len(enabled),
        "rules_path": str(RULES_PATH),
        "log_path": str(LOG_PATH),
        "last_log_lines": log_tail,
    }


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    try:
        if name == "slack_watcher_status":
            return _status()
        if name == "slack_watcher_rules_list":
            return {"ok": True, "rules": _load_rules()}
        if name == "slack_watcher_rules_set":
            rules = args["rules"]
            if not isinstance(rules, list):
                return {"ok": False, "error": "rules must be a list"}
            _save_rules(rules)
            return {"ok": True, "count": len(rules), "path": str(RULES_PATH)}
        if name == "slack_watcher_rules_add":
            rule = args["rule"]
            if not isinstance(rule, dict) or not rule.get("name"):
                return {"ok": False, "error": "rule must be a dict with a 'name'"}
            rules = _load_rules()
            rules.append(rule)
            _save_rules(rules)
            return {"ok": True, "count": len(rules)}
        if name == "slack_watcher_logs_tail":
            n = int(args.get("lines", 50))
            if not LOG_PATH.exists():
                return {"ok": True, "lines": [], "note": "no log yet"}
            lines = LOG_PATH.read_text().splitlines()[-n:]
            return {"ok": True, "lines": lines}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return None
