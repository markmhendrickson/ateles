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
        "shared_role_token_uses_aauth_key_without_shipping_bearer",
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
        "login_machine_leased_elsewhere_is_refused",
        "login_machine_without_version_is_refused",
        "cleanup_read_back_config_absent_keeps_the_reservation",
        "cleanup_read_back_config_null_keeps_the_reservation",
        "lease_release_unverifiable_keeps_the_reservation",
        "exhausted_ledger_without_month_marker_is_refused",
        "exhausted_ledger_without_day_marker_is_refused",
        "incomplete_result_record_is_an_unknown_outcome",
        "non_finite_budget_setting_disables_fly",
        "negative_daily_cap_refuses_an_opted_in_class_without_local_fallback",
        "held_reservation_with_unreadable_cost_refuses_admission",
        "machine_is_destroyed_while_the_ledger_is_unusable",
        "runner_entrypoint_exits_by_deadline_with_full_output",
        "runner_entrypoint_exits_by_deadline_while_output_is_held",
        "runner_entrypoint_exits_by_deadline_when_cleanup_stalls",
        "runner_entrypoint_exits_promptly_when_the_run_raises",
        "runner_entrypoint_exits_promptly_when_the_run_asks_to_exit",
        "login_claim_waits_for_the_machine_to_settle",
        "login_machine_that_never_settles_fails_closed_without_job_files",
        "large_system_prompt_travels_as_a_file_not_an_argument",
        "fly_dispatch_is_not_gated_by_the_macs_usage_reading",
        "the_same_dispatch_bound_for_the_mac_keeps_its_usage_gate",
        "fly_dispatch_keeps_the_operators_zero_headroom_override",
        "claude_runs_on_its_login_machine_with_the_volume_token",
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


def test_judge_goes_red_when_key_only_job_omits_the_discarded_token_probe(
        tmp_path, monkeypatch):
    scenario = _by_id("shared_role_token_uses_aauth_key_without_shipping_bearer")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    missing_probe = dataclasses.replace(
        trace,
        job_specs=[{**spec, "probe_env_names": []} for spec in trace.job_specs],
    )
    assert any("did not probe LANIUS_NEOTOMA_TOKEN" in problem
               for problem in runner.judge(scenario, missing_probe))
    wrong_child = dataclasses.replace(trace, key_only_child_has_mcp=True)
    assert any("received a Neotoma MCP config" in problem
               for problem in runner.judge(scenario, wrong_child))
    unsigned = dataclasses.replace(trace, result_record_signed=False)
    assert any("did not sign the result record" in problem
               for problem in runner.judge(scenario, unsigned))


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


def test_judge_goes_red_when_a_foreign_lease_is_overridden(tmp_path, monkeypatch):
    scenario = _by_id("login_machine_leased_elsewhere_is_refused")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    taken = dataclasses.replace(trace, login_lease_holder="ateles-fly-runner x", login_mutations=2)
    assert len(runner.judge(scenario, taken)) >= 2


def test_judge_goes_red_when_settlement_trusts_an_incomplete_answer(tmp_path, monkeypatch):
    scenario = _by_id("lease_release_unverifiable_keeps_the_reservation")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    trusted = dataclasses.replace(
        trace, in_flight_after=0,
        envelope={**trace.envelope, "remote_machine_state": "terminated"},
    )  # fmt: skip
    assert len(runner.judge(scenario, trusted)) >= 2


def test_judge_goes_red_when_a_damaged_ledger_is_reset(tmp_path, monkeypatch):
    scenario = _by_id("exhausted_ledger_without_month_marker_is_refused")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    reset = dataclasses.replace(trace, ledger_after={"spend_usd": 0.0}, exit_code=0,
                                envelope={**trace.envelope, "ok": True})  # fmt: skip
    assert len(runner.judge(scenario, reset)) >= 2


def test_judge_goes_red_when_the_entrypoint_exits_late_or_skips_cleanup(tmp_path, monkeypatch):
    scenario = _by_id("runner_entrypoint_exits_promptly_when_the_run_raises")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    late = dataclasses.replace(trace, exit_code=None, exit_after_condition=30.0,
                               cleanup_reached=False, descendant_gone_after=False)  # fmt: skip
    assert len(runner.judge(scenario, late)) >= 3


def test_judge_goes_red_when_job_files_are_left_on_a_login_machine(tmp_path, monkeypatch):
    scenario = _by_id("login_machine_that_never_settles_fails_closed_without_job_files")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    left = dataclasses.replace(trace, login_job_files_after=True, starts=["fly-x"])
    assert len(runner.judge(scenario, left)) >= 2


def test_judge_goes_red_when_a_large_prompt_is_one_argument(tmp_path, monkeypatch):
    scenario = _by_id("large_system_prompt_travels_as_a_file_not_an_argument")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    inline = dataclasses.replace(trace, argv_max_bytes=150_000, system_prompt_file_bytes=0)
    assert len(runner.judge(scenario, inline)) >= 2


def test_judge_goes_red_when_a_fly_run_hides_the_reading_it_set_aside(tmp_path, monkeypatch):
    scenario = _by_id("fly_dispatch_is_not_gated_by_the_macs_usage_reading")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    hidden = dataclasses.replace(trace, envelope={**trace.envelope, "usage_gate_exempted": ""})
    assert runner.judge(scenario, hidden)
def test_judge_goes_red_when_the_claude_token_leaves_the_mac(tmp_path, monkeypatch):
    scenario = _by_id("claude_runs_on_its_login_machine_with_the_volume_token")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    leaked = dataclasses.replace(trace, everything_sent=trace.everything_sent + runner.CLAUDE_OAUTH,
                                 claude_login_job_files_after=True)  # fmt: skip
    assert len(runner.judge(scenario, leaked)) >= 2
