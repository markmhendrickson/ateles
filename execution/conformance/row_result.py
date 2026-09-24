"""Shared result and failure-code contracts for conformance suite runners.

Frozen ahead of any runner or instrument (arch: contracts before runners). `FailureCode` values match
the UX section of ateles#1191 exactly; a runner or instrument raises `ConformanceFailure` and never a
bare string or exception subclass of its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class RowOutcome(str, Enum):
    GREEN = "green"
    RED = "red"


class FailureCode(str, Enum):
    RUNTIME_MISSING = "RUNTIME_MISSING"
    EMPTY_SELECTION = "EMPTY_SELECTION"
    SELF_CHECK_ZERO_RED = "SELF_CHECK_ZERO_RED"
    SELF_CHECK_MULTI_RED = "SELF_CHECK_MULTI_RED"
    REFERENCE_RED = "REFERENCE_RED"
    ROW_UNEXPECTED = "ROW_UNEXPECTED"
    SURFACE_PARITY = "SURFACE_PARITY"
    INSTRUMENT_MISSING = "INSTRUMENT_MISSING"


# RowId is the foundation table's own vocabulary (`AD-21` .. `AD-34`); kept as a plain str alias
# rather than a NewType so instruments can build one with an f-string without a cast.
RowId = str

# VariantId names an admission obligation, 1 through 6 (`docs/foundation/adapters.md#the-admission-contract`).
VariantId = int


@dataclass(frozen=True)
class RowResult:
    row_id: RowId
    outcome: RowOutcome
    variant: Optional[VariantId] = None


@dataclass(frozen=True)
class ConformanceFailure(Exception):
    """A structured, first-class failure — never a log footnote.

    `row_ids` names every row the failure is about. `expected_red` / `observed_red` are populated
    for self-check failures (`SELF_CHECK_ZERO_RED`, `SELF_CHECK_MULTI_RED`, `ROW_UNEXPECTED`); both
    default empty for failure codes that carry no red-set comparison (e.g. `RUNTIME_MISSING`).
    """

    code: FailureCode
    hint: str
    next_action: str
    row_ids: tuple[RowId, ...] = field(default_factory=tuple)
    variant: Optional[VariantId] = None
    expected_red: frozenset[RowId] = field(default_factory=frozenset)
    observed_red: frozenset[RowId] = field(default_factory=frozenset)

    def __str__(self) -> str:  # pragma: no cover - trivial
        parts = [f"{self.code.value}: {self.hint}", f"next: {self.next_action}"]
        if self.row_ids:
            parts.append(f"rows={','.join(self.row_ids)}")
        if self.variant is not None:
            parts.append(f"variant={self.variant}")
        if self.expected_red or self.observed_red:
            parts.append(
                f"expected_red={{{','.join(sorted(self.expected_red))}}} "
                f"observed_red={{{','.join(sorted(self.observed_red))}}}"
            )
        return " | ".join(parts)
