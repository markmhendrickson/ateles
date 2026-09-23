"""Disable one variant's failing artefact -> that variant's self-check or row assertion goes red.

Proves the fixture is not a test that always passes: each mutant is load-bearing, and re-wiring its
failing artefact (as a fix to the mutant would) turns the self-check red because the variant no
longer produces its expected red set (PM acceptance criterion: revert-to-red)."""

from __future__ import annotations

from execution.conformance.adapter_admission.instruments import check_ad11
from execution.conformance.adapter_admission.reference import make_reference_adapter
from execution.conformance.row_result import FailureCode
from execution.conformance.adapter_admission.runner import RunReport, _run_variant
from execution.conformance.adapter_admission.variants import EXPECTED_RED, make_variant_1, make_variant_4


def test_obligation_1_revert_to_red():
    # The variant as specified: drop counter unwired -> AD-21 red.
    report = RunReport()
    _run_variant(report, lambda: make_variant_1(make_reference_adapter), 1, EXPECTED_RED[1])
    assert not report.failures

    # "Revert": re-wire the drop counter on the obligation-1 mutant (undo the failing artefact).
    def fixed_variant_1_factory():
        adapter = make_variant_1(make_reference_adapter)
        adapter.drop_counter_wired = True  # re-wired — the mutant no longer violates obligation 1
        return adapter

    reverted_report = RunReport()
    _run_variant(reverted_report, fixed_variant_1_factory, 1, EXPECTED_RED[1])
    assert any(f.code == FailureCode.SELF_CHECK_ZERO_RED for f in reverted_report.failures)
    assert reverted_report.exit_code != 0


def test_obligation_4_revert_to_red_last_seen_cursor():
    # Obligation 4: introducing a last_seen cursor turns AD-17 red via the obl-4 variant path.
    report = RunReport()
    _run_variant(report, lambda: make_variant_4(make_reference_adapter), 4, EXPECTED_RED[4])
    assert not report.failures
    observed = {r.row_id for r in report.rows if r.outcome.value == "red"}
    assert observed == EXPECTED_RED[4]

    def fixed_variant_4_factory():
        adapter = make_variant_4(make_reference_adapter)
        adapter.cleanup()  # remove the last_seen cursor the mutant introduced
        adapter.last_seen_cursor_path = None
        return adapter

    reverted_report = RunReport()
    _run_variant(reverted_report, fixed_variant_4_factory, 4, EXPECTED_RED[4])
    assert any(f.code == FailureCode.SELF_CHECK_ZERO_RED for f in reverted_report.failures)
    assert reverted_report.exit_code != 0


def test_ad11_detects_inbound_only_mutation_independent_of_outbound():
    # check_ad11 (reused as AD-23) must go red on the INBOUND half of obligation 3's mutation
    # alone — a redelivery is two distinct Delivery objects carrying the same external_id, never
    # one object reused, or the id()-based per-attempt-keying mutant is invisible to this
    # instrument (the bug this test pins as fixed: reusing one Delivery object gave both calls
    # the same id() regardless of per_attempt_inbound_keying, masking the inbound mutation).
    inbound_only = make_reference_adapter()
    inbound_only.per_attempt_inbound_keying = True
    assert check_ad11(inbound_only) is True

    conformant = make_reference_adapter()
    assert check_ad11(conformant) is False
