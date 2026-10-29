"""Effect tests for task-dispatch session provenance."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
for _path in (str(_REPO_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import apis  # noqa: E402
import task_watchdog as tw  # noqa: E402
from lib.daemon_runtime import session_finalize  # noqa: E402
from lib.daemon_runtime.task_lifecycle import FieldWrite  # noqa: E402


@pytest.fixture(autouse=True)
def _versioned_prelaunch_fake(monkeypatch):
    """Adapt existing lifecycle fakes to the server's conditional-write contract."""
    def read(task_id):
        snapshot = apis.fetch_task_snapshot(task_id)
        if snapshot is None:
            return None, None
        version = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, default=str).encode()
        ).hexdigest()
        return snapshot, version

    def write(task_id, field, value, *, expected_version, key_suffix, **_kwargs):
        current, version = read(task_id)
        if current is None or not expected_version:
            return FieldWrite(False)
        if expected_version != version:
            return FieldWrite(False, conflict=True)
        status = apis.TaskStatus.FAILED if field == "status" else apis.TaskStatus.EXECUTING
        kwargs = {"handler": "apis", "key_suffix": key_suffix}
        if field == "blocked_reason":
            kwargs["reason"] = value
        if not apis.set_task_status(task_id, status, **kwargs):
            return FieldWrite(False)
        _, version = read(task_id)
        return FieldWrite(True, version=version)

    monkeypatch.setattr(apis, "fetch_task_snapshot_and_version", read)
    monkeypatch.setattr(apis, "set_task_field_if_version", write)


class _Notifier:
    def __init__(self):
        self.messages: list[tuple[tuple, dict]] = []

    def send(self, *_args, **_kwargs):
        self.messages.append((_args, _kwargs))


class _Job:
    def __init__(self):
        self.finished_events: list[tuple] = []
        self.failed_events: list[tuple] = []

    def finished(self, *_args, **_kwargs):
        self.finished_events.append(_args)

    def failed(self, *_args, **_kwargs):
        self.failed_events.append(_args)


def _visible_recovery(monkeypatch):
    """Keep existing test fakes faithful to the task reason readback contract."""
    write = apis.set_task_status
    read = apis.fetch_task_snapshot
    visible = {"active": False, "reason": ""}

    def write_status(*args, **kwargs):
        if kwargs.get("key_suffix", "").endswith(("-recovering", "-verified")):
            visible.update(active=True, reason=kwargs["reason"])
        return write(*args, **kwargs)

    def read_status(entity_id):
        if visible["active"]:
            reason = visible["reason"]
            if reason == "":
                visible["active"] = False
            return {"status": "executing", "blocked_reason": reason}
        snapshot = read(entity_id)
        if not snapshot or not snapshot.get("status"):
            return {"status": "executing", "blocked_reason": "", **(snapshot or {})}
        return {"blocked_reason": "", **snapshot}

    monkeypatch.setattr(apis, "set_task_status", write_status)
    monkeypatch.setattr(apis, "fetch_task_snapshot", read_status)


class _VersionedTask:
    """A task correction fake with entity-level versions and intervening writes."""

    def __init__(self):
        self.fields = {"status": "awaiting_approval", "blocked_reason": ""}
        self.revision = 0
        self.before_write = None
        self.response_lost_at = None
        self.omit_version_at = None
        self.writes = []

    def version(self):
        return f"{self.revision:064x}"

    def hold(self):
        self.fields.update(status="blocked", blocked_reason="held by another gate")
        self.revision += 1

    def read(self, _task_id):
        return dict(self.fields)

    def read_with_version(self, _task_id):
        return self.read(_task_id), self.version()

    def status(self, _task_id, value, **kwargs):
        suffix = kwargs.get("key_suffix", "")
        if self.before_write and suffix.endswith(("-recovering", "-verified")):
            self.before_write("blocked_reason", kwargs.get("reason", ""), suffix)
        settlement = "cancelled" if suffix.endswith("-cancelled") else "failed"
        if self.before_write and value is apis.TaskStatus.FAILED:
            self.before_write("status", value.value, f"{suffix}-{settlement}-status")
        self.fields["status"] = value.value
        if kwargs.get("reason") is not None:
            if self.before_write and value is apis.TaskStatus.FAILED:
                self.before_write("blocked_reason", kwargs["reason"],
                                  f"{suffix}-{settlement}-reason")
            self.fields["blocked_reason"] = kwargs["reason"]
        self.revision += 1
        return True

    def conditional(self, _task_id, field, value, *, expected_version, key_suffix, **_kwargs):
        self.writes.append((field, value, key_suffix, expected_version))
        if self.before_write:
            self.before_write(field, value, key_suffix)
        if not expected_version:
            return FieldWrite(False)
        if expected_version != self.version():
            return FieldWrite(False, conflict=True)
        self.fields[field] = value
        self.revision += 1
        if self.response_lost_at == key_suffix:
            return FieldWrite(False)
        if self.omit_version_at == key_suffix:
            return FieldWrite(True)
        return FieldWrite(True, version=self.version())


