"""
test_stream_intake.py — Apis keeps reading the SSE stream while a handler runs.

The shared SSE client awaits each handler before reading the next event. Apis's
`handle_event` can await a whole agent run (task.created → dispatch_task →
_finish_dispatch, up to 30 minutes), so handling it inline stopped Apis hearing
anything — keep-alive pings included — for as long as the run lasted.

These tests drive the callback Apis hands the SSE client
(`make_stream_dispatch`) exactly as the client does — `await handler(event)`
once per event, in order — with `dispatch_task` / `handle_checkpoint_brief`
replaced by fakes that can be held open.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
NeotomaEvent = apis.NeotomaEvent


class _Notifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message, priority=None, handler=None, **kwargs):
        self.sent.append(message)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.setattr(apis, "_created_seen", {})
    monkeypatch.setattr(apis, "_announced", {})
    writes: list = []
    monkeypatch.setattr(apis, "set_task_status", lambda *a, **k: writes.append((a, k)) or True)
    return writes


def _task(entity_id: str, action: str = "created") -> NeotomaEvent:
    return NeotomaEvent(
        event_type="entity_created",
        entity_type="task",
        entity_id=entity_id,
        action=action,
        snapshot={"title": f"task {entity_id}", "status": "pending"},
        hydrated=True,
    )


def _brief(brief_id: str, task_id: str) -> NeotomaEvent:
    return NeotomaEvent(
        event_type="entity_updated",
        entity_type="checkpoint_brief",
        entity_id=brief_id,
        action="updated",
        snapshot={"task_entity_id": task_id, "status": "approved"},
        hydrated=True,
    )


class _Runs:
    """A stand-in for dispatch_task whose calls can be held open per task."""

    def __init__(self):
        self.started: list[tuple[str, str]] = []
        self.finished: list[tuple[str, str]] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.active: dict[str, int] = {}
        self.max_active_per_task: dict[str, int] = {}
        self.cancelled: list[str] = []
        self.raise_for: set[str] = set()

    def hold(self, task_id: str) -> asyncio.Event:
        return self.gates.setdefault(task_id, asyncio.Event())

    async def run(self, task_id: str, what: str) -> None:
        self.started.append((task_id, what))
        self.active[task_id] = self.active.get(task_id, 0) + 1
        self.max_active_per_task[task_id] = max(
            self.max_active_per_task.get(task_id, 0), self.active[task_id]
        )
        try:
            gate = self.gates.get(task_id)
            if gate is not None:
                await gate.wait()
            if task_id in self.raise_for:
                raise RuntimeError("agent run crashed")
            self.finished.append((task_id, what))
        except asyncio.CancelledError:
            self.cancelled.append(task_id)
            raise
        finally:
            self.active[task_id] -= 1


def _install(monkeypatch, runs: _Runs) -> None:
    async def fake_dispatch(entity_id, snapshot, trigger, notifier, **kw):
        await runs.run(entity_id, trigger)

    async def fake_brief(entity_id, snapshot, notifier, detach_after_accept=False):
        await runs.run(snapshot["task_entity_id"], f"brief:{entity_id}")
        return True

    monkeypatch.setattr(apis, "dispatch_task", fake_dispatch)
    monkeypatch.setattr(apis, "handle_checkpoint_brief", fake_brief)


async def _feed(handler, *events) -> None:
    """What SSEClient._connect_and_stream does with each parsed event."""
    for event in events:
        await asyncio.wait_for(handler(event), timeout=2)


async def _until(predicate, timeout: float = 2.0) -> None:
    async def _spin():
        while not predicate():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(_spin(), timeout=timeout)


async def _drain() -> None:
    pending = [t for t in apis._background_dispatch_tasks if not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


# ── Effect ──────────────────────────────────────────────────────────────────


def test_held_handler_does_not_stop_a_later_task_being_handled(monkeypatch):
    runs = _Runs()
    _install(monkeypatch, runs)

    async def go():
        handler = apis.make_stream_dispatch(_Notifier())
        gate_a = runs.hold("ent_A")
        await _feed(handler, _task("ent_A"), _task("ent_B"))
        # A is still running; B's dispatch has already happened.
        await _until(lambda: ("ent_B", "created") in runs.finished)
        assert ("ent_A", "created") not in runs.finished
        gate_a.set()
        await _drain()
        assert ("ent_A", "created") in runs.finished

    asyncio.run(go())


# ── Ordering ────────────────────────────────────────────────────────────────


def test_events_for_one_task_run_in_order_and_never_at_once(monkeypatch):
    runs = _Runs()
    _install(monkeypatch, runs)
    monkeypatch.setattr(apis, "AUTO_EXECUTE", True)

    async def go():
        handler = apis.make_stream_dispatch(_Notifier())
        gate = runs.hold("ent_A")
        await _feed(
            handler, _task("ent_A"), _task("ent_A", "due_today"), _brief("ent_CB", "ent_A")
        )
        await _until(lambda: runs.started)
        for _ in range(20):
            await asyncio.sleep(0)
        # Only the first is running; the due_today and the brief that releases
        # the same task wait for it.
        assert runs.started == [("ent_A", "created")]
        gate.set()
        await _drain()
        order = [w for _, w in runs.started]
        # One entity's events keep arrival order; the brief joins the task's
        # queue once its snapshot names the task, so it runs after the created
        # dispatch it arrived behind, never alongside either event.
        assert order[0] == "created"
        assert order.index("due_today") > order.index("created")
        assert sorted(order[1:]) == ["brief:ent_CB", "due_today"]
        assert runs.max_active_per_task["ent_A"] == 1

    asyncio.run(go())


def test_duplicate_created_dispatches_once(monkeypatch):
    runs = _Runs()
    _install(monkeypatch, runs)

    async def go():
        handler = apis.make_stream_dispatch(_Notifier())
        await _feed(handler, _task("ent_A"), _task("ent_A"), _task("ent_A"))
        await _drain()
        assert runs.started == [("ent_A", "created")]

    asyncio.run(go())


# ── Failure ─────────────────────────────────────────────────────────────────


def test_failure_is_logged_with_the_entity_and_writes_nothing(monkeypatch, caplog, _isolated):
    runs = _Runs()
    _install(monkeypatch, runs)
    runs.raise_for.add("ent_A")

    async def go():
        handler = apis.make_stream_dispatch(_Notifier())
        await _feed(handler, _task("ent_A"), _task("ent_B"))
        await _drain()
        # No ordering state is left behind to wedge a later delivery for A.
        assert "task/ent_A" not in apis._stream_tails()
        assert "ent_A" not in apis._stream_task_locks()

    with caplog.at_level(logging.ERROR, logger="apis"):
        asyncio.run(go())
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any(
        "task/ent_A" in r.getMessage() and "action=created" in r.getMessage()
        and r.exc_info is not None
        for r in errors
    )
    # Nothing beyond what the handler itself wrote: no status write by intake.
    assert _isolated == []
    # The failure did not stop later work.
    assert ("ent_B", "created") in runs.finished


# ── Shutdown ────────────────────────────────────────────────────────────────


def test_shutdown_cancels_detached_work_like_a_killed_inline_run(monkeypatch, caplog):
    runs = _Runs()
    _install(monkeypatch, runs)

    async def go():
        handler = apis.make_stream_dispatch(_Notifier())
        runs.hold("ent_A")
        await _feed(handler, _task("ent_A"), _task("ent_A", "updated"))
        await _until(lambda: runs.started)
        # What asyncio.run does to every task when the daemon's loop stops.
        for t in list(apis._background_dispatch_tasks):
            t.cancel()
        await asyncio.gather(*list(apis._background_dispatch_tasks), return_exceptions=True)
        assert runs.cancelled == ["ent_A"]
        assert ("ent_A", "created") not in runs.finished
        # No ordering state is left behind to wedge a later delivery.
        assert not apis._stream_tails().get("task/ent_A")
        assert "ent_A" not in apis._stream_task_locks()

    with caplog.at_level(logging.WARNING, logger="apis"):
        asyncio.run(go())
    assert any("task/ent_A" in r.getMessage() and "cancelled" in r.getMessage()
               for r in caplog.records)


# ── Limit ───────────────────────────────────────────────────────────────────


def test_dispatching_events_wait_for_a_free_slot(monkeypatch, caplog):
    runs = _Runs()
    _install(monkeypatch, runs)
    monkeypatch.setattr(apis, "_STREAM_CONCURRENCY", 2)
    monkeypatch.setattr(apis, "_stream_slots", apis.weakref.WeakKeyDictionary())

    async def go():
        handler = apis.make_stream_dispatch(_Notifier())
        gates = {t: runs.hold(t) for t in ("ent_A", "ent_B", "ent_C")}
        await _feed(handler, _task("ent_A"), _task("ent_B"), _task("ent_C"))
        await _until(lambda: len(runs.started) == 2)
        for _ in range(20):
            await asyncio.sleep(0)
        assert [t for t, _ in runs.started] == ["ent_A", "ent_B"]
        # A cheap event takes no slot: it is handled while both are taken.
        with caplog.at_level(logging.INFO, logger="apis"):
            await _feed(handler, _task("ent_D", "updated"))
            await _until(lambda: any(
                "ent_D" in r.getMessage() and "action=updated" in r.getMessage()
                for r in caplog.records))
        gates["ent_A"].set()
        await _until(lambda: len(runs.started) == 3)
        assert runs.started[2] == ("ent_C", "created")
        gates["ent_B"].set()
        gates["ent_C"].set()
        await _drain()

    asyncio.run(go())


def test_a_slow_read_of_an_earlier_event_is_not_overtaken(monkeypatch):
    """Arrival order, not read-completion order, decides one entity's order."""
    runs = _Runs()
    _install(monkeypatch, runs)
    monkeypatch.setattr(apis, "AUTO_EXECUTE", True)

    async def fake_hydrate(event):
        # The first delivery's read is slow; the second's is instant.
        if event.action == "created":
            await asyncio.sleep(0.1)
        event.snapshot = {"title": "t", "status": "pending"}
        event.hydrated = True
        return event

    monkeypatch.setattr(apis, "hydrate_snapshot", fake_hydrate)

    async def go():
        handler = apis.make_stream_dispatch(_Notifier())
        created, due = _task("ent_A"), _task("ent_A", "due_today")
        created.snapshot = due.snapshot = {}
        created.hydrated = due.hydrated = False
        await _feed(handler, created, due)
        await _drain()
        assert [w for _, w in runs.started] == ["created", "due_today"]

    asyncio.run(go())


