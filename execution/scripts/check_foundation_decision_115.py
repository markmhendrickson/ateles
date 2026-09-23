#!/usr/bin/env python3
"""Bind decision 115's register, rule, vocabulary, and conformance rows.

Decision 115 deliberately extends four existing mechanisms instead of adding a
lifecycle state machine.  A partial prose edit can silently collapse it back
into a status field, omit one execution mechanism, or leave the conformance
suite unable to fail.  This stdlib-only check binds the reader-visible pieces.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


FOUNDATION_DIR = Path("docs/foundation")
ANCHOR = "agent-inventory-review-disablement-and-retirement"
HEADING = "Agent inventory, review, disablement, and retirement"
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.M)
_ROW_RE = re.compile(r"^\|\s*115\s*\|")

REQUIRED_AUTHORITY_MARKERS = (
    "**The rules in this section.**",
    "**Ruled (decision 115, 2026-09-22)",
    "**Two grains, not one flattened row.**",
    "**The decisions are mechanism-specific.**",
    "| task path |",
    "| engine step path |",
    "| self-triggering daemon |",
    "| interactive session |",
    "**Access and purpose review is one live recurring task per reviewed agent.**",
    "`REFERS_TO`",
    "`FOLLOWS`",
    "`action_policy`",
    "**Reconciliation is predicate-scoped and fail-closed.**",
    "| independent host evidence missing or stale while self-report is fresh |",
    "| deployment declared and no process observed |",
    "**Disablement and retirement are different derived outcomes.**",
    "Neither outcome is a stored lifecycle status",
)


class CorpusProblem(Exception):
    """The decision-115 corpus files are missing or unreadable."""


def anchor(heading: str) -> str:
    text = re.sub(r"`", "", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def section(text: str, wanted_anchor: str) -> str | None:
    headings = [
        (len(match.group(1)), match.group(2), match.start(), match.end())
        for match in _HEADING_RE.finditer(text)
    ]
    for index, (level, title, _start, end) in enumerate(headings):
        if anchor(title) != wanted_anchor:
            continue
        section_end = len(text)
        for next_level, _title, next_start, _next_end in headings[index + 1 :]:
            if next_level <= level:
                section_end = next_start
                break
        return text[end:section_end]
    return None


def line_number(text: str, needle: str) -> int:
    offset = text.find(needle)
    return text.count("\n", 0, max(offset, 0)) + 1


def check(root: Path) -> list[str]:
    fdir = root / FOUNDATION_DIR
    paths = {
        "authority": fdir / "authority_model.md",
        "conformance": fdir / "conformance.md",
        "suite": fdir / "conformance_suite.md",
        "vocabulary": fdir / "vocabulary.md",
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise CorpusProblem("missing " + ", ".join(missing))

    texts = {name: path.read_text(encoding="utf-8") for name, path in paths.items()}
    problems: list[str] = []

    row = next(
        (line for line in texts["conformance"].splitlines() if _ROW_RE.match(line)),
        None,
    )
    if row is None:
        problems.append(
            f'{paths["conformance"]}:1: decision-115-register — no row beginning "| 115 |"'
        )
    else:
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        if len(cells) < 5 or "**ruled**" not in cells[4].lower():
            problems.append(
                f"{paths['conformance']}:{line_number(texts['conformance'], row)}: "
                "decision-115-register — status cell must contain **ruled**"
            )
        if f"authority_model.md#{ANCHOR}" not in row:
            problems.append(
                f"{paths['conformance']}:{line_number(texts['conformance'], row)}: "
                "decision-115-register — row must point at the authority ruling"
            )

    authority_section = section(texts["authority"], ANCHOR)
    if authority_section is None:
        problems.append(
            f'{paths["authority"]}:1: decision-115-authority — missing section "{HEADING}"'
        )
    else:
        for marker in REQUIRED_AUTHORITY_MARKERS:
            if marker not in authority_section:
                problems.append(
                    f"{paths['authority']}:1: decision-115-authority — missing marker {marker!r}"
                )

    for number in range(22, 27):
        prefix = f"| AU-{number} |"
        row = next(
            (line for line in texts["suite"].splitlines() if line.startswith(prefix)),
            None,
        )
        if row is None:
            problems.append(
                f"{paths['suite']}:1: decision-115-suite — missing AU-{number}"
            )
        elif f"authority_model.md#{ANCHOR}" not in row:
            problems.append(
                f"{paths['suite']}:{line_number(texts['suite'], row)}: "
                f"decision-115-suite — AU-{number} must cite the authority ruling"
            )

    vocabulary_section = section(texts["vocabulary"], "agent-inventory")
    if vocabulary_section is None:
        problems.append(
            f"{paths['vocabulary']}:1: decision-115-vocabulary — missing agent inventory term"
        )
    else:
        for marker in (
            "[derived read](#derived-read)",
            "two grains",
            "mechanism-specific",
        ):
            if marker not in vocabulary_section:
                problems.append(
                    f"{paths['vocabulary']}:1: decision-115-vocabulary — missing {marker!r}"
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
        print(f"decision 115 check: {exc}", file=sys.stderr)
        return 1
    for problem in problems:
        print(problem)
    print(f"decision 115 check: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
