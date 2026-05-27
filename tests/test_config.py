from pathlib import Path

from slack_bridge_mcp import config


def test_env_file_values_are_loaded_when_process_env_is_empty(tmp_path, monkeypatch):
    env_file = tmp_path / ".env.local"
    env_file.write_text(
        "\n".join(
            [
                "SLACK_BRIDGE_WORKSPACE_URL=https://example.slack.com/",
                "SLACK_BRIDGE_API_BASE=https://example.slack.com/api/",
                "SLACK_BRIDGE_TEAM_ID=T1234567890",
                "SLACK_BRIDGE_CONFIG_DIR=~/custom-slack-bridge",
            ]
        )
        + "\n"
    )
    monkeypatch.setenv("SLACK_BRIDGE_ENV_FILE", str(env_file))
    for key in (
        "SLACK_BRIDGE_WORKSPACE_URL",
        "SLACK_BRIDGE_API_BASE",
        "SLACK_BRIDGE_TEAM_ID",
        "SLACK_BRIDGE_CONFIG_DIR",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = config.settings()

    assert settings.workspace_url == "https://example.slack.com/"
    assert settings.api_base == "https://example.slack.com/api/"
    assert settings.team_id == "T1234567890"
    assert settings.config_dir == Path.home() / "custom-slack-bridge"


def test_process_env_overrides_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env.local"
    env_file.write_text("SLACK_BRIDGE_API_BASE=https://from-file.slack.com/api/\n")
    monkeypatch.setenv("SLACK_BRIDGE_ENV_FILE", str(env_file))
    monkeypatch.setenv("SLACK_BRIDGE_API_BASE", "https://from-env.slack.com/api/")

    assert config.settings().api_base == "https://from-env.slack.com/api/"


def test_default_paths_are_under_generic_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_BRIDGE_ENV_FILE", str(tmp_path / "missing.env"))
    monkeypatch.setenv("SLACK_BRIDGE_CONFIG_DIR", "/tmp/slack-bridge-config")
    for key in (
        "SLACK_BRIDGE_TOKEN_ENV_PATH",
        "SLACK_BRIDGE_BROWSER_PROFILE_DIR",
        "SLACK_BRIDGE_ARCHIVE_DB_PATH",
        "SLACK_BRIDGE_COLD_ARCHIVE_DIR",
        "SLACK_BRIDGE_USERS_CACHE_PATH",
        "SLACK_BRIDGE_WATCHER_RULES_PATH",
    ):
        monkeypatch.delenv(key, raising=False)

    settings = config.settings()

    assert settings.token_env_path == Path("/tmp/slack-bridge-config/tokens.env")
    assert settings.browser_profile_dir == Path("/tmp/slack-bridge-config/browser-profile")
    assert settings.archive_db_path == Path("/tmp/slack-bridge-config/archive.db")
    assert settings.cold_archive_dir == Path("/tmp/slack-bridge-config/archive-cold")
    assert settings.users_cache_path == Path("/tmp/slack-bridge-config/users-cache.json")
    assert settings.watcher_rules_path == Path("/tmp/slack-bridge-config/watcher-rules.yml")
