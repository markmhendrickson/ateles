from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import check_foundation_decision_114 as decision_114  # noqa: E402

REPO_ROOT = SCRIPT_DIR.parents[1]

CONFORMANCE_RULED = """\
# Conformance

## The register of open design decisions

| # | The question | Argued in | Blocks | Status |
|---|---|---|---|---|
| 114 | where a rule that binds every agent's behaviour lives | `migration.md#gaps-and-contradictions-the-mapping-exposed` | decision 31 | **ruled** (2026-09-25): tied to its agent by a `GOVERNS` edge |
"""

CONFORMANCE_OPEN = CONFORMANCE_RULED.replace(
    "**ruled** (2026-09-25): tied to its agent by a `GOVERNS` edge",
    "**open** (2026-09-10): three candidates, none taken yet",
)

SCOPE_OK = decision_114.APPROVED_SCOPE_TEXTS[0]
AGENT_SUB_OK = decision_114.APPROVED_AGENT_SUB_TEXTS[0]
GOVERNS_OK = decision_114.APPROVED_GOVERNS_AGENT_TEXTS[0]


def rule_row(
    *,
    scope: str | None = SCOPE_OK,
    agent_sub: str | None = AGENT_SUB_OK,
    governs: str | None = GOVERNS_OK,
    extra_fields: str = "",
    extra_edges: str = "; `SUPERSEDES` → `rule` (the rule it replaces)",
    governs_target: str = "`agent`",
) -> str:
    scope_part = "`scope`" + (f" ({scope})" if scope is not None else "")
    agent_sub_part = "`agent_sub`" + (f" ({agent_sub})" if agent_sub is not None else "")
    fields = f"`rule`; `rule_kind`; {scope_part}; {agent_sub_part}{extra_fields}"
    if governs is None:
        edges = extra_edges.lstrip("; ")
    else:
        edges = f"`GOVERNS` → {governs_target} ({governs}){extra_edges}"
    return (
        f"| agent behavioural rule | `rule` | {fields} | {edges} | the rules in force | "
        "the rendered mirrors | an operator's name |"
    )


def data_model(*rows: str, trailer: str = "") -> str:
    body = "\n".join(rows)
    return f"""# Data model

## Concepts

<!-- rendered: data_model concepts -->

| Concept | Entity type | Key fields | Edges (type, direction, target) | Derived reads | Projections | Deliberately not a field |
|---|---|---|---|---|---|---|
| task | `task` | `status` | `ADDRESSED_BY` → batch | claimable | `step_status` | the lease holder |
{body}

## Relationships

Unrelated section that must not be scanned as part of Concepts.
{trailer}"""


def write_corpus(root: Path, conformance: str = CONFORMANCE_RULED, data_model_text: str | None = None) -> None:
    fdir = root / "docs" / "foundation"
    fdir.mkdir(parents=True)
    (fdir / "conformance.md").write_text(conformance, encoding="utf-8")
    (fdir / "data_model.md").write_text(
        data_model_text if data_model_text is not None else data_model(rule_row()), encoding="utf-8"
    )


# --- Green -------------------------------------------------------------------


def test_passes_on_ruled_corpus_with_the_approved_statements(tmp_path: Path) -> None:
    write_corpus(tmp_path)
    assert decision_114.check(tmp_path) == []


def test_passes_on_the_live_corpus() -> None:
    assert decision_114.check(REPO_ROOT) == []


def test_approved_text_matches_through_whitespace_changes(tmp_path: Path) -> None:
    spaced = GOVERNS_OK.replace(" ", "  ")
    write_corpus(tmp_path, data_model_text=data_model(rule_row(governs=spaced)))
    assert decision_114.check(tmp_path) == []


def test_noop_when_register_row_still_open(tmp_path: Path) -> None:
    write_corpus(
        tmp_path,
        conformance=CONFORMANCE_OPEN,
        data_model_text=data_model(rule_row(scope="closed: global, swarm, or agent", agent_sub=None, governs=None)),
    )
    assert decision_114.check(tmp_path) == []


def test_second_matching_row_outside_concepts_section_is_ignored(tmp_path: Path) -> None:
    write_corpus(
        tmp_path,
        data_model_text=data_model(rule_row(), trailer=rule_row(governs=None) + "\n"),
    )
    assert decision_114.check(tmp_path) == []


