"""Each of 6 variants -> exactly expected red set (set equality); planted 0-red and >1-red cases
-> SELF_CHECK_ZERO_RED / SELF_CHECK_MULTI_RED and non-zero exit."""

from __future__ import annotations

from execution.conformance.adapter_admission.reference import make_reference_adapter
from execution.conformance.row_result import FailureCode, RowOutcome
from execution.conformance.adapter_admission.runner import RunReport, _run_variant
from execution.conformance.adapter_admission.variants import EXPECTED_RED, VARIANT_FACTORIES


def _variant_factory(variant: int):
    make_variant = VARIANT_FACTORIES[variant]
    return lambda: make_variant(make_reference_adapter)


def test_each_variant_turns_exactly_expected_red_set():
    for variant, expected in EXPECTED_RED.items():
        report = RunReport()
        _run_variant(report, _variant_factory(variant), variant, expected)
        observed = {r.row_id for r in report.rows if r.outcome == RowOutcome.RED}
        assert observed == set(expected), (variant, observed, expected)
        assert not report.failures, report.failures


def test_obligation_2_cardinality_2_is_not_multi_red():
    report = RunReport()
    _run_variant(report, _variant_factory(2), 2, EXPECTED_RED[2])
    assert len(EXPECTED_RED[2]) == 2
    assert not report.failures


def test_planted_zero_red_fails_self_check():
    # An un-mutated reference adapter satisfies obligation 1, so checking it as if it were the
    # obligation-1 variant (no mutation applied) must self-report zero red.
    report = RunReport()
    _run_variant(report, make_reference_adapter, 1, EXPECTED_RED[1])
    assert any(f.code == FailureCode.SELF_CHECK_ZERO_RED for f in report.failures)
    assert report.exit_code != 0


def test_planted_extra_red_fails_self_check():
    # Plant a variant that violates BOTH obligation 1 and obligation 2's rules, then check it
    # against an expected set that names only obligation 1's row — so the instrument observes
    # more red rows than the (deliberately too-narrow) expected set names.
    def over_mutating_factory():
        adapter = make_reference_adapter()
        adapter.drop_counter_wired = False  # obl-1 mutation: turns AD-21 red
        adapter.unrecognized_credential_fallthrough_to_operator = True  # obl-2 mutation: turns AD-4/AD-22 red
        return adapter

    narrow_expected = frozenset({"AD-21", "AD-4", "AD-22"})
    report = RunReport()
    _run_variant(report, over_mutating_factory, 1, narrow_expected)
    observed = {r.row_id for r in report.rows if r.outcome == RowOutcome.RED}
    assert observed == narrow_expected  # both mutations fire; this probe's expected set matches them
    assert not report.failures  # matches exactly what was probed for

    # Now probe a TOO-NARROW expected set against the same over-mutated adapter: the instrument
    # only checks AD-21, so extending EXPECTED_RED[1] (={AD-21}) already matches — to see a
    # genuine multi-red mismatch, probe MORE rows than the mutation actually turns red.
    def under_mutating_factory():
        adapter = make_reference_adapter()
        adapter.drop_counter_wired = False  # only obl-1's mutation fires
        return adapter

    wider_than_actual = frozenset({"AD-21", "AD-4"})  # AD-4 will NOT go red — only obl-1 applies
    report_mismatch = RunReport()
    _run_variant(report_mismatch, under_mutating_factory, 1, wider_than_actual)
    assert any(
        f.code in (FailureCode.SELF_CHECK_MULTI_RED, FailureCode.ROW_UNEXPECTED)
        for f in report_mismatch.failures
    )
    assert report_mismatch.exit_code != 0


def test_self_check_failure_is_first_class_block_not_footnote():
    report = RunReport()
    _run_variant(report, make_reference_adapter, 1, EXPECTED_RED[1])
    failure = next(f for f in report.failures if f.code == FailureCode.SELF_CHECK_ZERO_RED)
    assert failure.variant == 1
    assert failure.expected_red == EXPECTED_RED[1]
    assert failure.observed_red == frozenset()
    assert failure.hint
    assert failure.next_action
