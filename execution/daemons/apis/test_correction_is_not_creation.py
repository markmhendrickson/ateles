"""
test_correction_is_not_creation.py — a correction to an existing task must not
re-run the create/dispatch path, and a gate decision must not overwrite a status
that moved after the snapshot it was drawn from.

13 Nov 2026: a session corrected ~90 old tasks. Neotoma emits
``observation.created`` (action "created") then ``entity.updated`` for each, and
Apis keyed on ``action == "created"`` alone, so every task this process had not
seen since its restart ran the full create path: 10 closures reverted, 8 statuses
flipped to blocked / awaiting_approval, 8 spurious PLAN checkpoints.

Two independent guards, each tested so it goes red without its guard:
  1. only entity.created counts as a creation (sse_client + handle_event);
  2. the gate re-reads the task before its status/checkpoint write and refuses
     on terminal, on a changed status, and on an unreadable task.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
from lib.daemon_runtime.sse_client import NeotomaEvent  # noqa: E402
from unroutable_ledger import UnroutableLedger  # noqa: E402


class _Notifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message, priority=None, handler=None, **kwargs):
        self.sent.append(message)

    def clear_dedupe(self, key):
        pass


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(apis, "_unroutable", UnroutableLedger(path=tmp_path / "l.json"))
    monkeypatch.setattr(apis, "_created_seen", {})
    monkeypatch.setattr(apis, "_announced", {})


# ── guard 1: observation.created is not a creation ───────────────────────────


def _handle(raw: dict, monkeypatch):
    dispatched: list = []

    async def _hydrate(ev):
        ev.snapshot = {"title": "old task", "status": "pending", "tags": ["ops"]}
        ev.hydrated = True
        return ev

    async def _dispatch(entity_id, snapshot, trigger, notifier, **kw):
        dispatched.append(entity_id)

    monkeypatch.setattr(apis, "hydrate_snapshot", _hydrate)
    monkeypatch.setattr(apis, "dispatch_task", _dispatch)
    n = _Notifier()
    asyncio.run(apis.handle_event(NeotomaEvent.from_raw(raw), n))
    return dispatched, n


def test_correction_event_pair_does_not_dispatch(monkeypatch):
    for event_type, action in (("observation.created", "created"), ("entity.updated", "updated")):
        dispatched, n = _handle(
            {"event_type": event_type, "action": action,
             "entity_type": "task", "entity_id": "ent_old"}, monkeypatch)
        assert dispatched == [], f"{event_type} ran the create path"
        assert n.sent == []


def test_a_new_task_still_dispatches(monkeypatch):
    dispatched, _ = _handle(
        {"event_type": "entity.created", "action": "created",
         "entity_type": "task", "entity_id": "ent_new"}, monkeypatch)
    assert dispatched == ["ent_new"]


# ── guard 2: re-read before the gate writes ──────────────────────────────────


def _run_dispatch(monkeypatch, snapshot, fresh, *, status_calls):
    """Dispatch `snapshot` while a re-read returns `fresh` (None = unreadable)."""
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda _id: fresh)
    monkeypatch.setattr(
        apis, "set_task_status", lambda *a, **k: status_calls.append(a) or True)
    n = _Notifier()
    asyncio.run(apis.dispatch_task(
        "ent_t", snapshot, trigger="created", notifier=n, snapshot_hydrated=True))
    return n


UNROUTABLE = {"title": "Something with no domain words", "tags": [], "status": "pending"}
ROUTABLE = {"title": "Fix the flaky CI pipeline", "tags": ["ops"], "status": "pending"}


def test_unchanged_task_still_gets_its_gate_write(monkeypatch):
    """Sanity: the guard is not a blanket refusal."""
    calls: list = []
    _run_dispatch(monkeypatch, UNROUTABLE, dict(UNROUTABLE), status_calls=calls)
    assert [c[1].value for c in calls] == ["blocked"]


@pytest.mark.parametrize("snap", [UNROUTABLE, ROUTABLE])
def test_terminal_on_reread_is_not_overwritten(monkeypatch, snap):
    calls: list = []
    n = _run_dispatch(monkeypatch, snap, {**snap, "status": "completed"}, status_calls=calls)
    assert calls == [], "a status write landed on a task that closed meanwhile"
    assert not any("PLAN checkpoint" in m for m in n.sent)


@pytest.mark.parametrize("snap", [UNROUTABLE, ROUTABLE])
def test_changed_status_on_reread_is_not_overwritten(monkeypatch, snap):
    calls: list = []
    _run_dispatch(monkeypatch, snap, {**snap, "status": "awaiting_input"}, status_calls=calls)
    assert calls == []


@pytest.mark.parametrize("snap", [UNROUTABLE, ROUTABLE])
def test_unreadable_task_fails_closed(monkeypatch, snap):
    calls: list = []
    n = _run_dispatch(monkeypatch, snap, None, status_calls=calls)
    assert calls == []
    assert n.sent == []


def test_gate_hold_rereads_after_routing(monkeypatch):
    """The AWAITING_APPROVAL hold + checkpoint is a later write; a task that
    closes between ROUTED and the gate must not be held or briefed."""
    calls: list = []

    def _fetch(_id):
        # Before the ROUTED write: as filed. After it: closed by someone else.
        return dict(ROUTABLE) if not calls else dict(ROUTABLE, status="done")

    monkeypatch.setattr(apis, "fetch_task_snapshot", _fetch)
    monkeypatch.setattr(apis, "set_task_status", lambda *a, **k: calls.append(a) or True)
    briefs: list = []
    monkeypatch.setattr(apis, "write_checkpoint_brief", lambda **k: briefs.append(k) or "b")
    monkeypatch.setattr(apis, "READINESS_GATE", False)
    n = _Notifier()
    asyncio.run(apis.dispatch_task(
        "ent_t", ROUTABLE, trigger="created", notifier=n, snapshot_hydrated=True))
    written = [c[1].value for c in calls]
    assert "awaiting_approval" not in written
    assert briefs == []
