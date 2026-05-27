"""Read-side MCP tools over the local Slack archive.

The archive daemon writes; these tools only read. Safe to call any time —
falls back gracefully when daemon hasn't run yet (empty result + hint).
"""

from __future__ import annotations

import time
from datetime import UTC
from typing import Any

from mcp.types import Tool

from ..archive import cold, db
from ..client import SlackError, call
from ..config import permalink

TOOLS: list[Tool] = [
    Tool(
        name="slack_archive_compact",
        description=(
            "Run the hot→cold compaction now. Moves messages older than "
            "horizon_days from SQLite to Parquet+zstd cold tier and VACUUMs. "
            "Idempotent — safe to run repeatedly. Use `dry_run` to preview "
            "without writing. Normally runs nightly via launchd; this tool "
            "is for on-demand compaction or testing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "horizon_days": {"type": "integer", "default": 90, "minimum": 1, "maximum": 3650},
                "dry_run": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_ratelimit_status",
        description=(
            "Inspect the in-process Slack rate-limit buckets. Returns "
            "per-class current tokens, refill rate (current vs base), recent "
            "429s, and call counters. Use to diagnose 'why is the daemon "
            "slow?' / 'are we getting throttled by Slack?'"
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="slack_archive_status",
        description=(
            "Health snapshot of the local Slack archive: db size, "
            "channels known/polled, total messages, last write time."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="slack_archive_search",
        description=(
            "Full-text search the local archive (FTS5/porter stemming). "
            "Supports SQLite FTS5 syntax: 'gerrit replication', "
            "'\"exact phrase\"', 'replic* NEAR/3 fail*'. Use channel/user/since "
            "to narrow. Returns msgs with permalinks (when known)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "channel": {
                    "type": "string",
                    "description": "Channel id or '#name' (resolves via search.messages)",
                },
                "user": {"type": "string", "description": "User id (Uxxx) — exact match on sender"},
                "since": {"type": "string", "description": "ISO date (YYYY-MM-DD) or unix ts"},
                "until": {"type": "string", "description": "ISO date (YYYY-MM-DD) or unix ts"},
                "limit": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_archive_thread",
        description=(
            "Reconstruct a thread from the archive (no API call). Pass "
            "channel id + parent ts. Returns parent + replies in order. "
            "If thread spans the archive horizon, falls back to API."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {"type": "string"},
                "thread_ts": {"type": "string"},
            },
            "required": ["channel", "thread_ts"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_archive_extract_chunks",
        description=(
            "Slice the archive into time/topic chunks pre-digested for "
            "summarization. For each chunk returns counts, participants, "
            "salient messages (length-ranked, dedup'd), keyword density, "
            "and timestamps. Use group_by=week|day|month for time slices, "
            "or group_by=thread for thread-level slices. Optional FTS "
            "filter via `match`. ~5x cheaper than reading raw history when "
            "you want a multi-period synthesis."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel id (Cxxx/Dxxx) or '#name'"},
                "group_by": {
                    "type": "string",
                    "enum": ["week", "day", "month", "thread"],
                    "default": "week",
                },
                "match": {
                    "type": "string",
                    "description": "Optional FTS query (e.g. 'gerrit replication')",
                },
                "since": {"type": "string", "description": "ISO date or unix ts"},
                "until": {"type": "string", "description": "ISO date or unix ts"},
                "salient_count": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
                "keyword_count": {"type": "integer", "default": 10, "minimum": 1, "maximum": 50},
                "max_chunks": {"type": "integer", "default": 60, "minimum": 1, "maximum": 200},
            },
            "required": ["channel"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="slack_archive_backfill",
        description=(
            "One-shot: walk a channel's history backwards via the API and "
            "fill the archive. Use to seed channels the daemon hasn't been "
            "polling yet. Rate-limit-bound — large channels can take minutes."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Channel id or '#name'"},
                "days_back": {"type": "integer", "default": 30, "minimum": 1, "maximum": 365},
                "max_pages": {"type": "integer", "default": 50, "minimum": 1, "maximum": 500},
            },
            "required": ["channel"],
            "additionalProperties": False,
        },
    ),
]


def _parse_when(s: str | None) -> str | None:
    """Accepts ISO date 'YYYY-MM-DD', unix ts, or Slack ts. Returns Slack ts."""
    if not s:
        return None
    s = s.strip()
    if "-" in s and "T" not in s and len(s) >= 10:  # YYYY-MM-DD
        from datetime import datetime

        dt = datetime.strptime(s[:10], "%Y-%m-%d").replace(tzinfo=UTC)
        return f"{int(dt.timestamp())}.000000"
    if "." not in s and s.isdigit():  # unix seconds
        return f"{s}.000000"
    return s  # already a Slack ts


def _resolve_channel_id(channel: str) -> str:
    """Channel can be id (Cxxx/Gxxx/Dxxx) or '#name'. Hits search.messages
    when name needs resolution."""
    name = channel.lstrip("#")
    if (
        name.startswith(("C", "G", "D"))
        and name.isalnum()
        and name == name.upper()
        and len(name) >= 9
    ):
        return name
    data = call("search.messages", query=f"in:{name}", count=1)
    matches = (data.get("messages") or {}).get("matches") or []
    for m in matches:
        ch = m.get("channel") or {}
        if ch.get("name") == name:
            return ch["id"]
    raise SlackError(f"channel '{channel}' not found via search.messages")


def _build_permalink(channel_id: str, ts: str) -> str:
    """Build a permalink using the configured Slack web base URL."""
    return permalink(channel_id, ts)


def _status() -> dict[str, Any]:
    conn = db.open_db()
    db.init_schema(conn)
    hot = db.stats(conn)
    hot["last_recorded_ago_s"] = (
        (int(time.time()) - hot["last_recorded_at"]) if hot["last_recorded_at"] else None
    )
    return {"ok": True, "hot": hot, "cold": cold.stats()}


def _search(
    query: str,
    channel: str | None,
    user: str | None,
    since: str | None,
    until: str | None,
    limit: int,
) -> dict[str, Any]:
    """FTS5 over hot tier; if `since` predates the hot horizon (or is unset
    and we have cold data), also scan the cold Parquet tier with a LIKE
    fallback (cold tier doesn't carry an FTS index — by-design tradeoff)."""
    conn = db.open_db()
    db.init_schema(conn)
    cid = _resolve_channel_id(channel) if channel else None
    since_ts = _parse_when(since)
    until_ts = _parse_when(until)

    # --- HOT TIER ---
    sql = [
        "SELECT m.msg_id, m.channel_id, m.ts, m.user, m.user_label, m.text,",
        "       m.thread_ts, m.subtype",
        "FROM messages_fts f JOIN messages m ON m.msg_id = f.rowid",
        "WHERE messages_fts MATCH ?",
        "  AND m.superseded_by IS NULL AND m.deleted_at IS NULL",
    ]
    params: list[Any] = [query]
    if cid:
        sql.append("AND m.channel_id = ?")
        params.append(cid)
    if user:
        sql.append("AND m.user = ?")
        params.append(user)
    if since_ts:
        sql.append("AND m.ts >= ?")
        params.append(since_ts)
    if until_ts:
        sql.append("AND m.ts <= ?")
        params.append(until_ts)
    sql.append("ORDER BY m.ts DESC LIMIT ?")
    params.append(limit)
    hot_rows = [dict(r) for r in conn.execute(" ".join(sql), params).fetchall()]

    # --- COLD TIER (only if needed) ---
    cold_rows: list[dict[str, Any]] = []
    cold_oldest = cold.horizon_oldest_ts()
    last_ts: str | None = None
    if cid:
        last_ts = db.get_channel_last_ts(conn, cid)
    needs_cold = (
        cold_oldest is not None
        and len(hot_rows) < limit
        and (since_ts is None or since_ts <= (last_ts or "0"))
    )
    if needs_cold:
        # Cold has no FTS — use plain LIKE on `text`. Slower but bounded by
        # partition pruning on channel/since/until.
        like = "%" + query.replace("'", "''").replace("%", "\\%") + "%"
        where = "lower(text) LIKE lower(?)"
        cold_params: list[Any] = [like]
        if cid:
            where += " AND channel_id = ?"
            cold_params.append(cid)
        if user:
            where += " AND user = ?"
            cold_params.append(user)
        if since_ts:
            where += " AND ts >= ?"
            cold_params.append(since_ts)
        if until_ts:
            where += " AND ts <= ?"
            cold_params.append(until_ts)
        # Always exclude tombstoned cold rows from search results.
        where += " AND deleted_at IS NULL"
        cold_rows = cold.query_via_duckdb(
            where=where,
            params=tuple(cold_params),
            columns="msg_id, channel_id, ts, user, user_label, text, thread_ts, subtype",
            order_by="ts DESC",
            limit=limit - len(hot_rows),
        )

    # Merge, sort, cap
    merged = hot_rows + cold_rows
    merged.sort(key=lambda r: r["ts"], reverse=True)
    merged = merged[:limit]

    return {
        "ok": True,
        "count": len(merged),
        "hot_count": len(hot_rows),
        "cold_count": len(cold_rows),
        "matches": [
            {
                "channel_id": r["channel_id"],
                "ts": r["ts"],
                "user": r["user"],
                "user_label": r["user_label"],
                "text": r["text"],
                "thread_ts": r["thread_ts"],
                "subtype": r["subtype"],
                "permalink": _build_permalink(r["channel_id"], r["ts"]),
            }
            for r in merged
        ],
    }


def _thread(channel: str, thread_ts: str) -> dict[str, Any]:
    conn = db.open_db()
    db.init_schema(conn)
    cid = _resolve_channel_id(channel)
    # Hot tier
    hot_rows = [
        dict(r)
        for r in conn.execute(
            """SELECT msg_id, ts, user, user_label, text, thread_ts, subtype
           FROM messages
           WHERE channel_id = ? AND (ts = ? OR thread_ts = ?)
             AND superseded_by IS NULL AND deleted_at IS NULL
           ORDER BY ts ASC""",
            (cid, thread_ts, thread_ts),
        ).fetchall()
    ]
    # Cold tier (only if hot didn't fully cover the thread; cheap to also ask)
    cold_rows: list[dict[str, Any]] = []
    if cold.horizon_oldest_ts() is not None:
        cold_rows = cold.query_via_duckdb(
            where=("channel_id = ? AND (ts = ? OR thread_ts = ?) AND deleted_at IS NULL"),
            params=(cid, thread_ts, thread_ts),
            columns="msg_id, ts, user, user_label, text, thread_ts, subtype",
            order_by="ts ASC",
        )
    merged = {r["ts"]: r for r in hot_rows}
    for r in cold_rows:
        merged.setdefault(r["ts"], r)  # hot wins on conflict
    if merged:
        return {
            "ok": True,
            "source": "archive",
            "channel_id": cid,
            "thread_ts": thread_ts,
            "count": len(merged),
            "hot_count": len(hot_rows),
            "cold_count": len(cold_rows),
            "messages": sorted(merged.values(), key=lambda r: r["ts"]),
        }
    # Fallback: archive miss → hit the API
    data = call("conversations.replies", channel=cid, ts=thread_ts, limit=200)
    msgs = data.get("messages") or []
    return {
        "ok": True,
        "source": "api",
        "channel_id": cid,
        "thread_ts": thread_ts,
        "count": len(msgs),
        "messages": [
            {
                "ts": m.get("ts"),
                "user": m.get("user") or m.get("bot_id") or m.get("username"),
                "user_label": None,
                "text": m.get("text"),
                "thread_ts": m.get("thread_ts"),
                "subtype": m.get("subtype"),
            }
            for m in msgs
        ],
    }


_STOPWORDS = frozenset(
    [
        "a",
        "about",
        "after",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "been",
        "being",
        "but",
        "by",
        "came",
        "can",
        "come",
        "could",
        "do",
        "does",
        "doing",
        "done",
        "don",
        "dont",
        "down",
        "each",
        "else",
        "end",
        "ever",
        "every",
        "for",
        "from",
        "get",
        "got",
        "had",
        "has",
        "have",
        "having",
        "he",
        "hello",
        "her",
        "here",
        "him",
        "his",
        "how",
        "however",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "just",
        "le",
        "la",
        "les",
        "un",
        "une",
        "me",
        "my",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "one",
        "only",
        "or",
        "other",
        "our",
        "out",
        "over",
        "per",
        "que",
        "quoi",
        "qui",
        "quand",
        "quel",
        "re",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "tu",
        "to",
        "too",
        "under",
        "until",
        "us",
        "very",
        "was",
        "way",
        "we",
        "well",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "why",
        "will",
        "with",
        "would",
        "yes",
        "you",
        "your",
        "yo",
        "yep",
        "yep",
        "ouep",
        "ofc",
        "oui",
        "non",
        "c'est",
        "ca",
        "ça",
        "donc",
        "faire",
        "fait",
        "fais",
        "fis",
        "cest",
        "pas",
        "mais",
        "bon",
        "ok",
        "oki",
        "okay",
        "merci",
        "hello",
        "hi",
        "hey",
        "there",
        "alors",
        "aussi",
        "avant",
        "apres",
        "après",
        "autre",
        "comme",
        "depuis",
        "deja",
        "vraiment",
        "voire",
        "pourrait",
        "peut",
        "être",
        "etre",
        "j'ai",
        "j'aii",
        "javais",
        "jétais",
        "letre",
        "c",
        "quand",
        "voila",
        "tout",
        "tous",
        "tres",
        "tt",
        "cb",
        "ya",
        "yaaa",
        "yapas",
        "pour",
        "est",
        "dans",
        "sur",
        "avec",
        "des",
        "les",
        "son",
        "ses",
        "cette",
        "ces",
        "aux",
        "chez",
        "sans",
        "sous",
        "entre",
        "vers",
        "plus",
        "moins",
        "très",
        "bien",
        "beaucoup",
        "peu",
        "trop",
        "encore",
        "deja",
        "toujours",
        "jamais",
        "souvent",
        "rarement",
        "vois",
        "voir",
        "vu",
        "vue",
        "vues",
        "savoir",
        "sais",
        "sait",
        "savait",
        "suis",
        "sommes",
        "etes",
        "était",
        "étaient",
        "seront",
        "avoir",
        "aurai",
        "aurais",
        "aura",
        "aurait",
        "avaient",
        "ayant",
        "été",
        "ete",
        "par",
        "ne",
        "nous",
        "vous",
        "ils",
        "elles",
        "il",
        "elle",
        "eux",
        "leur",
        "leurs",
        "notre",
        "nos",
        "votre",
        "vos",
        "mon",
        "ma",
        "mes",
        "ton",
        "ta",
        "tes",
    ]
)
_TOKEN_RE = __import__("re").compile(r"[A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9_-]{2,}")


def _bucket_key(ts: str, group_by: str) -> tuple[str, str] | None:
    """Return (bucket_id, human_label) for a Slack ts string."""
    import datetime as _dt

    try:
        unix = float(ts)
    except (ValueError, TypeError):
        return None
    dt = _dt.datetime.fromtimestamp(unix, tz=_dt.UTC)
    if group_by == "day":
        return dt.strftime("%Y-%m-%d"), dt.strftime("%a %d %b %Y")
    if group_by == "month":
        return dt.strftime("%Y-%m"), dt.strftime("%B %Y")
    iso = dt.isocalendar()
    week_start = dt - _dt.timedelta(days=iso.weekday - 1)
    return f"{iso.year}-W{iso.week:02d}", week_start.strftime("%d %b %Y")


def _keyword_density(texts: list[str], top_n: int) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for t in texts:
        for tok in _TOKEN_RE.findall(t.lower()):
            if tok in _STOPWORDS or tok.isdigit() or len(tok) < 3:
                continue
            counts[tok] = counts.get(tok, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top_n]
    return [{"term": k, "count": v} for k, v in ranked]


def _salient_messages(rows: list[Any], top_n: int) -> list[dict[str, Any]]:
    """Top-N longest distinct messages, oldest-first within ties."""
    seen: set[str] = set()
    candidates: list[tuple[int, dict[str, Any]]] = []
    for r in rows:
        text = (r["text"] or "").strip()
        if not text:
            continue
        # Collapse repeated bot messages: dedup by first 80 chars
        key = text[:80]
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            (
                len(text),
                {
                    "ts": r["ts"],
                    "user_label": r["user_label"] or r["user"],
                    "text": text[:600] + ("…" if len(text) > 600 else ""),
                },
            )
        )
    candidates.sort(key=lambda kv: -kv[0])
    return [m for _, m in candidates[:top_n]]


def _extract_chunks(
    channel: str,
    group_by: str,
    match: str | None,
    since: str | None,
    until: str | None,
    salient_count: int,
    keyword_count: int,
    max_chunks: int,
) -> dict[str, Any]:
    conn = db.open_db()
    db.init_schema(conn)
    cid = _resolve_channel_id(channel)

    # Pull raw rows with optional FTS filter + time bounds — HOT first.
    sql = ["SELECT m.ts, m.user, m.user_label, m.text, m.thread_ts, m.subtype FROM messages m"]
    params: list[Any] = []
    if match:
        sql.append("JOIN messages_fts f ON m.msg_id = f.rowid AND messages_fts MATCH ?")
        params.append(match)
    sql.append("WHERE m.channel_id = ? AND m.superseded_by IS NULL AND m.deleted_at IS NULL")
    params.append(cid)
    since_ts = _parse_when(since)
    until_ts = _parse_when(until)
    if since_ts:
        sql.append("AND m.ts >= ?")
        params.append(since_ts)
    if until_ts:
        sql.append("AND m.ts <= ?")
        params.append(until_ts)
    sql.append("ORDER BY m.ts ASC")
    rows = [dict(r) for r in conn.execute(" ".join(sql), params).fetchall()]

    # COLD tier: also pull rows older than hot-min if available.
    if cold.horizon_oldest_ts() is not None:
        where = "channel_id = ? AND deleted_at IS NULL"
        cold_params: list[Any] = [cid]
        if match:
            # Cold tier has no FTS — fall back to LIKE on each token.
            terms = [
                t for t in match.split() if t and t.upper() not in ("AND", "OR", "NEAR", "NOT")
            ]
            for t in terms:
                where += " AND lower(text) LIKE lower(?)"
                cold_params.append(f"%{t}%")
        if since_ts:
            where += " AND ts >= ?"
            cold_params.append(since_ts)
        if until_ts:
            where += " AND ts <= ?"
            cold_params.append(until_ts)
        cold_rows = cold.query_via_duckdb(
            where=where,
            params=tuple(cold_params),
            columns="ts, user, user_label, text, thread_ts, subtype",
            order_by="ts ASC",
        )
        rows = cold_rows + rows  # cold is older → prepend
        # Dedup defensively (a row in both tiers wins to hot)
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for r in rows:
            if r["ts"] in seen:
                continue
            seen.add(r["ts"])
            deduped.append(r)
        rows = deduped
    if not rows:
        return {
            "ok": True,
            "channel_id": cid,
            "group_by": group_by,
            "chunks": [],
            "note": "no messages match",
        }

    # Group
    if group_by == "thread":
        grouped: dict[str, list[Any]] = {}
        for r in rows:
            key = r["thread_ts"] or r["ts"]
            grouped.setdefault(key, []).append(r)
        ordered_keys = sorted(grouped.keys(), key=lambda k: float(k))
    else:
        grouped = {}
        labels: dict[str, str] = {}
        for r in rows:
            bk = _bucket_key(r["ts"], group_by)
            if not bk:
                continue
            key, label = bk
            grouped.setdefault(key, []).append(r)
            labels.setdefault(key, label)
        ordered_keys = sorted(grouped.keys())

    # Build per-chunk summary
    chunks: list[dict[str, Any]] = []
    for key in ordered_keys[-max_chunks:]:  # most recent N when over cap
        msgs = grouped[key]
        senders: dict[str, int] = {}
        for r in msgs:
            label = r["user_label"] or r["user"] or "?"
            senders[label] = senders.get(label, 0) + 1
        threads = {(r["thread_ts"] or r["ts"]) for r in msgs}
        texts = [r["text"] or "" for r in msgs]
        chunk = {
            "chunk_id": key,
            "label": labels.get(key) if group_by != "thread" else f"thread {key}",
            "earliest_ts": msgs[0]["ts"],
            "latest_ts": msgs[-1]["ts"],
            "permalink_first": _build_permalink(cid, msgs[0]["ts"]),
            "total_messages": len(msgs),
            "by_sender": dict(sorted(senders.items(), key=lambda kv: -kv[1])),
            "distinct_threads": len(threads),
            "salient_messages": _salient_messages(msgs, salient_count),
            "keyword_density": _keyword_density(texts, keyword_count),
        }
        chunks.append(chunk)
    return {
        "ok": True,
        "channel_id": cid,
        "group_by": group_by,
        "chunk_count": len(chunks),
        "total_messages": sum(c["total_messages"] for c in chunks),
        "match": match,
        "chunks": chunks,
    }


def _backfill(channel: str, days_back: int, max_pages: int) -> dict[str, Any]:
    """Walks conversations.history backwards over `days_back` and ingests every
    message + thread reply into the archive. Use to seed channels the daemon
    started polling forward-only on (the WS feed and 5min sweep can't recover
    history that predates the first checkpoint)."""
    from ..archive import ingest as archive_ingest

    conn = db.open_db()
    db.init_schema(conn)
    cid = _resolve_channel_id(channel)
    db.ensure_channel(
        conn,
        cid,
        channel.lstrip("#") if channel.startswith("#") else None,
        is_im=cid.startswith("D"),
    )

    oldest = f"{int(time.time()) - days_back * 86400}.000000"

    existing_oldest = conn.execute(
        "SELECT MIN(ts) FROM messages WHERE channel_id=?", (cid,)
    ).fetchone()[0]
    if existing_oldest and existing_oldest <= oldest:
        existing_count = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE channel_id=?", (cid,)
        ).fetchone()[0]
        return {
            "ok": True,
            "channel_id": cid,
            "days_back": days_back,
            "skipped": True,
            "reason": "horizon already covered",
            "cached_messages": existing_count,
            "cached_oldest": existing_oldest,
        }

    cursor = ""
    pages = 0
    new = 0
    while pages < max_pages:
        params = {"channel": cid, "oldest": oldest, "limit": 200}
        if cursor:
            params["cursor"] = cursor
        data = call("conversations.history", **params)
        for m in data.get("messages") or []:
            event = dict(m)
            event["channel"] = cid
            if archive_ingest.ingest_event(conn, event, via="backfill") == "inserted":
                new += 1
            # Walk thread replies for each parent — backfill is the only place
            # we still do this, because the WS feed delivers replies live and
            # the sweeper relies on them already being in the archive.
            if m.get("reply_count") and m.get("thread_ts") == m["ts"]:
                rdata = call("conversations.replies", channel=cid, ts=m["ts"], limit=1000)
                for r in rdata.get("messages") or []:
                    if r["ts"] == m["ts"]:
                        continue
                    revent = dict(r)
                    revent["channel"] = cid
                    if archive_ingest.ingest_event(conn, revent, via="backfill") == "inserted":
                        new += 1
        if not data.get("has_more"):
            break
        cursor = (data.get("response_metadata") or {}).get("next_cursor") or ""
        if not cursor:
            break
        pages += 1
    return {
        "ok": True,
        "channel_id": cid,
        "days_back": days_back,
        "pages": pages + 1,
        "new_messages": new,
    }


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    try:
        if name == "slack_ratelimit_status":
            from .. import ratelimit

            return {"ok": True, "buckets": ratelimit.status_all()}
        if name == "slack_archive_compact":
            from ..archive import compact

            return compact.compact(
                horizon_days=int(args.get("horizon_days", 90)),
                dry_run=bool(args.get("dry_run", False)),
            )
        if name == "slack_archive_status":
            return _status()
        if name == "slack_archive_search":
            return _search(
                args["query"],
                args.get("channel"),
                args.get("user"),
                args.get("since"),
                args.get("until"),
                int(args.get("limit", 50)),
            )
        if name == "slack_archive_thread":
            return _thread(args["channel"], args["thread_ts"])
        if name == "slack_archive_backfill":
            return _backfill(
                args["channel"], int(args.get("days_back", 30)), int(args.get("max_pages", 50))
            )
        if name == "slack_archive_extract_chunks":
            return _extract_chunks(
                args["channel"],
                args.get("group_by", "week"),
                args.get("match"),
                args.get("since"),
                args.get("until"),
                int(args.get("salient_count", 5)),
                int(args.get("keyword_count", 10)),
                int(args.get("max_chunks", 60)),
            )
    except SlackError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return None
