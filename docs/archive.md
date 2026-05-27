# Local Slack Archive

The optional archive daemon stores readable Slack messages in a local SQLite
database and moves older rows to compressed Parquet shards.

## Hot Storage

`SLACK_BRIDGE_ARCHIVE_DB_PATH` points to the SQLite database. The schema keeps:

- channel checkpoints for resumable polling
- message text and raw JSON
- edit versions and soft-delete markers
- FTS5 search over message text

Run the poller:

```bash
SLACK_BRIDGE_ENV_FILE=.env.local python -m slack_bridge_mcp.archive
```

## Cold Storage

`SLACK_BRIDGE_COLD_ARCHIVE_DIR` points to Hive-style Parquet partitions:

```text
year_month=2026-05/
  channel_id=C0123456789/
    shard-00000001.parquet
```

Run compaction:

```bash
SLACK_BRIDGE_ENV_FILE=.env.local python -m slack_bridge_mcp.archive.compact
```

## Useful MCP Tools

- `slack_archive_status`
- `slack_archive_search`
- `slack_archive_thread`
- `slack_archive_backfill`
- `slack_archive_compact`

Archive files can contain private Slack messages. Keep them outside Git.
