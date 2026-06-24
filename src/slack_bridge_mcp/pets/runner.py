"""Invoke a pet: build its MCP config + allowed tools + prompt, run ``claude -p``.

Each invocation is an isolated subprocess with ``cwd`` set to the pet directory,
so Claude Code auto-loads the pet's ``CLAUDE.md`` persona and ``.claude/skills/``
behavior tree. The pet decides what to do (via its skills) and acts through its
granted tools — it posts/edits Slack *itself* (the model never returns text for
the shell to forward; forwarding model text was an early footgun).
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from .spec import BotSpec

log = logging.getLogger("slack-watcher")

CLAUDE_BIN = os.environ.get("SLACK_BRIDGE_CLAUDE_BIN", "claude")
_CLAUDE_JSON = Path.home() / ".claude.json"

PROMPT_TEMPLATE = """A new Slack event arrived that you, {name}, are configured to handle.

Event:
- channel: {channel_id} {channel_name}
- from: {user} {user_label}
- ts: {ts}
- thread_ts: {thread_ts}
- permalink: {permalink}
- text:
<<<
{text}
>>>

Follow your CLAUDE.md persona and your skills in `.claude/skills/` to handle this.
Your skills are a decision tree: classify the situation, route to the right skill,
and take exactly the action it prescribes — which may legitimately be to do nothing.
Read your `memory/` for context before acting{memory_note}.
Act through your tools (post/edit/react/etc.); do NOT return an explanation as your
final answer — the work is the tool calls you make, not the text you print."""


def _ensure_dirs(spec: BotSpec) -> None:
    for p in (spec.directory / "audit", spec.directory / "logs", spec.directory / ".runtime"):
        p.mkdir(parents=True, exist_ok=True)
    if spec.can_write_memory:
        spec.memory_dir.mkdir(parents=True, exist_ok=True)


def _build_mcp_config(spec: BotSpec) -> tuple[Path, list[str]]:
    """Write a per-pet --mcp-config containing only the granted servers.

    Returns (path, missing_servers).
    """
    all_servers: dict[str, Any] = {}
    if _CLAUDE_JSON.exists():
        try:
            all_servers = (json.loads(_CLAUDE_JSON.read_text()) or {}).get("mcpServers", {}) or {}
        except json.JSONDecodeError:
            all_servers = {}
    chosen: dict[str, Any] = {}
    missing: list[str] = []
    for srv in spec.mcp_servers:
        if srv in all_servers:
            chosen[srv] = all_servers[srv]
        else:
            missing.append(srv)
    cfg_path = spec.directory / ".runtime" / "mcp.json"
    cfg_path.write_text(json.dumps({"mcpServers": chosen}, indent=2))
    return cfg_path, missing


def _build_prompt(spec: BotSpec, ctx: dict[str, Any]) -> str:
    cn = ctx.get("channel_name")
    ul = ctx.get("user_label")
    memory_note = (
        " and journal anything worth remembering for next time" if spec.can_write_memory else ""
    )
    return PROMPT_TEMPLATE.format(
        name=spec.name,
        channel_id=ctx.get("channel_id", ""),
        channel_name=f"({cn})" if cn else "",
        user=ctx.get("user", ""),
        user_label=f"({ul})" if ul else "",
        ts=ctx.get("ts", ""),
        thread_ts=ctx.get("thread_ts") or "(top-level)",
        permalink=ctx.get("permalink", ""),
        text=ctx.get("text", ""),
        memory_note=memory_note,
    )


def _log_line(path: Path, msg: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    iso = datetime.datetime.fromtimestamp(time.time(), tz=datetime.UTC).isoformat()
    with path.open("a") as fh:
        fh.write(f"[{iso}] {msg}\n")


def run(spec: BotSpec, ctx: dict[str, Any]) -> dict[str, Any]:
    """Run one pet invocation for a matched event. Synchronous (called in a worker thread)."""
    _ensure_dirs(spec)
    cfg_path, missing = _build_mcp_config(spec)
    if missing:
        log.warning("pet %s: MCP servers not in ~/.claude.json, skipped: %s", spec.name, missing)

    prompt = _build_prompt(spec, ctx)
    cmd = [
        CLAUDE_BIN,
        "-p",
        prompt,
        "--mcp-config",
        str(cfg_path),
        "--strict-mcp-config",
        "--permission-mode",
        "bypassPermissions",
    ]
    allowed = spec.allowed_tools()
    if allowed:
        cmd += ["--allowedTools", ",".join(allowed)]
    if spec.model:
        cmd += ["--model", spec.model]
    for g in spec.grounding_dirs:
        cmd += ["--add-dir", g]

    env = dict(os.environ)
    env["SLACK_BRIDGE_PET_NAME"] = spec.name
    env["SLACK_BRIDGE_PET_AUDIT_DIR"] = str(spec.directory / "audit")
    env["SLACK_BRIDGE_PET_DRYRUN"] = "1" if spec.dry_run else "0"

    fire_iso = datetime.datetime.fromtimestamp(time.time(), tz=datetime.UTC).isoformat()
    _log_line(
        spec.log_path,
        f"=== FIRE event_ts={ctx.get('ts')} channel={ctx.get('channel_id')} "
        f"dry_run={spec.dry_run} ===",
    )
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(spec.directory),
            env=env,
            capture_output=True,
            text=True,
            timeout=spec.timeout_s,
        )
    except subprocess.TimeoutExpired:
        _log_line(spec.log_path, f"TIMEOUT after {spec.timeout_s}s")
        return {"ok": False, "pet": spec.name, "error": "timeout"}
    except FileNotFoundError:
        _log_line(spec.log_path, f"claude binary not found: {CLAUDE_BIN}")
        return {"ok": False, "pet": spec.name, "error": f"claude binary not found: {CLAUDE_BIN}"}

    dur = round(time.time() - started, 1)
    out = (proc.stdout or "").strip()
    err = (proc.stderr or "").strip()
    if out:
        _log_line(spec.log_path, f"stdout: {out[:2000]}")
    if err:
        _log_line(spec.log_path, f"stderr: {err[:1000]}")
    _log_line(spec.log_path, f"DONE rc={proc.returncode} in {dur}s")
    return {
        "ok": proc.returncode == 0,
        "pet": spec.name,
        "rc": proc.returncode,
        "duration_s": dur,
        "fired_at": fire_iso,
    }
