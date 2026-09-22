from __future__ import annotations

import re
import shutil
import sys
from collections.abc import Callable
from itertools import product
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_foundation_decision_84 as decision_84  # noqa: E402

CORPUS = {
    "conformance.md": "| 84 | question | pointer | dependency | **ruled**: every workflow-entering task gets an intake batch; an aggregate parent is exempt; persistent assembly exclusion survives a lapsed lease; creation grants no lease; only the `pm` step owner claims |\n",
    "vocabulary.md": "### claimable\nClaimable excludes an unresolved persistent assembly exclusion except for its PM-only classify claim.\n### terminal\nTerminal.\n",
    "work_model.md": "### Intake is every task's first workflow\nEvery workflow-entering task's intake batch opens at creation; an aggregate parent task never enters a workflow and has no intake batch.\n### What distinguishes a task being assembled from one intake has not reached\nFor a workflow-entering task: persistent assembly exclusion; lease lapse; creation grants no lease; the declared `pm` step owner; multi-agent assembly. The creating principal receives no `classify` lease by being the creator. Contributors gain no lease or execution privilege.\n### What a claim predicate treats as claimable\nThe assembly exclusion exposes its open `classify` step only to a principal that resolves as the declaration's `pm` step owner.\n### A task is live when some principal could claim it now\n### How a batch is formed, and what chooses its workflow\nEvery workflow-entering task has an intake batch on creation; an assembling task adds a persistent classify hold. The consequence worth naming has two forms, not one. An intake batch opens in the admitted creation unit of the workflow-entering task, without a predecessor verdict. Every successor batch is opened by a closing verdict.\n**A successor batch's tasks are chosen by its verdict.**\n### A batch may hold on a condition discovered mid-flight\nThe assembly exception admits its creator-time finding before a step owner or held lease exists; its persistent assembly exclusion survives the lease lapse.\n### A batch may depend on a task it created\n### Parent and child tasks\nAn **aggregate parent task is not claimable, never enters a workflow, and has no intake batch or `ADDRESSED_BY` edge** — it is a grouping, and a batch carries tasks that are executed, which an aggregate parent never is.\n### A recurring task is one live instance, and its completion creates the next\n### Where tasks come from: every source, indexed\nEvery workflow-entering task source ends at the universal workflow entry: a task with its intake batch at creation; an aggregate parent is the exception.\n### An intake rule turns a described change in the record into a task, and nothing else\n",
    "data_model.md": "| task | every workflow-entering task | intake batch at creation; aggregate parent has none |\nA creator-time assembly finding is a persistent assembly exclusion that survives lease lapse and exposes only the declaration-resolved `pm` owner.\n",
    "workflows.md": "## intake\n**Entry condition:** every workflow-entering task enters with its intake batch and `ADDRESSED_BY` edge admitted atomically at creation. An aggregate parent never enters a workflow; decision 84's assembly exception adds a persistent `classify` hold.\n## feature\nEvery workflow-entering task is mentioned here too, but this section cannot satisfy intake's contract.\n",
    "scenarios.md": "## (f) A parent task with children in independent batches\n**aggregate parent is the explicit workflow-entry exception: it is not claimable, never enters a workflow, and has no intake batch or `ADDRESSED_BY` edge.**\n## (j) A task created, routed by intake, and entering its successor\nEvery workflow-entering task has its intake batch at creation; an assembling task is the ruled exception only in carrying a persistent assembly exclusion, and only the resolved `pm` step owner claims.\nC[task and intake batch created atomically] --> I[intake batch: unrouted with no route verdict]\nF -.->|FOLLOWS| I\n## What the scenarios do not show\n",
    "conformance_suite.md": "| WM-13 | `work_model.md#intake-is-every-tasks-first-workflow`: every workflow-entering task atomically gets one intake batch and `ADDRESSED_BY` at creation; an aggregate parent gets neither and is not claimable | read | red | M |\n| WM-14 | every workflow-entering child task | intake batch on creation | read | red | M |\n| WM-14a | every workflow-entering task | persistent hold | lapse and transfer | creator and declaration-resolved `pm` step owner | mutant |\n| WM-21 | every daemon-created workflow-entering task | intake batch at creation | read | red | M |\n| WM-27 | intake batch opens at creation without a predecessor verdict; every successor batch is opened by a closing verdict | read | red | M |\n| WM-31 | assembly exception creator-time finding | fixture | read | survives lapse | M |\n| WM-31a | assembly exception | lapse | read | PM-only | M |\n| WM-32b | every workflow-entering peer task | intake batch at creation | read | red | M |\n| WM-35 | `work_model.md#parent-and-child-tasks`: an aggregate parent is not claimable, never enters a workflow, and has no intake batch or `ADDRESSED_BY`; its children are workflow-entering tasks | read | red | M |\n| WM-35a | every recurring workflow-entering task | intake batch at creation | read | red | M |\n| WM-39 | every ordinary workflow-entering peer task and every assembling task gets an intake batch at creation; aggregate parent is the exception; the assembly exception adds a persistent assembly exclusion | declared `pm` step owner | effect | red | M |\n",
}

# Keep the compact fixture's protected semantic regions identical to the live
# corpus while leaving every unrelated section deliberately minimal.
CORPUS["workflows.md"] = (
    "## intake\n\n"
    + decision_84.INTAKE_SEMANTIC_BLOCK
    + "\n\n| # | Step | Step owner (role) | Required | Parallel / join | Closes on |\n"
    + "|---|---|---|---|---|---|\n"
    + "| 1 | `classify` | `pm` | yes | | routed |\n\n"
    + "## feature\nEvery workflow-entering task is mentioned here too, but this "
    + "section cannot satisfy intake's contract.\n"
)

