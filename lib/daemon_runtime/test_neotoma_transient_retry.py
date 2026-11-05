"""Bounded retry of transient Neotoma failures on the fail-closed paths."""

from __future__ import annotations

import httpx
import pytest

from lib.daemon_runtime import gating
from lib.daemon_runtime import neotoma_transient as nt
from lib.daemon_runtime import session_finalize as sf


class _Resp:
    def __init__(self, payload=None, status=200):
        self._payload = payload if payload is not None else {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            req = httpx.Request("GET", "http://x")
            raise httpx.HTTPStatusError(
                "bad", request=req, response=httpx.Response(self.status_code, request=req)
            )

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _no_pauses(monkeypatch):
    pauses: list[float] = []
    monkeypatch.setattr(nt, "_sleep", pauses.append)
    monkeypatch.setattr(gating, "NEOTOMA_BEARER_TOKEN", "t")
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "t")
    return pauses


def _scripted(monkeypatch, module, name, script, calls):
    items = list(script)

    def fake(*args, **kwargs):
        calls.append(kwargs)
        item = items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(module.httpx, name, fake)


# ── _fetch_entity ────────────────────────────────────────────────────────────


def test_fetch_entity_timeout_then_success(monkeypatch, _no_pauses):
    calls: list = []
    _scripted(monkeypatch, gating, "get",
              [httpx.ReadTimeout("slow"), _Resp({"entity_id": "e"})], calls)
    assert gating._fetch_entity("e") == {"entity_id": "e"}
    assert len(calls) == 2
    assert _no_pauses == [2.0]


def test_fetch_entity_4xx_attempted_once(monkeypatch, _no_pauses):
    calls: list = []
    _scripted(monkeypatch, gating, "get", [_Resp(status=404)], calls)
    assert gating._fetch_entity("e") is None
    assert len(calls) == 1 and _no_pauses == []


def test_fetch_entity_all_attempts_time_out(monkeypatch, _no_pauses):
    calls: list = []
    _scripted(monkeypatch, gating, "get", [httpx.ReadTimeout("slow")] * 3, calls)
    assert gating._fetch_entity("e") is None
    assert len(calls) == 3
    assert _no_pauses == [2.0, 5.0]


# ── run-session saves and read-backs ─────────────────────────────────────────


def test_post_store_retry_sends_same_idempotency_key(monkeypatch):
    calls: list = []
    _scripted(monkeypatch, sf, "post",
              [httpx.ConnectTimeout("slow"), _Resp({"entities": []})], calls)
    body = {"idempotency_key": "run-session-k", "entities": []}
    assert sf._post_store(body, retry_transient=True) == {"entities": []}
    assert [c["json"]["idempotency_key"] for c in calls] == ["run-session-k"] * 2


def test_post_store_default_does_not_retry(monkeypatch):
    calls: list = []
    _scripted(monkeypatch, sf, "post", [httpx.ReadTimeout("slow")], calls)
    assert sf._post_store({"idempotency_key": "k"}) is None
    assert len(calls) == 1


def test_post_store_4xx_attempted_once(monkeypatch):
    calls: list = []
    _scripted(monkeypatch, sf, "post", [_Resp(status=400)], calls)
    assert sf._post_store({"idempotency_key": "k"}, retry_transient=True) is None
    assert len(calls) == 1


def test_create_run_session_survives_one_timed_out_save(monkeypatch):
    task, key = "ent_task", "r1"
    native = f"{task}:{key}"
    store_calls: list = []
    stored = {"entities": [
        {"entity_type": "conversation", "entity_id": "c"},
        {"entity_type": "agent_session", "entity_id": "s"},
    ]}
    _scripted(monkeypatch, sf, "post",
              [httpx.ReadTimeout("slow"), _Resp(stored)], store_calls)

    def get(url, **kw):
        if url.endswith("/relationships"):
            src = url.split("/entities/")[1].split("/")[0]
            targets = {"c": [task, "s"], "s": [task]}[src]
            return _Resp({"relationships": [
                {"source_entity_id": src, "target_entity_id": t,
                 "relationship_type": "REFERS_TO"} for t in targets]})
        if url.endswith("/c"):
            return _Resp({"entity_type": "conversation", "snapshot": {
                "conversation_id": native, "session_id": native}})
        return _Resp({"entity_type": "agent_session", "snapshot": {
            "harness": "ateles-swarm", "native_session_id": native}})

    monkeypatch.setattr(sf.httpx, "get", get)
    run = sf.create_run_session(task_id=task, agent="a", run_key=key)
    assert run is not None and run.agent_session_id == "s"
    keys = {c["json"]["idempotency_key"] for c in store_calls}
    assert len(store_calls) == 2 and keys == {f"run-session-{task}-{key}"}