def _install_versioned_task(monkeypatch, task):
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", True)
    monkeypatch.setattr(apis, "DRY_RUN", False)
    monkeypatch.setattr(apis, "_activity", SimpleNamespace(started=lambda *_a: _Job()))
    monkeypatch.setattr(apis, "_release_lifecycle_proven", lambda *_a: True)
    monkeypatch.setattr(apis, "set_task_status", task.status)
    monkeypatch.setattr(apis, "fetch_task_snapshot", task.read)
    monkeypatch.setattr(apis, "fetch_task_snapshot_and_version", task.read_with_version)
    monkeypatch.setattr(apis, "set_task_field_if_version", task.conditional)


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["recovering", "verified", "failed-reason", "failed-status"])
async def test_prelaunch_version_conflict_preserves_intervening_hold(monkeypatch, boundary):
    task = _VersionedTask()
    _install_versioned_task(monkeypatch, task)
    spawned = []

    def interleave(field, _value, suffix):
        if suffix.endswith(boundary):
            task.hold()
            task.before_write = None

    task.before_write = interleave
    monkeypatch.setattr(apis, "create_run_session", lambda **_kw: (
        None if boundary.startswith("failed") else SimpleNamespace(
            conversation_id="ent_conversation", agent_session_id="ent_session"
        )
    ))

    async def spawn(*_args, **_kwargs):
        spawned.append(True)

    monkeypatch.setattr(apis, "_spawn_harness_skill", spawn)
    await apis.dispatch_task(
        "ent_task", {"title": "Task", "assigned_to": "cicada",
                     "repo": "markmhendrickson/ateles", "status": "awaiting_approval"},
        "approved", _Notifier(), gate_override=True,
    )
    assert spawned == []
    assert task.fields == {"status": "blocked", "blocked_reason": "held by another gate"}


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["cancelled-reason", "cancelled-status"])
async def test_prelaunch_cancellation_conflict_preserves_intervening_hold(monkeypatch, boundary):
    task = _VersionedTask()
    _install_versioned_task(monkeypatch, task)
    started = threading.Event()
    release = threading.Event()
    spawned = []

    def session(**_kwargs):
        started.set()
        release.wait(1)
        return None

    monkeypatch.setattr(apis, "create_run_session", session)
    monkeypatch.setattr(apis, "_spawn_harness_skill", lambda *_a, **_k: spawned.append(True))
    running = asyncio.create_task(apis.dispatch_task(
        "ent_task", {"title": "Task", "assigned_to": "cicada",
                     "repo": "markmhendrickson/ateles", "status": "awaiting_approval"},
        "approved", _Notifier(), gate_override=True,
    ))
    assert await asyncio.to_thread(started.wait, 1)
    task.before_write = lambda _field, _value, suffix: task.hold() if suffix.endswith(boundary) else None
    running.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await running
    assert spawned == []
    assert task.fields == {"status": "blocked", "blocked_reason": "held by another gate"}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing_version", "invalid_version", "lost_response", "partial_write"])
async def test_prelaunch_uncertain_version_never_starts_worker(monkeypatch, failure):
    task = _VersionedTask()
    _install_versioned_task(monkeypatch, task)
    spawned = []
    session_calls = []
    if failure == "missing_version":
        monkeypatch.setattr(apis, "fetch_task_snapshot_and_version",
                            lambda task_id: (task.read(task_id), None))
    elif failure == "invalid_version":
        monkeypatch.setattr(apis, "fetch_task_snapshot_and_version",
                            lambda task_id: (task.read(task_id), "invalid"))
    elif failure == "lost_response":
        task.response_lost_at = "approved-0-recovering"
    else:
        task.omit_version_at = "approved-0-recovering"

    def session(**_kwargs):
        session_calls.append(True)
        return None

    monkeypatch.setattr(apis, "create_run_session", session)
    monkeypatch.setattr(apis, "_spawn_harness_skill", lambda *_a, **_k: spawned.append(True))
    await apis.dispatch_task(
        "ent_task", {"title": "Task", "assigned_to": "cicada",
                     "repo": "markmhendrickson/ateles", "status": "awaiting_approval"},
        "approved", _Notifier(), gate_override=True,
    )
    assert spawned == session_calls == []
    assert task.fields["status"] == "executing"
    assert task.fields["blocked_reason"] in {
        "", "Verifying run records (up to 45 seconds); worker not started"
    }


