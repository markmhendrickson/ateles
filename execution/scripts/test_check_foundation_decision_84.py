from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_foundation_decision_84 as decision_84  # noqa: E402

CORPUS = {
    "conformance.md": "| 84 | question | pointer | dependency | **ruled**: persistent assembly exclusion survives a lapsed lease; creation grants no lease; only the `pm` step owner claims |\n",
    "work_model.md": "persistent assembly exclusion; lease lapse; creation grants no lease; the declared `pm` step owner; multi-agent assembly\n### A batch may hold on a condition discovered mid-flight\nThe assembly exception admits its creator-time finding before a step owner or held lease exists; its persistent assembly exclusion survives the lease lapse.\n### A batch may depend on a task it created\n",
    "data_model.md": "A creator-time assembly finding is a persistent assembly exclusion that survives lease lapse and exposes only the declaration-resolved `pm` owner.\n",
    "workflows.md": "decision 84's assembly exception has an intake batch and persistent hold\n",
    "scenarios.md": "an assembling task is the ruled exception; only the resolved `pm` step owner claims\n",
    "conformance_suite.md": "| WM-14a | persistent hold | lapse and transfer | creator and declaration-resolved `pm` step owner | mutant |\n| WM-31 | assembly exception creator-time finding | fixture | read | survives lapse | M |\n| WM-31a | assembly exception | lapse | read | PM-only | M |\n| WM-39 | assembly exception and ordinary creation | declared `pm` step owner | effect | red | M |\n",
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


def test_generic_hold_model_without_assembly_exception_fails(tmp_path: Path) -> None:
    problems = mutate(tmp_path, "work_model.md", "creator-time", "ordinary")
    assert any("hold-model" in problem for problem in problems)


def test_finding_schema_without_persistent_exclusion_fails(tmp_path: Path) -> None:
    problems = mutate(tmp_path, "data_model.md", "persistent assembly exclusion", "ordinary hold")
    assert any("finding-schema" in problem for problem in problems)


def test_workflow_universal_mutant_fails(tmp_path: Path) -> None:
    problems = mutate(tmp_path, "workflows.md", "decision 84's assembly exception", "every task")
    assert any("workflow-exception" in problem for problem in problems)


def test_wm14a_without_transfer_fails(tmp_path: Path) -> None:
    problems = mutate(tmp_path, "conformance_suite.md", "lapse and transfer", "renewal")
    assert any("wm-14a" in problem for problem in problems)


def test_wm39_without_exception_fails(tmp_path: Path) -> None:
    problems = mutate(tmp_path, "conformance_suite.md", "assembly exception", "all tasks lack a batch")
    assert any("wm-39" in problem for problem in problems)
