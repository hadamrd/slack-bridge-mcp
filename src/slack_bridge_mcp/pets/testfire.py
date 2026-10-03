"""Test-fire a pet against a real recent message — no waiting for a live event.

Usage::

    python -m slack_bridge_mcp.pets.testfire <pet-name> [--ts TS] [--channel CID]
                                             [--limit N] [--match-only] [--force]

It fetches recent history from the pet's trigger channel (raw, *with* attachments),
finds the most recent message that MATCHES the pet's rule (or the one at ``--ts``),
builds the exact enriched event + context the live daemon would, and runs the pet
once through the real runner. This is the deterministic way to exercise a pet
end-to-end while debugging.

Safety: refuses to fire a pet that is NOT in ``dry_run`` unless ``--force`` is
given — so a test can never silently take live action.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ..client import call
from ..watcher.daemon import _enrich_event
from ..watcher.rules import _match_rule, build_context
from .registry import bots_dir, compile_rule
from .runner import run as run_pet
from .spec import load_spec


def _event(msg: dict[str, Any], channel: str) -> dict[str, Any]:
    ev = dict(msg)
    ev["channel"] = channel
    ev.setdefault("type", "message")
    return _enrich_event(ev)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="testfire")
    ap.add_argument("pet", help="pet directory name under the bots dir")
    ap.add_argument("--ts", help="fire on this specific message ts (else newest match)")
    ap.add_argument("--channel", help="override channel id (default: pet trigger channel_id)")
    ap.add_argument("--limit", type=int, default=30, help="how many recent messages to scan")
    ap.add_argument("--match-only", action="store_true", help="report the match; do not fire")
    ap.add_argument("--force", action="store_true", help="fire even if the pet is not dry_run")
    args = ap.parse_args(argv)

    spec = load_spec(bots_dir() / args.pet)
    rule = compile_rule(spec)
    channel = args.channel or spec.trigger.get("channel_id")
    if not channel:
        print("no channel: pass --channel or set trigger.channel_id in bot.yml", file=sys.stderr)
        return 2

    msgs = call("conversations.history", channel=channel, limit=args.limit).get("messages", [])
    chosen: dict[str, Any] | None = None
    ev: dict[str, Any] = {}
    if args.ts:
        chosen = next((m for m in msgs if m.get("ts") == args.ts), None)
        if chosen is None:
            print(f"ts {args.ts} not in last {args.limit} messages", file=sys.stderr)
            return 2
        ev = _event(chosen, channel)
    else:
        for m in msgs:  # newest first
            cand = _event(m, channel)
            if _match_rule(rule, cand):
                chosen, ev = m, cand
                break
        if chosen is None:
            print(f"no message in last {args.limit} matched rule {rule['match']}", file=sys.stderr)
            return 1

    text = ev.get("text") or ""
    head = text.splitlines()[0][:100] if text else "(empty)"
    print(f"matched ts={chosen.get('ts')} | {head}")
    if args.match_only:
        print("--match-only: not firing.")
        return 0

    if not spec.dry_run and not args.force:
        print("REFUSING: pet is not dry_run. Re-run with --force to fire live.", file=sys.stderr)
        return 3

    print(
        f"firing pet '{spec.name}' (dry_run={spec.dry_run}); this runs claude -p, may take a bit..."
    )
    result = run_pet(spec, build_context(ev))
    print(json.dumps(result, indent=2, default=str))
    print(f"audit: {spec.audit_path}")
    print(f"log:   {spec.log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
