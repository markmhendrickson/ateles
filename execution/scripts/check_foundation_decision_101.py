#!/usr/bin/env python3
"""Check that decision 101's ruled shape is registered in data_model.md.

Decision 101 rules that ``principal_binding`` carries credential fields on the
edge (one edge per credential). Marking the register row **ruled** without
amending ``data_model.md#relationships`` is false readiness for G17 sequencing:
the status token would unblock stage-1 registration before a writable shape
exists. This check binds the two effects.

Stdlib only; registered in ``conformance.md#mechanical-checks-on-this-directory``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FOUNDATION_DIR = Path("docs/foundation")
_DECISION_ROW_RE = re.compile(r"^\|\s*101\s*\|")
_PRINCIPAL_BINDING_ROW_RE = re.compile(
    r"^\|\s*`principal_binding`\s*\|(?P<body>.*)\|\s*$"
)

# Tokens the relationships row must carry when row 101 is ruled.
REQUIRED_FIELD_TOKENS = (
    "credential_kind",
    "credential_value",
    "credential_issuer",
)
# Expiry may appear as expires_at or the word expiry.
EXPIRY_TOKEN_RE = re.compile(r"expires_at|\bexpiry\b", re.I)
# One edge per credential / several edges cardinality.
CARDINALITY_RE = re.compile(
    r"one edge per credential|several edges|many edges", re.I
)
# Resolution: match kind+value → principal.
RESOLUTION_RE = re.compile(
    r"(kind\+value|match.*kind|resolv).{0,80}principal"
    r"|credential-to-principal resolution",
    re.I,
)
LEGACY_ENDPOINTS_RE = re.compile(r"agent\s*→\s*principal", re.I)
LEGACY_MEANING_RE = re.compile(
    r"the principal the agent acts as", re.I
)


class CorpusProblem(Exception):
    """The decision-101 corpus files are missing or unreadable."""


def decision_101_row(conformance_text: str) -> tuple[int, list[str]] | None:
    for no, line in enumerate(conformance_text.splitlines(), 1):
        if not _DECISION_ROW_RE.match(line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        return no, cells
    return None


def principal_binding_relationships_row(
    data_model_text: str,
) -> tuple[int, str] | None:
    in_relationships = False
    for no, line in enumerate(data_model_text.splitlines(), 1):
        if line.startswith("## Relationships"):
            in_relationships = True
            continue
        if in_relationships and line.startswith("## "):
            break
        if not in_relationships:
            continue
        match = _PRINCIPAL_BINDING_ROW_RE.match(line)
        if match:
            return no, match.group("body")
    return None


def row_is_legacy_only(row_body: str) -> bool:
    """True when the row still matches the pre-101 fieldless acts-as shape."""
    has_legacy = bool(LEGACY_ENDPOINTS_RE.search(row_body)) and bool(
        LEGACY_MEANING_RE.search(row_body)
    )
    has_fields = all(token in row_body for token in REQUIRED_FIELD_TOKENS)
    return has_legacy and not has_fields


def check_data_model_row(path: Path, row_no: int, row_body: str) -> list[str]:
    problems: list[str] = []
    if row_is_legacy_only(row_body):
        problems.append(
            f"{path}:{row_no}: decision-101-data-model — principal_binding "
            "row still matches the legacy agent → principal / "
            '"the principal the agent acts as" shape with no credential fields'
        )
        return problems

    for token in REQUIRED_FIELD_TOKENS:
        if token not in row_body:
            problems.append(
                f"{path}:{row_no}: decision-101-data-model — principal_binding "
                f"row missing `{token}`"
            )
    if not EXPIRY_TOKEN_RE.search(row_body):
        problems.append(
            f"{path}:{row_no}: decision-101-data-model — principal_binding "
            "row missing expiry / `expires_at`"
        )
    if not CARDINALITY_RE.search(row_body):
        problems.append(
            f"{path}:{row_no}: decision-101-data-model — principal_binding "
            "row missing one-edge-per-credential cardinality language"
        )
    if not RESOLUTION_RE.search(row_body):
        problems.append(
            f"{path}:{row_no}: decision-101-data-model — principal_binding "
            "row missing kind+value → principal resolution language"
        )
    return problems


def check(root: Path) -> list[str]:
    fdir = root / FOUNDATION_DIR
    conformance_path = fdir / "conformance.md"
    data_model_path = fdir / "data_model.md"
    if not conformance_path.is_file() or not data_model_path.is_file():
        raise CorpusProblem(
            f"expected {conformance_path} and {data_model_path} under --root {root}"
        )

    problems: list[str] = []
    conformance_text = conformance_path.read_text(encoding="utf-8")
    row = decision_101_row(conformance_text)
    if row is None:
        problems.append(
            f"{conformance_path}:1: decision-101-register — no register row "
            'beginning "| 101 |"'
        )
        return problems

    row_no, cells = row
    status = cells[4] if len(cells) > 4 else ""
    ruled = "**ruled**" in status.lower()

    data_model_text = data_model_path.read_text(encoding="utf-8")
    binding = principal_binding_relationships_row(data_model_text)
    if binding is None:
        if ruled:
            problems.append(
                f"{data_model_path}:1: decision-101-data-model — missing "
                "`principal_binding` relationships row while register row 101 "
                "is **ruled**"
            )
        return problems

    binding_no, binding_body = binding
    if ruled:
        problems.extend(
            check_data_model_row(data_model_path, binding_no, binding_body)
        )

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
        print(f"decision 101 check: {exc}", file=sys.stderr)
        return 1

    for problem in problems:
        print(problem)
    print(f"decision 101 check: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
