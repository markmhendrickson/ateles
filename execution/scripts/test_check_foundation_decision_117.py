from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_foundation_decision_117 as decision_117  # noqa: E402


CONFORMANCE_RULED = """\
# Conformance

## The register of open design decisions

| # | Question | Pointer | Dependencies | Status |
|---|---|---|---|---|
| 117 | whether a grant may say what a principal may do to an edge | `authority_model.md#grants` | decision 41 | **ruled** (2026-09-22): option A — widen the capability tuple |
"""

CONFORMANCE_OPEN = CONFORMANCE_RULED.replace(
    "**ruled** (2026-09-22): option A — widen the capability tuple",
    "**open** (2026-09-10): three candidates, none taken yet",
)

GRANTS_RULED = """\
# Authority model

## Grants

**Write admission per relationship type is default-deny, and the grant is the allowlist, on the same
shape decision 41 already states for entity types (ruled, decision 117, 2026-09-22).** A capability entry
gains an optional `relationship_types[]` beside its `entity_types[]`; absent or empty means default-deny
for edges, exactly as zero entity types already means deny for entities.

**Two bounding conditions, part of the ruling and not separable from it.** A relationship write inside
decision 43's closed thirteen-record bootstrap set is admitted by membership in that set and by nothing
else — no standing operator bypass, so the operator's own credential binding (bootstrap step 3, written
before any grant exists to admit it) stays admitted the way it is admitted today.

**What this does not settle**, left open on the ruling's own instruction: whether `principal_binding` and
`delegation_edge` join the closed governance list of eight and who is sole writer for each; whether
`ownership_grant` passes the admission test at all.

## Principals

Unrelated section that must not be scanned as part of #grants.
"""

# The pre-117 shape: the capability tuple is entity_types + repositories only,
# with no relationship-type term anywhere in the section, and none of the
# other decision-117 ruling language (bounding conditions, deferral) either.
# This is what the corpus looked like before decision 117 was ruled.
GRANTS_LEGACY = """\
# Authority model

## Grants

A capability entry is operation x `entity_types[]` x `repositories[]`; zero grants matching is deny. A
wildcard over `entity_types[]` is the fail-open shape, not an allowlist.

## Principals

Unrelated section that must not be scanned as part of #grants.
"""

# Ruled, but missing the default-deny-on-absent language: names both fields
# but never states what an absent/empty relationship_types[] means.
GRANTS_NO_DEFAULT_DENY = """\
# Authority model

## Grants

A capability entry gains `relationship_types[]` beside `entity_types[]`.

**What this does not settle**, left open on the ruling's own instruction: whether `principal_binding` and
`delegation_edge` join the closed governance list of eight.
"""

# Ruled, missing the bootstrap step-3 sentence and the governance-list
# deferral sentence, but with the tuple language intact.
GRANTS_NO_BOOTSTRAP_NO_DEFERRAL = """\
# Authority model

## Grants

**Write admission per relationship type is default-deny (ruled, decision 117).** A capability entry gains
an optional `relationship_types[]` beside its `entity_types[]`; absent or empty means default-deny for
edges, exactly as zero entity types already means deny for entities.
"""

GATES_TEXT = """\
# Gates and workflows

**Governance writes are actions, and the governance types are one closed list, stated here and nowhere
else.** A write to any of these eight is an action, evaluated at the action gate under the instance's
`action_policy`: `agent`, `agent_policy`, a `workflow` declaration, `action_policy`, `agent_grant`,
`swarm_roster`, the schema registry, and an `intake_rule`. The test that admits a type is what a write to
it changes.

**Some other rule.** Unrelated paragraph that must not be read as part of the closed-list section.
"""


def write_corpus(
    root: Path,
    conformance: str = CONFORMANCE_RULED,
    authority_model: str = GRANTS_RULED,
    gates_and_workflows: str = GATES_TEXT,
) -> None:
    fdir = root / "docs" / "foundation"
    fdir.mkdir(parents=True)
    (fdir / "conformance.md").write_text(conformance, encoding="utf-8")
    (fdir / "authority_model.md").write_text(authority_model, encoding="utf-8")
    (fdir / "gates_and_workflows.md").write_text(gates_and_workflows, encoding="utf-8")


