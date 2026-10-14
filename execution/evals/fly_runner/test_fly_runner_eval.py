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
