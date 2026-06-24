"""Slack "pets" — a framework for programmable, AI-augmented Slack agents.

A pet is a directory under SLACK_BRIDGE_BOTS_DIR. Its *behavior tree* is a
`.claude/skills/` directory + a `CLAUDE.md` persona; its capabilities (memory,
MCP servers, Slack mutation tools) are declared in `bot.yml`. The supervisor
daemon (in ``slack_bridge_mcp.watcher``) funnels matching Slack events to the
pet, which runs as an isolated headless ``claude -p`` subprocess, decides what
to do via its skills, and acts through its granted tools.

Submodules:
- ``spec``     — load + validate ``bot.yml`` into ``BotSpec``.
- ``registry`` — discover pets, compile triggers into rules, list/status.
- ``runner``   — invoke a pet (build mcp-config + allowedTools + prompt, run).
- ``audit``    — snapshot-before-mutate, audit JSONL, undo.
"""