# --- Green: the ruled corpus passes ------------------------------------------


def test_passes_on_ruled_corpus_with_full_ruling_language(tmp_path: Path) -> None:
    write_corpus(tmp_path)

    assert decision_117.check(tmp_path) == []


def test_noop_shape_when_register_row_still_open(tmp_path: Path) -> None:
    """Before the row rules, nothing downstream is required yet — mirrors the
    decision-101/78 no-op-before-ruled pattern, so a legacy tuple pre-ruling
    is not flagged."""
    write_corpus(tmp_path, conformance=CONFORMANCE_OPEN, authority_model=GRANTS_LEGACY)

    assert decision_117.check(tmp_path) == []


# --- Red: prove the checker actually fails on the defects it exists for -----


def test_fails_when_register_row_117_absent(tmp_path: Path) -> None:
    conformance_without_row = "\n".join(
        line
        for line in CONFORMANCE_RULED.splitlines()
        if not line.startswith("| 117 |")
    )
    write_corpus(tmp_path, conformance=conformance_without_row)

    problems = decision_117.check(tmp_path)

    assert len(problems) == 1
    assert "decision-117-register" in problems[0]
    assert 'no register row beginning "| 117 |"' in problems[0]


def test_fails_on_legacy_entity_types_only_tuple_once_ruled(tmp_path: Path) -> None:
    """The exact scenario the task brief calls out as the red fixture: register
    ruled, but #grants still states the pre-117 entity-types-by-repositories
    tuple with no relationship-type term anywhere."""
    write_corpus(tmp_path, authority_model=GRANTS_LEGACY)

    problems = decision_117.check(tmp_path)

    assert problems
    assert any("decision-117-legacy-tuple" in p for p in problems)
    assert any("decision-117-grants" in p and "relationship_types" in p for p in problems)
    assert any("decision-117-bootstrap" in p for p in problems)


def test_fails_when_relationship_types_field_missing(tmp_path: Path) -> None:
    grants_missing_field = GRANTS_RULED.replace("`relationship_types[]`", "the new field")
    write_corpus(tmp_path, authority_model=grants_missing_field)

    problems = decision_117.check(tmp_path)

    assert any(
        "decision-117-grants" in p and "relationship_types" in p for p in problems
    )


def test_fails_when_entity_types_field_missing(tmp_path: Path) -> None:
    grants_missing_field = GRANTS_RULED.replace("`entity_types[]`", "its existing field")
    write_corpus(tmp_path, authority_model=grants_missing_field)

    problems = decision_117.check(tmp_path)

    assert any(
        "decision-117-grants" in p and "entity_types" in p for p in problems
    )


def test_fails_when_default_deny_language_missing(tmp_path: Path) -> None:
    write_corpus(tmp_path, authority_model=GRANTS_NO_DEFAULT_DENY)

    problems = decision_117.check(tmp_path)

    assert any(
        "decision-117-grants" in p and "default-deny" in p for p in problems
    )


def test_fails_when_governance_list_placement_and_deferral_both_absent(
    tmp_path: Path,
) -> None:
    write_corpus(tmp_path, authority_model=GRANTS_NO_BOOTSTRAP_NO_DEFERRAL)

    problems = decision_117.check(tmp_path)

    assert any("decision-117-governance-list" in p for p in problems)


# A grants section carrying the full ruled-tuple language but explicitly
# NOT the governance-list deferral sentence — isolates the placement branch
# so a passing test here can only be explained by gates_and_workflows.md's
# own content, never by authority_model.md's deferral sentence covering for
# it (the failure a prior revision of this test had: it mutated
# gates_and_workflows.md but passed only because GRANTS_RULED's deferral
# sentence was present regardless of what gates_and_workflows.md said).
GRANTS_RULED_NO_DEFERRAL = GRANTS_RULED.replace(
    """
**What this does not settle**, left open on the ruling's own instruction: whether `principal_binding` and
`delegation_edge` join the closed governance list of eight and who is sole writer for each; whether
`ownership_grant` passes the admission test at all.
""",
    "",
)


