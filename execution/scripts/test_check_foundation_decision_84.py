from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_foundation_decision_84 as decision_84  # noqa: E402

CORPUS = {
    "conformance.md": "| 84 | question | pointer | dependency | **ruled**: every workflow-entering task gets an intake batch; an aggregate parent is exempt; persistent assembly exclusion survives a lapsed lease; creation grants no lease; only the `pm` step owner claims |\n",
    "work_model.md": "### Intake is every task's first workflow\nEvery workflow-entering task's intake batch opens at creation; an aggregate parent task never enters a workflow and has no intake batch.\n### What distinguishes a task being assembled from one intake has not reached\nFor a workflow-entering task: persistent assembly exclusion; lease lapse; creation grants no lease; the declared `pm` step owner; multi-agent assembly. The creating principal receives no `classify` lease by being the creator. Contributors gain no lease or execution privilege.\n### What a claim predicate treats as claimable\nThe assembly exclusion exposes its open `classify` step only to a principal that resolves as the declaration's `pm` step owner.\n### A task is live when some principal could claim it now\n### How a batch is formed, and what chooses its workflow\nEvery workflow-entering task has an intake batch on creation; an assembling task adds a persistent classify hold. The consequence worth naming has two forms, not one. An intake batch opens in the admitted creation unit of the workflow-entering task, without a predecessor verdict. Every successor batch is opened by a closing verdict.\n**A successor batch's tasks are chosen by its verdict.**\n### A batch may hold on a condition discovered mid-flight\nThe assembly exception admits its creator-time finding before a step owner or held lease exists; its persistent assembly exclusion survives the lease lapse.\n### A batch may depend on a task it created\n### Parent and child tasks\nAn **aggregate parent task is not claimable, never enters a workflow, and has no intake batch or `ADDRESSED_BY` edge** — it is a grouping, and a batch carries tasks that are executed, which an aggregate parent never is.\n### A recurring task is one live instance, and its completion creates the next\n### Where tasks come from: every source, indexed\nEvery workflow-entering task source ends at the universal workflow entry: a task with its intake batch at creation; an aggregate parent is the exception.\n### An intake rule turns a described change in the record into a task, and nothing else\n",
    "data_model.md": "| task | every workflow-entering task | intake batch at creation; aggregate parent has none |\nA creator-time assembly finding is a persistent assembly exclusion that survives lease lapse and exposes only the declaration-resolved `pm` owner.\n",
    "workflows.md": "## intake\n**Entry condition:** every workflow-entering task enters with its intake batch and `ADDRESSED_BY` edge admitted atomically at creation. An aggregate parent never enters a workflow; decision 84's assembly exception adds a persistent `classify` hold.\n## feature\nEvery workflow-entering task is mentioned here too, but this section cannot satisfy intake's contract.\n",
    "scenarios.md": "## (f) A parent task with children in independent batches\n**aggregate parent is the explicit workflow-entry exception: it is not claimable, never enters a workflow, and has no intake batch or `ADDRESSED_BY` edge.**\n## (j) A task created, routed by intake, and entering its successor\nEvery workflow-entering task has its intake batch at creation; an assembling task is the ruled exception only in carrying a persistent assembly exclusion, and only the resolved `pm` step owner claims.\nC[task and intake batch created atomically] --> I[intake batch: unrouted with no route verdict]\nF -.->|FOLLOWS| I\n## What the scenarios do not show\n",
    "conformance_suite.md": "| WM-13 | `work_model.md#intake-is-every-tasks-first-workflow`: every workflow-entering task atomically gets one intake batch and `ADDRESSED_BY` at creation; an aggregate parent gets neither and is not claimable | read | red | M |\n| WM-14 | every workflow-entering child task | intake batch on creation | read | red | M |\n| WM-14a | every workflow-entering task | persistent hold | lapse and transfer | creator and declaration-resolved `pm` step owner | mutant |\n| WM-21 | every daemon-created workflow-entering task | intake batch at creation | read | red | M |\n| WM-27 | intake batch opens at creation without a predecessor verdict; every successor batch is opened by a closing verdict | read | red | M |\n| WM-31 | assembly exception creator-time finding | fixture | read | survives lapse | M |\n| WM-31a | assembly exception | lapse | read | PM-only | M |\n| WM-32b | every workflow-entering peer task | intake batch at creation | read | red | M |\n| WM-35 | `work_model.md#parent-and-child-tasks`: an aggregate parent is not claimable, never enters a workflow, and has no intake batch or `ADDRESSED_BY`; its children are workflow-entering tasks | read | red | M |\n| WM-35a | every recurring workflow-entering task | intake batch at creation | read | red | M |\n| WM-39 | every ordinary workflow-entering peer task and every assembling task gets an intake batch at creation; aggregate parent is the exception; the assembly exception adds a persistent assembly exclusion | declared `pm` step owner | effect | red | M |\n",
}