# --- Red: structure ----------------------------------------------------------


def test_fails_when_register_row_114_absent(tmp_path: Path) -> None:
    write_corpus(tmp_path, conformance="# Conformance\n\nNo register here.\n")
    problems = decision_114.check(tmp_path)
    assert len(problems) == 1 and "decision-114-register" in problems[0]


def test_fails_when_concepts_row_absent_while_ruled(tmp_path: Path) -> None:
    write_corpus(tmp_path, data_model_text=data_model())
    problems = decision_114.check(tmp_path)
    assert len(problems) == 1 and "no concepts-table row" in problems[0]


def test_reports_ambiguity_when_a_decoy_row_precedes_a_broken_one(tmp_path: Path) -> None:
    write_corpus(tmp_path, data_model_text=data_model(rule_row(), rule_row(governs=None)))
    problems = decision_114.check(tmp_path)
    assert len(problems) == 1 and "more than one" in problems[0]


def test_fails_when_governs_edge_missing(tmp_path: Path) -> None:
    write_corpus(tmp_path, data_model_text=data_model(rule_row(governs=None)))
    problems = decision_114.check(tmp_path)
    assert any("`GOVERNS` → `agent`" in p for p in problems)


def test_fails_when_governs_edge_has_no_parenthetical(tmp_path: Path) -> None:
    row = rule_row(governs=None, extra_edges="; `GOVERNS` → `agent`; `SUPERSEDES` → `rule` (x)")
    write_corpus(tmp_path, data_model_text=data_model(row))
    assert decision_114.check(tmp_path)


def test_fails_when_governs_edge_points_at_agent_sub(tmp_path: Path) -> None:
    write_corpus(tmp_path, data_model_text=data_model(rule_row(governs_target="`agent_sub`")))
    assert decision_114.check(tmp_path)


def test_fails_when_text_follows_the_approved_parenthetical(tmp_path: Path) -> None:
    row = rule_row(extra_edges=" — though nothing traverses it; `SUPERSEDES` → `rule` (x)")
    write_corpus(tmp_path, data_model_text=data_model(row))
    assert decision_114.check(tmp_path)


def test_fails_when_a_prose_entry_sits_in_the_edge_list(tmp_path: Path) -> None:
    row = rule_row(extra_edges="; note that the edge above is never traversed")
    write_corpus(tmp_path, data_model_text=data_model(row))
    problems = decision_114.check(tmp_path)
    assert len(problems) == 1 and "not an edge" in problems[0]


def test_fails_when_agent_sub_description_missing(tmp_path: Path) -> None:
    write_corpus(tmp_path, data_model_text=data_model(rule_row(agent_sub=None)))
    assert decision_114.check(tmp_path)


def test_fails_when_scope_description_missing(tmp_path: Path) -> None:
    write_corpus(tmp_path, data_model_text=data_model(rule_row(scope=None)))
    assert decision_114.check(tmp_path)


def test_fails_when_a_second_scope_description_denies_the_first(tmp_path: Path) -> None:
    row = rule_row(extra_fields="; `scope` (still read directly by the loader)")
    write_corpus(tmp_path, data_model_text=data_model(row))
    assert decision_114.check(tmp_path)


# --- Red: every denial an earlier revision's word lists had to chase ---------
#
# Each revision of this checker before the exact-text rule read the prose for a denial, and each review
# round found a denial its word list lacked. These are every row those revisions' tests planted. Under the
# exact-text rule none of them can pass, whatever words they use.

