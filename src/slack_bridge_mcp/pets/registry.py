"""Discover pets, compile their triggers into rules, and report status.

The supervisor merges ``compile_rules()`` output into the same RulesEngine the
legacy watcher uses, so a pet is just a rule whose action is ``{kind: pet}``.
Pet specs are reloaded fresh at fire time (so ``dry_run`` / capability edits
take effect immediately); the daemon recompiles the *rule set* only when a
``bot.yml`` changes (so enable/disable/trigger edits take effect).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import settings
from .spec import BotSpec, SpecError, load_spec, load_specs


def bots_dir() -> Path:
    return settings().bots_dir


def compile_rule(spec: BotSpec) -> dict[str, Any]:
    """Turn a pet spec into a RulesEngine rule dict."""
    return {
        "name": f"pet:{spec.name}",
        "enabled": spec.enabled,
        "match": dict(spec.trigger),
        "ignore_self": spec.ignore_self,
        "rate_limit_per_min": spec.rate_limit_per_min,
        "stop_on_match": False,
        "actions": [{"kind": "pet", "name": spec.name}],
    }


def compile_rules(specs: list[BotSpec]) -> list[dict[str, Any]]:
    return [compile_rule(s) for s in specs if s.enabled]


def load_one(name: str) -> BotSpec | None:
    """Fresh-load a single pet by directory name (used at fire time)."""
    d = bots_dir() / name
    if not (d / "bot.yml").exists():
        return None
    try:
        return load_spec(d)
    except SpecError:
        return None


def signature() -> float:
    """Max mtime across all bot.yml files — cheap change detector for the daemon."""
    d = bots_dir()
    if not d.exists():
        return 0.0
    mtimes = [p.stat().st_mtime for p in d.glob("*/bot.yml")]
    return max(mtimes) if mtimes else 0.0


def _fire_stats(spec: BotSpec) -> dict[str, Any]:
    """Parse the pet log for fire markers → count + last-fired timestamp."""
    log_path = spec.log_path
    if not log_path.exists():
        return {"fire_count": 0, "last_fired": None}
    count = 0
    last = None
    for line in log_path.read_text().splitlines():
        if "=== FIRE" in line:
            count += 1
            # line format: "[<iso>] === FIRE ..."
            if line.startswith("[") and "]" in line:
                last = line[1 : line.index("]")]
    return {"fire_count": count, "last_fired": last}


def set_field(name: str, key: str, value: Any) -> dict[str, Any]:
    """Patch a single top-level field in a pet's bot.yml (enabled / dry_run)."""
    import yaml

    d = bots_dir() / name
    bot_yml = d / "bot.yml"
    if not bot_yml.exists():
        return {"ok": False, "error": f"no such pet: {name}"}
    data = yaml.safe_load(bot_yml.read_text()) or {}
    data[key] = value
    bot_yml.write_text(yaml.safe_dump(data, sort_keys=False))
    return {"ok": True, "pet": name, key: value}


def status() -> dict[str, Any]:
    """Full status snapshot for slack_pet_list / slack_pet_status."""
    specs, errors = load_specs(bots_dir())
    pets_out = []
    for s in specs:
        pets_out.append(
            {
                "name": s.name,
                "enabled": s.enabled,
                "dry_run": s.dry_run,
                "archetype": s.archetype,
                "description": s.description,
                "trigger": s.trigger,
                "memory": s.memory,
                "mcp_servers": s.mcp_servers,
                "slack_tools": s.slack_tools,
                "mutating_tools": s.mutating_slack_tools(),
                "model": s.model,
                "directory": str(s.directory),
                **_fire_stats(s),
            }
        )
    return {
        "ok": True,
        "bots_dir": str(bots_dir()),
        "count": len(pets_out),
        "enabled_count": sum(1 for p in pets_out if p["enabled"]),
        "pets": pets_out,
        "errors": errors,
    }


def one_status(name: str) -> dict[str, Any]:
    spec = load_one(name)
    if not spec:
        return {"ok": False, "error": f"no such pet: {name}"}
    return {
        "ok": True,
        "name": spec.name,
        "enabled": spec.enabled,
        "dry_run": spec.dry_run,
        "archetype": spec.archetype,
        "trigger": spec.trigger,
        "memory": spec.memory,
        "mcp_servers": spec.mcp_servers,
        "slack_tools": spec.slack_tools,
        "allowed_tools": spec.allowed_tools(),
        "directory": str(spec.directory),
        **_fire_stats(spec),
    }
