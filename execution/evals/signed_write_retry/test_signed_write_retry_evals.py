"""Signed-write retry eval: a transient failure of a lens-signed gate write is retried,
bounded and idempotent, and everything else still fails closed at once.

Collected by the existing ``pytest execution/evals/`` step. Offline: no key, no
network, no waiting. See ``signed_write_retry_harness.py`` and ``scenarios.json``.

Run: pytest execution/evals/signed_write_retry/ -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import signed_write_retry_harness as harness  # noqa: E402

SCENARIOS = harness.load()["scenarios"]
IDS = [s["id"] for s in SCENARIOS]
BY_ID = {s["id"]: s for s in SCENARIOS}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_scenario(scenario):
    obs = harness.run_scenario(scenario)
    assert harness.check(scenario, obs) == [], scenario["why"]


def test_scenario_ids_are_unique_and_documented():
    assert len(set(IDS)) == len(IDS)
    assert all(s.get("why") and s.get("expect") and s.get("steps") for s in SCENARIOS)


# Planted red: each mutation reinstates a defect; the scenarios named must catch it.
RED = [
    ("no_retry", "one_transport_error_on_a_gate_that_already_reads_signed_off_ends_verified"),
    ("no_retry", "a_timeout_then_a_503_then_success_uses_both_retries_with_the_stated_backoff"),
    ("no_retry", "a_gate_write_that_landed_with_a_lost_response_is_replayed_not_applied_twice"),
    ("retry_4xx", "a_refused_4xx_is_never_retried"),
    ("timeout_before_thread_only", "an_attempt_queued_past_the_deadline_issues_no_write"),
    ("old_cap", "a_write_whose_every_attempt_hangs_ends_inside_the_cap_counting_the_attempts"),
    ("fresh_key_per_attempt", "a_gate_write_that_landed_with_a_lost_response_is_replayed_not_applied_twice"),
    ("fresh_key_per_attempt", "a_history_entry_that_landed_with_a_lost_response_is_not_doubled"),
]


@pytest.mark.parametrize("mutation,scenario_id", RED, ids=[f"{m}-{s[:40]}" for m, s in RED])
def test_the_scenario_goes_red_when_the_defect_is_reinstated(mutation, scenario_id):
    scenario = BY_ID[scenario_id]
    assert harness.check(scenario, harness.run_scenario(scenario)) == []
    problems = harness.check(scenario, harness.run_scenario(scenario, mutation=mutation))
    assert problems, f"{mutation} passed: the eval cannot tell the designs apart"
