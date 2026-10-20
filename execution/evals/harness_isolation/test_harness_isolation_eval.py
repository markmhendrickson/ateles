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
            "fly_bound_dispatch_builds_no_local_home",
            "codex_malformed_bracketed_host_is_refused_before_launch",
            "cursor_non_numeric_port_is_refused_before_launch",
            "codex_out_of_range_port_is_refused_before_launch",
            "cursor_zero_port_is_refused_before_launch",
            "malformed_endpoint_fails_over_and_keeps_every_repair",
            "cursor_relative_workspace_resolves_to_one_absolute_path",
            "codex_relative_workspace_resolves_to_one_absolute_path",
            "seated_codex_review_uses_the_roles_own_token",
            "seated_codex_review_without_own_token_is_refused_before_launch",
            "seated_codex_review_with_a_blank_token_is_refused_before_launch",
            "no_grant_role_with_every_credential_present_gets_none"} <= ids


def test_the_judge_goes_red_when_the_child_gets_a_different_workspace(tmp_path, monkeypatch):
    scenario = _by_id("cursor_relative_workspace_resolves_to_one_absolute_path")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    moved = dataclasses.replace(trace, child={**trace.child, "workspace_flag_value": "ws",
                                              "project_mcp": True})
    assert len(runner.judge(scenario, moved)) >= 2


def test_the_judge_goes_red_when_a_refused_value_is_echoed(tmp_path, monkeypatch):
    scenario = _by_id("cursor_non_numeric_port_is_refused_before_launch")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    echoed = dataclasses.replace(trace, output_text=trace.output_text + " neotoma.eval.invalid:bad",
                                 envelope={**trace.envelope, "cooled_until": "soon"})
    assert len(runner.judge(scenario, echoed)) >= 2


def test_the_judge_goes_red_when_a_seated_child_gets_the_shared_bearer(tmp_path, monkeypatch):
    import hashlib

    scenario = _by_id("seated_codex_review_uses_the_roles_own_token")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    shared = hashlib.sha256(runner.SHARED_BEARER.encode()).hexdigest()
    swapped = dataclasses.replace(trace, child={**trace.child, "bearer_digest": shared})
    assert len(runner.judge(scenario, swapped)) >= 2


def test_the_judge_goes_red_when_an_ungranted_child_keeps_a_credential(tmp_path, monkeypatch):
    scenario = _by_id("no_grant_role_with_every_credential_present_gets_none")
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    kept = dataclasses.replace(trace, child={**trace.child,
                                             "neotoma_credential_vars": ["LANIUS_NEOTOMA_TOKEN"]})
    assert runner.judge(scenario, kept)
