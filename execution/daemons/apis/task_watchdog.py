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
  * VERIFIED          → reconcile the already-produced result's exact run
                        provenance and close DONE; never re-run the effect.
  * everything else   → left alone (terminal, awaiting_approval is operator-owned,
                        blocked already escalated, pending is the SSE create path).

Tasks are read by state, newest first: one server-side status filter per
watched status, sorted by ``last_observation_at`` descending and paged by
offset until a short page, up to a stated safety bound (a bound that stops the
read with matching tasks still unread is logged at WARNING). A failed or
stalled task is retried or escalated only when its last
update is within RETRY_WINDOW_SECONDS (24h by default); older or ageless ones
are left untouched and counted as ``skipped_old``, so the long-unseen backlog
is not revived.

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
import re
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

from lib.daemon_runtime.task_lifecycle import (  # noqa: E402
    MAX_ATTEMPTS,
    TaskStatus,
    backoff_seconds,
    normalize,
    set_task_status,
)
from lib.daemon_runtime.session_finalize import (  # noqa: E402
    recover_run_session,
    update_run_session_status,
)

log = logging.getLogger("apis.watchdog")

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

# How long a task may sit ROUTED/EXECUTING before the watchdog treats it as
# stalled. Must exceed APIS_DISPATCH_TIMEOUT (default 1800s) so a legitimately
# long-running agent is not re-dispatched out from under itself.
def stall_seconds(env=None) -> int:
    """How long an in-flight task may sit before the watchdog re-dispatches
    it. It must outlast one whole dispatch. With the Fly executor enabled
    that includes a login-machine queue wait (APIS_FLY_QUEUE_MAX_WAIT_SECONDS,
    not counted against the dispatch timeout) and the Apis deadline grace,
    or the watchdog would re-dispatch a run that is still in flight."""
    env = os.environ if env is None else env
    base = int(env.get("APIS_DISPATCH_TIMEOUT", "1800")) + 600
    if (env.get("APIS_FLY_RUNNER_CLASSES") or "").strip():
        # The same strict parser the executor uses. An invalid setting
        # disables Fly (every launch refused), so nothing queues: no wait.
        from fly_runner import APIS_DEADLINE_GRACE_SECONDS, LIMITS, FlyConfigInvalid, parse_setting

        queue = next(lim for lim in LIMITS if lim.attr == "queue_max_wait_seconds")
        try:
            base += int(parse_setting(queue, env)) + APIS_DEADLINE_GRACE_SECONDS
        except FlyConfigInvalid:
            pass
    return max(base, int(env.get("APIS_STALL_SECONDS", "3600")))


STALL_SECONDS = stall_seconds()
# Watchdog sweep cadence.
SWEEP_INTERVAL_SECONDS = max(60, int(os.environ.get("APIS_WATCHDOG_INTERVAL_SECONDS", "300")))
# Rows per page of each status-filtered query.
QUERY_LIMIT = max(50, int(os.environ.get("APIS_WATCHDOG_QUERY_LIMIT", "500")))
# Pages read per watched status per sweep. The pages are read one at a time,
# never concurrently, so a sweep costs at most
# len(_WATCHED_STATUSES) * MAX_PAGES_PER_STATUS requests.
MAX_PAGES_PER_STATUS = max(1, int(os.environ.get("APIS_WATCHDOG_MAX_PAGES", "5")))
# Neotoma refuses a ``cursor`` beside ``snapshot_filters``
# (ERR_CURSOR_COMBINATION) and an ``offset`` past 2000 (ERR_OFFSET_TOO_DEEP),
# so a filtered listing is walked by offset (see lib/daemon_runtime/gating.py).
MAX_QUERY_OFFSET = 2000
# Only a task whose last update is this recent is retried or escalated. Older
# failed or stalled tasks are left exactly as they are: making the backlog
# visible must not revive it or page the operator about it.
RETRY_WINDOW_SECONDS = max(
    0, int(os.environ.get("APIS_WATCHDOG_RETRY_WINDOW_SECONDS", str(24 * 3600)))
)

# Statuses the watchdog actively manages.
_RETRYABLE_INFLIGHT = frozenset({TaskStatus.ROUTED.value, TaskStatus.EXECUTING.value})
# Every status the watchdog acts on; each is queried server-side by state.
_WATCHED_STATUSES = (
    TaskStatus.FAILED.value,
    TaskStatus.VERIFIED.value,
    *sorted(_RETRYABLE_INFLIGHT),
)


class WatchdogAction(str, Enum):
    NONE = "none"
    RETRY = "retry"        # re-dispatch (failed, or stalled in-flight / resume)
    RECONCILE = "reconcile"  # repair terminal provenance; never repeat the effect
    ESCALATE = "escalate"  # attempts exhausted → BLOCKED + page operator


