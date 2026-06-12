#!/usr/bin/env python3
"""Tests for the observe-only session-integrity tracker (ateles#6 layer 2).

Verifies the classification logic and the write-detection heuristics without
touching Neotoma (finalize()'s network writes are no-ops when no bearer token
is set, which is the case under test).
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Ensure no token so _post() is a no-op during tests.
os.environ.pop("NEOTOMA_BEARER_TOKEN", None)

from session_integrity import SessionIntegrityTracker, _is_write_tool, _entity_types  # noqa: E402


def _mk():
    return SessionIntegrityTracker(agent_sub="tester@ateles-swarm", server_name="mcpsrv_neotoma")


# ── write-tool detection ─────────────────────────────────────────────────────

def test_write_tool_detection():
    assert _is_write_tool("mcp__mcpsrv_neotoma__store")
    assert _is_write_tool("mcp__mcpsrv_neotoma__correct")
    assert _is_write_tool("mcp__mcpsrv_neotoma__create_relationship")
    assert _is_write_tool("mcp__mcpsrv_neotoma__submit_issue")
    assert not _is_write_tool("mcp__mcpsrv_neotoma__retrieve_entities")
    assert not _is_write_tool("mcp__mcpsrv_neotoma__list_observations")


def test_entity_types_extraction():
    args = {"entities": [{"entity_type": "analysis"}, {"entity_type": "conversation_message"}]}
    assert _entity_types(args) == {"analysis", "conversation_message"}
    assert _entity_types({"entity_type": "plan"}) == {"plan"}
    assert _entity_types({}) == set()


# ── classification ───────────────────────────────────────────────────────────

def test_noop_session_is_exempt():
    t = _mk()
    t.observe("mcp__mcpsrv_neotoma__retrieve_entities", {"search": "x"})
    assert t.classify() == "exempt"
    assert t.write_count == 0


def test_bookkeeping_only_is_exempt():
    t = _mk()
    # Storing only conversation/conversation_message is bookkeeping, not domain.
    t.observe("mcp__mcpsrv_neotoma__store", {"entities": [
        {"entity_type": "conversation"},
        {"entity_type": "conversation_message"},
    ]})
    assert t.classify() == "exempt"
    assert t.wrote_domain is False
    assert t.write_count == 1  # counted as a write, but not domain


def test_domain_write_without_plan_is_violated():
    t = _mk()
    t.observe("mcp__mcpsrv_neotoma__store", {"entities": [{"entity_type": "finding"}]})
    assert t.wrote_domain is True
    assert t.bound_plan is False
    assert t.classify() == "violated"


def test_domain_write_with_plan_is_integral():
    t = _mk()
    t.observe("mcp__mcpsrv_neotoma__store", {"entities": [{"entity_type": "analysis"}]})
    t.observe("mcp__mcpsrv_neotoma__correct", {"entity_type": "plan", "entity_id": "ent_99ace4dd6673aa36ed08b1fe"})
    assert t.classify() == "integral"
    assert "ent_99ace4dd6673aa36ed08b1fe" in t.plan_ids


def test_plan_binding_via_part_of_relationship():
    t = _mk()
    t.observe("mcp__mcpsrv_neotoma__store", {
        "entities": [{"entity_type": "analysis"}],
        "relationships": [{"relationship_type": "PART_OF", "target_entity_id": "ent_99ace4dd6673aa36ed08b1fe"}],
        "plan_id": "ent_99ace4dd6673aa36ed08b1fe",
    })
    assert t.bound_plan is True
    assert t.classify() == "integral"


def test_bookkeeping_relationship_not_counted_domain():
    t = _mk()
    # create_relationship between two msgs, no plan — bookkeeping, not domain.
    t.observe("mcp__mcpsrv_neotoma__create_relationship", {
        "relationship_type": "PART_OF",
        "source_entity_id": "ent_msg",
        "target_entity_id": "ent_conv",
    })
    assert t.wrote_domain is False
    assert t.classify() == "exempt"


def test_finalize_is_idempotent_and_safe_without_token():
    t = _mk()
    t.observe("mcp__mcpsrv_neotoma__store", {"entities": [{"entity_type": "finding"}]})
    t.finalize()
    t.finalize()  # second call is a no-op, must not raise
    assert t._finalized is True


if __name__ == "__main__":
    import subprocess
    raise SystemExit(subprocess.call(["python3", "-m", "pytest", __file__, "-q"]))
