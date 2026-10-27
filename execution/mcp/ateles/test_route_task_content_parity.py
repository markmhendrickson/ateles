"""MCP `route_task` classifies blast radius from content, not only from a
caller-supplied `action_type` (ateles#682).

Before, omitting `action_type` returned no classification at all, so the
preview could not tell a filing agent that a report-only task and a PR task
differ, and could not say what the dispatcher's gate would do. It now resolves
the SAME way the gate does (declared -> agent prior -> escalation by task text, under the effective policy), and the
shared case table in `lib.daemon_runtime.action_type_parity_cases` is also run
through the dispatcher's resolver in
`execution/daemons/apis/test_gate_action_type_parity.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import server as srv  # noqa: E402
from lib.daemon_runtime.action_type_parity_cases import (  # noqa: E402
    PARITY_CASES,
    PARITY_POLICY,
)

_ROSTER = {
    "entity_id": "ent_roster",
    "roster_key": "default",
    "swarm_domain": "ateles-swarm",
    "roles": {
        "code": "cicada",
        "payments": "monedula",
        "dispatcher": "apis",
        "pr_steward": "vanellus",
    },
}
_POLICY_ENTITY = {"snapshot": dict(PARITY_POLICY, title="parity", confidence_threshold=0.85)}


def _route(description, action_type=None):
    with (
        patch("server._get_swarm_roster", return_value=_ROSTER),
        patch("server._retrieve_entities", return_value=[]),
        patch("server._get", return_value=_POLICY_ENTITY),
    ):
        return srv._route_task(description, action_type)


@pytest.mark.parametrize(
    "case_id, description, declared, agent, expected_type, expected_source, expected_blast",
    PARITY_CASES,
    ids=[c[0] for c in PARITY_CASES],
)
def test_route_task_matches_the_shared_parity_table(
    case_id, description, declared, agent, expected_type, expected_source, expected_blast
):
    result = _route(description, declared)
    assert result["matched_agent"] == agent
    if expected_type is None:
        assert "action_type" not in result
        assert "action_blast_radius" not in result
        return
    assert result["action_type"] == expected_type
    assert result["action_type_source"] == expected_source
    assert result["action_blast_radius"] == expected_blast


def test_omitting_action_type_still_yields_a_blast_classification():
    """The regression assertion: fails against the old `if action_type:` guard."""
    result = _route("implement a new feature")
    assert "action_blast_radius" in result


def test_a_declared_value_is_echoed_exactly_as_written():
    assert _route("implement the change", "  Open_Or_Merge_PR ")["action_type"] == "  Open_Or_Merge_PR "


def test_a_low_declared_type_with_outward_text_is_reported_as_escalated():
    result = _route("implement the local cleanup, then push it", "local_edit")
    assert result["action_type"] == "git_push"
    assert result["action_type_source"] == "declared_escalated"
    assert result["action_blast_radius"] == "high"
    assert "push" in result["action_type_note"]


def test_a_low_declared_type_with_neutral_text_stays_declared_with_no_note():
    result = _route("implement the local cleanup", "local_edit")
    assert result["action_type"] == "local_edit"
    assert result["action_type_source"] == "declared"
    assert "action_type_note" not in result


def test_report_only_language_does_not_make_the_preview_less_strict():
    report = _route("Review the code path. Write NOTHING - this job's only output is a divergence report.")
    explicit_pr = _route("implement the issue", "open_or_merge_pr")
    assert (report["action_blast_radius"], explicit_pr["action_blast_radius"]) == ("high", "high")


def test_the_description_can_escalate_an_agent_with_no_prior():
    result = _route("something completely unrelated, but open a PR for it")
    assert (result["action_type"], result["action_type_source"]) == ("open_or_merge_pr", "content")


def _route_with_policy(description, policy_snapshot):
    with (
        patch("server._get_swarm_roster", return_value=_ROSTER),
        patch("server._retrieve_entities", return_value=[]),
        patch("server._get", return_value={"snapshot": policy_snapshot}),
    ):
        return srv._route_task(description)


def test_escalation_follows_the_effective_policy_not_the_default_vocabulary():
    # open_or_merge_pr is unclassified here, so Cicada's prior is "never"; the
    # description names a payment, which this policy classifies HIGH. Replacing
    # never with high would make the preview (and the gate) less strict.
    policy = {
        "title": "t",
        "blast_radius_default": "low",
        "high_blast_action_types": ["payment"],
        "low_blast_action_types": ["compute_only_analysis"],
    }
    result = _route_with_policy("implement it and pay the invoice for the rent", policy)
    assert result["matched_agent"] == "cicada"
    assert (result["action_type"], result["action_type_source"]) == (
        "open_or_merge_pr", "agent_prior",
    )
    assert result["action_blast_radius"] == "never"


def test_no_policy_means_no_escalation_only_the_prior():
    with (
        patch("server._get_swarm_roster", return_value=_ROSTER),
        patch("server._retrieve_entities", return_value=[]),
        patch("server._get", return_value=None),
    ):
        result = srv._route_task("something completely unrelated, but open a PR for it")
    assert "action_type" not in result


@pytest.mark.parametrize("raw_policy", [
    {"title": "omitted sets"},
    {"title": "empty sets", "high_blast_action_types": [], "low_blast_action_types": []},
    {"title": "encoded empty sets", "high_blast_action_types": "[]", "low_blast_action_types": "[]"},
    {"title": "malformed sets", "high_blast_action_types": "bad", "low_blast_action_types": None},
    {"title": "normalized default", "blast_radius_default": " HIGH "},
    {"title": "invalid default", "blast_radius_default": "invalid"},
    {"title": "null default", "blast_radius_default": None},
    {"title": "custom sets", "high_blast_action_types": '[" PAYMENT "]',
     "low_blast_action_types": [" COMPUTE_ONLY_ANALYSIS "]},
])
@pytest.mark.parametrize("description,declared", [
    ("implement a new feature", None),
    ("something unrelated but open a PR for it", None),
    ("something unrelated but pay the invoice", None),
    ("implement a new feature", "local_edit"),
    ("implement the local cleanup, then push it", "local_edit"),
    ("implement a new feature", "operator_only"),
    ("implement a new feature", "unknown_action"),
])
def test_same_raw_record_agrees_with_dispatcher_effective_policy(raw_policy, description, declared):
    # Exercise the actual production parser and dispatcher resolver, not a
    # pre-populated ExecutionPolicy that hides default/normalization drift.
    from lib.daemon_runtime.gating import _parse_policy

    record = {"snapshot": {"snapshot": raw_policy}}
    with (
        patch("server._get_swarm_roster", return_value=_ROSTER),
        patch("server._retrieve_entities", return_value=[]),
        patch("server._get", return_value=record),
    ):
        preview = srv._route_task(description, declared)
    effective = _parse_policy("raw-policy", record)
    from lib.daemon_runtime.action_type import AGENT_ACTION_TYPE_PRIOR, resolve_gate_action_type
    resolved = resolve_gate_action_type(
        preview["matched_agent"], {"title": description, "action_type": declared},
        AGENT_ACTION_TYPE_PRIOR, verdict=lambda at: effective.blast_radius_for(at).value,
    )
    assert preview.get("action_type") == resolved.value
    assert preview.get("action_type_source", "none") == resolved.source
    if resolved.value is None:
        assert "action_blast_radius" not in preview
    else:
        assert preview["action_blast_radius"] == effective.blast_radius_for(resolved.value).value
