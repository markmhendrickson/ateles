"""Fly repository-delivery eval scenarios, plus proof the judge can go red.

Collected by the ``execution/evals/`` pytest lane in ``ateles-tests.yml``.
Offline: the real job entrypoint, a local bare remote, a stand-in GitHub API.
"""

from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fly_delivery_eval as runner  # noqa: E402

SCENARIOS = runner.load_scenarios()


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_scenario(scenario, tmp_path, monkeypatch):
    trace = runner.run_scenario(scenario, tmp_path, monkeypatch)
    assert runner.judge(scenario, trace) == [], (scenario["id"], trace.record)


def _by_id(name):
    return next(s for s in SCENARIOS if s["id"] == name)


def _trace(name, tmp_path, monkeypatch):
    scenario = _by_id(name)
    return scenario, runner.run_scenario(scenario, tmp_path, monkeypatch)


def test_the_judge_goes_red_when_the_default_branch_moves(tmp_path, monkeypatch):
    scenario, trace = _trace("delivery_pushes_and_opens_a_pull_request", tmp_path, monkeypatch)
    moved = dict(trace.remote_after, **{"refs/heads/main": "0" * 40})
    assert runner.judge(scenario, dataclasses.replace(trace, remote_after=moved))


def test_the_judge_goes_red_when_a_refused_delivery_pushes(tmp_path, monkeypatch):
    scenario, trace = _trace("refused_import_pushes_nothing", tmp_path, monkeypatch)
    pushed = dict(trace.remote_after, **{f"refs/heads/{runner.BRANCH}": "1" * 40})
    assert runner.judge(scenario, dataclasses.replace(trace, remote_after=pushed))


def test_the_judge_goes_red_when_a_pull_request_is_duplicated(tmp_path, monkeypatch):
    scenario, trace = _trace("existing_open_pull_request_is_updated_not_duplicated", tmp_path,
                             monkeypatch)
    extra = [*trace.api_calls, {"method": "POST", "path": "/x", "body": {}, "token_ok": True}]
    assert runner.judge(scenario, dataclasses.replace(trace, api_calls=extra))


def test_the_judge_goes_red_when_a_secret_leaks(tmp_path, monkeypatch):
    scenario, trace = _trace("token_echoed_by_the_agent_is_scrubbed", tmp_path, monkeypatch)
    assert runner.judge(scenario, dataclasses.replace(trace, output_text=runner.TOKEN))
    assert runner.judge(scenario, dataclasses.replace(trace, secret_on_disk=True))
    leaked = dict(trace.record, stdout=runner.HEADER)
    assert runner.judge(scenario, dataclasses.replace(trace, record=leaked))


def test_the_judge_goes_red_when_work_is_left_behind(tmp_path, monkeypatch):
    scenario, trace = _trace("no_changes_pushes_nothing", tmp_path, monkeypatch)
    assert runner.judge(scenario, dataclasses.replace(trace, work_left=["job-x"]))


def test_the_judge_goes_red_when_a_failed_pr_records_a_number(tmp_path, monkeypatch):
    scenario, trace = _trace("pull_request_failure_keeps_the_pushed_commit", tmp_path, monkeypatch)
    assert runner.judge(scenario, dataclasses.replace(trace, record={**trace.record, "pr_number": 3}))


def test_the_judge_goes_red_when_the_cli_runs_without_its_clone(tmp_path, monkeypatch):
    scenario, trace = _trace("clone_failure_never_runs_the_cli", tmp_path, monkeypatch)
    assert runner.judge(scenario, dataclasses.replace(trace, cli_ran=True))


def test_the_scenario_set_covers_the_required_cases():
    assert {s["id"] for s in SCENARIOS} >= {
        "delivery_pushes_and_opens_a_pull_request",
        "existing_open_pull_request_is_updated_not_duplicated",
        "job_without_a_delivery_is_unchanged",
        "clone_failure_never_runs_the_cli",
        "failed_cli_pushes_nothing",
        "refused_import_pushes_nothing",
        "no_changes_pushes_nothing",
        "no_time_pushes_nothing",
        "rejected_push_leaves_the_remote_head_unchanged",
        "pull_request_failure_keeps_the_pushed_commit",
        "token_echoed_by_the_agent_is_scrubbed",
    }
