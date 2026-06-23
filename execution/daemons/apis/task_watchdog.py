"""
task_watchdog.py — Apis stall watchdog (plan ent_aff87747b49e338790568af6, task #2).

Task #1 gave every task a real lifecycle status that Apis writes inline. But the
SSE loop fires once per event: a task that goes FAILED, or that is left mid-flight
(ROUTED/EXECUTING) when the daemon restarts, never gets picked up again. This
watchdog is the out-of-band sweeper that closes those gaps WITHOUT blocking the
SSE loop (no inline sleep in dispatch):

  * FAILED            → retry (re-dispatch) with exponential backoff, up to
                        MAX_ATTEMPTS, then escalate to the operator (BLOCKED).
  * ROUTED/EXECUTING  → if stalled longer than the stall window (the agent
    stalled past stall    subprocess died, or the daemon restarted mid-flight),
                        re-dispatch — this also implements task RESUME after a
                        restart — then escalate once attempts are exhausted.
  * everything else   → left alone (terminal, awaiting_approval is operator-owned,
                        blocked already escalated, pending is the SSE create path).

The decision logic is pure (classify_task / should_retry_now) and unit-tested;
the I/O (query Neotoma, re-dispatch, escalate) is thin and fail-open. Attempt
counts are held in memory keyed by task id until a persistent task-schema
attempt field lands (the task #1 follow-up); a restart resets them, which is
safe — it just grants a fresh retry budget.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

import httpx

# Allow standalone execution (the self-test) by putting the repo root on the
# path; inside the daemon, apis.py has already done this before importing us.
import sys as _sys
from pathlib import Path as _Path

_REPO_ROOT = _Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime.task_lifecycle import (
    MAX_ATTEMPTS,
    TaskStatus,
    backoff_seconds,
    normalize,
    set_task_status,
)

log = logging.getLogger("apis.watchdog")

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

# How long a task may sit ROUTED/EXECUTING before the watchdog treats it as
# stalled. Must exceed APIS_DISPATCH_TIMEOUT (default 1800s) so a legitimately
# long-running agent is not re-dispatched out from under itself.
STALL_SECONDS = max(
    int(os.environ.get("APIS_DISPATCH_TIMEOUT", "1800")) + 600,
    int(os.environ.get("APIS_STALL_SECONDS", "3600")),
)
# Watchdog sweep cadence.
SWEEP_INTERVAL_SECONDS = max(60, int(os.environ.get("APIS_WATCHDOG_INTERVAL_SECONDS", "300")))
# Max tasks pulled per sweep (client-side filtered). Pagination is a follow-up.
QUERY_LIMIT = max(50, int(os.environ.get("APIS_WATCHDOG_QUERY_LIMIT", "500")))

# Statuses the watchdog actively manages.
_RETRYABLE_INFLIGHT = frozenset({TaskStatus.ROUTED.value, TaskStatus.EXECUTING.value})


class WatchdogAction(str, Enum):
    NONE = "none"
    RETRY = "retry"        # re-dispatch (failed, or stalled in-flight / resume)
    ESCALATE = "escalate"  # attempts exhausted → BLOCKED + page operator


@dataclass
class _AttemptState:
    attempts: int = 0
    last_retry_ts: float = 0.0


@dataclass
class TaskWatchdog:
    """Periodic sweeper. Holds per-task attempt state in memory."""

    stall_seconds: int = STALL_SECONDS
    _state: dict[str, _AttemptState] = field(default_factory=dict)

    # ── pure decision logic (unit-tested) ──────────────────────────────────

    def attempts_for(self, task_id: str) -> int:
        st = self._state.get(task_id)
        return st.attempts if st else 0

    def classify(self, task_id: str, status: str, age_seconds: float | None) -> WatchdogAction:
        """Decide what to do with one task, given its status and age."""
        s = normalize(status)
        attempts = self.attempts_for(task_id)

        if s == TaskStatus.FAILED.value:
            return WatchdogAction.RETRY if attempts < MAX_ATTEMPTS else WatchdogAction.ESCALATE

        if s in _RETRYABLE_INFLIGHT:
            if age_seconds is not None and age_seconds >= self.stall_seconds:
                return WatchdogAction.RETRY if attempts < MAX_ATTEMPTS else WatchdogAction.ESCALATE
            return WatchdogAction.NONE

        # pending (SSE create owns it), verified/done/declined/superseded
        # (terminal-ish), awaiting_approval (operator-owned), blocked (already
        # escalated) → not the watchdog's job.
        return WatchdogAction.NONE

    def should_retry_now(self, task_id: str, now: float) -> bool:
        """Respect exponential backoff between retries of the same task."""
        st = self._state.get(task_id)
        if st is None or st.attempts == 0:
            return True
        return (now - st.last_retry_ts) >= backoff_seconds(st.attempts)

    def record_retry(self, task_id: str, now: float) -> int:
        st = self._state.setdefault(task_id, _AttemptState())
        st.attempts += 1
        st.last_retry_ts = now
        return st.attempts

    def forget(self, task_id: str) -> None:
        self._state.pop(task_id, None)

    # ── I/O (fail-open) ────────────────────────────────────────────────────

    async def sweep(self, notifier, dispatch_fn) -> dict:
        """One pass: classify every active task and act. Returns a counts dict.

        `dispatch_fn(task_id, snapshot, trigger)` is an async callable (Apis's
        dispatch_task closure) used to re-dispatch a retryable task.
        """
        now = time.time()
        counts = {"scanned": 0, "retried": 0, "escalated": 0, "skipped_backoff": 0}
        try:
            tasks = _query_tasks(QUERY_LIMIT)
        except Exception as exc:  # noqa: BLE001 — never let a query error kill the loop
            log.warning("[watchdog] task query failed: %s", exc)
            return counts

        for entity_id, snapshot in tasks:
            counts["scanned"] += 1
            status = snapshot.get("status", "")
            age = _age_seconds(snapshot, now)
            action = self.classify(entity_id, status, age)

            if action == WatchdogAction.NONE:
                continue

            if action == WatchdogAction.ESCALATE:
                set_task_status(
                    entity_id, TaskStatus.BLOCKED, handler="apis-watchdog",
                    from_status=status,
                    reason=f"exhausted {MAX_ATTEMPTS} dispatch attempts (watchdog)",
                    key_suffix="watchdog",
                )
                _notify(notifier, entity_id, snapshot, MAX_ATTEMPTS)
                counts["escalated"] += 1
                self.forget(entity_id)
                continue

            # RETRY — respect backoff between attempts of the same task.
            if not self.should_retry_now(entity_id, now):
                counts["skipped_backoff"] += 1
                continue
            n = self.record_retry(entity_id, now)
            log.info(
                "[watchdog] re-dispatching %s (status=%s age=%ss attempt=%d/%d)",
                entity_id, normalize(status), int(age or 0), n, MAX_ATTEMPTS,
            )
            # Re-open to ROUTED so the lifecycle reflects the retry, then dispatch.
            set_task_status(
                entity_id, TaskStatus.ROUTED, handler="apis-watchdog",
                from_status=status, key_suffix=f"watchdog-retry-{n}",
            )
            try:
                await dispatch_fn(entity_id, snapshot, "watchdog_retry")
                counts["retried"] += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("[watchdog] re-dispatch of %s failed: %s", entity_id, exc)

        if counts["scanned"]:
            log.info("[watchdog] sweep: %s", counts)
        return counts

    async def run(self, notifier, dispatch_fn) -> None:
        """Sweep forever on SWEEP_INTERVAL_SECONDS. Fail-open per iteration."""
        log.info(
            "[watchdog] starting (stall=%ss, interval=%ss, max_attempts=%d)",
            self.stall_seconds, SWEEP_INTERVAL_SECONDS, MAX_ATTEMPTS,
        )
        while True:
            try:
                await self.sweep(notifier, dispatch_fn)
            except Exception as exc:  # noqa: BLE001 — never crash the daemon
                log.warning("[watchdog] sweep error (ignored): %s", exc)
            await asyncio.sleep(SWEEP_INTERVAL_SECONDS)


# ── module-level I/O helpers ────────────────────────────────────────────────


def _notify(notifier, entity_id: str, snapshot: dict, attempts: int) -> None:
    title = snapshot.get("title", "(untitled)")
    try:
        from lib.notify import Priority

        notifier.send(
            f"Task BLOCKED after {attempts} attempts (watchdog): {title[:70]}\n  {entity_id}",
            priority=Priority.BLOCKER,
            handler="apis-watchdog",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("[watchdog] notify failed for %s: %s", entity_id, exc)


def _query_tasks(limit: int) -> list[tuple[str, dict]]:
    """Return [(entity_id, snapshot), …] for tasks, via POST /entities/query.

    Raises on transport error (the caller swallows it). Returns [] when no token.
    """
    if not NEOTOMA_BEARER_TOKEN:
        log.warning("[watchdog] no bearer token — cannot query tasks")
        return []
    resp = httpx.post(
        f"{NEOTOMA_BASE_URL}/entities/query",
        headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
        json={"entity_type": "task", "limit": limit},
        timeout=20,
    )
    resp.raise_for_status()
    data = resp.json()
    rows = data.get("entities") or data.get("results") or []
    out: list[tuple[str, dict]] = []
    for row in rows:
        eid = row.get("entity_id") or row.get("id")
        snap = _unwrap_snapshot(row)
        if eid and isinstance(snap, dict):
            out.append((eid, snap))
    return out


def _unwrap_snapshot(row: dict) -> dict:
    """Tolerate the several snapshot nesting shapes Neotoma returns."""
    snap = row.get("snapshot")
    if isinstance(snap, dict):
        inner = snap.get("snapshot")
        if isinstance(inner, dict):
            return inner
        return snap
    return row


def _age_seconds(snapshot: dict, now: float) -> float | None:
    """Seconds since the task was last touched, from the best available stamp."""
    for key in ("updated_at", "last_observation_at", "updated_date", "computed_at", "created_at"):
        ts = snapshot.get(key)
        parsed = _parse_ts(ts)
        if parsed is not None:
            return max(0.0, now - parsed)
    return None


def _parse_ts(ts) -> float | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        # Accept ISO 8601, with or without trailing Z.
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


# ── self-test (pure logic) ──────────────────────────────────────────────────


def _selftest() -> int:
    wd = TaskWatchdog(stall_seconds=3600)
    checks: dict[str, bool] = {}

    # FAILED → retry until exhausted, then escalate.
    checks["failed_retry"] = wd.classify("t1", "failed", None) == WatchdogAction.RETRY
    wd._state["t1"] = _AttemptState(attempts=MAX_ATTEMPTS)
    checks["failed_exhausted_escalate"] = wd.classify("t1", "failed", None) == WatchdogAction.ESCALATE

    # In-flight: fresh → NONE, stalled → RETRY.
    checks["executing_fresh_none"] = wd.classify("t2", "executing", 60) == WatchdogAction.NONE
    checks["executing_stalled_retry"] = wd.classify("t2", "executing", 4000) == WatchdogAction.RETRY
    checks["routed_stalled_retry"] = wd.classify("t3", "routed", 5000) == WatchdogAction.RETRY

    # Hands-off states.
    checks["pending_none"] = wd.classify("t4", "pending", 99999) == WatchdogAction.NONE
    checks["done_none"] = wd.classify("t4", "done", 99999) == WatchdogAction.NONE
    checks["awaiting_none"] = wd.classify("t4", "awaiting_approval", 99999) == WatchdogAction.NONE
    checks["blocked_none"] = wd.classify("t4", "blocked", 99999) == WatchdogAction.NONE

    # Backoff between retries.
    wd2 = TaskWatchdog()
    checks["first_retry_ok"] = wd2.should_retry_now("x", now=1000.0)
    wd2.record_retry("x", now=1000.0)  # attempts=1
    checks["retry_blocked_by_backoff"] = not wd2.should_retry_now("x", now=1000.0 + 1)
    checks["retry_ok_after_backoff"] = wd2.should_retry_now("x", now=1000.0 + backoff_seconds(1) + 1)
    checks["record_increments"] = wd2.record_retry("x", now=2000.0) == 2

    # Timestamp parsing + age.
    now = 1_000_000.0
    checks["age_from_iso"] = _age_seconds({"updated_at": _iso(now - 120)}, now) is not None and \
        abs(_age_seconds({"updated_at": _iso(now - 120)}, now) - 120) < 2
    checks["age_none_when_absent"] = _age_seconds({}, now) is None

    ok = all(checks.values())
    for k, v in checks.items():
        print(f"[{'PASS' if v else 'FAIL'}] {k}")
    return 0 if ok else 1


def _iso(epoch: float) -> str:
    from datetime import timezone

    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


if __name__ == "__main__":
    import sys

    sys.exit(_selftest())
