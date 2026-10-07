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