def test_a_brief_whose_task_cannot_be_read_never_runs_beside_that_task(monkeypatch):
    """A brief with no readable task id is not acted on outside the task's lock."""
    runs = _Runs()
    _install(monkeypatch, runs)
    monkeypatch.setattr(apis, "_BRIEF_READ_DELAYS_S", (0.0,))
    monkeypatch.setattr(apis, "fetch_checkpoint_record", lambda _id: None)

    async def fake_hydrate(event):
        if event.entity_type == "checkpoint_brief":
            event.hydrated = False  # the read failed: snapshot unknown
        return event

    monkeypatch.setattr(apis, "hydrate_snapshot", fake_hydrate)

    async def released_by_its_own_read(entity_id, snapshot, notifier, detach_after_accept=False):
        # handle_checkpoint_brief refreshes the record itself; model that later
        # read succeeding, which is what would release the task beside its run.
        await runs.run("ent_A", f"brief:{entity_id}")
        return True

    monkeypatch.setattr(apis, "handle_checkpoint_brief", released_by_its_own_read)

    async def go():
        handler = apis.make_stream_dispatch(_Notifier())
        gate = runs.hold("ent_A")
        brief = _brief("ent_CB", "ent_A")
        brief.snapshot, brief.hydrated = {}, False
        await _feed(handler, _task("ent_A"), brief)
        await _until(lambda: runs.started)
        for _ in range(20):
            await asyncio.sleep(0.005)
        assert runs.max_active_per_task["ent_A"] == 1
        assert [w for _, w in runs.started] == ["created"]
        gate.set()
        await _drain()

    asyncio.run(go())


