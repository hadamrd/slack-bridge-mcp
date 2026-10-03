"""Slack auth bridge tools — keep a persistent browser session alive and
extract `xoxc`/`xoxd` from it on demand for the actions MCP to consume.

Tools:
  - slack_login           -> first-time setup. Opens a headed Chromium against
                             the configured Slack workspace so the user can
                             complete their normal login flow
                             once. Subsequent calls reuse the persisted profile.
  - slack_refresh_tokens  -> headless. Opens the configured workspace inside the warm
                             profile, scrapes xoxc from the bootstrap HTML,
                             pulls xoxd from the cookie jar, writes both to
                             SLACK_BRIDGE_TOKEN_ENV_PATH. Caller is
                             responsible for restarting the slack MCP afterwards.
  - slack_status          -> cheap "is the session alive?" probe. Hits
                             slack.com/api/auth.test with the cached token and
                             current cookies.
"""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from contextlib import suppress
from typing import Any

from mcp.types import Tool

from ..browser import (
    UA,
    extract_session_cookies,
    extract_xoxc_from_page,
    run_in_thread,
    slack_context,
)
from ..config import settings, token_env_path

TOOLS: list[Tool] = [
    Tool(
        name="slack_login",
        description=(
            "Open a HEADED Chromium against app.slack.com so the user can "
            "complete the workspace login flow once. The session is persisted at "
            "SLACK_BRIDGE_BROWSER_PROFILE_DIR and reused by future headless "
            "tool calls. Run this only when slack_status reports the session "
            "is dead. Blocks until the user closes the browser window."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="slack_refresh_tokens",
        description=(
            "Headless: open the persistent profile, navigate to "
            "SLACK_BRIDGE_WORKSPACE_URL, scrape xoxc from the browser state, pull xoxd "
            "from the cookie jar, write both to SLACK_BRIDGE_TOKEN_ENV_PATH. "
            "Returns the user/team that the tokens authenticate as. Caller "
            "must call mcp_restart('slack') afterwards for the actions MCP to "
            "see the new tokens."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="slack_status",
        description=(
            "Health check: read tokens from SLACK_BRIDGE_TOKEN_ENV_PATH, "
            "POST slack.com/api/auth.test, return {ok, user, team, error}. "
            "Cheap (one HTTP call); call before assuming the session is alive."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
]


def _read_env() -> dict[str, str]:
    env_path = token_env_path()
    if not env_path.exists():
        return {}
    out = {}
    for line in env_path.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


# _write_env now takes (xoxc, cookies_dict) — see definition below.


def _click_sso(page) -> None:
    """Auto-click the SAML SSO start button if we land on a sign-in page.

    On a warm identity-provider session (Okta/DUO cookies still valid) this
    completes the whole SSO handshake via redirects with no user interaction —
    which is what lets the HEADLESS refresh self-heal a dead Slack token. If
    the button isn't there (already signed in, or a different page), skip.
    """
    sso = page.locator('a[href*="/sso/saml/start"]').first
    with suppress(Exception):
        sso.wait_for(state="visible", timeout=4000)
        sso.click()


def _navigate_and_capture(
    ctx, page, capture_timeout_s: int
) -> tuple[str | None, dict[str, str], str | None]:
    """Navigate to the workspace, drive SSO if needed, and poll for a live
    token. Returns (xoxc, cookies, team_id) captured from the SAME session so
    the token/cookie pair is always consistent.

    We poll `localConfig_v2` for a scrapable `xoxc-` (the authoritative "really
    logged in" signal) rather than watching for a `/client/` redirect — the
    latter is fragile on the Enterprise Grid unified client-v2, which lands on
    `<workspace>.enterprise.slack.com` and hydrates `teams` asynchronously.
    """
    page.goto(settings().workspace_url, wait_until="domcontentloaded", timeout=60_000)
    _click_sso(page)
    xoxc: str | None = None
    team_id: str | None = None
    deadline = time.monotonic() + capture_timeout_s
    while True:
        # Check first so a warm session returns immediately instead of eating a
        # full poll interval.
        with suppress(Exception):
            xoxc, team_id = extract_xoxc_from_page(page)
        if xoxc or time.monotonic() >= deadline:
            break
        page.wait_for_timeout(2000)
    return xoxc, extract_session_cookies(ctx), team_id


def _login_blocking(capture_timeout_s: int = 300) -> dict[str, Any]:
    """Headed login that ALSO captures tokens in the same session.

    Grabs the token AND all five session cookies together and persists them —
    no separate headless refresh, no cookie/token drift. The window can be left
    open; it returns on its own the moment a live token appears.
    """
    with slack_context(headless=False) as ctx:
        page = ctx.new_page()
        xoxc, cookies, team_id = _navigate_and_capture(ctx, page, capture_timeout_s)
        if not xoxc:
            return {
                "ok": False,
                "logged_in": False,
                "note": (
                    "no live token appeared within the window — complete SSO until "
                    "you see your channels, then re-run. Leave the window open."
                ),
                "url": page.url,
            }
        result = _persist_and_verify(xoxc, cookies, team_id)
        result["logged_in"] = True
        return result


def _login() -> dict[str, Any]:
    return run_in_thread(_login_blocking)


# The bridge reads tokens directly from SLACK_BRIDGE_TOKEN_ENV_PATH via
# client._read_env on every call.


def _scrape_blocking(capture_timeout_s: int = 30) -> tuple[str | None, dict[str, str], str | None]:
    """Headless capture: same navigate-and-poll path as login, minus the UI.

    Because it drives SSO too, a dead Slack token self-heals with ZERO
    interaction as long as the identity-provider session is still warm — the
    common "logged into Okta an hour ago, Slack token rotated" case. Bounded so
    a genuinely-dead profile (no token, cold IdP) still returns promptly and we
    fall back to an interactive slack_login.
    """
    with slack_context(headless=True) as ctx:
        page = ctx.new_page()
        return _navigate_and_capture(ctx, page, capture_timeout_s)


def _write_env(xoxc: str, cookies: dict[str, str]) -> None:
    """Persist xoxc + all session cookies, atomically.

    The token file is read by SEPARATE processes (the archive daemon, watcher
    and pet runners each read it via client._read_env), so a plain overwrite
    risks a torn read — or an outright-corrupt file if we crash mid-write. We
    write to a temp file in the same directory, fix its perms, then os.replace
    it over the target (atomic rename on the same filesystem).
    """
    env_path = token_env_path()
    env_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lines = [f"SLACK_MCP_XOXC_TOKEN={xoxc}"]
    # Map cookie names to env-friendly keys
    for env_key, cookie_name in (
        ("SLACK_MCP_XOXD_TOKEN", "d"),
        ("SLACK_MCP_DS_TOKEN", "d-s"),
        ("SLACK_MCP_B_TOKEN", "b"),
        ("SLACK_MCP_X_TOKEN", "x"),
        ("SLACK_MCP_LC_TOKEN", "lc"),
    ):
        if cookies.get(cookie_name):
            lines.append(f"{env_key}={cookies[cookie_name]}")
    tmp_path = env_path.with_name(f"{env_path.name}.{os.getpid()}.tmp")
    tmp_path.write_text("\n".join(lines) + "\n")
    tmp_path.chmod(0o600)
    os.replace(tmp_path, env_path)


def _persist_and_verify(
    xoxc: str, cookies: dict[str, str], team_id: str | None
) -> dict[str, Any]:
    """Write the token + cookies to the env file and confirm with auth.test.
    Shared by both the headed login and the headless refresh so recovery
    behaves identically whichever path produced the tokens."""
    if not cookies.get("d"):
        return {"ok": False, "error": "no 'd' cookie captured — run slack_login."}
    _write_env(xoxc, cookies)
    info = _auth_test(xoxc, cookies)
    if not info.get("ok"):
        return {"ok": False, "error": f"tokens captured but auth.test rejected: {info}"}
    return {
        "ok": True,
        "user": info.get("user"),
        "team": info.get("team"),
        "team_id": team_id,
        "user_id": info.get("user_id"),
        "cookies_captured": sorted(cookies.keys()),
        "ds_captured": "d-s" in cookies,
        "env_path": str(token_env_path()),
    }


def _refresh() -> dict[str, Any]:
    xoxc, cookies, team_id = run_in_thread(_scrape_blocking)
    if not xoxc:
        return {
            "ok": False,
            "error": "no xoxc in localConfig — session likely expired. Run slack_login.",
        }
    return _persist_and_verify(xoxc, cookies, team_id)


def _auth_test(xoxc: str, cookies: dict[str, str]) -> dict[str, Any]:
    """Verify the xoxc + cookie set with auth.test. Sends ALL cookies in the
    Cookie header so we test the same path the rest of the bridge uses."""
    cookie_hdr = "; ".join(
        f"{k}={v}" for k, v in cookies.items() if v and k in ("d", "d-s", "b", "x", "lc")
    )
    req = urllib.request.Request(
        settings().api_base + "auth.test",
        data=urllib.parse.urlencode({"token": xoxc}).encode(),
        headers={
            "Cookie": cookie_hdr,
            "User-Agent": UA,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    try:
        return json.loads(urllib.request.urlopen(req, timeout=15).read())
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def _status() -> dict[str, Any]:
    env = _read_env()
    xoxc = env.get("SLACK_MCP_XOXC_TOKEN")
    if not xoxc:
        return {"ok": False, "error": "no xoxc cached — run slack_refresh_tokens"}
    cookies: dict[str, str] = {}
    for env_key, cookie_name in (
        ("SLACK_MCP_XOXD_TOKEN", "d"),
        ("SLACK_MCP_DS_TOKEN", "d-s"),
        ("SLACK_MCP_B_TOKEN", "b"),
        ("SLACK_MCP_X_TOKEN", "x"),
        ("SLACK_MCP_LC_TOKEN", "lc"),
    ):
        if env.get(env_key):
            cookies[cookie_name] = env[env_key]
    if "d" not in cookies:
        return {"ok": False, "error": "no d cookie cached — run slack_refresh_tokens"}
    info = _auth_test(xoxc, cookies)
    return {
        "ok": bool(info.get("ok")),
        "user": info.get("user"),
        "team": info.get("team"),
        "cookies_in_use": sorted(cookies.keys()),
        "error": info.get("error"),
    }


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    if name == "slack_login":
        return _login()
    if name == "slack_refresh_tokens":
        return _refresh()
    if name == "slack_status":
        return _status()
    return None
