from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_foundation_decision_119 as decision_119  # noqa: E402


CONFORMANCE = """\
| # | Question | Pointer | Dependencies | Status |
|---|---|---|---|---|
| 119 | commissioning | `planning_model.md#commissioning-a-planning-record-drives-one-dependency-ready-frontier` | decisions 31, 35, 68 | **ruled** (2026-09-28) |
"""

PLANNING = f"""\
# Planning

{decision_119.COMMISSIONING_HEADING}

`commission_plan`, `pause_plan`, `resume_plan`, and `cancel_plan` are actions.
Each confirmed action writes a decision linked by `SUPERSEDES`; there is no `commission_status`.
`DEPENDS_ON` may connect any task or planning record to a task or planning record.
The dependency-ready frontier exposes maximum safe parallelism.
Continuous planning is distinct from forever-recurring delivery work.
Every `completion_criteria[]` item needs descendant effect evidence.
A checkpoint is never completion evidence.
Newly discovered work requires admission; no silent scope expansion is allowed.
Resume must re-derive current work and never replays an effect.
The stop condition is proved completion or confirmed cancellation.

### Next section
"""

DATA_MODEL = """\
| Relationship | From -> To | Meaning |
|---|---|---|
| `DEPENDS_ON` | task or planning record -> task or planning record | dependency |
"""

SUITE = """\
| PM-13 | control | blocked dependency |
| PM-14 | frontier | independent parallel children |
| PM-15 | recurrence | forever recurrence |
| PM-16 | evidence | refuse parent completion |
| PM-17 | recovery | safe resumption |
"""


def write_corpus(
    root: Path,
    *,
    conformance: str = CONFORMANCE,
    planning: str = PLANNING,
    data_model: str = DATA_MODEL,
    suite: str = SUITE,
) -> None:
    fdir = root / "docs" / "foundation"
    fdir.mkdir(parents=True)
    (fdir / "conformance.md").write_text(conformance, encoding="utf-8")
    (fdir / "planning_model.md").write_text(planning, encoding="utf-8")
    (fdir / "data_model.md").write_text(data_model, encoding="utf-8")
    (fdir / "conformance_suite.md").write_text(suite, encoding="utf-8")


def test_passes_when_ruled_shape_is_complete(tmp_path: Path) -> None:
    write_corpus(tmp_path)

    assert decision_119.check(tmp_path) == []


def test_noop_while_register_row_is_open(tmp_path: Path) -> None:
    write_corpus(
        tmp_path,
        conformance=CONFORMANCE.replace("**ruled**", "**open**"),
        planning="# Planning\n",
    )

    assert decision_119.check(tmp_path) == []


def test_fails_when_register_row_is_absent(tmp_path: Path) -> None:
    write_corpus(tmp_path, conformance="# Conformance\n")

    problems = decision_119.check(tmp_path)

    assert problems == [
        f"{tmp_path / 'docs/foundation/conformance.md'}:1: "
        'decision-119-register — no register row beginning "| 119 |"'
    ]


def test_planted_red_fails_when_planning_dependency_target_is_removed(
    tmp_path: Path,
) -> None:
    """Pre-119 task-only DEPENDS_ON must fail once decision 119 is ruled."""
    task_only = DATA_MODEL.replace(
        "task or planning record -> task or planning record", "task -> task"
    )
    write_corpus(tmp_path, data_model=task_only)

    problems = decision_119.check(tmp_path)

    assert any("decision-119-depends-on" in problem for problem in problems)


def test_fails_when_checkpoint_is_allowed_to_prove_completion(tmp_path: Path) -> None:
    planning = PLANNING.replace(
        "A checkpoint is never completion evidence.",
        "A checkpoint can be completion evidence.",
    )
    write_corpus(tmp_path, planning=planning)

    problems = decision_119.check(tmp_path)

    assert any("decision-119-checkpoint-negative" in problem for problem in problems)


def test_fails_when_a_required_conformance_row_is_missing(tmp_path: Path) -> None:
    write_corpus(
        tmp_path, suite=SUITE.replace("| PM-17 | recovery | safe resumption |\n", "")
    )

    problems = decision_119.check(tmp_path)

    assert any("`PM-17`" in problem for problem in problems)


def test_raises_when_a_corpus_file_is_missing(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    (tmp_path / "docs/foundation/planning_model.md").unlink()

    with pytest.raises(decision_119.CorpusProblem, match="planning_model.md"):
        decision_119.check(tmp_path)
