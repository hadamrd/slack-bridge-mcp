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
