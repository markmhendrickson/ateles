"""neotoma-agent leaves a backlog task's due date and tags alone on task.created."""

from __future__ import annotations

import asyncio
import importlib.util
import pathlib
import sys

_HERE = pathlib.Path(__file__).resolve().parent
_ROOT = _HERE.parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from lib.daemon_runtime.sse_client import NeotomaEvent  # noqa: E402

_spec = importlib.util.spec_from_file_location("neotoma_agent_backlog_under_test",
                                               _HERE / "neotoma_agent.py")
mod = importlib.util.module_from_spec(_spec)
sys.modules["neotoma_agent_backlog_under_test"] = mod
_spec.loader.exec_module(mod)


def _run(status, monkeypatch):
    calls: list = []

    async def _hygiene(entity_id, snapshot):
        calls.append(entity_id)

    async def _hydrated(ev):
        ev.hydrated = True
        return ev

    monkeypatch.setattr(mod, "apply_due_date_hygiene", _hygiene)
    monkeypatch.setattr(mod, "hydrate_snapshot", _hydrated)
    monkeypatch.setattr(mod, "DUE_DATE_HYGIENE_ENABLED", True)
    ev = NeotomaEvent(entity_type="task", entity_id="ent_hyg_1", action="created",
                      snapshot={"title": "Synthetic", "status": status})
    asyncio.run(mod.handle_event(ev, None, None))
    return calls


def test_backlog_task_gets_no_due_date_hygiene(monkeypatch):
    assert _run("backlog", monkeypatch) == []


def test_pending_task_still_gets_due_date_hygiene(monkeypatch):
    assert _run("pending", monkeypatch) == ["ent_hyg_1"]
