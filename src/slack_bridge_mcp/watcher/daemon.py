"""Slack watcher daemon — long-lived WebSocket subscriber + rule engine.

Architecture:
- Asyncio event loop owns the WS connection.
- On `message` events: enrich with channel name + user label from local cache,
  match against rules, dispatch matched actions in a thread (so blocking
  shell/HTTP/slack_call calls don't stall the WS read loop).
- Reconnect on disconnect with exponential backoff.
- Token refresh on `invalid_auth` is a manual intervention (run `slack_refresh_tokens`)
  — same model as the archive daemon.

Run via launchd or directly:
    .venv/bin/python -m slack_bridge_mcp.watcher
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import websockets
import websockets.exceptions

from ..archive import db as archive_db
from ..archive import ingest as archive_ingest
from ..client import SlackError, _build_cookie_header, _read_env, call
from ..config import permalink, settings
from .actions import run_actions
from .rules import RulesEngine, build_context

LOG_PATH = settings().watcher_log_path
RECONNECT_BACKOFF_INITIAL_S = 2
RECONNECT_BACKOFF_MAX_S = 120

log = logging.getLogger("slack-watcher")
_running = True
_actions_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="slack-watcher-actions")
# Single-thread pool for archive writes — serializes SQLite writes without
# blocking the WS event loop. SQLite WAL still allows the polling sweeper
# to write concurrently from its own process.
_archive_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="slack-watcher-archive")
_archive_conn: Any | None = None


def _get_archive_conn() -> Any:
    """Lazy single connection used by the archive worker thread.
    SQLite connections are not safe to share across threads — and we have
    exactly one worker, so this connection is owned by that worker."""
    global _archive_conn
    if _archive_conn is None:
        _archive_conn = archive_db.open_db()
        archive_db.init_schema(_archive_conn)
    return _archive_conn


def _archive_worker(event: dict[str, Any]) -> None:
    """Runs in _archive_pool. Inserts/edits/deletes the archive row for an event."""
    try:
        result = archive_ingest.ingest_event(_get_archive_conn(), event, via="ws")
        if result not in ("noop",):
            log.debug("archive %s: %s/%s", result, event.get("channel"), event.get("ts"))
    except Exception as e:
        log.warning("archive ingest failed: %s", e)


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
    log.info("received signal %d, stopping", signum)


def _build_ws_url() -> str:
    """Get a fresh WS URL from client.getWebSocketURL + tack on the auth params
    we observed in the live web client."""
    env = _read_env()
    xoxc = env["SLACK_MCP_XOXC_TOKEN"]
    info = call("client.getWebSocketURL")
    base = info["primary_websocket_url"].rstrip("/")
    cfg = settings()
    routing = cfg.websocket_gateway_server or info.get("routing_context")

    # Params observed in live Slack web client
    params = {
        "token": xoxc,
        "sync_desync": "1",
        "slack_client": "desktop",
        "start_args": (
            "?agent=client&org_wide_aware=true&agent_version=1778252852"
            "&eac_cache_ts=true&cache_ts=0&name_tagging=true"
            "&only_self_subteams=true&connect_only=true&ms_latest=true"
        ),
        "no_query_on_subscribe": "1",
        "flannel": "3",
        "lazy_channels": "1",
        "batch_presence_aware": "1",
    }
    if routing:
        params["gateway_server"] = routing
    if cfg.enterprise_id:
        params["enterprise_id"] = cfg.enterprise_id
    return f"{base}/?{urllib.parse.urlencode(params)}"


def _enrich_event(event: dict[str, Any]) -> dict[str, Any]:
    """Add channel_name + user_label fields to a raw Slack event using the
    archive's channel cache + the persistent users cache."""
    cid = event.get("channel")
    if cid:
        try:
            from ..archive import db

            conn = db.open_db()
            row = conn.execute("SELECT name FROM channels WHERE id=?", (cid,)).fetchone()
            if row and row["name"]:
                event["_channel_name"] = row["name"]
        except Exception:
            pass
    uid = event.get("user")
    if isinstance(uid, str) and uid.startswith("U"):
        try:
            from .. import caches

            event["_user_label"] = caches.actor_label(uid)
        except Exception:
            pass
    # Build a permalink (best-effort, format same as elsewhere)
    if cid and event.get("ts"):
        event["_permalink"] = permalink(cid, event["ts"])
    return event


