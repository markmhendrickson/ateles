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
    assert len({b["idempotency_key"] for b in fake.bodies}) == 1  # true replay, deduped


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


def _always_fail(posts):
    def post(url, headers=None, json=None, timeout=None):  # noqa: A002
        posts.append(json)
        req = httpx.Request("POST", url)
        if json["entities"][0].get("entity_type") == "daemon_report":
            return httpx.Response(200, request=req, content=b"{}")
        return httpx.Response(503, request=req, content=b"<html>gateway 10.0.0.1</html>")
    return post


def _reports(posts):
    return [p for p in posts if p["entities"][0].get("entity_type") == "daemon_report"]


def test_update_run_session_status_escalates_once_after_n_failures(monkeypatch):
    posts: list[dict] = []
    monkeypatch.setattr(sf.httpx, "post", _always_fail(posts))
    n = getattr(sf, "ESCALATE_AFTER_CONSECUTIVE_FAILURES", 5)
    for _ in range(n + 4):
        assert sf.update_run_session_status(_run(), status="completed") is False
    reports = _reports(posts)
    assert len(reports) == 1
    msg = reports[0]["entities"][0]["message"]
    assert "ent_t:approved-0" in msg and "completed" in msg
    assert "10.0.0.1" not in msg  # HTML body scrubbed
    assert len(getattr(sf, "_consecutive_failures", {})) <= 1  # one logical write


def test_update_run_session_status_success_resets_streak(monkeypatch):
    posts: list[dict] = []
    monkeypatch.setattr(sf.httpx, "post", _always_fail(posts))
    n = getattr(sf, "ESCALATE_AFTER_CONSECUTIVE_FAILURES", 5)
    for _ in range(n - 1):
        sf.update_run_session_status(_run(), status="completed")
    fake = _FakeNeotoma()
    monkeypatch.setattr(sf.httpx, "post", fake.post)
    monkeypatch.setattr(sf, "_entity_readback_matches", lambda *a, **k: True)
    assert sf.update_run_session_status(_run(), status="completed") is True
    monkeypatch.setattr(sf.httpx, "post", _always_fail(posts))
    for _ in range(n - 1):
        sf.update_run_session_status(_run(), status="completed")
    assert _reports(posts) == []


def test_failure_tracking_is_bounded(monkeypatch):
    monkeypatch.setattr(sf.httpx, "post", _always_fail([]))
    cap = getattr(sf, "MAX_TRACKED_WRITES", 256)
    for i in range(cap + 50):
        sf._post_store({"entities": [{"entity_type": "x"}], "idempotency_key": f"k{i}"})
    assert len(sf._consecutive_failures) <= cap


def test_retry_transient_branch_records_outcomes(monkeypatch):
    posts: list[dict] = []
    monkeypatch.setattr(sf.httpx, "post", _always_fail(posts))
    n = getattr(sf, "ESCALATE_AFTER_CONSECUTIVE_FAILURES", 5)
    body = {"entities": [{"entity_type": "x"}], "idempotency_key": "rk"}
    for _ in range(n):
        sf._post_store(body, retry_transient=True, streak_key="w")
    assert len(_reports(posts)) == 1
    ok = _FakeNeotoma()
    monkeypatch.setattr(sf.httpx, "post", ok.post)
    sf._post_store(body, retry_transient=True, streak_key="w")
    assert "w" not in sf._consecutive_failures


def test_finalize_session_failures_escalate(monkeypatch):
    posts: list[dict] = []
    monkeypatch.setattr(sf.httpx, "post", _always_fail(posts))
    n = getattr(sf, "ESCALATE_AFTER_CONSECUTIVE_FAILURES", 5)
    for _ in range(n):
        assert sf.finalize_session(trigger_text="t", outcome_text="o", handler="apis") is False
    assert len(_reports(posts)) == 1


def test_response_body_scrubbed_to_code_and_message():
    resp = httpx.Response(
        400, request=httpx.Request("POST", "https://x/store"),
        content=b'{"error":{"code":"ERR_X","message":"bad","trace":"10.1.1.1"}}',
    )
    text = sf.describe_store_error(httpx.HTTPStatusError("400", request=resp.request, response=resp))
    assert "ERR_X bad" in text and "10.1.1.1" not in text
