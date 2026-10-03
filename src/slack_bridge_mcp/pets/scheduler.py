"""Global pet-run scheduler — bound concurrency, order by priority, dedupe flaps.

Each pet fire is a heavyweight ``claude -p`` subprocess (which may itself spawn
sub-agents), so the supervisor must NOT run them unbounded. Every pet invocation
is funnelled through one priority queue served by a small, fixed worker pool
(size = ``SLACK_BRIDGE_PET_MAX_CONCURRENCY``, default 1). Higher-priority alerts
run first; an identical alert that re-fires while one is queued/running, or within
``SLACK_BRIDGE_PET_COOLDOWN_S`` of the last run, is skipped — so a flapping alert
can't pile the queue with redundant investigations.
"""

from __future__ import annotations

import itertools
import logging
import queue
import re
import threading
import time
from typing import Any

from ..config import settings
from .registry import load_one
from .runner import run as run_pet
from .spec import BotSpec

log = logging.getLogger("slack-watcher")

# Priority bumps inferred from the alert text, added to the pet's own `priority`.
_SEVERITY: list[tuple[re.Pattern[str], int]] = [
    (re.compile(r"\b(sev\s*1|sev1|p1|page|pagerduty|critical|crit)\b", re.I), 30),
    (re.compile(r"\b(sev\s*2|sev2|p2|error|high)\b", re.I), 15),
    (re.compile(r"\bfiring\b", re.I), 5),
    (re.compile(r"\b(warn|warning|sev\s*3|p3)\b", re.I), 2),
]


def event_priority(spec_priority: int, text: str) -> int:
    """pet's base priority + the strongest severity bump found in the alert text."""
    bump = 0
    for rx, weight in _SEVERITY:
        if rx.search(text or ""):
            bump = max(bump, weight)
    return spec_priority + bump


def fingerprint(pet: str, text: str) -> str:
    """Stable key so re-fires of the same alert dedupe: drop the leading
    ``[FIRING:N]`` / ``[RESOLVED]`` token, keep the (normalised) title line."""
    first = text.splitlines()[0] if text else ""
    first = re.sub(r"^\s*\[[^\]]*\]\s*", "", first)
    norm = re.sub(r"\s+", " ", first).strip().lower()[:160]
    return f"{pet}:{norm}"


class PetScheduler:
    def __init__(self, max_concurrency: int, cooldown_s: int, max_queue: int) -> None:
        self.max_concurrency = max(1, max_concurrency)
        self.cooldown_s = max(0, cooldown_s)
        self._q: queue.PriorityQueue[tuple[int, int, BotSpec, dict[str, Any], str]] = (
            queue.PriorityQueue(maxsize=max(0, max_queue))
        )
        self._seq = itertools.count()
        self._lock = threading.Lock()
        self._pending: set[str] = set()  # fingerprints queued or running
        self._last_run: dict[str, float] = {}  # fingerprint -> monotonic finish time
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        for i in range(self.max_concurrency):
            threading.Thread(target=self._worker, name=f"pet-worker-{i}", daemon=True).start()
        log.info(
            "pet scheduler up: max_concurrency=%d cooldown_s=%d",
            self.max_concurrency,
            self.cooldown_s,
        )

    def enqueue(self, spec: BotSpec, ctx: dict[str, Any], priority: int) -> dict[str, Any]:
        fp = fingerprint(spec.name, ctx.get("text", ""))
        now = time.monotonic()
        with self._lock:
            if fp in self._pending:
                log.info("pet %s: same alert already queued/running, skip (%s)", spec.name, fp)
                return {"queued": False, "reason": "duplicate-inflight"}
            last = self._last_run.get(fp)
            if self.cooldown_s and last is not None and (now - last) < self.cooldown_s:
                left = int(self.cooldown_s - (now - last))
                log.info("pet %s: in cooldown (%ds left), skip (%s)", spec.name, left, fp)
                return {"queued": False, "reason": "cooldown", "cooldown_left_s": left}
            self._pending.add(fp)
        try:
            self._q.put_nowait((-priority, next(self._seq), spec, ctx, fp))
        except queue.Full:
            with self._lock:
                self._pending.discard(fp)
            log.warning("pet %s: queue full (max=%d), dropping", spec.name, self._q.maxsize)
            return {"queued": False, "reason": "queue-full"}
        log.info(
            "pet %s queued: priority=%d depth=%d concurrency=%d",
            spec.name,
            priority,
            self._q.qsize(),
            self.max_concurrency,
        )
        return {"queued": True, "priority": priority, "depth": self._q.qsize()}

    def _worker(self) -> None:
        while True:
            _negpri, _seq, spec, ctx, fp = self._q.get()
            try:
                fresh = load_one(spec.name) or spec  # apply latest enable/dry_run/caps
                if not fresh.enabled:
                    log.info("pet %s disabled before run, skipping", spec.name)
                else:
                    run_pet(fresh, ctx)
            except Exception as e:
                log.exception("pet %s run failed: %s", spec.name, e)
            finally:
                with self._lock:
                    self._pending.discard(fp)
                    self._last_run[fp] = time.monotonic()
                self._q.task_done()


_scheduler: PetScheduler | None = None
_scheduler_lock = threading.Lock()


def get_scheduler() -> PetScheduler:
    """Lazily build + start the process-wide scheduler from current settings."""
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            cfg = settings()
            _scheduler = PetScheduler(
                cfg.pet_max_concurrency, cfg.pet_cooldown_s, cfg.pet_max_queue
            )
            _scheduler.start()
        return _scheduler