def _process_event(rules: RulesEngine, event: dict[str, Any]) -> None:
    """Match + dispatch a single event. Synchronous; called from a thread."""
    matched = rules.match(event)
    if not matched:
        return
    ctx = build_context(event)
    for rule in matched:
        log.info("rule fired: %r on %s/%s", rule.get("name"), event.get("channel"), event.get("ts"))
        results = run_actions(rule.get("actions") or [], ctx)
        for r in results:
            if not r.get("ok", True):
                log.warning("  action %r failed: %s", r.get("kind"), r.get("error"))


async def _ws_connect_loop(rules: RulesEngine) -> None:
    """Maintain WS connection, dispatch events. Reconnect on drop.

    Slack drops un-authenticated WS connections after ~5s if they look like
    bots. We mirror the official web client's behaviour:
    - Send full Cookie header in the upgrade handshake.
    - User-Agent header matches Chrome.
    - Origin: https://app.slack.com — Slack checks this.
    """
    backoff = RECONNECT_BACKOFF_INITIAL_S
    cycles_since_rules_check = 0

    while _running:
        try:
            url = _build_ws_url()
            env = _read_env()
            cookie_hdr = _build_cookie_header(env)
            extra_headers = {
                "Cookie": cookie_hdr,
                "Origin": "https://app.slack.com",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            }
            log.info("connecting...")
            async with websockets.connect(
                url,
                additional_headers=extra_headers,
                ping_interval=30,
                ping_timeout=20,
                user_agent_header=None,  # we set it manually above
            ) as ws:
                log.info("connected")
                backoff = RECONNECT_BACKOFF_INITIAL_S  # reset

                async for raw in ws:
                    if not _running:  # mutated by signal handler
                        break  # type: ignore[unreachable]
                    cycles_since_rules_check += 1
                    if cycles_since_rules_check >= 50:
                        rules.maybe_reload()
                        cycles_since_rules_check = 0
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    et = event.get("type")
                    if et == "hello":
                        log.info("hello received")
                        continue
                    if et != "message":
                        continue
                    # Archive every message-class event (regular + edits + deletes).
                    # Submit to the single-thread archive worker so the WS loop
                    # never blocks on SQLite.
                    _archive_pool.submit(_archive_worker, dict(event))
                    # Rules: keep the historical filter — fire only on user-visible
                    # new messages, skip edits/hidden.
                    if event.get("subtype") in ("message_changed", "message_deleted"):
                        continue
                    if event.get("hidden"):
                        continue
                    _enrich_event(event)
                    _actions_pool.submit(_process_event, rules, event)
        except SlackError as e:
            if "invalid_auth" in str(e):
                log.error("invalid_auth at WS-URL fetch — token expired; sleeping 5min")
                await asyncio.sleep(300)
                continue
            log.exception("slack error: %s", e)
        except (websockets.exceptions.ConnectionClosed, OSError) as e:
            log.warning("ws closed: %s", e)
        except Exception as e:
            log.exception("unexpected: %s", e)

        if not _running:  # signal-handler mutated
            break  # type: ignore[unreachable]
        backoff = min(RECONNECT_BACKOFF_MAX_S, backoff * 2)
        log.info("reconnecting in %ds...", backoff)
        await asyncio.sleep(backoff)


def main() -> None:
    _setup_logging()
    log.info("slack-watcher starting (logs at %s)", LOG_PATH)
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    rules = RulesEngine()
    rules.maybe_reload()
    log.info("initial rules: %d active", len(rules.rules))

    try:
        asyncio.run(_ws_connect_loop(rules))
    except KeyboardInterrupt:
        pass
    finally:
        _actions_pool.shutdown(wait=False, cancel_futures=True)
        _archive_pool.shutdown(wait=True, cancel_futures=False)
        log.info("stopped")


if __name__ == "__main__":
    main()
