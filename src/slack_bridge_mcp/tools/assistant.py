"""Slack-Assistant bot tools (Glean, Rovo, etc.) + native AI summarization.

Cracks this module relies on (see slack-mcp-tokens.md memory doc):
- assistant_app_thread routing: Slack-Assistant bots gate by parent message
  subtype, not client flags. Plain `chat.postMessage` with the right
  thread_ts triggers their substantive handler.
- `assistant.threads.startThread` is user-callable (despite the
  `assistant.threads.start` family being bot-only). Lets us programmatically
  open fresh assistant conversations.
- `ai.alpha.summarize.{thread,channelUnreads}` are user-callable; results
  delivered async via WebSocket frames of type `ai_summary_completed`.
  We open a brief Playwright session to capture that frame.
"""

from __future__ import annotations

from typing import Any

from mcp.types import Tool

from .. import caches
from ..client import SlackError, call
from .messaging import _open_dm  # shared helper

TOOLS: list[Tool] = [
    Tool(
        name="slack_list_assistant_threads",
        description=(
            "List the existing assistant_app_thread conversations in a bot DM "
            "(e.g. Glean). Each Slack-Assistant bot keeps a discrete thread "
            "per 'conversation'. Use to discover ts values you can pass to "
            "slack_assistant_ask via thread_ts."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "user": {"type": "string", "description": "Bot user id, email, or name"},
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
            },
            "required": ["user"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_new_assistant_thread",
        description=(
            "Create a fresh assistant_app_thread in a bot DM (e.g. Glean). "
            "Use when you want to start a conversation with clean context — "
            "the bot will treat it as a brand-new chat with no memory of "
            "prior threads. Returns the new thread_ts. Uses Slack's "
            "user-callable `assistant.threads.startThread` endpoint."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "user": {"type": "string", "description": "Bot user id, email, or name"},
            },
            "required": ["user"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_summarize_thread",
        description=(
            "Summarize a Slack thread using Slack's own AI (the model that "
            "powers Slack AI Recap). Triggers `ai.alpha.summarize.thread`, "
            "captures the WS-delivered result. Returns plain `text` + rich "
            "`blocks` (with citations linking to specific messages) + topic "
            "summary. Free, native, no extra LLM cost. Usually 5-30s. "
            "Note: opens a brief headless Chrome to listen for the WS frame "
            "(no other way to receive the async result)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel id (Cxxx/Dxxx)"},
                "thread_ts": {"type": "string", "description": "Parent message ts"},
                "timeout_s": {"type": "integer", "default": 90, "minimum": 10, "maximum": 300},
            },
            "required": ["channel", "thread_ts"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_summarize_channel_unreads",
        description=(
            "Summarize unread messages in a channel using Slack's own AI. "
            "Same backend as Slack AI Recap. Returns null/error if the "
            "channel has no unread messages. Triggers "
            "`ai.alpha.summarize.channelUnreads` + WS frame capture."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel id"},
                "timeout_s": {"type": "integer", "default": 90, "minimum": 10, "maximum": 300},
            },
            "required": ["channel"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_assistant_ask",
        description=(
            "Ask a Slack Assistant-API bot (e.g. Glean) a question — "
            "native HTTP, no browser. Finds the user's existing "
            "assistant_app_thread in the bot DM, posts the question as a "
            "thread reply, and polls for the bot's substantive answer. "
            "Glean can take 30-300s for complex multi-source queries; tune "
            "wait_s accordingly. If the bot's reply is truncated by Slack's "
            "40k-char message limit (Glean appends a marker), the tool "
            "auto-sends a 'continue' prompt and concatenates pages "
            "(disable with auto_continue=false). Returns the merged answer "
            "and a `pages` count. Legacy `capture_network` and "
            "`use_playwright` flags are kept for protocol research only."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "user": {
                    "type": "string",
                    "description": "Bot user id, email, or name (e.g. 'Glean')",
                },
                "question": {"type": "string"},
                "thread_ts": {
                    "type": "string",
                    "description": (
                        "Optional explicit assistant_app_thread ts to post into. "
                        "If omitted, auto-targets the most recent assistant_app_thread "
                        "in the bot DM. To start a fresh conversation, click '+' in "
                        "Slack desktop's bot DM — auto-find will pick up the new thread."
                    ),
                },
                "wait_s": {"type": "integer", "default": 240, "minimum": 10, "maximum": 600},
                "auto_continue": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "On detecting a truncation marker in the bot's reply, "
                        "automatically send 'continue from where you left off' "
                        "and concatenate pages."
                    ),
                },
                "max_pages": {
                    "type": "integer",
                    "default": 3,
                    "minimum": 1,
                    "maximum": 10,
                    "description": "Cap on continuation rounds. Each adds wait_s of latency.",
                },
                "capture_network": {"type": "boolean", "default": False},
                "use_playwright": {
                    "type": "boolean",
                    "default": False,
                    "description": "Force Playwright UI driving (legacy/research path).",
                },
            },
            "required": ["user", "question"],
            "additionalProperties": False,
        },
    ),
]

