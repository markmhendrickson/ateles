from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_foundation_decision_114 as decision_114  # noqa: E402


CONFORMANCE_RULED = """\
# Conformance

## The register of open design decisions

| # | Question | Pointer | Dependencies | Status |
|---|---|---|---|---|
| 114 | where a rule that binds every agent's behaviour lives | `migration.md#gaps-and-contradictions-the-mapping-exposed` | decision 31 | **ruled** (2026-09-25): `agent_policy` is the home, tied to its agent by a `GOVERNS` edge |
"""

CONFORMANCE_OPEN = CONFORMANCE_RULED.replace(
    "**ruled** (2026-09-25): `agent_policy` is the home, tied to its agent by a `GOVERNS` edge",
    "**open** (2026-09-10): three candidates, none taken yet",
)

# The ruled shape: the concepts-table row's edges column names GOVERNS -> agent,
# and the row states scope/agent_sub is superseded by the edge.
DATA_MODEL_RULED = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| task | `task` | `status` | `ADDRESSED_BY` -> batch | claimable | `step_status` | the lease holder |
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (the field `GOVERNS` supersedes; `scope`/`agent_sub` is read nowhere once the edge resolves, decision 114) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal, decision 114); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section that must not be scanned as part of Concepts.
"""

# The pre-114 legacy shape: scope/agent_sub stated as the live mechanism, no
# GOVERNS edge anywhere in the row, no superseded language. This is what the
# corpus looked like before decision 114 was ruled (and is what the checker
# must catch if it survives past ruling).
DATA_MODEL_LEGACY = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| task | `task` | `status` | `ADDRESSED_BY` -> batch | claimable | `step_status` | the lease holder |
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (closed: global, swarm, or agent; where the scope is agent the target is `agent_sub`); `agent_sub` (the field that scopes a rule to an agent) | `SUPERSEDES` -> `agent_policy`; `REFERS_TO` <- finding | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section that must not be scanned as part of Concepts.
"""

# Ruled, GOVERNS edge present, but no superseded language for scope/agent_sub.
DATA_MODEL_GOVERNS_NO_SUPERSEDE = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` | `GOVERNS` -> `agent`; `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""


def write_corpus(
    root: Path,
    conformance: str = CONFORMANCE_RULED,
    data_model: str = DATA_MODEL_RULED,
) -> None:
    fdir = root / "docs" / "foundation"
    fdir.mkdir(parents=True)
    (fdir / "conformance.md").write_text(conformance, encoding="utf-8")
    (fdir / "data_model.md").write_text(data_model, encoding="utf-8")


# --- Green: the ruled corpus passes ------------------------------------------


def test_passes_on_ruled_corpus_with_governs_edge_and_superseded_language(
    tmp_path: Path,
) -> None:
    write_corpus(tmp_path)

    assert decision_114.check(tmp_path) == []


def test_noop_shape_when_register_row_still_open(tmp_path: Path) -> None:
    """Before the row rules, nothing downstream is required yet — mirrors the
    decision-101/117 no-op-before-ruled pattern, so the legacy data_model row
    pre-ruling is not flagged."""
    write_corpus(tmp_path, conformance=CONFORMANCE_OPEN, data_model=DATA_MODEL_LEGACY)

    assert decision_114.check(tmp_path) == []


# --- Red: prove the checker actually fails on the defects it exists for -----


def test_fails_when_register_row_114_absent(tmp_path: Path) -> None:
    conformance_without_row = "\n".join(
        line
        for line in CONFORMANCE_RULED.splitlines()
        if not line.startswith("| 114 |")
    )
    write_corpus(tmp_path, conformance=conformance_without_row)

    problems = decision_114.check(tmp_path)

    assert len(problems) == 1
    assert "decision-114-register" in problems[0]
    assert 'no register row beginning "| 114 |"' in problems[0]


def test_fails_on_legacy_scope_agent_sub_only_row_once_ruled(tmp_path: Path) -> None:
    """The exact scenario this task's own audit found live on main: register
    ruled, but data_model.md#concepts still describes the pre-114 shape —
    scope/agent_sub as the mechanism, no GOVERNS edge, no superseded note."""
    write_corpus(tmp_path, data_model=DATA_MODEL_LEGACY)

    problems = decision_114.check(tmp_path)

    assert problems
    assert any("decision-114-data-model" in p and "GOVERNS" in p for p in problems)
    assert any("decision-114-data-model" in p and "superseded" in p for p in problems)


def test_fails_when_governs_edge_present_but_no_superseded_language(
    tmp_path: Path,
) -> None:
    write_corpus(tmp_path, data_model=DATA_MODEL_GOVERNS_NO_SUPERSEDE)

    problems = decision_114.check(tmp_path)

    assert any(
        "decision-114-data-model" in p and "superseded" in p for p in problems
    )
    assert not any("GOVERNS" in p and "missing" in p for p in problems)


DATA_MODEL_SUPERSEDED_NO_GOVERNS_EDGE = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded by traversal for an agent-specific rule; `scope`/`agent_sub` is read nowhere once the edge resolves, decision 114) | `SUPERSEDES` -> `agent_policy`; `REFERS_TO` <- finding | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""


def test_fails_when_governs_edge_missing_but_superseded_language_present(
    tmp_path: Path,
) -> None:
    data_model_no_governs = DATA_MODEL_SUPERSEDED_NO_GOVERNS_EDGE
    assert "GOVERNS" not in data_model_no_governs
    write_corpus(tmp_path, data_model=data_model_no_governs)

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "GOVERNS" in p for p in problems)


def test_fails_when_concepts_row_absent_while_ruled(tmp_path: Path) -> None:
    data_model_no_row = "\n".join(
        line
        for line in DATA_MODEL_RULED.splitlines()
        if not line.startswith("| agent behavioural rule |")
    )
    write_corpus(tmp_path, data_model=data_model_no_row)

    problems = decision_114.check(tmp_path)

    assert len(problems) == 1
    assert "decision-114-data-model" in problems[0]
    assert "no concepts-table row" in problems[0]


def test_raises_when_conformance_file_is_absent(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    (tmp_path / "docs" / "foundation" / "conformance.md").unlink()

    with pytest.raises(decision_114.CorpusProblem, match="conformance.md"):
        decision_114.check(tmp_path)


def test_raises_when_data_model_file_is_absent(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    (tmp_path / "docs" / "foundation" / "data_model.md").unlink()

    with pytest.raises(decision_114.CorpusProblem, match="data_model.md"):
        decision_114.check(tmp_path)
