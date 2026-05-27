"""Runtime configuration loaded from environment variables and an env file."""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

ENV_FILE_VAR = "SLACK_BRIDGE_ENV_FILE"
DEFAULT_ENV_FILE = ".env.local"


def _env_file_path() -> Path:
    return Path(os.environ.get(ENV_FILE_VAR, DEFAULT_ENV_FILE)).expanduser()


@lru_cache(maxsize=8)
def _read_env_file(path: str) -> dict[str, str]:
    env_path = Path(path).expanduser()
    if not env_path.exists():
        return {}
    values: dict[str, str] = {}
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        try:
            parsed = shlex.split(value, comments=False, posix=True)
            value = parsed[0] if len(parsed) == 1 else value
        except ValueError:
            value = value.strip("\"'")
        values[key] = value
    return values


def env(name: str, default: str | None = None) -> str | None:
    """Read a value from the process env first, then the configured env file."""
    if name in os.environ:
        return os.environ[name]
    return _read_env_file(str(_env_file_path())).get(name, default)


def path_env(name: str, default: Path | str) -> Path:
    value = env(name)
    if value:
        return Path(value).expanduser()
    return Path(default).expanduser()


def _origin(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}"
    return url.rstrip("/")


@dataclass(frozen=True)
class Settings:
    env_file: Path
    config_dir: Path
    workspace_url: str
    api_base: str
    web_base_url: str
    app_base_url: str
    team_id: str | None
    enterprise_id: str | None
    websocket_gateway_server: str | None
    browser_timezone: str
    token_env_path: Path
    browser_profile_dir: Path
    archive_db_path: Path
    cold_archive_dir: Path
    users_cache_path: Path
    watcher_rules_path: Path
    watcher_log_path: Path
    archive_log_path: Path
    archive_compact_log_path: Path


def settings() -> Settings:
    config_dir = path_env("SLACK_BRIDGE_CONFIG_DIR", Path.home() / ".slack-bridge-mcp")
    workspace_url = env("SLACK_BRIDGE_WORKSPACE_URL", "https://app.slack.com/client") or ""
    api_base = env("SLACK_BRIDGE_API_BASE", "https://slack.com/api/") or ""
    web_base_url = env("SLACK_BRIDGE_WEB_BASE_URL") or _origin(workspace_url)
    log_dir = path_env("SLACK_BRIDGE_LOG_DIR", config_dir / "logs")

    return Settings(
        env_file=_env_file_path(),
        config_dir=config_dir,
        workspace_url=workspace_url,
        api_base=api_base,
        web_base_url=web_base_url.rstrip("/"),
        app_base_url=(env("SLACK_BRIDGE_APP_BASE_URL", "https://app.slack.com") or "").rstrip("/"),
        team_id=env("SLACK_BRIDGE_TEAM_ID"),
        enterprise_id=env("SLACK_BRIDGE_ENTERPRISE_ID"),
        websocket_gateway_server=env("SLACK_BRIDGE_WEBSOCKET_GATEWAY_SERVER"),
        browser_timezone=env("SLACK_BRIDGE_BROWSER_TIMEZONE", "UTC") or "UTC",
        token_env_path=path_env("SLACK_BRIDGE_TOKEN_ENV_PATH", config_dir / "tokens.env"),
        browser_profile_dir=path_env(
            "SLACK_BRIDGE_BROWSER_PROFILE_DIR", config_dir / "browser-profile"
        ),
        archive_db_path=path_env("SLACK_BRIDGE_ARCHIVE_DB_PATH", config_dir / "archive.db"),
        cold_archive_dir=path_env("SLACK_BRIDGE_COLD_ARCHIVE_DIR", config_dir / "archive-cold"),
        users_cache_path=path_env("SLACK_BRIDGE_USERS_CACHE_PATH", config_dir / "users-cache.json"),
        watcher_rules_path=path_env(
            "SLACK_BRIDGE_WATCHER_RULES_PATH", config_dir / "watcher-rules.yml"
        ),
        watcher_log_path=path_env("SLACK_BRIDGE_WATCHER_LOG_PATH", log_dir / "watcher.log"),
        archive_log_path=path_env("SLACK_BRIDGE_ARCHIVE_LOG_PATH", log_dir / "archive.log"),
        archive_compact_log_path=path_env(
            "SLACK_BRIDGE_ARCHIVE_COMPACT_LOG_PATH", log_dir / "archive-compact.log"
        ),
    )


def token_env_path() -> Path:
    return settings().token_env_path


def permalink(channel_id: str, ts: str) -> str:
    return f"{settings().web_base_url}/archives/{channel_id}/p{ts.replace('.', '')}"


def app_channel_url(channel_id: str) -> str:
    cfg = settings()
    if cfg.team_id:
        return f"{cfg.app_base_url}/client/{cfg.team_id}/{channel_id}"
    return f"{cfg.app_base_url}/client/{channel_id}"