work_model = CORPUS["work_model.md"]
batch_start = work_model.index(
    "### How a batch is formed, and what chooses its workflow"
)
batch_end = work_model.index(
    "### A batch may hold on a condition discovered mid-flight"
)
batch_section = (
    "### How a batch is formed, and what chooses its workflow\n\n"
    "Every workflow-entering task has an intake batch on creation; an assembly "
    "exception adds a persistent assembly exclusion.\n\n"
    + decision_84.BATCH_FORMATION_SEMANTIC_BLOCK
    + "\n\n**The workflow is fixed once:** fixed.\n\n"
)
work_model = work_model[:batch_start] + batch_section + work_model[batch_end:]
parent_start = work_model.index("### Parent and child tasks")
parent_end = work_model.index(
    "### A recurring task is one live instance, and its completion creates the next"
)
parent_section = (
    "### Parent and child tasks\n\n"
    + decision_84.AGGREGATE_PARENT_MODEL_SECTION
    + "\n\n"
)
CORPUS["work_model.md"] = (
    work_model[:parent_start] + parent_section + work_model[parent_end:]
)

CORPUS["scenarios.md"] = (
    "## (f) A parent task with children in independent batches\n\n"
    + decision_84.AGGREGATE_PARENT_SCENARIO_SECTION
    + "\n\n## (g) An operator-only task, claimed by the operator-facing agent\n\n"
    + "Operator-only scenario.\n\n"
    + "## (j) A task created, routed by intake, and entering its successor\n"
    + "Every workflow-entering task has its intake batch at creation; an assembling "
    + "task is the ruled exception only in carrying a persistent assembly exclusion, "
    + "and only the resolved `pm` step owner claims.\n"
    + "C[task and intake batch created atomically] --> I[intake batch: unrouted with "
    + "no route verdict]\nF -.->|FOLLOWS| I\n"
    + "## What the scenarios do not show\n"
)


def write_corpus(root: Path) -> None:
    fdir = root / "docs" / "foundation"
    fdir.mkdir(parents=True)
    for name, text in CORPUS.items():
        if name in {
            "conformance_suite.md",
            "data_model.md",
            "scenarios.md",
            "vocabulary.md",
            "work_model.md",
            "workflows.md",
        }:
            text = (REPO_ROOT / "docs" / "foundation" / name).read_text(
                encoding="utf-8"
            )
        (fdir / name).write_text(text, encoding="utf-8")


def mutate(tmp_path: Path, name: str, old: str, new: str) -> list[str]:
    write_corpus(tmp_path)
    path = tmp_path / "docs" / "foundation" / name
    path.write_text(path.read_text().replace(old, new), encoding="utf-8")
    return decision_84.check(tmp_path)


def mutate_real_corpus(tmp_path: Path, name: str, old: str, new: str) -> list[str]:
    fdir = tmp_path / "docs" / "foundation"
    fdir.mkdir(parents=True)
    for corpus_name in CORPUS:
        shutil.copy2(REPO_ROOT / "docs" / "foundation" / corpus_name, fdir)
    path = fdir / name
    text = path.read_text(encoding="utf-8")
    assert text.count(old) == 1
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return decision_84.check(tmp_path)


def mutate_real_corpus_text(
    tmp_path: Path, name: str, transform: Callable[[str], str]
) -> list[str]:
    fdir = tmp_path / "docs" / "foundation"
    fdir.mkdir(parents=True)
    for corpus_name in CORPUS:
        shutil.copy2(REPO_ROOT / "docs" / "foundation" / corpus_name, fdir)
    path = fdir / name
    before = path.read_text(encoding="utf-8")
    after = transform(before)
    assert after != before
    path.write_text(after, encoding="utf-8")
    return decision_84.check(tmp_path)


def mutate_real_corpus_normalized(
    tmp_path: Path, name: str, old: str, new: str
) -> list[str]:
    def transform(text: str) -> str:
        pattern = re.compile(r"\s+".join(re.escape(part) for part in old.split()))
        matches = list(pattern.finditer(text))
        assert len(matches) == 1
        match = matches[0]
        return text[: match.start()] + new + text[match.end() :]

    return mutate_real_corpus_text(tmp_path, name, transform)


