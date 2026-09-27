#!/usr/bin/env python3
"""Check that decision 114's ruled shape is registered where a reader meets it.

Decision 114 rules that ``agent_policy`` is the home for a rule binding every
agent's behaviour, and that an agent-specific row is tied to the agent(s) it
governs by a `GOVERNS` graph edge, resolved by traversal — superseding the
`scope`/`agent_sub` field pair, which is read nowhere once the edge resolves
(`conformance.md#the-register-of-open-design-decisions`, row 114;
`migration.md#gaps-and-contradictions-the-mapping-exposed`, G34;
`vocabulary.md#rule`).

Marking the register row **ruled** without `data_model.md#concepts`'s
`agent_policy` row actually carrying the `GOVERNS` edge is false readiness: a
reader (or the daemon loader and session rule index the ruling names as the
two consumers, `migration.md` G34) finds the concepts table still describing
the pre-114 shape — a rule reaching its agent by `scope`/`agent_sub` alone,
with no edge, and no note that those fields are superseded. That is exactly
the corpus state this task's own notes found: the ruling is fully argued in
`conformance.md` and `migration.md`, and `vocabulary.md`'s new `rule` entry
states the edge, but `data_model.md#concepts` — the one table a reader
consults for what fields and edges a type actually carries — still lists only
`scope`/`agent_sub` for `agent_policy`, with the edge column silent on
`GOVERNS` entirely.

This binds three things:

1. Register row 114 is **ruled**.
2. `data_model.md#concepts`'s `agent_policy` row's edges column names a
   `GOVERNS` edge to the `agent` it binds.
3. The same row states that `scope`/`agent_sub` is superseded by the edge for
   an agent-specific rule (the row may still document the two fields as
   historical/legacy, but not as the live resolution mechanism with no
   superseding note).

This is a corpus-shape check, the same kind `check_foundation_decision_101.py`
and `check_foundation_decision_117.py` already are: it takes on no traversal
or loader-implementation scope (that is G34's remaining half, which the
ruling text itself says stays open beyond the `agent_policy` case).

Stdlib only; registered in ``conformance.md#mechanical-checks-on-this-directory``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FOUNDATION_DIR = Path("docs/foundation")

_DECISION_ROW_RE = re.compile(r"^\|\s*114\s*\|")

# The concepts table row for the agent behavioural rule type. Matched on the
# leading cell (the concept name) rather than the entity-type cell, since the
# concept name is the stable, human-readable anchor and the entity type
# (`` `agent_policy` ``) appears identically in several rows' prose.
_AGENT_POLICY_ROW_RE = re.compile(
    r"^\|\s*agent behavioural rule\s*\|(?P<entity_type>[^|]*)\|(?P<fields>[^|]*)\|"
    r"(?P<edges>[^|]*)\|"
)

# --- Affirmative-shape requirements (not a negation denylist) --------------
#
# A prior revision of this checker matched *any* mention of the required
# claim and then scanned a fixed character window around it for a hardcoded
# list of negation words ("never", "not", "no", ...). Falco (PR #1321 review,
# comment 5856297532) demonstrated that denylist is bypassed by any denial
# phrased with a synonym outside the list — "without", "lacking", "fails to"
# all pass a sentence like "this row functions without any `GOVERNS` ->
# `agent` edge" as a genuine claim, because the substring a genuine claim
# would contain is *also* present in the prose denying it. Waxwing's
# follow-up review found the same class one level narrower: a negation more
# than ~40 characters from the claim (a longer qualifying clause) was missed
# by the fixed-width window even for a listed word.
#
# Both gaps share one root cause: matching "the concept is mentioned, and no
# denial-shaped text sits nearby" can never be complete against free-form
# English, because there is no finite list of ways to deny a claim. The fix
# is to stop trying to recognize every denial and instead require the
# *specific affirmative shape* the corpus is supposed to carry — text that
# cannot be produced by casually negating a sentence, because it isn't
# freeform prose being scanned for a substring; it's a structural position in
# the table that only a genuine, correctly-directed claim occupies.
#
# 1. GOVERNS edge (edges column): the concepts table's edges column is a
#    ``;``-separated list of ``EDGE_TYPE -> target (...)`` entries. A
#    genuine edge is always list-entry-shaped: it starts the cell or follows
#    a ``;``, with nothing but whitespace before the edge-type token. Prose
#    *about* an edge ("this row functions without any `GOVERNS` -> `agent`
#    edge") has words before the edge-type token that are not list-entry
#    syntax, so it can never match this anchor — not because "without" is on
#    a list, but because a denial is grammatically prose, not a list item.
_GOVERNS_EDGE_ENTRY_RE = re.compile(
    r"(?:^|;)\s*`?GOVERNS`?\s*(?:→|->)\s*`?agent`?", re.I
)

# 2. Superseded claim (fields column): this table's convention is
#    ``field-name` (parenthetical description of that field)`` — the
#    parenthetical immediately following `scope` or `agent_sub` is that
#    field's *own* description, authored by whoever wrote the row, not
#    arbitrary row prose. Requiring the supersession claim to live inside
#    that field's own parenthetical (rather than anywhere in the row) is
#    itself an affirmative-shape requirement: a sentence merely mentioning
#    "scope/agent_sub" and "superseded" elsewhere in the row — including in
#    a denial bolted onto some other field's description — cannot satisfy it.
_FIELD_OWN_PAREN_RE = re.compile(
    r"`(?:scope|agent_sub)`\s*\(((?:[^()]|\([^()]*\))*)\)", re.I
)

# Defense in depth, not the primary mechanism: even inside a field's own
# parenthetical, reject a hedge/denial word. Clause-scoped to that single
# parenthetical (a real punctuation boundary, not a character count), so
# this has no long-distance-negation gap — the whole scoped text is checked,
# however long the clause is. Kept deliberately broader than the two
# confirmed bypasses (not just "without"/"lacking"/"fails to") because it is
# now a secondary check over a narrow, structurally-anchored span rather
# than the sole gate over free-form prose; being one synonym behind here is
# a much smaller residual than being one synonym behind was when this list
# was the only thing standing between a denial and a false green.
_HEDGE_RE = re.compile(
    r"\b(?:never|not|no|isn't|aren't|doesn't|don't|n't|without|lack(?:s|ing)?|"
    r"fail(?:s|ed)?\s+to|absent|nor|neither)\b",
    re.I,
)


def _has_governs_edge_entry(edges_cell: str) -> bool:
    """True when the edges cell contains a genuine ``GOVERNS -> agent`` entry.

    Structural, not lexical: the match must be list-entry-shaped (cell-start
    or after ``;``, then the edge-type token). Prose describing or denying an
    edge is never list-entry-shaped, so it cannot satisfy this regardless of
    what words it uses.
    """
    return bool(_GOVERNS_EDGE_ENTRY_RE.search(edges_cell))


def _has_affirmative_superseded_claim(fields_cell: str) -> bool:
    """True when ``scope``'s or ``agent_sub``'s own parenthetical affirms
    that it is superseded, with no hedge/denial word in that parenthetical.
    """
    for match in _FIELD_OWN_PAREN_RE.finditer(fields_cell):
        description = match.group(1)
        if re.search(r"supersed\w*", description, re.I) and not _HEDGE_RE.search(
            description
        ):
            return True
    return False


class CorpusProblem(Exception):
    """The decision-114 corpus files are missing or unreadable."""


class AmbiguousCorpusRow(Exception):
    """More than one candidate row was found where exactly one is required."""


def decision_114_row(conformance_text: str) -> tuple[int, list[str]] | None:
    for no, line in enumerate(conformance_text.splitlines(), 1):
        if not _DECISION_ROW_RE.match(line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        return no, cells
    return None


def _iter_concepts_section_lines(data_model_text: str) -> "list[tuple[int, str]]":
    """Yield (line no, line) pairs for lines inside the ``## Concepts`` section.

    Scoped to that section specifically — stopping at the next ``## ``
    heading — rather than the whole document, so a second table sharing the
    concepts-row shape elsewhere (an appendix, migration notes, a
    before/after comparison) is never mistaken for the live concepts row.
    """
    lines: list[tuple[int, str]] = []
    in_concepts = False
    for no, line in enumerate(data_model_text.splitlines(), 1):
        if line.startswith("## "):
            in_concepts = line.strip() == "## Concepts"
            continue
        if in_concepts:
            lines.append((no, line))
    return lines


def agent_policy_concepts_row(
    data_model_text: str,
) -> tuple[int, str, str, str] | None:
    """Return (line no, fields cell, edges cell, whole row) for the row.

    Requires exactly one matching row within ``## Concepts``. Zero matches
    returns ``None`` (handled by the caller as "no row"); more than one
    raises ``AmbiguousCorpusRow`` rather than silently taking the first —
    a compliant decoy placed first must not be able to mask a broken real
    row placed second.
    """
    matches: list[tuple[int, str, str, str]] = []
    for no, line in _iter_concepts_section_lines(data_model_text):
        match = _AGENT_POLICY_ROW_RE.match(line)
        if match:
            matches.append((no, match.group("fields"), match.group("edges"), line))
    if not matches:
        return None
    if len(matches) > 1:
        line_nos = ", ".join(str(no) for no, _, _, _ in matches)
        raise AmbiguousCorpusRow(
            "found more than one `agent behavioural rule` concepts-table row "
            f"within ## Concepts (lines {line_nos}); expected exactly one"
        )
    return matches[0]


def check_concepts_row(
    path: Path, row_no: int, fields_cell: str, edges_cell: str
) -> list[str]:
    problems: list[str] = []
    if not _has_governs_edge_entry(edges_cell):
        problems.append(
            f"{path}:{row_no}: decision-114-data-model — `agent_policy` "
            "concepts row's edges column is missing a `GOVERNS` → `agent` "
            "edge while register row 114 is **ruled** (a mention of GOVERNS "
            "in prose does not count — the edges column must carry it as a "
            "`;`-separated edge-list entry, e.g. '`GOVERNS` -> `agent` (...)')"
        )
    if not _has_affirmative_superseded_claim(fields_cell):
        problems.append(
            f"{path}:{row_no}: decision-114-data-model — `agent_policy` "
            "concepts row's `scope`/`agent_sub` field description does not "
            "affirmatively state that it is superseded by the `GOVERNS` "
            "edge for an agent-specific rule (the claim must live inside "
            "that field's own parenthetical description, with no hedge or "
            "denial word in it)"
        )
    return problems


def check(root: Path) -> list[str]:
    fdir = root / FOUNDATION_DIR
    conformance_path = fdir / "conformance.md"
    data_model_path = fdir / "data_model.md"
    for path in (conformance_path, data_model_path):
        if not path.is_file():
            raise CorpusProblem(
                f"expected {conformance_path} and {data_model_path} under --root {root}"
            )

    conformance_text = conformance_path.read_text(encoding="utf-8")
    row = decision_114_row(conformance_text)
    if row is None:
        return [
            f"{conformance_path}:1: decision-114-register — no register row "
            'beginning "| 114 |"'
        ]

    row_no, cells = row
    if len(cells) < 5 or "**ruled**" not in cells[4].lower():
        # Not yet ruled: nothing downstream is required yet (mirrors the
        # decision-101/117 no-op-before-ruled pattern).
        return []

    data_model_text = data_model_path.read_text(encoding="utf-8")
    try:
        concepts_row = agent_policy_concepts_row(data_model_text)
    except AmbiguousCorpusRow as exc:
        return [f"{data_model_path}:1: decision-114-data-model — {exc}"]
    if concepts_row is None:
        return [
            f"{data_model_path}:1: decision-114-data-model — no concepts-table "
            "row for `agent behavioural rule` (`agent_policy`) while register "
            "row 114 is **ruled**"
        ]

    concepts_row_no, fields_cell, edges_cell, _whole_row = concepts_row
    return check_concepts_row(data_model_path, concepts_row_no, fields_cell, edges_cell)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    args = parser.parse_args(argv)

    try:
        problems = check(args.root)
    except CorpusProblem as exc:
        print(f"decision 114 check: {exc}", file=sys.stderr)
        return 1

    for problem in problems:
        print(problem)
    print(f"decision 114 check: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
