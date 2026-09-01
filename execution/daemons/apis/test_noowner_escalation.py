"""
test_noowner_escalation.py — the no-owner escalation path, through dispatch_task.

Driven through the REAL entrypoint rather than the ledger's helpers. The guard
being tested here is precisely the kind that a unit test of the helper would
pass while the wired-up path still escalated: `snapshot_hydrated` has to be
threaded from the SSE handler all the way into the `skill is None` branch, and
only an end-to-end call proves that.

The measured defect (2026-09-01, apis.log): 123 escalations from 35 distinct
tasks; 218 `task.created` events for those 35; and
ent_c192afd8760fd9f3fbd3c08c — which has a title, a description and five tags —
escalated three times, its real tags logged at 16:22:05 and `tags=[]` at
16:25:25 straight after a 502 on the hydration GET.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
from unroutable_ledger import UnroutableLedger  # noqa: E402


class _Notifier:
    """Captures sends so the test can count PAGES, not log lines."""

    def __init__(self):
        self.sent: list[str] = []

    def send(self, message, priority=None, handler=None):
        self.sent.append(message)


@pytest.fixture(autouse=True)
def _isolated_ledger(monkeypatch, tmp_path):
    """Every test gets its own on-disk ledger — never the operator's real one."""
    monkeypatch.setattr(apis, "_unroutable", UnroutableLedger(path=tmp_path / "l.json"))
    monkeypatch.setattr(apis, "_created_seen", {})


@pytest.fixture(autouse=True)
def _no_neotoma_writes(monkeypatch):
    """dispatch_task writes task status; keep that off the network."""
    monkeypatch.setattr(apis, "set_task_status", lambda *a, **k: True)


def _dispatch(entity_id, snapshot, notifier, hydrated=True):
    asyncio.run(
        apis.dispatch_task(
            entity_id, snapshot, trigger="created",
            notifier=notifier, snapshot_hydrated=hydrated,
        )
    )


# ── the hydration guard ──────────────────────────────────────────────────────


def test_unhydrated_snapshot_does_not_escalate():
    """A failed read must never be reported as 'this task has no owner'."""
    n = _Notifier()
    _dispatch("ent_c192afd8760fd9f3fbd3c08c", {}, n, hydrated=False)
    assert n.sent == [], f"escalated on an unread snapshot: {n.sent}"


def test_unhydrated_snapshot_does_not_mark_the_task_blocked(monkeypatch):
    """Blocking on a failed read would strand a routable task for the operator."""
    calls: list = []
    monkeypatch.setattr(apis, "set_task_status", lambda *a, **k: calls.append(a))
    _dispatch("ent_x", {}, _Notifier(), hydrated=False)
    assert calls == []


def test_genuinely_unroutable_task_still_escalates():
    """The guard must not become a blanket silence — read OK, no owner, page."""
    n = _Notifier()
    _dispatch("ent_x", {"title": "Something with no domain words", "tags": []}, n)
    assert len(n.sent) == 1
    assert "unroutable" in n.sent[0]
    assert "ent_x" in n.sent[0]


def test_a_routable_task_is_never_escalated(monkeypatch):
    """Sanity: routing itself works. Regression guard on the tables."""
    monkeypatch.setattr(apis, "_activity", _FakeActivity())
    n = _Notifier()
    # Routes to cicada via the ops/engineering patterns.
    snapshot = {"title": "Fix the flaky CI pipeline", "tags": ["ops"]}
    with pytest.raises(_Stop):
        _dispatch("ent_ok", snapshot, n)
    assert not any("unroutable" in m for m in n.sent)


class _Stop(Exception):
    """Halts dispatch once routing has demonstrably succeeded."""


class _FakeActivity:
    def started(self, *a, **k):
        raise _Stop()


# ── dedup through the real dispatcher ────────────────────────────────────────


def test_same_task_escalates_once_across_redelivered_events():
    n = _Notifier()
    snapshot = {"title": "No owner here", "tags": []}
    for _ in range(6):  # the measured ~6.2x redelivery
        _dispatch("ent_dup", snapshot, n)
    assert len(n.sent) == 1, f"expected one page, got {len(n.sent)}: {n.sent}"