def test_complete_carry_passes(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    assert decision_84.check(tmp_path) == []


def test_lease_only_mutant_fails(tmp_path: Path) -> None:
    problems = mutate(
        tmp_path, "conformance.md", "persistent assembly exclusion", "held lease"
    )
    assert any("register" in problem for problem in problems)


def test_creator_ownership_mutant_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus_normalized(
        tmp_path, "work_model.md", "Creation grants no lease", "creator gets lease"
    )
    assert any("model" in problem for problem in problems)


def test_exact_creator_authority_clause_mutant_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus_normalized(
        tmp_path,
        "work_model.md",
        "The creating principal receives no `classify` lease by being the creator.",
        "The creator receives the `classify` lease automatically.",
    )
    assert any("creator-authority-model" in problem for problem in problems)


def test_exact_pm_only_claim_clause_mutant_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus_normalized(
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
    problems = mutate_real_corpus_normalized(
        tmp_path,
        "data_model.md",
        "whether the creator-time assembly finding is a persistent assembly exclusion",
        "whether the creator-time assembly finding is an ordinary hold",
    )
    assert any("finding-schema" in problem for problem in problems)


def test_workflow_assembly_mutant_fails(tmp_path: Path) -> None:
    problems = mutate(
        tmp_path, "workflows.md", "Decision 84's assembly exception", "every task"
    )
    assert any("workflow-exception" in problem for problem in problems)


def test_ordinary_task_without_intake_batch_at_creation_mutant_fails(
    tmp_path: Path,
) -> None:
    problems = mutate_real_corpus_normalized(
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
        "C[task + intake batch + ADDRESSED_BY admitted atomically] --> I[intake batch: unrouted while no closing route verdict]",
        "C[task created] --> U{intake batch exists?}\n"
        "U -->|no: unrouted by that fact| I[task enters intake; batch record opens]",
    )
    assert any("universal-entry" in problem for problem in problems)


def test_aggregate_parent_forced_into_intake_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus_normalized(
        tmp_path,
        "work_model.md",
        "An **aggregate parent task is not claimable, never enters a workflow, and has no intake batch or `ADDRESSED_BY` edge** — it is a grouping, and a batch carries tasks that are executed, which an aggregate parent never is.",
        "An aggregate parent task enters intake and receives an intake batch.",
    )
    assert any("aggregate-parent" in problem for problem in problems)


def test_intake_atomic_entry_is_scoped_to_intake_workflow(tmp_path: Path) -> None:
    problems = mutate_real_corpus_normalized(
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


@pytest.mark.parametrize(
    "name, old, new, label",
    (
        (
            "workflows.md",
            "(`work_model.md#intake-is-every-tasks-first-workflow`).\n\n**Steps**",
            "(`work_model.md#intake-is-every-tasks-first-workflow`).\n\n"
            "**Steps**\n\nDespite the entry condition, a task may be published "
            "before its intake batch or `ADDRESSED_BY` edge exists.",
            "intake-workflow-atomic-entry",
        ),
        (
            "work_model.md",
            "**A successor batch's tasks are the tasks the closing verdict carried",
            "**A successor batch's tasks also establish that an intake batch may "
            "require a predecessor verdict; the tasks the closing verdict carried",
            "batch-opening-model",
        ),
        (
            "work_model.md",
            "**A task's one `PART_OF` edge targets its parent task or a planning record",
            "**A task's one `PART_OF` edge targets its parent task or a planning "
            "record; after publication an aggregate parent may become claimable, "
            "enter a workflow, and receive an intake batch",
            "aggregate-parent-model",
        ),
        (
            "scenarios.md",
            "```mermaid\nflowchart TD\n    P[aggregate parent: not claimable, no "
            "intake batch, never in a workflow]",
            "```mermaid\nflowchart TD\n    P[aggregate parent becomes claimable and "
            "enters intake after publication]\n    P -->|ADDRESSED_BY| PB[aggregate "
            "parent batch]",
            "aggregate-parent-scenario",
        ),
    ),
)
def test_real_contradiction_at_protected_end_boundary_fails(
    tmp_path: Path, name: str, old: str, new: str, label: str
) -> None:
    problems = mutate_real_corpus(tmp_path, name, old, new)
    assert any(label in problem for problem in problems)


@pytest.mark.parametrize(
    "name, old, new, label",
    (
        (
            "workflows.md",
            "**Entry condition:** every workflow-entering task",
            "Despite the following entry condition, a task may be published before "
            "its intake batch exists.\n\n**Entry condition:** every workflow-entering task",
            "intake-workflow-atomic-entry",
        ),
        (
            "work_model.md",
            "The consequence worth naming has two forms, not one.",
            "Despite the following rule, an intake batch may require a predecessor "
            "verdict.\n\nThe consequence worth naming has two forms, not one.",
            "batch-opening-model",
        ),
        (
            "work_model.md",
            "Children `PART_OF` an aggregate parent",
            "Despite the following exception, an aggregate parent may enter a "
            "workflow.\n\nChildren `PART_OF` an aggregate parent",
            "aggregate-parent-model",
        ),
        (
            "scenarios.md",
            "A parent task is created as the grouping",
            "Despite the following scenario, an aggregate parent may enter a "
            "workflow.\n\nA parent task is created as the grouping",
            "aggregate-parent-scenario",
        ),
    ),
)
def test_real_contradiction_at_protected_start_boundary_fails(
    tmp_path: Path, name: str, old: str, new: str, label: str
) -> None:
    problems = mutate_real_corpus(tmp_path, name, old, new)
    assert any(label in problem for problem in problems)


def test_real_canonical_decoy_before_contradictory_intake_fails(tmp_path: Path) -> None:
    def transform(text: str) -> str:
        marker = "**Entry condition:**"
        position = text.index(marker, text.index("## intake"))
        decoy = decision_84.INTAKE_ENTRY_PARAGRAPH + "\n\n**Steps**\n\n"
        text = text[:position] + decoy + text[position:]
        old = "batch and `ADDRESSED_BY` edge admitted atomically at\ncreation."
        position = text.rfind(old)
        return text[:position] + text[position:].replace(
            old,
            "batch and `ADDRESSED_BY` edge admitted later after\ncreation.",
            1,
        )

    problems = mutate_real_corpus_text(tmp_path, "workflows.md", transform)
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


def test_real_canonical_decoy_before_contradictory_batch_opening_fails(
    tmp_path: Path,
) -> None:
    def transform(text: str) -> str:
        marker = "The consequence worth naming has two forms, not one."
        position = text.index(marker)
        decoy = (
            decision_84.BATCH_OPENING_PARAGRAPH
            + "\n\n**A successor batch's tasks are fixed by its verdict.**\n\n"
        )
        text = text[:position] + decoy + text[position:]
        old = "the workflow-entering task, without a predecessor verdict."
        position = text.rfind(old)
        return text[:position] + text[position:].replace(
            old,
            "the workflow-entering task, only after a predecessor verdict.",
            1,
        )

    problems = mutate_real_corpus_text(tmp_path, "work_model.md", transform)
    assert any("batch-opening-model" in problem for problem in problems)


def test_real_canonical_decoy_before_contradictory_parent_model_fails(
    tmp_path: Path,
) -> None:
    def transform(text: str) -> str:
        marker = "Children `PART_OF` an aggregate parent"
        position = text.index(marker)
        decoy = (
            decision_84.AGGREGATE_PARENT_MODEL_PARAGRAPH
            + "\n\n**A task's one `PART_OF` edge targets its parent task or a "
            "planning record.**\n\n"
        )
        text = text[:position] + decoy + text[position:]
        old = (
            "is not claimable,\nnever enters a workflow, and has no intake batch "
            "or `ADDRESSED_BY` edge**"
        )
        position = text.rfind(old)
        return text[:position] + text[position:].replace(
            old,
            "becomes claimable after publication,\nenters a workflow, and receives "
            "an intake batch and `ADDRESSED_BY` edge**",
            1,
        )

    problems = mutate_real_corpus_text(tmp_path, "work_model.md", transform)
    assert any("aggregate-parent-model" in problem for problem in problems)


def test_real_canonical_decoy_before_contradictory_parent_scenario_fails(
    tmp_path: Path,
) -> None:
    def transform(text: str) -> str:
        marker = "A parent task is created as the grouping"
        position = text.index(marker)
        decoy = (
            decision_84.AGGREGATE_PARENT_SCENARIO_PARAGRAPH
            + "\n\n```mermaid\nflowchart TD\n```\n\n"
        )
        text = text[:position] + decoy + text[position:]
        old = (
            "is not claimable, never enters a workflow,\nand has no intake batch "
            "or `ADDRESSED_BY` edge.**"
        )
        position = text.rfind(old)
        return text[:position] + text[position:].replace(
            old,
            "becomes claimable after publication, enters a workflow,\nand receives "
            "an intake batch and `ADDRESSED_BY` edge.**",
            1,
        )

    problems = mutate_real_corpus_text(tmp_path, "scenarios.md", transform)
    assert any("aggregate-parent-scenario" in problem for problem in problems)


def test_real_duplicate_intake_heading_with_contradiction_fails(tmp_path: Path) -> None:
    def transform(text: str) -> str:
        intake = text.index("## intake")
        feature = text.index("## feature", intake)
        duplicate = (
            "## intake\n\n**Entry condition:** a workflow-entering task may be "
            "published before its intake batch and `ADDRESSED_BY` edge exist.\n\n"
            "**Steps**\n\n"
        )
        return text[:feature] + duplicate + text[feature:]

    problems = mutate_real_corpus_text(tmp_path, "workflows.md", transform)
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


def test_real_duplicate_parent_scenario_heading_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "scenarios.md",
        "## (g) An operator-only task, claimed by the operator-facing agent",
        "## (f) A parent task with children in independent batches\n\n"
        "An aggregate parent may enter a workflow.\n\n"
        "## (g) An operator-only task, claimed by the operator-facing agent",
    )
    assert any("aggregate-parent-scenario" in problem for problem in problems)


def test_real_html_comment_decoy_cannot_mask_live_intake_mutant(tmp_path: Path) -> None:
    def transform(text: str) -> str:
        marker = "**Entry condition:**"
        position = text.index(marker, text.index("## intake"))
        decoy = "<!-- " + decision_84.INTAKE_ENTRY_PARAGRAPH + "\n\n**Steps** -->\n\n"
        text = text[:position] + decoy + text[position:]
        old = "batch and `ADDRESSED_BY` edge admitted atomically at\ncreation."
        return text.replace(
            old,
            "batch and `ADDRESSED_BY` edge admitted later after\ncreation.",
            1,
        )

    problems = mutate_real_corpus_text(tmp_path, "workflows.md", transform)
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


def test_real_reordered_intake_markers_fail(tmp_path: Path) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "workflows.md",
        "**Purpose:** turn a created task into a routed one:",
        "**Steps**\n\n**Purpose:** turn a created task into a routed one:",
    )
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


def test_real_missing_intake_end_marker_fails(tmp_path: Path) -> None:
    def transform(text: str) -> str:
        start = text.index("## intake")
        end = text.index("## feature", start)
        section = text[start:end]
        old = (
            "| # | Step | Step owner (role) | Required | Parallel / join | Closes on |"
        )
        assert section.count(old) == 1
        return (
            text[:start]
            + section.replace(
                old, "| steps table deliberately missing its canonical header |", 1
            )
            + text[end:]
        )

    problems = mutate_real_corpus_text(tmp_path, "workflows.md", transform)
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


def test_real_scenario_corruption_cannot_be_masked_by_later_decoy(
    tmp_path: Path,
) -> None:
    def transform(text: str) -> str:
        section_start = text.index(
            "## (f) A parent task with children in independent batches"
        )
        prose_start = text.index(
            "A parent task is created as the grouping", section_start
        )
        diagram_start = text.index("```mermaid", prose_start)
        corrupted = (
            "An aggregate parent becomes claimable, enters a workflow, and receives "
            "an intake batch and `ADDRESSED_BY` edge.\n\n"
        )
        text = text[:prose_start] + corrupted + text[diagram_start:]
        return (
            text
            + "\n\n"
            + decision_84.AGGREGATE_PARENT_SCENARIO_PARAGRAPH
            + "\n\n```mermaid\nflowchart TD\n```\n"
        )

    problems = mutate_real_corpus_text(tmp_path, "scenarios.md", transform)
    assert any("aggregate-parent-scenario" in problem for problem in problems)


def test_real_missing_parent_scenario_end_heading_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "scenarios.md",
        "## (g) An operator-only task, claimed by the operator-facing agent",
        "The operator-only scenario heading is missing.",
    )
    assert any("aggregate-parent-scenario" in problem for problem in problems)


