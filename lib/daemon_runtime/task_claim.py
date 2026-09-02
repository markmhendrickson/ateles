"""Atomic claim + lease: the pull-model primitive.

Agents CLAIM work from a queue rather than a router assigning owners. One
mechanism does two jobs:

  * it SERIALIZES concurrent claimants (two agents reading one queue must not
    both take a task), and
  * it EXPIRES when a runner dies (a killed process writes nothing, so its
    lease simply lapses).

Vocabulary: created / claimed / running / released.

LIVENESS IS DERIVED AT READ TIME, NEVER A STORED FLAG
-----------------------------------------------------
`status == "running"` is not trusted. A claim is live iff

    holder is set AND last_activity_at is within the lease window

A SIGKILLed runner writes nothing further, `last_activity_at` goes stale, and
the lease lapses without any cooperation from the dying process. That inversion
is what `participation_record` got wrong: it required a terminal write to
arrive, and 143 rows are stranded because those writes fail silently.

WHY THE CLAIM IS KEYED ON THE TASK, NOT THE RUNNER
---------------------------------------------------
The design proposed keying `agent_session` on (harness, native_session_id) of
the RUNNER, relying on `name_collision_policy: reject` to fail the loser. That
was probed against prod and DOES NOT HOLD. Two stores against one canonical key
returned:

    claimant A -> action="created"          entity ent_b1214be…
    claimant B -> action="matched_existing" entity ent_b1214be…  (same row)

B received a SUCCESS response and its fields OVERWROTE A's (verified by
read-back: the snapshot showed B's title). `reject` de-duplicates into a single
row; it does not raise for the second writer. Had the claim been built on that
assumption, both agents would have believed they held the task — precisely the
failure this primitive exists to prevent.

So the canonical key is the TASK (`native_session_id = "task:<entity_id>"`),
which makes competing claimants collide on one row by construction, and the
winner is decided by two signals together:

  1. `action` on the store response — "created" means the row did not exist.
  2. A READ-BACK of the holder field — because the snapshot is last-writer-wins,
     `action` alone is not sufficient; a late writer can still stomp the row.

A claimant holds the task only if, after writing, the persisted holder is its
own runner id. Anything else means it lost the race and must not start work.

FAIL-CLOSED
-----------
If the claim cannot be written or verified, the agent MUST NOT start work
(operator decision ent_670cacab2f46fd9547ced7ed, shipped as ateles#714).
Working without a claim recreates the untracked population this eliminates, and
under pull it also breaks mutual exclusion. Every failure path here returns a
non-held Claim; none of them fall through to "proceed anyway".
"""

from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone

log = logging.getLogger("ateles.claim")

# Canonical harness value for claim rows, so claims are distinguishable from
# the ordinary interactive agent_session rows sharing this entity type.
CLAIM_HARNESS = "ateles-claim"

# How long a claim stays valid without a heartbeat. A claim is live only while
# last_activity_at is within this window, so this is the maximum time a killed
# runner can pin a task. Far shorter than the watchdog's 3600s STALL_SECONDS
# because a lease is a real signal rather than a proxy for "recently touched".
LEASE_SECONDS = max(60, int(os.environ.get("ATELES_LEASE_SECONDS", "900")))

