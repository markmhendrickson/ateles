"""A task planning returns to `backlog` is kept and left alone by every automatic path.

Planning resets stale `in_progress` tasks (no executor, untouched for days). A
reset to `pending`/`open`/empty reads as never started, and Neotoma redelivers
`task.created` after the correction, so the reset re-entered `dispatch_task`:
a task with no route ended as a no-owner escalation or an operator checkpoint.
`backlog` is the kept-but-not-dispatched status; only an explicit move back to
`pending` makes the task dispatchable again.

These tests assert the effect on each automatic path: no route, no status
write, no checkpoint brief, no spawn, no notice.

Run: pytest execution/daemons/apis/test_backlog_task_never_dispatches.py -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "codex-delivery-check"))

import apis  # noqa: E402
import codex_delivery_check as cdc  # noqa: E402
import task_reconciler as tr  # noqa: E402
import task_watchdog as tw  # noqa: E402
from lib.daemon_runtime.sse_client import NeotomaEvent  # noqa: E402
from lib.daemon_runtime.task_lifecycle import can_transition, is_backlog  # noqa: E402
from unroutable_ledger import UnroutableLedger  # noqa: E402

_OLD = 10 * 24 * 3600.0  # untouched for ten days
_REAL_REREAD = apis._task_status_still  # conftest makes it inert per test


class _Notifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message, priority=None, handler=None, **kwargs):
        self.sent.append(message)

    def clear_dedupe(self, key):
        pass


class _SpawnResult:
    ok = True
    pr_url = None
    detail = "stubbed"


def _stale(status: str) -> dict:
    """A stale task with no assignee and tags nothing routes."""
    return {
        "title": "Water the plants on the terrace",
        "body": "Synthetic stale task.",
        "tags": ["misc", "someday"],
        "status": status,
        "updated_at": tr._iso_ago(_OLD),
    }


@pytest.fixture
def effects(monkeypatch, tmp_path):
    """Capture every effect dispatch can have, and keep everything off the network."""
    ledger = UnroutableLedger(path=tmp_path / "l.json")
    monkeypatch.setattr(apis, "_unroutable", ledger)
    monkeypatch.setattr(apis, "_created_seen", {})
    monkeypatch.setattr(apis, "_OWNERSHIP_RECHECK_ENABLED", False)
    out = {"writes": [], "briefs": [], "spawns": [], "ledger": ledger, "notifier": _Notifier()}

    def _write(entity_id, status, **kw):
        out["writes"].append((entity_id, getattr(status, "value", status)))
        return True

    async def _spawn(skill, entity_id, *a, **kw):
        out["spawns"].append((skill, entity_id))
        return _SpawnResult()

    monkeypatch.setattr(apis, "set_task_status", _write)
    monkeypatch.setattr(
        apis, "write_checkpoint_brief",
        lambda **kw: out["briefs"].append(kw.get("task_entity_id")) or "ent_brief",
    )
    monkeypatch.setattr(apis, "_spawn_harness_skill", _spawn)

    async def _noop_hydrate(ev):
        ev.hydrated = True
        return ev

    monkeypatch.setattr(apis, "hydrate_snapshot", _noop_hydrate)
    return out


def _assert_left_alone(out, where: str):
    assert out["writes"] == [], f"{where}: status written on a backlog task: {out['writes']}"
    assert out["briefs"] == [], f"{where}: checkpoint brief for a backlog task"
    assert out["spawns"] == [], f"{where}: agent spawned on a backlog task"
    assert out["notifier"].sent == [], f"{where}: noticed a backlog task: {out['notifier'].sent}"
    assert out["ledger"].drain() in (None, "", []), f"{where}: backlog task in the unroutable ledger"


def _dispatch(out, entity_id, snapshot, monkeypatch, **kw):
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda _id: dict(snapshot))
    asyncio.run(apis.dispatch_task(
        entity_id, snapshot, trigger=kw.pop("trigger", "created"),
        notifier=out["notifier"], snapshot_hydrated=True, **kw,
    ))


# ── Every automatic path leaves a backlog task alone ─────────────────────────


@pytest.mark.parametrize("status", ["backlog", "BACKLOG", " Backlog "])
@pytest.mark.parametrize("trigger", ["created", "reconcile", "watchdog_retry", "build_released",
                                     "due_today"])
def test_dispatch_leaves_backlog_task_alone(status, trigger, effects, monkeypatch):
    _dispatch(effects, "ent_backlog_1", _stale(status), monkeypatch, trigger=trigger)
    _assert_left_alone(effects, f"dispatch_task(trigger={trigger})")


def test_stale_approval_does_not_take_a_task_out_of_the_backlog(effects, monkeypatch):
    _dispatch(effects, "ent_backlog_2", _stale("backlog"), monkeypatch,
              trigger="approved", gate_override=True)
    _assert_left_alone(effects, "gate_override")


@pytest.mark.parametrize("action", ["created", "due_today", "updated"])
def test_event_redelivery_leaves_backlog_task_alone(action, effects, monkeypatch):
    """The reset's `task.created` redelivery: no notice, no dispatch, no claim."""
    seen: list = []
    real = apis.dispatch_task

    async def _spy(entity_id, *a, **kw):
        seen.append(entity_id)
        return await real(entity_id, *a, **kw)

    monkeypatch.setattr(apis, "dispatch_task", _spy)
    monkeypatch.setattr(apis, "AUTO_EXECUTE", True)
    ev = NeotomaEvent(entity_type="task", entity_id="ent_backlog_3", action=action,
                      snapshot=_stale("backlog"))
    for _ in range(2):  # the original delivery and its redelivery
        asyncio.run(apis.handle_event(ev, effects["notifier"]))
    assert seen == []
    _assert_left_alone(effects, f"task.{action}")
    assert "ent_backlog_3" not in apis._created_seen, "a backlog delivery claimed the created dedupe"


