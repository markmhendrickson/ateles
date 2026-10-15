"""Owner-history eval: a gate sign-off writes only what the store's array reducer needs.

Collected by the existing ``pytest execution/evals/`` step. Offline: no key, no
network. See ``owner_history_harness.py`` for the store model and ``scenarios.json``.

Run: pytest execution/evals/owner_history/ -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import owner_history_harness as harness  # noqa: E402

DATA = harness.load()
SCENARIOS = DATA["scenarios"]
IDS = [s["id"] for s in SCENARIOS]
BY_ID = {s["id"]: s for s in SCENARIOS}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_scenario(scenario):
    obs = harness.run_scenario(scenario)
    assert harness.check(scenario, obs) == [], scenario["why"]


def test_scenario_ids_are_unique_and_documented():
    assert len(set(IDS)) == len(IDS)
    assert all(s.get("why") and s.get("expect") and s.get("steps") for s in SCENARIOS)


GROWTH = [
    "repeated_sign_offs_do_not_grow_a_pre_doubled_history",
    "triage_history_is_carried_up_once_and_never_again",
]


def test_the_failure_scenarios_go_red_if_a_failure_note_is_suppressed_by_an_earlier_one():
    """Planted red: the rejected rule, a failure note dropped when an identical one
    (same gate, reason and observed state) exists ANYWHERE in the history."""
    scenario = BY_ID["a_failure_on_each_of_two_commits_stays_attributable_and_the_history_does_not_end_as_success"]
    obs = harness.run_scenario(scenario, mutation="suppress_repeated_failure_notes")
    problems = harness.check(scenario, obs)
    assert problems and any("ends on" in p or "history" in p for p in problems), problems


def test_the_window_scenario_goes_red_with_a_fixed_observation_window():
    scenario = BY_ID["an_older_history_correction_is_not_missed_behind_many_newer_observations"]
    obs = harness.run_scenario(scenario, mutation="fixed_window")
    problems = harness.check(scenario, obs)
    assert problems and any("length" in p or "write" in p for p in problems), problems


@pytest.mark.parametrize("scenario_id", GROWTH)
def test_the_growth_scenarios_go_red_with_the_old_writer(scenario_id):
    """Planted red: resend the history that was read, plus one entry."""
    scenario = BY_ID[scenario_id]
    obs = harness.run_scenario(scenario, mutation="old_writer")
    problems = harness.check(scenario, obs)
    assert problems, "the old writer passed: the eval cannot tell the designs apart"
    assert any("length" in p or "write" in p for p in problems), problems


def test_the_old_writer_reproduces_the_canary_growth():
    """Ten sign-offs from an empty history: 1, 3, 7, ... 1023 entries."""
    scenario = {
        "gates": {"pm": "pending"},
        "steps": [{"gate": "pm", "head": f"{i:040x}"} for i in range(1, 11)],
        "expect": {},
    }
    obs = harness.run_scenario(scenario, mutation="old_writer")
    assert len(obs["store"].owner_history) == 2**10 - 1
    clean = harness.run_scenario(scenario)
    assert len(clean["store"].owner_history) == 10