def test_real_intake_contradiction_after_table_header_fails(tmp_path: Path) -> None:
    def transform(text: str) -> str:
        start = text.index("## intake")
        marker = (
            "| # | Step | Step owner (role) | Required | Parallel / join | Closes on |"
        )
        position = text.index(marker, start) + len(marker)
        contradiction = (
            "\nDespite the entry rule, a task may be published before its intake "
            "batch exists."
        )
        return text[:position] + contradiction + text[position:]

    problems = mutate_real_corpus_text(tmp_path, "workflows.md", transform)
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


def test_real_batch_contradiction_before_block_start_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "work_model.md",
        "**A batch comes into existence at one of two moments",
        "Despite the following rule, an intake batch may require a predecessor "
        "verdict.\n\n**A batch comes into existence at one of two moments",
    )
    assert any("batch-opening-model" in problem for problem in problems)


def test_real_batch_contradiction_after_block_end_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "work_model.md",
        "**The workflow is fixed once:",
        "**The workflow is fixed once:** Despite the preceding rule, an intake "
        "batch may require a predecessor verdict. **",
    )
    assert any("batch-opening-model" in problem for problem in problems)


def test_real_batch_mermaid_predecessor_inversion_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "work_model.md",
        'IB["its intake batch opens without a predecessor verdict"]',
        'IB["its intake batch opens only after a predecessor verdict; not without '
        'a predecessor verdict"]',
    )
    assert any("batch-opening-model" in problem for problem in problems)


