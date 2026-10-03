from slack_bridge_mcp.archive import compact, db


def test_compact_empty_archive_preserves_dry_run_flag(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_BRIDGE_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("SLACK_BRIDGE_ARCHIVE_DB_PATH", str(tmp_path / "archive.db"))
    monkeypatch.setenv("SLACK_BRIDGE_COLD_ARCHIVE_DIR", str(tmp_path / "cold"))
    db.DB_PATH = tmp_path / "archive.db"
    compact.db.DB_PATH = tmp_path / "archive.db"

    result = compact.compact(horizon_days=90, dry_run=True)

    assert result["ok"] is True
    assert result["dry_run"] is True
    assert result["moved"] == 0
    assert result["groups"] == 0


def test_compact_keeps_edits_and_deletes_that_land_after_the_snapshot(tmp_path, monkeypatch):
    db.DB_PATH = compact.db.DB_PATH = tmp_path / "archive.db"
    conn = db.open_db()
    db.init_schema(conn)
    old = ["1000000001.000100", "1000000002.000100", "1000000003.000100"]
    for ts in old:
        db.insert_message(conn, channel_id="C1", ts=ts, user="U1", user_label=None, text="v0",
                          thread_ts=None, subtype=None, raw={}, via="test")
    shards = []

    def append_shard(ym, cid, rows):
        shards.append(rows)
        # the watcher writes while the shard is being written
        db.apply_edit(conn, channel_id="C1", ts=old[0], user="U1", user_label=None, text="v1",
                      thread_ts=None, subtype=None, raw={}, via="test")
        db.mark_deleted(conn, "C1", old[1])
        return tmp_path, len(rows)

    monkeypatch.setattr(compact.cold, "append_shard", append_shard)
    monkeypatch.setattr(compact.cold, "stats", lambda: {"total_size_bytes": 0})

    result = compact.compact(horizon_days=1)

    left = conn.execute("SELECT ts, text, deleted_at FROM messages ORDER BY msg_id").fetchall()
    assert result["changed_during_compaction"] == 2
    assert [r["ts"] for r in left] == [old[0], old[1], old[0]]   # v0 history + v1, and the tombstone
    assert left[2]["text"] == "v1" and left[1]["deleted_at"] is not None