def test_measured_burst_collapses_from_123_pages_to_a_handful():
    """35 distinct tasks x ~6 redeliveries — the actual observed shape.

    The live defect produced 123 pages for this. Aggregation means the whole
    burst arrives as one opening report plus whatever the flush loop emits, and
    every task must still be NAMED — reduced volume, not reduced information.
    """
    n = _Notifier()
    for task in range(35):
        for _ in range(6):
            _dispatch(f"ent_{task}", {"title": f"task {task}", "tags": []}, n)
    # Whatever is still buffered is delivered by the periodic flush.
    tail = apis._unroutable.drain(force=True)
    if tail:
        n.sent.append(tail)

    assert len(n.sent) < 35, f"expected aggregation, got {len(n.sent)} pages"
    combined = "\n".join(n.sent)
    reported = set(re.findall(r"ent_\d+", combined))
    missing = {f"ent_{i}" for i in range(35)} - reported
    assert not missing, f"unroutable tasks went unreported: {sorted(missing)}"


def test_a_new_unroutable_task_is_still_reported():
    """Dedup must not swallow a genuinely NEW task — the #583/#636 failure."""
    n = _Notifier()
    for _ in range(5):
        _dispatch("ent_old", {"title": "old", "tags": []}, n)
    _dispatch("ent_brand_new", {"title": "brand new", "tags": []}, n)
    tail = apis._unroutable.drain(force=True)
    if tail:
        n.sent.append(tail)
    combined = "\n".join(n.sent)
    assert "ent_brand_new" in combined, "a new unroutable task went unreported"


def test_escalation_dedup_survives_a_restart(tmp_path, monkeypatch):
    """A restart must not re-page the operator about the standing backlog."""
    path = tmp_path / "ledger.json"
    snapshot = {"title": "No owner", "tags": []}

    monkeypatch.setattr(apis, "_unroutable", UnroutableLedger(path=path))
    n1 = _Notifier()
    _dispatch("ent_a", snapshot, n1)
    assert len(n1.sent) == 1

    # Restart: fresh ledger object, fresh created-seen set, same file.
    monkeypatch.setattr(apis, "_unroutable", UnroutableLedger(path=path))
    monkeypatch.setattr(apis, "_created_seen", {})
    n2 = _Notifier()
    _dispatch("ent_a", snapshot, n2)
    assert n2.sent == [], "re-paged the whole backlog after a restart"


# ── unreadable tasks must not silently vanish ────────────────────────────────
#
# The hydration guard defers instead of escalating. The reconciler sweep that
# would otherwise re-examine such a task is DEFAULT-OFF and is off in production
# today, so deferring without tracking would put the task on the floor.


def test_one_transient_read_failure_stays_quiet():
    n = _Notifier()
    _dispatch("ent_x", {}, n, hydrated=False)
    assert n.sent == []


def test_persistently_unreadable_task_is_eventually_reported():
    n = _Notifier()
    for _ in range(8):
        _dispatch("ent_stuck", {}, n, hydrated=False)
    combined = "\n".join(n.sent) + (apis._unroutable.drain_unreadable() or "")
    assert "ent_stuck" in combined, "a permanently unreadable task was never reported"
    assert "could NOT be read" in combined


def test_unreadable_report_is_not_an_unowned_escalation():
    """It must not claim the task has no owner — that is not known."""
    n = _Notifier()
    for _ in range(8):
        _dispatch("ent_stuck", {}, n, hydrated=False)
    combined = "\n".join(n.sent) + (apis._unroutable.drain_unreadable() or "")
    assert "unroutable — no owner" not in combined


def test_recovery_clears_the_unreadable_streak():
    """A task that becomes readable must not inherit its old failure streak."""
    n = _Notifier()
    for _ in range(4):
        _dispatch("ent_flaky", {}, n, hydrated=False)
    _dispatch("ent_flaky", {"title": "now readable", "tags": []}, n, hydrated=True)
    apis._created_seen.clear()
    for _ in range(3):
        _dispatch("ent_flaky", {}, n, hydrated=False)
    assert apis._unroutable.drain_unreadable() is None


def test_a_failed_first_event_does_not_claim_the_entity():
    """The idempotency key must only be claimed by an event we actually handled.

    Found by replaying the real trace: 14 of 37 tasks had their FIRST
    `task.created` fail hydration. Claiming the entity on that failure made every
    later, readable redelivery a no-op and dropped all 14 tasks silently.
    """
    n = _Notifier()
    # First delivery fails to hydrate…
    _dispatch("ent_late", {}, n, hydrated=False)
    assert n.sent == []
    # …the redelivery reads fine and MUST still be processed.
    _dispatch("ent_late", {"title": "readable now", "tags": []}, n, hydrated=True)
    combined = "\n".join(n.sent) + (apis._unroutable.drain(force=True) or "")
    assert "ent_late" in combined, "task dropped after a failed first event"


