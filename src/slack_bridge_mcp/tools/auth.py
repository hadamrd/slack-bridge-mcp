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
import urllib.parse
import urllib.request
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


def _login_blocking() -> dict[str, Any]:
    with slack_context(headless=False) as ctx:
        page = ctx.new_page()
        page.goto(settings().workspace_url, wait_until="domcontentloaded", timeout=60_000)
        # If we land on a workspace sign-in page, auto-click a common SAML SSO
        # button so the user only has to handle their identity provider. Skip if
        # the button isn't there (already signed in / different page).
        sso = page.locator('a[href*="/sso/saml/start"]').first
        try:
            sso.wait_for(state="visible", timeout=4000)
            sso.click()
        except Exception:
            pass  # not on the sign-in page; proceed to wait for client redirect
        try:
            page.wait_for_url("**/client/**", timeout=300_000)
            return {"ok": True, "logged_in": True, "url": page.url}
        except Exception as e:
            return {
                "ok": False,
                "logged_in": False,
                "note": "did not detect successful login within 5 min; close the window when done.",
                "error": str(e),
            }


def _login() -> dict[str, Any]:
    return run_in_thread(_login_blocking)


# The bridge reads tokens directly from SLACK_BRIDGE_TOKEN_ENV_PATH via
# client._read_env on every call.


def _scrape_blocking() -> tuple[str | None, dict[str, str], str | None]:
    """Returns (xoxc, all_session_cookies, team_id).

    Capture the full session cookie set (d, d-s, b, x, lc) so we can keep
    operating without re-opening Chrome. The d-s cookie is browser-session
    in the cookie jar but server-validated by VALUE — persisting it in our
    env file lets every subsequent API call work even when no browser is
    running.
    """
    with slack_context(headless=True) as ctx:
        page = ctx.new_page()
        page.goto(settings().workspace_url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(5000)
        xoxc, team_id = extract_xoxc_from_page(page)
        cookies = extract_session_cookies(ctx)
        return xoxc, cookies, team_id


def _write_env(xoxc: str, cookies: dict[str, str]) -> None:
    """Persist xoxc + all session cookies. Atomic via .tmp + rename."""
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
    env_path.write_text("\n".join(lines) + "\n")
    env_path.chmod(0o600)


def _refresh() -> dict[str, Any]:
    xoxc, cookies, team_id = run_in_thread(_scrape_blocking)
    if not xoxc:
        return {
            "ok": False,
            "error": "no xoxc in localConfig — session likely expired. Run slack_login.",
        }
    if not cookies.get("d"):
        return {"ok": False, "error": "no d cookie in profile — run slack_login."}
    if not cookies.get("d-s"):
        # We can probably still operate from prior d-s if we have one cached,
        # but that means the live browser session is dying. Surface a warning
        # but still proceed.
        pass
    _write_env(xoxc, cookies)
    info = _auth_test(xoxc, cookies)
    if not info.get("ok"):
        return {"ok": False, "error": f"tokens scraped but auth.test rejected: {info}"}
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
