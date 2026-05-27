"""Local Slack archive — tiered storage.

Hot tier:  SLACK_BRIDGE_ARCHIVE_DB_PATH (SQLite WAL + FTS5, last 90 days)
Cold tier: SLACK_BRIDGE_COLD_ARCHIVE_DIR/year_month=YYYY-MM/
                                                channel_id=CXXX/
                                                shard-NNNNNNNN.parquet
"""
