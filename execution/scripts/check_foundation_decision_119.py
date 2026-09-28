#!/usr/bin/env python3
"""Check that decision 119's commissioning design is bound to the corpus.

This is a corpus-shape check, not runtime enforcement.  It makes a ruled
commissioning decision fail closed when the canonical planning model omits a
load-bearing part of the decision, when ``DEPENDS_ON`` still excludes planning
records, or when the conformance suite loses one of the planted negatives.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FOUNDATION_DIR = Path("docs/foundation")
DECISION_ROW_RE = re.compile(r"^\|\s*119\s*\|")
COMMISSIONING_HEADING = (
    "### Commissioning a planning record drives one dependency-ready frontier"
)
CONFORMANCE_IDS = ("PM-13", "PM-14", "PM-15", "PM-16", "PM-17")


class CorpusProblem(Exception):
    """The decision-119 corpus files are missing or unreadable."""


def decision_119_row(conformance_text: str) -> tuple[int, list[str]] | None:
    for line_no, line in enumerate(conformance_text.splitlines(), 1):
        if DECISION_ROW_RE.match(line):
            return line_no, [cell.strip() for cell in line.strip("|").split("|")]
    return None


def commissioning_section(planning_text: str) -> tuple[int, str] | None:
    lines = planning_text.splitlines()
    for index, line in enumerate(lines):
        if line.strip() != COMMISSIONING_HEADING:
            continue
        body: list[str] = []
        for following in lines[index + 1 :]:
            if following.startswith("### "):
                break
            body.append(following)
        return index + 1, "\n".join(body)
    return None


def require(
    section: str, pattern: str, label: str, path: Path, line_no: int
) -> list[str]:
    if re.search(pattern, section, re.I | re.S):
        return []
    return [
        f"{path}:{line_no}: decision-119-{label} — commissioning section "
        f"does not bind {label.replace('-', ' ')}"
    ]


def check(root: Path) -> list[str]:
    fdir = root / FOUNDATION_DIR
    conformance_path = fdir / "conformance.md"
    planning_path = fdir / "planning_model.md"
    data_model_path = fdir / "data_model.md"
    suite_path = fdir / "conformance_suite.md"
    paths = (conformance_path, planning_path, data_model_path, suite_path)
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise CorpusProblem(
            f"expected {', '.join(str(path) for path in paths)} under --root "
            f"{root}; missing {', '.join(missing)}"
        )

    conformance_text = conformance_path.read_text(encoding="utf-8")
    row = decision_119_row(conformance_text)
    if row is None:
        return [
            f'{conformance_path}:1: decision-119-register — no register row beginning "| 119 |"'
        ]
    row_no, cells = row
    if len(cells) < 5 or "**ruled**" not in cells[4].lower():
        return []

    planning_text = planning_path.read_text(encoding="utf-8")
    section = commissioning_section(planning_text)
    if section is None:
        return [
            f"{planning_path}:1: decision-119-section — no "
            f"`{COMMISSIONING_HEADING}` section found while register row 119 "
            "is **ruled**"
        ]

    section_line, body = section
    problems: list[str] = []
    requirements = (
        (
            r"`commission_(?:plan|project)`.*`pause_(?:plan|project)`.*"
            r"`resume_(?:plan|project)`.*`cancel_(?:plan|project)`",
            "control-actions",
        ),
        (
            r"decision.*`SUPERSEDES`.*no\s+`?(?:commission_)?status`?",
            "no-parallel-status",
        ),
        (
            r"`DEPENDS_ON`.*(?:task|planning record).*(?:task|planning record)",
            "planning-dependencies",
        ),
        (
            r"dependency-ready frontier.*maximum safe parallelism",
            "frontier-parallelism",
        ),
        (r"continuous planning.*forever-recurring", "continuous-versus-recurring"),
        (r"completion_criteria\[\].*descendant.*evidence", "completion-evidence"),
        (r"checkpoint.*(?:not|never).*completion evidence", "checkpoint-negative"),
        (r"newly\s+discovered\s+work.*silent\s+scope\s+expansion", "scope-admission"),
        (r"resume.*(?:re-derive|rederive).*never replays", "safe-resumption"),
        (r"stop condition.*(?:complete|completion).*cancel", "stop-condition"),
    )
    for pattern, label in requirements:
        problems.extend(require(body, pattern, label, planning_path, section_line))

    data_model_text = data_model_path.read_text(encoding="utf-8")
    depends_row = next(
        (
            line
            for line in data_model_text.splitlines()
            if line.startswith("| `DEPENDS_ON` |")
        ),
        "",
    )
    if not depends_row or "planning record" not in depends_row:
        problems.append(
            f"{data_model_path}:1: decision-119-depends-on — `DEPENDS_ON` row "
            "does not admit a planning record as a valid endpoint"
        )

    suite_text = suite_path.read_text(encoding="utf-8")
    for conformance_id in CONFORMANCE_IDS:
        if f"| {conformance_id} |" not in suite_text:
            problems.append(
                f"{suite_path}:1: decision-119-conformance — missing "
                f"conformance row `{conformance_id}`"
            )
    planted_negatives = (
        "blocked dependency",
        "independent parallel children",
        "safe resumption",
        "refuse parent completion",
    )
    for phrase in planted_negatives:
        if phrase not in suite_text.lower():
            problems.append(
                f"{suite_path}:1: decision-119-planted-negative — missing "
                f"planted conformance phrase `{phrase}`"
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
        print(f"decision 119 check: {exc}", file=sys.stderr)
        return 1
    for problem in problems:
        print(problem)
    print(f"decision 119 check: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
