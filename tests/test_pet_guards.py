"""Pet grants are enforced and rules can skip the user's own posts."""
from slack_bridge_mcp.pets.runner import builtin_tools
from slack_bridge_mcp.watcher.rules import _match_rule


def test_only_granted_builtins_are_exposed():
    allowed = ["mcp__slack-bridge__slack_channel_history", "Write(//p/memory/**)", "Edit(//p/memory/**)"]
    assert builtin_tools(allowed) == ["Write", "Edit"]
    assert builtin_tools(["mcp__slack-bridge__slack_thread"]) == []  # no Bash, no file tools


def test_ignore_self_skips_own_messages():
    rule = {"name": "echo", "match": {"channel_id": "C1"}}
    mine, theirs = {"channel": "C1", "user": "U_ME", "text": "x"}, {"channel": "C1", "user": "U2", "text": "x"}
    assert not _match_rule(rule, mine, "U_ME")
    assert _match_rule(rule, theirs, "U_ME")
    assert _match_rule({**rule, "ignore_self": False}, mine, "U_ME")