def test_a_brief_read_late_still_takes_its_tasks_lock(monkeypatch):
    """When the stream's read fails but a fresh read names the task, the brief
    queues behind that task's running dispatch."""
    runs = _Runs()
    _install(monkeypatch, runs)
    monkeypatch.setattr(
        apis, "fetch_checkpoint_record",
        lambda _id: {"snapshot": {"task_entity_id": "ent_A"}},
    )

    async def fake_hydrate(event):
        if event.entity_type == "checkpoint_brief":
            event.hydrated = False
        return event

    monkeypatch.setattr(apis, "hydrate_snapshot", fake_hydrate)

    async def go():
        handler = apis.make_stream_dispatch(_Notifier())
        gate = runs.hold("ent_A")
        brief = _brief("ent_CB", "ent_A")
        brief.snapshot, brief.hydrated = {}, False

        async def fake_brief(entity_id, snapshot, notifier, detach_after_accept=False):
            await runs.run("ent_A", f"brief:{entity_id}")
            return True

        monkeypatch.setattr(apis, "handle_checkpoint_brief", fake_brief)
        await _feed(handler, _task("ent_A"), brief)
        await _until(lambda: runs.started)
        for _ in range(20):
            await asyncio.sleep(0.005)
        assert [w for _, w in runs.started] == ["created"]
        gate.set()
        await _drain()
        assert [w for _, w in runs.started] == ["created", "brief:ent_CB"]
        assert runs.max_active_per_task["ent_A"] == 1

    asyncio.run(go())
