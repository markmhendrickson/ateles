"""
task_reconciler.py — Apis level-triggered reconciliation sweep (ateles#586).

WHY THIS EXISTS
---------------
Apis dispatches tasks edge-triggered, off the `task.created` SSE event. That is
fine while the event stream is healthy and catastrophic when it is not: a task
created during an outage produces exactly one event, nobody consumes it, and
nothing ever looks at that task again. It stays `pending` forever.

ateles#589 is the proof. `NEOTOMA_SSE_SUBSCRIPTION_ID_APIS` was absent from the
Apis process, `sse_client.py` logged a warning and returned, and the dispatcher
was deaf to task events for 88 days — 67,450 silent skips, last task event
2026-06-04, ~100 tasks stranded `pending`.

Restoring that env var is the repair. This module is the *durability backstop*
that makes the next outage survivable, and it is needed regardless of the env
fix: nothing in the swarm re-examines an existing `pending` task.

`task_watchdog.py` explicitly does NOT cover this. It manages FAILED and stalled
ROUTED/EXECUTING tasks — work that started and got stuck. It classifies
`pending` as NONE ("the SSE create path owns it"), which was true only while the
SSE create path was alive. A task that was NEVER dispatched is invisible to it.
This module owns exactly that hole and nothing else.

WHAT IT DOES
------------
Every APIS_RECONCILE_INTERVAL_SECONDS, query tasks, keep the ones that are
`pending`, un-owned by any in-flight dispatch, and older than a grace window,
then dispatch up to APIS_RECONCILE_MAX_PER_SWEEP of them through the SAME
`dispatch_task` used by the SSE path — so the confidence x blast-radius
execution gate still applies and high-blast work still holds for a checkpoint.
The sweep does not dispatch anything; it hands each task to Apis's dispatcher.

NOT DOUBLE-DISPATCHING (the correctness crux)
---------------------------------------------
Three independent layers, each covering a case the others miss:

  1. STATUS FILTER. Only `pending` (or an empty/absent status) is eligible.
     `dispatch_task` writes ROUTED before the readiness gate, the execution
     gate, and any spawn — so from the instant the SSE path picks a task up it
     is no longer `pending` and no longer selectable here. Every other status
     (routed/executing/awaiting_*/failed/blocked/done/...) is skipped with a
     logged reason; failed and stalled work belongs to the watchdog, and
     awaiting_approval belongs to the operator.

  2. GRACE WINDOW. Layer 1 has a race: a task created two seconds ago may be
     inside `dispatch_task` right now with its ROUTED correction still in
     flight. So a task must have been untouched for at least
     APIS_RECONCILE_GRACE_SECONDS (default 900s, comfortably longer than a
     dispatch takes to reach its ROUTED write) before the sweep will consider
     it. A live SSE path therefore always wins the race; the sweep only ever
     sees tasks the event path demonstrably did not take.

  3. IN-PROCESS CLAIM LEDGER. Status writes are fail-OPEN by design
     (`set_task_status` logs and returns False rather than raising). If the
     ROUTED write is lost, the task still reads `pending` on the next pass and
     layers 1+2 would both re-select it. So the reconciler records every task
     id it has dispatched and refuses to dispatch a claimed id again, for the
     lifetime of the process. A restart clears the ledger, which is safe: after
     a restart layer 1 governs, and the re-dispatched work is the same
     idempotent skill invocation the watchdog's retry path already performs.

Claims are never released on success. This sweep's job is to give a stranded
task its FIRST dispatch; retry-after-failure is the watchdog's job and stays
there, so a task cannot be retried by two mechanisms on different schedules.

BOUNDED PER PASS
----------------
~100 tasks are waiting right now. Fanning all of them into T4 agents in one
pass would be its own incident. APIS_RECONCILE_MAX_PER_SWEEP (default 5) caps
dispatches per pass; the remainder are logged as deferred and picked up next
pass, so the backlog drains at a visible, controlled rate instead of stampeding.

OBSERVABILITY
-------------
A silent sweep would reproduce exactly the invisibility this fixes. Every pass
logs its counts, every skip logs a reason at task granularity, and the reasons
are a closed vocabulary (SkipReason) so they can be counted and alerted on.

Environment:
  APIS_RECONCILE_ENABLED           "1" to run the sweep (default: "0" — off).
  APIS_RECONCILE_INTERVAL_SECONDS  Sweep cadence (default: 900).
  APIS_RECONCILE_MAX_PER_SWEEP     Max dispatches per pass (default: 5).
  APIS_RECONCILE_GRACE_SECONDS     Min task age before eligible (default: 900).
  APIS_RECONCILE_QUERY_LIMIT       Tasks fetched per pass (default: 500).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from enum import Enum

import httpx

# Allow standalone execution by putting the repo root on the path; inside the
# daemon, apis.py has already done this before importing us.
import sys as _sys
from pathlib import Path as _Path

_REPO_ROOT = _Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in _sys.path:
    _sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime.task_lifecycle import TaskStatus, normalize  # noqa: E402

# Sibling daemon module (imported bare, as apis.py does at runtime).
from task_watchdog import _age_seconds as _watchdog_age  # noqa: E402

log = logging.getLogger("apis.reconciler")

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

# Off by default. See module docstring + the PR: the first pass runs against a
# ~100-task backlog, so the operator turns this on deliberately, watches one
# pass, and leaves it on.
ENABLED = os.environ.get("APIS_RECONCILE_ENABLED", "0") == "1"

# Sweep cadence. Slower than the watchdog (300s): this is a backstop for a rare
# failure, not a hot loop, and every pass costs a full task query.
INTERVAL_SECONDS = max(60, int(os.environ.get("APIS_RECONCILE_INTERVAL_SECONDS", "900")))

# Bounded fan-out per pass — the cap that keeps a backlog drain from becoming
# its own incident.
MAX_PER_SWEEP = max(1, int(os.environ.get("APIS_RECONCILE_MAX_PER_SWEEP", "5")))

# Double-dispatch layer 2: a task must be this old (untouched) before the sweep
# will consider it, so a live SSE dispatch always wins the race to ROUTED.
GRACE_SECONDS = max(60, int(os.environ.get("APIS_RECONCILE_GRACE_SECONDS", "900")))

# Tasks fetched per pass (client-side filtered, like the watchdog).
QUERY_LIMIT = max(50, int(os.environ.get("APIS_RECONCILE_QUERY_LIMIT", "500")))

# Double-dispatch layer 1: the only statuses this sweep will act on. An absent
# or empty status counts as pending — legacy rows predate the lifecycle field.
_SWEEPABLE = frozenset({TaskStatus.PENDING.value, ""})


class SkipReason(str, Enum):
    """Closed vocabulary of why a scanned task was not dispatched.

    Closed so skips can be counted and alerted on. A silent sweep reproduces the
    invisibility this module exists to fix, so every scanned-but-skipped task
    resolves to exactly one of these and is logged with it.
    """

    NOT_PENDING = "not_pending"          # layer 1: another mechanism owns it
    WITHIN_GRACE = "within_grace"        # layer 2: SSE may still be dispatching it
    ALREADY_CLAIMED = "already_claimed"  # layer 3: this sweep dispatched it before
    CAP_REACHED = "cap_reached"          # bounded fan-out: deferred to next pass


@dataclass
class TaskReconciler:
    """Level-triggered sweeper for tasks the edge-triggered path never saw.

    Holds the in-process claim ledger (double-dispatch layer 3).
    """

    max_per_sweep: int = MAX_PER_SWEEP
    grace_seconds: int = GRACE_SECONDS
    _claimed: set[str] = field(default_factory=set)

    # ── pure decision logic (unit-tested) ──────────────────────────────────

    def is_claimed(self, task_id: str) -> bool:
        return task_id in self._claimed

    def claim(self, task_id: str) -> None:
        """Record a dispatch. Never released — first dispatch only; retries are
        the watchdog's job, so a task is never retried by two mechanisms."""
        self._claimed.add(task_id)

    def should_dispatch(
        self, task_id: str, status: str, age_seconds: float | None
    ) -> SkipReason | None:
        """Return None when the task should be dispatched, else why it is skipped.

        Applies double-dispatch layers 1-3 in order. The cap (bounded fan-out) is
        applied by the caller, since it depends on how many were dispatched
        earlier in the same pass.
        """
        # Layer 1 — status. dispatch_task writes ROUTED before any gate or spawn,
        # so anything the SSE path has touched has already left `pending`.
        if normalize(status) not in _SWEEPABLE:
            return SkipReason.NOT_PENDING

        # Layer 2 — grace window. An unknown age (no parseable timestamp) is
        # treated as within grace: fail SAFE toward not dispatching, since the
        # cost of a missed pass is one more sweep interval and the cost of a
        # wrong dispatch is duplicated agent work.
        if age_seconds is None or age_seconds < self.grace_seconds:
            return SkipReason.WITHIN_GRACE

        # Layer 3 — claim ledger. Covers a lost (fail-open) ROUTED write.
        if self.is_claimed(task_id):
            return SkipReason.ALREADY_CLAIMED

        return None

    # ── I/O (fail-open) ────────────────────────────────────────────────────

    async def sweep(self, dispatch_fn) -> dict:
        """One pass. Returns a counts dict; never raises.

        `dispatch_fn(task_id, snapshot, trigger)` is Apis's dispatch_task
        closure — the SAME entry point the SSE path uses, so the readiness gate
        and the confidence x blast-radius execution gate both still apply and
        high-blast work is held for an operator checkpoint rather than executed.
        """
        now = time.time()
        counts = {
            "scanned": 0,
            "dispatched": 0,
            "dispatch_failed": 0,
            SkipReason.NOT_PENDING.value: 0,
            SkipReason.WITHIN_GRACE.value: 0,
            SkipReason.ALREADY_CLAIMED.value: 0,
            SkipReason.CAP_REACHED.value: 0,
        }
        try:
            tasks = _query_tasks(QUERY_LIMIT)
        except Exception as exc:  # noqa: BLE001 — a query error never kills the loop
            log.warning("[reconciler] task query failed: %s", exc)
            return counts

        for entity_id, snapshot in tasks:
            counts["scanned"] += 1
            status = snapshot.get("status", "")
            age = _age_seconds(snapshot, now)
            title = str(snapshot.get("title", "(untitled)"))[:60]

            skip = self.should_dispatch(entity_id, status, age)

            # Bounded fan-out: an otherwise-eligible task past the cap is
            # DEFERRED, not skipped for cause — it is picked up next pass.
            if skip is None and counts["dispatched"] >= self.max_per_sweep:
                skip = SkipReason.CAP_REACHED

            if skip is not None:
                counts[skip.value] += 1
                # Per-task granularity for the skips that represent a JUDGEMENT
                # about that task (it is owned elsewhere / it may be racing SSE
                # / we already dispatched it). The two bulk reasons log at DEBUG
                # and are reported as totals below instead: NOT_PENDING is every
                # done/routed task in the query window, and CAP_REACHED is the
                # whole undrained backlog — on the ~100-task backlog this fix
                # exists for, that is 95 identical lines per pass, which buries
                # the five that matter.
                _log = (
                    log.debug
                    if skip in (SkipReason.NOT_PENDING, SkipReason.CAP_REACHED)
                    else log.info
                )
                _log(
                    "[reconciler] skip %s (%s): status=%s age=%ss title=%r",
                    entity_id, skip.value, normalize(status) or "(none)",
                    int(age) if age is not None else "unknown", title,
                )
                continue

            # Claim BEFORE dispatching. If the dispatch raises, the task stays
            # claimed for this process — deliberate: a task that crashed the
            # dispatcher must not be re-thrown at it every 15 minutes. The
            # watchdog and the operator own recovery from there.
            self.claim(entity_id)
            log.info(
                "[reconciler] dispatching stranded task %s (age=%ss, pending since "
                "creation, never seen by the event path): %r",
                entity_id, int(age or 0), title,
            )
            try:
                await dispatch_fn(entity_id, snapshot, "reconcile")
                counts["dispatched"] += 1
            except Exception as exc:  # noqa: BLE001 — one bad task never kills the sweep
                counts["dispatch_failed"] += 1
                log.warning(
                    "[reconciler] dispatch of %s failed: %s: %s",
                    entity_id, type(exc).__name__, exc,
                )

        # Always log the pass, even an empty one: "the sweep ran and found
        # nothing" and "the sweep did not run" must not look the same.
        log.info("[reconciler] sweep: %s", counts)
        # The undrained remainder is the one number an operator watching a
        # backlog drain actually wants, and it is the number that says whether
        # the cap is set sensibly. Stated explicitly rather than left to be
        # read out of the counts dict.
        deferred = counts[SkipReason.CAP_REACHED.value]
        if deferred:
            log.info(
                "[reconciler] %s eligible task(s) deferred by the per-sweep cap "
                "(%s) — next pass in %ss",
                deferred, self.max_per_sweep, INTERVAL_SECONDS,
            )
        return counts

    async def run(self, dispatch_fn) -> None:
        """Sweep forever on INTERVAL_SECONDS. Fail-open per iteration."""
        if not ENABLED:
            log.info(
                "[reconciler] DISABLED (APIS_RECONCILE_ENABLED != 1) — stranded "
                "pending tasks will NOT be reconciled. See ateles#586."
            )
            return
        log.info(
            "[reconciler] starting (interval=%ss, cap=%s/sweep, grace=%ss, "
            "query_limit=%s)",
            INTERVAL_SECONDS, self.max_per_sweep, self.grace_seconds, QUERY_LIMIT,
        )
        while True:
            try:
                await self.sweep(dispatch_fn)
            except Exception as exc:  # noqa: BLE001 — never crash the daemon
                log.warning("[reconciler] sweep error (ignored): %s", exc)
            await asyncio.sleep(INTERVAL_SECONDS)


