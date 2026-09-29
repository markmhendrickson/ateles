"""Effect tests for task-dispatch session provenance."""

from __future__ import annotations

import sys
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
    assert task_fields == {"result": _PR_HEADER, "status": "done"}
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
    assert "provenance" in task_statuses[-1][1]["reason"]
    assert job.finished_events == []
    assert job.failed_events


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
    monkeypatch.setattr(tw, "_query_tasks", lambda _limit: [("ent_task", snapshot)])
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
