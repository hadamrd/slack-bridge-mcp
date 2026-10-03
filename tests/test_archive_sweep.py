"""The sweeper checkpoint moves only once every page down to it was read."""
from slack_bridge_mcp.archive import daemon, db


def test_backlog_beyond_the_page_cap_is_not_skipped(tmp_path, monkeypatch):
    db.DB_PATH = tmp_path / "archive.db"
    conn = db.open_db()
    db.init_schema(conn)
    db.ensure_channel(conn, "C1", "c1", is_im=False)
    conn.execute("UPDATE channels SET last_ts='100.000000' WHERE id='C1'")
    history = [f"{100 + i}.000000" for i in range(1, 7)]  # 6 messages newer than the checkpoint

    def call(method, channel, oldest, limit, latest="", cursor=""):
        msgs = sorted((t for t in history if t > oldest and (not latest or t < latest)), reverse=True)
        start = int(cursor or 0)
        page = msgs[start:start + 2]
        more = start + 2 < len(msgs)
        return {"messages": [{"ts": t, "text": t, "user": "U1"} for t in page], "has_more": more,
                "response_metadata": {"next_cursor": str(start + 2) if more else ""}}

    monkeypatch.setattr(daemon, "call", call)
    monkeypatch.setattr(daemon, "MAX_PAGES_PER_CHANNEL", 1)
    monkeypatch.setattr(daemon, "_backlog", {})

    sweeps = 0
    while sweeps < 5:
        daemon._sweep_channel(conn, "C1")
        sweeps += 1
        if not daemon._backlog:
            break
    stored = [r["ts"] for r in conn.execute("SELECT ts FROM messages ORDER BY ts")]
    assert stored == history
    assert db.get_channel_last_ts(conn, "C1") == history[-1]
    assert sweeps == 3  # 2 messages per sweep, checkpoint moved only on the last one