def test_reconciler_sweep_leaves_backlog_task_alone(monkeypatch):
    rec = tr.TaskReconciler()
    rows = [("ent_backlog_4", _stale("backlog")), ("ent_backlog_5", _stale(" Backlog "))]
    monkeypatch.setattr(tr, "_query_tasks", lambda limit, cursor=None: (rows, None))
    calls: list = []

    async def dispatch_fn(task_id, snapshot, trigger):
        calls.append(task_id)

    asyncio.run(rec.sweep(dispatch_fn))
    assert calls == []


def test_watchdog_sweep_leaves_backlog_task_alone(monkeypatch):
    wd = tw.TaskWatchdog(stall_seconds=60)
    monkeypatch.setattr(tw, "_query_tasks", lambda limit: [("ent_backlog_6", _stale("backlog"))])
    writes: list = []
    notes: list = []
    monkeypatch.setattr(tw, "set_task_status", lambda *a, **k: writes.append(a))
    monkeypatch.setattr(tw, "_notify", lambda *a, **k: notes.append(a))
    calls: list = []

    async def dispatch_fn(task_id, snapshot, trigger):
        calls.append(task_id)

    asyncio.run(wd.sweep(object(), dispatch_fn))
    assert (calls, writes, notes) == ([], [], [])
    assert wd.classify("ent_backlog_6", "backlog", _OLD) == tw.WatchdogAction.NONE


def test_unlaunched_fly_recovery_does_not_report_a_task_now_in_backlog(monkeypatch):
    """Listed as executing, re-read as backlog: recorded, not named to the operator."""
    monkeypatch.setattr(apis, "_unlaunched_silence", lambda *a: (None, "unproven"))
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda _id: _stale("backlog"))
    n = _Notifier()
    counts = {"examined": 0, "recorded": 0, "unrecovered": 0}
    asyncio.run(apis._recover_unlaunched_fly_task("ent_backlog_7", {"status": "executing"}, n, counts))
    assert n.sent == [] and counts["unrecovered"] == 0 and counts["recorded"] == 1


def test_undated_session_task_alert_skips_backlog():
    assert "backlog" not in cdc.open_task_statuses()
    rows = [{"entity_id": "ent_backlog_8",
             "snapshot": {"status": "backlog", "assigned_to": "session"}}]
    assert cdc.undated_session_task_rows(rows) == []


