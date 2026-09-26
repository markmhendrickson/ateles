"""Tests for the shared session-finalize routine (/end convergence)."""

from __future__ import annotations

from lib.daemon_runtime import session_finalize as sf


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {}


# ── pure payload builder ─────────────────────────────────────────────────────


def test_build_payload_new_conversation_with_learning():
    b = sf.build_finalize_payload(
        trigger_text="task X due", outcome_text="did X", handler="apis",
        plan_id="ent_plan", task_id="ent_task", learning="X needs retries",
    )
    types = [e["entity_type"] for e in b["entities"]]
    assert types.count("agent_message") == 2
    assert "conversation" in types
    assert "learning" in types
    roles = {e.get("role") for e in b["entities"] if e["entity_type"] == "agent_message"}
    assert roles == {"user", "assistant"}
    conv_refs = {
        r.get("target_entity_id") for r in b["relationships"]
        if r.get("source_index") == 0 and r["relationship_type"] == "REFERS_TO"
    }
    assert conv_refs == {"ent_task"}
    assert not any(
        r.get("source_index") == 0 and r["relationship_type"] == "PART_OF"
        for r in b["relationships"]
    )
    learning_index = types.index("learning")
    membership = [
        r for r in b["relationships"]
        if r.get("source_index") == learning_index and r["relationship_type"] == "PART_OF"
    ]
    assert [r["target_entity_id"] for r in membership] == ["ent_task"]
    assert b.get("idempotency_key")


def test_build_payload_existing_conversation_no_dup():
    b = sf.build_finalize_payload(
        trigger_text="t", outcome_text="o", handler="formica",
        conversation_id="ent_conv", task_id="ent_task2",
    )
    types = [e["entity_type"] for e in b["entities"]]
    assert "conversation" not in types  # do not recreate the conversation
    assert "learning" not in types       # none supplied
    msg_links = [
        r for r in b["relationships"]
        if r["relationship_type"] == "PART_OF" and r.get("target_entity_id") == "ent_conv"
    ]
    assert len(msg_links) == 2  # both messages link to the existing conversation


# ── I/O contract ─────────────────────────────────────────────────────────────


def test_finalize_session_posts_to_store(monkeypatch):
    calls: list[tuple] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append((url, json, headers))
        return _Resp()

    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(sf.httpx, "post", fake_post)
    ok = sf.finalize_session(
        trigger_text="t", outcome_text="o", handler="apis", task_id="ent_task",
    )
    assert ok is True
    assert len(calls) == 1
    url, body, headers = calls[0]
    assert url.endswith("/store")
    assert "entities" in body and "relationships" in body
    assert headers["Authorization"] == "Bearer tok"


def test_finalize_session_fail_open_without_token(monkeypatch):
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "")
    assert sf.finalize_session(trigger_text="t", outcome_text="o", handler="apis") is False


def test_finalize_session_fail_open_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(sf.httpx, "post", boom)
    assert sf.finalize_session(trigger_text="t", outcome_text="o", handler="apis") is False


def test_load_end_skill_parses_content(monkeypatch):
    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"snapshot": {"snapshot": {"content": "# end\nbody"}}}

    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(sf.httpx, "get", lambda *a, **k: _R())
    assert sf.load_end_skill() == "# end\nbody"


def test_load_end_skill_fail_open(monkeypatch):
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "")
    assert sf.load_end_skill() is None


# ── E1: conversation-per-execution-run ───────────────────────────────────────


def test_build_run_conversation_refers_to_task_and_uses_task_ascent():
    b = sf.build_run_conversation_payload(
        task_id="ent_task", plan_id="ent_plan", agent="cicada", run_key="created-0",
    )
    assert [e["entity_type"] for e in b["entities"]] == ["conversation"]
    refs = {
        r["target_entity_id"] for r in b["relationships"]
        if r["relationship_type"] == "REFERS_TO" and r["source_index"] == 0
    }
    assert refs == {"ent_task"}
    assert not any(r["relationship_type"] == "PART_OF" for r in b["relationships"])
    # run_key in the idempotency key → SSE replay reuses, retry (new key) opens new
    assert b["idempotency_key"] == "run-conv-ent_task-created-0"


def test_build_run_conversation_task_only():
    b = sf.build_run_conversation_payload(task_id="ent_task", agent="cicada", run_key="r1")
    assert len(b["relationships"]) == 1
    assert b["relationships"][0]["target_entity_id"] == "ent_task"
    assert b["relationships"][0]["relationship_type"] == "REFERS_TO"


