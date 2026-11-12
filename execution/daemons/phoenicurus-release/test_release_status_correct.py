"""set_release_status must take effect after a prior correction, and verify it.

Fake Neotoma semantics: a correction pins a field (priority 1000) and a later
/store observation of that field is ignored, exactly as in production. The
v0.28.0 symptom: log said "status -> published", snapshot still read approved.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _DAEMON_DIR.parent.parent.parent
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import publish  # noqa: E402

ENT = "ent_release_1"


class FakeNeotoma:
    def __init__(self, status="approved", pinned=("status",), entities=None):
        self.snap = {"version": "v0.28.0", "status": status}
        self.pinned = set(pinned)
        self.entities = [ENT] if entities is None else entities
        self.stores = 0

    def query(self, entity_type, limit=100):
        return [{"entity_id": e, "snapshot": dict(self.snap)} for e in self.entities]

    def store(self, entities, key):
        self.stores += 1
        for rec in entities:
            for k, v in rec.items():
                if k not in self.pinned and k != "entity_type":
                    self.snap[k] = v
        return {"ok": True}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/correct":
            body = json.loads(request.content)
            self.snap[body["field"]] = body["value"]
            self.pinned.add(body["field"])
            return httpx.Response(200, json={"success": True})
        if request.method == "GET" and request.url.path == f"/entities/{ENT}":
            return httpx.Response(200, json={"snapshot": dict(self.snap)})
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch):
    f = FakeNeotoma()
    monkeypatch.setattr(publish, "NEOTOMA_BASE_URL", "https://neotoma.test")
    monkeypatch.setattr(publish, "neotoma_query", f.query)
    monkeypatch.setattr(publish, "neotoma_store", f.store)
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(f.handler), **kw),
    )
    return f


def test_store_only_write_is_ignored_after_a_correction(fake):
    """The old behaviour: documents why the status write needed to change."""
    publish.neotoma_store([{"entity_type": "release_result", "status": "published"}], "k")
    assert fake.snap["status"] == "approved"  # silently ignored


def test_status_takes_effect_after_a_correction_and_is_read_back(fake):
    publish.set_release_status("v0.28.0", "publishing")
    publish.set_release_status("v0.28.0", "published", {"release_url": "https://x/r"})
    assert fake.snap["status"] == "published"
    assert fake.snap["release_url"] == "https://x/r"


def test_unconfirmed_readback_raises_and_logs_error(fake, monkeypatch, caplog):
    # A server that accepts the correction but never reflects it.
    monkeypatch.setattr(
        fake, "handler",
        lambda r: httpx.Response(200, json={"snapshot": {"status": "approved"}}),
    )
    with caplog.at_level("ERROR"):
        with pytest.raises(publish.StatusWriteError):
            publish.set_release_status("v0.28.0", "published")
    assert any("NOT confirmed" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("entities", [[], ["ent_a", "ent_b"]])
def test_unresolvable_entity_fails_closed_without_writing(fake, entities):
    fake.entities = entities
    with pytest.raises(publish.StatusWriteError):
        publish.set_release_status("v0.28.0", "published")
    assert fake.snap["status"] == "approved"
    assert fake.stores == 0


def test_main_halts_without_writing_failed_when_status_write_fails(monkeypatch):
    sent = []
    calls = []
    monkeypatch.setattr(sys, "argv", ["publish.py", "--version", "v0.28.0"])
    monkeypatch.setattr(publish, "find_release", lambda v, e: {"entity_id": ENT, "snapshot": {"version": "v0.28.0"}})
    monkeypatch.setattr(publish, "telegram_send", sent.append)

    def boom(*a, **k):
        raise publish.StatusWriteError("unconfirmed")

    monkeypatch.setattr(publish, "publish_release", boom)
    monkeypatch.setattr(publish, "set_release_status", lambda *a, **k: calls.append(a))
    assert publish.main() == 1
    assert calls == []  # never tries to record "failed" through the broken path
    assert "unverified" in sent[0]
