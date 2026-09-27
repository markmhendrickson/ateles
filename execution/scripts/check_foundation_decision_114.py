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

_GOVERNS_EDGE_RE = re.compile(r"`?GOVERNS`?\s*(?:→|->)\s*`?agent`?", re.I)
_SUPERSEDED_RE = re.compile(
    r"scope`?\s*/\s*`?agent_sub`?[^.;]{0,80}supersed"
    r"|supersed[^.;]{0,80}`?scope`?\s*/\s*`?agent_sub`?"
    r"|`?scope`?\s*/\s*`?agent_sub`?[^.;]{0,80}read\s+nowhere",
    re.I,
)
_LEGACY_SCOPE_FIELDS_RE = re.compile(r"`?agent_sub`?", re.I)


class CorpusProblem(Exception):
    """The decision-114 corpus files are missing or unreadable."""


def decision_114_row(conformance_text: str) -> tuple[int, list[str]] | None:
    for no, line in enumerate(conformance_text.splitlines(), 1):
        if not _DECISION_ROW_RE.match(line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        return no, cells
    return None


def agent_policy_concepts_row(data_model_text: str) -> tuple[int, str, str] | None:
    """Return (line no, edges cell, whole row) for the concepts-table row.

    Scoped to lines that actually match the concepts-table row shape rather
    than the whole document, so a mention of ``agent behavioural rule`` in
    prose elsewhere (there is none today, but the check should not depend on
    that) is never mistaken for the table row itself.
    """
    for no, line in enumerate(data_model_text.splitlines(), 1):
        match = _AGENT_POLICY_ROW_RE.match(line)
        if match:
            return no, match.group("edges"), line
    return None


def check_concepts_row(path: Path, row_no: int, edges_cell: str, whole_row: str) -> list[str]:
    problems: list[str] = []
    if not _GOVERNS_EDGE_RE.search(edges_cell):
        problems.append(
            f"{path}:{row_no}: decision-114-data-model — `agent_policy` "
            "concepts row's edges column is missing a `GOVERNS` → `agent` "
            "edge while register row 114 is **ruled**"
        )
    if not _SUPERSEDED_RE.search(whole_row):
        problems.append(
            f"{path}:{row_no}: decision-114-data-model — `agent_policy` "
            "concepts row does not state that `scope`/`agent_sub` is "
            "superseded by the `GOVERNS` edge for an agent-specific rule"
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
    concepts_row = agent_policy_concepts_row(data_model_text)
    if concepts_row is None:
        return [
            f"{data_model_path}:1: decision-114-data-model — no concepts-table "
            "row for `agent behavioural rule` (`agent_policy`) while register "
            "row 114 is **ruled**"
        ]

    concepts_row_no, edges_cell, whole_row = concepts_row
    return check_concepts_row(data_model_path, concepts_row_no, edges_cell, whole_row)


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
