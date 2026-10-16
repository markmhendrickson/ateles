"""Dispatch-gate eval: the pre-gate producer score (ateles#1142).

Collected by the existing ``pytest execution/evals/`` step in
``.github/workflows/ateles-tests.yml`` (the swarm's eval lane for agent-facing
behaviour). Offline: no model is called; the scenarios are in
``scenarios.json`` and every scenario drives the real ``apis.dispatch_task``.

Run: pytest execution/evals/dispatch_gate/ -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dispatch_gate_harness as harness  # noqa: E402

DATA = harness.load()
SCENARIOS = DATA["scenarios"]
BASE = DATA["base_task"]
IDS = [s["id"] for s in SCENARIOS]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_scenario(scenario):
    obs = harness.run_scenario(scenario, BASE)
    assert harness.check(scenario, obs) == [], scenario["why"]


def test_scenario_ids_are_unique_and_documented():
    assert len(set(IDS)) == len(IDS)
    assert all(s.get("why") and s.get("expect") for s in SCENARIOS)


def test_the_eval_covers_every_required_behaviour():
    required = {
        "high_score_executes", "low_score_checkpoints_with_explanation",
        "unparsable_reply_stays_unscored", "scorer_refused_by_pace_gate_stays_unscored",
        "scorer_crash_stays_unscored", "scorer_timeout_stays_unscored",
        "high_blast_is_never_scored", "explicit_score_is_not_rescored",
        "operator_override_is_not_scored", "tier_mapped_to_top_switches_scorer_off",
        "hard_floor_caps_a_producer_score", "just_below_threshold_is_held_unrounded",
        "over_long_task_is_declined_not_partially_scored",
        "auto_execution_is_recorded_durably_with_the_exact_value_before_dispatch",
        "failed_assessment_write_checkpoints_instead_of_executing",
    }
    assert required <= set(IDS)


# An eval whose checks cannot fail is not an eval: with the producer removed,
# every scenario that expects a score to be obtained must go red.
PRODUCER_DEPENDENT = [s for s in SCENARIOS if s["expect"]["scorer_called"]]


@pytest.mark.parametrize("scenario", PRODUCER_DEPENDENT, ids=[s["id"] for s in PRODUCER_DEPENDENT])
def test_checks_go_red_without_the_producer(scenario):
    obs = harness.run_scenario(scenario, BASE, mutation="no_producer")
    assert harness.check(scenario, obs), "this scenario cannot detect the producer's absence"


def test_checks_go_red_if_the_assessment_is_not_actually_written():
    """A dispatcher that claims the record landed without writing it is caught."""
    scenario = next(
        s for s in SCENARIOS
        if s["id"] == "auto_execution_is_recorded_durably_with_the_exact_value_before_dispatch"
    )
    obs = harness.run_scenario(scenario, BASE, mutation="skip_assessment")
    assert harness.check(scenario, obs)


def test_checks_go_red_if_the_score_is_rounded_before_the_gate():
    scenario = next(s for s in SCENARIOS if s["id"] == "just_below_threshold_is_held_unrounded")
    obs = harness.run_scenario(scenario, BASE, mutation="round_before_gate")
    assert harness.check(scenario, obs)


CONTENT_IDS = {
    "content_risky_low_prior_never_spawns", "content_report_only_preserves_cicada_prior",
    "content_declared_unknown_action_held", "content_declared_operator_only_held",
    "content_effective_never_prior_preserved", "content_unicode_body_escalates",
    "content_unicode_description_escalates", "content_drivers_supplied",
    "content_drivers_missing", "content_preview_raw_policy_parity",
}


def test_content_gate_required_eval_scenarios_are_committed():
    assert CONTENT_IDS <= set(IDS)


@pytest.mark.parametrize("scenario", [s for s in SCENARIOS if s["id"] in {
    "content_risky_low_prior_never_spawns", "content_unicode_body_escalates",
    "content_unicode_description_escalates",
}], ids=lambda s: s["id"])
def test_content_checks_go_red_without_content_escalation(scenario):
    assert harness.check(scenario, harness.run_scenario(scenario, BASE, mutation="no_content"))


def test_preview_checks_go_red_with_raw_policy_sets():
    scenario = next(s for s in SCENARIOS if s["id"] == "content_preview_raw_policy_parity")
    assert harness.check(scenario, harness.run_scenario(scenario, BASE, mutation="raw_preview"))


@pytest.mark.parametrize("raw_policy", [
    {},
    {"high_blast_action_types": [], "low_blast_action_types": []},
    {"high_blast_action_types": "[]", "low_blast_action_types": "[]"},
    {"high_blast_action_types": "malformed", "low_blast_action_types": None},
    {"blast_radius_default": " HIGH "},
    {"blast_radius_default": "invalid"},
    {"blast_radius_default": None},
    {"high_blast_action_types": '[" PAYMENT "]',
     "low_blast_action_types": [" COMPUTE_ONLY_ANALYSIS "]},
])
@pytest.mark.parametrize("declared", [None, "local_edit", "operator_only", "unknown_action"])
def test_same_raw_policy_through_actual_dispatch_and_mcp(raw_policy, declared):
    scenario = {
        "task": {"title": "implement a new feature", "action_type": declared,
                 "confidence": 0.99},
        "policy": raw_policy,
        "preview": {"task_description": "implement a new feature", "action_type": declared},
    }
    observed = harness.run_scenario(scenario, BASE)
    assert observed["action"] == {
        key: observed["preview"][key]
        for key in ("action_type", "action_type_source", "action_blast_radius")
    }