@pytest.mark.asyncio
async def test_prelaunch_replayed_marker_ack_cannot_fail_current_run(monkeypatch):
    task = _VersionedTask()
    _install_versioned_task(monkeypatch, task)
    original = task.conditional

    def replay(_task_id, field, value, **kwargs):
        if kwargs["key_suffix"].endswith("-recovering"):
            return FieldWrite(True, version=task.version())  # old ack, no current write
        return original(_task_id, field, value, **kwargs)

    monkeypatch.setattr(apis, "set_task_field_if_version", replay)
    monkeypatch.setattr(apis, "create_run_session", lambda **_kw:
                        (_ for _ in ()).throw(AssertionError("no store")))
    spawned = []
    monkeypatch.setattr(apis, "_spawn_harness_skill", lambda *_a, **_k: spawned.append(True))
    await apis.dispatch_task(
        "ent_task", {"title": "Task", "assigned_to": "cicada",
                     "repo": "markmhendrickson/ateles", "status": "awaiting_approval"},
        "approved", _Notifier(), gate_override=True,
    )
    assert spawned == []
    assert task.fields == {"status": "executing", "blocked_reason": ""}


@pytest.mark.asyncio
async def test_prelaunch_conditional_sequence_starts_verified_worker(monkeypatch):
    task = _VersionedTask()
    _install_versioned_task(monkeypatch, task)
    run = SimpleNamespace(conversation_id="ent_conversation", agent_session_id="ent_session")
    monkeypatch.setattr(apis, "create_run_session", lambda **_kw: run)
    monkeypatch.setattr(apis, "append_turn", lambda **_kw: True)
    monkeypatch.setattr(apis, "update_run_session_status", lambda *_a, **_k: True)
    spawned = []

    async def spawn(*_args, **kwargs):
        spawned.append(kwargs)
        return SimpleNamespace(
            stdout="[cicada] pull_request_link: BLOCKED — test", stderr="",
            ok=True, returncode=0,
        )

    monkeypatch.setattr(apis, "_spawn_harness_skill", spawn)
    await apis.dispatch_task(
        "ent_task", {"title": "Task", "assigned_to": "cicada",
                     "repo": "markmhendrickson/ateles", "status": "awaiting_approval"},
        "approved", _Notifier(), gate_override=True,
    )
    assert len(spawned) == 1
    assert spawned[0]["run_conversation_id"] == run.conversation_id
    assert spawned[0]["run_agent_session_id"] == run.agent_session_id
    assert [field for field, _value, _key, _version in task.writes[:2]] == [
        "blocked_reason", "blocked_reason"
    ]
    assert task.writes[1][1] == ""


