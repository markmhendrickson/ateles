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
#
# Every alternative must carry a RESOLUTION verb or the explicit
# credential-to-principal phrase. An earlier revision allowed a bare
# `.{0,80}principal` tail, which the word "attribution" elsewhere in the same
# row satisfied -- so deleting the resolution language left the check green.
RESOLUTION_RE = re.compile(
    r"kind\+value(?:\[\+issuer\])?\s*(?:→|->)\s*principal"
    r"|match(?:es|ing)?\s+live\s+edges\s+on\s+kind"
    r"|resolv\w*\s+(?:a\s+)?credential\w*\s+to\s+(?:a\s+|the\s+)?principal"
    r"|credential-to-principal\s+resolution",
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


AUTHORITY_HEADING = (
    "### What the credential binding carries, and what a check reads "
    "to resolve a credential to a principal"
)


# The endpoint assignment decision 101 settles, and the defect class that makes
# it worth a mechanical assertion: swapping the two endpoints (AAuth → operator,
# acts-as → agent) leaves every field, cardinality, and resolution token intact,
# so every other assertion here stays green on a corpus that says the opposite
# of the ruling. Registration is one-way under G26 (`migration.md`), so a swap
# that reaches stage 1 is not correctable afterwards. Attribution requires the
# AAuth edge to end at the agent (or a write attributes to the operator and
# A-for-B is unrecordable); decision 48's counting rule requires the acts-as
# edge to end at the operator (or two agents under one operator count as two
# interests). One edge cannot end at both, which is why there are two.
AAUTH_ENDPOINT_RE = re.compile(
    r"AAuth\s+kind\s+yields\s+the\s+\*{0,2}agent"
    r"|AAuth\s+edge[^.;]{0,80}?ends\s+at\s+the\s+\*{0,2}agent"
    r"|resolves[^.;]{0,120}?\(`sub`\s*\+\s*`iss`\)\s*to\s+the\s*\n?\s*`agent`",
    re.I,
)
ACTS_AS_ENDPOINT_RE = re.compile(
    r"acts-as\s+kind\s+yields\s+the\s+\*{0,2}operator"
    r"|acts-as[^.;]{0,120}?whose\s+endpoint\s+is\s+the\s+\*{0,2}`?operator"
    r"|acts-as\s+edge[^.;]{0,80}?ends\s+at\s+the\s+\*{0,2}`?operator",
    re.I,
)
# A swap states the inverse. Detecting it explicitly lets the diagnostic name
# what is wrong rather than only that something is missing.
AAUTH_SWAPPED_RE = re.compile(
    r"AAuth\s+kind\s+yields\s+the\s+\*{0,2}operator"
    r"|AAuth\s+edge[^.;]{0,80}?ends\s+at\s+the\s+\*{0,2}`?operator",
    re.I,
)
ACTS_AS_SWAPPED_RE = re.compile(
    r"acts-as\s+kind\s+yields\s+the\s+\*{0,2}agent"
    r"|acts-as[^.;]{0,120}?whose\s+endpoint\s+is\s+the\s+\*{0,2}`?agent"
    r"|acts-as\s+edge[^.;]{0,80}?ends\s+at\s+the\s+\*{0,2}`?agent",
    re.I,
)


def authority_ruling_body(text: str) -> str | None:
    """The body of the decision-101 ruling section, heading to next heading.

    The endpoint assignment is stated inside this section, so the assertion
    reads the section rather than the whole document — a correct sentence
    elsewhere must not vouch for a swapped one here.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() != AUTHORITY_HEADING:
            continue
        body: list[str] = []
        for follow in lines[i + 1 :]:
            if follow.startswith("## ") or follow.startswith("### "):
                break
            body.append(follow)
        return "\n".join(body)
    return None


def check_authority_endpoints(path: Path, heading_no: int, body: str) -> list[str]:
    """Assert which principal each of the two binding kinds resolves to."""
    problems: list[str] = []

    if AAUTH_SWAPPED_RE.search(body):
        problems.append(
            f"{path}:{heading_no}: decision-101-endpoints — the AAuth "
            "credential is stated to resolve to the operator; decision 101 "
            "ends that edge at the **agent** (attribution records a write as "
            "A-for-B, which an operator endpoint makes unrecordable)"
        )
    elif not AAUTH_ENDPOINT_RE.search(body):
        problems.append(
            f"{path}:{heading_no}: decision-101-endpoints — the ruling "
            "section does not state that the AAuth credential resolves to the "
            "**agent**"
        )

    if ACTS_AS_SWAPPED_RE.search(body):
        problems.append(
            f"{path}:{heading_no}: decision-101-endpoints — the acts-as "
            "binding is stated to end at the agent; decision 101 ends that "
            "edge at the **operator** (decision 48's counting rule reads it, "
            "and an agent endpoint would make two agents under one operator "
            "two interests)"
        )
    elif not ACTS_AS_ENDPOINT_RE.search(body):
        problems.append(
            f"{path}:{heading_no}: decision-101-endpoints — the ruling "
            "section does not state that the acts-as binding's endpoint is "
            "the **operator**"
        )

    return problems


def authority_ruling_section(text: str) -> tuple[int, str] | None:
    """The decision-101 ruling section in ``authority_model.md``, with its opener.

    Returns (line number of the heading, the first non-blank line beneath it),
    or None when the heading is absent.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == AUTHORITY_HEADING:
            for follow in lines[i + 1 :]:
                if follow.strip():
                    return i + 1, follow
            return i + 1, ""
    return None


def check(root: Path) -> list[str]:
    fdir = root / FOUNDATION_DIR
    conformance_path = fdir / "conformance.md"
    data_model_path = fdir / "data_model.md"
    authority_path = fdir / "authority_model.md"
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

    # The document that STATES the ruling. Without this the whole of
    # authority_model.md's field table, endpoint rule, and resolver outcomes
    # could be deleted or reverted to `**Open.**` and this check would stay
    # green on the data_model row alone -- the ruled-but-not-implemented
    # divergence, on the document that is the implementation of the ruling.
    if ruled:
        if not authority_path.is_file():
            problems.append(
                f"{authority_path}:1: decision-101-authority — missing while "
                "register row 101 is **ruled**"
            )
        else:
            authority_text = authority_path.read_text(encoding="utf-8")
            section = authority_ruling_section(authority_text)
            if section is None:
                problems.append(
                    f"{authority_path}:1: decision-101-authority — no section "
                    f'"{AUTHORITY_HEADING.lstrip("# ")}" while register row 101 '
                    "is **ruled**"
                )
            else:
                heading_no, opener = section
                if not opener.lstrip().startswith("**Ruled"):
                    problems.append(
                        f"{authority_path}:{heading_no}: decision-101-authority "
                        "— the ruling section must open with \"**Ruled\" while "
                        "register row 101 is **ruled**; it opens "
                        f"{opener.strip()[:40]!r}"
                    )
                body = authority_ruling_body(authority_text)
                if body is not None:
                    problems.extend(
                        check_authority_endpoints(
                            authority_path, heading_no, body
                        )
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
