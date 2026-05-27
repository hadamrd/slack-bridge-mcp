"""Slack-watcher daemon — long-lived WS subscriber + YAML rule engine.

Connects via the URL from `client.getWebSocketURL`, sends Cookie+Origin
headers (matching the official Slack web client), reads message events,
matches against rules, dispatches actions in a worker thread pool.

Run via launchd:
  launchctl load -w ~/Library/LaunchAgents/com.example.slack-bridge-watcher.plist

Or directly:
  .venv/bin/python -m slack_bridge_mcp.watcher

Rules live at SLACK_BRIDGE_WATCHER_RULES_PATH; auto-reloaded on
mtime change (every 50 events).
"""
