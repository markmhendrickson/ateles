"""
Tests for lib/daemon_runtime/sse_client.py — specifically hydrate_snapshot,
which fills an SSE event's snapshot by fetching the entity (the stream itself
delivers only metadata).
"""

from __future__ import annotations

import sse_client as sc
from sse_client import NeotomaEvent, hydrate_snapshot


class _FakeResp:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")

    def json(self):
        return self._payload


def _install_fake_get(monkeypatch, payload=None, *, capture=None, raises=None):
    def fake_get(url, headers=None, timeout=None):
        if capture is not None:
            capture["url"] = url
            capture["headers"] = headers or {}
        if raises is not None:
            raise raises
        return _FakeResp(payload or {})

    monkeypatch.setattr(sc.httpx, "get", fake_get)


def test_empty_snapshot_is_hydrated_from_entity(monkeypatch):
    _install_fake_get(
        monkeypatch,
        payload={"snapshot": {"title": "T", "tags": ["neotoma"], "assigned_to": "gryllus"}},
    )
    ev = NeotomaEvent(entity_type="task", entity_id="ent_x", action="created")
    hydrate_snapshot(ev)
    assert ev.snapshot["tags"] == ["neotoma"]
    assert ev.snapshot["assigned_to"] == "gryllus"


def test_populated_snapshot_is_left_alone_no_fetch(monkeypatch):
    called = {"n": 0}

    def fake_get(*a, **k):
        called["n"] += 1
        raise AssertionError("should not fetch when snapshot already present")

    monkeypatch.setattr(sc.httpx, "get", fake_get)
    ev = NeotomaEvent(
        entity_type="task", entity_id="ent_x", action="created",
        snapshot={"title": "already here"},
    )
    hydrate_snapshot(ev)
    assert called["n"] == 0
    assert ev.snapshot["title"] == "already here"


def test_missing_entity_id_is_noop(monkeypatch):
    def fake_get(*a, **k):
        raise AssertionError("should not fetch without an entity_id")

    monkeypatch.setattr(sc.httpx, "get", fake_get)
    ev = NeotomaEvent(entity_type="task", entity_id="", action="created")
    hydrate_snapshot(ev)
    assert ev.snapshot == {}


def test_fetch_error_fails_soft(monkeypatch):
    _install_fake_get(monkeypatch, raises=RuntimeError("neotoma down"))
    ev = NeotomaEvent(entity_type="task", entity_id="ent_x", action="created")
    # must not raise — dispatch loop should survive a transient blip
    hydrate_snapshot(ev)
    assert ev.snapshot == {}


def test_open_mode_sends_no_auth_header(monkeypatch):
    cap: dict = {}
    monkeypatch.setattr(sc, "NEOTOMA_BEARER_TOKEN", "")
    _install_fake_get(monkeypatch, payload={"snapshot": {"title": "T"}}, capture=cap)
    ev = NeotomaEvent(entity_type="task", entity_id="ent_x", action="created")
    hydrate_snapshot(ev)
    assert "Authorization" not in cap["headers"]


def test_bearer_mode_sends_auth_header(monkeypatch):
    cap: dict = {}
    monkeypatch.setattr(sc, "NEOTOMA_BEARER_TOKEN", "tok-123")
    _install_fake_get(monkeypatch, payload={"snapshot": {"title": "T"}}, capture=cap)
    ev = NeotomaEvent(entity_type="task", entity_id="ent_x", action="created")
    hydrate_snapshot(ev)
    assert cap["headers"].get("Authorization") == "Bearer tok-123"


def test_non_dict_snapshot_falls_back_to_empty(monkeypatch):
    _install_fake_get(monkeypatch, payload={"snapshot": None})
    ev = NeotomaEvent(entity_type="task", entity_id="ent_x", action="created")
    hydrate_snapshot(ev)
    assert ev.snapshot == {}