@pytest.mark.asyncio
async def test_delayed_run_recovery_keeps_other_dispatch_responsive(monkeypatch):
    seen = []
    task_state = {}
    spawned = []
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", True)
    monkeypatch.setattr(apis, "DRY_RUN", False)
    monkeypatch.setattr(apis, "_activity", SimpleNamespace(started=lambda *_a: _Job()))
    monkeypatch.setattr(apis, "_release_lifecycle_proven", lambda *_a: True)

    def status(task_id, value, **kwargs):
        current = task_state.setdefault(task_id, {})
        current["status"] = value.value
        if kwargs.get("reason") is not None:
            current["blocked_reason"] = kwargs["reason"]
        if task_id == "ent_second" and value is apis.TaskStatus.EXECUTING:
            seen.append("second_status")
        return True

    def session(*, task_id, **_kwargs):
        if task_id == "ent_first":
            seen.append("first_waiting")
            time.sleep(0.2)  # real delay on the critical recovery path
            seen.append("first_resolved")
        return None

    monkeypatch.setattr(apis, "set_task_status", status)
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda task_id: dict(task_state[task_id]))
    monkeypatch.setattr(apis, "create_run_session", session)

    async def spawn(*_args, **_kwargs):
        spawned.append(True)

    monkeypatch.setattr(apis, "_spawn_harness_skill", spawn)
    notifier = _Notifier()

    async def dispatch(task_id):
        await apis.dispatch_task(
            task_id,
            {"title": "Independent task", "assigned_to": "cicada",
             "repo": "markmhendrickson/ateles", "status": "awaiting_approval"},
            "approved", notifier, gate_override=True,
        )

    first = asyncio.create_task(dispatch("ent_first"))
    second = asyncio.create_task(dispatch("ent_second"))

    async def sentinel():
        await asyncio.sleep(0.05)
        assert task_state["ent_first"]["status"] == "executing"
        assert task_state["ent_first"]["blocked_reason"] == (
            "Verifying run records (up to 45 seconds); worker not started"
        )
        seen.append("sentinel")

    await asyncio.gather(first, second, sentinel())
    assert seen.index("second_status") < seen.index("first_resolved")
    assert seen.index("sentinel") < seen.index("first_resolved")
    assert spawned == []
    assert "provenance" in task_state["ent_first"]["blocked_reason"]
    assert "provenance" in task_state["ent_second"]["blocked_reason"]


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [False, True])
async def test_cancelled_recovery_cannot_launch_after_thread_finishes(monkeypatch, held):
    started = threading.Event()
    release = threading.Event()
    state = {}
    spawned = []
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", True)
    monkeypatch.setattr(apis, "DRY_RUN", False)
    monkeypatch.setattr(apis, "_activity", SimpleNamespace(started=lambda *_a: _Job()))
    monkeypatch.setattr(apis, "_release_lifecycle_proven", lambda *_a: True)

    def status(_id, value, **kwargs):
        state["status"] = value.value
        if kwargs.get("reason") is not None:
            state["blocked_reason"] = kwargs["reason"]
        return True

    def session(**_kwargs):
        started.set()
        release.wait(1)
        return SimpleNamespace(
            conversation_id="ent_conversation", agent_session_id="ent_session",
            native_session_id="ent_task:approved-0",
        )

    monkeypatch.setattr(apis, "set_task_status", status)
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda _id: dict(state))
    monkeypatch.setattr(apis, "create_run_session", session)

    async def spawn(*_args, **_kwargs):
        spawned.append(True)

    monkeypatch.setattr(apis, "_spawn_harness_skill", spawn)
    task = asyncio.create_task(apis.dispatch_task(
        "ent_task", {"title": "Task", "assigned_to": "cicada",
                     "repo": "markmhendrickson/ateles", "status": "awaiting_approval"},
        "approved", _Notifier(), gate_override=True,
    ))
    assert await asyncio.to_thread(started.wait, 1)
    if held:
        state.update(status="blocked", blocked_reason="held by another gate")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    await asyncio.sleep(0.03)
    assert spawned == []
    if held:
        assert state == {"status": "blocked", "blocked_reason": "held by another gate"}
    else:
        assert state["status"] == "failed"
        assert "cancelled" in state["blocked_reason"]


@pytest.mark.asyncio
async def test_partial_recovery_marker_stops_without_launch(monkeypatch):
    state = {"status": "awaiting_approval", "blocked_reason": ""}
    spawned = []
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", True)
    monkeypatch.setattr(apis, "DRY_RUN", False)
    monkeypatch.setattr(apis, "_activity", SimpleNamespace(started=lambda *_a: _Job()))
    monkeypatch.setattr(apis, "_release_lifecycle_proven", lambda *_a: True)

    def status(_id, value, **kwargs):
        state["status"] = value.value
        if kwargs.get("key_suffix", "").endswith("-recovering"):
            return False  # status committed, companion reason did not
        if kwargs.get("reason") is not None:
            state["blocked_reason"] = kwargs["reason"]
        return True

    monkeypatch.setattr(apis, "set_task_status", status)
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda _id: dict(state))
    monkeypatch.setattr(apis, "create_run_session", lambda **_kw:
                        (_ for _ in ()).throw(AssertionError("no store")))

    async def spawn(*_args, **_kwargs):
        spawned.append(True)

    monkeypatch.setattr(apis, "_spawn_harness_skill", spawn)
    await apis.dispatch_task(
        "ent_task", {"title": "Task", "assigned_to": "cicada",
                     "repo": "markmhendrickson/ateles", "status": "awaiting_approval"},
        "approved", _Notifier(), gate_override=True,
    )
    assert spawned == []
    assert state == {"status": "executing", "blocked_reason": ""}


@pytest.mark.asyncio
async def test_cancellation_during_marker_write_leaves_unproven_marker(monkeypatch):
    state = {"status": "awaiting_approval", "blocked_reason": ""}
    started = threading.Event()
    release = threading.Event()
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", True)
    monkeypatch.setattr(apis, "DRY_RUN", False)
    monkeypatch.setattr(apis, "_activity", SimpleNamespace(started=lambda *_a: _Job()))
    monkeypatch.setattr(apis, "_release_lifecycle_proven", lambda *_a: True)

    def status(_id, value, **kwargs):
        if kwargs.get("key_suffix", "").endswith("-recovering"):
            started.set()
            release.wait(1)
        state["status"] = value.value
        if kwargs.get("reason") is not None:
            state["blocked_reason"] = kwargs["reason"]
        return True

    monkeypatch.setattr(apis, "set_task_status", status)
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda _id: dict(state))
    monkeypatch.setattr(apis, "create_run_session", lambda **_kw:
                        (_ for _ in ()).throw(AssertionError("no store")))
    task = asyncio.create_task(apis.dispatch_task(
        "ent_task", {"title": "Task", "assigned_to": "cicada",
                     "repo": "markmhendrickson/ateles", "status": "awaiting_approval"},
        "approved", _Notifier(), gate_override=True,
    ))
    assert await asyncio.to_thread(started.wait, 1)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert state == {
        "status": "executing",
        "blocked_reason": "Verifying run records (up to 45 seconds); worker not started",
    }


