"""Sixth importable -> AD-21..26 all green; stdout lists each AD-NN."""

from __future__ import annotations

from execution.conformance.adapter_admission.runner import format_report, run
from execution.conformance.row_result import RowOutcome


def test_reference_pass_all_green():
    report = run("adapter-admission", _use_fake_on_missing=True)
    reference_rows = [r for r in report.rows if r.variant is None and r.row_id.startswith("AD-2")]
    reference_rows = [r for r in reference_rows if r.row_id in {f"AD-2{n}" for n in range(1, 7)}]
    assert len(reference_rows) == 6
    assert all(r.outcome == RowOutcome.GREEN for r in reference_rows), reference_rows
    assert not any(f.code.value == "REFERENCE_RED" for f in report.failures)


def test_reference_pass_stdout_lists_each_row():
    report = run("adapter-admission", _use_fake_on_missing=True)
    output = format_report(report)
    for n in range(1, 7):
        assert f"AD-2{n}\tgreen" in output
