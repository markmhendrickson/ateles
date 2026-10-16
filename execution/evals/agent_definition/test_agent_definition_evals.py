"""Agent-definition eval: a definition that cannot be loaded now fails closed.

Collected by the existing ``pytest execution/evals/`` step. Offline: no key, no
network, no waiting. See ``agent_definition_harness.py`` and ``scenarios.json``.

Run: pytest execution/evals/agent_definition/ -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import agent_definition_harness as harness  # noqa: E402

SCENARIOS = harness.load()["scenarios"]
IDS = [s["id"] for s in SCENARIOS]
BY_ID = {s["id"]: s for s in SCENARIOS}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_scenario(scenario):
    obs = harness.run_scenario(scenario)
    assert harness.check(scenario, obs) == [], scenario["why"]


def test_scenario_ids_are_unique_and_documented():
    assert len(set(IDS)) == len(IDS)
    assert all(s.get("why") and s.get("expect") and s.get("kind") for s in SCENARIOS)


# Planted red: each mutation reinstates a defect; the scenarios named must catch it.
RED = [
    ("reinstate_stub", "a_persistent_timeout_refuses_the_dispatch_and_spawns_nothing"),
    ("reinstate_stub", "a_daemon_that_cannot_read_its_definition_exits_instead_of_skipping_the_status_check"),
    ("no_retry", "a_timeout_then_success_dispatches_on_the_real_definition"),
    ("no_retry", "a_server_error_then_success_loads_the_real_definition"),
    ("no_retry", "a_daemon_survives_one_timeout_at_startup"),
    ("retry_rejected", "a_refused_request_is_final_at_once"),
    ("loose_absence", "a_malformed_answer_is_not_absence"),
    ("loose_absence", "a_result_for_another_agent_is_not_absence"),
    ("loose_absence", "a_route_error_is_not_absence"),
    ("old_deadline", "a_load_whose_every_request_hangs_ends_inside_the_deadline_counting_the_requests"),
]


@pytest.mark.parametrize("mutation,scenario_id", RED, ids=[f"{m}-{s[:44]}" for m, s in RED])
def test_the_scenario_goes_red_when_the_defect_is_reinstated(mutation, scenario_id):
    scenario = BY_ID[scenario_id]
    assert harness.check(scenario, harness.run_scenario(scenario)) == []
    problems = harness.check(scenario, harness.run_scenario(scenario, mutation=mutation))
    assert problems, f"{mutation} passed: the eval cannot tell the designs apart"
