from __future__ import annotations

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_foundation_decisions_99_107_109 as rulings  # noqa: E402

FILES = {
    "conformance.md": "| 99 | q | p | d | **ruled** exactly one instance; controlling instance; naming none fails closed |\n| 107 | q | p | d | **ruled** |\n| 108 | q | p | d | **ruled** |\n| 109 | q | p | d | **ruled** |\n",
    "planning_model.md": "every planning record belongs to exactly one instance; controlling instance; naming several instances and no controlling one fails closed\n",
    "authority_model.md": "traversal-only and non-presentable; acts_as is denied as a non-presentable kind; agent-to-agent acts-as write is refused and uses `delegation_edge`; operator-sourced acts-as write is also refused\n",
    "data_model.md": "traversal-only and non-presentable; agent → agent acts-as is refused and uses `delegation_edge`; operator-sourced acts-as is refused\n",
    "conformance_suite.md": "| PM-13 | one instance; controlling instance; none means refusal | fixture | create | chooses an instance by order | M |\n| AU-29 | rules | present `credential_kind: acts_as`; write A → B acts-as; write O → P acts-as; use `delegation_edge` | read | removing a check turns this row red | M |\n",
}


def write_corpus(root: Path) -> None:
    fdir = root / "docs" / "foundation"
    fdir.mkdir(parents=True)
    for name, text in FILES.items():
        (fdir / name).write_text(text, encoding="utf-8")


def mutate(tmp_path: Path, name: str, old: str, new: str) -> list[str]:
    write_corpus(tmp_path)
    path = tmp_path / "docs" / "foundation" / name
    path.write_text(path.read_text().replace(old, new), encoding="utf-8")
    return rulings.check(tmp_path)


def test_complete_carry_passes(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    assert rulings.check(tmp_path) == []


def test_decision_99_instance_ownership_mutant_fails(tmp_path: Path) -> None:
    problems = mutate(tmp_path, "planning_model.md", "exactly one instance", "somewhere")
    assert any("99-model" in problem for problem in problems)


def test_decision_99_controlling_instance_mutant_fails(tmp_path: Path) -> None:
    problems = mutate(tmp_path, "conformance_suite.md", "controlling instance", "first available")
    assert any("99-pm13" in problem for problem in problems)


def test_decision_107_presentation_mutant_fails(tmp_path: Path) -> None:
    problems = mutate(tmp_path, "authority_model.md", "denied as a non-presentable kind", "admitted")
    assert any("107-109-authority" in problem for problem in problems)


def test_decision_108_agent_endpoint_mutant_fails(tmp_path: Path) -> None:
    problems = mutate(tmp_path, "authority_model.md", "agent-to-agent acts-as write is refused", "agent-to-agent acts-as write is admitted")
    assert any("107-109-authority" in problem for problem in problems)


def test_decision_108_delegation_alternative_mutant_fails(tmp_path: Path) -> None:
    problems = mutate(tmp_path, "data_model.md", "`delegation_edge`", "nothing")
    assert any("107-109-data-model" in problem for problem in problems)


def test_decision_109_operator_source_mutant_fails(tmp_path: Path) -> None:
    problems = mutate(tmp_path, "authority_model.md", "operator-sourced acts-as write is also refused", "operator-sourced acts-as write is admitted")
    assert any("107-109-authority" in problem for problem in problems)
