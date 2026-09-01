"""
Tests for lib/daemon_runtime/sse_client.py — specifically hydrate_snapshot,
which fills an SSE event's snapshot by fetching the entity (the stream itself
delivers only metadata).

hydrate_snapshot is async (it must not block the daemon event loop), so each
test drives it via asyncio.run — the repo has no pytest-asyncio dependency.
"""

from __future__ import annotations

import asyncio

import lib.daemon_runtime.sse_client as sc
from lib.daemon_runtime.sse_client import NeotomaEvent, hydrate_snapshot


class _FakeResp:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self._status = status

    def raise_for_status(self):
        if self._status >= 400:
            raise RuntimeError(f"HTTP {self._status}")

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient as an async context manager."""

    def __init__(self, payload=None, *, capture=None, raises=None, **kwargs):
        self._payload = payload
        self._capture = capture
        self._raises = raises

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, headers=None):
        if self._capture is not None:
            self._capture["url"] = url
            self._capture["headers"] = headers or {}
        if self._raises is not None:
            raise self._raises
        return _FakeResp(self._payload or {})


def _install_fake_client(monkeypatch, payload=None, *, capture=None, raises=None):
    def factory(**kwargs):
        return _FakeAsyncClient(payload, capture=capture, raises=raises, **kwargs)

    monkeypatch.setattr(sc.httpx, "AsyncClient", factory)


def test_empty_snapshot_is_hydrated_from_entity(monkeypatch):
    _install_fake_client(
        monkeypatch,
        payload={"snapshot": {"title": "T", "tags": ["neotoma"], "assigned_to": "cicada"}},
    )
    ev = NeotomaEvent(entity_type="task", entity_id="ent_x", action="created")
    asyncio.run(hydrate_snapshot(ev))
    assert ev.snapshot["tags"] == ["neotoma"]
    assert ev.snapshot["assigned_to"] == "cicada"


def test_populated_snapshot_is_left_alone_no_fetch(monkeypatch):
    def factory(**kwargs):
        raise AssertionError("should not construct a client when snapshot present")

    monkeypatch.setattr(sc.httpx, "AsyncClient", factory)
    ev = NeotomaEvent(
        entity_type="task", entity_id="ent_x", action="created",
        snapshot={"title": "already here"},
    )
    asyncio.run(hydrate_snapshot(ev))
    assert ev.snapshot["title"] == "already here"


def test_missing_entity_id_is_noop(monkeypatch):
    def factory(**kwargs):
        raise AssertionError("should not fetch without an entity_id")

    monkeypatch.setattr(sc.httpx, "AsyncClient", factory)
    ev = NeotomaEvent(entity_type="task", entity_id="", action="created")
    asyncio.run(hydrate_snapshot(ev))
    assert ev.snapshot == {}


def test_fetch_error_fails_soft(monkeypatch):
    _install_fake_client(monkeypatch, raises=RuntimeError("neotoma down"))
    ev = NeotomaEvent(entity_type="task", entity_id="ent_x", action="created")
    # must not raise — dispatch loop should survive a transient blip
    asyncio.run(hydrate_snapshot(ev))
    assert ev.snapshot == {}


def test_open_mode_sends_no_auth_header(monkeypatch):
    cap: dict = {}
    monkeypatch.setattr(sc, "NEOTOMA_BEARER_TOKEN", "")
    _install_fake_client(monkeypatch, payload={"snapshot": {"title": "T"}}, capture=cap)
    ev = NeotomaEvent(entity_type="task", entity_id="ent_x", action="created")
    asyncio.run(hydrate_snapshot(ev))
    assert "Authorization" not in cap["headers"]


def test_bearer_mode_sends_auth_header(monkeypatch):
    cap: dict = {}
    monkeypatch.setattr(sc, "NEOTOMA_BEARER_TOKEN", "tok-123")
    _install_fake_client(monkeypatch, payload={"snapshot": {"title": "T"}}, capture=cap)
    ev = NeotomaEvent(entity_type="task", entity_id="ent_x", action="created")
    asyncio.run(hydrate_snapshot(ev))
    assert cap["headers"].get("Authorization") == "Bearer tok-123"


def test_non_dict_snapshot_falls_back_to_empty(monkeypatch):
    _install_fake_client(monkeypatch, payload={"snapshot": None})
    ev = NeotomaEvent(entity_type="task", entity_id="ent_x", action="created")
    asyncio.run(hydrate_snapshot(ev))
    assert ev.snapshot == {}


# ── hydrated flag: a FAILED read must not read as an empty entity ────────────
#
# The no-owner loop's routing half. hydrate_snapshot used to leave snapshot={}
# on any fetch error, which callers could not tell apart from a task that
# genuinely has no tags — so a transient 502 made a fully-tagged task look
# unroutable. ent_c192afd8760fd9f3fbd3c08c (title + description + five tags)
# was escalated three times; its real tags were logged at 16:22:05 and `tags=[]`
# at 16:25:25, right after a 502 on this exact GET.


def test_successful_hydration_marks_hydrated(monkeypatch):
    _install_fake_client(monkeypatch, payload={"snapshot": {"title": "T", "tags": ["ops"]}})
    ev = NeotomaEvent(entity_type="task", entity_id="ent_x", action="created")
    asyncio.run(hydrate_snapshot(ev))
    assert ev.hydrated is True


def test_failed_hydration_leaves_hydrated_false(monkeypatch):
    _install_fake_client(monkeypatch, raises=RuntimeError("502 Bad Gateway"))
    ev = NeotomaEvent(entity_type="task", entity_id="ent_x", action="created")
    asyncio.run(hydrate_snapshot(ev))
    assert ev.snapshot == {}
    assert ev.hydrated is False, "a failed read must not look like an empty entity"


def test_genuinely_empty_entity_is_hydrated_true(monkeypatch):
    """The distinction that matters: read OK but the entity really has no fields."""
    _install_fake_client(monkeypatch, payload={"snapshot": {}})
    ev = NeotomaEvent(entity_type="task", entity_id="ent_x", action="created")
    asyncio.run(hydrate_snapshot(ev))
    assert ev.snapshot == {}
    assert ev.hydrated is True


def test_embedded_snapshot_counts_as_hydrated():
    ev = NeotomaEvent.from_raw(
        {"entity_type": "task", "entity_id": "ent_x", "action": "created",
         "snapshot": {"title": "T"}}
    )
    assert ev.hydrated is True


def test_event_without_snapshot_starts_unhydrated():
    ev = NeotomaEvent.from_raw(
        {"entity_type": "task", "entity_id": "ent_x", "action": "created"}
    )
    assert ev.hydrated is False


def test_prehydrated_event_short_circuits_as_hydrated(monkeypatch):
    def factory(**kwargs):
        raise AssertionError("should not fetch when the snapshot is present")

    monkeypatch.setattr(sc.httpx, "AsyncClient", factory)
    ev = NeotomaEvent(
        entity_type="task", entity_id="ent_x", action="created",
        snapshot={"title": "already here"},
    )
    asyncio.run(hydrate_snapshot(ev))
    assert ev.hydrated is True
