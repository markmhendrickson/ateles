"""`published_summary` is not a declared `release_result` field.

Neotoma accepts an undeclared field, routes it to raw_fragments, and the
snapshot never shows it, so the v0.29.0 read-back could only fail and the log
said nothing about which field or why. The fake models that: it serves the
declared-field list at GET /schemas/release_result and drops undeclared fields.
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
DECLARED = {"version", "status", "release_url", "reason", "branch"}


class Fake:
    def __init__(self, schema_ok=True):
        self.snap = {"version": "v0.29.0", "status": "publishing"}
        self.corrected = []
        self.schema_ok = schema_ok

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/schemas/release_result":
            if not self.schema_ok:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={"schema_definition": {"fields": {f: {"type": "string"} for f in DECLARED}}},
            )
        if request.method == "POST" and path == "/correct":
            body = json.loads(request.content)
            self.corrected.append(body["field"])
            if body["field"] in DECLARED:  # undeclared -> raw_fragments, not snapshot
                self.snap[body["field"]] = body["value"]
            return httpx.Response(200, json={"success": True})
        if request.method == "GET" and path == f"/entities/{ENT}":
            return httpx.Response(200, json={"snapshot": dict(self.snap)})
        return httpx.Response(404)


@pytest.fixture
def fake(monkeypatch):
    f = Fake()
    monkeypatch.setattr(publish, "NEOTOMA_BASE_URL", "https://neotoma.test")
    real_client = httpx.Client
    monkeypatch.setattr(
        httpx, "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(f.handler), **kw),
    )
    return f


def test_undeclared_summary_is_not_written_and_log_names_the_field(fake, caplog):
    with caplog.at_level("WARNING"):
        publish.set_release_status(
            "v0.29.0", "published",
            {"release_url": "https://x/r", "published_summary": "probes: ran"},
            entity_id=ENT,
        )
    assert "published_summary" not in fake.corrected
    assert fake.snap["release_url"] == "https://x/r"
    assert fake.snap["status"] == "published"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("published_summary" in m and "not declared" in m and "raw_fragments" in m for m in msgs)
    assert not any(r.levelname == "ERROR" for r in caplog.records)


def test_schema_unreadable_still_attempts_write_and_error_names_field(fake, caplog):
    fake.schema_ok = False
    with caplog.at_level("ERROR"):
        publish.set_release_status(
            "v0.29.0", "published", {"published_summary": "probes: ran"}, entity_id=ENT
        )
    assert "published_summary" in fake.corrected
    errs = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("field 'published_summary'" in m and "undeclared" in m for m in errs)
    assert fake.snap["status"] == "published"


def test_declared_extra_still_written(fake):
    publish.set_release_status("v0.29.0", "failed", {"reason": "boom"}, entity_id=ENT)
    assert fake.snap["reason"] == "boom"
