"""_write_env must produce a correct, 0600, atomically-written token file.

The token file is read by separate processes, so a partial/torn write would
break them. We can't easily prove atomicity, but we can assert: correct
content, correct perms, only cookies that are present, and no leftover .tmp.
"""

import stat

from slack_bridge_mcp.tools import auth


def _configure(monkeypatch, tmp_path):
    target = tmp_path / "tokens.env"
    monkeypatch.setattr(auth, "token_env_path", lambda: target)
    return target


def test_write_env_content_perms_and_no_tmp(monkeypatch, tmp_path):
    target = _configure(monkeypatch, tmp_path)
    auth._write_env(
        "xoxc-abc",
        {"d": "xoxd-1", "d-s": "ds-1", "b": "b-1", "x": "x-1", "lc": "lc-1"},
    )

    body = target.read_text()
    assert "SLACK_MCP_XOXC_TOKEN=xoxc-abc" in body
    assert "SLACK_MCP_XOXD_TOKEN=xoxd-1" in body
    assert "SLACK_MCP_DS_TOKEN=ds-1" in body
    # 0600
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    # no leftover temp files in the dir
    assert list(tmp_path.glob("*.tmp")) == []


def test_write_env_omits_absent_cookies(monkeypatch, tmp_path):
    target = _configure(monkeypatch, tmp_path)
    auth._write_env("xoxc-abc", {"d": "xoxd-1"})  # only d present

    body = target.read_text()
    assert "SLACK_MCP_XOXD_TOKEN=xoxd-1" in body
    for absent in ("SLACK_MCP_DS_TOKEN", "SLACK_MCP_B_TOKEN", "SLACK_MCP_X_TOKEN"):
        assert absent not in body


def test_write_env_overwrites_atomically(monkeypatch, tmp_path):
    target = _configure(monkeypatch, tmp_path)
    auth._write_env("xoxc-old", {"d": "old"})
    auth._write_env("xoxc-new", {"d": "new"})

    body = target.read_text()
    assert "xoxc-new" in body and "xoxc-old" not in body
    assert list(tmp_path.glob("*.tmp")) == []
