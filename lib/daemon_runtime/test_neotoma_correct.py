"""Corrections go to POST /correct (the route that exists); non-2xx is a failure."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime import grant_checker  # noqa: E402
from lib.daemon_runtime.neotoma_correct import correct_field  # noqa: E402

BASE = "https://neotoma.test"


class Server:
    """Fake Neotoma: only /correct and GET /entities/<id> exist; anything else 404s."""

    def __init__(self, status=200, store=True):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.entities: dict[str, dict] = {}
        self.status = status
        self.store = store

    def handler(self, request: httpx.Request) -> httpx.Response:
        import json

        path = request.url.path
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, path, body))
        if request.method == "POST" and path == "/correct":
            if self.status != 200:
                return httpx.Response(self.status, json={"error": "x"})
            if self.store:
                self.entities.setdefault(body["entity_id"], {})[body["field"]] = body["value"]
            return httpx.Response(200, json={"success": True})
        if request.method == "GET" and path.startswith("/entities/"):
            eid = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"snapshot": self.entities.get(eid, {})})
        return httpx.Response(404, json={"error": "Cannot POST " + path})

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.handler))


def _posts(server):
    return [(p, b) for m, p, b in server.calls if m == "POST"]


def test_correct_field_posts_to_correct_with_payload():
    s = Server()
    assert correct_field(s.client(), BASE, "ent_1", "task", "due_date", "2026-01-01", "k1")
    assert _posts(s) == [
        (
            "/correct",
            {
                "entity_id": "ent_1",
                "entity_type": "task",
                "field": "due_date",
                "value": "2026-01-01",
                "idempotency_key": "k1",
            },
        )
    ]


def test_404_is_a_failure():
    s = Server(status=404)
    assert correct_field(s.client(), BASE, "ent_1", "task", "f", "v", "k") is False


def test_readback_mismatch_is_a_failure(caplog):
    s = Server(store=False)
    assert correct_field(s.client(), BASE, "ent_1", "task", "f", "v", "k") is False
    assert "MISMATCH" in caplog.text


def test_grant_suspend_uses_correct_one_field_per_call(monkeypatch):
    s = Server()
    monkeypatch.setattr(grant_checker, "NEOTOMA_BEARER_TOKEN", "t")
    monkeypatch.setattr(grant_checker, "NEOTOMA_BASE_URL", BASE)
    real = httpx.Client
    monkeypatch.setattr(
        grant_checker.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(s.handler))
    )
    assert grant_checker.suspend_grant("ent_g", "why") is True
    posts = _posts(s)
    assert {p for p, _ in posts} == {"/correct"}
    assert [b["field"] for _, b in posts] == ["status", "suspended_reason", "suspended_at"]
    assert all(b["entity_type"] == "agent_grant" for _, b in posts)


def test_grant_restore_and_404_failure(monkeypatch):
    s = Server(status=404)
    monkeypatch.setattr(grant_checker, "NEOTOMA_BEARER_TOKEN", "t")
    monkeypatch.setattr(grant_checker, "NEOTOMA_BASE_URL", BASE)
    real = httpx.Client
    monkeypatch.setattr(
        grant_checker.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(s.handler))
    )
    assert grant_checker.restore_grant("ent_g") is False


def test_lanius_mark_stale_posts_to_correct(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "lanius_sweep", _REPO_ROOT / "execution" / "scripts" / "lanius_sweep.py"
    )
    lanius = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lanius)
    monkeypatch.setattr(lanius, "NEOTOMA_BASE_URL", BASE)
    s = Server()
    assert lanius.mark_stale(s.client(), "ent_i", dry_run=False) is True
    (path, body), = _posts(s)
    assert path == "/correct"
    assert (body["entity_type"], body["field"], body["value"]) == ("issue", "status", "stale")
    s404 = Server(status=404)
    assert lanius.mark_stale(s404.client(), "ent_i", dry_run=False) is False


def test_neotoma_agent_hygiene_posts_to_correct(monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "neotoma_agent_mod", _REPO_ROOT / "execution/daemons/neotoma-agent/neotoma_agent.py"
    )
    agent = importlib.util.module_from_spec(spec)
    sys.modules["neotoma_agent_mod"] = agent
    spec.loader.exec_module(agent)
    monkeypatch.setattr(agent, "NEOTOMA_BEARER_TOKEN", "t")
    monkeypatch.setattr(agent, "NEOTOMA_BASE_URL", BASE)
    s = Server()
    real = httpx.Client
    monkeypatch.setattr(
        agent.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(s.handler))
    )
    asyncio.run(agent.apply_due_date_hygiene("ent_t", {"title": "pay invoice", "body": ""}))
    posts = _posts(s)
    assert posts and {p for p, _ in posts} == {"/correct"}
    assert all(b["entity_type"] == "task" and b["entity_id"] == "ent_t" for _, b in posts)
    assert "due_date" in [b["field"] for _, b in posts]

    s404 = Server(status=404)
    monkeypatch.setattr(
        agent.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(s404.handler))
    )
    import logging

    errs = []
    h = logging.Handler()
    h.emit = lambda r: errs.append(r) if r.levelno >= logging.ERROR else None
    logging.getLogger().addHandler(h)
    asyncio.run(agent.apply_due_date_hygiene("ent_t", {"title": "pay invoice", "body": ""}))
    logging.getLogger().removeHandler(h)
    assert any("NOT applied" in r.getMessage() for r in errs)


def test_readback_never_passes_on_root_identity_with_empty_snapshot():
    from lib.daemon_runtime.neotoma_correct import _readback_matches

    ident = {"entity_type": "task", "title": "T"}
    for body in (
        {**ident, "snapshot": {}},
        {**ident, "snapshot": None},
        {"entity": {**ident, "snapshot": {}}},
        {**ident, "snapshot": {"snapshot": {}}},
    ):
        # value None would equal a missing field, so this is the worst case
        assert not _readback_matches(body, "notes", None), body
        assert not _readback_matches(body, "title", "T"), body


def test_readback_reads_present_snapshot_only():
    from lib.daemon_runtime.neotoma_correct import _readback_matches

    assert _readback_matches({"entity": {"snapshot": {"a": 1}}}, "a", 1)
    assert _readback_matches({"snapshot": {"snapshot": {"a": 1}}}, "a", 1)
    assert _readback_matches({"a": 1, "entity_type": "task"}, "a", 1)  # flat, no key
    # root field is ignored when a snapshot key exists
    assert not _readback_matches({"a": 1, "snapshot": {"b": 2}}, "a", 1)