HISTORICAL_BYPASS_ROWS = (
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (the field `GOVERNS` supersedes; `scope`/`agent_sub` is read nowhere once the edge resolves, decision 114) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal, decision 114); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (closed: global, swarm, or agent; where the scope is agent the target is `agent_sub`); `agent_sub` (the field that scopes a rule to an agent) | `SUPERSEDES` -> `agent_policy`; `REFERS_TO` <- finding | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` | `GOVERNS` -> `agent`; `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded by traversal for an agent-specific rule; `scope`/`agent_sub` is read nowhere once the edge resolves, decision 114) | `SUPERSEDES` -> `agent_policy`; `REFERS_TO` <- finding | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` | this row never carries a `GOVERNS` -> `agent` edge; `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (`scope`/`agent_sub` is NOT superseded by any edge) | `GOVERNS` -> `agent`; `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (`scope`/`agent_sub` is not superseded by any edge) | this row never carries a `GOVERNS` -> `agent` edge; `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | `GOVERNS` -> `agent`; `SUPERSEDES` -> `agent_policy` | decoy row: compliant | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` | `SUPERSEDES` -> `agent_policy` | real row: broken, no GOVERNS edge, no superseded language | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (`scope`/`agent_sub` remains live, without ever being superseded by any edge) | this row functions without any `GOVERNS` -> `agent` edge; `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (`scope`/`agent_sub` is lacking any superseding claim from an edge) | this row is currently lacking a `GOVERNS` -> `agent` edge; `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (it is not accurate, under any reading of the current ruling text or any of its cited dependencies, to say that this field pair is superseded by the edge) | `GOVERNS` -> `agent`; `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | some other edge -> thing; `GOVERNS` -> `agent` (NOTE: this project rejected adding this edge; the row does not actually carry it); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | `GOVERNS` -> `agent` (this edge was proposed but never actually implemented; the table row exists only as an aspirational placeholder and no traversal honors it); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (superseded only in theory; in practice the edge is unimplemented and this field is still read) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (was superseded in an earlier draft that was later reverted) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (superseded (this claim is false, kept for historical record)) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (**closed**: `global`, `swarm`, or `agent`, legacy for the `agent` value — the one-agent case is resolved by the `GOVERNS` edge and `scope`/`agent_sub` is read nowhere once the edge resolves, superseded by decision 114); `agent_sub` (superseded the same way: the field the pre-114 record used to scope a rule to an agent, now read by nothing) | `GOVERNS` → `agent` (the agent this row binds, resolved by traversal — decision 114; a row carrying none reaches every agent its `scope` names when `scope` is `global` or `swarm`; when `scope` is `agent` and the row carries no `GOVERNS` edge, it names no target and so binds no agent — the restrictive branch); `SUPERSEDES` → `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal; there is no edge here for this row); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (superseded by the GOVERNS edge; the field itself remains without any superseding note) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (superseded by the edge; any reader should know this claim is not accurate) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal; any accurate reading finds this edge is not real); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | `GOVERNS` -> `agent` (resolved by traversal (per decision 114 (but never actually implemented))); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (superseded (per decision 114 (but never actually implemented))) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (superseded (per decision 114 (ruled 2026-09-25))); `agent_sub` (superseded; read nowhere once the edge resolves) | `GOVERNS` -> `agent` (resolved by traversal (per decision 114 (ruled 2026-09-25))); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | `GOVERNS` -> `agent_sub` (the row still points the edge at the old field, not the agent entity); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | no such edge is ever stored today; `GOVERNS` -> `agent` (this is only a hypothetical illustration of what the edge would look like if it existed); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal; a row exactly like this one carries no such edge); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope`; `agent_sub` (superseded in name only; nothing actually reads the edge instead, and scope/agent_sub is still consulted by the loader today) | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | `GOVERNS` -> `agent` (resolved by traversal; when scope is agent, this row has no such edge); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (superseded by the GOVERNS edge; when scope is agent, this field is not superseded and remains the target selector); `agent_sub` | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | `GOVERNS` -> `agent` (resolved by traversal; note: this specific row is edgeless); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (superseded by the GOVERNS edge; note: when scope is agent, this specific field remains unsuperseded); `agent_sub` | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (this documentation entry supersedes an earlier draft; the field itself is superseded by nothing); `agent_sub` | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (superseded by the `GOVERNS` edge on paper only; the loader still reads scope directly); `agent_sub` | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (notionally superseded by the `GOVERNS` edge, though the loader still reads it directly); `agent_sub` | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (superseded by the `GOVERNS` edge? hardly — this field is still read directly by the loader); `agent_sub` | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | `GOVERNS` -> `agent` (on paper only; the loader still resolves membership via `scope` directly); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (legacy; superseded for an agent-specific rule); `agent_sub` (superseded; read nowhere once the edge resolves) | `GOVERNS` -> `agent` (an edge? technically stored, functionally inert since nothing reads it); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
    "| agent behavioural rule | `agent_policy` | `rule`; `rule_kind`; `scope` (this field is superseded gradually as callers migrate, but as of today the field is barely superseded); `agent_sub` | `GOVERNS` -> `agent` (the agent this row binds, resolved by traversal); `SUPERSEDES` -> `agent_policy` | the rules in force for an agent at a time | the rendered mirrors | an operator's name |",
)


