"""Tests for the SSE snapshot re-fetch (lib/daemon_runtime/sse_client.py).

Neotoma SSE events carry no `snapshot`, so daemons routing on event.snapshot
would see an empty dict and dispatch nothing. _fetch_snapshot re-fetches the
entity by id (token-optional / open-mode, async) so handlers get real routing
fields without blocking the SSE event loop.
"""

from __future__ import annotations

import asyncio

from lib.daemon_runtime import sse_client
from lib.daemon_runtime.sse_client import NeotomaEvent, _fetch_snapshot


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Minimal stand-in for httpx.AsyncClient used as an async context manager."""

    def __init__(self, payload=None, exc=None, capture=None, **kwargs):
        self._payload = payload
        self._exc = exc
        self._capture = capture if capture is not None else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        self._capture["url"] = url
        self._capture["headers"] = headers
        if self._exc is not None:
            raise self._exc
        return _FakeResp(self._payload)


def _patch_client(monkeypatch, *, payload=None, exc=None):
    capture: dict = {}

    def factory(**kwargs):
        return _FakeAsyncClient(payload=payload, exc=exc, capture=capture, **kwargs)

    monkeypatch.setattr(sse_client.httpx, "AsyncClient", factory)
    return capture


# ── _fetch_snapshot unwrapping ────────────────────────────────────────────────


def test_unwraps_snapshot_envelope(monkeypatch):
    cap = _patch_client(
        monkeypatch,
        payload={
            "entity_id": "ent_x",
            "snapshot": {"tags": ["neotoma"], "assigned_to": "gryllus"},
        },
    )
    snap = asyncio.run(_fetch_snapshot("ent_x", "http://localhost:9180", None))
    assert snap == {"tags": ["neotoma"], "assigned_to": "gryllus"}
    assert cap["url"] == "http://localhost:9180/entities/ent_x"
    assert cap["headers"] == {}  # open-mode: no bearer when token is None


def test_sends_bearer_when_token_present(monkeypatch):
    cap = _patch_client(
        monkeypatch, payload={"entity_id": "ent_x", "snapshot": {"status": "open"}}
    )
    asyncio.run(_fetch_snapshot("ent_x", "http://localhost:9180", "tok123"))
    assert cap["headers"] == {"Authorization": "Bearer tok123"}


def test_falls_back_to_flat_object_when_no_envelope(monkeypatch):
    _patch_client(
        monkeypatch, payload={"entity_id": "ent_x", "tags": ["a"], "assigned_to": "b"}
    )
    snap = asyncio.run(_fetch_snapshot("ent_x", "http://localhost:9180", None))
    assert snap["tags"] == ["a"]


def test_empty_entity_id_returns_empty():
    assert asyncio.run(_fetch_snapshot("", "http://localhost:9180", None)) == {}


def test_fetch_failure_returns_empty(monkeypatch):
    _patch_client(monkeypatch, exc=RuntimeError("network down"))
    assert asyncio.run(_fetch_snapshot("ent_x", "http://localhost:9180", None)) == {}


# ── event already carries a snapshot → no re-fetch needed ─────────────────────


def test_event_with_snapshot_is_left_untouched(monkeypatch):
    def fail(**kwargs):
        raise AssertionError("should not construct a client when snapshot present")

    monkeypatch.setattr(sse_client.httpx, "AsyncClient", fail)
    ev = NeotomaEvent.from_raw(
        {
            "entity_type": "task",
            "entity_id": "ent_x",
            "action": "created",
            "snapshot": {"tags": ["x"]},
        }
    )

    async def _enrich():
        if not ev.snapshot and ev.entity_id:
            ev.snapshot = await _fetch_snapshot(
                ev.entity_id, "http://localhost:9180", None
            )

    asyncio.run(_enrich())
    assert ev.snapshot == {"tags": ["x"]}


if __name__ == "__main__":
    import subprocess

    raise SystemExit(subprocess.call(["python3", "-m", "pytest", __file__, "-q"]))
