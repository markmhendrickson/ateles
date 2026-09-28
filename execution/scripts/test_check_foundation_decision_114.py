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

    assert any("decision-114-data-model" in p and "superseded" in p for p in problems)
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


# --- Red: Falco's adversarial reproductions (ateles PR #1321 review) --------
#
# Each of these reproduces a phrasing that made the pre-fix checker (pure
# substring/proximity regexes, first-match-only row lookup) report 0
# problems on a corpus that states the opposite of what row 114 rules.

DATA_MODEL_NEGATED_GOVERNS = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` | this row never carries a `GOVERNS` -> `agent` edge; `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""

DATA_MODEL_NEGATED_SUPERSEDE = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (`scope`/`agent_sub` is NOT superseded by any edge) | `GOVERNS` -> `agent`; `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""

# Both denials in one row: the exact drift state the checker exists to
# catch, phrased as an explicit denial rather than an omission.
DATA_MODEL_NEGATED_BOTH = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (`scope`/`agent_sub` is not superseded by any edge) | this row never carries a `GOVERNS` -> `agent` edge; `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""

# A compliant decoy row placed first in ## Concepts, with a broken real row
# placed second in the same section — the first-match-only lookup masked
# this before the fix.
DATA_MODEL_DECOY_THEN_BROKEN_ROW = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | `GOVERNS` -> `agent`; `SUPERSEDES` -> `agent_policy` | decoy row: compliant | the rendered mirrors | an operator's name |
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` | `SUPERSEDES` -> `agent_policy` | real row: broken, no GOVERNS edge, no superseded language | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""


def test_fails_when_governs_edge_only_appears_negated(tmp_path: Path) -> None:
    """Falco finding 1: 'this row never carries a GOVERNS -> agent edge'
    contains the substring a genuine claim would too — must not pass."""
    write_corpus(tmp_path, data_model=DATA_MODEL_NEGATED_GOVERNS)

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "GOVERNS" in p for p in problems)


def test_fails_when_superseded_claim_only_appears_negated(tmp_path: Path) -> None:
    """Falco finding 1 (supersede half): 'is NOT superseded by any edge'
    must not satisfy the superseded-language check."""
    write_corpus(tmp_path, data_model=DATA_MODEL_NEGATED_SUPERSEDE)

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "superseded" in p for p in problems)


def test_fails_when_both_claims_appear_negated_in_one_row(tmp_path: Path) -> None:
    """Falco finding 3: both denials combined in one row must still report
    both problems, not a false green."""
    write_corpus(tmp_path, data_model=DATA_MODEL_NEGATED_BOTH)

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "GOVERNS" in p for p in problems)
    assert any("decision-114-data-model" in p and "superseded" in p for p in problems)


def test_raises_ambiguous_when_decoy_row_masks_broken_real_row(tmp_path: Path) -> None:
    """Falco finding 5: a compliant decoy row placed first in ## Concepts
    must not silently mask a broken real row placed second — the checker
    must refuse rather than take the first match."""
    write_corpus(tmp_path, data_model=DATA_MODEL_DECOY_THEN_BROKEN_ROW)

    problems = decision_114.check(tmp_path)

    assert problems
    assert any("more than one" in p and "agent behavioural rule" in p for p in problems)


# --- Red: "without" bypass (ateles PR #1321, comment 5856297532) ------------
#
# Falco's re-review found the fixed-window negation denylist above still
# bypassed by any denial phrased with a word outside its hardcoded list.
# Live-reproduced against the checker at commit 89e952a6: "without" evades
# `_NEGATION_RE` entirely, so a row denying both claims using only "without"
# (never "not"/"never"/"no") reported 0 problems. These fixtures pin that
# exact phrasing plus a second, semantically equivalent negative
# construction ("lacking") that was never in the old denylist either — the
# structural affirmative-shape fix must reject both, and for a reason that
# has nothing to do with recognizing either particular word.

DATA_MODEL_WITHOUT_BYPASS = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (`scope`/`agent_sub` remains live, without ever being superseded by any edge) | this row functions without any `GOVERNS` -> `agent` edge; `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""

