#!/usr/bin/env python3
"""Bind decisions 99 and 107-109 to their effect-shaped corpus rows.

Stdlib only; registered in ``conformance.md#mechanical-checks-on-this-directory``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FOUNDATION_DIR = Path("docs/foundation")


class CorpusProblem(Exception):
    """Required foundation files are absent."""


def _line(text: str, pattern: str) -> str:
    match = re.search(pattern, text, re.M)
    return match.group(0) if match else ""


def _require(label: str, text: str, tokens: tuple[str, ...]) -> list[str]:
    body = " ".join(text.lower().split())
    missing = [token for token in tokens if token not in body]
    return [f"foundation-rulings-{label} — missing " + ", ".join(missing)] if missing else []


def check(root: Path) -> list[str]:
    fdir = root / FOUNDATION_DIR
    paths = {
        name: fdir / name
        for name in (
            "conformance.md",
            "planning_model.md",
            "authority_model.md",
            "data_model.md",
            "conformance_suite.md",
        )
    }
    absent = [str(path) for path in paths.values() if not path.is_file()]
    if absent:
        raise CorpusProblem("missing " + ", ".join(absent))
    texts = {name: path.read_text(encoding="utf-8") for name, path in paths.items()}

    problems: list[str] = []
    row99 = _line(texts["conformance.md"], r"^\|\s*99\s*\|.*$")
    pm13 = _line(texts["conformance_suite.md"], r"^\|\s*PM-13\s*\|.*$")
    problems += _require(
        "99-register",
        row99,
        ("**ruled**", "exactly one instance", "controlling instance", "fails closed"),
    )
    problems += _require(
        "99-model",
        texts["planning_model.md"],
        ("every planning record belongs", "exactly one instance", "controlling instance", "naming several instances and no controlling one", "fails closed"),
    )
    problems += _require(
        "99-pm13",
        pm13,
        ("one instance", "controlling instance", "none means refusal", "chooses an instance by order"),
    )

    for number in (107, 108, 109):
        row = _line(texts["conformance.md"], rf"^\|\s*{number}\s*\|.*$")
        problems += _require(f"{number}-register", row, ("**ruled**",))

    authority = texts["authority_model.md"]
    data_model = texts["data_model.md"]
    au29 = _line(texts["conformance_suite.md"], r"^\|\s*AU-29\s*\|.*$")
    acts_as_tokens = (
        "traversal-only",
        "non-presentable",
        "denied as a non-presentable kind",
        "agent-to-agent acts-as write is refused",
        "`delegation_edge`",
        "operator-sourced acts-as write is also refused",
    )
    problems += _require("107-109-authority", authority, acts_as_tokens)
    problems += _require(
        "107-109-data-model",
        data_model,
        ("traversal-only", "non-presentable", "agent → agent acts-as is refused", "operator-sourced acts-as is refused", "`delegation_edge`"),
    )
    problems += _require(
        "107-109-au29",
        au29,
        ("present `credential_kind: acts_as`", "write a → b acts-as", "write o → p acts-as", "`delegation_edge`", "turns this row red"),
    )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args(argv)
    try:
        problems = check(args.root)
    except CorpusProblem as exc:
        print(f"foundation rulings check: {exc}", file=sys.stderr)
        return 1
    for problem in problems:
        print(problem)
    print(f"foundation rulings 99/107-109 check: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
