"""Turn Slack attachments/blocks into text, and parse alert cards.

Bot messages (JSM ChatOps / Opsgenie, Grafana, Jenkins...) often have an empty
`text` and carry everything in `attachments[].blocks` or `blocks`. Every tool
that returns a message should go through `message_text` so callers never see
an empty body for a message that has content.
"""

from __future__ import annotations

import re
from typing import Any

_LINK = re.compile(r"<([^|>]+)\|([^>]+)>")
_BARE_LINK = re.compile(r"<(https?://[^>]+)>")
_ALERT_TITLE = re.compile(r"Alert #(\d+):\s*(.+)")
_TAG = re.compile(r"`([^`:]+):([^`]*)`")
_URL = re.compile(r"https?://[^\s>|]+")


def _unlink(s: str) -> str:
    """`<url|label>` -> `label (url)`, `<url>` -> `url`."""
    s = _LINK.sub(lambda m: f"{m.group(2)} ({m.group(1)})", s)
    return _BARE_LINK.sub(r"\1", s)


def _block_lines(block: dict[str, Any]) -> list[str]:
    t = block.get("type")
    out: list[str] = []
    if t in ("section", "header", "context"):
        if isinstance(block.get("text"), dict):
            out.append(block["text"].get("text") or "")
        for f in block.get("fields") or []:
            out.append(f.get("text") or "")
        for e in block.get("elements") or []:
            if isinstance(e, dict) and e.get("text"):
                out.append(e["text"] if isinstance(e["text"], str) else e["text"].get("text", ""))
    elif t == "rich_text":
        for el in block.get("elements") or []:
            out.append("".join(x.get("text") or x.get("url") or "" for x in el.get("elements") or []))
    return [x for x in out if x and x.strip() and x.strip() != "‎"]


def _attachment_lines(a: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for b in a.get("blocks") or []:
        lines += _block_lines(b)
    if not lines:
        for k in ("pretext", "title", "text"):
            if a.get(k):
                lines.append(a[k])
        for f in a.get("fields") or []:
            lines.append(f"{f.get('title')}: {f.get('value')}")
    if not lines and a.get("fallback"):
        lines.append(a["fallback"])
    return lines


def message_text(m: dict[str, Any]) -> str:
    """Best readable body: `text`, else blocks, else attachments."""
    text = (m.get("text") or "").strip()
    if text:
        return text
    lines: list[str] = []
    for b in m.get("blocks") or []:
        lines += _block_lines(b)
    for a in m.get("attachments") or []:
        lines += _attachment_lines(a)
    return _unlink("\n".join(lines)).strip()


def _card_fields(blocks: list[dict[str, Any]]) -> dict[str, str]:
    """JSM cards lay fields out as [label, value, spacer, spacer, ...]."""
    out: dict[str, str] = {}
    for b in blocks:
        fields = [(f.get("text") or "").strip() for f in b.get("fields") or []]
        for i, f in enumerate(fields):
            m = re.search(r"\*([^*]+)\*", f)
            if m and i + 1 < len(fields):
                out[m.group(1).strip().lower()] = fields[i + 1].strip()
    return out


def parse_alert(m: dict[str, Any]) -> dict[str, Any] | None:
    """Parse a JSM ChatOps / Opsgenie alert card. None if it isn't one."""
    blocks: list[dict[str, Any]] = list(m.get("blocks") or [])
    for a in m.get("attachments") or []:
        blocks += a.get("blocks") or []
    raw = "\n".join(
        [m.get("text") or ""] + [a.get("fallback") or "" for a in m.get("attachments") or []]
        + [ln for b in blocks for ln in _block_lines(b)]
    )
    title = _ALERT_TITLE.search(_LINK.sub(r"\2", raw))
    if not title:
        return None
    url = None
    link = re.search(r"<(https?://[^|>]*opsg[^|>]*)\|", raw)
    if link:
        url = link.group(1)

    fields = _card_fields(blocks)
    tags = dict(_TAG.findall(fields.get("tags", "")))
    desc = ""
    for b in blocks:
        t = (b.get("text") or {}).get("text") or ""
        if t.startswith("*Description*"):
            desc = t[len("*Description*"):].strip()
    runbook = re.search(r"Documentation:\s*<?(https?://[^\s>|]+)", desc)
    source = re.search(r"Sources?:\s*<?(https?://[^\s>|]+)", desc)
    clean_desc = re.split(r"\n\s*Documentation:|\n\s*Sources?:", _unlink(desc))[0].strip()

    if not fields and not desc:
        # Follow-up post: "<link|Alert #N: name>" + "X acknowledged the alert".
        # text, fallback and blocks repeat the same lines; keep each once.
        lines = [ln.strip() for ln in _LINK.sub(r"\2", raw).splitlines()]
        rest = "\n".join(dict.fromkeys(ln for ln in lines if ln and "Alert #" not in ln))
        return {
            "id": int(title.group(1)),
            "alertname": title.group(2).strip().strip("*"),
            "kind": "event",
            "event": rest or None,
            "opsgenie_url": url,
        }

    return {
        "id": int(title.group(1)),
        "kind": "card",
        # Tags truncate alertname at 40 chars; the card title is the full name.
        "alertname": title.group(2).strip().strip("*"),
        "status": fields.get("status") or None,
        "responders": fields.get("responders") or None,
        "priority": tags.get("priority"),
        "severity": tags.get("severity"),
        "app": tags.get("app"),
        "app_owner": tags.get("app_owner"),
        "env": tags.get("env"),
        "team": tags.get("team"),
        "tags": tags,
        "description": clean_desc or None,
        "runbook": runbook.group(1) if runbook else None,
        "grafana_rule": source.group(1) if source else None,
        "opsgenie_url": url,
    }


_AM = re.compile(r"^\s*\[(FIRING|RESOLVED)(?::(\d+))?\]\s+([^\n]+)")
_GRAFANA_SRC = re.compile(r"src \((https://grafana\.[\w.]+/alerting/grafana/([\w-]+)/view)\)")
_DOC = re.compile(r"\(doc \((https?://[^)\s]+)\)")


def parse_alertmanager(m: dict[str, Any]) -> dict[str, Any] | None:
    """Alert posts '[FIRING:1] title ...' / '[RESOLVED] title ...'.

    Grafana-managed rules (post links `src (.../alerting/grafana/<uid>/view)`) use the whole
    first line as the rule title, colons and dashes included ('Concourse: SQL server DB
    pipelines: Failing builds in preproduction'). Prometheus rules render 'Name: summary'."""
    text = message_text(m)
    hit = _AM.match(text or "")
    if not hit:
        return None
    title = re.sub(r"\s+-\s*$", "", hit.group(3).strip())
    src = _GRAFANA_SRC.search(text)
    doc = _DOC.search(text)
    if src:
        name, summary = title, ""
    else:
        name, _, summary = title.partition(": ")
        if " " in name:  # no 'Name: summary' shape; the whole title is the name
            name, summary = title, ""
    out = {"alertname": name[:160], "summary": summary[:200], "state": hit.group(1).lower(),
           "count": int(hit.group(2) or 0), "kind": "am"}
    if src:
        out.update(grafana_rule=src.group(1), rule_uid=src.group(2))
    if doc:
        out["runbook"] = doc.group(1)
    return out
