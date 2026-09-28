from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_foundation_decision_119 as decision_119  # noqa: E402

REPO_ROOT = SCRIPT_DIR.parents[1]
CORPUS_FILES = (
    "conformance.md",
    "planning_model.md",
    "data_model.md",
    "conformance_suite.md",
)


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


def copy_actual_corpus(root: Path) -> None:
    source = REPO_ROOT / "docs" / "foundation"
    target = root / "docs" / "foundation"
    target.mkdir(parents=True)
    for name in CORPUS_FILES:
        shutil.copy2(source / name, target / name)


def mutate_actual_corpus(root: Path, filename: str, old: str, new: str) -> None:
    path = root / "docs" / "foundation" / filename
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1, f"mutation target drifted in {filename}: {old!r}"
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_passes_when_ruled_shape_is_complete(tmp_path: Path) -> None:
    copy_actual_corpus(tmp_path)

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
    copy_actual_corpus(tmp_path)
    mutate_actual_corpus(
        tmp_path,
        "planning_model.md",
        "it is never completion evidence and cannot\nsubstitute for the effect it allowed",
        "it may be completion evidence and may\nsubstitute for the effect it allowed",
    )

    problems = decision_119.check(tmp_path)

    assert any(
        "decision-119-completion-checkpoint-negative" in problem for problem in problems
    )


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


@pytest.mark.parametrize(
    ("old", "new", "label"),
    (
        (
            "The proposer needs\na grant that admits the action and the engine's write",
            "The proposer needs\na role that names the action and the engine's write",
            "control-grant",
        ),
        (
            "`ownership_grant` on the record supplies the required checkpoint seat",
            "`ownership_grant` on the record is consulted",
            "control-owner-seat",
        ),
        (
            "existing action gate permits the action and its read-back confirms the effect",
            "engine accepts the action and its read-back confirms the effect",
            "control-action-gate",
        ),
        (
            "existing `amend_<level>` action and a confirmed planning decision",
            "existing `amend_<level>` action and a planning decision",
            "scope-confirmed-amendment",
        ),
        (
            "satisfied\nonly by that record's proved completion, never by its cancellation",
            "satisfied\nby that record's completion or cancellation",
            "frontier-dependency-satisfaction",
        ),
        (
            "it is never completion evidence and cannot\nsubstitute for the effect it allowed",
            "it may be completion evidence and may\nsubstitute for the effect it allowed",
            "completion-checkpoint-negative",
        ),
        (
            "while paused it remains\nreachable only to evaluate and take `resume_<level>` or `cancel_<level>`",
            "while paused it remains\ninert until delivery work is restarted elsewhere",
            "recovery-control-reachability",
        ),
    ),
)
def test_actual_corpus_mutation_breaks_normative_clause(
    tmp_path: Path, old: str, new: str, label: str
) -> None:
    copy_actual_corpus(tmp_path)
    mutate_actual_corpus(tmp_path, "planning_model.md", old, new)

    problems = decision_119.check(tmp_path)

    assert any(f"decision-119-{label}" in problem for problem in problems)


@pytest.mark.parametrize(
    ("row_id", "old", "new", "label"),
    (
        (
            "PM-13",
            "any effect without the action gate and owner seat",
            "an effect is observed",
            "pm-13-action-gate-owner-seat",
        ),
        (
            "PM-14",
            "a blocked dependency enters the frontier",
            "a dependency is inspected",
            "pm-14-blocked-dependency",
        ),
        (
            "PM-15",
            "`PL1` completes while the delivery recurrence is unbounded",
            "`PL1` observes the delivery recurrence",
            "pm-15-unbounded-recurrence",
        ),
        (
            "PM-16",
            "refuse parent completion does not fire with an unmet criterion",
            "parent completion is inspected",
            "pm-16-refuse-parent-completion",
        ),
        (
            "PM-17",
            "`resume_plan` is unreachable while delivery is quiesced or bypasses authorization or confirmed read-back",
            "`resume_plan` is inspected after restart",
            "pm-17-resume-control-path",
        ),
    ),
)
def test_actual_corpus_mutation_breaks_row_specific_refusal_contract(
    tmp_path: Path, row_id: str, old: str, new: str, label: str
) -> None:
    copy_actual_corpus(tmp_path)
    mutate_actual_corpus(tmp_path, "conformance_suite.md", old, new)

    problems = decision_119.check(tmp_path)

    assert any(f"decision-119-{label}" in problem for problem in problems), (
        row_id,
        problems,
    )