# Keep the compact fixture's pinned semantic paragraphs identical to the live
# corpus while leaving the rest of the fixture deliberately minimal.
CORPUS["workflows.md"] = CORPUS["workflows.md"].replace(
    "**Entry condition:** every workflow-entering task enters with its intake "
    "batch and `ADDRESSED_BY` edge admitted atomically at creation. An aggregate "
    "parent never enters a workflow; decision 84's assembly exception adds a "
    "persistent `classify` hold.",
    decision_84.INTAKE_ENTRY_PARAGRAPH,
).replace("## intake\n", "## intake\n\n").replace("\n## feature", "\n\n## feature")
CORPUS["work_model.md"] = CORPUS["work_model.md"].replace(
    "The consequence worth naming has two forms, not one. An intake batch opens "
    "in the admitted creation unit of the workflow-entering task, without a "
    "predecessor verdict. Every successor batch is opened by a closing verdict.",
    decision_84.BATCH_OPENING_PARAGRAPH,
).replace(
    "An **aggregate parent task is not claimable, never enters a workflow, and "
    "has no intake batch or `ADDRESSED_BY` edge** — it is a grouping, and a batch "
    "carries tasks that are executed, which an aggregate parent never is.",
    decision_84.AGGREGATE_PARENT_MODEL_PARAGRAPH,
).replace(
    "### How a batch is formed, and what chooses its workflow\n",
    "### How a batch is formed, and what chooses its workflow\n\n",
).replace(
    "persistent classify hold. The consequence worth naming",
    "persistent classify hold.\n\nThe consequence worth naming",
).replace(
    "predicate must have matched.\n**A successor batch's tasks",
    "predicate must have matched.\n\n**A successor batch's tasks",
).replace(
    "### Parent and child tasks\n",
    "### Parent and child tasks\n\n",
).replace(
    "intake batch atomically at creation.\n### A recurring task",
    "intake batch atomically at creation.\n\n### A recurring task",
)
CORPUS["scenarios.md"] = CORPUS["scenarios.md"].replace(
    "**aggregate parent is the explicit workflow-entry exception: it is not "
    "claimable, never enters a workflow, and has no intake batch or `ADDRESSED_BY` "
    "edge.**",
    decision_84.AGGREGATE_PARENT_SCENARIO_PARAGRAPH,
).replace(
    "## (f) A parent task with children in independent batches\n",
    "## (f) A parent task with children in independent batches\n\n",
).replace(
    "stored nowhere.\n## (j)",
    "stored nowhere.\n\n```mermaid\nflowchart TD\n```\n\n## (j)",
)


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


def mutate_real_corpus(
    tmp_path: Path, name: str, old: str, new: str
) -> list[str]:
    fdir = tmp_path / "docs" / "foundation"
    fdir.mkdir(parents=True)
    for corpus_name in CORPUS:
        shutil.copy2(REPO_ROOT / "docs" / "foundation" / corpus_name, fdir)
    path = fdir / name
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
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
    problems = mutate(tmp_path, "workflows.md", "Decision 84's assembly exception", "every task")
    assert any("workflow-exception" in problem for problem in problems)


def test_ordinary_task_without_intake_batch_at_creation_mutant_fails(tmp_path: Path) -> None:
    problems = mutate(
        tmp_path,
        "workflows.md",
        "every workflow-entering task enters with its intake batch and `ADDRESSED_BY` edge admitted atomically at creation",
        "Every non-assembly task meets the no-intake-batch entry condition once, at creation",
    )
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


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
        "An **aggregate parent task is not claimable, never enters a workflow, and has no intake batch or `ADDRESSED_BY` edge** — it is a grouping, and a batch carries tasks that are executed, which an aggregate parent never is.",
        "An aggregate parent task enters intake and receives an intake batch.",
    )
    assert any("aggregate-parent" in problem for problem in problems)


