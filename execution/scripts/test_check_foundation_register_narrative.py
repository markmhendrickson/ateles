"""Fail-then-pass coverage for the register-narrative checker.

An unverified guard is not a control (`principles.md`, invariant 1): a checker
that has never been shown to fail proves nothing when it passes, because a
checker returning no problems unconditionally passes too. Every test here
therefore asserts a planted violation is REPORTED and the corresponding good
text is CLEAN, so each report is shown to bind.

The negative tests matter as much as the positive ones here. This check earns
its place only if it leaves the register's dated history alone — that history is
the narrative's most valuable content, and a check that flagged it would be
removed within a pass.
"""

from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_foundation_register_narrative as rn  # noqa: E402


# A minimal register: 73 and 79 open, 72 ruled, 20 withdrawn, 19 not a decision.
TABLE = """\
# Conformance

## The register of open design decisions

| # | Question | Where argued | Scope | Status |
|---|---|---|---|---|
| 1–12 | the twelve questions of revision 12 | — | — | **ruled** (2026-09-04) |
| 19 | — | — | — | **not a decision** |
| 20 | — | — | — | **withdrawn** |
| 72 | whether `verdict` names its record neutrally | `vocabulary.md#verdict` | — | \
**ruled** (2026-09-08): the record is the `verdict`; a batch is never reopened |
| 73 | whether a term should prefer a single word | `principles.md` | — | **open** (2026-09-06) |
| 79 | what a declaration scope names | `workflows.md` | — | **open** (2026-09-07) |
"""


def _doc(root: Path, narrative: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "conformance.md").write_text(TABLE + "\n" + narrative, encoding="utf-8")
    return root


# --------------------------------------------------------------------------
# The table parse itself — the check is only as good as this.
# --------------------------------------------------------------------------


def test_status_is_read_from_the_bold_lead_not_the_whole_cell() -> None:
    """Row 72 is ruled, though the word "reopened" sits later in its cell.

    Matching a status keyword anywhere in the cell mislabelled nine ruled rows
    as open on the real document ("never reopened", "before the step opens").
    """
    rows = rn.parse_register(TABLE)
    assert rows["72"] == "ruled"
    assert rows["73"] == "open"
    assert rows["19"] == "not a decision"
    assert rows["20"] == "withdrawn"
    assert rn.open_numbers(rows) == {73, 79}


# --------------------------------------------------------------------------
# stale-open-rows
# --------------------------------------------------------------------------


def test_stale_present_tense_open_rows_is_reported(tmp_path: Path) -> None:
    """The exact shape found on main: a bare present-tense list."""
    root = _doc(tmp_path, "**The open rows: 72 and 73.**\n")
    problems = rn.check(root)
    assert any("stale-open-rows" in p for p in problems), problems
    assert any("[73, 79]" in p for p in problems), problems


def test_correct_present_tense_open_rows_is_clean(tmp_path: Path) -> None:
    """The pass half: the same shape, agreeing with the table."""
    root = _doc(tmp_path, "The open rows: 73 and 79.\n")
    assert rn.check(root) == []


def test_stale_zero_claim_is_reported(tmp_path: Path) -> None:
    """"zero" is a checkable claim, not an absence of one."""
    root = _doc(tmp_path, "The open rows: zero.\n")
    problems = rn.check(root)
    assert any("stale-open-rows" in p for p in problems), problems


def test_pointing_at_the_table_is_clean(tmp_path: Path) -> None:
    """The prescribed fix — naming no numbers — passes."""
    root = _doc(
        tmp_path,
        "Which rows are open is read from the status column of the table above,\n"
        "and is not restated here.\n",
    )
    assert rn.check(root) == []


# --------------------------------------------------------------------------
# The history exemption — the reason this check is safe to run
# --------------------------------------------------------------------------


def test_dated_history_with_wrong_numbers_is_left_alone(tmp_path: Path) -> None:
    """"were then" dates the claim to its pass; it is history, not drift.

    These numbers disagree with the table on purpose: that is exactly what a
    correct historical sentence looks like once later passes have ruled rows.
    """
    root = _doc(tmp_path, "The open rows were then four — 33, 34, 55, and 57.\n")
    assert rn.check(root) == []


def test_pass_scoped_history_is_left_alone(tmp_path: Path) -> None:
    root = _doc(tmp_path, "The open rows after this pass: 40, 55, and 64.\n")
    assert rn.check(root) == []


def test_after_all_passes_history_is_left_alone(tmp_path: Path) -> None:
    """The line 600 shape on the real document."""
    root = _doc(
        tmp_path,
        "**The open rows after all four passes: zero** — every decision 1 "
        "through 65 was then ruled.\n",
    )
    assert rn.check(root) == []


# --------------------------------------------------------------------------
# next-free-number
# --------------------------------------------------------------------------


def test_next_free_number_is_reported_even_when_arithmetically_right(
    tmp_path: Path,
) -> None:
    """No value is correct: a concurrent branch can take it before it is read.

    80 is genuinely the next number after this table's highest row, and it is
    still reported — the defect is stating a number at all.
    """
    root = _doc(tmp_path, "The next free number is 80.\n")
    problems = rn.check(root)
    assert any("next-free-number" in p for p in problems), problems


def test_stating_the_sweep_without_a_number_is_clean(tmp_path: Path) -> None:
    """The fix already on main, in the paragraph this check protects."""
    root = _doc(
        tmp_path,
        "The next free number is established by sweeping every branch's copy of\n"
        "this table, and never by reading a number written here.\n",
    )
    assert rn.check(root) == []


# --------------------------------------------------------------------------
# status-contradiction
# --------------------------------------------------------------------------


def test_prose_calling_a_ruled_decision_open_is_reported(tmp_path: Path) -> None:
    root = _doc(tmp_path, "For now decision 72 is open, and turns on his judgement.\n")
    problems = rn.check(root)
    assert any("status-contradiction" in p for p in problems), problems


def test_prose_calling_an_open_decision_unruled_is_clean(tmp_path: Path) -> None:
    root = _doc(tmp_path, "Decision 73 remains unruled, deliberately.\n")
    assert rn.check(root) == []


def test_prose_naming_a_decision_with_no_row_is_reported(tmp_path: Path) -> None:
    """The register is the index; a number absent from it is unfindable."""
    root = _doc(tmp_path, "Decision 404 is open.\n")
    problems = rn.check(root)
    assert any("status-contradiction" in p for p in problems), problems


# --------------------------------------------------------------------------
# Scope limits, asserted so they are not mistaken for coverage
# --------------------------------------------------------------------------


def test_table_rows_are_never_scanned_as_narrative(tmp_path: Path) -> None:
    """A status cell's own prose must not be read as a claim about itself."""
    root = _doc(tmp_path, "")
    assert rn.check(root) == []


def test_the_real_register_is_clean(tmp_path: Path) -> None:
    """The shipped document passes its own check."""
    root = Path(__file__).resolve().parents[2] / "docs" / "foundation"
    if not (root / "conformance.md").is_file():
        return  # branch without the corpus; the CI step skips likewise
    assert rn.check(root) == []
