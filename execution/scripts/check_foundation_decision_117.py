#!/usr/bin/env python3
"""Check that decision 117's ruled shape is registered where readers meet it.

Decision 117 rules that a grant's capability tuple gains an optional
``relationship_types[]`` beside its ``entity_types[]``, with absent or empty
meaning default-deny for edge writes. Marking the register row **ruled**
without ``authority_model.md#grants`` actually carrying that tuple text is
false readiness: a reader (or a later editor) trusts the register's status
token and finds the corpus still describes the pre-117, entity-only shape.

This binds five things the ruling's own text and its Engineering build
checklist name:

1. Register row 117 is **ruled**.
2. ``authority_model.md#grants`` carries the widened tuple — an optional
   ``relationship_types[]`` beside ``entity_types[]`` — with default-deny-on-
   absent/empty language.
3. The closed-list-of-eight section in ``gates_and_workflows.md`` either
   names ``principal_binding``, ``delegation_edge``, and ``ownership_grant``
   as governance types, or the ruling corpus states the list stays
   entity-only and names the alternate relationship-type mechanism (the
   widened tuple in #2) — both readings are legitimate; only silence on the
   question is not, and there is none here, since row 117's own text states
   the deferral in `authority_model.md`.
4. The bootstrap step-3 admission sentence — that the operator's own
   credential binding, written at bootstrap before any grant exists to admit
   it, stays admitted the way it is admitted today — is present.
5. Once row 117 is ruled, a legacy entity-types-only tuple (the pre-117
   shape, with no relationship-type term at all) in ``authority_model.md#grants``
   is refused, not silently accepted as still describing the current rule.

This does not assert live enforcement: it is a corpus-shape check, the same
kind ``check_foundation_decision_101.py`` and ``check_foundation_decision_78.py``
already are, and it takes on none of the resolver/enforcement scope those
checkers also decline (no G25 registry read, no runtime grant evaluation).

Stdlib only; registered in ``conformance.md#mechanical-checks-on-this-directory``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FOUNDATION_DIR = Path("docs/foundation")

_DECISION_ROW_RE = re.compile(r"^\|\s*117\s*\|")

# The widened tuple: an optional relationship_types[] beside entity_types[],
# with default-deny-on-absent/empty language. Matched loosely enough to
# survive paraphrase across conformance.md and authority_model.md (which
# state it in slightly different words) but anchored on the two field names
# and the deny-on-absent-or-empty meaning, since those are the load-bearing
# facts of the ruling rather than incidental phrasing.
_RELATIONSHIP_TYPES_FIELD_RE = re.compile(r"`?relationship_types\[\]`?")
_ENTITY_TYPES_FIELD_RE = re.compile(r"`?entity_types\[\]`?")
_DEFAULT_DENY_RE = re.compile(
    r"absent(?:\s+or\s+empty)?\s+mean(?:s|ing)\s+default-deny"
    r"|default-deny[^.;]{0,40}absent(?:\s+or\s+empty)?",
    re.I,
)

# Governance-list placement (item 3): either the closed list names all three
# edge types, or the corpus states the list stays entity-only and names the
# widened-tuple mechanism as the alternate. A silent list — mentioning none
# of the three terms and no deferral statement — is the failure this catches.
GOVERNANCE_TERMS = ("principal_binding", "delegation_edge", "ownership_grant")
_DEFERRAL_RE = re.compile(
    r"(?:whether\s+)?`?principal_binding`?\s+and\s+`?delegation_edge`?\s+"
    r"join\s+the\s+closed\s+governance\s+list",
    re.I,
)

# Bootstrap step-3 admission (item 4): the operator's own credential binding,
# written before any grant exists to admit it, stays admitted as it is today.
_BOOTSTRAP_STEP3_RE = re.compile(
    r"bootstrap\s+step\s*3[^.;]{0,40}written\s+before\s+any\s+grant\s+exists"
    r"[^.;]{0,80}stays\s+admitted\s+the\s+way\s+it\s+is\s+admitted\s+today",
    re.I,
)

# The pre-117 legacy shape: a capability tuple stated as entity types and
# repositories only, with no relationship-type term anywhere. Matched as the
# grants section carrying operation/entity_types/repositories language and
# nowhere mentioning relationship_types.
_LEGACY_TUPLE_RE = re.compile(
    r"entity_types\[\][^.\n]{0,60}repositories|repositories[^.\n]{0,60}entity_types\[\]",
    re.I,
)

GRANTS_HEADING = "## Grants"


class CorpusProblem(Exception):
    """The decision-117 corpus files are missing or unreadable."""


def decision_117_row(conformance_text: str) -> tuple[int, list[str]] | None:
    for no, line in enumerate(conformance_text.splitlines(), 1):
        if not _DECISION_ROW_RE.match(line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        return no, cells
    return None


def grants_section(authority_text: str) -> tuple[int, str] | None:
    lines = authority_text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() != GRANTS_HEADING:
            continue
        body: list[str] = []
        for follow in lines[i + 1 :]:
            if follow.startswith("## "):
                break
            body.append(follow)
        return i + 1, "\n".join(body)
    return None


def governance_list_section(gates_text: str) -> tuple[int, str] | None:
    marker = "**Governance writes are actions, and the governance types are one closed list"
    idx = gates_text.find(marker)
    if idx == -1:
        return None
    line_no = gates_text.count("\n", 0, idx) + 1
    # Section runs to the next blank-line-delimited paragraph break at a
    # bold lead (the next rule), matching how this document composes rules
    # as consecutive bold-led paragraphs rather than headed subsections.
    tail = gates_text[idx:]
    next_para = re.search(r"\n\n\*\*", tail[2:])
    end = idx + 2 + next_para.start() if next_para else len(gates_text)
    return line_no, gates_text[idx:end]


def check_grants_tuple(path: Path, row_no: int, body: str) -> list[str]:
    problems: list[str] = []
    if not _RELATIONSHIP_TYPES_FIELD_RE.search(body):
        problems.append(
            f"{path}:{row_no}: decision-117-grants — `#grants` section missing "
            "`relationship_types[]`"
        )
    if not _ENTITY_TYPES_FIELD_RE.search(body):
        problems.append(
            f"{path}:{row_no}: decision-117-grants — `#grants` section missing "
            "`entity_types[]` (the field the ruling widens beside)"
        )
    if not _DEFAULT_DENY_RE.search(body):
        problems.append(
            f"{path}:{row_no}: decision-117-grants — `#grants` section missing "
            "default-deny-on-absent/empty language for relationship types"
        )
    return problems


def check_governance_list_placement(
    gates_path: Path, gates_text: str, authority_path: Path, authority_text: str
) -> list[str]:
    """Item 3: closed list places the three edge types, or defers explicitly.

    Placement is checked against the closed-list section itself
    (`gates_and_workflows.md`'s "one closed list" paragraph), not against
    prose elsewhere that merely names the three terms in another context —
    `governance_list_section` isolates that paragraph so a placement claim is
    read from the one place the corpus says the list actually lives ("stated
    here and nowhere else"). If that section does not place all three, the
    deferral is checked in the ruling text (`authority_model.md`'s mirrored
    decision-117 section): the closed list staying silent on relationship
    types is the gap ateles#925 raised, and the ruling's own "what this does
    not settle" paragraph is where an explicit entity-only-for-now deferral,
    naming the widened tuple as the alternate mechanism, is actually stated.
    Either reading of item 3 is acceptable; silence on both is not.
    """
    section = governance_list_section(gates_text)
    section_text = section[1] if section is not None else ""
    if all(term in section_text for term in GOVERNANCE_TERMS):
        return []
    if _DEFERRAL_RE.search(authority_text):
        return []
    return [
        f"{gates_path}:1: decision-117-governance-list — neither the closed "
        "governance-list section places `principal_binding`/`delegation_edge`/"
        f"`ownership_grant`, nor does {authority_path} state the deferral "
        "(the list stays entity-only for now, with the widened "
        "relationship_types[] tuple as the named alternate mechanism)"
    ]


def check_bootstrap_admission(path: Path, text: str) -> list[str]:
    if _BOOTSTRAP_STEP3_RE.search(text):
        return []
    return [
        f"{path}:1: decision-117-bootstrap — ruling text missing the bootstrap "
        "step-3 admission sentence (the operator's own credential binding, "
        "written before any grant exists to admit it, stays admitted the way "
        "it is admitted today)"
    ]


def check_legacy_tuple_refused(path: Path, row_no: int, body: str) -> list[str]:
    """Item 5: once ruled, a legacy entity-types-only tuple must not stand.

    A `#grants` section that still states the capability tuple as entity
    types and repositories only, with no relationship-type term anywhere in
    the section, is the pre-117 shape — acceptable before the row was ruled,
    a defect once it is.
    """
    if not _LEGACY_TUPLE_RE.search(body):
        return []
    if _RELATIONSHIP_TYPES_FIELD_RE.search(body):
        return []
    return [
        f"{path}:{row_no}: decision-117-legacy-tuple — `#grants` section "
        "still states the capability tuple as entity types and repositories "
        "only, with no relationship-type term, though register row 117 is "
        "**ruled**"
    ]


def check(root: Path) -> list[str]:
    fdir = root / FOUNDATION_DIR
    conformance_path = fdir / "conformance.md"
    authority_path = fdir / "authority_model.md"
    gates_path = fdir / "gates_and_workflows.md"
    for path in (conformance_path, authority_path, gates_path):
        if not path.is_file():
            raise CorpusProblem(
                f"expected {conformance_path}, {authority_path}, and "
                f"{gates_path} under --root {root}"
            )

    conformance_text = conformance_path.read_text(encoding="utf-8")
    row = decision_117_row(conformance_text)
    if row is None:
        return [
            f"{conformance_path}:1: decision-117-register — no register row "
            'beginning "| 117 |"'
        ]

    row_no, cells = row
    if len(cells) < 5 or "**ruled**" not in cells[4].lower():
        # Not yet ruled: nothing downstream is required yet (mirrors the
        # decision-101/78 pattern of a no-op shape before the row rules).
        return []

    problems: list[str] = []

    authority_text = authority_path.read_text(encoding="utf-8")
    grants = grants_section(authority_text)
    if grants is None:
        problems.append(
            f"{authority_path}:1: decision-117-grants — no `{GRANTS_HEADING}` "
            "section found while register row 117 is **ruled**"
        )
    else:
        grants_line_no, grants_body = grants
        problems.extend(check_grants_tuple(authority_path, grants_line_no, grants_body))
        problems.extend(
            check_legacy_tuple_refused(authority_path, grants_line_no, grants_body)
        )

    gates_text = gates_path.read_text(encoding="utf-8")
    problems.extend(
        check_governance_list_placement(
            gates_path, gates_text, authority_path, authority_text
        )
    )
    problems.extend(check_bootstrap_admission(authority_path, authority_text))

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    args = parser.parse_args(argv)

    try:
        problems = check(args.root)
    except CorpusProblem as exc:
        print(f"decision 117 check: {exc}", file=sys.stderr)
        return 1

    for problem in problems:
        print(problem)
    print(f"decision 117 check: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
