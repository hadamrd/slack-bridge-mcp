# slack-bridge-mcp

An MCP server that lets compatible AI clients search Slack, read threads,
send messages, download Slack-hosted files, and maintain an optional local
SQLite/Parquet archive for fast history search.

The project is intentionally configuration-only: no workspace domains, team
IDs, tokens, local paths, or private URLs are hardcoded. Runtime settings come
from process environment variables or a local env file.

## Features

- MCP tools for Slack search, channel history, threads, users, bots, files, and messaging.
- Browser-assisted login/token refresh using a persistent Playwright profile.
- Optional local archive with SQLite FTS5 hot storage and Parquet cold storage.
- Optional WebSocket watcher daemon with YAML rules and actions.
- Single `.env.local` configuration surface for paths, workspace URLs, and Slack IDs.

## Install

```bash
git clone https://github.com/<owner>/slack-bridge-mcp.git
cd slack-bridge-mcp
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/playwright install chrome
cp .env.example .env.local
```

Edit `.env.local` for your Slack workspace before running login.

## Configuration

By default the server reads `.env.local` in the current working directory.
Set `SLACK_BRIDGE_ENV_FILE=/path/to/env` to use a different file. Process
environment variables override values in the env file.

Required for most installations:

```bash
SLACK_BRIDGE_WORKSPACE_URL=https://your-workspace.slack.com/
SLACK_BRIDGE_API_BASE=https://your-workspace.slack.com/api/
SLACK_BRIDGE_WEB_BASE_URL=https://your-workspace.slack.com
SLACK_BRIDGE_TEAM_ID=T0123456789
```

All local storage defaults to `~/.slack-bridge-mcp`, and can be overridden:

```bash
SLACK_BRIDGE_CONFIG_DIR=~/.slack-bridge-mcp
SLACK_BRIDGE_TOKEN_ENV_PATH=~/.slack-bridge-mcp/tokens.env
SLACK_BRIDGE_BROWSER_PROFILE_DIR=~/.slack-bridge-mcp/browser-profile
SLACK_BRIDGE_ARCHIVE_DB_PATH=~/.slack-bridge-mcp/archive.db
SLACK_BRIDGE_COLD_ARCHIVE_DIR=~/.slack-bridge-mcp/archive-cold
SLACK_BRIDGE_USERS_CACHE_PATH=~/.slack-bridge-mcp/users-cache.json
SLACK_BRIDGE_WATCHER_RULES_PATH=~/.slack-bridge-mcp/watcher-rules.yml
SLACK_BRIDGE_LOG_DIR=~/.slack-bridge-mcp/logs
```

`tokens.env`, browser profiles, archive DBs, and caches contain private data.
Keep them outside Git and restrict file permissions.

## Run As MCP

Example MCP client config:

```json
{
  "mcpServers": {
    "slack-bridge": {
      "type": "stdio",
      "command": "/absolute/path/to/slack-bridge-mcp/.venv/bin/python",
      "args": ["-m", "slack_bridge_mcp.server"],
      "env": {
        "SLACK_BRIDGE_ENV_FILE": "/absolute/path/to/slack-bridge-mcp/.env.local"
      }
    }
  }
}
```

First run:

1. Call `slack_login` from your MCP client and complete Slack login in the browser.
2. Call `slack_refresh_tokens` to write the local token env file.
3. Call `slack_status` to verify the cached session.

## Optional Daemons

Archive poller:

```bash
SLACK_BRIDGE_ENV_FILE=.env.local .venv/bin/python -m slack_bridge_mcp.archive
```

Watcher:

```bash
SLACK_BRIDGE_ENV_FILE=.env.local .venv/bin/python -m slack_bridge_mcp.watcher
```

Compaction:

```bash
SLACK_BRIDGE_ENV_FILE=.env.local .venv/bin/python -m slack_bridge_mcp.archive.compact
```

macOS launchd templates are in `launchd/`. Replace placeholders such as
`{{REPO_DIR}}` and `{{PYTHON}}` before installing them.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check src tests
.venv/bin/ruff format --check src tests
PYTHONPATH=src .venv/bin/pytest -q
```

The manual pre-commit helper runs lint, format check, and mypy:

```bash
./hooks/pre-commit
```

## Security Notes

- This project uses Slack web-session tokens and cookies captured from your own browser session.
- Do not commit `.env.local`, token files, browser profiles, archive data, downloaded files, or logs.
- Prefer a dedicated Slack app/token flow where possible; browser-derived tokens may violate some workspace policies.
- Review your workspace policy before enabling message posting or watcher actions.
