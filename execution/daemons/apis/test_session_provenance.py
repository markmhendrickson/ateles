"""Effect tests for task-dispatch session provenance."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
for _path in (str(_REPO_ROOT), str(_HERE)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import apis  # noqa: E402


class _Notifier:
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
