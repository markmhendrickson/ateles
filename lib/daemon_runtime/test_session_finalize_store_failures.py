"""Run-session status writes must validate, and sustained failure must escalate."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path

import httpx
import pytest

from lib.daemon_runtime import session_finalize as sf

_SCHEMA = json.loads(
    (Path(__file__).parent / "fixtures" / "agent_session_schema.json").read_text()
)


class _FakeNeotoma:
    """Mimics /store: schema fields and idempotency-key content check."""

    def __init__(self):
        self.seen: dict[str, str] = {}
        self.bodies: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None):  # noqa: A002
        import json as _json

        self.bodies.append(json)
        req = httpx.Request("POST", url)
        digest = hashlib.sha256(
            _json_dumps(json["entities"]).encode()
        ).hexdigest()
        key = json.get("idempotency_key")
        if key in self.seen and self.seen[key] != digest:
            return httpx.Response(
                400,
                request=req,
                content=_json.dumps({"error": {"code": "ERR_IDEMPOTENCY_MISMATCH"}}).encode(),
            )
        self.seen[key] = digest
        return httpx.Response(200, request=req, content=b'{"entities": []}')


def _json_dumps(obj) -> str:
    return json.dumps(obj, sort_keys=True)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "tok")
    for name in ("_consecutive_failures", "_escalated"):
        getattr(sf, name, set()).clear()


def _run():
    return sf.RunSession(
        conversation_id="ent_c", agent_session_id="ent_s", native_session_id="ent_t:approved-0"
    )


def test_status_payload_matches_agent_session_schema(monkeypatch):
    fake = _FakeNeotoma()
    monkeypatch.setattr(sf.httpx, "post", fake.post)
    monkeypatch.setattr(sf, "_entity_readback_matches", lambda *a, **k: True)
    assert sf.update_run_session_status(_run(), status="completed") is True
    entity = fake.bodies[0]["entities"][0]
    assert entity.pop("entity_type") == _SCHEMA["entity_type"]
    assert set(_SCHEMA["required_fields"]) <= set(entity)
    assert set(entity) <= set(_SCHEMA["fields"])
    run_entities = sf.build_run_session_payload(task_id="ent_t", agent="cicada", run_key="r")["entities"]
    session = [e for e in run_entities if e["entity_type"] == "agent_session"][0]
    assert set(_SCHEMA["required_fields"]) <= set(session)
    assert set(session) - {"entity_type"} <= set(_SCHEMA["fields"])


def test_repeated_status_update_is_not_an_idempotency_mismatch(monkeypatch):
    """The watchdog re-runs this each sweep; the second write used to 400 forever."""
    fake = _FakeNeotoma()
    monkeypatch.setattr(sf.httpx, "post", fake.post)
    monkeypatch.setattr(sf, "_entity_readback_matches", lambda *a, **k: True)
    assert sf.update_run_session_status(_run(), status="completed") is True
    assert sf.update_run_session_status(_run(), status="completed") is True
    assert len({b["idempotency_key"] for b in fake.bodies}) == 2


def test_store_failure_logs_response_body(monkeypatch, caplog):
    fake = _FakeNeotoma()
    fake.seen["k"] = "different"
    monkeypatch.setattr(sf.httpx, "post", fake.post)
    with caplog.at_level(logging.WARNING, logger="daemon_runtime.finalize"):
        assert sf._post_store({"entities": [], "idempotency_key": "k"}) is None
    assert "ERR_IDEMPOTENCY_MISMATCH" in caplog.text


def test_sustained_failure_escalates_once_with_daemon_report(monkeypatch, caplog):
    posts: list[dict] = []

    def fail_then_record(url, headers=None, json=None, timeout=None):  # noqa: A002
        posts.append(json)
        req = httpx.Request("POST", url)
        if json["entities"] and json["entities"][0].get("entity_type") == "daemon_report":
            return httpx.Response(200, request=req, content=b"{}")
        return httpx.Response(400, request=req, content=b"{}")

    monkeypatch.setattr(sf.httpx, "post", fail_then_record)
    body = {"entities": [{"entity_type": "agent_session"}], "idempotency_key": "kk"}
    with caplog.at_level(logging.ERROR, logger="daemon_runtime.finalize"):
        for _ in range(getattr(sf, "ESCALATE_AFTER_CONSECUTIVE_FAILURES", 5) + 3):
            assert sf._post_store(body) is None
    reports = [p for p in posts if p["entities"][0].get("entity_type") == "daemon_report"]
    assert len(reports) == 1
    assert reports[0]["entities"][0]["severity"] == "error"
    assert "consecutive" in caplog.text


def test_success_resets_failure_streak(monkeypatch):
    results = iter([400] * (getattr(sf, "ESCALATE_AFTER_CONSECUTIVE_FAILURES", 5) - 1) + [200] + [400] * 2)
    posts: list[dict] = []

    def post(url, headers=None, json=None, timeout=None):  # noqa: A002
        posts.append(json)
        return httpx.Response(next(results), request=httpx.Request("POST", url), content=b"{}")

    monkeypatch.setattr(sf.httpx, "post", post)
    body = {"entities": [], "idempotency_key": "kk"}
    for _ in range(getattr(sf, "ESCALATE_AFTER_CONSECUTIVE_FAILURES", 5) + 2):
        sf._post_store(body)
    assert not any(p["entities"] and p["entities"][0].get("entity_type") == "daemon_report" for p in posts)
