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
The ledger is written to disk (`APIS_UNROUTABLE_LEDGER`, default
`~/.local/state/ateles/apis_unroutable.json`) and reloaded at startup, so a
daemon restart does not re-page the operator about the whole standing backlog.

This is load-bearing, not incidental. ateles#636 shipped a digest queue that
looked like it recorded state and had **zero non-test callers of
`flush_digest()`** — state that appears to be kept but never is. An in-process
set would have reproduced exactly that: Apis restarts often, and every restart
would have re-escalated all ~35 tasks. `test_unroutable_ledger.py` asserts
across a simulated restart for that reason.

Writes are atomic (tmp file + `os.replace`) and every I/O path is fail-OPEN: a
corrupt or unwritable ledger degrades to "escalate anyway" (noisy but visible)
rather than crashing the dispatcher or silently swallowing pages.

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
  APIS_UNROUTABLE_LEDGER            Ledger path (default ~/.local/state/ateles/…).
  APIS_UNROUTABLE_WINDOW_SECONDS    Aggregation window (default 300).
  APIS_UNROUTABLE_REASSERT_SECONDS  Re-assert a still-unroutable task (default 86400).
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("apis.unroutable")


class HydrationUnknown(Exception):
    """Raised when routing was asked to decide on a snapshot that was never read.

    Exists to keep "Neotoma did not answer" from being spelled the same way as
    "this task has no tags". The caller defers instead of escalating.
    """


def _default_ledger_path() -> Path:
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
    """Disk-backed dedup + aggregation for no-owner escalations.

    Call `note(...)` for every unroutable task; it returns True when the task is
    newly escalatable. Call `drain(...)` to get the aggregated message for the
    tasks accumulated so far, or None when there is nothing new to say.
    """

    path: Path = field(default_factory=_default_ledger_path)
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
    _last_unread_emit: float = 0.0
    _loaded: bool = False

    def __post_init__(self) -> None:
        # Coerce a str path to Path. Without this, `UnroutableLedger(path="…")`
        # fails every save with `'str' object has no attribute 'parent'` — and
        # because saves are fail-open, it does so QUIETLY: the ledger looks like
        # it is persisting and keeps nothing, so every restart re-pages the whole
        # backlog. Exactly the ateles#636 shape this module exists to avoid.
        if not isinstance(self.path, Path):
            self.path = Path(self.path).expanduser()

    # ── persistence (fail-open) ────────────────────────────────────────────

    def load(self) -> None:
        """Read the ledger from disk. A missing or corrupt file is not an error:
        the daemon starts with an empty ledger and escalates — noisy but
        visible, which is the correct direction to fail."""
        self._loaded = True
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return
        except Exception as exc:  # noqa: BLE001 — corrupt ledger must not crash boot
            log.warning(
                "[unroutable] ledger unreadable at %s (%s) — starting empty; "
                "the standing backlog will be re-escalated once",
                self.path, exc,
            )
            return
        if not isinstance(raw, dict):
            return
        seen = raw.get("tasks")
        if isinstance(seen, dict):
            self._seen = {
                k: v for k, v in seen.items() if isinstance(v, dict) and "fp" in v
            }
        unreadable = raw.get("unreadable")
        if isinstance(unreadable, dict):
            self._unreadable = {
                k: v for k, v in unreadable.items() if isinstance(v, dict)
            }
        roles = raw.get("roles")
        if isinstance(roles, dict):
            self._roles = {
                k: float(v) for k, v in roles.items() if isinstance(v, (int, float))
            }
        log.info(
            "[unroutable] ledger loaded from %s: %s task(s), %s role(s) already "
            "escalated — these will not be re-paged",
            self.path, len(self._seen), len(self._roles),
        )

    def save(self) -> None:
        """Atomically persist. Never raises: losing the ledger costs duplicate
        pages after a restart, which must not be traded for a dead dispatcher."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {
                    "version": 1,
                    "tasks": self._seen,
                    "roles": self._roles,
                    "unreadable": self._unreadable,
                },
                sort_keys=True,
            )
            fd, tmp = tempfile.mkstemp(
                dir=str(self.path.parent), prefix=".apis_unroutable.", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w") as fh:
                    fh.write(payload)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as exc:  # noqa: BLE001
            log.warning("[unroutable] could not persist ledger to %s: %s", self.path, exc)

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
        if not self._loaded:
            self.load()
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
        self.save()
        return True

    def note_undefined_role(self, role: str, now: float | None = None) -> bool:
        """Record that `role` has no agent_definition. True on first sight.

        Deduped on the ROLE, not the task: ten tasks that would route to an
        undefined role are one fact about that role, not ten pages.
        """
        if not self._loaded:
            self.load()
        now = time.time() if now is None else now
        last = self._roles.get(role)
        if last is not None and self.reassert_seconds and (now - last) < self.reassert_seconds:
            log.debug("[unroutable] role %r already reported undefined", role)
            return False
        self._roles[role] = now
        self.save()
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
        if not self._loaded:
            self.load()
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
        self.save()
        log.warning(
            "[unroutable] task %s unreadable after %s attempts — reporting",
            entity_id, rec["n"],
        )
        return True

    def clear_unreadable(self, entity_id: str) -> None:
        """Forget a task that became readable again."""
        if self._unreadable.pop(entity_id, None) is not None:
            self._pending_unreadable.discard(entity_id)
            self.save()

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