# A second negative construction outside the old denylist and outside
# "without" too, to prove the fix isn't itself just a longer word list.
DATA_MODEL_LACKING_BYPASS = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (`scope`/`agent_sub` is lacking any superseding claim from an edge) | this row is currently lacking a `GOVERNS` -> `agent` edge; `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""


def test_fails_on_without_phrasing_bypass(tmp_path: Path) -> None:
    """Falco's live reproduction against 89e952a6: 'without any GOVERNS ->
    agent edge' and 'without ever being superseded' both evaded the old
    fixed-word negation denylist entirely and reported 0 problems. Must
    fail on both halves now."""
    write_corpus(tmp_path, data_model=DATA_MODEL_WITHOUT_BYPASS)

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "GOVERNS" in p for p in problems)
    assert any("decision-114-data-model" in p and "superseded" in p for p in problems)


def test_fails_on_lacking_phrasing_bypass(tmp_path: Path) -> None:
    """A semantically equivalent denial using 'lacking' rather than
    'without' or any word the old denylist held — proves the fix rejects by
    structural shape, not by having grown a longer word list."""
    write_corpus(tmp_path, data_model=DATA_MODEL_LACKING_BYPASS)

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "GOVERNS" in p for p in problems)
    assert any("decision-114-data-model" in p and "superseded" in p for p in problems)


# --- Red: long-distance negation (Waxwing, PR #1321 arch re-review) --------
#
# Waxwing's non-blocking finding on the fixed-window denylist: a negation
# word placed further than the (then) 40-character window from the claim —
# a longer qualifying clause — was not detected, so the checker would report
# 0 problems on a row actually denying the claim. The affirmative-shape
# fix has no character-window at all (the whole field-owned parenthetical is
# scanned, however long), so this closes as a side effect of that redesign
# rather than needing a wider or unbounded window.

DATA_MODEL_LONG_DISTANCE_NEGATION = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (it is not accurate, under any reading of the current ruling text or any of its cited dependencies, to say that this field pair is superseded by the edge) | `GOVERNS` -> `agent`; `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""


def test_fails_on_negation_far_from_the_claim(tmp_path: Path) -> None:
    """Waxwing's finding: a negation word more than ~40 characters from the
    claim, inside a longer qualifying clause, must still be caught — the
    affirmative-shape check has no fixed window to exceed."""
    write_corpus(tmp_path, data_model=DATA_MODEL_LONG_DISTANCE_NEGATION)

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "superseded" in p for p in problems)


def test_second_matching_row_outside_concepts_section_is_ignored(
    tmp_path: Path,
) -> None:
    """A second row-shaped line living in a different section (e.g. an
    appendix) must not trigger the ambiguous-row refusal — only rows inside
    ## Concepts are candidates."""
    data_model = DATA_MODEL_RULED.replace(
        "## Relationships\n\nUnrelated section that must not be scanned as part of Concepts.\n",
        "## Relationships\n\nUnrelated section that must not be scanned as part of Concepts.\n"
        "\n## Appendix: pre-114 shape for reference\n\n"
        "| agent behavioural rule | `agent_policy` | `rule`; `scope`; `agent_sub` | `SUPERSEDES` -> `agent_policy` | n/a | n/a | n/a |\n",
    )
    write_corpus(tmp_path, data_model=data_model)

    assert decision_114.check(tmp_path) == []


# --- Red: Waxwing's and Falco's third-round reproductions (PR #1321,
# comments 5856631946 and 5856636444) — the affirmative-shape fix proved
# structural position alone, without checking the position's own content for
# a denial. Both found the same class one layer deeper than the prior round.

# Waxwing: a GOVERNS edge that is genuinely list-entry-shaped, but whose own
# trailing parenthetical denies the very edge it appears to assert.
DATA_MODEL_GOVERNS_DENIED_IN_OWN_PAREN = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | some other edge -> thing; `GOVERNS` -> `agent` (NOTE: this project rejected adding this edge; the row does not actually carry it); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""

# Falco's exact end-to-end reproduction: list-entry-shaped GOVERNS edge whose
# own parenthetical denies it using "proposed but never actually
# implemented" / "no traversal honors it" — words the pre-this-fix checker's
# structural anchor never read because it stopped at the `agent` token.
DATA_MODEL_GOVERNS_ASPIRATIONAL_PLACEHOLDER = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | `GOVERNS` -> `agent` (this edge was proposed but never actually implemented; the table row exists only as an aspirational placeholder and no traversal honors it); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""