def test_intake_atomic_entry_is_scoped_to_intake_workflow(tmp_path: Path) -> None:
    problems = mutate(
        tmp_path,
        "workflows.md",
        "every workflow-entering task enters with its intake batch and `ADDRESSED_BY` edge admitted atomically at creation",
        "Tasks may enter intake later without atomic admission",
    )
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


def test_aggregate_parent_with_addressed_by_edge_fails(tmp_path: Path) -> None:
    problems = mutate(
        tmp_path,
        "work_model.md",
        "never enters a workflow, and has no intake batch or `ADDRESSED_BY` edge",
        "never enters a workflow, and has no intake batch but does carry an `ADDRESSED_BY` edge",
    )
    assert any("aggregate-parent-model" in problem for problem in problems)


def test_wm13_eventual_intake_without_edge_fails(tmp_path: Path) -> None:
    problems = mutate(
        tmp_path,
        "conformance_suite.md",
        "every workflow-entering task atomically gets one intake batch and `ADDRESSED_BY` at creation",
        "every workflow-entering task gets its intake batch eventually",
    )
    assert any("wm-13-atomic-entry" in problem for problem in problems)


def test_real_wm13_eventual_only_mutant_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "conformance_suite.md",
        "every workflow-entering task atomically gets one intake batch and "
        "`ADDRESSED_BY` at creation",
        "every workflow-entering task atomically gets one intake batch and "
        "`ADDRESSED_BY` eventually",
    )
    assert any("wm-13-atomic-entry" in problem for problem in problems)


def test_real_wm13_non_atomic_only_mutant_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "conformance_suite.md",
        "every workflow-entering task atomically gets one intake batch and "
        "`ADDRESSED_BY` at creation",
        "every workflow-entering task separately gets one intake batch and "
        "`ADDRESSED_BY` at creation",
    )
    assert any("wm-13-atomic-entry" in problem for problem in problems)


@pytest.mark.parametrize(
    "replacement",
    (
        "every workflow-entering task not atomically gets one intake batch and "
        "`ADDRESSED_BY` at creation",
        "every workflow-entering task non-atomically gets one intake batch and "
        "`ADDRESSED_BY` at creation",
        "every workflow-entering task atomically gets one intake batch and "
        "`ADDRESSED_BY` after creation",
    ),
)
def test_real_wm13_negated_or_delayed_wording_fails(
    tmp_path: Path, replacement: str
) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "conformance_suite.md",
        "every workflow-entering task atomically gets one intake batch and "
        "`ADDRESSED_BY` at creation",
        replacement,
    )
    assert any("wm-13-atomic-entry" in problem for problem in problems)


@pytest.mark.parametrize(
    "replacement",
    (
        "every workflow-entering task enters with its intake batch and "
        "`ADDRESSED_BY` edge not admitted atomically at\ncreation",
        "every workflow-entering task enters with its intake batch and "
        "`ADDRESSED_BY` edge admitted non-atomically at\ncreation",
        "every workflow-entering task enters with its intake batch and "
        "`ADDRESSED_BY` edge admitted later after\ncreation",
    ),
)
def test_real_intake_negated_or_delayed_atomic_entry_fails(
    tmp_path: Path, replacement: str
) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "workflows.md",
        "every workflow-entering task enters with its intake batch and "
        "`ADDRESSED_BY` edge admitted atomically at\ncreation",
        replacement,
    )
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


def test_real_batch_opening_predecessor_inversion_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "work_model.md",
        "the workflow-entering task, without a predecessor verdict. "
        "Every successor batch is opened",
        "the workflow-entering task, only after a predecessor verdict. "
        "Every successor batch is opened",
    )
    assert any("batch-opening-model" in problem for problem in problems)


def test_real_wm13_fails_to_atomic_inversion_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "conformance_suite.md",
        "every workflow-entering task atomically gets one intake batch and "
        "`ADDRESSED_BY` at creation",
        "every workflow-entering task fails to atomically get one intake batch and "
        "`ADDRESSED_BY` at creation",
    )
    assert any("wm-13-atomic-entry" in problem for problem in problems)


def test_real_wm13_no_addressed_by_inversion_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "conformance_suite.md",
        "every workflow-entering task atomically gets one intake batch and "
        "`ADDRESSED_BY` at creation",
        "every workflow-entering task atomically gets one intake batch and no "
        "`ADDRESSED_BY` at creation",
    )
    assert any("wm-13-atomic-entry" in problem for problem in problems)


