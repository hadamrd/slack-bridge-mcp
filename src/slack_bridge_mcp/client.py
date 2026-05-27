"""Thin HTTP client for slack.com/api calls authenticated with the bridge's
xoxc + xoxd tokens. Reuses the same env file that `slack_refresh_tokens`
writes — single source of truth.

Not a full Slack SDK. Just enough to back conversational tools (list, history,
search, post). Returns parsed JSON dicts; raises `SlackError` on transport or
API-level failures so dispatchers can convert them to user-friendly errors.
"""

from __future__ import annotations

import json
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


def call(method: str, **params: Any) -> dict[str, Any]:
    """POST to slack.com/api/<method> with form-encoded params + xoxc auth.

    Goes through the token-bucket rate limiter (`ratelimit.acquire`) before
    sending — daemon polls, MCP tool calls, and backfill scans all share the
    same per-method-class budget, so a backfill can't starve the user's
    interactive tools (and vice versa).

    Retries once on rate-limit (429 — penalises the bucket on the way back).
    Other transport errors propagate as `SlackError`.
    """
    from . import ratelimit

    env = _read_env()
    xoxc = env.get("SLACK_MCP_XOXC_TOKEN") or ""
    if not xoxc:
        raise SlackError("xoxc missing from env — run slack_refresh_tokens")
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
                payload = json.loads(resp.read())
            if not payload.get("ok"):
                raise SlackError(f"{method}: {payload.get('error', 'unknown')} — {payload}")
            return payload
        except urllib.error.HTTPError as e:
            if e.code == 429:
                retry_after = int(e.headers.get("Retry-After", "5"))
                ratelimit.penalty_429(method, retry_after_s=retry_after)
                if attempt == 0:
                    time.sleep(min(retry_after, 30))
                    continue
            raise SlackError(f"{method}: HTTP {e.code} — {e.read()[:200]!r}") from e
        except urllib.error.URLError as e:
            raise SlackError(f"{method}: transport — {e}") from e
    raise SlackError(f"{method}: rate-limited twice")


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