# ── module-level I/O helpers ────────────────────────────────────────────────


def _query_tasks(limit: int) -> list[tuple[str, dict]]:
    """Return [(entity_id, snapshot), …] for tasks, via POST /entities/query.

    Raises on transport error (the caller swallows it). Returns [] when no token.
    """
    if not NEOTOMA_BEARER_TOKEN:
        log.warning("[reconciler] no bearer token — cannot query tasks")
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


# Stamps that live at the ROW level of an EntitySnapshot, as siblings of
# `snapshot` rather than inside it (openapi.yaml: `computed_at` and
# `last_observation_at` are declared alongside `snapshot` on EntitySnapshot).
# Dropping them is not cosmetic: `_age_seconds` looks for exactly these keys,
# so a row whose own snapshot carries no stamp read as ageless, which
# should_dispatch maps to WITHIN_GRACE — skipped on every pass, forever. The
# fail-safe had no expiry because nothing would ever give that row an age, and
# on the measured backlog that was 463 of 474 pending tasks (ateles#598 pm
# lens, confirmed at contract level by the steward).
_ROW_LEVEL_STAMPS = ("last_observation_at", "computed_at")


def _unwrap_snapshot(row: dict) -> dict:
    """Tolerate the several snapshot nesting shapes Neotoma returns.

    Row-level stamps are carried down into the returned dict so the age
    calculation can see them, WITHOUT letting them shadow a stamp the snapshot
    already carries: `updated_at` inside the snapshot is the semantically
    correct signal (it moves when the SSE path writes ROUTED), and the
    row-level stamps are the weaker fallback because they move on any
    observation write, not just a dispatch. `_age_seconds` already encodes that
    precedence; this only ensures the fallbacks are present to be considered.
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


# Timestamp parsing is shared verbatim with the watchdog — same entity, same
# stamp fields, same tolerance for Neotoma's several shapes. `updated_at` is
# preferred there, which is exactly what the grace window needs: that stamp moves
# when the SSE path writes ROUTED, so a task actively being dispatched reads as
# young and stays inside the window.
_age_seconds = _watchdog_age


def _iso_ago(seconds_ago: float) -> str:
    """ISO-8601 UTC stamp `seconds_ago` in the past. Test/diagnostic helper —
    the sweep's age logic is driven entirely by these stamps, so constructing
    them correctly is part of the module's contract rather than the tests'."""
    from datetime import datetime, timezone

    return datetime.fromtimestamp(
        time.time() - seconds_ago, tz=timezone.utc
    ).isoformat()