@pytest.mark.parametrize("row", HISTORICAL_BYPASS_ROWS)
def test_every_historical_bypass_row_fails(tmp_path: Path, row: str) -> None:
    write_corpus(tmp_path, data_model_text=data_model(row))
    assert decision_114.check(tmp_path)


# The PR #1321 review round at 8e49b9c8 (qa, arch, security) broke the position check with words no list
# held. Each is tried in the claim slot of all three approved spans, and as an aside appended to each.
HEDGES = (
    "seems", "appears", "arguably", "supposedly", "allegedly", "purportedly", "presumably",
    "apparently", "in theory", "theoretically", "eventually", "will be", "purports to be",
    "claims to be", "formerly claimed to be",
)
ASIDES = (
    "subject to removal pending review",
    "per an earlier draft",
    "whether this actually binds anything is debatable",
    "will be superseded once the edge is adopted",
    "though the loader still reads it directly",
)


def _hedged(text: str, word: str, hedge: str) -> str:
    assert word in text
    return text.replace(word, f"{hedge} {word}", 1)


@pytest.mark.parametrize("hedge", HEDGES)
@pytest.mark.parametrize(
    "column",
    ("governs", "scope", "agent_sub"),
)
def test_hedge_in_the_claim_slot_fails(tmp_path: Path, hedge: str, column: str) -> None:
    kwargs = {
        "governs": {"governs": _hedged(GOVERNS_OK, "resolved", hedge)},
        "scope": {"scope": _hedged(SCOPE_OK, "superseded", hedge)},
        "agent_sub": {"agent_sub": _hedged(AGENT_SUB_OK, "superseded", hedge)},
    }[column]
    write_corpus(tmp_path, data_model_text=data_model(rule_row(**kwargs)))
    assert decision_114.check(tmp_path)


@pytest.mark.parametrize("aside", ASIDES)
@pytest.mark.parametrize("column", ("governs", "scope", "agent_sub"))
def test_aside_appended_to_a_claim_fails(tmp_path: Path, aside: str, column: str) -> None:
    base = {"governs": GOVERNS_OK, "scope": SCOPE_OK, "agent_sub": AGENT_SUB_OK}[column]
    write_corpus(tmp_path, data_model_text=data_model(rule_row(**{column: f"{base}; {aside}"})))
    assert decision_114.check(tmp_path)


def test_security_combined_bypass_fixture_fails(tmp_path: Path) -> None:
    """Security's end-to-end exit-0 reproduction at 8e49b9c8, both columns in one row."""
    row = rule_row(
        scope="will be superseded once the edge is adopted",
        agent_sub="will be superseded once the edge is adopted",
        governs="subject to removal pending review",
    )
    write_corpus(tmp_path, data_model_text=data_model(row))
    assert len(decision_114.check(tmp_path)) == 2


def test_fails_when_a_second_governs_agent_entry_denies_the_approved_one(tmp_path: Path) -> None:
    """Self-review finding: one approved entry must not vouch for a second, denying one."""
    row = rule_row(extra_edges="; `GOVERNS` → `agent` (not yet; the scope field is still what the loader reads)")
    write_corpus(tmp_path, data_model_text=data_model(row))
    assert decision_114.check(tmp_path)


def test_fails_when_prose_is_dressed_as_an_edge_of_an_unknown_type(tmp_path: Path) -> None:
    """Self-review finding: `NOTE` → x is not an edge the row may carry."""
    row = rule_row(extra_edges="; `NOTE` → x (all of the above is superseded and not in force)")
    write_corpus(tmp_path, data_model_text=data_model(row))
    problems = decision_114.check(tmp_path)
    assert len(problems) == 1 and "not an edge" in problems[0]


# --- Red: text attached to a fields-column claim entry (qa, 3c5341cf) ----------------------------


