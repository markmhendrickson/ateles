"""Fly runner eval scenarios (ateles#1456), plus proof the judge can go red.

Collected by the ``execution/evals/`` pytest lane in ``ateles-tests.yml``.
Offline and deterministic: no model call, no Fly or GitHub call, only
synthetic credentials.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fly_runner_eval as runner  # noqa: E402

SCENARIOS = runner.load_scenarios()


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_scenario(scenario, tmp_path, monkeypatch):
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    assert runner.judge(scenario, trace) == [], (scenario["id"], trace.envelope)


def test_scenario_set_covers_the_required_behaviours():
    ids = {s["id"] for s in SCENARIOS}
    assert {
        "opted_out_class_stays_local",
        "forced_fly_ineligible_role_refused",
        "forced_fly_without_app_refused_not_pat",
        "opted_in_class_runs_on_fly",
        "forced_fly_budget_deferral",
        "forced_fly_disconnect_cleans_up",
        "forced_fly_timeout_cleans_up",
        "oversized_output_is_flagged",
        "login_start_response_lost",
        "expired_reservation_reaped_listed",
        "expired_reservation_reaped_absent",
        "tracked_unconfirmed_delete_keeps_capacity_reserved",
        "opted_in_lost_create_unconfirmed_no_local_replay",
        "opted_in_lost_create_terminated_no_local_replay",
        "opted_in_create_refused_falls_back_locally",
        "untracked_unconfirmed_delete_keeps_capacity_reserved",
        "concurrent_dispatchers_preserve_live_owner",
        "abandoned_owner_is_recovered",
        "unknown_outcome_never_fails_over",
        "timeout_unconfirmed_keeps_recovery_metadata",
        "recorded_success_unconfirmed_cleanup_is_visible",
        "login_back_to_back_dispatchers_keep_their_own_payload",
        "login_cleanup_unverified_blocks_readmission",
        "abandoned_login_reservation_never_stops_a_live_run",
        "malformed_result_read_keeps_outcome_unknown",
        "login_machine_stopped_with_foreign_job_and_no_ledger_entry_is_refused",
        "login_machine_started_with_foreign_job_and_no_ledger_entry_is_refused",
    } <= ids
    assert len(ids) == len(SCENARIOS)


# ---- The judge must be able to fail on what it watches ---------------------


def _by_id(name: str) -> dict:
    return next(s for s in SCENARIOS if s["id"] == name)


def test_judge_goes_red_when_an_opted_out_class_reaches_fly(tmp_path, monkeypatch):
    scenario = _by_id("opted_out_class_stays_local")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    wrong = dataclasses.replace(trace, local_cli_ran=False, fly_requests=3)
    assert runner.judge(scenario, wrong)


def test_judge_goes_red_when_a_pat_is_used(tmp_path, monkeypatch):
    scenario = _by_id("forced_fly_without_app_refused_not_pat")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    leaked = dataclasses.replace(trace, everything_sent=runner.SHARED_PAT, exit_code=0,
                                 envelope={"ok": True})  # fmt: skip
    problems = runner.judge(scenario, leaked)
    assert any("shared PAT was sent" in p for p in problems)


def test_judge_goes_red_when_cleanup_is_skipped(tmp_path, monkeypatch):
    scenario = _by_id("forced_fly_disconnect_cleans_up")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    leaked = dataclasses.replace(trace, machines_remaining=["m1"], revoked=[], in_flight_after=1)
    problems = runner.judge(scenario, leaked)
    assert len(problems) >= 3


def test_judge_goes_red_when_recovery_drops_cost_or_capacity(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    settled = _by_id("expired_reservation_reaped_listed")
    trace = runner.run_scenario(settled, tmp_path / "a", monkeypatch)
    assert runner.judge(settled, dataclasses.replace(trace, spend_usd=0.0))
    kept = _by_id("tracked_unconfirmed_delete_keeps_capacity_reserved")
    trace = runner.run_scenario(kept, tmp_path / "b", monkeypatch)
    assert runner.judge(kept, dataclasses.replace(trace, seed_entries_left=[]))


def test_judge_goes_red_when_truncation_is_silent(tmp_path, monkeypatch):
    scenario = _by_id("oversized_output_is_flagged")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    silent = dataclasses.replace(trace, envelope={**trace.envelope, "output_truncated": False})
    assert runner.judge(scenario, silent)


def test_judge_goes_red_on_a_local_replay_of_an_uncertain_launch(tmp_path, monkeypatch):
    scenario = _by_id("opted_in_lost_create_unconfirmed_no_local_replay")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    replayed = dataclasses.replace(
        trace, local_cli_ran=True, exit_code=0,
        envelope={"ok": True, "stdout": "LOCAL GATE_INHERITANCE: clear"},
    )  # fmt: skip
    assert len(runner.judge(scenario, replayed)) >= 3


def test_judge_goes_red_when_a_live_owner_is_reaped_or_a_dispatch_fails_over(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    live = _by_id("concurrent_dispatchers_preserve_live_owner")
    trace = runner.run_scenario(live, tmp_path / "a", monkeypatch)
    assert runner.judge(live, dataclasses.replace(trace, machines_remaining=[]))
    once = _by_id("unknown_outcome_never_fails_over")
    trace = runner.run_scenario(once, tmp_path / "b", monkeypatch)
    failed_over = {**trace.envelope, "attempted_providers": ["claude", "codex"]}
    assert runner.judge(once, dataclasses.replace(trace, envelope=failed_over))


def test_judge_goes_red_when_a_login_handoff_loses_a_payload(tmp_path, monkeypatch):
    scenario = _by_id("login_back_to_back_dispatchers_keep_their_own_payload")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    erased = dataclasses.replace(trace, starts=[trace.starts[0], ""])
    assert runner.judge(scenario, erased)


def test_judge_goes_red_when_a_live_login_job_is_stopped(tmp_path, monkeypatch):
    scenario = _by_id("abandoned_login_reservation_never_stops_a_live_run")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    stopped = dataclasses.replace(trace, login_state="stopped", login_stop_requests=1,
                                  login_config_run_id="")  # fmt: skip
    assert len(runner.judge(scenario, stopped)) >= 3


def test_judge_goes_red_when_admission_overwrites_a_foreign_job(tmp_path, monkeypatch):
    scenario = _by_id("login_machine_stopped_with_foreign_job_and_no_ledger_entry_is_refused")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    overwritten = dataclasses.replace(trace, login_mutations=2, login_config_run_id="mine")
    assert len(runner.judge(scenario, overwritten)) >= 2
