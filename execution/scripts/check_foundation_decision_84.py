#!/usr/bin/env python3
"""Bind decision 84's persistent assembly exclusion across the corpus.

The assembly finding, not a currently-held lease, is what must keep incomplete
work out of the ordinary claim pool. Creation also confers no step ownership:
only the intake declaration's resolved PM owner may claim ``classify``.

Stdlib only; registered in ``conformance.md#mechanical-checks-on-this-directory``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FOUNDATION_DIR = Path("docs/foundation")


class CorpusProblem(Exception):
    """The decision-84 corpus files are missing or unreadable."""


def _line(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.M)
    return match.group(0) if match else ""


def _require(label: str, text: str, groups: tuple[tuple[str, ...], ...]) -> list[str]:
    normalized = " ".join(text.lower().split())
    missing = [
        "/".join(group)
        for group in groups
        if not any(token in normalized for token in group)
    ]
    return [f"decision-84-{label} — missing " + ", ".join(missing)] if missing else []


def check(root: Path) -> list[str]:
    fdir = root / FOUNDATION_DIR
    names = (
        "conformance.md",
        "work_model.md",
        "workflows.md",
        "scenarios.md",
        "conformance_suite.md",
    )
    texts: dict[str, str] = {}
    for name in names:
        path = fdir / name
        if not path.is_file():
            raise CorpusProblem(f"missing {path}")
        texts[name] = path.read_text(encoding="utf-8")

    register = _line(texts["conformance.md"], r"^\|\s*84\s*\|.*$")
    wm14a = _line(texts["conformance_suite.md"], r"^\|\s*WM-14a\s*\|.*$")
    wm39 = _line(texts["conformance_suite.md"], r"^\|\s*WM-39\s*\|.*$")

    problems: list[str] = []
    problems += _require(
        "register",
        register,
        (
            ("**ruled**",),
            ("persistent assembly exclusion",),
            ("lapsed", "lease lapse"),
            ("creation grants no lease",),
            ("`pm` step owner",),
        ),
    )
    problems += _require(
        "model",
        texts["work_model.md"],
        (
            ("persistent assembly exclusion",),
            ("lease lapse", "lapsed lease"),
            ("creation grants no lease", "creating principal receives no `classify` lease"),
            ("declared `pm` step owner", "declaration's `pm` owner role"),
            ("multi-agent assembly",),
        ),
    )
    problems += _require(
        "workflow-exception",
        texts["workflows.md"],
        (("decision 84's assembly",), ("persistent",), ("intake batch",)),
    )
    problems += _require(
        "scenario-exception",
        texts["scenarios.md"],
        (("assembling task is the ruled exception",), ("resolved `pm` step owner",)),
    )
    problems += _require(
        "wm-14a",
        wm14a,
        (
            ("persistent",),
            ("lapse",),
            ("transfer",),
            ("creator",),
            ("resolved `pm` step owner", "declaration-resolved `pm`"),
            ("mutant",),
        ),
    )
    problems += _require(
        "wm-39",
        wm39,
        (("assembly exception",), ("ordinary",), ("declared `pm` step owner",)),
    )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args(argv)
    try:
        problems = check(args.root)
    except CorpusProblem as exc:
        print(f"decision 84 check: {exc}", file=sys.stderr)
        return 1
    for problem in problems:
        print(problem)
    print(f"decision 84 check: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