def _field_entry(field: str) -> str:
    return f"`{field}` ({ {'scope': SCOPE_OK, 'agent_sub': AGENT_SUB_OK}[field]})"


def _row_with_field_entry(field: str, entry: str) -> str:
    """A rule row whose `field` entry is replaced by ``entry`` and the other field left approved."""
    other = "agent_sub" if field == "scope" else "scope"
    fields = f"`rule`; `rule_kind`; {entry}; {_field_entry(other)}"
    edges = f"`GOVERNS` → `agent` ({GOVERNS_OK}); `SUPERSEDES` → `rule` (the rule it replaces)"
    return (
        f"| agent behavioural rule | `rule` | {fields} | {edges} | the rules in force | "
        "the rendered mirrors | an operator's name |"
    )


FIELD_WRAPPERS = tuple(
    [lambda e, a=aside: f"{e} — {a}" for aside in ASIDES]
    + [
        lambda e: f"not {e}",
        lambda e: f"{e}, reverted",
        lambda e: f"~~{e}~~",
        lambda e: f"<del>{e}</del>",
    ]
)


@pytest.mark.parametrize("wrap", FIELD_WRAPPERS)
@pytest.mark.parametrize("field", ("scope", "agent_sub"))
def test_text_around_a_field_claim_fails(tmp_path: Path, wrap, field: str) -> None:
    """At 3c5341cf only the parenthetical was compared, so every one of these returned 0 problems."""
    row = _row_with_field_entry(field, wrap(_field_entry(field)))
    write_corpus(tmp_path, data_model_text=data_model(row))
    assert decision_114.check(tmp_path)


def test_bare_second_mention_of_a_field_fails(tmp_path: Path) -> None:
    row = rule_row(extra_fields="; `scope` is still what the loader reads")
    write_corpus(tmp_path, data_model_text=data_model(row))
    assert decision_114.check(tmp_path)


def test_exact_field_entries_pass(tmp_path: Path) -> None:
    for field in ("scope", "agent_sub"):
        write_corpus(tmp_path / field, data_model_text=data_model(_row_with_field_entry(field, _field_entry(field))))
        assert decision_114.check(tmp_path / field) == []


# --- Red: decoy register row (security, 3c5341cf) ----------------------------------------------


def test_non_ruled_decoy_register_row_does_not_disable_the_check(tmp_path: Path) -> None:
    """At 3c5341cf the first `| 114 |` line anywhere won, so this returned 0 problems."""
    decoy = "| 114 | decoy | x | y | **open** |\n\n"
    conformance = CONFORMANCE_RULED.replace("## The register", decoy + "## The register", 1)
    write_corpus(tmp_path, conformance=conformance, data_model_text=data_model(rule_row(governs=None)))
    problems = decision_114.check(tmp_path)
    assert problems and any("GOVERNS" in p for p in problems)


def test_second_register_row_114_is_ambiguous(tmp_path: Path) -> None:
    conformance = CONFORMANCE_RULED.replace("| 114 |", "| 114 | decoy | x | y | **open** |\n| 114 |", 1)
    write_corpus(tmp_path, conformance=conformance)
    problems = decision_114.check(tmp_path)
    assert len(problems) == 1 and "more than one register row" in problems[0]


def test_register_row_only_inside_a_comment_is_not_found(tmp_path: Path) -> None:
    conformance = CONFORMANCE_RULED.replace("| 114 |", "<!--\n| 114 |", 1) + "-->\n"
    write_corpus(tmp_path, conformance=conformance)
    problems = decision_114.check(tmp_path)
    assert len(problems) == 1 and "no register row" in problems[0]


# --- Red: markup hiding the approved text (security, 3c5341cf) ---------------------------------


def _live_like_row() -> str:
    return rule_row(extra_edges="; `PART_OF` → `policy` (a grouping); `SUPERSEDES` → `rule` (the rule it replaces)")