def test_real_intake_comment_mutation_inside_fingerprint_fails(
    tmp_path: Path,
) -> None:
    def transform(text: str) -> str:
        start = text.index("## intake")
        marker = "or to the operator.\n\n"
        position = text.index(marker, start) + len(marker)
        return (
            text[:position]
            + "<!-- decision-84 protected section changed -->\n\n"
            + text[position:]
        )

    problems = mutate_real_corpus_text(tmp_path, "workflows.md", transform)
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


def test_real_intake_inserted_peer_heading_cannot_truncate_fingerprint(
    tmp_path: Path,
) -> None:
    def transform(text: str) -> str:
        position = text.index("## feature", text.index("## intake"))
        contradiction = (
            "## Intake exceptions\n\n"
            "Despite the intake rule, a workflow-entering task may be published "
            "before its intake batch or `ADDRESSED_BY` edge exists.\n\n"
        )
        return text[:position] + contradiction + text[position:]

    problems = mutate_real_corpus_text(tmp_path, "workflows.md", transform)
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


def test_real_batch_inserted_peer_heading_cannot_truncate_fingerprint(
    tmp_path: Path,
) -> None:
    def transform(text: str) -> str:
        start = text.index("### How a batch is formed, and what chooses its workflow")
        position = text.index(
            "### A batch may hold on a condition discovered mid-flight", start
        )
        contradiction = (
            "### Batch-opening exceptions\n\n"
            "Despite the formation rule, an intake batch opens only after a "
            "predecessor verdict.\n\n"
        )
        return text[:position] + contradiction + text[position:]

    problems = mutate_real_corpus_text(tmp_path, "work_model.md", transform)
    assert any("batch-opening-model" in problem for problem in problems)


def test_owning_heading_section_keeps_child_headings_in_its_body() -> None:
    text = "## protected\nbody\n### child\nchild body\n## expected end\nafter\n"
    assert (
        decision_84._owning_heading_section(text, "protected", "expected end")
        == "body\n### child\nchild body\n"
    )


def test_real_comment_fence_precedence_cannot_expose_hidden_intake(
    tmp_path: Path,
) -> None:
    def transform(text: str) -> str:
        position = text.index("## intake")
        prefix = "```markdown\n<!--\n```\n-->\n```\n"
        return text[:position] + prefix + text[position:]

    problems = mutate_real_corpus_text(tmp_path, "workflows.md", transform)
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


@pytest.mark.parametrize("tag", ("pre", "SCRIPT", "style", "textarea"))
def test_real_raw_html_block_cannot_hide_intake(tmp_path: Path, tag: str) -> None:
    def transform(text: str) -> str:
        start = text.index("## intake\n")
        end = text.index("\n", text.index("## feature\n", start)) + 1
        return (
            text[:start]
            + f"<{tag}>\n"
            + text[start:end]
            + f"</{tag.lower()}>\n"
            + text[end:]
        )

    problems = mutate_real_corpus_text(tmp_path, "workflows.md", transform)
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


@pytest.mark.parametrize(
    "name,marker,label",
    (
        ("conformance.md", "| 84 |", "register"),
        ("conformance_suite.md", "| WM-14a |", "wm-14a"),
    ),
)
def test_real_raw_html_block_cannot_hide_table_row(
    tmp_path: Path, name: str, marker: str, label: str
) -> None:
    def transform(text: str) -> str:
        start = text.index(marker)
        end = text.index("\n", start) + 1
        return text[:start] + "<pre>\n" + text[start:end] + "</pre>\n" + text[end:]

    problems = mutate_real_corpus_text(tmp_path, name, transform)
    assert any(label in problem for problem in problems)


def test_closed_raw_html_block_before_intake_keeps_intake_active(
    tmp_path: Path,
) -> None:
    def transform(text: str) -> str:
        position = text.index("## intake")
        prefix = "<pre>\n## hidden heading\n</pre>\n\n"
        return text[:position] + prefix + text[position:]

    assert mutate_real_corpus_text(tmp_path, "workflows.md", transform) == []


@pytest.mark.parametrize(
    "name,marker,label",
    (
        ("conformance.md", "| 84 |", "register"),
        ("conformance_suite.md", "| WM-13 |", "wm-13-atomic-entry"),
    ),
)
def test_real_unclosed_comment_cannot_expose_hidden_table_row(
    tmp_path: Path, name: str, marker: str, label: str
) -> None:
    def transform(text: str) -> str:
        position = text.index(marker)
        return text[:position] + "<!--\n" + text[position:]

    problems = mutate_real_corpus_text(tmp_path, name, transform)
    assert any(label in problem for problem in problems)


def test_real_creator_auto_lease_appended_contradiction_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus_normalized(
        tmp_path,
        "work_model.md",
        "The creating principal receives no `classify` lease by being the creator.",
        "The creating principal receives no `classify` lease by being the creator. "
        "Despite that sentence, the creator automatically receives the `classify` lease.",
    )
    assert any("creator-authority-model" in problem for problem in problems)


def test_real_non_pm_claim_appended_contradiction_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus_normalized(
        tmp_path,
        "work_model.md",
        "assembly exclusion exposes its open `classify` step only to a principal that resolves as the declaration's `pm` step owner",
        "assembly exclusion exposes its open `classify` step only to a principal that "
        "resolves as the declaration's `pm` step owner; despite that sentence, any "
        "principal may claim `classify`",
    )
    assert any("claim-model" in problem for problem in problems)


def test_real_lease_lapse_deletes_exclusion_appended_contradiction_fails(
    tmp_path: Path,
) -> None:
    problems = mutate_real_corpus_normalized(
        tmp_path,
        "work_model.md",
        "the finding and its persistent assembly exclusion survive the lease lapse",
        "the finding and its persistent assembly exclusion survive the lease lapse; "
        "despite that sentence, lease lapse deletes the assembly finding and exclusion",
    )
    assert any("hold-model" in problem for problem in problems)


