#!/usr/bin/env python3
"""Check that decision 114's ruled shape is registered where a reader meets it.

Decision 114 rules that a rule binding an agent's behaviour is tied to the agent(s) it governs by a
`GOVERNS` graph edge, resolved by traversal — superseding the `scope`/`agent_sub` field pair, which is
read nowhere once the edge resolves (`conformance.md#the-register-of-open-design-decisions`, row 114;
`migration.md#gaps-and-contradictions-the-mapping-exposed`, G34; `vocabulary.md#rule`). Decision 120
later moved the rule to the core `rule` type and kept 114's binding, so the row this reads is still the
concepts table's *agent behavioural rule* row, whatever its entity-type cell names.

Marking the register row **ruled** without that row carrying the edge is false readiness: a reader (or the
daemon loader and session rule index the ruling names as its two consumers) finds the concepts table still
describing the pre-114 shape.

This binds three things:

1. Register row 114 is **ruled**.
2. The row's edges column carries a `GOVERNS` → `agent` entry whose own parenthetical is, verbatim, one of
   ``APPROVED_GOVERNS_AGENT_TEXTS``.
3. The row's `scope` and `agent_sub` field descriptions are, verbatim, one of
   ``APPROVED_SCOPE_TEXTS`` and ``APPROVED_AGENT_SUB_TEXTS`` respectively.

**Why exact text and not a reading of the prose.** Seven review rounds on PR #1321 each found a new way to
deny the claim in ordinary English — a negation word the list lacked, a negation too far from the claim,
a copula or hedge adverb ("seems", "arguably", "supposedly") in front of it, a future tense ("will be
superseded"), a pending-review aside — and each fix grew a denylist that the next round was one synonym
ahead of. There is no finite list of ways to deny a sentence, so a checker that reads free prose for a
denial cannot be complete. This one does not read the prose at all: the claim-bearing spans must be the
approved sentences exactly (whitespace-normalized), so no hedge, denial, or aside can be added to them
without the check going red. Changing the wording means changing the approved text in this file in the
same PR, which puts the new wording in front of review rather than past it.

**What it does not verify.** Prose elsewhere in the row — another field's description, another edge's
parenthetical, the derived-reads column — is not read. A contradiction written there is review's to
catch, not this check's; the check guarantees that the three spans that carry the ruled claim say
exactly what was ruled. It also requires every edges-column entry to be an edge of a type in
``ALLOWED_EDGE_TYPES`` (`` `TYPE` → target ``), so a bare prose entry cannot sit in the edge list, and
exactly one `GOVERNS` → `agent` entry, so a second one cannot carry a denial beside the approved one.

This is a corpus-shape check, the same kind `check_foundation_decision_101.py` and
`check_foundation_decision_117.py` already are: it takes on no traversal or loader-implementation scope.

Stdlib only; registered in ``conformance.md#mechanical-checks-on-this-directory``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FOUNDATION_DIR = Path("docs/foundation")

_DECISION_ROW_RE = re.compile(r"^\|\s*114\s*\|")

# The approved claim sentences. Each tuple is closed: a span passes only by equalling one entry after
# whitespace normalization. Add an entry (or replace one) in the same PR that changes the row's wording.
APPROVED_GOVERNS_AGENT_TEXTS = (
    "the agent this rule binds, resolved by traversal in the daemon loader and the session rule index "
    "— decision 114, ruled 2026-09-25, `migration.md#gaps-and-contradictions-the-mapping-exposed`, G34",
)
APPROVED_SCOPE_TEXTS = (
    "legacy, **closed**: `global`, `swarm`, or `agent` — superseded by the `GOVERNS` edge, decision "
    "114, and read nowhere once the edge resolves",
)
APPROVED_AGENT_SUB_TEXTS = (
    "legacy, superseded the same way: the agent a rule binds is the `GOVERNS` edge's target",
)


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _approved(text: str, approved: tuple[str, ...]) -> bool:
    return _normalize(text) in {_normalize(a) for a in approved}


def _extract_balanced_paren(text: str, open_at: int) -> str | None:
    """Return the content between a balanced ``(...)`` starting at ``text[open_at]``, or ``None``.

    Handles arbitrary nesting depth, unlike a fixed-depth regex group.
    """
    assert text[open_at] == "("
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_at + 1 : i]
    return None


def _split_top_level(cell: str, sep: str = ";") -> list[str]:
    """Split ``cell`` on ``sep`` outside any parentheses."""
    parts: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(cell):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        elif ch == sep and depth == 0:
            parts.append(cell[start:i])
            start = i + 1
    parts.append(cell[start:])
    return parts


# The concepts table row for the agent behavioural rule type, matched on the leading cell (the concept
# name): the stable anchor, since the entity-type cell changed from `agent_policy` to `rule` (decision 120).
_AGENT_POLICY_ROW_RE = re.compile(
    r"^\|\s*agent behavioural rule\s*\|(?P<entity_type>[^|]*)\|(?P<fields>[^|]*)\|"
    r"(?P<edges>[^|]*)\|"
)

# The relationship types this row may carry, closed: an entry naming any other backticked token (a
# `NOTE`, a `CAVEAT`) is prose dressed as an edge. Add a type here in the same PR that adds it to the row.
ALLOWED_EDGE_TYPES = frozenset({"GOVERNS", "PART_OF", "SUPERSEDES", "REFERS_TO"})

# An edges-column entry: a backticked relationship type, an arrow, then the target.
_EDGE_ENTRY_RE = re.compile(r"^\s*`(?P<type>[A-Z_]+)`\s*(?:→|->|←|<-)\s*\S")

# The GOVERNS → agent entry. `(?!\w)` after the target rejects `agent_sub` and any other longer
# identifier, while allowing the closing backtick, whitespace, or `(`.
_GOVERNS_AGENT_RE = re.compile(r"^\s*`GOVERNS`\s*(?:→|->)\s*`?agent`?(?!\w)")

_FIELD_OWN_PAREN_RE = re.compile(r"`(scope|agent_sub)`\s*(\()")


def _malformed_edge_entries(edges_cell: str) -> list[str]:
    malformed = []
    for entry in _split_top_level(edges_cell):
        if not entry.strip():
            continue
        match = _EDGE_ENTRY_RE.match(entry)
        if not match or match.group("type") not in ALLOWED_EDGE_TYPES:
            malformed.append(entry.strip())
    return malformed


def _governs_agent_entry_is_approved(entry_rest: str) -> bool:
    """``entry_rest`` is what follows the target: exactly one approved parenthetical, then nothing."""
    rest = entry_rest.strip()
    if not rest.startswith("("):
        return False
    own_paren = _extract_balanced_paren(rest, 0)
    if own_paren is None or rest[len(own_paren) + 2:].strip():
        return False
    return _approved(own_paren, APPROVED_GOVERNS_AGENT_TEXTS)


def _has_governs_edge_entry(edges_cell: str) -> bool:
    """True when the edges cell carries exactly one ``GOVERNS`` → ``agent`` entry, and it is approved.

    A second such entry fails it even beside an approved one, the same rule the fields column applies
    to a second `scope`/`agent_sub` description: otherwise a denial rides in as another entry.
    """
    entries = [
        entry[match.end():]
        for entry in _split_top_level(edges_cell)
        if (match := _GOVERNS_AGENT_RE.match(entry))
    ]
    return len(entries) == 1 and _governs_agent_entry_is_approved(entries[0])


def _field_descriptions(fields_cell: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {"scope": [], "agent_sub": []}
    for match in _FIELD_OWN_PAREN_RE.finditer(fields_cell):
        description = _extract_balanced_paren(fields_cell, match.start(2))
        if description is not None:
            out[match.group(1)].append(description)
    return out


def _has_affirmative_superseded_claim(fields_cell: str) -> bool:
    """True when every `scope` and `agent_sub` description in the cell is an approved text, and both
    fields carry one. A second, unapproved description of either field fails it too, so a denial cannot
    ride beside the approved sentence as another mention of the same field."""
    found = _field_descriptions(fields_cell)
    return (
        bool(found["scope"])
        and bool(found["agent_sub"])
        and all(_approved(d, APPROVED_SCOPE_TEXTS) for d in found["scope"])
        and all(_approved(d, APPROVED_AGENT_SUB_TEXTS) for d in found["agent_sub"])
    )


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
    for entry in _malformed_edge_entries(edges_cell):
        problems.append(
            f"{path}:{row_no}: decision-114-data-model — agent behavioural rule row's edges column "
            f"has an entry that is not an edge of a type in ALLOWED_EDGE_TYPES (`` `TYPE` → target (...) ``): "
            f"{entry[:80]!r}"
        )
    if not _has_governs_edge_entry(edges_cell):
        problems.append(
            f"{path}:{row_no}: decision-114-data-model — agent behavioural rule row's edges column "
            "does not carry exactly one `GOVERNS` → `agent` entry with an approved parenthetical while "
            "register row 114 is **ruled**. The entry's parenthetical must equal one of APPROVED_GOVERNS_AGENT_TEXTS in "
            "execution/scripts/check_foundation_decision_114.py; to reword it, change the approved "
            "text there in the same PR"
        )
    if not _has_affirmative_superseded_claim(fields_cell):
        problems.append(
            f"{path}:{row_no}: decision-114-data-model — agent behavioural rule row's `scope` and "
            "`agent_sub` descriptions are not the approved statements that they are superseded by the "
            "`GOVERNS` edge. Each must equal one of APPROVED_SCOPE_TEXTS / APPROVED_AGENT_SUB_TEXTS in "
            "execution/scripts/check_foundation_decision_114.py; to reword it, change the approved "
            "text there in the same PR"
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
            "row for `agent behavioural rule` while register "
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
