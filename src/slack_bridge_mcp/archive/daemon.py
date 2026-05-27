"""Slack archive backfill sweeper.

Role since v2: this is the **catch-up** path, not the primary ingest. The
WS watcher is the primary writer; this sweeper exists only to:
  - bootstrap a fresh archive (no WS history)
  - close gaps when the watcher was offline (sleep, reboot, auth expiry)
  - re-discover newly-joined channels
  - re-resolve channel names that have changed

Cadence: SWEEP_INTERVAL_S=300 (5 min). API quota cost is ~1 call per
channel per 5 min when there's nothing new (cheap thanks to the `oldest`
checkpoint). When the WS watcher is up, this sweep is mostly a no-op.

Run via launchd. Invoke directly:
    .venv/bin/python -m slack_bridge_mcp.archive
"""

from __future__ import annotations

import logging
import signal
import sys
import time
from typing import Any

from ..client import SlackError, call
from ..config import settings
from . import db, ingest

SWEEP_INTERVAL_S = 300
ENUMERATE_EVERY_N_CYCLES = 4  # client.counts re-discovery every ~20 min
MAX_BACKOFF_S = 600
MAX_PAGES_PER_CHANNEL = 5
NAME_REFRESH_S = 24 * 3600  # re-resolve channel names this often
LOG_PATH = settings().archive_log_path

log = logging.getLogger("slack-archive")
_running = True


def _setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(str(LOG_PATH)), logging.StreamHandler(sys.stderr)],
    )


def _stop(signum: int, _frame: Any) -> None:
    global _running
    _running = False
    log.info("received signal %d, stopping after current cycle", signum)


def _resolve_channel_name(channel_id: str) -> str | None:
    try:
        data = call("search.messages", query=f"in:{channel_id}", count=1)
    except SlackError:
        return None
    matches = (data.get("messages") or {}).get("matches") or []
    for m in matches:
        ch = m.get("channel") or {}
        if ch.get("id") == channel_id and ch.get("name"):
            return ch["name"]
    return None


def _enumerate_channels(conn) -> list[tuple[str, bool]]:
    counts = call("client.counts")
    out: list[tuple[str, bool]] = []
    now = int(time.time())
    rows = {
        r["id"]: (r["name"], r["name_resolved_at"] or 0)
        for r in conn.execute("SELECT id, name, name_resolved_at FROM channels")
    }
    for c in counts.get("channels") or []:
        cid = c["id"]
        db.ensure_channel(conn, cid, None, is_im=False)
        name, resolved_at = rows.get(cid, (None, 0))
        if not name or (now - resolved_at) > NAME_REFRESH_S:
            new_name = _resolve_channel_name(cid)
            if new_name and new_name != name:
                conn.execute(
                    "UPDATE channels SET name=?, name_resolved_at=? WHERE id=?",
                    (new_name, now, cid),
                )
                log.info("channel name: %s → #%s", cid, new_name)
        out.append((cid, False))
    for d in counts.get("ims") or []:
        db.ensure_channel(conn, d["id"], None, is_im=True)
        out.append((d["id"], True))
    conn.commit()
    return out


def _sweep_channel(conn, channel_id: str) -> int:
    """Catch up new messages since the channel checkpoint. Returns count."""
    last_ts = db.get_channel_last_ts(conn, channel_id) or "0"
    new = 0
    pages = 0
    cursor = ""
    max_seen_ts = last_ts
    while pages < MAX_PAGES_PER_CHANNEL:
        params = {"channel": channel_id, "oldest": last_ts, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        try:
            data = call("conversations.history", **params)
        except SlackError as e:
            if "invalid_auth" in str(e):
                log.warning("invalid_auth on %s — skipping cycle", channel_id)
                raise
            log.warning("history failed for %s: %s", channel_id, e)
            break
        msgs = data.get("messages") or []
        for m in msgs:
            # Synthesize the WS-style envelope so ingest.ingest_event works
            # uniformly for HTTP-fetched rows too.
            event = dict(m)
            event["channel"] = channel_id
            if ingest.ingest_event(conn, event, via="poll") == "inserted":
                new += 1
            if m.get("ts", "") > max_seen_ts:
                max_seen_ts = m["ts"]
        if not data.get("has_more"):
            break
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
        pages += 1
    db.update_channel_checkpoint(conn, channel_id, max_seen_ts if max_seen_ts != last_ts else None)
    return new


def main() -> None:
    _setup_logging()
    log.info("slack-archive sweeper starting (interval=%ds)", SWEEP_INTERVAL_S)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    conn = db.open_db()
    db.init_schema(conn)

    channels: list[tuple[str, bool]] = []
    cycles = 0
    backoff = 0

    while _running:
        try:
            if cycles % ENUMERATE_EVERY_N_CYCLES == 0:
                channels = _enumerate_channels(conn)
                log.info("enumerated %d channels/DMs", len(channels))

            cycle_new = 0
            failed_auth = False
            for cid, _is_im in channels:
                if not _running:
                    break  # type: ignore[unreachable]
                try:
                    cycle_new += _sweep_channel(conn, cid)
                except SlackError as e:
                    if "invalid_auth" in str(e):
                        failed_auth = True
                        break
                    log.warning("channel %s failed: %s", cid, e)

            if failed_auth:
                log.warning("auth expired — sleeping 5min before retry")
                _sleep(300)
                continue

            log.info("cycle %d: +%d messages from sweep", cycles, cycle_new)
            backoff = 0
            cycles += 1
        except Exception as e:
            log.exception("cycle error: %s", e)
            backoff = min(MAX_BACKOFF_S, max(60, backoff * 2 or 60))

        _sleep(backoff or SWEEP_INTERVAL_S)

    log.info("stopped")


def _sleep(seconds: int) -> None:
    deadline = time.time() + seconds
    while _running and time.time() < deadline:
        time.sleep(min(1.0, deadline - time.time()))


if __name__ == "__main__":
    main()