def test_html_comment_hiding_the_governs_entry_fails(tmp_path: Path) -> None:
    """Security's reproduction: a comment opened in one edge entry and closed in the next hides the
    approved GOVERNS entry from a renderer while every entry still parses. 0 problems at 3c5341cf."""
    row = rule_row(
        governs=GOVERNS_OK,
        extra_edges="; `SUPERSEDES` → `rule` (the rule it replaces)",
    ).replace("`GOVERNS` → `agent`", "`PART_OF` → `policy` (a grouping <!-- ); `GOVERNS` → `agent`", 1)
    row = row.replace("; `SUPERSEDES`", " --> ); `SUPERSEDES`", 1)
    write_corpus(tmp_path, data_model_text=data_model(row))
    problems = decision_114.check(tmp_path)
    assert any("decision-114-markup" in p for p in problems)


def test_approved_field_texts_moved_into_a_comment_fail(tmp_path: Path) -> None:
    row = rule_row(scope="still the live target selector", agent_sub="still read").replace(
        "| `GOVERNS`",
        f"<!-- `scope` ({SCOPE_OK}); `agent_sub` ({AGENT_SUB_OK}) --> | `GOVERNS`",
        1,
    )
    write_corpus(tmp_path, data_model_text=data_model(row))
    problems = decision_114.check(tmp_path)
    assert any("decision-114-markup" in p for p in problems)


def test_strikethrough_around_the_governs_entry_fails(tmp_path: Path) -> None:
    row = _live_like_row().replace("`GOVERNS` → `agent`", "~~`GOVERNS` → `agent`", 1).replace(
        "; `PART_OF`", "~~; `PART_OF`", 1
    )
    write_corpus(tmp_path, data_model_text=data_model(row))
    problems = decision_114.check(tmp_path)
    assert any("decision-114-markup" in p for p in problems)


@pytest.mark.parametrize("markup", ("<!-- x -->", "<span hidden>x</span>", "<details>x</details>", "~~x~~"))
def test_any_hiding_markup_on_the_concepts_row_fails(tmp_path: Path, markup: str) -> None:
    row = _live_like_row().replace("the rules in force", f"the rules in force {markup}")
    write_corpus(tmp_path, data_model_text=data_model(row))
    problems = decision_114.check(tmp_path)
    assert len(problems) == 1 and "decision-114-markup" in problems[0]


def test_hiding_markup_on_register_row_114_fails(tmp_path: Path) -> None:
    conformance = CONFORMANCE_RULED.replace("tied to its agent", "tied <!-- not --> to its agent")
    write_corpus(tmp_path, conformance=conformance)
    problems = decision_114.check(tmp_path)
    assert len(problems) == 1 and "decision-114-markup" in problems[0] and "conformance.md" in problems[0]


def test_ruled_hidden_in_a_comment_still_enables_the_check(tmp_path: Path) -> None:
    """Hiding can only turn the check on: `**ruled**` inside a comment on an open row enforces."""
    conformance = CONFORMANCE_OPEN.replace("**open**", "**open** <!-- **ruled** -->")
    write_corpus(tmp_path, conformance=conformance, data_model_text=data_model(rule_row(governs=None)))
    assert decision_114.check(tmp_path)


def test_ascii_arrow_less_than_is_not_markup(tmp_path: Path) -> None:
    row = _live_like_row().replace("; `PART_OF` → `policy`", "; `REFERS_TO` <- finding (the finding)")
    write_corpus(tmp_path, data_model_text=data_model(row))
    assert decision_114.check(tmp_path) == []


# --- Red: row lookup that a reader and the checker disagree on (security, 835ec7b3) ------------
#
# Each fixture pairs a broken real row (what renders in the table) with an approved copy elsewhere in
# the section. At 835ec7b3 the checker located only the copy, so each returned 0 problems.

_BROKEN_ROW = rule_row(governs=None, scope="the live target selector")


def _with_section_prefix(text: str, heading: str, prefix: str) -> str:
    return text.replace(heading + "\n", heading + "\n\n" + prefix, 1)


def test_format_character_in_the_key_with_a_details_copy_is_red(tmp_path: Path) -> None:
    """Real row's key cell carries U+200B; the approved row sits in a `<details>` block."""
    real = _BROKEN_ROW.replace("| agent behavioural rule |", "| agent behavioural rule​ |", 1)
    text = data_model(real, "", "<details>", "", rule_row(), "", "</details>")
    write_corpus(tmp_path, data_model_text=text)
    problems = decision_114.check(tmp_path)
    assert len(problems) == 1 and "more than one" in problems[0]