# Falco: a superseded claim inside its own field parenthetical, using a
# denial word absent from the pre-this-fix `_HEDGE_RE` ("unimplemented",
# "in practice"/"only in theory").
DATA_MODEL_SUPERSEDED_ONLY_IN_THEORY = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (superseded only in theory; in practice the edge is unimplemented and this field is still read) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""

# Falco's two named variants: "reverted" and "(this claim is false, kept for
# historical record)" — neither word was in the pre-this-fix `_HEDGE_RE`.
DATA_MODEL_SUPERSEDED_REVERTED = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (was superseded in an earlier draft that was later reverted) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""

DATA_MODEL_SUPERSEDED_FALSE_HISTORICAL = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (superseded (this claim is false, kept for historical record)) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""


def test_fails_when_governs_edge_denied_in_its_own_parenthetical(
    tmp_path: Path,
) -> None:
    """Waxwing finding (PR #1321 comment 5856631946): a list-entry-shaped
    GOVERNS edge whose own trailing parenthetical denies it must not pass —
    structural position proves the edge is asserted, not that the assertion
    is true."""
    write_corpus(tmp_path, data_model=DATA_MODEL_GOVERNS_DENIED_IN_OWN_PAREN)

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "GOVERNS" in p for p in problems)


def test_fails_when_governs_edge_is_aspirational_placeholder(
    tmp_path: Path,
) -> None:
    """Falco's exact end-to-end reproduction (PR #1321 comment 5856636444):
    'this edge was proposed but never actually implemented ... no traversal
    honors it' must not pass despite matching the list-entry shape."""
    write_corpus(tmp_path, data_model=DATA_MODEL_GOVERNS_ASPIRATIONAL_PLACEHOLDER)

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "GOVERNS" in p for p in problems)


def test_fails_when_superseded_claim_denied_only_in_theory(tmp_path: Path) -> None:
    """Falco finding 2 (PR #1321 comment 5856636444): 'superseded only in
    theory; in practice the edge is unimplemented' uses no word from the
    pre-fix hedge denylist and must still be rejected."""
    write_corpus(tmp_path, data_model=DATA_MODEL_SUPERSEDED_ONLY_IN_THEORY)

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "superseded" in p for p in problems)


def test_fails_when_superseded_claim_denied_as_reverted(tmp_path: Path) -> None:
    """Falco's 'reverted' variant — a supersession claimed to have happened
    and then been undone is not a live claim that the field is superseded
    now."""
    write_corpus(tmp_path, data_model=DATA_MODEL_SUPERSEDED_REVERTED)

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "superseded" in p for p in problems)


def test_fails_when_superseded_claim_denied_as_false_and_historical(
    tmp_path: Path,
) -> None:
    """Falco's 'this claim is false, kept for historical record' variant."""
    write_corpus(tmp_path, data_model=DATA_MODEL_SUPERSEDED_FALSE_HISTORICAL)

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "superseded" in p for p in problems)


