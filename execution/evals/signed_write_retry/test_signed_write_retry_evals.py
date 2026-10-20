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
    # Arch finding: the retry must not be sent under a different signing principal.
    ("no_principal_check", "a_landed_write_with_a_lost_response_is_not_retried_when_the_signing_principal_changed"),
    ("no_retry", "a_response_that_is_not_json_after_a_landed_write_is_retried_with_the_same_key"),
]


@pytest.mark.parametrize("mutation,scenario_id", RED, ids=[f"{m}-{s[:40]}" for m, s in RED])
def test_the_scenario_goes_red_when_the_defect_is_reinstated(mutation, scenario_id):
    scenario = BY_ID[scenario_id]
    assert harness.check(scenario, harness.run_scenario(scenario)) == []
    problems = harness.check(scenario, harness.run_scenario(scenario, mutation=mutation))
    assert problems, f"{mutation} passed: the eval cannot tell the designs apart"


@pytest.mark.parametrize("cancel", [False, True], ids=["expiry", "cancellation"])
def test_real_preparation_boundary_eval(tmp_path, cancel):
    observation = harness.run_preparation_boundary(tmp_path, cancel=cancel)
    assert harness.check_preparation_boundary(observation) == [], observation


def test_real_preparation_eval_rejects_detached_budget_planted_control(tmp_path):
    observation = harness.run_preparation_boundary(tmp_path, mutation=True)
    assert observation["preparation_started"]
    assert observation["helper_launches"] == 1, observation
    assert not observation["permit_reclaimed"], observation
    assert harness.check_preparation_boundary(observation), observation


@pytest.mark.parametrize("caller", ["direct", "gate"])
@pytest.mark.parametrize("cancel", [False, True], ids=["expiry", "cancellation"])
def test_complete_real_helper_creation_eval(tmp_path, caller, cancel):
    observation = harness.run_complete_boundary(tmp_path, caller=caller, cancel=cancel)
    assert harness.check_complete_boundary(observation) == [], observation


@pytest.mark.parametrize("caller", ["direct", "gate"])
def test_complete_real_descendant_custody_eval(tmp_path, caller):
    observation = harness.run_complete_boundary(tmp_path, caller=caller, cancel=True, descendant=True)
    assert observation.get("owned_pids"), observation
    assert harness.check_complete_boundary(observation) == [], observation


def test_complete_boundary_eval_rejects_detached_lifetime_control(tmp_path):
    observation = harness.run_complete_boundary(tmp_path, mutation=True)
    assert observation["boundary_reached"] and observation["late_effect"], observation
    assert harness.check_complete_boundary(observation), observation


@pytest.mark.parametrize("caller", ["direct", "gate"])
def test_complete_real_first_start_eval(tmp_path, caller):
    observation = harness.run_complete_boundary(tmp_path, caller=caller, start=True)
    assert observation["actual_created_pids"], observation
    assert harness.check_complete_boundary(observation) == [], observation


def test_first_start_eval_rejects_unbound_caller_control(tmp_path):
    observation = harness.run_complete_boundary(tmp_path, caller="direct", start=True,
                                                 mutation="unbound_start")
    assert observation["actual_created_pids"] and not observation["caller_bounded"], observation
    assert harness.check_complete_boundary(observation), observation


def test_descendant_eval_rejects_direct_child_only_control(tmp_path):
    observation = harness.run_complete_boundary(tmp_path, descendant=True, cancel=True,
                                                 mutation="no_group")
    assert observation["late_effect"], observation
    assert not observation["owned_pids_terminal_at_return"], observation
    assert harness.check_complete_boundary(observation), observation


@pytest.mark.parametrize(
    "kwargs,blocked",
    [({"rotate": True}, True), ({"swap": True}, True), ({}, False)],
    ids=["key-rotated-between-attempts", "key-swapped-after-fingerprint-before-helper-read",
         "key-unchanged-control"],
)
def test_real_helper_retry_is_not_sent_under_a_changed_principal(tmp_path, kwargs, blocked):
    """Real gate write, real spawned preparer, real signed_fetch.mjs. After a first
    attempt that lost its response, a key that changed (before the preparer, or after
    its fingerprint and before the helper reads it) must stop the retry before anything
    is signed or sent; an unchanged key must still be retried (the positive control)."""
    observation = harness.run_identity_boundary(tmp_path, **kwargs)
    assert harness.check_identity_boundary(observation, blocked=blocked) == [], observation


def test_the_swap_window_eval_signs_with_the_pinned_key_when_unchanged(tmp_path):
    observation = harness.run_identity_boundary(tmp_path)
    assert observation["signed_with"] == ["original-x", "original-x"], observation


@pytest.mark.parametrize(
    "kwargs,mutation",
    [({"rotate": True}, "no_checks"), ({"swap": True}, "no_helper_check")],
    ids=["both-checks-removed", "helper-check-removed"],
)
def test_real_helper_identity_eval_goes_red_when_a_check_is_removed(tmp_path, kwargs, mutation):
    """Planted reds: with the named check(s) removed the retry reaches the signer again."""
    observation = harness.run_identity_boundary(tmp_path, mutation=mutation, **kwargs)
    assert observation["launches"] == 2, observation
    assert harness.check_identity_boundary(observation, blocked=True), observation


def test_the_helper_check_alone_also_stops_a_key_rotated_between_attempts(tmp_path):
    """Defense in depth: with the preparer's comparison removed, the helper's own key
    check still refuses the rotated key before anything is signed."""
    observation = harness.run_identity_boundary(tmp_path, rotate=True, mutation=True)
    assert harness.check_identity_boundary(observation, blocked=True) == [], observation