# ── Fail closed for real work, and the way back out ──────────────────────────


@pytest.mark.parametrize("status", ["pending", "", "open"])
def test_unroutable_not_started_task_still_escalates_as_today(status, effects, monkeypatch):
    """New work keeps today's path: an unroutable new task is still blocked and reported."""
    _dispatch(effects, "ent_new_1", _stale(status), monkeypatch)
    assert effects["writes"], f"{status!r} task was not handled — the guard is too broad"


def test_unknown_status_keeps_current_behaviour(effects, monkeypatch):
    _dispatch(effects, "ent_unknown_1", _stale("in_progress"), monkeypatch)
    assert effects["writes"], "an unknown status stopped dispatching"


def test_backlog_moved_to_pending_dispatches_normally(effects, monkeypatch):
    """The explicit way back: same task, now pending, takes today's path."""
    task = _stale("backlog")
    _dispatch(effects, "ent_back_1", task, monkeypatch)
    _assert_left_alone(effects, "while in backlog")
    task["status"] = "pending"
    _dispatch(effects, "ent_back_1", task, monkeypatch)
    assert effects["writes"], "a task moved back to pending was not dispatched"
    assert can_transition("backlog", "pending")
    assert not can_transition("backlog", "routed")


def test_stale_pending_payload_cannot_write_on_a_live_backlog_task(effects, monkeypatch):
    """The decision snapshot says pending; the live record says backlog. The
    re-read before the first write refuses (``_task_status_still``)."""
    monkeypatch.setattr(apis, "_task_status_still", _REAL_REREAD)
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda _id: _stale("backlog"))
    asyncio.run(apis.dispatch_task("ent_race_1", _stale("pending"), trigger="created",
                                   notifier=effects["notifier"], snapshot_hydrated=True))
    assert effects["writes"] == [] and effects["briefs"] == [] and effects["spawns"] == []


def test_is_backlog_normalizes():
    assert is_backlog(" BackLog ") and not is_backlog("pending") and not is_backlog(None)


# ── Round 2: unreadable streak, dated alerts, unhydrated and embedded payloads ─


def _unreadable_hydrate(monkeypatch):
    """Hydration fails: the event keeps an unknown (empty) snapshot."""

    async def _fail(ev):
        ev.hydrated = False
        return ev

    monkeypatch.setattr(apis, "hydrate_snapshot", _fail)


def _event(entity_id, action="created", snapshot=None, raw=None):
    return NeotomaEvent(entity_type="task", entity_id=entity_id, action=action,
                        snapshot=dict(snapshot or {}), raw=dict(raw or {}))


def test_readable_backlog_delivery_ends_the_unreadable_streak(effects, monkeypatch):
    """unreadable, readable backlog x2, unreadable: no "could NOT be read" page."""
    eid = "ent_streak_1"
    live = {"value": None}  # the live read fails on the unreadable deliveries
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda _id: live["value"])
    notifier = effects["notifier"]

    def _hydrate_to(snapshot):
        async def _h(ev):
            ev.snapshot, ev.hydrated = (dict(snapshot), True) if snapshot else ({}, False)
            return ev
        return _h

    for snap in (None, _stale("backlog"), _stale("backlog"), None):
        monkeypatch.setattr(apis, "hydrate_snapshot", _hydrate_to(snap))
        asyncio.run(apis.handle_event(_event(eid), notifier))
    assert not any("could NOT be read" in m for m in notifier.sent), notifier.sent
    # The last delivery was unreadable, so a fresh streak of one has begun.
    assert effects["ledger"]._unreadable[eid]["n"] == 1


def test_dispatch_backlog_guard_ends_the_unreadable_streak(effects, monkeypatch):
    """The watchdog/reconciler/approval paths read the task fine: streak ends."""
    eid = "ent_streak_2"
    effects["ledger"].note_unreadable(eid)
    _dispatch(effects, eid, _stale("backlog"), monkeypatch, trigger="reconcile")
    assert eid not in effects["ledger"]._unreadable
    effects["ledger"].note_unreadable(eid)  # a later blip starts from zero
    assert effects["ledger"].drain_unreadable(force=True) is None


