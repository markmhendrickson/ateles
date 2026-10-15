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
