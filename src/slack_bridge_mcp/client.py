"""Thin HTTP client for slack.com/api calls authenticated with the bridge's
xoxc + xoxd tokens. Reuses the same env file that `slack_refresh_tokens`
writes — single source of truth.

Not a full Slack SDK. Just enough to back conversational tools (list, history,
search, post). Returns parsed JSON dicts; raises `SlackError` on transport or
API-level failures so dispatchers can convert them to user-friendly errors.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .config import settings, token_env_path

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Slack API-level errors that mean "your tokens are no longer valid". When we
# see one of these we try a single headless token refresh (re-scrape xoxc +
# cookies from the warm browser profile) and retry the call. This closes the
# common failure mode: Slack rotates the cookie set server-side while the
# browser profile session is still alive, so a headless re-scrape recovers
# without any human. It CANNOT recover a fully-expired profile — that still
# needs an interactive slack_login (SSO can't be completed headlessly).
_AUTH_ERRORS = frozenset(
    {"invalid_auth", "not_authed", "token_revoked", "token_expired", "invalid_session"}
)

# Serialize refresh attempts and throttle them: a burst of failing calls must
# trigger at most one browser launch per cooldown window, and concurrent
# callers should reuse the result of the in-flight refresh rather than each
# spawning their own headless Chrome.
_refresh_lock = threading.Lock()
_last_refresh_at = 0.0
_last_refresh_ok = False
_REFRESH_COOLDOWN_S = 60.0


def _try_auto_refresh() -> bool:
    """Headless token refresh after an auth failure. Returns True if the env
    now holds fresh, working tokens.

    Throttled + serialized via `_refresh_lock`: within `_REFRESH_COOLDOWN_S`
    of a prior attempt we return that attempt's result instead of launching a
    second browser. A failed refresh (dead profile) is cached too, so a storm
    of failing calls doesn't repeatedly open Chrome only to fail again.
    """
    global _last_refresh_at, _last_refresh_ok
    with _refresh_lock:
        now = time.monotonic()
        if now - _last_refresh_at < _REFRESH_COOLDOWN_S:
            return _last_refresh_ok
        # Late import: avoids a module-load import cycle (tools.auth -> browser
        # -> client). Safe at call time — client is fully initialized by now.
        from .tools.auth import _refresh

        try:
            result = _refresh()
        except Exception:
            result = {"ok": False}
        _last_refresh_at = time.monotonic()
        _last_refresh_ok = bool(result.get("ok"))
        return _last_refresh_ok


class SlackError(Exception):
    """Raised when the Slack API returns ok=false or transport fails."""


def _read_env() -> dict[str, str]:
    env_path = token_env_path()
    if not env_path.exists():
        raise SlackError(
            f"{env_path} missing; run slack_refresh_tokens (or slack_login if the session expired)"
        )
    env: dict[str, str] = {}
    for line in env_path.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def _build_cookie_header(env: dict[str, str]) -> str:
    """Build the full Cookie header from the env file.

    Why all five: we discovered (2026-05-09) that Slack server validates the
    `d-s` cookie by VALUE not by liveness. As long as we keep sending the
    captured d-s value (plus d, b, x, lc), the server keeps the session
    alive — no live browser required. Sending only `d` is no longer enough
    (server appears to require d-s for fresh xoxc emission).
    """
    pairs: list[tuple[str, str]] = []
    for env_key, cookie_name in (
        ("SLACK_MCP_XOXD_TOKEN", "d"),
        ("SLACK_MCP_DS_TOKEN", "d-s"),
        ("SLACK_MCP_B_TOKEN", "b"),
        ("SLACK_MCP_X_TOKEN", "x"),
        ("SLACK_MCP_LC_TOKEN", "lc"),
    ):
        v = env.get(env_key)
        if v:
            pairs.append((cookie_name, v))
    return "; ".join(f"{k}={v}" for k, v in pairs)


def _tokens() -> tuple[str, str]:
    """Backwards-compatible: returns (xoxc, xoxd). Prefer _read_env directly
    when you need the full cookie set."""
    env = _read_env()
    xoxc = env.get("SLACK_MCP_XOXC_TOKEN")
    xoxd = env.get("SLACK_MCP_XOXD_TOKEN")
    if not (xoxc and xoxd):
        raise SlackError("xoxc/xoxd tokens missing from env file — run slack_refresh_tokens")
    return xoxc, xoxd


def _post(method: str, env: dict[str, str], params: dict[str, Any]) -> dict[str, Any]:
    """One rate-limited POST to slack.com/api/<method>. Returns the parsed
    JSON payload as-is (it may carry ok=false). Retries once on 429; other
    transport/HTTP errors raise SlackError.
    """
    from . import ratelimit

    xoxc = env.get("SLACK_MCP_XOXC_TOKEN") or ""
    body = urllib.parse.urlencode(
        {"token": xoxc, **{k: v for k, v in params.items() if v is not None}}
    )
    headers = {
        "Cookie": _build_cookie_header(env),
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }

    url = settings().api_base + method
    req = urllib.request.Request(url, data=body.encode(), headers=headers)
    for attempt in range(2):
        # Block until the bucket grants this method a token.
        if not ratelimit.acquire(method, max_wait_s=120.0):
            raise SlackError(f"{method}: rate-limit budget unavailable after 120s wait")
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", "5"))
                # the bucket now refuses every caller until Retry-After has passed; the
                # next acquire waits for it (or gives up past its 120s budget)
                ratelimit.penalty_429(method, retry_after_s=retry_after)
                if attempt == 0:
                    continue
            raise SlackError(f"{method}: HTTP {e.code} — {e.read()[:200]!r}") from e
        except urllib.error.URLError as e:
            raise SlackError(f"{method}: transport — {e}") from e
    raise SlackError(f"{method}: rate-limited twice")


def call(method: str, **params: Any) -> dict[str, Any]:
    """POST to slack.com/api/<method> with form-encoded params + xoxc auth.

    Goes through the token-bucket rate limiter (`ratelimit.acquire`) before
    sending — daemon polls, MCP tool calls, and backfill scans all share the
    same per-method-class budget, so a backfill can't starve the user's
    interactive tools (and vice versa).

    Retries once on rate-limit (429 — handled in `_post`). On an auth-level
    error (`invalid_auth` etc.) it attempts one headless token refresh and
    retries — recovering transparently when Slack has rotated the cookie set
    but the browser profile is still logged in. If the refresh can't produce
    working tokens, raises SlackError pointing at slack_login.
    """
    for auth_attempt in range(2):
        env = _read_env()
        xoxc = env.get("SLACK_MCP_XOXC_TOKEN") or ""
        if not xoxc:
            if auth_attempt == 0 and _try_auto_refresh():
                continue
            raise SlackError(
                "xoxc missing from env — run slack_refresh_tokens "
                "(or slack_login if the session expired)"
            )

        payload = _post(method, env, params)
        if payload.get("ok"):
            return payload

        err = payload.get("error", "unknown")
        if err in _AUTH_ERRORS and auth_attempt == 0 and _try_auto_refresh():
            continue  # retry once with freshly-scraped tokens
        if err in _AUTH_ERRORS:
            raise SlackError(
                f"{method}: {err} — token refresh could not recover the session; "
                f"run slack_login to complete SSO. ({payload})"
            )
        raise SlackError(f"{method}: {err} — {payload}")

    raise SlackError(f"{method}: authentication failed after refresh — run slack_login")


def fetch_url(url: str, *, max_bytes: int | None = None) -> tuple[bytes, dict[str, str]]:
    """Cookie-authenticated GET for non-API URLs (e.g. files.slack.com
    `url_private_download`). Returns (body, headers). Caps at `max_bytes`
    if given (truncates the read; full body still streamed)."""
    env = _read_env()
    headers = {
        "Cookie": _build_cookie_header(env),
        "User-Agent": UA,
    }
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read(max_bytes) if max_bytes else resp.read()
            resp_headers = {k.lower(): v for k, v in resp.headers.items()}
        return body, resp_headers
    except urllib.error.HTTPError as e:
        raise SlackError(f"fetch {url}: HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise SlackError(f"fetch {url}: transport — {e}") from e


def paged(method: str, key: str, page_size: int = 200, max_items: int = 2000, **params: Any):
    """Yield items across all cursor pages of a paginated method."""
    cursor = ""
    yielded = 0
    while True:
        data = call(method, limit=page_size, cursor=cursor or None, **params)
        for item in data.get(key, []):
            yield item
            yielded += 1
            if yielded >= max_items:
                return
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            return