@pytest.mark.asyncio
async def test_task_held_during_run_verification_cannot_launch(monkeypatch):
    state = {}
    spawned = []
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", True)
    monkeypatch.setattr(apis, "DRY_RUN", False)
    monkeypatch.setattr(apis, "_activity", SimpleNamespace(started=lambda *_a: _Job()))
    monkeypatch.setattr(apis, "_release_lifecycle_proven", lambda *_a: True)

    def status(_id, value, **kwargs):
        state["status"] = value.value
        if kwargs.get("reason") is not None:
            state["blocked_reason"] = kwargs["reason"]
        return True

    def session(**_kwargs):
        state.update(status="blocked", blocked_reason="held by another gate")
        return SimpleNamespace(
            conversation_id="ent_conversation", agent_session_id="ent_session",
            native_session_id="ent_task:approved-0",
        )

    monkeypatch.setattr(apis, "set_task_status", status)
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda _id: dict(state))
    monkeypatch.setattr(apis, "create_run_session", session)

    async def spawn(*_args, **_kwargs):
        spawned.append(True)

    monkeypatch.setattr(apis, "_spawn_harness_skill", spawn)
    await apis.dispatch_task(
        "ent_task", {"title": "Task", "assigned_to": "cicada",
                     "repo": "markmhendrickson/ateles", "status": "awaiting_approval"},
        "approved", _Notifier(), gate_override=True,
    )
    assert spawned == []
    assert state == {"status": "blocked", "blocked_reason": "held by another gate"}


_PR_HEADER = (
    "[cicada] pull_request_link: https://github.com/markmhendrickson/ateles/pull/999"
)


def _delivered_pr():
    """Cicada's success now has to name its PR to reach DONE (ateles#1155)."""
    return SimpleNamespace(
        ok=True, error="", returncode=0, stdout=_PR_HEADER + "\n", stderr=""
    )


def _resolve_pr_and_store_completion(monkeypatch):
    """Resolve the ref as real; point artifact completion at an in-memory task.

    Returns the task fields so a test can assert what the completion left behind.
    """
    from lib.daemon_runtime import task_lifecycle

    fields: dict = {}

    def post(url, headers=None, json=None, timeout=None):
        fields[json["field"]] = json["value"]
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(apis, "resolve_artifact_ref", lambda *a, **k: True)
    monkeypatch.setattr(task_lifecycle, "NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(task_lifecycle.httpx, "post", post)
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda _id: dict(fields))
    return fields


class _Response:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


@pytest.mark.asyncio
async def test_spawn_threads_session_identity_into_runner_and_brief(monkeypatch):
    captured: dict = {}

    async def fake_run_skill(skill, prompt, **kwargs):
        captured.update({"skill": skill, "prompt": prompt, **kwargs})
        return object()

    monkeypatch.setattr(apis, "run_skill", fake_run_skill)
    await apis._spawn_harness_skill(
        "cicada", "ent_task", {"title": "Implement it", "body": "Do the work"},
        "created", _Notifier(), run_conversation_id="ent_conversation",
        run_agent_session_id="ent_session",
    )
    assert captured["task_entity_id"] == "ent_task"
    assert captured["agent_session_id"] == "ent_session"
    assert "agent_session ent_session" in captured["prompt"]
    assert "exactly one PART_OF" in captured["prompt"]


def test_session_capture_is_enabled_unless_explicitly_disabled():
    assert apis._session_capture_enabled(None) is True
    assert apis._session_capture_enabled("") is True
    assert apis._session_capture_enabled("1") is True
    assert apis._session_capture_enabled("0") is False