def test_readback_timeout_then_match(monkeypatch):
    calls: list = []
    _scripted(monkeypatch, sf, "get", [
        httpx.ReadTimeout("slow"),
        _Resp({"entity_type": "conversation", "snapshot": {"conversation_id": "want"}}),
    ], calls)
    assert sf._entity_readback_matches(
        "c", entity_type="conversation",
        required_fields={"conversation_id": "want"}, retry_transient=True) is True
    assert len(calls) == 2


def test_readback_wrong_value_not_retried(monkeypatch):
    calls: list = []
    _scripted(monkeypatch, sf, "get", [
        _Resp({"entity_type": "conversation", "snapshot": {"conversation_id": "other"}}),
    ], calls)
    assert sf._entity_readback_matches(
        "c", entity_type="conversation",
        required_fields={"conversation_id": "want"}, retry_transient=True) is False
    assert len(calls) == 1


def test_run_session_all_saves_time_out_returns_none(monkeypatch):
    calls: list = []
    _scripted(monkeypatch, sf, "post", [httpx.ReadTimeout("slow")] * 3, calls)
    assert sf.create_run_session(task_id="t", agent="a", run_key="r") is None
    assert len(calls) == 3


# ── checkpoint_brief persist ─────────────────────────────────────────────────


def test_checkpoint_persist_retries_with_same_key(monkeypatch):
    calls: list = []
    _scripted(monkeypatch, gating, "post", [
        httpx.ReadTimeout("slow"),
        _Resp({"entities": [{"entity_id": "ent_cp"}]}),
    ], calls)
    monkeypatch.setattr(gating, "NEOTOMA_BASE_URL", "http://n")
    policy = gating.ExecutionPolicy(entity_id="default", loaded=True)
    decision = gating.evaluate_gate(
        confidence=0.3, action_type="local_edit", policy=policy)
    got = gating.write_checkpoint_brief(
        task_entity_id="ent_task", decision=decision, title="t",
        plan_summary="p", handler="apis", action_type="local_edit")
    assert got == "ent_cp"
    assert len(calls) == 2
    assert (calls[0]["json"]["idempotency_key"]
            == calls[1]["json"]["idempotency_key"])


def test_checkpoint_persist_4xx_attempted_once(monkeypatch):
    calls: list = []
    _scripted(monkeypatch, gating, "post", [_Resp(status=400)], calls)
    monkeypatch.setattr(gating, "NEOTOMA_BASE_URL", "http://n")
    policy = gating.ExecutionPolicy(entity_id="default", loaded=True)
    decision = gating.evaluate_gate(
        confidence=0.3, action_type="local_edit", policy=policy)
    assert gating.write_checkpoint_brief(
        task_entity_id="ent_task", decision=decision, title="t",
        plan_summary="p", handler="apis", action_type="local_edit") is None
    assert len(calls) == 1


# ── _release_lifecycle_proven (Apis) ─────────────────────────────────────────


def _apis():
    import importlib.util  # noqa: F401
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    d = str(root / "execution" / "daemons" / "apis")
    if d not in sys.path:
        sys.path.insert(0, d)
    import apis  # type: ignore

    return apis


def _task(status, blocked=""):
    return _Resp({"entity_id": "ent_t", "entity_type": "task",
                  "snapshot": {"status": status, "blocked_reason": blocked}})


def test_release_lifecycle_proven_timeout_then_match(monkeypatch):
    apis = _apis()
    calls: list = []
    _scripted(monkeypatch, gating, "get",
              [httpx.ReadTimeout("slow"), _task("executing")], calls)
    assert apis._release_lifecycle_proven("ent_t", apis.TaskStatus.EXECUTING) is True
    assert len(calls) == 2


def test_release_lifecycle_mismatch_not_retried(monkeypatch):
    apis = _apis()
    calls: list = []
    _scripted(monkeypatch, gating, "get", [_task("routed")], calls)
    assert apis._release_lifecycle_proven("ent_t", apis.TaskStatus.EXECUTING) is False
    assert len(calls) == 1
