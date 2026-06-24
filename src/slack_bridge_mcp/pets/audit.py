"""Audit + reversibility for pet Slack mutations.

Every mutation a pet performs (post / edit / delete / react) is appended to the
pet's ``audit/actions.jsonl`` as one JSON line. For edits and deletes the
*original* content is snapshotted first, which is what makes ``undo`` possible.
Under dry-run, the mutation is recorded as ``would_have`` and never executed.

The audit file is the transparency surface: ``slack_pet_logs`` tails it, and
``slack_pet_undo`` replays it backwards.
"""

from __future__ import annotations

import datetime
import json
import time
from pathlib import Path
from typing import Any

from ..client import call

# Slack tools whose original content we can restore.
REVERSIBLE = {"slack_update_message", "slack_delete_message", "slack_post_message", "slack_post_dm"}


def _now() -> tuple[str, float]:
    t = time.time()
    return datetime.datetime.fromtimestamp(t, tz=datetime.UTC).isoformat(), t


def fetch_message_text(channel_id: str, ts: str) -> str | None:
    """Best-effort fetch of a message's current text (for snapshot before edit/delete)."""
    try:
        data = call(
            "conversations.history",
            channel=channel_id,
            latest=ts,
            oldest=ts,
            inclusive="true",
            limit=1,
        )
        for m in data.get("messages") or []:
            if m.get("ts") == ts:
                return m.get("text")
    except Exception:
        return None
    return None


def append(audit_path: Path, record: dict[str, Any]) -> None:
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    with audit_path.open("a") as fh:
        fh.write(json.dumps(record, default=str) + "\n")


def read_all(audit_path: Path) -> list[dict[str, Any]]:
    if not audit_path.exists():
        return []
    out: list[dict[str, Any]] = []
    for line in audit_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def build_record(
    pet: str,
    tool: str,
    args: dict[str, Any],
    channel_id: str | None,
    original_text: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    iso, epoch = _now()
    return {
        "time": iso,
        "epoch": epoch,
        "pet": pet,
        "tool": tool,
        "channel_id": channel_id,
        "target_ts": args.get("ts"),
        "original_text": original_text,
        "new_text": args.get("text"),
        "dry_run": dry_run,
        "reversible": tool in REVERSIBLE,
        "undone": False,
    }


def undo(audit_path: Path, target_ts: str) -> dict[str, Any]:
    """Reverse the most recent reversible, not-yet-undone mutation on ``target_ts``.

    - update_message -> restore original_text via chat.update
    - delete_message -> repost original_text (new ts; Slack can't resurrect a ts)
    - post_message/post_dm -> delete the message we posted (result_ts/target_ts)
    """
    records = read_all(audit_path)
    candidate: dict[str, Any] | None = None
    for rec in reversed(records):
        if rec.get("undone") or not rec.get("reversible") or rec.get("dry_run"):
            continue
        match_ts = rec.get("result_ts") or rec.get("target_ts")
        if match_ts == target_ts:
            candidate = rec
            break
    if not candidate:
        return {"ok": False, "error": f"no reversible mutation found for ts={target_ts}"}

    cid = candidate.get("channel_id")
    tool = candidate.get("tool")
    try:
        if tool == "slack_update_message":
            call(
                "chat.update", channel=cid, ts=target_ts, text=candidate.get("original_text") or ""
            )
            action = "restored original text"
        elif tool == "slack_delete_message":
            r = call("chat.postMessage", channel=cid, text=candidate.get("original_text") or "")
            action = f"reposted deleted text as {r.get('ts')}"
        elif tool in ("slack_post_message", "slack_post_dm"):
            call("chat.delete", channel=cid, ts=target_ts)
            action = "deleted the posted message"
        else:
            return {"ok": False, "error": f"don't know how to undo {tool}"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    iso, epoch = _now()
    append(
        audit_path,
        {
            "time": iso,
            "epoch": epoch,
            "pet": candidate.get("pet"),
            "tool": "undo",
            "channel_id": cid,
            "target_ts": target_ts,
            "of_tool": tool,
            "action": action,
        },
    )
    return {"ok": True, "undone_tool": tool, "channel_id": cid, "ts": target_ts, "action": action}
