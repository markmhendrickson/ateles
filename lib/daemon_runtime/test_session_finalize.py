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