def test_fenced_heading_does_not_end_the_section_early(tmp_path: Path) -> None:
    """An approved copy under the heading, then a fence holding a `## ` line, then the real table."""
    text = _with_section_prefix(
        data_model(_BROKEN_ROW), "## Concepts", rule_row() + "\n\n```\n## x\n```\n\n"
    )
    write_corpus(tmp_path, data_model_text=text)
    problems = decision_114.check(tmp_path)
    assert len(problems) == 1 and "more than one" in problems[0]


def test_indented_real_row_with_a_fenced_copy_is_red(tmp_path: Path) -> None:
    text = data_model(" " + _BROKEN_ROW, trailer="")
    text = text.replace("\n## Relationships", "\n```\n" + rule_row() + "\n```\n\n## Relationships", 1)
    write_corpus(tmp_path, data_model_text=text)
    problems = decision_114.check(tmp_path)
    assert len(problems) == 1 and "more than one" in problems[0]


def test_indented_register_row_with_a_fenced_non_ruled_copy_is_red(tmp_path: Path) -> None:
    """The real ruled row indented one space; a non-ruled copy in a fence must not switch the check off."""
    conformance = CONFORMANCE_RULED.replace("| 114 |", " | 114 |", 1) + (
        "\n```\n| 114 | decoy | x | y | **open** |\n```\n"
    )
    write_corpus(tmp_path, conformance=conformance, data_model_text=data_model(_BROKEN_ROW))
    problems = decision_114.check(tmp_path)
    assert len(problems) == 1 and "more than one register row" in problems[0]


def test_heading_inside_an_html_pre_block_does_not_end_the_section(tmp_path: Path) -> None:
    text = _with_section_prefix(
        data_model(_BROKEN_ROW), "## Concepts", rule_row() + "\n\n<pre>\n\n## x\n\n</pre>\n\n"
    )
    write_corpus(tmp_path, data_model_text=text)
    problems = decision_114.check(tmp_path)
    assert len(problems) == 1 and "more than one" in problems[0]


@pytest.mark.parametrize("prefix", ("> ", "- ", "    "))
def test_a_row_behind_a_container_marker_counts_toward_ambiguity(tmp_path: Path, prefix: str) -> None:
    text = data_model(rule_row(), "", prefix + _BROKEN_ROW)
    write_corpus(tmp_path, data_model_text=text)
    problems = decision_114.check(tmp_path)
    assert len(problems) == 1 and "more than one" in problems[0]


def test_a_row_without_a_leading_pipe_counts_toward_ambiguity(tmp_path: Path) -> None:
    text = data_model(rule_row(), _BROKEN_ROW.lstrip("| "))
    write_corpus(tmp_path, data_model_text=text)
    problems = decision_114.check(tmp_path)
    assert len(problems) == 1 and "more than one" in problems[0]


def test_real_row_indented_four_columns_is_not_located(tmp_path: Path) -> None:
    """Four columns is indented code, not a table row: the lookup fails closed as "no row"."""
    write_corpus(tmp_path, data_model_text=data_model("", "    " + rule_row()))
    problems = decision_114.check(tmp_path)
    assert len(problems) == 1 and "no concepts-table row" in problems[0]


def test_row_is_located_with_up_to_three_spaces_of_indentation(tmp_path: Path) -> None:
    write_corpus(tmp_path, data_model_text=data_model("   " + rule_row()))
    assert decision_114.check(tmp_path) == []


@pytest.mark.parametrize("char", ("\u200b", "\u2060", "\u202e", "\ufeff"))
def test_format_character_on_the_row_is_refused(tmp_path: Path, char: str) -> None:
    row = rule_row().replace("| agent behavioural rule |", f"| agent behavioural rule{char} |", 1)
    write_corpus(tmp_path, data_model_text=data_model(row))
    problems = decision_114.check(tmp_path)
    assert len(problems) == 1 and "decision-114-format-character" in problems[0]


def test_format_character_on_register_row_114_is_refused(tmp_path: Path) -> None:
    conformance = CONFORMANCE_RULED.replace("| 114 |", "| 1\u200d14 |", 1)
    write_corpus(tmp_path, conformance=conformance)
    problems = decision_114.check(tmp_path)
    assert len(problems) == 1 and "decision-114-format-character" in problems[0]
