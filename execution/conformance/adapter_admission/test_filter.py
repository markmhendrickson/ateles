"""adapter-admission / AD-21 select correctly; unknown filter -> EMPTY_SELECTION."""

from __future__ import annotations

from execution.conformance.adapter_admission.instruments import ALL_ROWS
from execution.conformance.row_result import FailureCode
from execution.conformance.adapter_admission.runner import resolve_filter, run


def test_filter_adapter_admission_selects_all_rows():
    assert resolve_filter("adapter-admission") == ALL_ROWS


def test_filter_single_row_id():
    assert resolve_filter("AD-21") == frozenset({"AD-21"})
    assert resolve_filter("ad-21") == frozenset({"AD-21"})  # case-insensitive


def test_filter_obligation_number_selects_mapped_rows():
    selected = resolve_filter("2")
    assert selected == frozenset({"AD-4", "AD-22"})


def test_filter_unknown_selector_is_empty():
    assert resolve_filter("not-a-real-filter") == frozenset()
    assert resolve_filter("AD-99") == frozenset()
    assert resolve_filter("7") == frozenset()  # no obligation 7


def test_run_with_unknown_filter_reports_empty_selection():
    report = run("not-a-real-filter")
    assert report.exit_code != 0
    assert len(report.failures) == 1
    assert report.failures[0].code == FailureCode.EMPTY_SELECTION


def test_run_single_row_selector_reports_only_that_row():
    # Obligation 2's expected-red set has cardinality 2 ({AD-4, AD-22}); selecting AD-4 alone
    # must report only AD-4 — the self-check still evaluates the full pair internally (so a
    # coupled-row failure stays visible), but the reported RowResults are restricted to the
    # selection, matching the documented single-row selector contract.
    report = run("AD-4", _use_fake_on_missing=True)
    row_ids = {r.row_id for r in report.rows}
    assert row_ids == {"AD-4"}


def test_run_obligation_selector_reports_reference_row_and_mapped_rows():
    report = run("2", _use_fake_on_missing=True)
    row_ids = {r.row_id for r in report.rows}
    assert row_ids == {"AD-4", "AD-22"}
