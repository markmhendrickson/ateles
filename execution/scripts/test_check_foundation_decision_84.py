from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_foundation_decision_84 as decision_84  # noqa: E402

CORPUS = {
    "conformance.md": "| 84 | question | pointer | dependency | **ruled**: every workflow-entering task gets an intake batch; an aggregate parent is exempt; persistent assembly exclusion survives a lapsed lease; creation grants no lease; only the `pm` step owner claims |\n",
    "work_model.md": "### Intake is every task's first workflow\nEvery workflow-entering task's intake batch opens at creation; an aggregate parent task never enters a workflow and has no intake batch.\n### What distinguishes a task being assembled from one intake has not reached\nFor a workflow-entering task: persistent assembly exclusion; lease lapse; creation grants no lease; the declared `pm` step owner; multi-agent assembly. The creating principal receives no `classify` lease by being the creator. Contributors gain no lease or execution privilege.\n### What a claim predicate treats as claimable\nThe assembly exclusion exposes its open `classify` step only to a principal that resolves as the declaration's `pm` step owner.\n### A task is live when some principal could claim it now\n### How a batch is formed, and what chooses its workflow\nEvery workflow-entering task has an intake batch on creation; an assembling task adds a persistent classify hold. An intake batch opens at task creation without a predecessor verdict; only a successor batch is opened by a closing verdict.\n### A batch may hold on a condition discovered mid-flight\nThe assembly exception admits its creator-time finding before a step owner or held lease exists; its persistent assembly exclusion survives the lease lapse.\n### A batch may depend on a task it created\n### Parent and child tasks\nAn aggregate parent task never enters a workflow and has no intake batch; it is not claimable.\n### A recurring task is one live instance, and its completion creates the next\n### Where tasks come from: every source, indexed\nEvery workflow-entering task source ends at the universal workflow entry: a task with its intake batch at creation; an aggregate parent is the exception.\n### An intake rule turns a described change in the record into a task, and nothing else\n",
    "data_model.md": "| task | every workflow-entering task | intake batch at creation; aggregate parent has none |\nA creator-time assembly finding is a persistent assembly exclusion that survives lease lapse and exposes only the declaration-resolved `pm` owner.\n",
    "workflows.md": "Every workflow-entering task enters with an intake batch on creation; an aggregate parent never enters a workflow; decision 84's assembly exception adds a persistent `classify` hold.\n",
    "scenarios.md": "## (f) A parent task with children in independent batches\nAn aggregate parent task never enters a workflow and has no intake batch; it is not claimable.\n## (j) A task created, routed by intake, and entering its successor\nEvery workflow-entering task has its intake batch at creation; an assembling task is the ruled exception only in carrying a persistent assembly exclusion, and only the resolved `pm` step owner claims.\nC[task and intake batch created atomically] --> I[intake batch: unrouted with no route verdict]\nF -.->|FOLLOWS| I\n## What the scenarios do not show\n",
    "conformance_suite.md": "| WM-13 | every workflow-entering task | intake batch at creation | read | red | M |\n| WM-14 | every workflow-entering child task | intake batch on creation | read | red | M |\n| WM-14a | every workflow-entering task | persistent hold | lapse and transfer | creator and declaration-resolved `pm` step owner | mutant |\n| WM-21 | every daemon-created workflow-entering task | intake batch at creation | read | red | M |\n| WM-27 | intake batch opens at creation without a predecessor verdict; every successor batch is opened by a closing verdict | read | red | M |\n| WM-31 | assembly exception creator-time finding | fixture | read | survives lapse | M |\n| WM-31a | assembly exception | lapse | read | PM-only | M |\n| WM-32b | every workflow-entering peer task | intake batch at creation | read | red | M |\n| WM-35 | aggregate parent never enters a workflow, has no intake batch, and is not claimable | read | red | M |\n| WM-35a | every recurring workflow-entering task | intake batch at creation | read | red | M |\n| WM-39 | every ordinary workflow-entering peer task and every assembling task gets an intake batch at creation; aggregate parent is the exception; the assembly exception adds a persistent assembly exclusion | declared `pm` step owner | effect | red | M |\n",
}


def write_corpus(root: Path) -> None:
    fdir = root / "docs" / "foundation"
    fdir.mkdir(parents=True)
    for name, text in CORPUS.items():
        (fdir / name).write_text(text, encoding="utf-8")


def mutate(tmp_path: Path, name: str, old: str, new: str) -> list[str]:
    write_corpus(tmp_path)
    path = tmp_path / "docs" / "foundation" / name
    path.write_text(path.read_text().replace(old, new), encoding="utf-8")
    return decision_84.check(tmp_path)


