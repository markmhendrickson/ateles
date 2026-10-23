"""Eval of the Fly delivery receipt clearing command, plus proof the judge can
go red. Collected by the ``execution/evals/`` pytest lane in ``ateles-tests.yml``.
Offline; throwaway ledgers."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fly_delivery_receipt_eval as ev  # noqa: E402

SCENARIOS = ev.load_scenarios()


def _by_id(name):
    return next(s for s in SCENARIOS if s["id"] == name)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_scenario(scenario, tmp_path, monkeypatch):
    got = ev.run_scenario(scenario, tmp_path, monkeypatch)
    assert ev.judge(scenario, got) == [], scenario["id"]


@pytest.mark.parametrize(
    "mutation, scenario_id",
    [
        ("readback_gone_is_success", "raised_removal_reads_back_gone"),
        ("file_only_fsync", "directory_sync_fails_at_removing"),
        ("file_only_fsync", "directory_sync_fails_at_closing"),
    ],
)
def test_the_judge_goes_red_for_a_planted_mutation(
    mutation, scenario_id, tmp_path, monkeypatch
):
    scenario = _by_id(scenario_id)
    got = ev.run_scenario(scenario, tmp_path, monkeypatch, mutation=mutation)
    assert ev.judge(scenario, got)


def _subprocess(*argv, ledger):
    env = {**os.environ, "APIS_FLY_LEDGER_FILE": str(ledger.path)}
    return subprocess.run(
        [sys.executable, str(ev.COMMAND), *argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_real_command_lists_and_dry_runs_in_a_subprocess(tmp_path, monkeypatch):
    ledger = ev.make_ledger(tmp_path, monkeypatch)
    before = ledger.path.read_bytes()
    listed = _subprocess("list", ledger=ledger)
    assert listed.returncode == 0 and ev.KEY in listed.stdout and ev.OTHER in listed.stdout
    dry = _subprocess("clear", ev.KEY, ledger=ledger)
    assert dry.returncode == 0 and "Dry run" in dry.stdout and "run-eval" in dry.stdout
    assert ledger.path.read_bytes() == before
    assert not (ledger.path.parent / (ledger.path.name + ".audit.jsonl")).exists()
    for text in (listed.stdout, listed.stderr, dry.stdout, dry.stderr):
        assert ev.CREDENTIAL not in text and ev.ERROR_TEXT not in text
    wrong = _subprocess("clear", ev.KEY, "--confirm", ev.OTHER, ledger=ledger)
    assert wrong.returncode == 1 and ledger.path.read_bytes() == before