_RUN_SESSION_RESULT = re.compile(r"(?:^|;\s*)run_session=([^;\s]+)")


@dataclass
class _AttemptState:
    attempts: int = 0
    last_retry_ts: float = 0.0


@dataclass
class TaskQuery:
    """The tasks one sweep read, and how complete that read was.

    ``matched`` is per status: the server's own ``total`` when it reports one,
    otherwise the rows read. ``truncated`` names each status whose paging was
    stopped by the safety bound with matching tasks still unread (or an
    unknown total).
    """

    rows: list[tuple[str, dict]] = field(default_factory=list)
    matched: dict[str, int] = field(default_factory=dict)
    truncated: list[str] = field(default_factory=list)


@dataclass
class TaskWatchdog:
    """Periodic sweeper. Holds per-task attempt state in memory."""

    stall_seconds: int = STALL_SECONDS
    retry_window_seconds: int = RETRY_WINDOW_SECONDS
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

        if s == TaskStatus.VERIFIED.value:
            return WatchdogAction.RECONCILE

        if s in _RETRYABLE_INFLIGHT:
            if age_seconds is not None and age_seconds >= self.stall_seconds:
                return WatchdogAction.RETRY if attempts < MAX_ATTEMPTS else WatchdogAction.ESCALATE
            return WatchdogAction.NONE

        # pending (SSE create owns it), done/declined/superseded
        # (terminal-ish), awaiting_approval (operator-owned), blocked (already
        # escalated) → not the watchdog's job.
        return WatchdogAction.NONE

    def is_recent(self, recency_seconds: float | None) -> bool:
        """Whether a task was updated recently enough to retry or escalate.

        No reliable age is treated as old: an ageless task is never revived.
        """
        return recency_seconds is not None and recency_seconds <= self.retry_window_seconds

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
        counts = {
            "matched": 0,
            "scanned": 0,
            "retried": 0,
            "reconciled": 0,
            "escalated": 0,
            "skipped_backoff": 0,
            "skipped_old": 0,
        }
        try:
            tasks = _query_tasks(QUERY_LIMIT)
        except Exception as exc:  # noqa: BLE001 — never let a query error kill the loop
            log.warning("[watchdog] task query failed: %s", exc)
            return counts

        counts["matched"] = sum(tasks.matched.values())
        for entity_id, snapshot in tasks.rows:
            counts["scanned"] += 1
            status = snapshot.get("status", "")
            age = _age_seconds(snapshot, now)
            action = self.classify(entity_id, status, age)

            if action == WatchdogAction.NONE:
                continue

            if action in (WatchdogAction.RETRY, WatchdogAction.ESCALATE) and not self.is_recent(
                _recency_seconds(snapshot, now)
            ):
                # Old (or ageless) failed/stalled work: no status change, no
                # dispatch, no notification.
                counts["skipped_old"] += 1
                continue

            if action == WatchdogAction.RECONCILE:
                if _reconcile_completed_task(entity_id, snapshot):
                    counts["reconciled"] += 1
                    self.forget(entity_id)
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

        log.info("[watchdog] sweep: %s matched_by_status=%s", counts, tasks.matched)
        if tasks.truncated:
            log.warning(
                "[watchdog] sweep incomplete: paging bound (%d pages of %d, max offset %d) "
                "stopped the read of status %s with matching tasks unread; "
                "recent matching tasks may be among them "
                "(matched=%s, scanned=%d)",
                MAX_PAGES_PER_STATUS, QUERY_LIMIT, MAX_QUERY_OFFSET,
                ",".join(tasks.truncated), tasks.matched, counts["scanned"],
            )
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


def _reconcile_completed_task(entity_id: str, snapshot: dict) -> bool:
    """Finish provenance for an effect already produced by the executor.

    ``verified`` is intentionally non-dispatchable. The result field carries
    the exact native run identity, so recovery repairs that run and never has to
    infer which earlier attempt produced the effect.
    """
    result = str(snapshot.get("result") or "")
    match = _RUN_SESSION_RESULT.search(result)
    if not match:
        log.warning(
            "[watchdog] verified task %s has no exact run_session marker; "
            "leaving it verified",
            entity_id,
        )
        return False
    native_session_id = match.group(1)
    run = recover_run_session(
        task_id=entity_id,
        native_session_id=native_session_id,
    )
    if run is None or not update_run_session_status(run, status="completed"):
        log.warning(
            "[watchdog] terminal provenance still unresolved for task %s run %s",
            entity_id,
            native_session_id,
        )
        return False
    final_result = result.replace("provenance=unverified", "provenance=verified")
    return set_task_status(
        entity_id,
        TaskStatus.DONE,
        handler="apis-watchdog",
        from_status=TaskStatus.VERIFIED.value,
        reason="",
        result=final_result,
        key_suffix="provenance-reconciled",
    )


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


