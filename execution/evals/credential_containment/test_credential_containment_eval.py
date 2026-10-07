"""Credential containment eval scenarios, plus proof the evaluator can go red.

Collected by the ``execution/evals/`` pytest lane in ``ateles-tests.yml``.
Offline and deterministic: no model call, only synthetic credentials.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import runner  # noqa: E402

SCENARIOS = runner.load_scenarios()
HOOK_SCENARIOS = SCENARIOS["hook"]
LAUNCH_SCENARIOS = SCENARIOS["launch"]


@pytest.fixture(scope="module")
def workspace(tmp_path_factory):
    return runner.build_workspace(tmp_path_factory.mktemp("cred_eval"))


@pytest.mark.parametrize("scenario", HOOK_SCENARIOS, ids=[s["id"] for s in HOOK_SCENARIOS])
def test_hook_scenario(scenario, workspace):
    outcome = runner.run_hook(scenario, workspace)
    assert runner.judge_hook(scenario, outcome) == [], scenario["id"]


@pytest.mark.parametrize("scenario", LAUNCH_SCENARIOS, ids=[s["id"] for s in LAUNCH_SCENARIOS])
def test_launch_scenario(scenario, tmp_path, monkeypatch):
    trace = runner.run_launch(scenario, tmp_path, monkeypatch)
    assert runner.judge_launch(scenario, trace) == [], scenario["id"]


def test_scenario_set_covers_both_outcomes_on_every_surface():
    by_tool = {}
    for scenario in HOOK_SCENARIOS:
        by_tool.setdefault(scenario["tool"], set()).add(scenario["expect"])
    assert by_tool["Bash"] == {"allow", "deny"}
    assert by_tool["Read"] == {"allow", "deny"}
    assert by_tool["Grep"] == {"allow", "deny"}
    assert by_tool["Glob"] == {"allow"}  # names only: never refused
    assert {s["expect"] for s in LAUNCH_SCENARIOS} == {"refuse", "launch"}
    ids = [s["id"] for s in HOOK_SCENARIOS + LAUNCH_SCENARIOS]
    assert len(ids) == len(set(ids))


# ---- The evaluator must be able to fail on what it watches ----------------


def _write_hook(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_evaluator_goes_red_against_a_guard_that_allows_everything(tmp_path, workspace):
    permissive = _write_hook(tmp_path / "permissive.py", "import sys\nsys.exit(0)\n")
    denied = [s for s in HOOK_SCENARIOS if s["expect"] == "deny"]
    assert denied
    for scenario in denied:
        problems = runner.judge_hook(scenario, runner.run_hook(scenario, workspace, permissive))
        assert problems, f"{scenario['id']} passed against a guard that allows everything"


def test_evaluator_goes_red_against_a_guard_that_refuses_everything(tmp_path, workspace):
    refusing = _write_hook(
        tmp_path / "refusing.py",
        "import json, sys\n"
        "print(json.dumps({'hookSpecificOutput': {'permissionDecision': 'deny',"
        " 'permissionDecisionReason': 'Refused: credential file'}}))\n"
        "sys.exit(2)\n",
    )
    allowed = [s for s in HOOK_SCENARIOS if s["expect"] == "allow"]
    assert allowed
    for scenario in allowed:
        problems = runner.judge_hook(scenario, runner.run_hook(scenario, workspace, refusing))
        assert problems, f"{scenario['id']} passed against a guard that refuses everything"


def test_evaluator_goes_red_when_a_refusal_leaks_a_secret(tmp_path, workspace):
    leaking = _write_hook(
        tmp_path / "leaking.py",
        "import json, sys\n"
        f"print(json.dumps({{'hookSpecificOutput': {{'permissionDecision': 'deny',"
        f" 'permissionDecisionReason': 'Refused: credential file {runner.FAKE_SECRET_VALUES[0]}'}}}}))\n"
        "sys.exit(2)\n",
    )
    scenario = next(s for s in HOOK_SCENARIOS if s["expect"] == "deny")
    problems = runner.judge_hook(scenario, runner.run_hook(scenario, workspace, leaking))
    assert any("secret" in p for p in problems)


def test_launch_evaluator_goes_red_when_a_refused_launch_still_dispatches(
    tmp_path, monkeypatch
):
    import harness_lens_runner as hlr

    monkeypatch.setattr(hlr, "refuse_if_credential_files_in_worktree", lambda path: None)
    scenario = next(s for s in LAUNCH_SCENARIOS if s["expect"] == "refuse" and not s["dry_run"])
    trace = runner.run_launch(scenario, tmp_path, monkeypatch)
    problems = runner.judge_launch(scenario, trace)
    assert any("dispatch" in p for p in problems)


def test_refusal_document_is_json_serializable_and_value_free(workspace):
    scenario = next(s for s in HOOK_SCENARIOS if s["id"] == "bash_cat_dotenv")
    outcome = runner.run_hook(scenario, workspace)
    json.dumps({"decision": outcome.decision, "reason": outcome.reason})
    for secret in runner.FAKE_SECRET_VALUES:
        assert secret not in outcome.reason
