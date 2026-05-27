"""Action runners for the watcher daemon.

Each runner takes a fully-rendered action dict + context and executes it.
Errors are logged but do NOT crash the daemon — one bad rule shouldn't take
down the listener.
"""

from __future__ import annotations

import json
import logging
import subprocess
import urllib.error
import urllib.request
from typing import Any

from .rules import render_template

log = logging.getLogger("slack-watcher")


def run_actions(actions: list[dict[str, Any]], ctx: dict[str, Any]) -> list[dict[str, Any]]:
    """Run actions in order; return per-action result records."""
    results = []
    for raw in actions:
        a = render_template(raw, ctx)
        kind = a.get("kind")
        try:
            if kind == "shell":
                results.append(_shell(a))
            elif kind == "webhook":
                results.append(_webhook(a))
            elif kind == "post_back":
                results.append(_post_back(a))
            elif kind == "slack_call":
                results.append(_slack_call(a))
            elif kind == "log":
                msg = a.get("message", "(no message)")
                log.info("rule-log: %s", msg)
                results.append({"kind": "log", "ok": True, "message": msg})
            else:
                results.append({"kind": kind, "ok": False, "error": "unknown action kind"})
        except Exception as e:
            log.exception("action %r failed: %s", kind, e)
            results.append({"kind": kind, "ok": False, "error": f"{type(e).__name__}: {e}"})
    return results


def _shell(action: dict[str, Any]) -> dict[str, Any]:
    cmd = action.get("cmd")
    if not isinstance(cmd, list) or not cmd:
        return {"kind": "shell", "ok": False, "error": "cmd must be a non-empty list"}
    timeout = int(action.get("timeout_s", 30))
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return {
        "kind": "shell",
        "ok": r.returncode == 0,
        "rc": r.returncode,
        "stdout": r.stdout.strip()[:500],
        "stderr": r.stderr.strip()[:500],
    }


def _webhook(action: dict[str, Any]) -> dict[str, Any]:
    url = action.get("url")
    if not url:
        return {"kind": "webhook", "ok": False, "error": "missing url"}
    body = action.get("body")
    headers = {"Content-Type": "application/json"}
    if isinstance(body, dict):
        data = json.dumps(body).encode()
    elif isinstance(body, str):
        data = body.encode()
    else:
        data = b""
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {"kind": "webhook", "ok": True, "status": resp.status}
    except urllib.error.HTTPError as e:
        return {"kind": "webhook", "ok": False, "status": e.code}
    except urllib.error.URLError as e:
        return {"kind": "webhook", "ok": False, "error": str(e.reason)}


def _post_back(action: dict[str, Any]) -> dict[str, Any]:
    """Post a Slack message back via the bridge's client."""
    from ..client import call

    channel = action.get("channel")
    if not channel:
        return {"kind": "post_back", "ok": False, "error": "missing channel"}
    kw: dict[str, Any] = {"channel": channel, "text": action.get("text", "")}
    if action.get("thread_ts"):
        kw["thread_ts"] = action["thread_ts"]
    r = call("chat.postMessage", **kw)
    return {"kind": "post_back", "ok": True, "ts": r.get("ts")}


def _slack_call(action: dict[str, Any]) -> dict[str, Any]:
    """Call any bridge MCP tool by name. Useful for composing watcher rules
    with our existing tool surface (slack_summarize_thread, slack_assistant_ask,
    slack_archive_extract_chunks, etc.)."""
    from ..tools import dispatch

    tool = action.get("tool")
    args = action.get("args") or {}
    if not tool:
        return {"kind": "slack_call", "ok": False, "error": "missing tool"}
    result = dispatch(tool, args) or {}
    return {
        "kind": "slack_call",
        "ok": bool(result.get("ok", False)),
        "tool": tool,
        "result_summary": str(result)[:300],
    }
