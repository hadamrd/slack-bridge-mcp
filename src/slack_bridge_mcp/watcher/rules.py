"""YAML-driven rule matching for the Slack watcher.

Rules file lives at SLACK_BRIDGE_WATCHER_RULES_PATH (mode 600).
Reloaded on SIGHUP, and on every iteration if the file's mtime changed.

Rule schema:
    - name: human-readable
      enabled: true                  # default true; flip to disable
      match:
        channel_id: Cxxxxx           # optional, exact match
        channel_name: name           # optional; resolved via archive cache
        text_contains: "[FIRING"     # optional, case-insensitive substring
        text_regex: "..."            # optional, fullmatch
        from_user: Uxxxxx            # optional, exact
        from_bot: name               # optional, e.g. "alertmanager"
        subtype: bot_message         # optional, exact
        is_thread_reply: false       # optional bool
      ignore_self: true              # default true: skip messages from the user
      rate_limit_per_min: 6          # cap firings per rule
      stop_on_match: false           # default false; true halts further rules
      actions:
        - kind: shell
          cmd: ["/bin/echo", "{{text_first_line}}"]
        - kind: webhook
          url: http://localhost:9999/event
          body: { channel: "{{channel_id}}", text: "{{text}}" }
        - kind: post_back
          channel: "{{channel_id}}"
          thread_ts: "{{ts}}"
          text: ":eyes:"
        - kind: slack_call
          tool: slack_summarize_thread
          args: { channel: "{{channel_id}}", thread_ts: "{{ts}}" }

Substitution variables available in `cmd`/`text`/`url`/`args` strings:
    {{ts}}, {{ts_iso}}, {{date}}, {{channel_id}}, {{channel_name}},
    {{user}}, {{user_label}}, {{text}}, {{text_first_line}},
    {{thread_ts}}, {{permalink}}.
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import Any

import yaml

from ..config import settings

RULES_PATH = settings().watcher_rules_path

log = logging.getLogger("slack-watcher")


class _RuleState:
    """Per-rule runtime state — recent firings for rate limiting."""

    def __init__(self) -> None:
        self.recent_firings: list[float] = []  # epoch seconds


class RulesEngine:
    """Loads + matches rules. Reloads automatically when the file mtime changes."""

    def __init__(self) -> None:
        self.rules: list[dict[str, Any]] = []
        self.state: dict[str, _RuleState] = {}
        self._mtime: float = 0.0

    def maybe_reload(self) -> bool:
        """Returns True if rules were reloaded."""
        if not RULES_PATH.exists():
            if self.rules:
                log.info("rules file deleted; clearing rules")
                self.rules = []
                self._mtime = 0
                return True
            return False
        mtime = RULES_PATH.stat().st_mtime
        if mtime == self._mtime:
            return False
        try:
            data = yaml.safe_load(RULES_PATH.read_text()) or []
        except yaml.YAMLError as e:
            log.error("rules YAML parse error: %s", e)
            return False
        self.rules = [r for r in data if r.get("enabled", True)]
        # Initialise per-rule state for new rules
        for r in self.rules:
            n = r.get("name", "")
            if n not in self.state:
                self.state[n] = _RuleState()
        self._mtime = mtime
        log.info("rules reloaded: %d active", len(self.rules))
        return True

    def match(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        """Return a list of matching rules for the given Slack `message` event.
        Applies rate-limit + stop_on_match in order."""
        import time as _t

        now = _t.time()
        matched: list[dict[str, Any]] = []
        for rule in self.rules:
            if not _match_rule(rule, event):
                continue
            # Rate limit
            cap = int(rule.get("rate_limit_per_min", 0) or 0)
            st = self.state.get(rule["name"])
            if cap > 0 and st:
                cutoff = now - 60
                st.recent_firings = [t for t in st.recent_firings if t > cutoff]
                if len(st.recent_firings) >= cap:
                    log.warning("rule %r rate-limited (>= %d/min)", rule["name"], cap)
                    continue
                st.recent_firings.append(now)
            matched.append(rule)
            if rule.get("stop_on_match"):
                break
        return matched


def _match_rule(rule: dict[str, Any], event: dict[str, Any]) -> bool:
    m = rule.get("match", {}) or {}
    if rule.get("ignore_self", True):
        # Need user_id from somewhere — we'll inject it later in daemon when
        # we know who the running user is. For now, never ignore here.
        pass
    text = event.get("text") or ""
    if "channel_id" in m and event.get("channel") != m["channel_id"]:
        return False
    if "channel_name" in m and event.get("_channel_name") != m["channel_name"]:
        return False
    if "from_user" in m and event.get("user") != m["from_user"]:
        return False
    if "from_bot" in m and event.get("username") != m["from_bot"]:
        return False
    if "subtype" in m and event.get("subtype") != m["subtype"]:
        return False
    if "is_thread_reply" in m:
        is_reply = bool(event.get("thread_ts")) and event.get("thread_ts") != event.get("ts")
        if is_reply != bool(m["is_thread_reply"]):
            return False
    if "text_contains" in m and m["text_contains"].lower() not in text.lower():
        return False
    if "text_regex" in m:
        try:
            if not re.search(m["text_regex"], text):
                return False
        except re.error as e:
            log.error("rule %r bad regex: %s", rule.get("name"), e)
            return False
    return True


def render_template(template: Any, ctx: dict[str, Any]) -> Any:
    """Recursively substitute {{key}} placeholders. Strings get rendered;
    dicts/lists recurse."""
    if isinstance(template, str):
        out = template
        for k, v in ctx.items():
            out = out.replace("{{" + k + "}}", str(v) if v is not None else "")
        return out
    if isinstance(template, list):
        return [render_template(x, ctx) for x in template]
    if isinstance(template, dict):
        return {k: render_template(v, ctx) for k, v in template.items()}
    return template


def build_context(event: dict[str, Any]) -> dict[str, Any]:
    """Produce the substitution dict from a Slack message event."""
    ts = event.get("ts", "")
    ts_iso = ""
    date = ""
    try:
        unix = float(ts)
        dt = datetime.datetime.fromtimestamp(unix, tz=datetime.UTC)
        ts_iso = dt.isoformat()
        date = dt.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        pass
    text = event.get("text") or ""
    return {
        "ts": ts,
        "ts_iso": ts_iso,
        "date": date,
        "channel_id": event.get("channel", ""),
        "channel_name": event.get("_channel_name", ""),
        "user": event.get("user", ""),
        "user_label": event.get("_user_label", ""),
        "text": text,
        "text_first_line": text.splitlines()[0] if text else "",
        "thread_ts": event.get("thread_ts") or "",
        "permalink": event.get("_permalink", ""),
    }
