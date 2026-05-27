"""Playwright persistent-context wrapper for Slack web client.

The persistent profile lives at SLACK_BRIDGE_BROWSER_PROFILE_DIR. It survives
restarts and keeps cookies, localStorage, IndexedDB, ServiceWorker registration
— everything Slack expects from a "real" client. This dramatically reduces
Cloudflare/Slack bot-detection signals compared to an ephemeral storage_state
JSON file.

The persistent context is short-lived: opened on each tool call, closed when
the call returns. Slack's session state (which is what we care about) is
persisted to disk. Keeping a long-lived browser daemon would be cheaper
per-call but requires lifecycle management (launchd, crash recovery, sleep/wake)
that we don't need for a few-times-a-week token refresh.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, suppress
from typing import TYPE_CHECKING, Any, TypeVar

from .config import app_channel_url, settings

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext

# Single worker — Playwright contexts are not meant to be parallel-safe per
# profile dir, and we never need more than one concurrent browser session.
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="slack-pw")
T = TypeVar("T")


def run_in_thread(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Run a blocking Playwright sync-API call off the asyncio event loop.

    The MCP server runs `dispatch()` inside an asyncio loop's executor, but
    `playwright.sync_api` checks for a running loop in the calling thread and
    refuses to start there. Submitting the work to our own thread pool gives
    Playwright a loop-free thread to live in.
    """
    return _executor.submit(fn, *args, **kwargs).result()


# UA only used for direct HTTP calls (auth.test) — browser launches use the
# real Chrome UA so Slack/Cloudflare see a normal client.
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


@contextmanager
def slack_context(headless: bool = True):
    """Yield a Playwright BrowserContext for the persistent Slack profile.

    Uses the system-installed Google Chrome (via Playwright's `channel="chrome"`)
    rather than Playwright's bundled Chromium. Real Chrome is what
    Slack/Cloudflare expect from a logged-in user; the headless-shell Chromium
    fails their bot checks during SAML SSO. No spoofed UA, no
    AutomationControlled overrides — those are detection signals themselves.
    """
    from playwright.sync_api import sync_playwright

    cfg = settings()
    cfg.browser_profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    with sync_playwright() as p:
        ctx: BrowserContext = p.chromium.launch_persistent_context(
            user_data_dir=str(cfg.browser_profile_dir),
            channel="chrome",
            headless=headless,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id=cfg.browser_timezone,
        )
        try:
            yield ctx
        finally:
            ctx.close()


def extract_xoxc_from_page(page) -> tuple[str | None, str | None]:
    """Read the workspace api_token from localStorage.localConfig_v2.

    The new client-v2 stores per-team tokens under
    ``localConfig_v2 → teams[<id>].token``. Returns (token, team_id) or
    (None, None) if not yet populated.
    """
    import json as _json

    raw = page.evaluate("() => localStorage.getItem('localConfig_v2')")
    if not raw:
        return None, None
    try:
        cfg = _json.loads(raw)
    except _json.JSONDecodeError:
        return None, None
    teams = cfg.get("teams", {})
    if not teams:
        return None, None
    # Pick the first team that has a token. Enterprise Grid IDs start with E,
    # legacy workspaces with T — both work.
    for tid, team in teams.items():
        tok = team.get("token")
        if isinstance(tok, str) and tok.startswith("xoxc-"):
            return tok, tid
    return None, None


def extract_d_cookie(ctx) -> str | None:
    for c in ctx.cookies():
        if c["name"] == "d" and c["domain"].endswith("slack.com"):
            return c["value"]
    return None


def extract_session_cookies(ctx) -> dict[str, str]:
    """Pull all of (d, d-s, b, x, lc) from the browser cookie jar.

    Discovered 2026-05-09: server-side `d-s` is validated by VALUE not by
    liveness. If we persist all five and send them as the Cookie header on
    every API call, we never need an active browser process — the session
    stays valid until Slack rotates them server-side (typically hours-days).
    """
    out: dict[str, str] = {}
    wanted = {"d", "d-s", "b", "x", "lc"}
    for c in ctx.cookies():
        if c["name"] in wanted and c["domain"].endswith("slack.com") and c.get("value"):
            out[c["name"]] = c["value"]
    return out


