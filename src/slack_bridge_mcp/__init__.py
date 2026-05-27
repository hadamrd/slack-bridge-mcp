"""Slack-bridge MCP — pure-HTTP, native Slack client for Claude Code.

Architecture overview:

    server.py / __main__   MCP wiring (≤100 lines)
    client.py              HTTP client + 5-cookie auth + rate limiter
    browser.py             Playwright fallback for SSO + WS-event capture
    caches.py              Persistent users + bots cache (disk-backed)
    ratelimit.py           Token-bucket per method-class
    archive/               Local Slack message archive (hot + cold tiers)
        daemon.py          Polling sweeper (backfill role since v2)
        compact.py         Hot SQLite -> cold Parquet+zstd compaction
        db.py              SQLite schema (msg_id PK, FTS5 unicode61, soft delete)
        cold.py            Append-shard Parquet writer + DuckDB reader
        ingest.py          Shared event-handling for WS + polling paths
        migrate.py         One-shot v1 -> v2 schema migration
    watcher/               Long-lived WS subscriber + rule engine
        daemon.py          asyncio WS loop + reconnect
        rules.py           YAML rule loader + matcher
        actions.py         shell / webhook / post_back / slack_call runners
    tools/                 MCP tool definitions, one module per topic
"""