def test_governance_list_placement_fails_when_gates_silent_and_no_deferral(
    tmp_path: Path,
) -> None:
    """Control for the test below: with no deferral sentence in
    authority_model.md AND the closed list silent on all three terms, item 3
    must fail — proving the pass below is earned by gates content, not by a
    deferral sentence that happens to still be present."""
    assert "join the closed governance list" not in GRANTS_RULED_NO_DEFERRAL
    write_corpus(tmp_path, authority_model=GRANTS_RULED_NO_DEFERRAL)

    problems = decision_117.check(tmp_path)

    assert any("decision-117-governance-list" in p for p in problems)


def test_governance_list_placement_passes_when_all_three_terms_named_in_gates(
    tmp_path: Path,
) -> None:
    """Alternate acceptable reading of item 3: the closed list actually names
    all three edge types directly, rather than deferring. Uses the
    no-deferral grants fixture so this can only pass because
    gates_and_workflows.md's own closed-list section now places the three
    terms — not because authority_model.md's deferral sentence covers for
    it."""
    gates_with_placement = GATES_TEXT.replace(
        "and an `intake_rule`.",
        "an `intake_rule`, `principal_binding`, `delegation_edge`, and "
        "`ownership_grant`.",
    )
    write_corpus(
        tmp_path,
        authority_model=GRANTS_RULED_NO_DEFERRAL,
        gates_and_workflows=gates_with_placement,
    )

    problems = decision_117.check(tmp_path)

    assert not any("decision-117-governance-list" in p for p in problems)


def test_governance_list_placement_ignores_terms_outside_the_closed_list_section(
    tmp_path: Path,
) -> None:
    """The three terms appearing elsewhere in gates_and_workflows.md — outside
    the closed-list paragraph itself — must not satisfy placement: the
    corpus's own rule is that the list is stated in one place, so a mention
    anywhere in the document is not the same as the list placing the term."""
    gates_with_terms_elsewhere = GATES_TEXT.replace(
        "**Some other rule.** Unrelated paragraph that must not be read as "
        "part of the closed-list section.",
        "**Some other rule.** This paragraph mentions `principal_binding`, "
        "`delegation_edge`, and `ownership_grant` but is not the closed-list "
        "section itself.",
    )
    write_corpus(
        tmp_path,
        authority_model=GRANTS_RULED_NO_DEFERRAL,
        gates_and_workflows=gates_with_terms_elsewhere,
    )

    problems = decision_117.check(tmp_path)

    assert any("decision-117-governance-list" in p for p in problems)


def test_fails_when_bootstrap_step3_sentence_missing(tmp_path: Path) -> None:
    write_corpus(tmp_path, authority_model=GRANTS_NO_BOOTSTRAP_NO_DEFERRAL)

    problems = decision_117.check(tmp_path)

    assert any("decision-117-bootstrap" in p for p in problems)


def test_raises_when_conformance_file_is_absent(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    (tmp_path / "docs" / "foundation" / "conformance.md").unlink()

    with pytest.raises(decision_117.CorpusProblem, match="conformance.md"):
        decision_117.check(tmp_path)


def test_raises_when_authority_model_file_is_absent(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    (tmp_path / "docs" / "foundation" / "authority_model.md").unlink()

    with pytest.raises(decision_117.CorpusProblem, match="authority_model.md"):
        decision_117.check(tmp_path)


def test_raises_when_gates_and_workflows_file_is_absent(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    (tmp_path / "docs" / "foundation" / "gates_and_workflows.md").unlink()

    with pytest.raises(decision_117.CorpusProblem, match="gates_and_workflows.md"):
        decision_117.check(tmp_path)