def test_real_intake_never_atomic_inversion_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "workflows.md",
        "`ADDRESSED_BY` edge admitted atomically at\ncreation",
        "`ADDRESSED_BY` edge is never admitted atomically at\ncreation",
    )
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


def test_real_intake_creation_or_later_inversion_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "workflows.md",
        "`ADDRESSED_BY` edge admitted atomically at\ncreation",
        "`ADDRESSED_BY` edge admitted atomically at\ncreation or later",
    )
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


def test_real_batch_opening_not_without_inversion_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "work_model.md",
        "the workflow-entering task, without a predecessor verdict. "
        "Every successor batch is opened",
        "the workflow-entering task, not without a predecessor verdict. "
        "Every successor batch is opened",
    )
    assert any("batch-opening-model" in problem for problem in problems)


def test_real_aggregate_parent_qualified_exception_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "work_model.md",
        "is not claimable,\nnever enters a workflow, and has no intake batch or "
        "`ADDRESSED_BY` edge**",
        "is not claimable until publication,\nnever enters a workflow without an "
        "intake batch, and has no intake batch or `ADDRESSED_BY` edge until one "
        "is created**",
    )
    assert any("aggregate-parent-model" in problem for problem in problems)


def test_real_intake_appended_contradiction_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "workflows.md",
        "creation. Creation is publication",
        "creation. Despite that sentence, a workflow-entering task need not receive "
        "its intake batch or `ADDRESSED_BY` atomically at creation. Creation is "
        "publication",
    )
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


def test_real_parent_model_appended_qualification_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "work_model.md",
        "which an aggregate parent never is. This is the deliberate exception",
        "which an aggregate parent never is. This exception lasts only until "
        "publication; afterward the aggregate parent becomes claimable and enters "
        "a workflow. This is the deliberate exception",
    )
    assert any("aggregate-parent-model" in problem for problem in problems)


def test_real_batch_opening_appended_inversion_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "work_model.md",
        "task, without a predecessor verdict. Every successor batch",
        "task, without a predecessor verdict. Despite that sentence, an intake "
        "batch may require a predecessor verdict. Every successor batch",
    )
    assert any("batch-opening-model" in problem for problem in problems)


def test_real_parent_scenario_appended_qualification_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "scenarios.md",
        "edge.** When a reader asks whether the parent is complete",
        "edge.** This holds only before publication; afterward the parent may enter "
        "a workflow. When a reader asks whether the parent is complete",
    )
    assert any("aggregate-parent-scenario" in problem for problem in problems)


def test_real_intake_adjacent_paragraph_contradiction_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "workflows.md",
        "(`work_model.md#intake-is-every-tasks-first-workflow`).\n\n**Steps**",
        "(`work_model.md#intake-is-every-tasks-first-workflow`).\n\n"
        "Despite the entry condition above, a workflow-entering task may be "
        "published before its intake batch or `ADDRESSED_BY` edge exists.\n\n"
        "**Steps**",
    )
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


def test_real_batch_opening_adjacent_paragraph_inversion_fails(
    tmp_path: Path,
) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "work_model.md",
        "predicate\nmust have matched.\n\n**A successor batch's tasks",
        "predicate\nmust have matched.\n\nDespite the preceding rule, an intake "
        "batch may require a predecessor verdict.\n\n**A successor batch's tasks",
    )
    assert any("batch-opening-model" in problem for problem in problems)


def test_real_parent_model_adjacent_paragraph_qualification_fails(
    tmp_path: Path,
) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "work_model.md",
        "intake batch atomically at creation.\n\n**A task's one `PART_OF` edge",
        "intake batch atomically at creation.\n\nDespite that exception, an "
        "aggregate parent becomes claimable and enters a workflow after "
        "publication.\n\n**A task's one `PART_OF` edge",
    )
    assert any("aggregate-parent-model" in problem for problem in problems)


def test_real_parent_scenario_adjacent_paragraph_qualification_fails(
    tmp_path: Path,
) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "scenarios.md",
        "stored nowhere.\n\n```mermaid",
        "stored nowhere.\n\nDespite that exception, the aggregate parent may "
        "enter a workflow after publication.\n\n```mermaid",
    )
    assert any("aggregate-parent-scenario" in problem for problem in problems)


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
        ("WM-13", "atomically gets one intake batch and `ADDRESSED_BY` at creation"),
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
