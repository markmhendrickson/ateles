#!/usr/bin/env python3
"""render_data_model.py — the contract ``data_model.md#rendering`` states, and the check that binds today.

``docs/foundation/data_model.md#rendering`` names this script as the renderer of that document's two
tables from the schema registry, with ``--check`` exiting non-zero when a table on disk differs from
the registry. ``status.md`` recorded it absent from ``origin/main`` since revision 6, and it was still
absent when this file was added — so the contract existed, nothing implemented it, and nothing failed.

**What this file implements, and what it deliberately does not.** The registry-rendering half of the
contract needs a live record, and every check registered in
``conformance.md#mechanical-checks-on-this-directory`` runs stdlib-only with no Neotoma. A renderer
that could only run where ``NEOTOMA_BASE_URL`` is set would be skipped on exactly the machines and CI
lanes where the drift it guards against lands, which is the "reports without binding" defect
(``principles.md``, principle 1) the foundation names. So the halves are split by what can bind:

* ``--check`` (and the default render) enforce the half that needs **no** record — **governance-type
  coverage**: every governance entity type the foundation's own documents name must have a row in
  ``data_model.md#concepts``. This is the half that was actually broken. ``agent_policy`` is named by
  ``conformance.md``'s authority table as the authoritative home of an agent behavioural rule and is
  tested by four ``conformance_suite.md`` rows, and it was declared **zero times** in the document
  that projects "entity types, fields, and edge types the design names". ``swarm_roster`` was the
  same, and its absence was a registered gap (``migration.md``, G21).
* The registry half — regenerating the table cells from the registered types, their fields and
  versions — is **not** implemented here and is not claimed to be. ``status.md`` carries that, and a
  ``--check`` that silently covered only part of its contract would be worse than one that says which
  part it covers. ``--check`` prints the uncovered half so a reader of a green run knows what was and
  was not judged.

**Where the expected set comes from.** Not a literal list in this file: a hardcoded roster of
governance types is the copy-that-drifts ``CLAUDE.md`` names and ``conformance.md``'s decision 67
assigns to a registry read. The set is extracted from the foundation corpus itself — the
``conformance_suite.md`` row (WM-22) that enumerates the eight governance types an agent writes to,
which is the closed list ``work_model.md`` and ``gates_and_workflows.md`` both restate. A type added
to that enumeration is therefore covered by this check the day it is added, with no edit here.

Absence of the source row is a **failure**, never a pass: a check whose input vanished has not run
(``check_foundation_anchors.py``'s ``MissingCorpus`` sets the precedent, and exit 2 distinguishes
"did not run" from exit 1's "found a defect").

Usage:
    render_data_model.py            # report coverage; exit 1 on a governance type with no row
    render_data_model.py --check    # same, and the mode scripts/lint.sh invokes

Stdlib only; registered in ``conformance.md#mechanical-checks-on-this-directory``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FOUNDATION = Path("docs/foundation")
DATA_MODEL = FOUNDATION / "data_model.md"
SUITE = FOUNDATION / "conformance_suite.md"

# The WM-22 row enumerates the governance types an agent writes to, as a dashed clause naming its own
# count: "the eight governance types — `agent`, `agent_policy`, …, the registry, `intake_rule` —".
# Only that clause is read, not the whole row: the row also names `task_policy`, which it explicitly
# calls an input type and not a governance one, and a whole-row scrape would pull it in.
#
# The count is asserted against what was extracted so a silent shrink of the enumeration cannot
# silently shrink what this check covers. "the registry" is one of the eight and is deliberately not
# an entity type — it is the schema registry itself — so it is counted and then excluded from the
# set of types that need a concepts row.
_ENUM_RE = re.compile(r"the (\w+) governance types\s*[—-]\s*(.+?)\s*[—-]\s*and to a", re.S)
_TICKED = re.compile(r"`([a-z_]+)`")
_COUNT_WORDS = {
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12,
}
# Members of the enumeration that are not entity types and so cannot carry a concepts row.
_NOT_ENTITY_TYPES = {"the registry"}


class CheckDidNotRun(Exception):
    """An input this check reads is missing, so nothing was judged. Exit 2, never 0."""


def _read(path: Path) -> str:
    if not path.is_file():
        raise CheckDidNotRun(f"missing {path}; nothing was checked")
    return path.read_text(encoding="utf-8")


def governance_types(suite_text: str) -> set[str]:
    """The governance entity types, extracted from conformance_suite.md's own enumeration."""
    m = _ENUM_RE.search(suite_text)
    if not m:
        raise CheckDidNotRun(
            f"no governance-type enumeration in {SUITE} (expected WM-22's "
            '"the <count> governance types — ... — and to a" clause); the set this check reads is '
            "gone, so nothing was checked. If the enumeration moved or was reworded, point this "
            "check at its new home rather than letting it pass on an empty set."
        )
    count_word, clause = m.group(1).lower(), m.group(2)
    # Members as written, so a non-type member ("the registry") is counted before it is dropped.
    members = [p.strip() for p in clause.split(",") if p.strip()]
    expected = _COUNT_WORDS.get(count_word)
    if expected is not None and len(members) != expected:
        raise CheckDidNotRun(
            f"WM-22 says {count_word} ({expected}) governance types but its enumeration lists "
            f"{len(members)} ({members}); the row and its own count disagree, so the set this check "
            "would enforce is not trustworthy and nothing was checked."
        )
    found = {
        t
        for member in members
        if member not in _NOT_ENTITY_TYPES
        for t in _TICKED.findall(member)
    }
    if not found:
        raise CheckDidNotRun(
            f"WM-22's enumeration in {SUITE} names no backticked entity types; nothing was checked"
        )
    return found


def declared_types(data_model_text: str) -> set[str]:
    """Entity types carrying a row in the concepts table.

    Read from the `Entity type` cell (column 2) of each table row, not from anywhere the name might
    merely be mentioned: `agent_policy` was cited in prose across the corpus the whole time it had no
    row, so a whole-document substring search would have reported the gap as covered.
    """
    start = data_model_text.find("<!-- rendered: data_model concepts -->")
    end = data_model_text.find("<!-- /rendered -->", start)
    if start == -1 or end == -1:
        raise CheckDidNotRun(
            f"no concepts table markers in {DATA_MODEL}; nothing was checked"
        )
    out: set[str] = set()
    for line in data_model_text[start:end].splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        out.update(_TICKED.findall(cells[1]))
    return out


def check(root: Path) -> list[str]:
    expected = governance_types(_read(root / SUITE))
    declared = declared_types(_read(root / DATA_MODEL))
    missing = sorted(expected - declared)
    return [
        f"{DATA_MODEL}: governance type `{t}` is named by {SUITE} (WM-22) and has no row in "
        f"#concepts — a type the design depends on that this document does not declare"
        for t in missing
    ]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="exit 1 on an undeclared governance type")
    ap.add_argument("--root", default=".", type=Path)
    args = ap.parse_args()

    try:
        problems = check(args.root)
    except CheckDidNotRun as exc:
        print(f"render_data_model: DID NOT RUN — {exc}", file=sys.stderr)
        return 2

    if problems:
        for p in problems:
            print(f"render_data_model: {p}", file=sys.stderr)
        return 1

    print(
        "render_data_model: every governance type conformance_suite.md names has a row in "
        "data_model.md#concepts. NOT checked here: the table cells against the live schema "
        "registry (data_model.md#rendering's other half — needs a record; see status.md)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
