"""Harness-isolation eval scenarios, plus proof the judge can go red.

Collected by the ``execution/evals/`` pytest lane in ``ateles-tests.yml``.
Offline: stand-in CLIs, synthetic tokens, no network.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import harness_isolation_eval as runner  # noqa: E402

SCENARIOS = runner.load_scenarios()


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_scenario(scenario, tmp_path, monkeypatch):
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    assert runner.judge(scenario, trace) == [], (scenario["id"], trace.envelope)


def _by_id(name):
    return next(s for s in SCENARIOS if s["id"] == name)


def test_the_judge_goes_red_when_a_personal_server_reaches_the_child(tmp_path, monkeypatch):
    scenario = _by_id("codex_child_sees_only_production_neotoma")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    leaked = dataclasses.replace(trace, child={**trace.child,
                                               "servers": ["client-hosted", "mcpsrv_neotoma"]})
    assert len(runner.judge(scenario, leaked)) >= 2


def test_the_judge_goes_red_when_a_home_is_left_behind(tmp_path, monkeypatch):
    scenario = _by_id("cursor_child_sees_only_production_neotoma")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    assert runner.judge(scenario, dataclasses.replace(trace, homes_left=["/x"]))


def test_the_judge_goes_red_when_a_refused_dispatch_starts_a_child(tmp_path, monkeypatch):
    scenario = _by_id("codex_missing_role_token_is_refused_before_launch")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    started = dataclasses.replace(trace, spawned=True, envelope={**trace.envelope,
                                                                "configuration_repair": ""})
    assert len(runner.judge(scenario, started)) >= 2


def test_the_judge_goes_red_when_the_tool_filter_is_dropped(tmp_path, monkeypatch):
    scenario = _by_id("codex_restricted_grant_filters_the_tools")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    unfiltered = dataclasses.replace(trace, child={**trace.child,
                                                   "enabled_tools": {"mcpsrv_neotoma": None}})
    assert runner.judge(scenario, unfiltered)


def test_the_judge_goes_red_when_a_fly_run_builds_a_local_home(tmp_path, monkeypatch):
    scenario = _by_id("fly_bound_dispatch_builds_no_local_home")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    assert runner.judge(scenario, dataclasses.replace(trace, homes_built=["/x"]))


def test_the_scenario_set_covers_the_required_cases():
    ids = {s["id"] for s in SCENARIOS}
    assert {"codex_child_sees_only_production_neotoma",
            "cursor_child_sees_only_production_neotoma",
            "codex_restricted_grant_filters_the_tools",
            "no_grant_role_launches_without_a_server",
            "codex_missing_role_token_is_refused_before_launch",
            "cursor_missing_role_token_is_refused_before_launch",
            "invalid_neotoma_endpoint_is_refused_before_launch",
            "cursor_project_mcp_config_refuses_the_dispatch",
            "fly_bound_dispatch_builds_no_local_home"} <= ids
