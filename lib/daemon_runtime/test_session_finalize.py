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
    # the conversation (entity index 0) is anchored PART_OF both plan and task
    conv_targets = {
        r.get("target_entity_id") for r in b["relationships"]
        if r.get("source_index") == 0 and r["relationship_type"] == "PART_OF"
    }
    assert {"ent_plan", "ent_task"} <= conv_targets
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


def test_build_run_conversation_anchors_task_and_plan():
    b = sf.build_run_conversation_payload(
        task_id="ent_task", plan_id="ent_plan", agent="cicada", run_key="created-0",
    )
    assert [e["entity_type"] for e in b["entities"]] == ["conversation"]
    anchors = {
        r["target_entity_id"] for r in b["relationships"]
        if r["relationship_type"] == "PART_OF" and r["source_index"] == 0
    }
    assert anchors == {"ent_task", "ent_plan"}
    # run_key in the idempotency key → SSE replay reuses, retry (new key) opens new
    assert b["idempotency_key"] == "run-conv-ent_task-created-0"


def test_build_run_conversation_task_only():
    b = sf.build_run_conversation_payload(task_id="ent_task", agent="cicada", run_key="r1")
    assert len(b["relationships"]) == 1
    assert b["relationships"][0]["target_entity_id"] == "ent_task"


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
    monkeypatch.setattr(sf.httpx, "post", lambda *a, **k: _R())
    cid = sf.create_run_conversation(task_id="ent_task", agent="cicada", run_key="created-0")
    assert cid == "ent_conv99"


def test_create_run_conversation_fail_open(monkeypatch):
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "")
    assert sf.create_run_conversation(task_id="ent_task", agent="cicada", run_key="r") is None


def test_append_turn_posts_and_fails_open(monkeypatch):
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "tok")
    monkeypatch.setattr(sf.httpx, "post", lambda *a, **k: _Resp())
    assert sf.append_turn(conversation_id="ent_conv", role="assistant", content="progress") is True
    monkeypatch.setattr(sf, "NEOTOMA_BEARER_TOKEN", "")
    assert sf.append_turn(conversation_id="ent_conv", role="assistant", content="x") is False
