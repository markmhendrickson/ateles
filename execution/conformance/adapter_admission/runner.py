"""Orchestrates the AD-21..AD-34 admission fixture: resolve the filter, import the sixth adapter,
run the reference pass, run the six negative variants with self-check, run registered AD-27..34
instruments, and report.

Never returns a summary alone: `RunReport.rows` carries one `RowResult` per selected row, and
`RunReport.failures` carries every `ConformanceFailure` as a first-class block — including
`SELF_CHECK_*`, never folded into an ordinary row failure or a log line.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from execution.conformance.adapter_admission import instruments
from execution.conformance.adapter_admission.instruments import (
    ALL_ROWS,
    INSTRUMENT_MISSING_ROWS,
    ROW_CHECKS,
)
from execution.conformance.adapter_admission.reference import ReferenceAdapter, make_reference_adapter
from execution.conformance.row_result import (
    ConformanceFailure,
    FailureCode,
    RowId,
    RowOutcome,
    RowResult,
    VariantId,
)
from execution.conformance.adapter_admission.runtime import RuntimeMissingError, import_sixth_adapter_factory
from execution.conformance.adapter_admission.variants import EXPECTED_RED, VARIANT_DESCRIPTIONS, VARIANT_FACTORIES

REFERENCE_ROWS: tuple[RowId, ...] = tuple(f"AD-2{n}" for n in range(1, 7))
FILTER_ALL = "adapter-admission"


@dataclass
class RunReport:
    rows: list[RowResult] = field(default_factory=list)
    failures: list[ConformanceFailure] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if self.failures or any(r.outcome == RowOutcome.RED for r in self.rows) else 0


def resolve_filter(selector: str) -> frozenset[RowId]:
    """`adapter-admission` -> all rows; `AD-NN` -> that row; `obligation N` / bare int -> that
    variant's mapped rows plus its AD-2N row. Unknown selector -> empty set (`EMPTY_SELECTION`)."""
    selector = selector.strip()
    if selector == FILTER_ALL:
        return ALL_ROWS
    if selector.upper().startswith("AD-"):
        row = selector.upper()
        return frozenset({row}) if row in ALL_ROWS else frozenset()
    if selector.isdigit():
        variant = int(selector)
        if variant in EXPECTED_RED:
            return EXPECTED_RED[variant] | {f"AD-2{variant}"}
    return frozenset()


def _factory_or_raise(use_fake_on_missing: bool):
    """Import the real #1190 sixth. `use_fake_on_missing` exists only for this fixture's own test
    suite (`test_variants_self_check.py`, `test_runner_reference.py`), which must exercise the
    reference/variant contract without depending on the unmerged runtime — the CLI entry never
    passes it, so a real invocation always raises `RuntimeMissingError` until #1190 lands."""
    try:
        return import_sixth_adapter_factory()
    except RuntimeMissingError:
        if use_fake_on_missing:
            return make_reference_adapter
        raise


def run(selector: str = FILTER_ALL, *, _use_fake_on_missing: bool = False) -> RunReport:
    report = RunReport()
    selected = resolve_filter(selector)
    if not selected:
        report.failures.append(
            ConformanceFailure(
                code=FailureCode.EMPTY_SELECTION,
                hint=f"selector {selector!r} matched zero rows",
                next_action=f"use {FILTER_ALL!r}, an AD-NN row id, or an obligation number 1-6",
            )
        )
        return report

    try:
        factory = _factory_or_raise(_use_fake_on_missing)
    except RuntimeMissingError as exc:
        report.failures.append(
            ConformanceFailure(
                code=FailureCode.RUNTIME_MISSING,
                hint=str(exc),
                next_action="land ateles#1190 (lib.adapters + the sixth reference adapter) first",
                row_ids=tuple(sorted(selected)),
            )
        )
        return report

    # Reference pass: AD-21..26 must be green.
    reference_rows = [r for r in REFERENCE_ROWS if r in selected]
    if reference_rows:
        adapter = factory()
        reference_red: list[RowId] = []
        for row_id in reference_rows:
            check = ROW_CHECKS[row_id]
            is_red = check(adapter)
            report.rows.append(
                RowResult(row_id=row_id, outcome=RowOutcome.RED if is_red else RowOutcome.GREEN)
            )
            if is_red:
                reference_red.append(row_id)
        adapter.cleanup()
        if reference_red:
            report.failures.append(
                ConformanceFailure(
                    code=FailureCode.REFERENCE_RED,
                    hint=f"the reference (sixth) adapter turned {', '.join(reference_red)} red",
                    next_action="the reference adapter must be conformant; check ateles#1190's implementation",
                    row_ids=tuple(reference_red),
                )
            )

    # Negative variants: each must turn exactly its mapped row(s) red. The self-check always
    # evaluates the full `expected` set (a coupled-row failure must be visible even under a
    # single-row filter); `report_rows` restricts which RowResults are reported for this selection.
    for variant, expected in EXPECTED_RED.items():
        variant_row = f"AD-2{variant}"
        variant_mapped_rows = expected | {variant_row}
        overlap = variant_mapped_rows & selected
        if not overlap:
            continue
        _run_variant(
            report,
            _bind_variant_factory(VARIANT_FACTORIES[variant], factory),
            variant,
            expected,
            report_rows=overlap & expected,
        )

    # AD-31 / AD-32: static instruments.
    if "AD-31" in selected:
        is_red = instruments.check_ad31()
        report.rows.append(RowResult(row_id="AD-31", outcome=RowOutcome.RED if is_red else RowOutcome.GREEN))
        if is_red:
            report.failures.append(
                ConformanceFailure(
                    code=FailureCode.ROW_UNEXPECTED,
                    hint="AD-31: an obligation row has no five-rules row behind it, or vice versa",
                    next_action="fix OBLIGATION_TO_FIVE_RULES_ROW in instruments.py or the mapping it describes",
                    row_ids=("AD-31",),
                )
            )
    if "AD-32" in selected:
        document_text = instruments.default_sixth_document_path().read_text()
        is_red = instruments.check_ad32(document_text)
        report.rows.append(RowResult(row_id="AD-32", outcome=RowOutcome.RED if is_red else RowOutcome.GREEN))
        if is_red:
            report.failures.append(
                ConformanceFailure(
                    code=FailureCode.ROW_UNEXPECTED,
                    hint="AD-32: the sixth adapter's document is missing a required part",
                    next_action="add the missing part per docs/foundation/adapters.md#what-an-adapters-document-must-contain",
                    row_ids=("AD-32",),
                )
            )

    # AD-27..30, AD-33..34: never omitted, never a silent skip.
    for row_id in sorted(INSTRUMENT_MISSING_ROWS & selected):
        report.rows.append(RowResult(row_id=row_id, outcome=RowOutcome.RED))
        report.failures.append(
            ConformanceFailure(
                code=FailureCode.INSTRUMENT_MISSING,
                hint=f"{row_id}: no grant/workflow/provenance fixture exists in this repo yet",
                next_action="out of scope for ateles#1191 (see #921/#963); do not select this row until a fixture lands",
                row_ids=(row_id,),
            )
        )

    return report


