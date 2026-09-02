"""Regression tests for the `/observations` → `/store` migration.

The bug: `/observations` was removed from Neotoma, so `_store_email_entity`
and `_create_task_for_email` 404'd on every inbound email — silent data loss
on the mail-triage path (see PR #190). Both functions now POST to `/store`
with the entities-array + idempotency_key shape used by the known-good caller
in `lib/daemon_runtime/gating.py::write_checkpoint_brief`.

turdus imports httpx locally inside each function (`import httpx`), not at
module scope, so there is no `turdus.httpx` attribute to monkeypatch (verified:
`hasattr(turdus, "httpx")` is False after import). A local `import httpx`
still binds to the single shared `sys.modules["httpx"]` object, so patching
the real `httpx` module's `AsyncClient` reaches every local import — the fake
client is installed on `httpx` itself, not on `turdus`.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))


def _load_turdus(monkeypatch, *, bearer_token: str = "test-token"):
    monkeypatch.setenv("NEOTOMA_BEARER_TOKEN", bearer_token)
    monkeypatch.setenv("TURDUS_DRY_RUN", "0")
    sys.modules.pop("turdus", None)
    return importlib.import_module("turdus")


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

    def __init__(self, payload=None, *, capture=None, **kwargs):
        self._payload = payload if payload is not None else {}
        self._capture = capture

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None, **kwargs):
        if self._capture is not None:
            self._capture.setdefault("calls", []).append({"url": url, "json": json})
        return _FakeResp(self._payload)


def _install_fake_client(monkeypatch, payload=None, *, capture=None):
    def factory(**kwargs):
        return _FakeAsyncClient(payload, capture=capture, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


_MESSAGE = {
    "id": "msg-1",
    "sender": "Vendor <billing@vendor.example>",
    "subject": "Your invoice is ready",
    "snippet": "Invoice #123 due",
    "date_iso": "2026-07-08T00:00:00Z",
    "labels": ["INBOX"],
    "classification": "actionable",
}


# ── _store_email_entity ──────────────────────────────────────────────────────


def test_store_email_entity_posts_to_store_not_observations(monkeypatch):
    turdus = _load_turdus(monkeypatch)
    capture: dict = {}
    _install_fake_client(
        monkeypatch,
        payload={"entities": [{"entity_id": "ent_email_1"}]},
        capture=capture,
    )

    entity_id = asyncio.run(turdus._store_email_entity(_MESSAGE))

    assert entity_id == "ent_email_1"
    [call] = capture["calls"]
    assert call["url"].endswith("/store")
    assert not call["url"].endswith("/observations")


def test_store_email_entity_payload_shape(monkeypatch):
    turdus = _load_turdus(monkeypatch)
    capture: dict = {}
    _install_fake_client(
        monkeypatch,
        payload={"entities": [{"entity_id": "ent_email_1"}]},
        capture=capture,
    )

    asyncio.run(turdus._store_email_entity(_MESSAGE))

    [call] = capture["calls"]
    body = call["json"]
    assert body["idempotency_key"] == "turdus-email-msg-1"
    assert isinstance(body["entities"], list) and len(body["entities"]) == 1
    entity = body["entities"][0]
    assert entity["entity_type"] == "email_message"
    assert entity["canonical_name"] == "email_message:gmail:msg-1"
    # Flat, not nested: /store drops an unrecognized "snapshot" key wholesale.
    assert entity["sender"] == _MESSAGE["sender"]
    assert entity["subject"] == _MESSAGE["subject"]


# ── _create_task_for_email ───────────────────────────────────────────────────


def test_create_task_for_email_posts_to_store_not_observations(monkeypatch):
    turdus = _load_turdus(monkeypatch)
    capture: dict = {}
    _install_fake_client(
        monkeypatch,
        payload={"entities": [{"entity_id": "ent_task_1"}]},
        capture=capture,
    )

    asyncio.run(turdus._create_task_for_email(_MESSAGE, "ent_email_1"))

    urls = [call["url"] for call in capture["calls"]]
    assert any(u.endswith("/store") for u in urls)
    assert not any(u.endswith("/observations") for u in urls)


def test_create_task_for_email_payload_shape(monkeypatch):
    turdus = _load_turdus(monkeypatch)
    capture: dict = {}
    _install_fake_client(
        monkeypatch,
        payload={"entities": [{"entity_id": "ent_task_1"}]},
        capture=capture,
    )

    asyncio.run(turdus._create_task_for_email(_MESSAGE, "ent_email_1"))

    store_call = next(c for c in capture["calls"] if c["url"].endswith("/store"))
    body = store_call["json"]
    assert body["idempotency_key"] == "turdus-task-msg-1"
    assert isinstance(body["entities"], list) and len(body["entities"]) == 1
    entity = body["entities"][0]
    assert entity["entity_type"] == "task"
    assert entity["canonical_name"] == "task:turdus:email:msg-1"

    # /store reads entity fields FLAT. A nested "snapshot" key is not a
    # recognized field, so everything under it is dropped silently and the
    # task lands carrying only its canonical_name — which never dispatches,
    # because Apis routes on status/title/tags that are no longer there.
    assert "snapshot" not in entity, (
        "fields must be flat on the entity; a nested 'snapshot' is dropped by /store"
    )
    assert entity["title"]
    assert entity["description"]
    assert entity["status"] == "open"
    # ateles#682: blast radius must key on what the task does, not on the
    # agent that would handle it.
    assert entity["action_type"] == "compute_only_analysis"
    assert entity["confidence"] == 0.8
    # _MESSAGE is invoice-shaped, so this exercises the monedula branch.
    assert entity["assigned_to"] == "monedula"
    assert entity["priority"] == "urgent"
    assert entity["domain"] == "finance"


def test_create_task_for_plain_actionable_email_is_flat(monkeypatch):
    """The non-invoice branch also lands its fields flat."""
    turdus = _load_turdus(monkeypatch)
    capture: dict = {}
    _install_fake_client(
        monkeypatch,
        payload={"entities": [{"entity_id": "ent_task_1"}]},
        capture=capture,
    )

    plain = dict(_MESSAGE)
    plain["subject"] = "Can we meet on Thursday?"
    plain["sender"] = "Colleague <colleague@example.com>"
    plain["snippet"] = "Let me know what time works."
    assert not turdus._is_invoice(plain["sender"], plain["subject"], plain["snippet"])

    asyncio.run(turdus._create_task_for_email(plain, "ent_email_1"))

    entity = next(
        c for c in capture["calls"] if c["url"].endswith("/store")
    )["json"]["entities"][0]
    assert "snapshot" not in entity
    assert entity["status"] == "open"
    assert entity["title"].startswith("Email triage:")
    assert entity["action_type"] == "compute_only_analysis"
    assert "assigned_to" not in entity


def test_store_email_entity_payload_is_flat(monkeypatch):
    """email_message fields must be flat too — 497 shells were stored nested."""
    turdus = _load_turdus(monkeypatch)
    capture: dict = {}
    _install_fake_client(
        monkeypatch,
        payload={"entities": [{"entity_id": "ent_email_1"}]},
        capture=capture,
    )

    asyncio.run(turdus._store_email_entity(_MESSAGE))

    entity = next(
        c for c in capture["calls"] if c["url"].endswith("/store")
    )["json"]["entities"][0]
    assert "snapshot" not in entity
    assert entity["entity_type"] == "email_message"
    assert entity["message_id"] == "msg-1"
    assert entity["source"] == "gmail"


def test_create_task_for_email_links_task_to_email_entity(monkeypatch):
    turdus = _load_turdus(monkeypatch)
    capture: dict = {}
    _install_fake_client(
        monkeypatch,
        payload={"entities": [{"entity_id": "ent_task_1"}]},
        capture=capture,
    )

    asyncio.run(turdus._create_task_for_email(_MESSAGE, "ent_email_1"))

    rel_call = next(
        c for c in capture["calls"] if c["url"].endswith("/create_relationship")
    )
    assert rel_call["json"]["source_entity_id"] == "ent_task_1"
    assert rel_call["json"]["target_entity_id"] == "ent_email_1"
    assert rel_call["json"]["relationship_type"] == "REFERS_TO"


# ── response-parsing contract (entity_id extraction) ─────────────────────────
#
# gating.py::write_checkpoint_brief — the only other known-good /store caller
# in this repo — always reads `entities[0].entity_id`, never a top-level
# `entity_id`. turdus's `data.get("entity_id") or (entities[0].entity_id)`
# primary branch has no precedent elsewhere; this locks in the real /store
# response shape so a future drift is caught here instead of failing silently
# in production.


def test_store_email_entity_parses_real_store_response_shape(monkeypatch):
    """Canned response matches the actual /store contract: entities[0].entity_id,
    no top-level entity_id."""
    turdus = _load_turdus(monkeypatch)
    _install_fake_client(
        monkeypatch, payload={"entities": [{"entity_id": "ent_real_shape"}]}
    )

    entity_id = asyncio.run(turdus._store_email_entity(_MESSAGE))

    assert entity_id == "ent_real_shape"


def test_store_email_entity_top_level_entity_id_fallback(monkeypatch):
    """If /store ever returns a top-level entity_id, it must still resolve
    (documents the untested fallback branch flagged in review)."""
    turdus = _load_turdus(monkeypatch)
    _install_fake_client(monkeypatch, payload={"entity_id": "ent_top_level"})

    entity_id = asyncio.run(turdus._store_email_entity(_MESSAGE))

    assert entity_id == "ent_top_level"


def test_store_email_entity_missing_entity_id_returns_none(monkeypatch):
    turdus = _load_turdus(monkeypatch)
    _install_fake_client(monkeypatch, payload={"entities": []})

    entity_id = asyncio.run(turdus._store_email_entity(_MESSAGE))

    assert entity_id is None


# ── fail-open behavior ────────────────────────────────────────────────────────


def test_store_email_entity_no_token_returns_none(monkeypatch):
    turdus = _load_turdus(monkeypatch, bearer_token="")

    entity_id = asyncio.run(turdus._store_email_entity(_MESSAGE))

    assert entity_id is None


def test_store_email_entity_http_error_fails_soft(monkeypatch):
    turdus = _load_turdus(monkeypatch)

    class _RaisingClient(_FakeAsyncClient):
        async def post(self, url, json=None, **kwargs):
            raise RuntimeError("network down")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: _RaisingClient())

    entity_id = asyncio.run(turdus._store_email_entity(_MESSAGE))

    assert entity_id is None