def test_real_finding_schema_lease_lapse_contradiction_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus_normalized(
        tmp_path,
        "data_model.md",
        "which survives lease lapse and exposes only `classify` to the declaration-resolved `pm` step owner",
        "which survives lease lapse and exposes only `classify` to the declaration-resolved "
        "`pm` step owner; despite that sentence, lease lapse deletes the assembly finding",
    )
    assert any("finding-schema" in problem for problem in problems)


def test_real_wm14a_non_pm_permission_contradiction_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus_normalized(
        tmp_path,
        "conformance_suite.md",
        "only the declaration-resolved `pm` step owner may claim it",
        "only the declaration-resolved `pm` step owner may claim it; despite that "
        "sentence, the creator and any non-PM principal may claim it",
    )
    assert any("wm-14a" in problem for problem in problems)


def test_real_duplicate_scenario_heading_with_closing_hashes_fails(
    tmp_path: Path,
) -> None:
    def transform(text: str) -> str:
        duplicate = (
            "\n\n## (f) A parent task with children in independent batches ##\n\n"
            "The aggregate parent enters intake and receives `ADDRESSED_BY`.\n\n"
        )
        return text + duplicate

    problems = mutate_real_corpus_text(tmp_path, "scenarios.md", transform)
    assert any("aggregate-parent-scenario" in problem for problem in problems)


def test_real_batch_fingerprint_preserves_paragraph_boundary(tmp_path: Path) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "work_model.md",
        "must have matched.\n\n**A successor batch's tasks",
        "must have matched.\n**A successor batch's tasks",
    )
    assert any("batch-opening-model" in problem for problem in problems)


def test_real_batch_fingerprint_preserves_identifier_case(tmp_path: Path) -> None:
    def transform(text: str) -> str:
        start = text.index("### How a batch is formed, and what chooses its workflow")
        end = text.index(
            "### A batch may hold on a condition discovered mid-flight", start
        )
        section = text[start:end]
        assert section.count("`FOLLOWS`") >= 1
        return text[:start] + section.replace("`FOLLOWS`", "`follows`", 1) + text[end:]

    problems = mutate_real_corpus_text(tmp_path, "work_model.md", transform)
    assert any("batch-opening-model" in problem for problem in problems)


@pytest.mark.parametrize(
    "decoy",
    (
        "<!--\n## intake\n## feature\n-->",
        "```markdown\n## intake\n## feature\n```",
    ),
)
def test_real_inactive_heading_decoys_are_ignored(tmp_path: Path, decoy: str) -> None:
    def transform(text: str) -> str:
        position = text.index("## intake")
        return text[:position] + decoy + "\n\n" + text[position:]

    assert mutate_real_corpus_text(tmp_path, "workflows.md", transform) == []


@pytest.mark.parametrize(
    "opening,pseudo_closer",
    (
        ("```markdown", "```not-a-closing-fence"),
        ("````markdown", "```"),
        ("~~~~markdown", "~~~"),
        ("```markdown", "~~~"),
        ("~~~markdown", "```"),
        ("~~~markdown", "~~~~ trailing text"),
        ("```markdown", "    ```"),
        ("~~~markdown", "    ~~~"),
    ),
)
def test_real_pseudo_fence_closer_cannot_expose_hidden_intake(
    tmp_path: Path, opening: str, pseudo_closer: str
) -> None:
    def transform(text: str) -> str:
        position = text.index("## intake")
        hidden = opening + "\n" + pseudo_closer + "\n"
        return text[:position] + hidden + text[position:]

    problems = mutate_real_corpus_text(tmp_path, "workflows.md", transform)
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


@pytest.mark.parametrize(
    "opening,longer_closer",
    (
        ("```markdown", "````"),
        ("~~~markdown", "~~~~"),
    ),
)
def test_real_longer_fence_closer_keeps_following_intake_active(
    tmp_path: Path, opening: str, longer_closer: str
) -> None:
    def transform(text: str) -> str:
        position = text.index("## intake")
        closed = opening + "\nignored fenced prose\n" + longer_closer + "\n"
        return text[:position] + closed + text[position:]

    assert mutate_real_corpus_text(tmp_path, "workflows.md", transform) == []


def test_real_backtick_in_backtick_info_is_not_a_fence_opener(
    tmp_path: Path,
) -> None:
    def transform(text: str) -> str:
        position = text.index("## intake")
        pseudo_opener = "```language`not-valid-info\n"
        return text[:position] + pseudo_opener + text[position:]

    assert mutate_real_corpus_text(tmp_path, "workflows.md", transform) == []


@pytest.mark.parametrize(
    "opening,closer",
    (
        ("```markdown valid-info", "```"),
        ("~~~language `backticks-are-valid-here`", "~~~"),
    ),
)
def test_real_valid_fence_info_strings_remain_inert(
    tmp_path: Path, opening: str, closer: str
) -> None:
    def transform(text: str) -> str:
        position = text.index("## intake")
        fenced = opening + "\n## hidden heading\n" + closer + "\n"
        return text[:position] + fenced + text[position:]

    assert mutate_real_corpus_text(tmp_path, "workflows.md", transform) == []


def test_real_intake_heading_with_trailing_whitespace_remains_active(
    tmp_path: Path,
) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "workflows.md",
        "## intake\n",
        "## intake\t  \n",
    )
    assert problems == []


@pytest.mark.parametrize(
    "line",
    (
        "## intake   \n",
        "## intake\t  \r\n",
        "## intake ###\t  \r\n",
    ),
)
def test_active_heading_normalizes_legal_trailing_whitespace(line: str) -> None:
    assert decision_84._active_headings(line)[0][:2] == (2, "intake")