def _query_tasks(limit: int) -> TaskQuery:
    """Read the tasks in each watched status, newest first, via POST /entities/query.

    One status at a time, filtered server-side with ``snapshot_filters`` and
    sorted by ``last_observation_at`` descending, each walked by offset (the
    server refuses a cursor beside filters or a non-default sort). Requests
    are sequential, never concurrent. Raises on transport error (the caller
    swallows it). Returns an empty read when no token.

    Each status pages until a short page or the page and offset safety
    bounds. Paging never stops on an old row: the sort is requested, not
    trusted, so a server that ignored it cannot end the read early. Recency
    is applied by the caller, after the read.

    A status counts as truncated whenever the bound stops its read while the
    server's total exceeds the rows read, or no total is reported, so a read
    cut short is always visible whether or not the sort was honoured.
    """
    result = TaskQuery()
    if not NEOTOMA_BEARER_TOKEN:
        log.warning("[watchdog] no bearer token — cannot query tasks")
        return result
    seen: set[str] = set()
    for status in _WATCHED_STATUSES:
        offset = 0
        read = 0
        total: int | None = None
        stopped_by_bound = False
        for page_no in range(MAX_PAGES_PER_STATUS):
            body: dict = {
                "entity_type": "task",
                "snapshot_filters": {"status": {"op": "eq", "value": status}},
                "sort_by": "last_observation_at",
                "sort_order": "desc",
                "limit": limit,
                "include_snapshots": True,
            }
            if offset:
                body["offset"] = offset
            resp = httpx.post(
                f"{NEOTOMA_BASE_URL}/entities/query",
                headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
                json=body,
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            if total is None and isinstance(data.get("total"), int):
                total = data["total"]
            rows = data.get("entities") or data.get("results") or []
            read += len(rows)
            for row in rows:
                eid = row.get("entity_id") or row.get("id")
                snap = _unwrap_snapshot(row)
                if eid and isinstance(snap, dict) and eid not in seen:
                    seen.add(eid)
                    result.rows.append((eid, snap))
            if len(rows) < limit:
                break
            offset += len(rows)
            if offset > MAX_QUERY_OFFSET or page_no == MAX_PAGES_PER_STATUS - 1:
                stopped_by_bound = True
                break
        result.matched[status] = total if total is not None else read
        unread = total is None or total > read
        if stopped_by_bound and unread:
            result.truncated.append(status)
    return result


# Stamps that live at the ROW level of an EntitySnapshot, as siblings of
# `snapshot` rather than inside it (openapi.yaml declares `computed_at` and
# `last_observation_at` alongside `snapshot`). Without carrying them down, a
# task whose snapshot holds no stamp of its own has no age at all.
_ROW_LEVEL_STAMPS = ("last_observation_at", "computed_at")


def _unwrap_snapshot(row: dict) -> dict:
    """Tolerate the several snapshot nesting shapes Neotoma returns.

    Row-level stamps are carried into the returned dict without shadowing a
    stamp the snapshot already carries.
    """
    snap = row.get("snapshot")
    if isinstance(snap, dict):
        inner = snap.get("snapshot")
        base = inner if isinstance(inner, dict) else snap
        carried = {
            k: row[k]
            for k in _ROW_LEVEL_STAMPS
            if k in row and k not in base and row[k] is not None
        }
        return {**base, **carried} if carried else base
    return row


def _age_seconds(snapshot: dict, now: float) -> float | None:
    """Seconds since the task was last touched, from the best available stamp."""
    for key in ("updated_at", "last_observation_at", "updated_date", "computed_at", "created_at"):
        ts = snapshot.get(key)
        parsed = _parse_ts(ts)
        if parsed is not None:
            return max(0.0, now - parsed)
    return None


# Stamps that move when the task is written, so they bound the last status
# change from below. ``created_at`` (creation, not the failure) and
# ``computed_at`` (when the snapshot was computed, which can be any read) are
# deliberately absent: either could make an old task look recent or ageless.
_RECENCY_STAMPS = ("updated_at", "last_observation_at", "updated_date")


def _recency_seconds(snapshot: dict, now: float) -> float | None:
    """Seconds since the task's last write, or None when no write stamp exists.

    A task has no field stamped by its status change, so this is the last
    observation of any field: never older than the status change, and possibly
    newer when another field was written after it.
    """
    for key in _RECENCY_STAMPS:
        parsed = _parse_ts(snapshot.get(key))
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
    checks["verified_reconciles"] = wd.classify("t3", "verified", 5000) == WatchdogAction.RECONCILE

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
