"""Auto-refresh-on-auth-failure behaviour of client.call().

The bridge caches scraped Slack tokens; Slack rotates them server-side. When a
call comes back `invalid_auth`, call() should try one headless refresh and retry
— but only once, and it must NOT loop forever if the refresh can't recover.
"""

import pytest

from slack_bridge_mcp import client


@pytest.fixture(autouse=True)
def _reset_refresh_throttle(monkeypatch):
    # Neutralize the cooldown so each test drives refresh deterministically.
    monkeypatch.setattr(client, "_last_refresh_at", 0.0, raising=False)
    monkeypatch.setattr(client, "_last_refresh_ok", False, raising=False)
    monkeypatch.setattr(client, "_REFRESH_COOLDOWN_S", 0.0, raising=False)


def _env(xoxc="xoxc-live"):
    return {"SLACK_MCP_XOXC_TOKEN": xoxc, "SLACK_MCP_XOXD_TOKEN": "xoxd-live"}


def test_call_retries_after_successful_refresh(monkeypatch):
    monkeypatch.setattr(client, "_read_env", lambda: _env())
    posts = iter([{"ok": False, "error": "invalid_auth"}, {"ok": True, "messages": []}])
    monkeypatch.setattr(client, "_post", lambda *a, **k: next(posts))

    refreshed = {"n": 0}

    def _fake_refresh():
        refreshed["n"] += 1
        return True

    monkeypatch.setattr(client, "_try_auto_refresh", _fake_refresh)

    out = client.call("conversations.replies", channel="C1", ts="1.2")

    assert out["ok"] is True
    assert refreshed["n"] == 1  # exactly one refresh


def test_call_gives_up_after_one_refresh(monkeypatch):
    monkeypatch.setattr(client, "_read_env", lambda: _env())
    # Always invalid_auth, even after refresh -> must not loop, must raise with
    # a slack_login hint.
    monkeypatch.setattr(client, "_post", lambda *a, **k: {"ok": False, "error": "invalid_auth"})
    monkeypatch.setattr(client, "_try_auto_refresh", lambda: True)

    with pytest.raises(client.SlackError) as ei:
        client.call("conversations.replies", channel="C1", ts="1.2")

    assert "slack_login" in str(ei.value)


def test_call_does_not_refresh_on_non_auth_error(monkeypatch):
    monkeypatch.setattr(client, "_read_env", lambda: _env())
    monkeypatch.setattr(client, "_post", lambda *a, **k: {"ok": False, "error": "channel_not_found"})

    called = {"n": 0}
    monkeypatch.setattr(client, "_try_auto_refresh", lambda: called.__setitem__("n", called["n"] + 1) or True)

    with pytest.raises(client.SlackError) as ei:
        client.call("conversations.replies", channel="C1", ts="1.2")

    assert "channel_not_found" in str(ei.value)
    assert called["n"] == 0  # never attempted a refresh


def test_call_refreshes_when_xoxc_missing(monkeypatch):
    envs = iter([{}, _env()])  # first read has no token, second (post-refresh) does
    monkeypatch.setattr(client, "_read_env", lambda: next(envs))
    monkeypatch.setattr(client, "_post", lambda *a, **k: {"ok": True, "messages": []})
    monkeypatch.setattr(client, "_try_auto_refresh", lambda: True)

    out = client.call("conversations.replies", channel="C1", ts="1.2")
    assert out["ok"] is True
