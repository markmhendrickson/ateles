"""The Apis event stream does not wait for agent runs.

``handle_event`` is awaited by the SSE loop one event at a time. The created
and checkpoint_brief branches used to await the whole agent run (up to 30
minutes), so the stream heard nothing else meanwhile. They now pass
``detach_after_accept=True``: reading, gating and the hold path stay on the
stream, and only the run after the task durably enters EXECUTING is detached.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
from lib.daemon_runtime.sse_client import NeotomaEvent  # noqa: E402
from test_checkpoint_release import (  # noqa: E402,F401 — fixture re-export
    CHECKPOINT_TYPE,
    _cicada_delivers_a_pr,
    _Notifier,
    release_store,
)

# Bounded wait for "handle_event returned": far below any real run.
RETURN_TIMEOUT = 0.5


async def _no_hydrate(event):
    return event


def _event(entity_type: str, entity_id: str, action: str, snapshot: dict):
    return NeotomaEvent(
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        snapshot=snapshot,
        hydrated=True,
    )


async def _drain_background():
    background = list(apis._background_dispatch_tasks)
    if background:
        await asyncio.gather(*background, return_exceptions=True)


@pytest.fixture
def fake_created_dispatch(monkeypatch):
    """Model dispatch_task's detach point: the run is held open on an event.

    Honors ``detach_after_accept`` exactly as the real dispatch_task does (a
    background task in ``_background_dispatch_tasks`` vs an inline await), so a
    handle_event that drops the argument blocks on the held run.
    """
    monkeypatch.setattr(apis, "hydrate_snapshot", _no_hydrate)
    monkeypatch.setattr(apis, "_created_seen", {})
    monkeypatch.setattr(apis, "_announced", {})
    release = asyncio.Event()
    started: list[str] = []

    async def fake_dispatch(entity_id, snapshot, trigger, notifier, **kwargs):
        async def _finish_dispatch():
            started.append(entity_id)
            await release.wait()

        if kwargs.get("detach_after_accept"):
            task = asyncio.create_task(
                _finish_dispatch(), name=f"apis-dispatch-{entity_id}-{trigger}"
            )
            apis._background_dispatch_tasks.add(task)
            task.add_done_callback(apis._background_dispatch_done)
            return None
        await _finish_dispatch()
        return None

    monkeypatch.setattr(apis, "dispatch_task", fake_dispatch)
    yield started, release
    release.set()


@pytest.mark.asyncio
async def test_created_event_returns_while_run_held_open(fake_created_dispatch):
    started, release = fake_created_dispatch
    notifier = _Notifier()
    try:
        await asyncio.wait_for(
            apis.handle_event(
                _event("task", "ent_task_a", "created", {"title": "A"}), notifier
            ),
            timeout=RETURN_TIMEOUT,
        )
        await asyncio.wait_for(
            apis.handle_event(
                _event("task", "ent_task_b", "created", {"title": "B"}), notifier
            ),
            timeout=RETURN_TIMEOUT,
        )
        await asyncio.sleep(0)
        assert started == ["ent_task_a", "ent_task_b"]
    finally:
        release.set()
        await _drain_background()


@pytest.mark.asyncio
async def test_duplicate_created_does_not_start_second_run(fake_created_dispatch):
    started, release = fake_created_dispatch
    notifier = _Notifier()
    try:
        for _ in range(2):
            await asyncio.wait_for(
                apis.handle_event(
                    _event("task", "ent_task_a", "created", {"title": "A"}),
                    notifier,
                ),
                timeout=RETURN_TIMEOUT,
            )
        await asyncio.sleep(0)
        assert started == ["ent_task_a"]
        assert len(apis._background_dispatch_tasks) == 1
    finally:
        release.set()
        await _drain_background()


def _approve_and_run_real(monkeypatch, records, brief_id, spawn):
    monkeypatch.setattr(apis, "hydrate_snapshot", _no_hydrate)
    monkeypatch.setattr(apis, "DRY_RUN", False)
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", False)
    monkeypatch.setattr(apis, "RUN_EMAIL", False)
    _cicada_delivers_a_pr(monkeypatch, records)
    monkeypatch.setattr(apis, "_spawn_harness_skill", spawn)
    records[brief_id]["snapshot"]["status"] = "approved"


@pytest.mark.asyncio
async def test_approved_checkpoint_event_returns_while_run_held_open(
    monkeypatch, release_store
):
    records, brief_id, task_id = release_store
    finish = asyncio.Event()
    spawn_started = asyncio.Event()
    _Success = _cicada_delivers_a_pr(monkeypatch, records)

    async def slow_spawn(*args, **kwargs):
        spawn_started.set()
        await finish.wait()
        return _Success()

    _approve_and_run_real(monkeypatch, records, brief_id, slow_spawn)
    monkeypatch.setattr(apis, "_created_seen", {})
    later_dispatches: list[str] = []
    real_dispatch = apis.dispatch_task

    async def spy_dispatch(entity_id, *args, **kwargs):
        if entity_id != task_id:
            later_dispatches.append(entity_id)
            return None
        return await real_dispatch(entity_id, *args, **kwargs)

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)
    notifier = _Notifier()
    try:
        await asyncio.wait_for(
            apis.handle_event(
                _event(CHECKPOINT_TYPE, brief_id, "updated", {}), notifier
            ),
            timeout=RETURN_TIMEOUT,
        )
        await asyncio.wait_for(spawn_started.wait(), timeout=RETURN_TIMEOUT)
        assert records[task_id]["snapshot"]["status"] == "executing"
        assert records[brief_id]["snapshot"]["resolved_dispatched"] is True

        await asyncio.wait_for(
            apis.handle_event(
                _event("task", "ent_task_other", "created", {"title": "B"}),
                notifier,
            ),
            timeout=RETURN_TIMEOUT,
        )
        assert later_dispatches == ["ent_task_other"]
    finally:
        finish.set()
        await _drain_background()


@pytest.mark.asyncio
async def test_detached_run_that_raises_is_logged_and_marks_failed(
    monkeypatch, release_store, caplog
):
    records, brief_id, task_id = release_store

    async def crashing_spawn(*args, **kwargs):
        raise RuntimeError("harness exploded")

    _approve_and_run_real(monkeypatch, records, brief_id, crashing_spawn)

    class _Job:
        def finished(self, message):
            return None

        def failed(self, message):
            return None

    monkeypatch.setattr(apis._activity, "started", lambda message: _Job())
    caplog.set_level(logging.ERROR, logger=apis.log.name)

    await asyncio.wait_for(
        apis.handle_event(_event(CHECKPOINT_TYPE, brief_id, "updated", {}), _Notifier()),
        timeout=RETURN_TIMEOUT,
    )
    await _drain_background()
    await asyncio.sleep(0)

    # Same state a failed inline run leaves: FAILED with the crash as reason.
    assert records[task_id]["snapshot"]["status"] == "failed"
    assert "dispatch raised RuntimeError" in records[task_id]["snapshot"][
        "blocked_reason"
    ]
    failures = [r for r in caplog.records if "detached dispatch" in r.getMessage()]
    assert failures, "detached failure was not logged"
    assert task_id in failures[0].getMessage()
    assert failures[0].exc_info and isinstance(failures[0].exc_info[1], RuntimeError)