def test_passes_on_live_corpus_long_governs_gloss_discussing_other_rows(
    tmp_path: Path,
) -> None:
    """Regression guard for the false positive found while building this
    fix: the real `agent_policy` GOVERNS gloss is long, and after its first
    clause correctly documents the no-edge fallback for a DIFFERENT scope
    value ('when `scope` is `agent` and the row carries no `GOVERNS` edge, it
    names no target') — legitimate policy prose using 'no', not a denial of
    the edge this entry itself carries. A naive whole-parenthetical hedge
    scan flags this row; it must not."""
    data_model_real_shape = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (**closed**: `global`, `swarm`, or `agent`, legacy for the `agent` value — the one-agent case is resolved by the `GOVERNS` edge and `scope`/`agent_sub` is read nowhere once the edge resolves, superseded by decision 114); `agent_sub` (superseded the same way: the field the pre-114 record used to scope a rule to an agent, now read by nothing) | `GOVERNS` → `agent` (the agent this row binds, resolved by traversal — decision 114; a row carrying none reaches every agent its `scope` names when `scope` is `global` or `swarm`; when `scope` is `agent` and the row carries no `GOVERNS` edge, it names no target and so binds no agent — the restrictive branch); `SUPERSEDES` → `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""
    write_corpus(tmp_path, data_model=data_model_real_shape)

    assert decision_114.check(tmp_path) == []


# --- Red: harness code-review finding on this PR's own fix (self-review
# before opening the PR) — a weak-hedge-only denial placed after the
# parenthetical's first ``;``-clause, with no generic/conditional clause
# opener and no `_STRONG_HEDGE_RE` word anywhere, evaded an earlier revision
# of `_is_hedged` that scanned only the FIRST clause for weak words. Fixed by
# scanning every clause for weak words except one that opens with a
# generic/conditional subject ("a row...", "when..."), the grammatical shape
# the live corpus's own legitimate fallback prose actually uses.

DATA_MODEL_GOVERNS_BARE_NO_AFTER_SEMICOLON = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal; there is no edge here for this row); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""

DATA_MODEL_SUPERSEDED_BARE_WITHOUT_AFTER_SEMICOLON = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (superseded by the GOVERNS edge; the field itself remains without any superseding note) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""


def test_fails_when_governs_denial_uses_only_bare_no_after_semicolon(
    tmp_path: Path,
) -> None:
    """Code-review finding on this PR's own fix: a weak-hedge-only denial
    ('there is no edge here for this row') placed after the parenthetical's
    first `;`-clause, with no generic/conditional opener and no
    `_STRONG_HEDGE_RE` word, must not pass just because it comes after the
    first semicolon."""
    write_corpus(tmp_path, data_model=DATA_MODEL_GOVERNS_BARE_NO_AFTER_SEMICOLON)

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "GOVERNS" in p for p in problems)


def test_fails_when_superseded_denial_uses_only_bare_without_after_semicolon(
    tmp_path: Path,
) -> None:
    """Same finding, superseded-claim half: 'without any superseding note'
    placed after the first `;`, no generic/conditional opener, no strong
    hedge word."""
    write_corpus(
        tmp_path, data_model=DATA_MODEL_SUPERSEDED_BARE_WITHOUT_AFTER_SEMICOLON
    )

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "superseded" in p for p in problems)


# --- Red: second code-review pass on this PR's own fix — the
# generic-clause-opener carve-out ("a"/"an"/"any"/"when") was itself too
# broad: it exempted any clause merely STARTING with one of those four
# words, not only the specific "a different row" / "a different `scope`
# value" prose the carve-out was meant for. Narrowed to require "row" or
# "`scope`" as the clause's actual subject.

DATA_MODEL_SUPERSEDED_ANY_READER_DENIAL = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (superseded by the edge; any reader should know this claim is not accurate) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""

DATA_MODEL_GOVERNS_ANY_ACCURATE_READING_DENIAL = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal; any accurate reading finds this edge is not real); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""


def test_fails_when_superseded_denial_opens_with_any_reader(tmp_path: Path) -> None:
    """Second code-review finding on this PR's own fix: a clause starting
    with 'any reader...' is an ordinary-English denial, not the 'a
    different row' generic-fallback prose the opener carve-out exists for —
    it must not be exempted just because it starts with 'any'."""
    write_corpus(tmp_path, data_model=DATA_MODEL_SUPERSEDED_ANY_READER_DENIAL)

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "superseded" in p for p in problems)


def test_fails_when_governs_denial_opens_with_any_accurate_reading(
    tmp_path: Path,
) -> None:
    """Same finding, GOVERNS-edge half: 'any accurate reading finds this
    edge is not real' must not be exempted by the generic-opener carve-out."""
    write_corpus(tmp_path, data_model=DATA_MODEL_GOVERNS_ANY_ACCURATE_READING_DENIAL)

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "GOVERNS" in p for p in problems)


# --- Red: third code-review pass on this PR's own fix — a fixed-depth
# paren-balancing regex group (``(?:[^()]|\([^()]*\))*``) only balances ONE
# level of nesting. Two levels of nesting (a caveat bolted onto a caveat —
# idiomatic in this corpus's own prose) made the enclosing OPTIONAL group
# fail to match at all, so `match.group(1)` silently became `None` and the
# parenthetical's content was never checked — fail-open for the GOVERNS edge
# (optional group), and for the field's own parenthetical too since the same
# fixed-depth pattern was used there. Fixed with `_extract_balanced_paren`,
# an unbounded balanced-parenthesis scan with no depth limit to exceed.

DATA_MODEL_GOVERNS_NESTED_PAREN_DENIAL = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | `GOVERNS` -> `agent` (resolved by traversal (per decision 114 (but never actually implemented))); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""

DATA_MODEL_SUPERSEDED_NESTED_PAREN_DENIAL = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (superseded (per decision 114 (but never actually implemented))) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""

# Regression guard: a legitimate two-level-nested affirmative parenthetical
# must still pass — the fix must not overcorrect into rejecting genuine
# nested prose that carries no denial.
DATA_MODEL_GOVERNS_NESTED_PAREN_AFFIRMATIVE = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (superseded (per decision 114 (ruled 2026-09-25))); `agent_sub` (superseded; read nowhere once the edge resolves) | `GOVERNS` -> `agent` (resolved by traversal (per decision 114 (ruled 2026-09-25))); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""


def test_fails_when_governs_denial_hides_behind_double_nested_parens(
    tmp_path: Path,
) -> None:
    """Third code-review finding: a denial two parenthesis-levels deep in the
    GOVERNS edge's own gloss must not silently escape the hedge scan just
    because a fixed-depth regex group can't balance it."""
    write_corpus(tmp_path, data_model=DATA_MODEL_GOVERNS_NESTED_PAREN_DENIAL)

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "GOVERNS" in p for p in problems)


def test_fails_when_superseded_denial_hides_behind_double_nested_parens(
    tmp_path: Path,
) -> None:
    """Same finding, superseded-claim half."""
    write_corpus(tmp_path, data_model=DATA_MODEL_SUPERSEDED_NESTED_PAREN_DENIAL)

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "superseded" in p for p in problems)


def test_passes_on_double_nested_paren_affirmative_claim(tmp_path: Path) -> None:
    """Regression guard: legitimate two-level-nested prose with no denial
    must still pass — the balanced-paren fix must not overcorrect."""
    write_corpus(tmp_path, data_model=DATA_MODEL_GOVERNS_NESTED_PAREN_AFFIRMATIVE)

    assert decision_114.check(tmp_path) == []


# --- Red: Phoenicurus's QA reproductions (PR #1321 comment 5856295835) —
# both found through a full `check()` reproduction, not just unit-level
# regex probing, on the affirmative-shape fix that closed the security
# findings above. Same root cause pattern: a structural-position check
# (list-entry shape, own-parenthetical scoping) that verifies POSITION but
# leaves a gap either in the target being matched or in the vocabulary
# guarding the position's content.

# QA finding 1: `_GOVERNS_EDGE_ENTRY_RE` had no right-hand word boundary
# after the literal `agent`, so `GOVERNS -> agent_sub` (or any other
# `agent`-prefixed token) satisfied the "GOVERNS -> agent" requirement as a
# substring match — an edge pointed at the WRONG target, exactly the pre-114
# field this decision retires the row away from.
DATA_MODEL_GOVERNS_WRONG_TARGET_AGENT_SUB = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | `GOVERNS` -> `agent_sub` (the row still points the edge at the old field, not the agent entity); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""

# QA finding 2: counterfactual/hypothetical prose attached to an otherwise
# list-entry-shaped GOVERNS edge — honest that the edge is illustrative, not
# real — used none of the denial vocabulary the hedge scan checked for.
DATA_MODEL_GOVERNS_HYPOTHETICAL_ILLUSTRATION = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | no such edge is ever stored today; `GOVERNS` -> `agent` (this is only a hypothetical illustration of what the edge would look like if it existed); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""


def test_fails_when_governs_edge_points_at_wrong_target_agent_sub(
    tmp_path: Path,
) -> None:
    """Phoenicurus/QA finding 1 (PR #1321 comment 5856295835): a substring
    match with no right-hand word boundary let `GOVERNS -> agent_sub` (the
    WRONG target — the pre-114 field, not the agent entity) satisfy the
    `GOVERNS -> agent` requirement. Confirmed via full `check()`
    reproduction, not just the regex in isolation."""
    write_corpus(tmp_path, data_model=DATA_MODEL_GOVERNS_WRONG_TARGET_AGENT_SUB)

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "GOVERNS" in p for p in problems)


def test_fails_when_governs_edge_is_hypothetical_illustration(
    tmp_path: Path,
) -> None:
    """Phoenicurus/QA finding 2 (PR #1321 comment 5856295835): counterfactual
    prose ('this is only a hypothetical illustration of what the edge would
    look like if it existed') attached to an otherwise list-entry-shaped
    GOVERNS edge must not pass — being honest that the edge doesn't exist,
    phrased as an illustration rather than a flat denial, is still a denial."""
    write_corpus(tmp_path, data_model=DATA_MODEL_GOVERNS_HYPOTHETICAL_ILLUSTRATION)

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "GOVERNS" in p for p in problems)


# --- Red: harness code-review self-review on this PR's fourth commit
# (before opening/updating the PR) — a third pass on the same
# clause-opener-carve-out gap Falco and a prior review pass both already
# narrowed once. "A row exactly like this one carries no such edge" is
# grammatically "a row ..." but denies THIS entry in the present tense, not
# a different row's fallback behaviour — it still slipped the
# then-current carve-out, which exempted any clause with "row" or "`scope`"
# as its apparent subject. Narrowed further: the carve-out now requires the
# literal `` when `scope` is `` comparison the live corpus's own legitimate
# clause actually uses, not just the word "row" appearing early in the
# clause.

DATA_MODEL_GOVERNS_SPOOFED_ROW_OPENER_DENIAL = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal; a row exactly like this one carries no such edge); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""

DATA_MODEL_SUPERSEDED_IN_NAME_ONLY = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (superseded in name only; nothing actually reads the edge instead, and scope/agent_sub is still consulted by the loader today) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""


def test_fails_when_governs_denial_spoofs_the_row_opener_carveout(
    tmp_path: Path,
) -> None:
    """Self-review finding (third pass on the clause-opener carve-out): "a
    row exactly like this one carries no such edge" mimics the shape of the
    legitimate "a row carrying none reaches every agent..." fallback prose
    but is a present-tense denial of THIS entry, not a different row's
    behaviour. Must not be exempted just because the clause's apparent
    subject is "row"."""
    write_corpus(tmp_path, data_model=DATA_MODEL_GOVERNS_SPOOFED_ROW_OPENER_DENIAL)

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "GOVERNS" in p for p in problems)


def test_fails_when_superseded_claim_denied_as_in_name_only(
    tmp_path: Path,
) -> None:
    """Self-review finding: 'superseded in name only' denies the claim using
    vocabulary absent from both hedge tiers before this fix."""
    write_corpus(tmp_path, data_model=DATA_MODEL_SUPERSEDED_IN_NAME_ONLY)

    problems = decision_114.check(tmp_path)

    assert any("decision-114-data-model" in p and "superseded" in p for p in problems)


# --- Red: current-head UX review finding — the live no-edge fallback carve-out
# exempted every weak-negation clause beginning with ``when scope is``. That
# prefix is not enough to prove the clause describes the legitimate fallback:
# it can just as easily introduce a contradiction of the edge or field claim.
# Exercise both bypasses through the full checker, not the hedge helper alone.

DATA_MODEL_GOVERNS_SCOPE_PREFIXED_DENIAL = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | `GOVERNS` -> `agent` (resolved by traversal; when scope is agent, this row has no such edge); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""

DATA_MODEL_SUPERSEDED_SCOPE_PREFIXED_DENIAL = """\
# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (superseded by the GOVERNS edge; when scope is agent, this field is not superseded and remains the target selector); `agent_sub` | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |

## Relationships

Unrelated section.
"""


def test_fails_when_governs_denial_spoofs_scope_fallback_prefix(
    tmp_path: Path,
) -> None:
    write_corpus(tmp_path, data_model=DATA_MODEL_GOVERNS_SCOPE_PREFIXED_DENIAL)

    problems = decision_114.check(tmp_path)

    assert len(problems) == 1
    assert "edges column is missing a `GOVERNS`" in problems[0]
    assert "must carry it as a `;`-separated edge-list entry" in problems[0]


def test_fails_when_superseded_denial_spoofs_scope_fallback_prefix(
    tmp_path: Path,
) -> None:
    write_corpus(tmp_path, data_model=DATA_MODEL_SUPERSEDED_SCOPE_PREFIXED_DENIAL)

    problems = decision_114.check(tmp_path)

    assert len(problems) == 1
    assert "does not affirmatively state that it is superseded" in problems[0]
    assert "with no hedge or denial word in it" in problems[0]


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
