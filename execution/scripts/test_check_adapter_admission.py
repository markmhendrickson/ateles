"""CLI entry: --filter / positional selector + exit-code parity with runner.py (single surface today)."""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_adapter_admission as cli  # noqa: E402


def test_default_selector_reports_runtime_missing(capsys):
    exit_code = cli.main([])
    output = capsys.readouterr().out
    assert exit_code != 0
    assert "RUNTIME_MISSING" in output


def test_positional_selector_accepted(capsys):
    exit_code = cli.main(["adapter-admission"])
    output = capsys.readouterr().out
    assert exit_code != 0
    assert "RUNTIME_MISSING" in output


def test_filter_flag_accepted(capsys):
    exit_code = cli.main(["--filter", "AD-21"])
    output = capsys.readouterr().out
    assert exit_code != 0  # runtime still missing for a real row selection
    assert "RUNTIME_MISSING" in output


def test_unknown_filter_reports_empty_selection(capsys):
    exit_code = cli.main(["--filter", "not-a-real-filter"])
    output = capsys.readouterr().out
    assert exit_code != 0
    assert "EMPTY_SELECTION" in output


def test_row_count_and_failure_count_line_present(capsys):
    cli.main(["--filter", "AD-21"])
    output = capsys.readouterr().out
    assert "row(s) checked" in output
    assert "failure(s)" in output


def test_exit_code_matches_runner_report():
    from execution.conformance.adapter_admission.runner import run

    report = run("AD-21")
    assert cli.main(["--filter", "AD-21"]) == report.exit_code