@pytest.mark.parametrize("snap", [
    {"status": "backlog", "due_date": "2026-11-25", "title": "overdue"},
    {"status": "backlog", "due_date": "2026-11-30", "title": "blocked",
     "blocked_reason": "no route/owner"},
    {"status": " Backlog ", "due_date": "2026-11-28", "title": "due",
     "assigned_to": "session"},
])
def test_dated_task_alerts_skip_backlog(snap):
    from datetime import datetime, timezone

    rows = [{"entity_id": "ent_dated_1", "snapshot": snap}]
    now = datetime(2026, 11, 28, 9, tzinfo=timezone.utc)
    findings, _ = cdc.check_tasks(lambda off: (rows if off == 0 else [], False), now)
    assert findings == [], [f.key for f in findings]
    # The same row, pending, does alert: the skip is about backlog only.
    rows[0]["snapshot"] = {**snap, "status": "pending"}
    findings, _ = cdc.check_tasks(lambda off: (rows if off == 0 else [], False), now)
    assert findings


def test_unhydrated_reset_redelivery_reads_live_and_stays_silent(effects, monkeypatch):
    """Hydration fails; the live record says backlog: no notice, no ledger entry."""
    _unreadable_hydrate(monkeypatch)
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda _id: _stale("backlog"))
    seen: list = []
    monkeypatch.setattr(apis, "dispatch_task", lambda eid, *a, **k: seen.append(eid))
    for action in ("created", "created", "due_today"):
        asyncio.run(apis.handle_event(_event("ent_unhyd_1", action), effects["notifier"]))
    assert seen == []
    _assert_left_alone(effects, "unhydrated reset")
    assert "ent_unhyd_1" not in effects["ledger"]._unreadable


def test_embedded_stale_payload_defers_to_live_backlog(effects, monkeypatch):
    """The stream carried a pre-reset `pending` snapshot; live says backlog."""
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda _id: _stale("backlog"))
    seen: list = []
    monkeypatch.setattr(apis, "dispatch_task", lambda eid, *a, **k: seen.append(eid))
    old = _stale("pending")
    ev = _event("ent_embed_1", snapshot=old, raw={"snapshot": old})
    ev.hydrated = True
    asyncio.run(apis.handle_event(ev, effects["notifier"]))
    assert seen == [] and effects["notifier"].sent == []


def test_both_reads_fail_keeps_todays_provisional_notice(effects, monkeypatch):
    """Unknown status: one provisional notice across redeliveries, nothing run."""
    _unreadable_hydrate(monkeypatch)
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda _id: None)
    monkeypatch.setattr(apis, "_announced", {})
    for _ in range(5):
        asyncio.run(apis.handle_event(_event("ent_unk_1"), effects["notifier"]))
    created = [m for m in effects["notifier"].sent if m.startswith("Task created")]
    assert len(created) == 1, effects["notifier"].sent
    assert effects["writes"] == [] and effects["briefs"] == []
    assert effects["spawns"] == []


def test_unhydrated_live_pending_task_keeps_todays_path(effects, monkeypatch):
    """Not backlog: the event carries on exactly as delivered."""
    _unreadable_hydrate(monkeypatch)
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda _id: _stale("pending"))
    calls: list = []

    async def _spy(eid, snapshot, **kw):
        calls.append((eid, snapshot, kw.get("snapshot_hydrated")))

    monkeypatch.setattr(apis, "dispatch_task", _spy)
    asyncio.run(apis.handle_event(_event("ent_pend_1"), effects["notifier"]))
    assert calls == [("ent_pend_1", {}, False)]
    assert any(m.startswith("Task created") for m in effects["notifier"].sent)


def test_deferral_rollback_classifies_backlog_as_parked(monkeypatch):
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda _id: _stale("backlog"))
    claim = type("C", (), {"run_id": "r1"})()
    monkeypatch.setattr(apis, "_expected_deferral",
                        lambda c: {f: "x" for f in apis._DEFERRAL_FIELDS})
    state = apis._classify_deferral_state("ent_defer_1", claim)
    assert state["verdict"] == "moved" and state["kind"] == "terminal"