def summarize_via_ws_blocking(
    channel: str,
    thread_ts: str | None,
    timeout_s: int = 90,
) -> dict[str, Any]:
    """Open Slack web in Playwright, trigger ai.alpha.summarize.{thread|channelUnreads},
    capture the `ai_summary_completed` WS frame that delivers the result.

    The summary endpoint returns a `summary.id` immediately; the actual summary
    text is pushed via WebSocket. We monkey-patch the page's WebSocket before
    Slack's JS runs to capture every frame.
    """
    import json as _json

    from playwright.sync_api import sync_playwright

    cfg = settings()
    cfg.browser_profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    ws_hook = """
    (() => {
      window.__capturedWS = [];
      const origWS = window.WebSocket;
      window.WebSocket = function(url, protocols) {
        const ws = protocols ? new origWS(url, protocols) : new origWS(url);
        ws.addEventListener('message', (ev) => {
          try {
            const d = String(ev.data);
            if (d.indexOf('ai_summary_completed') !== -1) {
              window.__capturedWS.push({t: Date.now(), data: d});
            }
          } catch(_){}
        });
        return ws;
      };
      Object.setPrototypeOf(window.WebSocket, origWS);
      window.WebSocket.prototype = origWS.prototype;
    })();
    """

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(cfg.browser_profile_dir),
            channel="chrome",
            headless=True,
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        page.add_init_script(ws_hook)

        # Navigate to the channel — opens a WS connection
        page.goto(
            app_channel_url(channel),
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        page.wait_for_timeout(5000)  # let WS connect + page hydrate

        # Trigger the summarize call from inside the page (uses page's xoxc + WS subscription)
        # We use a regular HTTP call from outside the page for reliability —
        # but the WS that delivers the result needs the page to be subscribed,
        # which it is now since we're loaded into the channel.
        from . import client  # late import to avoid cycle

        if thread_ts:
            res = client.call("ai.alpha.summarize.thread", channel=channel, thread_ts=thread_ts)
        else:
            res = client.call("ai.alpha.summarize.channelUnreads", channel=channel)
        summary_id = (res.get("summary") or {}).get("id")
        if not summary_id:
            ctx.close()
            return {"ok": False, "error": "no summary_id returned", "raw": res}

        import time as _t

        deadline = _t.time() + timeout_s
        completed = None
        while _t.time() < deadline:
            page.wait_for_timeout(1500)
            frames = page.evaluate("() => window.__capturedWS")
            for f in frames or []:
                d = f.get("data", "")
                if summary_id in d:
                    # Parse the JSON envelope
                    try:
                        idx_open = d.index("{")
                        # find the outer JSON — Slack frames are usually a single JSON object
                        envelope = _json.loads(d[idx_open:])
                        if envelope.get("type") == "ai_summary_completed":
                            completed = envelope
                            break
                    except (_json.JSONDecodeError, ValueError):
                        # Fallback: store raw
                        completed = {"raw": d}
                        break
            if completed:
                break
        ctx.close()

    if not completed:
        return {
            "ok": False,
            "error": f"no ai_summary_completed event in {timeout_s}s",
            "summary_id": summary_id,
        }
    summary = completed.get("summary") or {}
    return {
        "ok": True,
        "summary_id": summary_id,
        "status": summary.get("ai_context_result_status"),
        "error": summary.get("error"),
        "text": (summary.get("result") or {}).get("text"),
        "topics": (summary.get("result") or {}).get("topics"),
        "blocks": (summary.get("result") or {}).get("blocks"),
    }


def assistant_post_blocking(
    channel_id: str,
    message: str,
    capture_network: bool = False,
    headless: bool = False,
) -> dict:
    """Drive the Slack web UI to post `message` into channel_id.

    Use for `assistant_app_thread` channels (Glean DM, Rovo, etc.) where
    `chat.postMessage` does NOT trigger the assistant event because Slack's
    Assistant API is locked to the official UI surface.

    Returns posting metadata + (optionally) captured Slack API calls + WS
    frames during the interaction, so we can later replay the posting via
    raw HTTP if we figure out the right protocol.
    """
    from playwright.sync_api import sync_playwright

    cfg = settings()
    cfg.browser_profile_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    network: list[dict] = []
    ws_frames: list[dict] = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(cfg.browser_profile_dir),
            channel="chrome",
            headless=headless,
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            timezone_id=cfg.browser_timezone,
        )

        if capture_network:

            def _on_req(req):
                if "slack.com" in req.url and ("api/" in req.url or "/edge/" in req.url):
                    with suppress(Exception):
                        network.append(
                            {
                                "kind": "req",
                                "method": req.method,
                                "url": req.url,
                                "post_data": (req.post_data or "")[:4000],
                            }
                        )

            def _on_resp(resp):
                if "slack.com" in resp.url and ("api/" in resp.url or "/edge/" in resp.url):
                    try:
                        body = ""
                        ct = resp.headers.get("content-type", "")
                        if "json" in ct or "text" in ct:
                            body = resp.text()[:4000]
                        network.append(
                            {
                                "kind": "resp",
                                "status": resp.status,
                                "url": resp.url,
                                "body": body,
                            }
                        )
                    except Exception:
                        pass

            def _payload_str(payload: object) -> str:
                if isinstance(payload, dict):
                    return str(payload.get("payload") or "")[:2000]
                return str(payload)[:2000]

            def _on_ws(ws):
                ws.on(
                    "framesent",
                    lambda p: ws_frames.append(
                        {"dir": "sent", "url": ws.url, "payload": _payload_str(p)}
                    ),
                )
                ws.on(
                    "framereceived",
                    lambda p: ws_frames.append(
                        {"dir": "recv", "url": ws.url, "payload": _payload_str(p)}
                    ),
                )

            ctx.on("request", _on_req)
            ctx.on("response", _on_resp)
            # NOTE: BrowserContext doesn't expose a 'websocket' event in
            # Playwright's typed API; we attach to Page instead.

        page = ctx.new_page()
        if capture_network:
            page.on("websocket", _on_ws)

        # Navigate to the channel — Slack web app uses /client/<TEAM>/<CID>
        url = app_channel_url(channel_id)
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(4000)  # let Slack hydrate

        # Locate message composer. Slack uses Lexical/Quill — try multiple
        # selectors in order of stability.
        editor_selectors = [
            'div[data-qa="message_input"][contenteditable="true"]',
            'div[contenteditable="true"][role="textbox"]',
            'div.ql-editor[contenteditable="true"]',
        ]
        editor = None
        for sel in editor_selectors:
            cand = page.locator(sel).first
            try:
                cand.wait_for(state="visible", timeout=4000)
                editor = cand
                break
            except Exception:
                continue
        if editor is None:
            ctx.close()
            return {
                "posted": False,
                "error": "could not find message composer — Slack UI may have changed",
                "network_events": len(network),
                "ws_frames": len(ws_frames),
            }

        editor.click()
        page.wait_for_timeout(300)
        # Type the message — character-by-character so Slack sees real keystrokes
        editor.type(message, delay=15)
        page.wait_for_timeout(300)
        # Submit (Enter without shift = send)
        page.keyboard.press("Enter")
        # Give Slack time to fire the actual API call before we close
        page.wait_for_timeout(3500)

        ctx.close()

    return {
        "posted": True,
        "channel_id": channel_id,
        "network_events_captured": len(network),
        "ws_frames_captured": len(ws_frames),
        "network": network if capture_network else None,
        "ws_frames": ws_frames if capture_network else None,
    }