def _bind_variant_factory(make_variant, base_factory):
    """Close over `make_variant`/`base_factory` so `_run_variant` gets a zero-arg callable."""
    return lambda: make_variant(base_factory)


def _run_variant(
    report: RunReport,
    adapter_factory,
    variant: VariantId,
    expected: frozenset[RowId],
    *,
    report_rows: frozenset[RowId] | None = None,
) -> None:
    """`adapter_factory` is a zero-arg callable returning the already-mutated variant adapter
    (built by the caller from a `VARIANT_FACTORIES[variant]` applied to a base factory, or, in
    this fixture's own tests, a hand-built factory that deliberately does or does not apply the
    mutation — that is how `test_variants_self_check.py` plants 0-red and >1-red cases).

    The self-check ("exactly this set turned red") always evaluates every row in `expected` —
    checking a subset would make a coupled-row failure invisible to a single-row filter. Which
    `RowResult`s land in `report.rows` is separately restricted to `report_rows` (default: all of
    `expected`), so `run("AD-4")` reports only AD-4 while still correctly self-checking AD-22 too.
    """
    if report_rows is None:
        report_rows = expected
    adapter: ReferenceAdapter = adapter_factory()
    observed_red: set[RowId] = set()
    for row_id in sorted(expected):
        check = ROW_CHECKS[row_id]
        is_red = check(adapter)
        if row_id in report_rows:
            report.rows.append(
                RowResult(row_id=row_id, outcome=RowOutcome.RED if is_red else RowOutcome.GREEN, variant=variant)
            )
        if is_red:
            observed_red.add(row_id)
    adapter.cleanup()

    if not observed_red:
        report.failures.append(
            ConformanceFailure(
                code=FailureCode.SELF_CHECK_ZERO_RED,
                hint=f"variant for obligation {variant} ({VARIANT_DESCRIPTIONS[variant]}) turned no rows red",
                next_action=f"obligation {variant} has no failing artefact — fix the mutant or the instrument",
                variant=variant,
                expected_red=expected,
                observed_red=frozenset(),
                row_ids=tuple(sorted(expected)),
            )
        )
    elif observed_red != expected:
        code = FailureCode.SELF_CHECK_MULTI_RED if len(observed_red) > len(expected) else FailureCode.ROW_UNEXPECTED
        report.failures.append(
            ConformanceFailure(
                code=code,
                hint=(
                    f"variant for obligation {variant} ({VARIANT_DESCRIPTIONS[variant]}) turned "
                    f"{sorted(observed_red)} red, expected exactly {sorted(expected)}"
                ),
                next_action="rows are coupled, or the mutant/instrument does not isolate the obligation",
                variant=variant,
                expected_red=expected,
                observed_red=frozenset(observed_red),
                row_ids=tuple(sorted(expected | observed_red)),
            )
        )


def format_report(report: RunReport) -> str:
    lines: list[str] = []
    for row in report.rows:
        variant_field = str(row.variant) if row.variant is not None else "-"
        lines.append(f"{row.row_id}\t{row.outcome.value}\tvariant={variant_field}")
    for failure in report.failures:
        lines.append(str(failure))
    return "\n".join(lines)
