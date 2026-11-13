"""
neotoma-agent's due-date hygiene must not run on a snapshot it could not read,
nor on an observation.created (a correction), only on a real task creation.

An unread snapshot is {} — "unknown", not "no due date". Hygiene on it writes a
default due date (plus tags) over the real one.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_DAEMONS = Path(__file__).resolve().parent
sys.path.insert(0, str(_DAEMONS.parent.parent))

from lib.daemon_runtime import NeotomaEvent  # noqa: E402


def _load():
    if "neotoma_agent" in sys.modules:
        return sys.modules["neotoma_agent"]
    spec = importlib.util.spec_from_file_location(
        "neotoma_agent", _DAEMONS / "neotoma-agent/neotoma_agent.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["neotoma_agent"] = mod
    spec.loader.exec_module(mod)
    return mod


neotoma_agent = _load()


class _Notifier:
    def send(self, *a, **k):
        pass


class _Grants:
    def is_suspended(self):
        return False


@pytest.fixture
def hygiene(monkeypatch):
    calls: list = []

    async def _apply(entity_id, snapshot):
        calls.append((entity_id, snapshot))

    monkeypatch.setattr(neotoma_agent, "DUE_DATE_HYGIENE_ENABLED", True)
    monkeypatch.setattr(neotoma_agent, "apply_due_date_hygiene", _apply)
    return calls


def _handle(monkeypatch, event, *, hydrate_ok: bool):
    async def _hydrate(ev):
        if hydrate_ok:
            ev.snapshot = {"title": "t", "due_date": "2026-12-01"}
            ev.hydrated = True
        else:
            ev.snapshot = {}
            ev.hydrated = False
        return ev

    monkeypatch.setattr(neotoma_agent, "hydrate_snapshot", _hydrate)
    asyncio.run(neotoma_agent.handle_event(event, _Notifier(), _Grants()))


def _raw(event_type):
    return NeotomaEvent.from_raw(
        {"event_type": event_type, "action": "created",
         "entity_type": "task", "entity_id": "ent_t"})


def test_hygiene_runs_on_a_hydrated_creation(monkeypatch, hygiene):
    _handle(monkeypatch, _raw("entity.created"), hydrate_ok=True)
    assert [c[0] for c in hygiene] == ["ent_t"]


def test_hygiene_skipped_when_snapshot_unread(monkeypatch, hygiene):
    _handle(monkeypatch, _raw("entity.created"), hydrate_ok=False)
    assert hygiene == [], "hygiene ran on an unread snapshot and would default the due date"


def test_hygiene_not_run_for_observation_created(monkeypatch, hygiene):
    _handle(monkeypatch, _raw("observation.created"), hydrate_ok=True)
    assert hygiene == []
