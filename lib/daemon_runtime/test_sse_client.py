"""Tests for the SSE snapshot re-fetch (lib/daemon_runtime/sse_client.py).

Neotoma SSE events carry no `snapshot`, so daemons routing on event.snapshot
would see an empty dict and dispatch nothing. _fetch_snapshot re-fetches the
entity by id (token-optional / open-mode) so handlers get real routing fields.
"""

from __future__ import annotations

import sse_client
from sse_client import NeotomaEvent, _fetch_snapshot


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


# ── _fetch_snapshot unwrapping ────────────────────────────────────────────────


def test_unwraps_snapshot_envelope(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        return _FakeResp({"entity_id": "ent_x", "snapshot": {"tags": ["neotoma"], "assigned_to": "gryllus"}})

    monkeypatch.setattr(sse_client.httpx, "get", fake_get)
    snap = _fetch_snapshot("ent_x", "http://localhost:9180", None)
    assert snap == {"tags": ["neotoma"], "assigned_to": "gryllus"}
    assert captured["url"] == "http://localhost:9180/entities/ent_x"
    # open-mode: no bearer header when token is None
    assert captured["headers"] == {}


def test_sends_bearer_when_token_present(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, timeout=None):
        captured["headers"] = headers
        return _FakeResp({"entity_id": "ent_x", "snapshot": {"status": "open"}})

    monkeypatch.setattr(sse_client.httpx, "get", fake_get)
    _fetch_snapshot("ent_x", "http://localhost:9180", "tok123")
    assert captured["headers"] == {"Authorization": "Bearer tok123"}


def test_falls_back_to_flat_object_when_no_envelope(monkeypatch):
    def fake_get(url, headers=None, timeout=None):
        return _FakeResp({"entity_id": "ent_x", "tags": ["a"], "assigned_to": "b"})

    monkeypatch.setattr(sse_client.httpx, "get", fake_get)
    snap = _fetch_snapshot("ent_x", "http://localhost:9180", None)
    assert snap["tags"] == ["a"]


def test_empty_entity_id_returns_empty():
    assert _fetch_snapshot("", "http://localhost:9180", None) == {}


def test_fetch_failure_returns_empty(monkeypatch):
    def boom(url, headers=None, timeout=None):
        raise RuntimeError("network down")

    monkeypatch.setattr(sse_client.httpx, "get", boom)
    assert _fetch_snapshot("ent_x", "http://localhost:9180", None) == {}


# ── event already carries a snapshot → no re-fetch needed ─────────────────────


def test_event_with_snapshot_is_left_untouched(monkeypatch):
    def fail(*a, **k):
        raise AssertionError("should not fetch when snapshot already present")

    monkeypatch.setattr(sse_client.httpx, "get", fail)
    ev = NeotomaEvent.from_raw(
        {"entity_type": "task", "entity_id": "ent_x", "action": "created", "snapshot": {"tags": ["x"]}}
    )
    # Mirror the guard used in the stream loop.
    if not ev.snapshot and ev.entity_id:
        ev.snapshot = _fetch_snapshot(ev.entity_id, "http://localhost:9180", None)
    assert ev.snapshot == {"tags": ["x"]}


if __name__ == "__main__":
    import subprocess

    raise SystemExit(subprocess.call(["python3", "-m", "pytest", __file__, "-q"]))