def test_real_duplicate_intake_heading_with_trailing_whitespace_is_ambiguous(
    tmp_path: Path,
) -> None:
    def transform(text: str) -> str:
        return text + "\n## intake ##\t  \nContradictory duplicate.\n"

    problems = mutate_real_corpus_text(tmp_path, "workflows.md", transform)
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


@pytest.mark.parametrize(
    "duplicate",
    (
        "## int&#97;ke\n",
        "## in*tak*e\n",
        "## **intake**\n",
        "## _intake_\n",
        "## `intake`\n",
    ),
)
def test_real_rendered_equivalent_intake_heading_is_ambiguous(
    tmp_path: Path, duplicate: str
) -> None:
    def transform(text: str) -> str:
        return text + "\n" + duplicate + "Contradictory duplicate.\n"

    problems = mutate_real_corpus_text(tmp_path, "workflows.md", transform)
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


def test_real_malformed_emphasis_heading_is_not_canonical_intake(
    tmp_path: Path,
) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "workflows.md",
        "## intake\n",
        "## **in***take***\n",
    )
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


def test_real_commonmark_equivalent_overlapping_heading_is_ambiguous(
    tmp_path: Path,
) -> None:
    def transform(text: str) -> str:
        return text + "\n## ***in**take*\nContradictory duplicate.\n"

    problems = mutate_real_corpus_text(tmp_path, "workflows.md", transform)
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


def test_real_commonmark_distinct_overlapping_heading_is_not_canonical(
    tmp_path: Path,
) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "workflows.md",
        "## intake\n",
        "## ***in**ta**ke***\n",
    )
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


def test_real_inline_token_mixed_equivalent_heading_is_ambiguous(
    tmp_path: Path,
) -> None:
    def transform(text: str) -> str:
        return text + "\n## ***`in`**`ta`**ke***\nContradictory duplicate.\n"

    problems = mutate_real_corpus_text(tmp_path, "workflows.md", transform)
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


def test_real_inline_token_mixed_distinct_heading_is_not_canonical(
    tmp_path: Path,
) -> None:
    problems = mutate_real_corpus(
        tmp_path,
        "workflows.md",
        "## intake\n",
        "## `in`*`ta`*ke\n",
    )
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


@pytest.mark.parametrize(
    "duplicate",
    (
        "## [intake](https://example.test)\n",
        '## [intake](https://example.test "title ) extra")\n',
        "## <em>intake</em>\n",
        '## in<span data-kind="middle">tak</span>e\n',
        "## prefix-intake-suffix\n",
        "## [route](https://example.test/intake)\n",
        '## <span data-rule="intake">route</span>\n',
        "## in[ta](https://x.test)ke\n",
    ),
)
def test_real_link_or_inline_html_intake_heading_is_ambiguous(
    tmp_path: Path, duplicate: str
) -> None:
    def transform(text: str) -> str:
        return text + "\n" + duplicate + "Contradictory duplicate.\n"

    problems = mutate_real_corpus_text(tmp_path, "workflows.md", transform)
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


def test_heading_projection_covers_broad_inline_decoration_matrix() -> None:
    """Every reviewed marker/entity/code/escape mixture is fail-closed."""

    delimiter_runs = (
        ("",)
        + tuple("*" * size for size in range(1, 8))
        + tuple("_" * size for size in range(1, 8))
    )
    chunk_variants = (
        ("in", "ta", "ke"),
        ("`in`", "`ta`", "ke"),
        ("i&#110;", "t&#97;", "k&#101;"),
        ("i\\*n", "t\\_a", "k\\`e"),
        ("`in`", "t&#97;", "k\\*e"),
    )
    count = 0
    for count, (before, after_in, after_ta, after_ke) in enumerate(
        product(delimiter_runs, repeat=4),
        start=1,
    ):
        first, second, third = chunk_variants[count % len(chunk_variants)]
        source = f"{before}{first}{after_in}{second}{after_ta}{third}{after_ke}"
        assert decision_84._heading_projection(source) == "intake"

    assert count == 50_625


@pytest.mark.parametrize(
    "title",
    (
        "[intake](https://example.test/path_(nested))",
        "[intake][reference]",
        "[intake][]",
        '![intake](image.png "title")',
        "[in[ta]ke](https://example.test)",
        "<em>intake</em>",
        "<EM CLASS='focus'>intake</EM>",
        "in<span data-text='a>b'>tak</span>e",
        "in<!-- hidden -->take",
        "in</em>take",
    ),
)
def test_heading_ambiguity_covers_link_and_html_syntax(
    title: str,
) -> None:
    assert decision_84._heading_is_ambiguous(title, "intake")


@pytest.mark.parametrize(
    "title",
    (
        "prefix-intake-suffix",
        "[route](https://example.test/intake)",
        '[route](https://example.test "intake title")',
        '<span data-rule="intake">route</span>',
        "Intake exceptions",
        "[Intake exceptions](https://example.test)",
        "<https://example.test/intake>",
        "<intake@example.test>",
        "<em intake",
        "[intake](https://example.test",
    ),
)
def test_heading_ambiguity_is_monotonic_over_raw_source(title: str) -> None:
    assert decision_84._heading_is_ambiguous(title, "intake")


@pytest.mark.parametrize(
    "insertions",
    (
        (
            "[x](https://example.test)",
            "<span>x</span>",
            "&#120;",
            "`x`",
            "\\*",
        ),
        (
            "prefix",
            '<i data-key="noise">x</i>',
            "httpsxexample",
            "[noise][reference]",
            "_suffix_",
        ),
    ),
)
def test_heading_ambiguity_catches_insertions_between_every_protected_character(
    insertions: tuple[str, ...],
) -> None:
    title = (
        "".join(
            char + insertion
            for char, insertion in zip("intak", insertions, strict=True)
        )
        + "e"
    )
    assert decision_84._heading_is_ambiguous(title, "intake")