def test_handle_event_idempotency_only_claims_hydrated_events(monkeypatch):
    """Through handle_event itself — where the `_seen_created` guard actually lives."""
    from lib.daemon_runtime.sse_client import NeotomaEvent

    seen: list = []

    async def _fake_dispatch(entity_id, snapshot, trigger, notifier, **kw):
        seen.append((entity_id, kw.get("snapshot_hydrated")))

    monkeypatch.setattr(apis, "dispatch_task", _fake_dispatch)
    # hydrate_snapshot would hit the network; the events below carry their own state.
    async def _noop_hydrate(ev):
        return ev

    monkeypatch.setattr(apis, "hydrate_snapshot", _noop_hydrate)
    n = _Notifier()

    failed = NeotomaEvent(entity_type="task", entity_id="ent_z", action="created")
    failed.hydrated = False
    asyncio.run(apis.handle_event(failed, n))

    ok = NeotomaEvent(
        entity_type="task", entity_id="ent_z", action="created",
        snapshot={"title": "readable", "tags": []},
    )
    ok.hydrated = True
    asyncio.run(apis.handle_event(ok, n))

    assert ("ent_z", True) in seen, "the readable redelivery was skipped"

    # A second READABLE delivery is a true duplicate and must be collapsed.
    asyncio.run(apis.handle_event(ok, n))
    assert sum(1 for e, h in seen if e == "ent_z" and h) == 1


def test_unhydrated_redeliveries_do_not_repeat_the_created_notice(monkeypatch):
    """Loxia review: a task whose early deliveries all 502 emitted one
    'Task created: (untitled)' INFO page per redelivery, because the announce
    was gated on the dispatch claim, which an unhydrated event must not take."""
    from lib.daemon_runtime.sse_client import NeotomaEvent

    async def _noop_hydrate(ev):
        return ev

    async def _noop_dispatch(*a, **k):
        return None

    monkeypatch.setattr(apis, "hydrate_snapshot", _noop_hydrate)
    monkeypatch.setattr(apis, "dispatch_task", _noop_dispatch)
    monkeypatch.setattr(apis, "_announced", {})
    n = _Notifier()

    for _ in range(5):
        ev = NeotomaEvent(entity_type="task", entity_id="ent_502", action="created")
        ev.hydrated = False
        asyncio.run(apis.handle_event(ev, n))

    created = [m for m in n.sent if m.startswith("Task created")]
    assert len(created) == 1, f"announced {len(created)} times: {created}"


def test_unreadable_announce_is_upgraded_by_the_readable_copy(monkeypatch):
    """~38% of tasks (14 of 37 on the measured trace) had their first created
    event fail hydration. Announcing that copy and suppressing every later one
    would pin the operator to a permanent 'Task created: (untitled)'."""
    from lib.daemon_runtime.sse_client import NeotomaEvent

    async def _noop_hydrate(ev):
        return ev

    async def _noop_dispatch(*a, **k):
        return None

    monkeypatch.setattr(apis, "hydrate_snapshot", _noop_hydrate)
    monkeypatch.setattr(apis, "dispatch_task", _noop_dispatch)
    monkeypatch.setattr(apis, "_announced", {})
    n = _Notifier()

    for _ in range(4):  # unreadable deliveries
        ev = NeotomaEvent(entity_type="task", entity_id="ent_u", action="created")
        ev.hydrated = False
        asyncio.run(apis.handle_event(ev, n))

    ok = NeotomaEvent(
        entity_type="task", entity_id="ent_u", action="created",
        snapshot={"title": "The Real Title", "tags": []},
    )
    ok.hydrated = True
    asyncio.run(apis.handle_event(ok, n))

    created = [m for m in n.sent if m.startswith("Task created")]
    assert len(created) == 2, f"expected provisional + upgrade, got {created}"
    assert "The Real Title" in created[-1]


def test_a_readable_announce_is_final(monkeypatch):
    """No third notice: once the real title is announced, later copies are quiet."""
    from lib.daemon_runtime.sse_client import NeotomaEvent

    async def _noop_hydrate(ev):
        return ev

    async def _noop_dispatch(*a, **k):
        return None

    monkeypatch.setattr(apis, "hydrate_snapshot", _noop_hydrate)
    monkeypatch.setattr(apis, "dispatch_task", _noop_dispatch)
    monkeypatch.setattr(apis, "_announced", {})
    monkeypatch.setattr(apis, "_created_seen", {})
    n = _Notifier()

    for _ in range(4):
        ev = NeotomaEvent(
            entity_type="task", entity_id="ent_r", action="created",
            snapshot={"title": "Title", "tags": []},
        )
        ev.hydrated = True
        asyncio.run(apis.handle_event(ev, n))
        apis._created_seen.clear()   # isolate the announce from the dispatch claim

    created = [m for m in n.sent if m.startswith("Task created")]
    assert len(created) == 1, f"announced {len(created)} times: {created}"
