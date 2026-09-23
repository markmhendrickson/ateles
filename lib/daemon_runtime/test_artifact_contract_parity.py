"""Parity: shared ARTIFACT_CONTRACTS is the only source (ateles#1155 / principle 9).

Fails if Anthus consumer map, Apis role map, or cicada agent docs disagree
with the shared declaration.
"""

from __future__ import annotations

import sys
from pathlib import Path

from artifact_contract import (
    gate_satisfaction_rules,
    role_required_artifact,
)

_REPO = Path(__file__).resolve().parents[2]
_ANTHUS = _REPO / "execution" / "daemons" / "anthus"
if str(_ANTHUS) not in sys.path:
    sys.path.insert(0, str(_ANTHUS))

import orchestrator  # noqa: E402


# Frozen baseline of every gate that existed pre-#1155 — regenerating the
# shared table without these keys must fail the parity test.
_EXPECTED_GATES = {
    "pm": "acceptance_criteria",
    "ux": "copy_and_ux_flow",
    "copy": "copy_and_ux_flow",
    "pm_scope": "acceptance_criteria",
    "ux_design": "copy_and_ux_flow",
    "growth_announce": "launch_brief",
    "social_draft": "social_post_draft",
    "devrel_docs": "docs_diff_or_no_change_note",
    "arch": "schema_or_api_proposal",
    "impl": "pull_request_link",
    "qa": "test_plan",
    "legal": "compliance_review",
    "compliance_supervisor": "compliance_verdict",
    "pr_review": "merge_decision",
    "release": "release_note",
    "draft": "social_post_draft",
    "draft_lint": "lint_report",
    "post": "published_post_link",
}


def test_gate_satisfaction_rules_match_anthus_consumer():
    derived = gate_satisfaction_rules()
    assert derived == orchestrator.GATE_SATISFACTION_RULES
    for gate, kind in _EXPECTED_GATES.items():
        assert derived.get(gate) == kind, f"missing/wrong gate {gate}"


def test_cicada_role_kind_is_pull_request_link():
    contract = role_required_artifact()["cicada"]
    assert contract.artifact_kind == "pull_request_link"
    assert "impl" in contract.gates


def test_cicada_agent_docs_require_pull_request_link():
    paths = [
        _REPO / "docs" / "agents" / "cicada.md",
        _REPO / ".claude" / "skills" / "cicada" / "SKILL.md",
    ]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "pull_request_link" in text, f"{path} missing pull_request_link"


def test_anthus_impl_kind_matches_apis_cicada_kind():
    assert (
        orchestrator.GATE_SATISFACTION_RULES["impl"]
        == role_required_artifact()["cicada"].artifact_kind
    )