def test_complete_carry_passes(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    assert decision_84.check(tmp_path) == []


def test_lease_only_mutant_fails(tmp_path: Path) -> None:
    problems = mutate(tmp_path, "conformance.md", "persistent assembly exclusion", "held lease")
    assert any("register" in problem for problem in problems)


def test_creator_ownership_mutant_fails(tmp_path: Path) -> None:
    problems = mutate(tmp_path, "work_model.md", "creation grants no lease", "creator gets lease")
    assert any("model" in problem for problem in problems)


def test_exact_creator_authority_clause_mutant_fails(tmp_path: Path) -> None:
    problems = mutate(
        tmp_path,
        "work_model.md",
        "The creating principal receives no `classify` lease by being the creator.",
        "The creator receives the `classify` lease automatically.",
    )
    assert any("creator-authority-model" in problem for problem in problems)


def test_exact_pm_only_claim_clause_mutant_fails(tmp_path: Path) -> None:
    problems = mutate(
        tmp_path,
        "work_model.md",
        "only to a principal that resolves as the declaration's `pm` step owner",
        "to any principal",
    )
    assert any("claim-model" in problem for problem in problems)


def test_generic_hold_model_without_assembly_exception_fails(tmp_path: Path) -> None:
    problems = mutate(tmp_path, "work_model.md", "creator-time", "ordinary")
    assert any("hold-model" in problem for problem in problems)


def test_finding_schema_without_persistent_exclusion_fails(tmp_path: Path) -> None:
    problems = mutate(tmp_path, "data_model.md", "persistent assembly exclusion", "ordinary hold")
    assert any("finding-schema" in problem for problem in problems)


def test_workflow_assembly_mutant_fails(tmp_path: Path) -> None:
    problems = mutate(tmp_path, "workflows.md", "decision 84's assembly exception", "every task")
    assert any("workflow-exception" in problem for problem in problems)


def test_ordinary_task_without_intake_batch_at_creation_mutant_fails(tmp_path: Path) -> None:
    problems = mutate(
        tmp_path,
        "workflows.md",
        "Every workflow-entering task enters with an intake batch on creation",
        "Every non-assembly task meets the no-intake-batch entry condition once, at creation",
    )
    assert any("universal-entry" in problem for problem in problems)


def test_retired_scenario_no_batch_diagram_fails(tmp_path: Path) -> None:
    problems = mutate(
        tmp_path,
        "scenarios.md",
        "C[task and intake batch created atomically] --> I[intake batch: unrouted with no route verdict]",
        "C[task created] --> U{intake batch exists?}\n"
        "U -->|no: unrouted by that fact| I[task enters intake; batch record opens]",
    )
    assert any("universal-entry" in problem for problem in problems)


def test_aggregate_parent_forced_into_intake_fails(tmp_path: Path) -> None:
    problems = mutate(
        tmp_path,
        "work_model.md",
        "An aggregate parent task never enters a workflow and has no intake batch; it is not claimable.",
        "An aggregate parent task enters intake and receives an intake batch.",
    )
    assert any("aggregate-parent" in problem for problem in problems)


def test_wm27_closing_verdict_claim_applied_to_intake_fails(tmp_path: Path) -> None:
    problems = mutate(
        tmp_path,
        "conformance_suite.md",
        "intake batch opens at creation without a predecessor verdict; every successor batch is opened by a closing verdict",
        "every batch is opened only by a closing verdict",
    )
    assert any("wm-27" in problem for problem in problems)


def test_scenario_intake_predecessor_node_must_be_defined(tmp_path: Path) -> None:
    problems = mutate(
        tmp_path,
        "scenarios.md",
        "C[task and intake batch created atomically] --> I[intake batch: unrouted with no route verdict]",
        "C[task and intake batch created atomically] --> U[unrouted with no route verdict]",
    )
    assert any("scenario-intake-node" in problem for problem in problems)


@pytest.mark.parametrize(
    "row, phrase",
    (
        ("WM-13", "intake batch at creation"),
        ("WM-14", "intake batch on creation"),
        ("WM-21", "intake batch at creation"),
        ("WM-32b", "intake batch at creation"),
        ("WM-35a", "intake batch at creation"),
        ("WM-39", "intake batch at creation"),
    ),
)
def test_each_creation_row_without_atomic_intake_entry_fails(
    tmp_path: Path, row: str, phrase: str
) -> None:
    problems = mutate(tmp_path, "conformance_suite.md", phrase, "later intake")
    assert any(f"universal-entry-{row.lower()}" in problem for problem in problems)


def test_wm14a_without_transfer_fails(tmp_path: Path) -> None:
    problems = mutate(tmp_path, "conformance_suite.md", "lapse and transfer", "renewal")
    assert any("wm-14a" in problem for problem in problems)


def test_wm39_without_exception_fails(tmp_path: Path) -> None:
    problems = mutate(tmp_path, "conformance_suite.md", "assembly exception", "all tasks lack a batch")
    assert any("wm-39" in problem for problem in problems)
