"""One-shot migration from the legacy composite-PK schema to the v2 schema.

What this does
--------------
1. Backs up `slack-archive.db` → `slack-archive.db.pre-v2.bak`.
2. Reads every "live" row (superseded_by IS NULL) from the legacy table.
3. Strips synthetic-ts edit hacks (`<ts>.edit.<recorded_at>` → `<ts>`).
   Edit history is dropped; the latest text wins.
4. Writes a fresh DB at the same path with the v2 schema, inserting rows
   in (channel_id, ts) order so msg_id is roughly time-monotonic.

Run via:
    python -m slack_bridge_mcp.archive.migrate            # apply
    python -m slack_bridge_mcp.archive.migrate --dry-run  # preview only

Idempotent: if the existing DB is already v2, exits with no-op.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import shutil
import sqlite3
import sys
import time
from pathlib import Path

from . import db

log = logging.getLogger("slack-archive-migrate")


def _detect_legacy(path: Path) -> bool:
    """Return True if the DB at `path` uses the legacy composite-PK schema."""
    if not path.exists():
        return False
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()}
        if not cols:
            return False
        return "msg_id" not in cols
    finally:
        conn.close()


def _strip_edit_suffix(ts: str) -> str:
    """Convert legacy synthetic ts `<ts>.edit.<n>` back to the original ts."""
    return ts.split(".edit.")[0] if ".edit." in ts else ts


def migrate(dry_run: bool = False) -> dict:
    db_path = db.DB_PATH

    if not db_path.exists():
        log.info("no archive DB at %s — nothing to migrate", db_path)
        return {"ok": True, "noop": True, "reason": "no archive db"}

    if not _detect_legacy(db_path):
        log.info("DB at %s is already v2 (or fresh) — no migration needed", db_path)
        return {"ok": True, "noop": True, "reason": "already v2"}

    backup = db_path.with_suffix(db_path.suffix + ".pre-v2.bak")
    log.info("legacy schema detected; planning migration → %s", backup)

    # Pull live rows from legacy DB
    legacy = sqlite3.connect(str(db_path))
    legacy.row_factory = sqlite3.Row
    try:
        legacy_count = legacy.execute("SELECT COUNT(*) AS c FROM messages").fetchone()["c"]
        live_rows = legacy.execute(
            """SELECT channel_id, ts, user, user_label, text, thread_ts, subtype,
                      raw_json, recorded_at
               FROM messages
               WHERE superseded_by IS NULL
               ORDER BY channel_id, ts ASC"""
        ).fetchall()
        channels = legacy.execute(
            "SELECT id, name, is_im, last_ts, last_polled_at FROM channels"
        ).fetchall()
    finally:
        legacy.close()

    log.info(
        "legacy DB: total_rows=%d live_rows=%d channels=%d",
        legacy_count,
        len(live_rows),
        len(channels),
    )

    summary = {
        "ok": True,
        "legacy_db_path": str(db_path),
        "backup_path": str(backup),
        "legacy_total_rows": legacy_count,
        "legacy_live_rows": len(live_rows),
        "channels": len(channels),
    }

    if dry_run:
        log.info("DRY RUN — would write %d live rows to v2 schema", len(live_rows))
        # First-row sample for sanity
        if live_rows:
            r = live_rows[0]
            log.info(
                "sample row: channel_id=%s ts=%s user=%s text=%.80s",
                r["channel_id"],
                _strip_edit_suffix(r["ts"]),
                r["user"],
                r["text"] or "",
            )
        summary["dry_run"] = True
        return summary

    # Phase 1: backup
    shutil.copy2(db_path, backup)
    log.info("backed up legacy DB → %s", backup)

    # Phase 2: clean up old DB + sidecars (WAL/SHM), then write fresh v2 DB
    for ext in ("", "-wal", "-shm", "-journal"):
        f = Path(str(db_path) + ext)
        with contextlib.suppress(FileNotFoundError):
            f.unlink()
    new_conn = db.open_db()
    db.init_schema(new_conn)

    # Channels first
    for c in channels:
        new_conn.execute(
            """INSERT OR REPLACE INTO channels(id, name, is_im, last_ts, last_polled_at)
               VALUES (?, ?, ?, ?, ?)""",
            (c["id"], c["name"], c["is_im"], c["last_ts"], c["last_polled_at"]),
        )

    # Rows: dedup by (channel_id, original_ts) — synthetic-ts edit rows collapse
    # onto the live row's text. We already filtered superseded_by IS NULL above,
    # so each (chan, original_ts) has at most one entry, but defensively dedup.
    seen: set[tuple[str, str]] = set()
    inserted = 0
    skipped_dup = 0
    new_conn.execute("BEGIN")
    try:
        for r in live_rows:
            ts = _strip_edit_suffix(r["ts"])
            key = (r["channel_id"], ts)
            if key in seen:
                skipped_dup += 1
                continue
            seen.add(key)
            new_conn.execute(
                """INSERT INTO messages
                   (channel_id, ts, edit_seq, user, user_label, text, thread_ts,
                    subtype, raw_json, recorded_at, ingested_via)
                   VALUES (?, ?, 0, ?, ?, ?, ?, ?, ?, ?, 'migrated')""",
                (
                    r["channel_id"],
                    ts,
                    r["user"],
                    r["user_label"],
                    r["text"],
                    r["thread_ts"],
                    r["subtype"],
                    r["raw_json"],
                    r["recorded_at"],
                ),
            )
            inserted += 1
        new_conn.execute("COMMIT")
    except Exception:
        new_conn.execute("ROLLBACK")
        raise

    log.info("v2 schema written: inserted=%d skipped_dup=%d", inserted, skipped_dup)

    new_conn.execute("VACUUM")
    new_conn.execute("ANALYZE")

    summary["v2_inserted_rows"] = inserted
    summary["v2_skipped_dup"] = skipped_dup
    summary["elapsed_s_approx"] = int(time.time())
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate archive DB to v2 schema")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stderr)],
    )
    out = migrate(dry_run=args.dry_run)
    import json

    print(json.dumps(out, indent=2, default=str))


if __name__ == "__main__":
    main()