@pytest.mark.parametrize(
    "title",
    (
        "feature",
        "Triage exceptions",
        "[Triage exceptions](https://example.test)",
        "Batch-opening exceptions",
        "What the scenarios do not show",
        "intact",
        "[intact](https://example.test)",
        "<https://example.test/route>",
        "<route@example.test>",
        "<em route",
        "[route](https://example.test",
    ),
)
def test_heading_ambiguity_leaves_unrelated_titles_distinct(title: str) -> None:
    assert not decision_84._heading_is_ambiguous(title, "intake")


@pytest.mark.parametrize(
    "filename,protected",
    (
        ("workflows.md", "intake"),
        (
            "work_model.md",
            "What distinguishes a task being assembled from one intake has not reached",
        ),
        ("work_model.md", "What a claim predicate treats as claimable"),
        (
            "scenarios.md",
            "(j) A task created, routed by intake, and entering its successor",
        ),
    ),
)
def test_live_same_level_heading_corpus_has_no_projection_false_positive(
    filename: str, protected: str
) -> None:
    headings = decision_84._active_headings(
        (REPO_ROOT / "docs" / "foundation" / filename).read_text()
    )
    exact = [entry for entry in headings if entry[1] == protected]
    assert len(exact) == 1
    level = exact[0][0]
    ambiguous = [
        entry[1]
        for entry in headings
        if entry[0] == level and decision_84._heading_is_ambiguous(entry[1], protected)
    ]
    assert ambiguous == [protected]


@pytest.mark.parametrize(
    "distinct",
    (
        "## intake##\n",
        "## in\\*take\n",
        "## in_take\n",
    ),
)
def test_real_decorated_intake_heading_is_conservatively_ambiguous(
    tmp_path: Path, distinct: str
) -> None:
    def transform(text: str) -> str:
        return text + "\n" + distinct + "Distinct heading.\n"

    problems = mutate_real_corpus_text(tmp_path, "workflows.md", transform)
    assert any("intake-workflow-atomic-entry" in problem for problem in problems)


def test_real_scenario_canonical_body_inside_fence_cannot_mask_contradiction(
    tmp_path: Path,
) -> None:
    def transform(text: str) -> str:
        heading = (
            "## (j) A task created, routed by intake, and entering its successor\n"
        )
        start = text.index(heading) + len(heading)
        end = text.index("## What the scenarios do not show", start)
        original = text[start:end]
        contradiction = (
            "\nA workflow-entering task may be published before any intake batch "
            "exists, and any creator may claim it.\n\n"
        )
        return (
            text[:start]
            + contradiction
            + "~~~~markdown\n"
            + original
            + "~~~~\n\n"
            + text[end:]
        )

    problems = mutate_real_corpus_text(tmp_path, "scenarios.md", transform)
    assert any("scenario-workflow-entry" in problem for problem in problems)


def test_real_claimable_vocabulary_assembly_exclusion_mutant_fails(
    tmp_path: Path,
) -> None:
    problems = mutate_real_corpus_normalized(
        tmp_path,
        "vocabulary.md",
        "whose status is not terminal",
        "whose status is not terminal; despite the exclusion below, every assembling "
        "task remains ordinarily claimable",
    )
    assert any("claimable-vocabulary" in problem for problem in problems)


def test_real_live_partition_assembly_exclusion_mutant_fails(
    tmp_path: Path,
) -> None:
    problems = mutate_real_corpus_normalized(
        tmp_path,
        "work_model.md",
        "A task is **live** when it is [claimable](vocabulary.md#claimable) by some principal",
        "A task is **live** when it is [claimable](vocabulary.md#claimable) by some "
        "principal; despite the exclusion below, every assembling task remains live",
    )
    assert any("live-model" in problem for problem in problems)


def test_wm27_closing_verdict_claim_applied_to_intake_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus_normalized(
        tmp_path,
        "conformance_suite.md",
        "an intake batch opens at workflow-entering task creation without a predecessor verdict; every successor batch is opened only by a closing verdict",
        "every batch is opened only by a closing verdict",
    )
    assert any("wm-27" in problem for problem in problems)


def test_scenario_intake_predecessor_node_must_be_defined(tmp_path: Path) -> None:
    problems = mutate(
        tmp_path,
        "scenarios.md",
        "C[task + intake batch + ADDRESSED_BY admitted atomically] --> I[intake batch: unrouted while no closing route verdict]",
        "C[task + intake batch + ADDRESSED_BY admitted atomically] --> U[unrouted with no route verdict]",
    )
    assert any("scenario-intake-node" in problem for problem in problems)


@pytest.mark.parametrize(
    "row", ("WM-13", "WM-14", "WM-21", "WM-32b", "WM-35a", "WM-39")
)
def test_each_creation_row_without_atomic_intake_entry_fails(
    tmp_path: Path, row: str
) -> None:
    def transform(text: str) -> str:
        match = re.search(rf"^\|\s*{re.escape(row)}\s*\|.*$", text, re.M)
        assert match
        original = match.group(0)
        changed = original
        for phrase in ("at creation", "on creation", "task creation"):
            changed = changed.replace(phrase, "later")
        assert changed != original
        return text[: match.start()] + changed + text[match.end() :]

    problems = mutate_real_corpus_text(tmp_path, "conformance_suite.md", transform)
    assert any(f"universal-entry-{row.lower()}" in problem for problem in problems)


def test_wm14a_without_transfer_fails(tmp_path: Path) -> None:
    problems = mutate_real_corpus_normalized(
        tmp_path,
        "conformance_suite.md",
        "the exclusion survives lapse, crash, return, and transfer",
        "the exclusion survives renewal",
    )
    assert any("wm-14a" in problem for problem in problems)


def test_wm39_without_exception_fails(tmp_path: Path) -> None:
    def transform(text: str) -> str:
        match = re.search(r"^\|\s*WM-39\s*\|.*$", text, re.M)
        assert match
        original = match.group(0)
        assert "assembly exception" in original
        changed = original.replace("assembly exception", "all tasks lack a batch", 1)
        return text[: match.start()] + changed + text[match.end() :]

    problems = mutate_real_corpus_text(tmp_path, "conformance_suite.md", transform)
    assert any("wm-39" in problem for problem in problems)