# Markers Glean (and similar bots) append when they had to truncate their
# own composed answer because of Slack's 40k-char / 50-block message limits.
_TRUNCATION_MARKERS = (
    "(truncated due to slack message length",
    "[truncated]",
    "…(truncated)",
    "(message truncated)",
)


def _is_truncated(text: str | None) -> bool:
    if not text:
        return False
    low = text.lower()
    return any(m.lower() in low for m in _TRUNCATION_MARKERS)


def _find_assistant_thread(channel_id: str) -> str | None:
    """Find the most-recent existing `assistant_app_thread` parent in this DM."""
    hist = call("conversations.history", channel=channel_id, limit=50)
    for m in hist.get("messages") or []:
        if m.get("subtype") == "assistant_app_thread" and m.get("thread_ts") == m.get("ts"):
            return m["ts"]
    return None


def _list_assistant_threads(channel_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """All assistant threads in a bot DM, newest first."""
    hist = call("conversations.history", channel=channel_id, limit=200)
    out = []
    for m in hist.get("messages") or []:
        if m.get("subtype") == "assistant_app_thread" and m.get("thread_ts") == m.get("ts"):
            replies = call("conversations.replies", channel=channel_id, ts=m["ts"], limit=2)
            first_human = next(
                (
                    r
                    for r in (replies.get("messages") or [])
                    if r["ts"] != m["ts"] and r.get("user") and not r.get("bot_id")
                ),
                None,
            )
            preview = (first_human.get("text") if first_human else "")[:150]
            out.append(
                {
                    "ts": m["ts"],
                    "reply_count": m.get("reply_count", 0),
                    "first_question_preview": preview,
                }
            )
            if len(out) >= limit:
                break
    return out


def _new_assistant_thread(channel_id: str, bot_uid: str) -> dict[str, Any]:
    """Create a fresh assistant_app_thread via `assistant.threads.startThread`."""
    r = call(
        "assistant.threads.startThread",
        channel_id=channel_id,
        bot_user_id=bot_uid,
        source="app-dm",
        reason="new-thread",
    )
    return {
        "thread_ts": r.get("thread_ts"),
        "channel_id": r.get("channel_id"),
        "message": r.get("message", {}),
    }


def _wait_for_bot_reply(
    cid: str, thread_ts: str, after_ts: str, bot_uid: str, wait_s: int
) -> tuple[dict | None, int]:
    """Poll conversations.replies for a new bot message strictly after `after_ts`."""
    import time as _t

    deadline = _t.time() + wait_s
    elapsed = 0
    while _t.time() < deadline:
        _t.sleep(5)
        elapsed += 5
        replies = call("conversations.replies", channel=cid, ts=thread_ts, limit=50)
        for m in replies.get("messages") or []:
            if m.get("ts", "") <= after_ts:
                continue
            sender = m.get("user") or m.get("bot_id")
            if sender == bot_uid or m.get("bot_id"):
                return m, elapsed
    return None, elapsed


def _resolve_bot_uid(user: str) -> str:
    """Resolve a bot reference (id / email / name) to a U-id."""
    if user.startswith("U") and user.isalnum() and len(user) >= 9:
        return user
    if "@" in user:
        return call("users.lookupByEmail", email=user)["user"]["id"]
    candidates = caches.find_users(user, limit=3)
    if not candidates:
        raise SlackError(f"user {user!r} not in cache; try slack_find_user first")
    return candidates[0]["id"]


def _assistant_ask(
    user: str,
    question: str,
    wait_s: int,
    capture_network: bool,
    use_playwright: bool,
    auto_continue: bool = True,
    max_pages: int = 3,
    thread_ts: str | None = None,
) -> dict[str, Any]:
    """Post a question to a Slack Assistant bot and poll for its reply."""
    import time as _t

    bot_uid = _resolve_bot_uid(user)
    cid = _open_dm(bot_uid)

    # Native HTTP path (default)
    if not use_playwright:
        if not thread_ts:
            thread_ts = _find_assistant_thread(cid)
        if not thread_ts:
            try:
                created = _new_assistant_thread(cid, bot_uid)
                thread_ts = created["thread_ts"]
            except SlackError as e:
                return {"ok": False, "error": f"no assistant thread + auto-create failed: {e}"}

        posted = call("chat.postMessage", channel=cid, thread_ts=thread_ts, text=question)
        post_ts = posted["ts"]

        bot_msg, elapsed_s = _wait_for_bot_reply(cid, thread_ts, post_ts, bot_uid, wait_s)
        if not bot_msg:
            return {
                "ok": True,
                "via": "native_http",
                "channel_id": cid,
                "bot_id": bot_uid,
                "thread_ts": thread_ts,
                "posted_ts": post_ts,
                "elapsed_s": elapsed_s,
                "reply_received": False,
                "reply": None,
            }

        pages = [bot_msg]
        last_ts = bot_msg["ts"]
        truncated = _is_truncated(bot_msg.get("text"))
        continuations = 0
        if auto_continue and truncated:
            for _ in range(max_pages - 1):
                follow_post = call(
                    "chat.postMessage",
                    channel=cid,
                    thread_ts=thread_ts,
                    text=(
                        "Continue from where you left off — pick up exactly "
                        "where the previous message ended, do not repeat."
                    ),
                )
                cont_msg, cont_elapsed = _wait_for_bot_reply(
                    cid, thread_ts, follow_post["ts"], bot_uid, wait_s
                )
                elapsed_s += cont_elapsed
                continuations += 1
                if not cont_msg:
                    break
                pages.append(cont_msg)
                last_ts = cont_msg["ts"]
                if not _is_truncated(cont_msg.get("text")):
                    break

        merged_text = "\n\n--- (continued) ---\n\n".join((p.get("text") or "") for p in pages)
        total_blocks = sum(len(p.get("blocks") or []) for p in pages)

        return {
            "ok": True,
            "via": "native_http",
            "channel_id": cid,
            "bot_id": bot_uid,
            "thread_ts": thread_ts,
            "posted_ts": post_ts,
            "elapsed_s": elapsed_s,
            "reply_received": True,
            "pages": len(pages),
            "continuations": continuations,
            "was_truncated": truncated,
            "still_truncated_after_max_pages": _is_truncated((pages[-1] or {}).get("text")),
            "reply": {
                "ts": last_ts,
                "text": merged_text,
                "blocks_count": total_blocks,
            },
        }

    # Legacy/research path: drive Slack web UI via Playwright
    from ..browser import assistant_post_blocking, run_in_thread

    pre = call("conversations.history", channel=cid, limit=1).get("messages") or []
    pre_ts = pre[0]["ts"] if pre else "0"
    post_result = run_in_thread(assistant_post_blocking, cid, question, capture_network, False)
    if not post_result.get("posted"):
        return {
            "ok": False,
            "error": post_result.get("error", "post failed"),
            "post_result": post_result,
        }

    deadline = _t.time() + wait_s
    bot_msg = None
    while _t.time() < deadline:
        _t.sleep(5)
        hist = call("conversations.history", channel=cid, limit=10)
        for m in hist.get("messages") or []:
            ts = m.get("ts")
            if not ts or ts <= pre_ts:
                continue
            sender = m.get("user") or m.get("bot_id")
            if sender == bot_uid or m.get("subtype") == "assistant_app_thread":
                bot_msg = m
                break
        if bot_msg:
            break
    return {
        "ok": True,
        "via": "playwright",
        "channel_id": cid,
        "bot_id": bot_uid,
        "reply_received": bot_msg is not None,
        "reply": (
            {
                "ts": bot_msg.get("ts") if bot_msg else None,
                "text": bot_msg.get("text") if bot_msg else None,
                "blocks_count": len(bot_msg.get("blocks") or []) if bot_msg else 0,
            }
            if bot_msg
            else None
        ),
        "post_result": post_result,
    }


def _summarize(channel: str, thread_ts: str | None, timeout_s: int) -> dict[str, Any]:
    """Run Slack AI summarize for a thread or channel-unreads, return the result."""
    from ..browser import run_in_thread, summarize_via_ws_blocking

    return run_in_thread(summarize_via_ws_blocking, channel, thread_ts, timeout_s)


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    try:
        if name == "slack_summarize_thread":
            return _summarize(args["channel"], args["thread_ts"], int(args.get("timeout_s", 90)))
        if name == "slack_summarize_channel_unreads":
            return _summarize(args["channel"], None, int(args.get("timeout_s", 90)))
        if name == "slack_assistant_ask":
            return _assistant_ask(
                args["user"],
                args["question"],
                int(args.get("wait_s", 240)),
                bool(args.get("capture_network", False)),
                bool(args.get("use_playwright", False)),
                bool(args.get("auto_continue", True)),
                int(args.get("max_pages", 3)),
                args.get("thread_ts"),
            )
        if name == "slack_list_assistant_threads":
            uid = _resolve_bot_uid(args["user"])
            cid = _open_dm(uid)
            return {
                "ok": True,
                "channel_id": cid,
                "threads": _list_assistant_threads(cid, int(args.get("limit", 20))),
            }
        if name == "slack_new_assistant_thread":
            uid = _resolve_bot_uid(args["user"])
            cid = _open_dm(uid)
            res = _new_assistant_thread(cid, uid)
            return {"ok": True, "channel_id": cid, "bot_id": uid, **res}
    except SlackError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return None
