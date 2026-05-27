"""Nightly compaction — moves messages older than HOT_HORIZON_DAYS from
SQLite into append-only Parquet shards in the cold tier.

Atomicity contract:
  1. Read live rows (superseded_by IS NULL) older than cutoff, grouped by
     (year_month, channel_id). Tombstoned rows (deleted_at IS NOT NULL)
     travel to cold too — that's how we preserve the "this message used to
     exist and was deleted" fact for history searches.
  2. For each group: write a NEW shard file (never rewrites old shards).
  3. After all writes succeed: DELETE the moved rows from SQLite.
  4. VACUUM SQLite.

If we crash between (2) and (3): re-running is safe — the new shard contains
data also still in hot, but `merge_shards` dedupes by msg_id.

Run via launchd nightly. Invoke directly:
    python -m slack_bridge_mcp.archive.compact [--horizon-days 90] [--dry-run]
"""

from __future__ import annotations

import argparse
import datetime
import logging
import sys
import time
from collections import defaultdict
from typing import Any

from ..config import settings
from . import cold, db

HOT_HORIZON_DAYS = 90
LOG_PATH = settings().archive_compact_log_path

log = logging.getLogger("slack-archive-compact")


def _setup_logging() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(str(LOG_PATH)), logging.StreamHandler(sys.stderr)],
    )


def _ts_to_year_month(ts: str) -> str | None:
    try:
        unix = float(ts)
    except (ValueError, TypeError):
        return None
    return datetime.datetime.fromtimestamp(unix, tz=datetime.UTC).strftime("%Y-%m")


def _row_to_dict(r: Any) -> dict[str, Any]:
    return {
        "msg_id": r["msg_id"],
        "channel_id": r["channel_id"],
        "ts": r["ts"],
        "edit_seq": r["edit_seq"],
        "user": r["user"],
        "user_label": r["user_label"],
        "text": r["text"],
        "thread_ts": r["thread_ts"],
        "subtype": r["subtype"],
        "raw_json": r["raw_json"],
        "recorded_at": r["recorded_at"],
        "deleted_at": r["deleted_at"],
    }


def compact(horizon_days: int = HOT_HORIZON_DAYS, dry_run: bool = False) -> dict[str, Any]:
    """Move messages with ts < (now - horizon_days) into the cold tier."""
    cutoff = f"{int(time.time()) - horizon_days * 86400}.000000"
    log.info(
        "compaction starting (horizon=%dd, cutoff=%s, dry_run=%s)", horizon_days, cutoff, dry_run
    )

    conn = db.open_db()
    db.init_schema(conn)
    pre = db.stats(conn)

    # Move all rows older than cutoff EXCEPT superseded edit-history (we keep
    # only the latest edit version in cold). Tombstoned rows are kept (their
    # raw_json still records the original content for audit).
    rows = conn.execute(
        """SELECT msg_id, channel_id, ts, edit_seq, user, user_label, text, thread_ts,
                  subtype, raw_json, recorded_at, deleted_at
           FROM messages
           WHERE ts < ? AND superseded_by IS NULL
           ORDER BY channel_id, ts""",
        (cutoff,),
    ).fetchall()
    if not rows:
        log.info("nothing to compact (no messages older than %dd)", horizon_days)
        return {"ok": True, "dry_run": dry_run, "moved": 0, "groups": 0, **pre}

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    skipped_unparseable = 0
    for r in rows:
        ym = _ts_to_year_month(r["ts"])
        if not ym:
            skipped_unparseable += 1
            continue
        groups[(ym, r["channel_id"])].append(_row_to_dict(r))

    log.info(
        "collected %d rows in %d (year_month × channel) groups",
        sum(len(v) for v in groups.values()),
        len(groups),
    )

    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "would_move": sum(len(v) for v in groups.values()),
            "groups": len(groups),
            "skipped_unparseable": skipped_unparseable,
        }

    # Phase 1: write new shards (append-only; never touches old shards)
    written: list[tuple[str, str, int]] = []
    for (ym, cid), group_rows in sorted(groups.items()):
        _path, n = cold.append_shard(ym, cid, group_rows)
        written.append((ym, cid, n))
        log.info("wrote shard %s/%s/+%d rows", ym, cid, n)

    # Phase 2: delete moved rows from SQLite. ONE TRANSACTION PER (year_month,
    # channel) GROUP — not one mega-tx for everything. Each row delete fires
    # the FTS5 messages_ad trigger; bundling 50k rows into a single tx made
    # the watcher + sweeper + MCP all wait on the lock for >14 minutes. Smaller
    # txns release the lock between groups so concurrent writers can interleave.
    deleted = 0
    for (_ym, cid), group_rows in groups.items():
        ts_list = [r["ts"] for r in group_rows]
        placeholders = ",".join("?" * len(ts_list))
        conn.execute("BEGIN")
        try:
            cur = conn.execute(
                f"DELETE FROM messages WHERE channel_id=? AND ts IN ({placeholders})",
                [cid, *ts_list],
            )
            deleted += cur.rowcount
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    log.info("deleted %d rows from hot SQLite (across %d txns)", deleted, len(groups))

    # Phase 3: WAL checkpoint instead of VACUUM. Full VACUUM blocks all
    # writers for the duration of the file rebuild (~minutes for 100+ MB).
    # PRAGMA wal_checkpoint(TRUNCATE) reclaims WAL pages without holding an
    # exclusive lock long. Run a real VACUUM monthly via launchd if needed.
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("ANALYZE")
    log.info("WAL checkpointed + analyzed")

    post = db.stats(conn)
    cold_stats = cold.stats()
    summary = {
        "ok": True,
        "horizon_days": horizon_days,
        "moved": deleted,
        "groups": len(groups),
        "skipped_unparseable": skipped_unparseable,
        "hot_before": pre,
        "hot_after": post,
        "cold": cold_stats,
    }
    log.info(
        "compaction summary: moved=%d, hot_size=%d→%d, cold_total=%d bytes",
        deleted,
        pre["db_size_bytes"],
        post["db_size_bytes"],
        cold_stats["total_size_bytes"],
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Slack archive compaction")
    parser.add_argument(
        "--horizon-days",
        type=int,
        default=HOT_HORIZON_DAYS,
        help="Messages older than this go to cold tier (default 90)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print what would be moved without writing"
    )
    args = parser.parse_args()

    _setup_logging()
    summary = compact(horizon_days=args.horizon_days, dry_run=args.dry_run)
    import json

    print(json.dumps(summary, indent=2, default=str))


if __name__ == "__main__":
    main()
