"""
unroutable_ledger.py — Apis unroutable-task escalation ledger + aggregator.

WHY THIS EXISTS
---------------
Apis escalated `Task has no owner — needs routing or assignment` once per
delivered `task.created` event. Measured 2026-09-01: **123 escalations from 35
distinct tasks** (218 `created` events for those 35 — 6.2x duplicate delivery),
sustained at 26-32 pages/hour. That volume was a material contributor to the
500+ emails in 24h and ~70 Telegram notifications/hour.

Two independent defects produced it, and this module addresses the notification
half. The routing half is fixed in `apis.dispatch_task` (see `HydrationUnknown`
below and its caller): a fetch failure during snapshot hydration used to be
indistinguishable from "this task has no tags", so a transient Neotoma 502
caused a fully-tagged, perfectly routable task to be escalated as unroutable.
Task `ent_c192afd8760fd9f3fbd3c08c` has a title, a description and five tags;
Apis escalated it three times, logging its real tags at 16:22:05 and `tags=[]`
at 16:25:25 after a 502.

WHAT IT DOES
------------
Two jobs, deliberately kept in one module because they share the same ledger:

  1. DEDUPLICATE. An unroutable task escalates ONCE. Subsequent cycles are
     silent about that task unless its routing inputs change (see the fingerprint
     below), at which point it is a genuinely new fact and escalates again.

  2. AGGREGATE. Escalations inside a short window are coalesced into a single
     "N tasks are unroutable: ..." report rather than N separate pages.

VISIBILITY IS THE CONSTRAINT THAT MATTERS
-----------------------------------------
Issues #583 and #636 are this codebase's cautionary tale: noise was fixed by
creating silence, and the silence was worse. So:

  - Nothing is ever suppressed outright. A deduplicated task is still counted,
    still logged, and still named in the periodic re-assertion below.
  - Suppression is bounded in TIME, not permanent. `REASSERT_SECONDS` (default
    24h) forces a still-unroutable task back into a report even if nothing about
    it changed, so a standing backlog cannot fade into apparent health.
  - The ledger records why it stayed quiet, so "we suppressed 40 duplicates"
    is an observable number rather than an inference from absent logs.

PERSISTENCE
-----------
State lives in ONE Neotoma `apis_unroutable_ledger` entity (see
`unroutable_store.py`), reloaded at startup so a daemon restart does not re-page
the operator about the whole standing backlog.

This is load-bearing, not incidental. ateles#636 shipped a digest queue that
looked like it recorded state and had **zero non-test callers of
`flush_digest()`** — state that appears to be kept but never is. An in-process
set would have reproduced exactly that: Apis restarts often, and every restart
would have re-escalated all ~35 tasks. `test_unroutable_ledger.py` asserts
across a simulated restart for that reason.

It used to be a JSON file on local disk, and the move is not cosmetic — that
file produced two coordination bugs in its first week. Two writers (`apis.py`
for unroutable tasks, `skill_runner.py` for undefined roles) each held their own
instance and each `save()` wrote its whole stale view back, silently dropping
the other's records; the merge-on-write fix for that could not express a DELETE,
so `clear_unreadable` never persisted until per-field tombstones were added. A
filesystem has no concurrency primitives, so every writer had to reimplement
them. Neotoma resolves identity server-side and appends observations, so two
writers touching one row is the ordinary case rather than a race.

READ FAILURES FAIL CLOSED; WRITE FAILURES FAIL OPEN
---------------------------------------------------
Asymmetric on purpose. A ledger that cannot be READ must never be treated as an
empty one: every standing unroutable task would look new and the whole backlog
would re-page at once — the 131-page flood this module exists to prevent, caused
by the thing meant to prevent it. So `LedgerUnavailable` propagates and the
caller holds the notification. Suppressing a page is recoverable (the task is
still unrouted and is seen next cycle); duplicating one is not.

A failed WRITE is the opposite: it costs at most one duplicate page after a
restart, which must not be traded for a dead dispatcher. Those degrade to a log
line, as they always did.

FINGERPRINTING
--------------
A task is re-escalated when the INPUTS TO THE ROUTING DECISION change — its
tags and assigned_to — not when any field changes. A body edit that leaves it
just as unroutable is not news; acquiring a tag is. The fingerprint deliberately
excludes the title, which Turdus rewrites without changing routability.

UNDEFINED ROLES ARE A DIFFERENT ESCALATION
------------------------------------------
A task that resolves to a role with no `agent_definition` is not an unroutable
task — it is an undefined ROLE, and reporting it once per affected task pages
the operator N times for one underlying fact. `note_undefined_role` dedups on
the role name so that condition escalates once per role.

Environment:
  APIS_UNROUTABLE_WINDOW_SECONDS    Aggregation window (default 300).
  APIS_UNROUTABLE_REASSERT_SECONDS  Re-assert a still-unroutable task (default 86400).
  APIS_UNROUTABLE_LEDGER            Legacy disk ledger. Read ONCE at startup to
                                    migrate prior state into Neotoma; never
                                    written. See `_migrate_from_disk`.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from unroutable_store import (
    FIELDS,
    LedgerUnavailable,
    NeotomaLedgerStore,
)

log = logging.getLogger("apis.unroutable")


class HydrationUnknown(Exception):
    """Raised when routing was asked to decide on a snapshot that was never read.

    Exists to keep "Neotoma did not answer" from being spelled the same way as
    "this task has no tags". The caller defers instead of escalating.
    """


_SHARED: "UnroutableLedger | None" = None


def shared_ledger() -> "UnroutableLedger":
    """The ONE ledger instance for this process.

    Both writers — apis.dispatch_task (unroutable tasks) and skill_runner
    (undefined roles) — go through this. Sharing is no longer load-bearing for
    correctness the way it was on disk, where each instance's `save()` wrote all
    three fields back from its own stale view and silently dropped the other
    writer's records (measured: 2 of 4 roles and 1 of 2 unreadable records lost
    in ~11 minutes). Writes are now per-field and identity resolves server-side,
    so a second instance would converge on the same row rather than clobber it.

    It stays a singleton anyway, for two reasons that still hold: the aggregation
    buffers (`_pending`, `_suppressed`) are per-process and splitting them would
    split one report into two, and one shared read cache means a dispatch cycle
    does no Neotoma I/O at all in the steady state.
    """
    global _SHARED
    if _SHARED is None:
        _SHARED = UnroutableLedger()
    return _SHARED


def _legacy_ledger_path() -> Path:
    """The pre-Neotoma disk ledger. Read once for migration, never written."""
    return Path(
        os.environ.get(
            "APIS_UNROUTABLE_LEDGER",
            str(Path.home() / ".local" / "state" / "ateles" / "apis_unroutable.json"),
        )
    ).expanduser()


# Coalescing window. Escalations arriving within this window are reported as one
# aggregated message. Short enough that a genuinely new unroutable task is
# surfaced promptly; long enough to collapse a burst of redelivered events.
WINDOW_SECONDS = max(0, int(os.environ.get("APIS_UNROUTABLE_WINDOW_SECONDS", "300")))

# A still-unroutable task is re-asserted after this long even with no change, so
# a standing backlog stays visible instead of decaying into silence (#583/#636).
REASSERT_SECONDS = max(
    0, int(os.environ.get("APIS_UNROUTABLE_REASSERT_SECONDS", "86400"))
)

# How many tasks get a full title line in an aggregated report. Beyond this the
# remaining entity IDs are still listed, compactly — never omitted.
MAX_TITLED = 20

# Consecutive failed hydrations before a task is reported as unreadable. Above 1
# so a single transient 502 — the common case — stays quiet, but low enough to
# actually fire: on the measured trace, 7 tasks received only TWO created events
# and both failed, so a threshold of 5 would have reported them never. Every
# task must end up either routed, escalated, or named as unreadable.
UNREADABLE_ATTEMPTS = max(
    1, int(os.environ.get("APIS_UNREADABLE_ATTEMPTS", "2"))
)


def fingerprint(tags, assigned_to) -> str:
    """Stable fingerprint of the ROUTING INPUTS for a task.

    Only tags and assigned_to: those are what `resolve_skill` reads. Title and
    body are excluded on purpose — Turdus rewrites titles without changing
    whether the task can be routed, and re-paging on that is noise.
    """
    try:
        norm_tags = sorted(str(t).strip().lower() for t in (tags or []) if str(t).strip())
    except TypeError:  # tags was not iterable — treat as none
        norm_tags = []
    return json.dumps(
        {"tags": norm_tags, "assigned_to": (assigned_to or "").strip().lower()},
        sort_keys=True,
    )


@dataclass
class _Pending:
    """One task awaiting inclusion in the next aggregated report."""

    entity_id: str
    title: str
    fp: str
    first_seen: float


@dataclass
class UnroutableLedger:
    """Neotoma-backed dedup + aggregation for no-owner escalations.

    Persists through a `NeotomaLedgerStore` (the default). Decision logic —
    dedup, bounded re-assertion, aggregation — is unchanged; only the store
    moved off disk. `APIS_UNROUTABLE_LEDGER` is legacy migration input only
    (`_migrate_from_disk`); it is never written and is not the live backend.

    Call `note(...)` for every unroutable task; it returns True when the task is
    newly escalatable. Call `drain(...)` to get the aggregated message for the
    tasks accumulated so far, or None when there is nothing new to say.
    """

    store: NeotomaLedgerStore = field(default_factory=NeotomaLedgerStore)
    window_seconds: int = WINDOW_SECONDS
    reassert_seconds: int = REASSERT_SECONDS

    # entity_id -> {"fp": str, "last_escalated": float, "count": int}
    _seen: dict[str, dict] = field(default_factory=dict)
    # role name -> last escalation time
    _roles: dict[str, float] = field(default_factory=dict)
    _pending: dict[str, _Pending] = field(default_factory=dict)
    _suppressed: int = 0
    _window_opened: float = 0.0
    # When the last aggregated report went out. Drives the "first report of a
    # quiet period goes immediately, the burst behind it is coalesced" rule.
    _last_emit: float = 0.0
    # entity_id -> {"n": attempts, "reported": ts}
    _unreadable: dict[str, dict] = field(default_factory=dict)
    _pending_unreadable: set = field(default_factory=set)
    # NB: the disk version needed per-field TOMBSTONES here, because merge-on-write
    # unioned the prior file back in and a union cannot express a delete, so
    # `clear_unreadable` never persisted. Neotoma writes the whole map as one
    # observation, so a removed key is simply absent from the next write — the
    # delete is representable and the tombstone machinery is gone.
    _last_unread_emit: float = 0.0
    _loaded: bool = False

    def __post_init__(self) -> None:
        # Accept a str/Path in the `store` slot so a caller that still passes a
        # filesystem path gets a loud, immediate error rather than a ledger that
        # appears to persist and keeps nothing. The disk version failed exactly
        # that way once already: `UnroutableLedger(path="...")` broke every save
        # with `'str' object has no attribute 'parent'`, fail-open swallowed it,
        # and every restart re-paged the whole backlog.
        if not isinstance(self.store, NeotomaLedgerStore):
            raise TypeError(
                "UnroutableLedger.store must be a NeotomaLedgerStore; the ledger "
                f"is no longer disk-backed (got {type(self.store).__name__!r}). "
                "Pass NeotomaLedgerStore(...) — a path here would silently "
                "persist nothing."
            )

    # ── persistence ────────────────────────────────────────────────────────

    def load(self) -> None:
        """Hydrate from Neotoma.

        Raises `LedgerUnavailable` when the read failed. It is NOT caught here:
        an unreadable ledger must not be spelled the same way as an empty one,
        because "empty" means every standing unroutable task is new and the whole
        backlog re-pages at once. `note`/`note_undefined_role`/`note_unreadable`
        let it propagate and their callers hold the notification.
        """
        state = self.store.load()
        self._seen = {
            k: v
            for k, v in (state.get("tasks") or {}).items()
            if isinstance(v, dict) and "fp" in v
        }
        self._roles = {
            k: float(v)
            for k, v in (state.get("roles") or {}).items()
            if isinstance(v, (int, float))
        }
        self._unreadable = {
            k: v
            for k, v in (state.get("unreadable") or {}).items()
            if isinstance(v, dict)
        }
        self._loaded = True
        self._migrate_from_disk()

    def _migrate_from_disk(self) -> None:
        """Carry pre-Neotoma disk state in, once.

        A record left behind on disk is not a cosmetic loss: every dropped entry
        is one re-page of a task the operator has already seen. So the legacy
        file is read and unioned in — Neotoma WINS on every key it already has,
        because it is the newer decision, and disk only fills gaps.

        Deliberately additive and idempotent: after the first migration Neotoma
        holds every key the file did, so re-running unions nothing new. The file
        is never written or deleted — it stays as a manual fallback, and a stale
        copy cannot resurrect state that Neotoma has since changed.
        """
        path = _legacy_ledger_path()
        try:
            raw = json.loads(path.read_text())
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001 — a corrupt legacy file is not fatal
            log.warning(
                "[unroutable] legacy ledger at %s is unreadable (%s) — skipping "
                "migration; anything only recorded there will re-escalate once",
                path, exc,
            )
            return
        if not isinstance(raw, dict):
            return

        migrated = {f: 0 for f in FIELDS}
        for key, mine, valid in (
            ("tasks", self._seen, lambda v: isinstance(v, dict) and "fp" in v),
            ("roles", self._roles, lambda v: isinstance(v, (int, float))),
            ("unreadable", self._unreadable, lambda v: isinstance(v, dict)),
        ):
            theirs = raw.get(key)
            if not isinstance(theirs, dict):
                continue
            for k, v in theirs.items():
                if k in mine or not valid(v):
                    continue
                mine[k] = float(v) if key == "roles" else v
                migrated[key] += 1

        if any(migrated.values()):
            log.info(
                "[unroutable] migrated legacy disk ledger %s into Neotoma: "
                "%d task(s), %d role(s), %d unreadable — these will not be re-paged",
                path, migrated["tasks"], migrated["roles"], migrated["unreadable"],
            )
            for key, value in (
                ("tasks", self._seen),
                ("roles", self._roles),
                ("unreadable", self._unreadable),
            ):
                if migrated[key]:
                    self.store.save_field(key, value)

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def save(self, *fields: str) -> None:
        """Persist the named state maps (all three when unspecified).

        Never raises: a write that cannot land costs at most a duplicate page
        after a restart, which must not be traded for a dead dispatcher.
        """
        payload = {
            "tasks": self._seen,
            "roles": self._roles,
            "unreadable": self._unreadable,
        }
        for name in (fields or FIELDS):
            self.store.save_field(name, payload[name])

    # ── decision logic (unit-tested, no I/O) ───────────────────────────────

    def should_escalate(self, entity_id: str, fp: str, now: float) -> tuple[bool, str]:
        """(escalate?, reason). Pure — the caller owns side effects."""
        prior = self._seen.get(entity_id)
        if prior is None:
            return True, "new"
        if prior.get("fp") != fp:
            # The routing inputs changed. Whatever this task is now, it is not
            # the thing we already paged about.
            return True, "changed"
        last = float(prior.get("last_escalated") or 0.0)
        if self.reassert_seconds and (now - last) >= self.reassert_seconds:
            # Bounded suppression: a standing backlog must not go quiet forever.
            return True, "reassert"
        return False, "duplicate"

    # ── recording ──────────────────────────────────────────────────────────

    def note(
        self, entity_id: str, title: str, tags, assigned_to, now: float | None = None
    ) -> bool:
        """Record an unroutable task. True when it is newly escalatable.

        A True return does NOT send anything — it stages the task for the next
        `drain()`, so a burst becomes one aggregated report.
        """
        self._ensure_loaded()
        now = time.time() if now is None else now
        fp = fingerprint(tags, assigned_to)
        escalate, reason = self.should_escalate(entity_id, fp, now)
        if not escalate:
            self._suppressed += 1
            prior = self._seen.get(entity_id) or {}
            prior["count"] = int(prior.get("count") or 1) + 1
            self._seen[entity_id] = prior
            # DEBUG, not silence: the count above makes the suppression
            # countable, and drain() reports the total in every message.
            log.debug(
                "[unroutable] suppressed duplicate escalation for %s (%s, seen %sx)",
                entity_id, reason, prior.get("count"),
            )
            return False

        self._seen[entity_id] = {
            "fp": fp,
            "last_escalated": now,
            "count": int((self._seen.get(entity_id) or {}).get("count") or 0) + 1,
        }
        if not self._pending:
            self._window_opened = now
        self._pending[entity_id] = _Pending(
            entity_id=entity_id, title=title or "(untitled)", fp=fp, first_seen=now
        )
        log.info(
            "[unroutable] task %s is unroutable (%s) — staged for the next "
            "aggregated report: %r",
            entity_id, reason, (title or "(untitled)")[:60],
        )
        self.save("tasks")
        return True

    def note_undefined_role(self, role: str, now: float | None = None) -> bool:
        """Record that `role` has no agent_definition. True on first sight.

        Deduped on the ROLE, not the task: ten tasks that would route to an
        undefined role are one fact about that role, not ten pages.
        """
        self._ensure_loaded()
        now = time.time() if now is None else now
        last = self._roles.get(role)
        if last is not None and self.reassert_seconds and (now - last) < self.reassert_seconds:
            log.debug("[unroutable] role %r already reported undefined", role)
            return False
        self._roles[role] = now
        self.save("roles")
        log.info("[unroutable] role %r has no agent_definition — escalating once", role)
        return True

    # ── unreadable tasks (hydration failed) ────────────────────────────────
    #
    # A task whose snapshot could not be read is NOT known to be unroutable, so
    # it must not be paged as "no owner". But it must not vanish either: the
    # reconciler sweep that would re-examine it is default-OFF and is off in
    # production today. So track attempts and escalate a task that stays
    # unreadable — a persistent read failure is a real condition (a wedged
    # Neotoma, a deleted entity) and the operator needs to see it once.

    def note_unreadable(self, entity_id: str, now: float | None = None) -> bool:
        """Record a failed hydration. True when this task should be reported.

        Reported only after UNREADABLE_ATTEMPTS failures, so a single transient
        502 stays quiet while a task that is persistently unreadable surfaces.
        """
        self._ensure_loaded()
        now = time.time() if now is None else now
        rec = self._unreadable.setdefault(entity_id, {"n": 0, "reported": 0.0})
        rec["n"] = int(rec.get("n") or 0) + 1
        if rec["n"] < UNREADABLE_ATTEMPTS:
            return False
        last = float(rec.get("reported") or 0.0)
        if last and self.reassert_seconds and (now - last) < self.reassert_seconds:
            return False
        rec["reported"] = now
        self._pending_unreadable.add(entity_id)
        self.save("unreadable")
        log.warning(
            "[unroutable] task %s unreadable after %s attempts — reporting",
            entity_id, rec["n"],
        )
        return True

    def clear_unreadable(self, entity_id: str) -> None:
        """Forget a task that became readable again.

        The whole map is rewritten, so the removed key is simply absent from the
        next observation. On disk this needed a tombstone: merge-on-write unioned
        the prior file back in, a union cannot express a delete, and the clear
        never persisted — the stale streak reloaded on restart and reported on
        the first later blip, which is what clearing exists to prevent.

        Called on every readable snapshot, so it must not pay for a load when
        there is nothing to forget: the in-memory check comes first and an
        unloaded ledger with no record short-circuits.
        """
        if not self._loaded:
            try:
                self.load()
            except LedgerUnavailable:
                # Forgetting is not urgent and there is nothing to page about.
                # The streak stays as it is and the next readable snapshot
                # clears it.
                return
        if self._unreadable.pop(entity_id, None) is not None:
            self._pending_unreadable.discard(entity_id)
            self.save("unreadable")

    def drain_unreadable(self, now: float | None = None, force: bool = False) -> str | None:
        """Aggregated report for tasks whose snapshot cannot be read.

        Windowed like `drain`: a Neotoma outage makes many tasks unreadable at
        once, and one report per task would rebuild the very storm this change
        removes. The periodic flush and shutdown both pass force=True.
        """
        if not self._pending_unreadable:
            return None
        now = time.time() if now is None else now
        if not force and (now - self._last_unread_emit) < self.window_seconds:
            return None
        self._last_unread_emit = now
        ids = sorted(self._pending_unreadable)
        self._pending_unreadable = set()
        n = len(ids)
        return (
            f"{n} task{'s' if n != 1 else ''} could NOT be read from Neotoma "
            f"after {UNREADABLE_ATTEMPTS}+ attempts — not routed, not escalated "
            "as unowned (their snapshots are unknown, not empty):\n"
            + "\n".join("    " + " ".join(ids[i : i + 4]) for i in range(0, n, 4))
        )

    # ── aggregation ────────────────────────────────────────────────────────

    def window_elapsed(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if not self._pending:
            return False
        return (now - self._window_opened) >= self.window_seconds

    def drain(self, now: float | None = None, force: bool = False) -> str | None:
        """Return the aggregated report and clear the buffer, or None.

        `force` emits immediately regardless of the window — used on shutdown so
        a pending report is never lost, which would be silence by accident.
        """
        now = time.time() if now is None else now
        if not self._pending:
            return None
        # Emit at once when forced, when the window has closed, or when this is
        # the opening report of a quiet period. That last case matters: making a
        # genuinely new unroutable task wait a full window would delay the page
        # by minutes for no benefit — it is the BURST BEHIND the opening event
        # that needs coalescing, not the event itself. `_last_emit` is what makes
        # the second and subsequent reports of a burst wait out the window.
        if force or self.window_elapsed(now):
            pass
        elif self._last_emit == 0.0 or (now - self._last_emit) >= self.window_seconds:
            pass  # opening report of a quiet period — send immediately
        else:
            return None

        items = sorted(self._pending.values(), key=lambda p: p.first_seen)
        suppressed = self._suppressed
        self._pending = {}
        self._suppressed = 0
        self._window_opened = 0.0
        self._last_emit = now

        n = len(items)
        head = (
            f"{n} task{'s' if n != 1 else ''} unroutable — no owner; "
            "needs routing or assignment"
        )
        # Titles are truncated past MAX_TITLED, but every entity_id is ALWAYS
        # listed. Dropping ids past a cap would make the operator unable to act
        # on the tail of a backlog — reduced volume is the goal, reduced
        # information is the #583/#636 failure wearing a different hat.
        titled = items[:MAX_TITLED]
        lines = [f"  • {p.title[:70]}\n    {p.entity_id}" for p in titled]
        rest = items[MAX_TITLED:]
        if rest:
            lines.append(f"  … and {len(rest)} more:")
            lines.extend(
                "    " + " ".join(p.entity_id for p in rest[i : i + 4])
                for i in range(0, len(rest), 4)
            )
        if suppressed:
            # Say what was NOT sent. A quiet dispatcher and a deduplicating one
            # must not look the same from the outside.
            lines.append(
                f"  ({suppressed} duplicate escalation(s) suppressed this window)"
            )
        return head + "\n" + "\n".join(lines)