# Heartbeat cadence. Must be comfortably under LEASE_SECONDS so an alive runner
# never lets its own lease lapse.
HEARTBEAT_SECONDS = max(15, int(os.environ.get("ATELES_HEARTBEAT_SECONDS", str(LEASE_SECONDS // 3))))


# ── claimable-status predicate ───────────────────────────────────────────────
#
# THE TRAP: live prod rows carry statuses that are NOT TaskStatus members. A
# 500-row sample of the 21,373 task entities showed:
#
#     completed  329      <- NOT a TaskStatus member; TaskStatus.DONE is "done" (7 rows)
#     pending     87
#     open        31      <- not a member
#     (unset)     19
#     done         7
#     in_progress  6      <- not a member
#     canceled     6      <- not a member
#     todo         5      <- not a member
#     blocked      4
#     awaiting_input 3
#     awaiting_release_confirmation 1, queued 1, awaiting_approval 1
#
# `task_lifecycle.normalize()` only lowercases/strips — it does NOT map
# "completed" onto "done", so "completed" never matches TERMINAL. A predicate
# written against the enum would treat all 329 completed tasks as claimable and
# re-run finished work; one written as "status in ACTIVE" would skip `open`,
# `todo` and `queued` — the very backlog this exists to drain.
#
# So the predicate is expressed over the REAL vocabulary, as an explicit
# DENY-list of terminal/owned spellings plus an explicit allow-list, and
# anything unrecognised is treated as NOT claimable (fail-closed: never
# speculatively re-run work whose state we cannot name).

CLAIMABLE_STATUSES: frozenset[str] = frozenset(
    {
        "pending",    # TaskStatus.PENDING — created, not yet routed
        "open",       # live prod spelling, outside the enum
        "todo",       # live prod spelling, outside the enum
        "queued",     # live prod spelling, outside the enum
        "failed",     # TaskStatus.FAILED — transient; retryable
        "routed",     # owner resolved but never picked up (daemon died mid-flight)
        "",           # unset status: created but never given one
    }
)

# Terminal or human-owned: never claimable. Kept explicit (rather than derived
# from TERMINAL) so the non-enum prod spellings are covered by name.
UNCLAIMABLE_STATUSES: frozenset[str] = frozenset(
    {
        "done", "completed", "verified",          # success (note: BOTH spellings)
        "declined", "superseded", "canceled", "cancelled",
        "blocked",                                 # already escalated to operator
        "awaiting_approval", "awaiting_input",     # operator-owned
        "awaiting_release_confirmation",
        "in_progress",                             # someone is on it
        "executing",                               # covered by lease check instead
    }
)


def is_claimable_status(status: str | None) -> bool:
    """True when a task's status means the work is available to be taken."""
    s = (status or "").strip().lower()
    if s in UNCLAIMABLE_STATUSES:
        return False
    return s in CLAIMABLE_STATUSES


def _now() -> float:
    return time.time()


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_ts(ts) -> float | None:
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return None
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def claim_key(task_id: str) -> str:
    """Canonical native_session_id for a task's claim row.

    Keying on the TASK (not the runner) is what makes competing claimants
    collide on a single row instead of each creating their own.
    """
    return f"task:{task_id}"


def new_runner_id() -> str:
    """A self-minted identity for this run.

    The Claude CLI's own session id is not available at spawn time
    (skill_runner.py:368-370), so the claim mints its own and the CLI id can be
    backfilled later. Includes host so "which machine holds this" is answerable
    for the cloud move.
    """
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:12]}"


def lease_is_live(holder: str | None, last_activity_at, now: float | None = None,
                  lease_seconds: int = LEASE_SECONDS) -> bool:
    """Derive liveness at READ TIME. Never consults a stored 'running' flag.

    A claim is live iff a holder is recorded AND its heartbeat is within the
    lease window. A killed runner stops writing, so this goes False on its own.
    """
    if not holder:
        return False
    ts = _parse_ts(last_activity_at)
    if ts is None:
        return False
    return ((now if now is not None else _now()) - ts) < lease_seconds


@dataclass
class Claim:
    """The outcome of attempting to claim one task."""

    task_id: str
    runner_id: str
    held: bool
    reason: str = ""
    entity_id: str | None = None
    holder: str | None = None
    acquired_at: float = field(default_factory=_now)

    @property
    def lost_race(self) -> bool:
        return not self.held and self.reason == "held_by_other"


class ClaimStore:
    """Neotoma-backed claim rows. Injected so tests can drive it in-memory.

    `store_fn(entities, idempotency_key) -> dict` mirrors Neotoma's /store.
    `read_fn(entity_id) -> dict` returns the entity snapshot.
    """

    def __init__(self, store_fn, read_fn, lease_seconds: int = LEASE_SECONDS,
                 now_fn=_now):
        self._store = store_fn
        self._read = read_fn
        self._lease_seconds = lease_seconds
        self._now = now_fn

    # ── claim ────────────────────────────────────────────────────────────────

    def acquire(self, task_id: str, runner_id: str, *, context: dict | None = None) -> Claim:
        """Attempt to take a task. Fail-closed: any error yields held=False.

        Two-phase, because Neotoma's canonical-key collision de-duplicates onto
        one row rather than rejecting the loser, and the snapshot is
        last-writer-wins:

          1. Read the existing claim. If another runner's lease is still live,
             lose immediately without writing (so we never stomp a live holder).
          2. Write our claim, then READ BACK. We hold the task only if the
             persisted holder is us.
        """
        key = claim_key(task_id)
        now = self._now()

        # Phase 1 — is someone already holding this?
        try:
            existing = self._read_claim(key)
        except Exception as exc:  # noqa: BLE001 — fail closed
            log.warning("[claim] pre-read failed for %s: %s", task_id, exc)
            return Claim(task_id, runner_id, False, reason="read_failed")

        if existing:
            holder = existing.get("holder")
            if holder and holder != runner_id and lease_is_live(
                holder, existing.get("last_activity_at"), now, self._lease_seconds
            ):
                return Claim(
                    task_id, runner_id, False, reason="held_by_other",
                    entity_id=existing.get("entity_id"), holder=holder,
                )
            # Holder absent, stale (lease lapsed → released), or already us:
            # the task is available to take.

        # Phase 2 — write our claim, then verify we actually own the row.
        payload = {
            "entity_type": "agent_session",
            "harness": CLAIM_HARNESS,
            "native_session_id": key,
            "kind": "claim",
            "status": "running",
            "title": f"claim {task_id}",
            # `holder` and `task_id` are not declared on agent_session (31
            # fields, verified via describe_entity_type); Neotoma stores
            # undeclared keys as raw_fragments and returns them on read, which
            # is how the holder survives the round-trip.
            "holder": runner_id,
            "task_id": task_id,
            "last_activity_at": _iso(now),
            "created_at": _iso(now),
        }
        if context:
            # Identity half: which host / checkout holds this. Required for the
            # cloud move and not inferable from "it's my machine".
            for k in ("cwd", "repo", "repo_remote_url", "branch", "git_head_sha",
                      "worktree_path", "origin_device", "trigger_kind", "trigger_ref"):
                if context.get(k):
                    payload[k] = context[k]

        try:
            resp = self._store(
                [payload],
                idempotency_key=f"claim-{task_id}-{runner_id}",
            )
        except Exception as exc:  # noqa: BLE001 — fail closed
            log.warning("[claim] store failed for %s: %s", task_id, exc)
            return Claim(task_id, runner_id, False, reason="store_failed")

        entity_id = _entity_id_of(resp)

        # Read-back verification. `action == "created"` is NOT sufficient on its
        # own: a concurrent writer that arrives after us matches the same row
        # and overwrites the holder. Only the persisted value decides.
        try:
            after = self._read_claim(key) or {}
        except Exception as exc:  # noqa: BLE001 — fail closed
            log.warning("[claim] verify read failed for %s: %s", task_id, exc)
            return Claim(task_id, runner_id, False, reason="verify_failed",
                         entity_id=entity_id)

        winner = after.get("holder")
        if winner != runner_id:
            return Claim(task_id, runner_id, False, reason="held_by_other",
                         entity_id=entity_id or after.get("entity_id"), holder=winner)

        return Claim(task_id, runner_id, True, reason="acquired",
                     entity_id=entity_id or after.get("entity_id"),
                     holder=runner_id, acquired_at=now)

    # ── heartbeat / release ──────────────────────────────────────────────────

    def heartbeat(self, claim: Claim) -> bool:
        """Extend the lease. Returns False if we no longer hold the task."""
        if not claim.held:
            return False
        key = claim_key(claim.task_id)
        try:
            self._store(
                [{
                    "entity_type": "agent_session",
                    "harness": CLAIM_HARNESS,
                    "native_session_id": key,
                    "holder": claim.runner_id,
                    "task_id": claim.task_id,
                    "status": "running",
                    "last_activity_at": _iso(self._now()),
                }],
                idempotency_key=f"claim-hb-{claim.task_id}-{claim.runner_id}-{int(self._now())}",
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("[claim] heartbeat failed for %s: %s", claim.task_id, exc)
            return False

    def release(self, claim: Claim, *, outcome: str = "released") -> bool:
        """Give the claim back so the task is claimable again.

        This is the OPTIMISATION that closes the window fast on a clean exit.
        It is explicitly NOT what makes the design correct — a SIGKILL never
        runs it, and the lapsing lease covers that case.
        """
        if not claim.held:
            return False
        key = claim_key(claim.task_id)
        try:
            self._store(
                [{
                    "entity_type": "agent_session",
                    "harness": CLAIM_HARNESS,
                    "native_session_id": key,
                    "holder": "",          # clearing the holder frees the task
                    "task_id": claim.task_id,
                    "status": outcome,
                    "last_activity_at": _iso(self._now()),
                }],
                idempotency_key=f"claim-rel-{claim.task_id}-{claim.runner_id}",
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("[claim] release failed for %s: %s", claim.task_id, exc)
            return False

    def release_expired(self, task_id: str) -> bool:
        """Clear a lapsed claim so the task is cleanly claimable again.

        Called by the watchdog, NOT by the dead runner (which by definition
        writes nothing). Refuses to touch a claim whose lease is still live, so
        a slow sweep can never yank a task away from a healthy runner.
        """
        row = self.inspect(task_id)
        if row is None:
            return False
        if row.get("live"):
            return False
        try:
            self._store(
                [{
                    "entity_type": "agent_session",
                    "harness": CLAIM_HARNESS,
                    "native_session_id": claim_key(task_id),
                    "holder": "",
                    "task_id": task_id,
                    "status": "expired",
                    "last_activity_at": _iso(self._now()),
                }],
                idempotency_key=f"claim-expire-{task_id}-{int(self._now())}",
            )
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("[claim] expiring %s failed: %s", task_id, exc)
            return False

    # ── read ─────────────────────────────────────────────────────────────────

    def inspect(self, task_id: str) -> dict | None:
        """Current claim row for a task, or None. Adds a derived `live` flag."""
        row = self._read_claim(claim_key(task_id))
        if row is None:
            return None
        row = dict(row)
        row["live"] = lease_is_live(
            row.get("holder"), row.get("last_activity_at"),
            self._now(), self._lease_seconds,
        )
        return row

    def _read_claim(self, key: str) -> dict | None:
        raw = self._read(key)
        if not raw:
            return None
        snap = raw.get("snapshot") if isinstance(raw, dict) else None
        if isinstance(snap, dict):
            inner = snap.get("snapshot")
            out = dict(inner if isinstance(inner, dict) else snap)
        else:
            out = dict(raw)
        if isinstance(raw, dict) and raw.get("entity_id"):
            out.setdefault("entity_id", raw["entity_id"])
        return out


def _entity_id_of(resp) -> str | None:
    if not isinstance(resp, dict):
        return None
    ents = resp.get("entities")
    if isinstance(ents, list) and ents:
        first = ents[0]
        if isinstance(first, dict):
            return first.get("entity_id")
    return resp.get("entity_id")


@dataclass
class _CtxGuard:
    store: ClaimStore
    claim: Claim

    def __enter__(self) -> Claim:
        return self.claim

    def __exit__(self, exc_type, exc, tb) -> bool:
        # try/finally closes the window on a clean or excepting exit. It cannot
        # cover SIGKILL — which is exactly why the lease, not this, is what
        # makes the design correct.
        if self.claim.held:
            self.store.release(
                self.claim, outcome="failed" if exc_type else "released"
            )
        return False


def claimed(store: ClaimStore, task_id: str, runner_id: str, *,
            context: dict | None = None) -> _CtxGuard:
    """Context manager: acquire a claim and always release it on exit.

    Fail-closed — inspect `.held` before doing any work:

        with claimed(store, task_id, runner_id) as claim:
            if not claim.held:
                return          # someone else has it, or the claim failed
            ...
    """
    return _CtxGuard(store, store.acquire(task_id, runner_id, context=context))
