"""MCP `route_task` classifies blast radius from content, not only from a
caller-supplied `action_type` (ateles#682).

Before, omitting `action_type` returned no classification at all, so the
preview could not tell a filing agent that a report-only task and a PR task
differ, and could not say what the dispatcher's gate would do. It now resolves
the SAME way the gate does (declared -> agent prior -> task text), and the
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


def test_read_only_report_is_low_and_an_explicit_pr_is_high():
    low = _route("Review the code path. Write NOTHING - this job's only output is a divergence report.")
    high = _route("implement the issue", "open_or_merge_pr")
    assert (low["action_blast_radius"], high["action_blast_radius"]) == ("low", "high")