@pytest.mark.asyncio
async def test_dispatch_persists_one_session_across_spawn_turns_and_completion(
    monkeypatch,
):
    run = SimpleNamespace(
        conversation_id="ent_conversation",
        agent_session_id="ent_session",
        native_session_id="ent_task:approved-2",
    )
    created: list[dict] = []
    spawned: list[dict] = []
    turns: list[dict] = []
    statuses: list[tuple[object, str]] = []

    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", True)
    monkeypatch.setattr(apis, "DRY_RUN", False)
    monkeypatch.setattr(
        apis, "_activity", SimpleNamespace(started=lambda *_args: _Job())
    )
    monkeypatch.setattr(apis, "set_task_status", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(apis, "_release_lifecycle_proven", lambda *_args: True)
    monkeypatch.setattr(
        apis,
        "create_run_session",
        lambda **kwargs: created.append(kwargs) or run,
    )
    monkeypatch.setattr(apis, "append_turn", lambda **kwargs: turns.append(kwargs))
    monkeypatch.setattr(
        apis,
        "update_run_session_status",
        lambda actual_run, *, status: statuses.append((actual_run, status)) or True,
    )

    task_fields = _resolve_pr_and_store_completion(monkeypatch)

    async def fake_spawn(*_args, **kwargs):
        spawned.append(kwargs)
        return _delivered_pr()

    monkeypatch.setattr(apis, "_spawn_harness_skill", fake_spawn)

    _visible_recovery(monkeypatch)
    await apis.dispatch_task(
        "ent_task",
        {
            "title": "Implement it",
            "assigned_to": "cicada",
            "repo": "markmhendrickson/ateles",
            "status": "awaiting_approval",
            "attempt": 2,
        },
        "approved",
        _Notifier(),
        gate_override=True,
    )

    # DONE rests on the agent's exact header, read back after the session closed.
    assert task_fields == {"result": _PR_HEADER, "blocked_reason": "", "status": "done"}
    assert created == [
        {
            "task_id": "ent_task",
            "plan_id": None,
            "agent": "cicada",
            "run_key": "approved-2",
            "title": "cicada run · Implement it",
        }
    ]
    assert spawned == [
        {
            "role": "cicada",
            "run_conversation_id": "ent_conversation",
            "run_agent_session_id": "ent_session",
        }
    ]
    assert [turn["conversation_id"] for turn in turns] == [
        "ent_conversation",
        "ent_conversation",
    ]
    assert statuses == [(run, "completed")]


@pytest.mark.parametrize(
    "failure_mode",
    ["store_failure", "identity_mismatch", "relationship_verification_failure"],
)
@pytest.mark.asyncio
async def test_dispatch_refuses_to_spawn_without_verified_session_provenance(
    monkeypatch,
    failure_mode,
):
    spawned: list[dict] = []
    task_statuses: list[tuple[tuple, dict]] = []
    job = _Job()
    notifier = _Notifier()

    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", True)
    monkeypatch.setattr(apis, "DRY_RUN", False)
    monkeypatch.setattr(
        apis, "_activity", SimpleNamespace(started=lambda *_args: job)
    )
    monkeypatch.setattr(
        apis,
        "set_task_status",
        lambda *args, **kwargs: task_statuses.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(apis, "_release_lifecycle_proven", lambda *_args: True)
    monkeypatch.setattr(session_finalize, "NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(session_finalize, "RUN_SESSION_RECOVERY_SECONDS", 0.01)

    if failure_mode == "store_failure":
        monkeypatch.setattr(
            session_finalize.httpx,
            "post",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("down")),
        )
    else:
        monkeypatch.setattr(
            session_finalize.httpx,
            "post",
            lambda *_args, **_kwargs: _Response({
                "entities": [
                    {"entity_type": "conversation", "entity_id": "ent_conversation"},
                    {"entity_type": "agent_session", "entity_id": "ent_session"},
                ]
            }),
        )

        def fake_get(url, **_kwargs):
            if url.endswith("/entities/ent_conversation"):
                return _Response({
                    "entity_id": "ent_conversation",
                    "entity_type": "conversation",
                    "snapshot": {
                        "conversation_id": "ent_task:approved-2",
                        "session_id": "ent_task:approved-2",
                    },
                })
            if url.endswith("/entities/ent_session"):
                native_session_id = (
                    "wrong-session"
                    if failure_mode == "identity_mismatch"
                    else "ent_task:approved-2"
                )
                return _Response({
                    "entity_id": "ent_session",
                    "entity_type": "agent_session",
                    "snapshot": {
                        "harness": "ateles-swarm",
                        "native_session_id": native_session_id,
                    },
                })
            return _Response({"relationships": []})

        monkeypatch.setattr(session_finalize.httpx, "get", fake_get)

    async def fake_spawn(*_args, **kwargs):
        spawned.append(kwargs)
        return SimpleNamespace(ok=True, error="", returncode=0)

    monkeypatch.setattr(apis, "_spawn_harness_skill", fake_spawn)

    _visible_recovery(monkeypatch)
    await apis.dispatch_task(
        "ent_task",
        {
            "title": "Implement it",
            "assigned_to": "cicada",
            "repo": "markmhendrickson/ateles",
            "status": "awaiting_approval",
            "attempt": 2,
        },
        "approved",
        notifier,
        gate_override=True,
    )

    assert spawned == []
    assert task_statuses[-1][0][1] is apis.TaskStatus.FAILED
    assert any("provenance" in kwargs.get("reason", "")
               for _, kwargs in task_statuses)
    assert job.finished_events == []
    assert job.failed_events


@pytest.mark.asyncio
async def test_timed_out_prelaunch_store_recovers_late_exact_run_once(monkeypatch):
    native_id = "ent_task:approved-2"
    stores = []
    query_rounds = 0
    spawned = []
    statuses = []

    def post(url, **kwargs):
        nonlocal query_rounds
        if url.endswith("/store"):
            stores.append(kwargs["json"])
            raise RuntimeError("response lost")
        assert url.endswith("/entities/query")
        if kwargs["json"]["entity_type"] == "agent_session":
            query_rounds += 1
        if query_rounds < 3:
            return _Response({"entities": []})
        if kwargs["json"]["entity_type"] == "agent_session":
            return _Response({"entities": [{"entity_id": "ent_session", "snapshot": {
                "harness": "ateles-swarm", "native_session_id": native_id,
                "trigger_ref": "ent_task",
            }}]})
        return _Response({"entities": [{"entity_id": "ent_conversation", "snapshot": {
            "conversation_id": native_id, "session_id": native_id,
        }}]})

    def get(url, **_kwargs):
        if url.endswith("/entities/ent_session"):
            return _Response({"entity_type": "agent_session", "snapshot": {
                "harness": "ateles-swarm", "native_session_id": native_id,
                "trigger_ref": "ent_task",
            }})
        if url.endswith("/entities/ent_conversation"):
            return _Response({"entity_type": "conversation", "snapshot": {
                "conversation_id": native_id, "session_id": native_id,
            }})
        source = url.split("/entities/", 1)[1].split("/", 1)[0]
        targets = {"ent_session": ["ent_task"],
                   "ent_conversation": ["ent_task", "ent_session"]}
        return _Response({"relationships": [{
            "source_entity_id": source, "target_entity_id": target,
            "relationship_type": "REFERS_TO",
        } for target in targets[source]]})

    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", True)
    monkeypatch.setattr(apis, "DRY_RUN", False)
    monkeypatch.setattr(apis, "_activity", SimpleNamespace(started=lambda *_a: _Job()))
    monkeypatch.setattr(apis, "set_task_status", lambda _id, status, **_kw:
                        statuses.append(status) or True)
    monkeypatch.setattr(apis, "_release_lifecycle_proven", lambda *_a: True)
    monkeypatch.setattr(apis, "append_turn", lambda **_kw: True)
    monkeypatch.setattr(apis, "update_run_session_status", lambda *_a, **_kw: True)
    monkeypatch.setattr(session_finalize, "NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(session_finalize, "RUN_SESSION_RECOVERY_SECONDS", 1)
    monkeypatch.setattr(session_finalize, "RUN_SESSION_RECOVERY_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(session_finalize.httpx, "post", post)
    monkeypatch.setattr(session_finalize.httpx, "get", get)

    async def spawn(*_args, **kwargs):
        spawned.append(kwargs)
        return SimpleNamespace(
            stdout="[cicada] pull_request_link: BLOCKED — test", stderr="",
            ok=True, returncode=0,
        )

    monkeypatch.setattr(apis, "_spawn_harness_skill", spawn)
    _visible_recovery(monkeypatch)
    await apis.dispatch_task(
        "ent_task", {"title": "Implement it", "assigned_to": "cicada",
                     "repo": "markmhendrickson/ateles", "status": "awaiting_approval",
                     "attempt": 2},
        "approved", _Notifier(), gate_override=True,
    )

    assert query_rounds >= 3
    assert len(stores) == 1
    assert stores[0]["idempotency_key"] == "run-session-ent_task-approved-2"
    assert spawned == [{
        "role": "cicada", "run_conversation_id": "ent_conversation",
        "run_agent_session_id": "ent_session",
    }]
    assert apis.TaskStatus.EXECUTING in statuses
    assert apis.TaskStatus.FAILED not in statuses[:statuses.index(apis.TaskStatus.EXECUTING) + 1]


@pytest.mark.asyncio
async def test_dispatch_holds_verified_when_terminal_state_is_unverified(
    monkeypatch,
):
    run = SimpleNamespace(
        conversation_id="ent_conversation",
        agent_session_id="ent_session",
        native_session_id="ent_task:approved-2",
    )
    task_statuses: list[tuple[tuple, dict]] = []
    turns: list[dict] = []
    job = _Job()

    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", True)
    monkeypatch.setattr(apis, "DRY_RUN", False)
    monkeypatch.setattr(
        apis, "_activity", SimpleNamespace(started=lambda *_args: job)
    )
    monkeypatch.setattr(
        apis,
        "set_task_status",
        lambda *args, **kwargs: task_statuses.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(apis, "_release_lifecycle_proven", lambda *_args: True)
    monkeypatch.setattr(apis, "create_run_session", lambda **_kwargs: run)
    monkeypatch.setattr(apis, "append_turn", lambda **kwargs: turns.append(kwargs))
    monkeypatch.setattr(
        apis, "update_run_session_status", lambda *_args, **_kwargs: False
    )
    monkeypatch.setattr(
        apis,
        "_spawn_harness_skill",
        lambda *_args, **_kwargs: _async_result(_delivered_pr()),
    )
    monkeypatch.setattr(apis, "resolve_artifact_ref", lambda *a, **k: True)

    _visible_recovery(monkeypatch)
    await apis.dispatch_task(
        "ent_task",
        {
            "title": "Implement it",
            "assigned_to": "cicada",
            "repo": "markmhendrickson/ateles",
            "status": "awaiting_approval",
            "attempt": 2,
        },
        "approved",
        _Notifier(),
        gate_override=True,
    )

    assert task_statuses[-1][0][1] is apis.TaskStatus.VERIFIED
    assert "terminal" in task_statuses[-1][1]["reason"]
    assert job.finished_events == []
    assert job.failed_events
    assert not any("completed" in turn["content"].lower() for turn in turns)


@pytest.mark.asyncio
async def test_watchdog_reconciles_terminal_provenance_without_repeating_effect(
    monkeypatch,
):
    run = SimpleNamespace(
        conversation_id="ent_conversation",
        agent_session_id="ent_session",
        native_session_id="ent_task:approved-2",
    )
    snapshot = {
        "title": "Implement it",
        "assigned_to": "cicada",
        "repo": "markmhendrickson/ateles",
        "status": "awaiting_approval",
        "attempt": 2,
    }
    executions: list[str] = []
    terminal_updates: list[str] = []
    task_statuses: list[str] = []

    def write_status(_task_id, status, **_kwargs):
        value = status.value if isinstance(status, apis.TaskStatus) else status
        snapshot["status"] = value
        if _kwargs.get("result") is not None:
            snapshot["result"] = _kwargs["result"]
        task_statuses.append(value)
        return True

    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", True)
    monkeypatch.setattr(apis, "DRY_RUN", False)
    monkeypatch.setattr(
        apis, "_activity", SimpleNamespace(started=lambda *_args: _Job())
    )
    monkeypatch.setattr(apis, "set_task_status", write_status)
    monkeypatch.setattr(apis, "_release_lifecycle_proven", lambda *_args: True)
    monkeypatch.setattr(apis, "create_run_session", lambda **_kwargs: run)
    monkeypatch.setattr(apis, "append_turn", lambda **_kwargs: True)

    def terminal_update(_run, *, status):
        terminal_updates.append(status)
        return len(terminal_updates) > 1

    monkeypatch.setattr(apis, "update_run_session_status", terminal_update)

    monkeypatch.setattr(apis, "resolve_artifact_ref", lambda *a, **k: True)

    async def execute_effect(*_args, **_kwargs):
        executions.append("executed")
        return _delivered_pr()

    monkeypatch.setattr(apis, "_spawn_harness_skill", execute_effect)
    monkeypatch.setattr(tw, "_query_tasks", lambda _limit, _cursor=None: (
        [("ent_task", snapshot)], None
    ))
    monkeypatch.setattr(tw, "set_task_status", write_status)
    monkeypatch.setattr(tw, "recover_run_session", lambda **_kwargs: run)
    monkeypatch.setattr(tw, "update_run_session_status", terminal_update)

    notifier = _Notifier()

    async def dispatch_again(task_id, current_snapshot, trigger):
        await apis.dispatch_task(
            task_id,
            current_snapshot,
            trigger,
            notifier,
            gate_override=True,
        )

    _visible_recovery(monkeypatch)
    await apis.dispatch_task(
        "ent_task",
        snapshot,
        "approved",
        notifier,
        gate_override=True,
    )

    assert executions == ["executed"]
    assert snapshot["status"] == apis.TaskStatus.VERIFIED.value
    assert apis.TaskStatus.DONE.value not in task_statuses

    counts = await tw.TaskWatchdog().sweep(notifier, dispatch_again)

    assert counts["reconciled"] == 1
    assert counts["retried"] == 0
    assert executions == ["executed"]
    assert snapshot["status"] == apis.TaskStatus.DONE.value
    assert terminal_updates == ["completed", "completed"]


async def _async_result(value):
    return value