def test_build_run_session_persists_runtime_identity_and_provenance():
    b = sf.build_run_session_payload(
        task_id="ent_task", plan_id="ent_plan", agent="cicada", run_key="created-0",
    )
    assert [e["entity_type"] for e in b["entities"]] == ["conversation", "agent_session"]
    conversation, session = b["entities"]
    assert conversation["session_id"] == session["native_session_id"]
    assert session["harness"] == "ateles-swarm"
    assert session["kind"] == "autonomous"
    assert session["trigger_ref"] == "ent_task"
    session_refs = [
        r for r in b["relationships"]
        if r.get("source_index") == 1 and r["relationship_type"] == "REFERS_TO"
    ]
    assert [r["target_entity_id"] for r in session_refs] == ["ent_task"]
    assert "ent_plan" not in {r.get("target_entity_id") for r in b["relationships"]}


def test_build_turn_payload_appends_to_conversation():
    b = sf.build_turn_payload(
        conversation_id="ent_conv", role="user", content="reply", sender_kind="operator",
    )
    assert [e["entity_type"] for e in b["entities"]] == ["agent_message"]
    assert b["entities"][0]["sender_kind"] == "operator"
    rel = b["relationships"][0]
    assert rel["target_entity_id"] == "ent_conv" and rel["relationship_type"] == "PART_OF"


def test_create_run_conversation_returns_id(monkeypatch):
    class _R:
        def raise_for_status(self):
            pass

        def json(self):
            return {"entities": [{"entity_type": "conversation", "entity_id": "ent_conv99"}]}

    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "tok")
    class _Get:
        def raise_for_status(self): pass
        def json(self):
            return {"entity_id": "ent_conv99", "entity_type": "conversation",
                    "snapshot": {"session_id": "ent_task:created-0"}}

    monkeypatch.setattr(sf.httpx, "post", lambda *a, **k: _R())
    monkeypatch.setattr(sf.httpx, "get", lambda *a, **k: _Get())
    cid = sf.create_run_conversation(task_id="ent_task", agent="cicada", run_key="created-0")
    assert cid == "ent_conv99"


def test_create_run_conversation_rejects_unreadable_write(monkeypatch):
    class _Post:
        def raise_for_status(self): pass
        def json(self):
            return {"entities": [{"entity_type": "conversation", "entity_id": "ent_conv99"}]}
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(sf.httpx, "post", lambda *a, **k: _Post())
    monkeypatch.setattr(sf.httpx, "get", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    assert sf.create_run_conversation(task_id="ent_task", agent="cicada", run_key="created-0") is None


def test_create_run_session_reads_back_both_records_and_task_link(monkeypatch):
    class _Response:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data
    def fake_get(url, **_kwargs):
        if url.endswith("/entities/ent_conv99"):
            return _Response({"entity_id": "ent_conv99", "entity_type": "conversation",
                              "snapshot": {"session_id": "ent_task:created-0"}})
        if url.endswith("/entities/ent_session99"):
            return _Response({"entity_id": "ent_session99", "entity_type": "agent_session",
                              "snapshot": {"harness": "ateles-swarm",
                                           "native_session_id": "ent_task:created-0"}})
        return _Response({"relationships": [{"source_entity_id": "ent_session99",
                                              "target_entity_id": "ent_task",
                                              "relationship_type": "REFERS_TO"}]})
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(sf.httpx, "post", lambda *a, **k: _Response({"entities": [
        {"entity_type": "conversation", "entity_id": "ent_conv99"},
        {"entity_type": "agent_session", "entity_id": "ent_session99"},
    ]}))
    monkeypatch.setattr(sf.httpx, "get", fake_get)
    run = sf.create_run_session(task_id="ent_task", plan_id="ent_plan",
                                agent="cicada", run_key="created-0")
    assert run == sf.RunSession("ent_conv99", "ent_session99", "ent_task:created-0")


def test_update_run_session_status_reads_back_terminal_state(monkeypatch):
    run = sf.RunSession("ent_conv99", "ent_session99", "ent_task:created-0")
    posted = []
    class _Response:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data
    def fake_post(*_args, **kwargs):
        posted.append(kwargs["json"])
        return _Response({"entities": [{"entity_type": "agent_session",
                                         "entity_id": "ent_session99"}]})
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(sf.httpx, "post", fake_post)
    monkeypatch.setattr(sf.httpx, "get", lambda *a, **k: _Response({
        "entity_id": "ent_session99", "entity_type": "agent_session",
        "snapshot": {"harness": "ateles-swarm", "native_session_id": "ent_task:created-0",
                     "status": "completed"}}))
    assert sf.update_run_session_status(run, status="completed") is True
    assert posted[0]["entities"][0]["status"] == "completed"


def test_create_run_conversation_fail_open(monkeypatch):
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "")
    assert sf.create_run_conversation(task_id="ent_task", agent="cicada", run_key="r") is None


def test_append_turn_posts_and_fails_open(monkeypatch):
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(sf.httpx, "post", lambda *a, **k: _Resp())
    assert sf.append_turn(conversation_id="ent_conv", role="assistant", content="progress") is True
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "")
    assert sf.append_turn(conversation_id="ent_conv", role="assistant", content="x") is False
