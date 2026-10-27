"""Dispatcher App-identity eval: which credential the Apis dispatcher writes
to GitHub with (ateles#1381).

Collected by the existing ``pytest execution/evals/`` step in
``.github/workflows/ateles-tests.yml`` (the swarm's eval lane for agent-facing
behaviour). Offline: no network, no private key; the scenarios are in
``scenarios.json`` and each drives the real dispatcher write paths.

Run: pytest execution/evals/dispatcher_app_identity/ -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dispatcher_app_identity_harness as harness  # noqa: E402

SCENARIOS = harness.load()["scenarios"]
IDS = [s["id"] for s in SCENARIOS]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=IDS)
def test_scenario(scenario):
    obs = harness.run_scenario(scenario)
    assert harness.check(scenario, obs) == [], scenario["why"]


def test_scenario_ids_are_unique_and_documented():
    assert len(set(IDS)) == len(IDS)
    assert all(s.get("why") and s.get("expect") is not None for s in SCENARIOS)


def test_the_eval_covers_every_required_behaviour():
    required = {
        "app_only_marker_and_spec_mirror_go_out_as_the_app",
        "cold_start_with_a_pat_present_still_writes_as_the_app",
        "expired_token_is_recovered_without_stalling_the_event_loop",
        "mint_failure_falls_back_to_the_pat_with_a_logged_warning",
        "no_credential_skips_visibly",
        "a_bot_authored_gate_notice_is_not_reposted",
        "a_403_logs_the_response_body_and_no_credential",
        "instance_token_only_is_never_sent_bare",
        "marker_with_no_credential_sends_nothing",
    }
    assert required <= set(IDS)


# An eval whose checks cannot fail is not an eval: with the App path removed
# (the pre-change behaviour), every scenario that expects the App must go red.
APP_EXPECTING = [
    s for s in SCENARIOS if any(r[2] == "app" for r in s["expect"].get("requests", []))
]


@pytest.mark.parametrize("scenario", APP_EXPECTING, ids=[s["id"] for s in APP_EXPECTING])
def test_checks_go_red_without_the_app_path(scenario):
    obs = harness.run_scenario(scenario, mutation="pat_first")
    assert harness.check(scenario, obs), "this scenario cannot detect the App path's absence"


def test_checks_go_red_if_a_credential_leaks_into_a_log():
    scenario = SCENARIOS[0]
    obs = harness.run_scenario(scenario)
    obs["all_logs"].append(f"auth header was Bearer {harness.APP_TOKEN}")
    assert harness.check(scenario, obs)


def test_checks_go_red_if_the_guard_accepts_a_credential_the_headers_drop():
    scenario = next(s for s in SCENARIOS if s["id"] == "instance_token_only_is_never_sent_bare")
    obs = harness.run_scenario(scenario, mutation="guard_accepts_instance_token")
    assert harness.check(scenario, obs), "a bare write would go unnoticed"
