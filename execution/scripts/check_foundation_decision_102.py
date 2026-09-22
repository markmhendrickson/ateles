#!/usr/bin/env python3
"""Bind decision 102 to migration registration and bootstrap read-back.

Decision 102 makes a recognized acyclicity declaration mandatory on every
relationship type.  A ruled register row is not enough: the declaration must
be written and fail closed in migration stage 1, read back in both migration
verification and bootstrap, and exercised on both admitted declaration modes.

Stdlib only; registered in ``conformance.md#mechanical-checks-on-this-directory``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


FOUNDATION_DIR = Path("docs/foundation")
_DECISION_ROW_RE = re.compile(r"^\|\s*102\s*\|", re.M)


class CorpusProblem(Exception):
    """The decision-102 corpus files are missing or unreadable."""


def _section(text: str, lead: str, *, end: str | None = None) -> str:
    start = text.find(lead)
    if start < 0:
        return ""
    stop = text.find(end, start + len(lead)) if end else -1
    return text[start : stop if stop >= 0 else len(text)]


def _missing(label: str, body: str, tokens: tuple[str, ...]) -> list[str]:
    lowered = " ".join(body.lower().split())
    absent = [token for token in tokens if token.lower() not in lowered]
    if not absent:
        return []
    return [f"decision-102-{label} — missing " + ", ".join(absent)]


def check(root: Path) -> list[str]:
    fdir = root / FOUNDATION_DIR
    paths = {
        "conformance": fdir / "conformance.md",
        "migration": fdir / "migration.md",
        "suite": fdir / "conformance_suite.md",
    }
    missing = [str(path) for path in paths.values() if not path.is_file()]
    if missing:
        raise CorpusProblem("missing " + ", ".join(missing))

    conformance = paths["conformance"].read_text(encoding="utf-8")
    row = _DECISION_ROW_RE.search(conformance)
    if row is None:
        return ["decision-102-register — no register row beginning `| 102 |`"]
    row_line = conformance[row.start() : conformance.find("\n", row.start())]
    if "**ruled**" not in row_line.lower():
        return ["decision-102-register — row 102 is not **ruled**"]

    migration = paths["migration"].read_text(encoding="utf-8")
    suite = paths["suite"].read_text(encoding="utf-8")
    stage_one = _section(
        migration,
        "**Stage 1 — the registry (operator act).**",
        end="**Stage 2 —",
    )
    precheck = _section(
        migration,
        "| a schema registration (stage 1) |",
        end="\n|",
    )
    verification_table = _section(migration, "## Verification")
    verification = _section(verification_table, "| 1 |", end="\n| 2 |")
    bootstrap = _section(
        suite,
        "| 1 | the registry:",
        end="\n| 2 |",
    )

    problems: list[str] = []
    problems += _missing(
        "migration-registration",
        stage_one,
        ("acyclic", "cycles_admitted", "missing", "refused"),
    )
    problems += _missing(
        "migration-precheck",
        precheck,
        ("acyclicity declaration", "absence", "refused"),
    )
    problems += _missing(
        "migration-readback",
        verification,
        (
            "written `acyclic` or `cycles_admitted` declaration",
            "omitting or corrupting",
            "refused",
            "cycle-closing edge is refused",
            "declared-cycles-admitted test type accepts its cycle",
        ),
    )
    problems += _missing(
        "bootstrap-readback",
        bootstrap,
        ("acyclic", "cycles_admitted", "declaration present", "omitting it is refused"),
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
        print(f"decision 102 check: {exc}", file=sys.stderr)
        return 1
    for problem in problems:
        print(problem)
    print(f"decision 102 check: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
