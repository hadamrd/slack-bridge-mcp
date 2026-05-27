"""Token-bucket rate limiter shared across every Slack API caller in this
process (daemon, MCP tools, backfill, refresh).

Why per-class buckets, not a global one
---------------------------------------
Slack rate-limits per method tier, so a `search.messages` storm doesn't have
to starve a `chat.postMessage`. We keep one bucket per `_BUCKET` group (see
`_METHOD_TO_BUCKET`), and `default` for anything unmapped.

Why in-memory (no disk persistence)
-----------------------------------
The daemon and the MCP server are independent processes; persisting state
would require file locking we don't want. Daemon restarts reset the bucket;
that's fine — Slack also moves on. The cost of a fresh bucket is at most
`max_tokens` requests of burst before the limiter throttles, well under any
real damage threshold.

Why we trust observed signals over Slack's docs
-----------------------------------------------
The advertised "Tier 2: 20+ req/min" means **at least** 20 — Enterprise Grid
appears to actually allow higher in steady state but punishes harder bursts.
We start conservative and let `penalty_429` ratchet the refill rate down
when we see real 429s. After 5 idle minutes the rate slowly recovers.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("slack-ratelimit")


@dataclass
class _BucketSpec:
    capacity: int  # max burst tokens
    refill_per_min: float  # steady-state tokens per minute


# Conservative starting points — feel free to bump after observation.
# Format: bucket_name -> (capacity, refill_per_min)
_BUCKETS: dict[str, _BucketSpec] = {
    "history": _BucketSpec(15, 25),  # conversations.history, conversations.replies
    "search": _BucketSpec(6, 12),  # search.messages — Slack throttles this hard
    "info": _BucketSpec(40, 60),  # users.info, users.list, etc.
    "post": _BucketSpec(30, 45),  # chat.postMessage, conversations.open
    "client": _BucketSpec(20, 30),  # client.counts, client.boot
    "default": _BucketSpec(15, 25),
}


_METHOD_TO_BUCKET: dict[str, str] = {
    # history-class
    "conversations.history": "history",
    "conversations.replies": "history",
    "conversations.list": "history",
    "users.conversations": "history",
    # info-class (stateless metadata reads, generally cheap)
    "conversations.info": "info",
    "users.info": "info",
    "users.list": "info",
    "users.lookupByEmail": "info",
    "users.profile.get": "info",
    "auth.test": "info",
    "bots.info": "info",
    # search-class (stricter)
    "search.messages": "search",
    "search.modules.messages": "search",
    "search.modules.channels": "search",
    "search.modules.users": "search",
    # post-class (writes)
    "conversations.open": "post",
    "users.profile.set": "post",
    "chat.postMessage": "post",
    "chat.update": "post",
    "chat.delete": "post",
    "reactions.add": "post",
    "reactions.remove": "post",
    # client-bootstrap class
    "client.counts": "client",
    "client.boot": "client",
}


class _TokenBucket:
    def __init__(self, name: str, spec: _BucketSpec):
        self.name = name
        self.capacity = float(spec.capacity)
        self.base_refill_per_sec = spec.refill_per_min / 60.0
        self.refill_per_sec = self.base_refill_per_sec
        self.tokens = self.capacity
        self.last_refill = time.monotonic()
        self.recent_429 = 0
        self.last_429_at = 0.0
        self.calls_acquired = 0  # telemetry
        self.calls_throttled = 0
        self.calls_429 = 0
        self._lock = threading.Lock()

    # No locking inside; caller must hold self._lock.
    def _refill_locked(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
        self.last_refill = now
        # Recover from 429 penalty after 5 min idle.
        if self.recent_429 > 0 and (now - self.last_429_at) > 300:
            self.recent_429 = max(0, self.recent_429 - 1)
            # Slowly restore refill rate.
            self.refill_per_sec = min(self.base_refill_per_sec, self.refill_per_sec * 1.4)

    def acquire(self, max_wait_s: float = 60.0) -> bool:
        """Block up to max_wait_s for one token. Returns False on timeout."""
        deadline = time.monotonic() + max_wait_s
        first_attempt = True
        while True:
            with self._lock:
                self._refill_locked()
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    self.calls_acquired += 1
                    return True
                wait = (1.0 - self.tokens) / max(self.refill_per_sec, 0.001)
            if time.monotonic() + wait > deadline:
                self.calls_throttled += 1
                return False
            if first_attempt:
                self.calls_throttled += 1
                first_attempt = False
            time.sleep(min(wait, 2.0))

    def penalty_429(self, retry_after_s: float = 5.0) -> None:
        """Called when Slack returns 429. Drains immediately + halves refill."""
        with self._lock:
            self.recent_429 += 1
            self.last_429_at = time.monotonic()
            self.tokens = 0.0
            # Multiplicative shrink, floored.
            self.refill_per_sec = max(0.1, self.refill_per_sec * 0.5)
            self.calls_429 += 1
            log.warning(
                "rate-limit penalty: bucket=%s now=%.2f rps (was %.2f), retry-after=%.0fs",
                self.name,
                self.refill_per_sec,
                self.base_refill_per_sec,
                retry_after_s,
            )

    def status(self) -> dict[str, Any]:
        with self._lock:
            self._refill_locked()
            return {
                "name": self.name,
                "tokens": round(self.tokens, 1),
                "capacity": self.capacity,
                "refill_per_sec": round(self.refill_per_sec, 2),
                "base_refill_per_sec": round(self.base_refill_per_sec, 2),
                "recent_429": self.recent_429,
                "calls_acquired": self.calls_acquired,
                "calls_throttled": self.calls_throttled,
                "calls_429": self.calls_429,
            }


_BUCKETS_INSTANCES: dict[str, _TokenBucket] = {
    name: _TokenBucket(name, spec) for name, spec in _BUCKETS.items()
}


def _bucket_for(method: str) -> _TokenBucket:
    return _BUCKETS_INSTANCES[_METHOD_TO_BUCKET.get(method, "default")]


def acquire(method: str, max_wait_s: float = 60.0) -> bool:
    """Block up to max_wait_s waiting for budget for `method`."""
    return _bucket_for(method).acquire(max_wait_s=max_wait_s)


def penalty_429(method: str, retry_after_s: float = 5.0) -> None:
    """Mark a 429 against the bucket for `method`. Future acquires throttle."""
    _bucket_for(method).penalty_429(retry_after_s)


def status_all() -> list[dict[str, Any]]:
    return [b.status() for b in _BUCKETS_INSTANCES.values()]
