"""Load + validate pet specs (``bot.yml``) into ``BotSpec`` objects.

A pet directory looks like::

    <bots_dir>/<name>/
        bot.yml
        CLAUDE.md
        .claude/skills/<skill>/SKILL.md
        memory/
        audit/actions.jsonl
        logs/run.log

``bot.yml`` schema is documented in ``docs/pets-framework.md``. This module is
the single source of truth for what a valid spec is; everything downstream
(registry, runner, control tools) consumes ``BotSpec``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Short capability name -> the slack-bridge MCP tool it unlocks. The full tool
# name handed to ``claude -p --allowedTools`` is mcp__slack-bridge__slack_<short>.
# Only these may appear in capabilities.slack_tools — anything else is rejected
# so a typo can't silently grant (or fail to grant) a capability.
SLACK_TOOLS: frozenset[str] = frozenset(
    {
        # read
        "channel_history",
        "thread",
        "search_messages",
        "message_reactions",
        "find_conversation",
        "open_dm",
        # write / mutate
        "post_message",
        "post_dm",
        "update_message",
        "delete_message",
        "react",
        "unreact",
        "mark_read",
        # native AI helpers
        "summarize_thread",
        "summarize_channel_unreads",
    }
)

# Slack tools that mutate workspace state. The central guard in tools/actions.py
# snapshots + audits these before running, and no-ops them under dry_run.
MUTATING_SLACK_TOOLS: frozenset[str] = frozenset(
    {"post_message", "post_dm", "update_message", "delete_message", "react", "unreact"}
)

MEMORY_MODES: frozenset[str] = frozenset({"none", "read", "read_append"})
ARCHETYPES: frozenset[str] = frozenset({"responder", "augmenter", "observer"})

# Match keys understood by the rule engine (slack_bridge_mcp.watcher.rules).
TRIGGER_KEYS: frozenset[str] = frozenset(
    {
        "channel_id",
        "channel_name",
        "text_contains",
        "text_regex",
        "from_user",
        "from_bot",
        "subtype",
        "is_thread_reply",
    }
)

SLACK_TOOL_PREFIX = "mcp__slack-bridge__slack_"


class SpecError(ValueError):
    """Raised when a bot.yml is malformed. Message is human-facing."""


@dataclass(frozen=True)
class BotSpec:
    name: str
    directory: Path
    enabled: bool
    dry_run: bool
    archetype: str
    description: str
    trigger: dict[str, Any]
    ignore_self: bool
    rate_limit_per_min: int
    # runtime
    model: str | None
    timeout_s: int
    grounding_dirs: list[str]
    # capabilities
    memory: str
    mcp_servers: list[str]
    slack_tools: list[str]
    extra_tools: list[str]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # ---- derived ---------------------------------------------------------

    @property
    def claude_md(self) -> Path:
        return self.directory / "CLAUDE.md"

    @property
    def skills_dir(self) -> Path:
        return self.directory / ".claude" / "skills"

    @property
    def memory_dir(self) -> Path:
        return self.directory / "memory"

    @property
    def audit_path(self) -> Path:
        return self.directory / "audit" / "actions.jsonl"

    @property
    def log_path(self) -> Path:
        return self.directory / "logs" / "run.log"

    @property
    def can_write_memory(self) -> bool:
        return self.memory == "read_append"

    def allowed_tools(self) -> list[str]:
        """The fully-qualified ``--allowedTools`` list for ``claude -p``."""
        tools: list[str] = list(self.extra_tools)
        if self.can_write_memory and "Write" not in tools:
            tools.append("Write")
        if self.can_write_memory and "Edit" not in tools:
            tools.append("Edit")
        for short in self.slack_tools:
            tools.append(SLACK_TOOL_PREFIX + short)
        # Allow every tool from each granted non-slack MCP server (wildcard).
        for srv in self.mcp_servers:
            if srv == "slack-bridge":
                continue
            tools.append(f"mcp__{srv}__*")
        # de-dup, preserve order
        seen: set[str] = set()
        out: list[str] = []
        for t in tools:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    def mutating_slack_tools(self) -> list[str]:
        return [t for t in self.slack_tools if t in MUTATING_SLACK_TOOLS]


def _as_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise SpecError(f"{where} must be a list of strings")
    return value


def parse_spec(directory: Path, data: dict[str, Any]) -> BotSpec:
    """Validate a parsed bot.yml dict against ``directory`` -> ``BotSpec``."""
    if not isinstance(data, dict):
        raise SpecError(f"{directory.name}/bot.yml must be a mapping")

    name = data.get("name") or directory.name
    if not isinstance(name, str) or not name.strip():
        raise SpecError(f"{directory}: 'name' must be a non-empty string")

    archetype = data.get("archetype", "responder")
    if archetype not in ARCHETYPES:
        raise SpecError(f"{name}: archetype {archetype!r} not in {sorted(ARCHETYPES)}")

    trigger = data.get("trigger") or {}
    if not isinstance(trigger, dict) or not trigger:
        raise SpecError(f"{name}: 'trigger' must be a non-empty mapping")
    bad_keys = set(trigger) - TRIGGER_KEYS
    if bad_keys:
        raise SpecError(
            f"{name}: unknown trigger keys {sorted(bad_keys)}; valid: {sorted(TRIGGER_KEYS)}"
        )

    runtime = data.get("runtime") or {}
    caps = data.get("capabilities") or {}

    memory = caps.get("memory", "read")
    if memory not in MEMORY_MODES:
        raise SpecError(f"{name}: capabilities.memory {memory!r} not in {sorted(MEMORY_MODES)}")

    slack_tools = _as_list(caps.get("slack_tools"), f"{name}: capabilities.slack_tools")
    unknown = [t for t in slack_tools if t not in SLACK_TOOLS]
    if unknown:
        raise SpecError(f"{name}: unknown slack_tools {unknown}; valid: {sorted(SLACK_TOOLS)}")
    # slack-bridge is implied whenever any slack tool is granted
    mcp_servers = _as_list(caps.get("mcp_servers"), f"{name}: capabilities.mcp_servers")
    if slack_tools and "slack-bridge" not in mcp_servers:
        mcp_servers = ["slack-bridge", *mcp_servers]

    return BotSpec(
        name=name.strip(),
        directory=directory,
        enabled=bool(data.get("enabled", True)),
        dry_run=bool(data.get("dry_run", False)),
        archetype=archetype,
        description=str(data.get("description", "")),
        trigger=dict(trigger),
        ignore_self=bool(data.get("ignore_self", True)),
        rate_limit_per_min=int(data.get("rate_limit_per_min", 0) or 0),
        model=runtime.get("model"),
        timeout_s=int(runtime.get("timeout_s", 180)),
        grounding_dirs=_as_list(runtime.get("grounding_dirs"), f"{name}: runtime.grounding_dirs"),
        memory=memory,
        mcp_servers=mcp_servers,
        slack_tools=slack_tools,
        extra_tools=_as_list(caps.get("extra_tools"), f"{name}: capabilities.extra_tools")
        or ["Read", "Grep", "Glob", "Skill"],
        raw=data,
    )


def load_spec(directory: Path) -> BotSpec:
    """Load + validate ``<directory>/bot.yml``."""
    bot_yml = directory / "bot.yml"
    if not bot_yml.exists():
        raise SpecError(f"{directory}: no bot.yml")
    try:
        data = yaml.safe_load(bot_yml.read_text()) or {}
    except yaml.YAMLError as e:
        raise SpecError(f"{directory.name}/bot.yml: YAML parse error: {e}") from e
    return parse_spec(directory, data)


def load_specs(bots_dir: Path) -> tuple[list[BotSpec], list[str]]:
    """Load every pet under ``bots_dir``. Returns (specs, errors).

    A malformed spec is collected as an error string rather than aborting the
    whole load — one bad pet shouldn't blind the supervisor to the others.
    """
    specs: list[BotSpec] = []
    errors: list[str] = []
    if not bots_dir.exists():
        return specs, errors
    for child in sorted(bots_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if not (child / "bot.yml").exists():
            continue
        try:
            specs.append(load_spec(child))
        except SpecError as e:
            errors.append(str(e))
    return specs, errors
