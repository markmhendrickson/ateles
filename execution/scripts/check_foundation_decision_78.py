#!/usr/bin/env python3
"""Check that decision 78 is ruled in the corpus where readers encounter it.

Decision 78 moved a still-open design question into a ruled operational
classification. A prose edit alone is easy to leave half-applied: the register
can say one thing while the adapters ruling still opens as an open question, or
the ruling can be updated without the register reflecting it. This check binds
the two observable reader-facing effects.

Stdlib only; registered in ``conformance.md#mechanical-checks-on-this-directory``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FOUNDATION_DIR = Path("docs/foundation")
DECISION_78_ANCHOR = (
    "whether-the-instance-of-the-record-serving-a-swarm-is-an-external-system-"
    "when-the-swarm-operates-it"
)
DECISION_78_HEADING = (
    "Whether the instance of the record serving a swarm is an external system "
    "when the swarm operates it"
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.M)
_DECISION_ROW_RE = re.compile(r"^\|\s*78\s*\|")


class CorpusProblem(Exception):
    """The decision-78 corpus files are missing or unreadable."""


def anchor(heading: str) -> str:
    text = re.sub(r"`", "", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def decision_78_row(conformance_text: str) -> tuple[int, list[str]] | None:
    for no, line in enumerate(conformance_text.splitlines(), 1):
        if not _DECISION_ROW_RE.match(line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        return no, cells
    return None


def decision_78_lead(adapters_text: str) -> tuple[int, str] | None:
    headings = [
        (len(match.group(1)), match.group(2), match.start(), match.end())
        for match in _HEADING_RE.finditer(adapters_text)
    ]
    for i, (level, title, start, end) in enumerate(headings):
        if anchor(title) != DECISION_78_ANCHOR:
            continue
        section_end = len(adapters_text)
        for next_level, _title, next_start, _next_end in headings[i + 1 :]:
            if next_level <= level:
                section_end = next_start
                break
        body = adapters_text[end:section_end]
        for line in body.splitlines():
            stripped = line.strip()
            if stripped:
                return line_of(adapters_text, start), stripped
        return line_of(adapters_text, start), ""
    return None


def check(root: Path) -> list[str]:
    fdir = root / FOUNDATION_DIR
    conformance_path = fdir / "conformance.md"
    adapters_path = fdir / "adapters.md"
    if not conformance_path.is_file() or not adapters_path.is_file():
        raise CorpusProblem(
            f"expected {conformance_path} and {adapters_path} under --root {root}"
        )

    problems: list[str] = []
    conformance_text = conformance_path.read_text(encoding="utf-8")
    row = decision_78_row(conformance_text)
    if row is None:
        problems.append(
            f"{conformance_path}:1: decision-78-register — no register row "
            'beginning "| 78 |"'
        )
    else:
        row_no, cells = row
        if len(cells) < 5 or "**ruled**" not in cells[4].lower():
            problems.append(
                f"{conformance_path}:{row_no}: decision-78-register — row 78 "
                'status cell must contain "**ruled**"'
            )

    adapters_text = adapters_path.read_text(encoding="utf-8")
    lead = decision_78_lead(adapters_text)
    if lead is None:
        problems.append(
            f"{adapters_path}:1: decision-78-adapters — missing section "
            f"`adapters.md#{DECISION_78_ANCHOR}`"
        )
    else:
        heading_no, lead_text = lead
        if not lead_text.startswith("**Ruled"):
            problems.append(
                f"{adapters_path}:{heading_no}: decision-78-adapters — "
                f'"{DECISION_78_HEADING}" must open with "**Ruled"'
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
        print(f"decision 78 check: {exc}", file=sys.stderr)
        return 1

    for problem in problems:
        print(problem)
    print(f"decision 78 check: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
