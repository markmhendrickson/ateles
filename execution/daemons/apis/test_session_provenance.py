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


class _Notifier:
    def send(self, *_args, **_kwargs):
        pass


class _Job:
    def finished(self, *_args, **_kwargs):
        pass

    def failed(self, *_args, **_kwargs):
        pass


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

    async def fake_spawn(*_args, **kwargs):
        spawned.append(kwargs)
        return SimpleNamespace(ok=True, error="", returncode=0)

    monkeypatch.setattr(apis, "_spawn_harness_skill", fake_spawn)

    await apis.dispatch_task(
        "ent_task",
        {
            "title": "Implement it",
            "assigned_to": "cicada",
            "status": "awaiting_approval",
            "attempt": 2,
        },
        "approved",
        _Notifier(),
        gate_override=True,
    )

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
