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


def _section(text: str, start: str, end: str) -> str:
    begin = text.find(start)
    if begin < 0:
        return ""
    stop = text.find(end, begin + len(start))
    return text[begin : stop if stop >= 0 else len(text)]


def _require(label: str, text: str, groups: tuple[tuple[str, ...], ...]) -> list[str]:
    normalized = " ".join(text.lower().split())
    missing = [
        "/".join(group)
        for group in groups
        if not any(token in normalized for token in group)
    ]
    return [f"decision-84-{label} — missing " + ", ".join(missing)] if missing else []


def _forbid(label: str, text: str, tokens: tuple[str, ...]) -> list[str]:
    normalized = " ".join(text.lower().split())
    present = [token for token in tokens if token in normalized]
    return [f"decision-84-{label} — forbidden " + ", ".join(present)] if present else []


def check(root: Path) -> list[str]:
    fdir = root / FOUNDATION_DIR
    names = (
        "conformance.md",
        "work_model.md",
        "data_model.md",
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
    wm31 = _line(texts["conformance_suite.md"], r"^\|\s*WM-31\s*\|.*$")
    wm31a = _line(texts["conformance_suite.md"], r"^\|\s*WM-31a\s*\|.*$")
    wm39 = _line(texts["conformance_suite.md"], r"^\|\s*WM-39\s*\|.*$")
    creation_row_names = ("WM-13", "WM-14", "WM-21", "WM-32b", "WM-35a", "WM-39")
    creation_row_map = {
        row: _line(texts["conformance_suite.md"], rf"^\|\s*{row}\s*\|.*$")
        for row in creation_row_names
    }
    creation_rows = " ".join(creation_row_map.values())
    task_schema = _line(texts["data_model.md"], r"^\|\s*task\s*\|.*$")
    intake_model = _section(
        texts["work_model.md"],
        "### Intake is every task's first workflow",
        "### What distinguishes a task being assembled from one intake has not reached",
    )
    direct_model = _section(
        texts["work_model.md"],
        "### What distinguishes a task being assembled from one intake has not reached",
        "### What a claim predicate treats as claimable",
    )
    claim_model = _section(
        texts["work_model.md"],
        "### What a claim predicate treats as claimable",
        "### A task is live when some principal could claim it now",
    )
    batch_formation = _section(
        texts["work_model.md"],
        "### How a batch is formed, and what chooses its workflow",
        "### A batch may hold on a condition discovered mid-flight",
    )
    source_index = _section(
        texts["work_model.md"],
        "### Where tasks come from: every source, indexed",
        "### An intake rule turns a described change in the record into a task, and nothing else",
    )
    hold_model = _section(
        texts["work_model.md"],
        "### A batch may hold on a condition discovered mid-flight",
        "### A batch may depend on a task it created",
    )
    scenario_j = _section(
        texts["scenarios.md"],
        "## (j) A task created, routed by intake, and entering its successor",
        "## What the scenarios do not show",
    )

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
        direct_model,
        (
            ("persistent assembly exclusion",),
            ("lease lapse", "lapsed lease"),
            ("creation grants no lease",),
            ("declared `pm` step owner", "declaration's `pm` owner role"),
            ("multi-agent assembly",),
        ),
    )
    problems += _require(
        "creator-authority-model",
        direct_model,
        (
            ("the creating principal receives no `classify` lease by being the creator",),
            ("contributors gain no lease or execution privilege",),
        ),
    )
    problems += _require(
        "claim-model",
        claim_model,
        (
            (
                "assembly exclusion exposes its open `classify` step only to a principal that resolves as the declaration's `pm` step owner",
            ),
        ),
    )
    universal_surfaces = {
        "intake-model": intake_model,
        "batch-formation": batch_formation,
        "source-index": source_index,
        "data-model": task_schema,
        "workflow": texts["workflows.md"],
        "scenario": scenario_j,
        "creation-rows": creation_rows,
    }
    for surface, text in universal_surfaces.items():
        problems += _require(
            f"universal-entry-{surface}",
            text,
            (
                ("every task",),
                ("intake batch",),
                ("at creation", "on creation", "creation boundary"),
            ),
        )
    for row, text in creation_row_map.items():
        problems += _require(
            f"universal-entry-{row.lower()}",
            text,
            (("intake batch",), ("at creation", "on creation")),
        )
    universal_entry = " ".join(universal_surfaces.values())
    problems += _require(
        "universal-entry-assembly-difference",
        universal_entry,
        (
            ("assembly exception", "assembling task"),
            ("persistent assembly exclusion", "persistent `classify` hold"),
        ),
    )
    problems += _forbid(
        "universal-entry",
        universal_entry,
        (
            "ordinary complete tasks end creation with no intake batch",
            "ordinary task with a batch at creation",
            "complete task is created for ordinary intake; that is its publication. it has no intake batch",
            "every non-assembly task meets this condition once, at creation",
            "intake batch exists?",
            "no: unrouted by that fact",
            "task enters intake; batch record opens",
        ),
    )
    problems += _require(
        "hold-model",
        hold_model,
        (
            ("assembly exception",),
            ("creator-time",),
            ("before a step owner or held lease exists",),
            ("persistent assembly exclusion",),
            ("survive the lease lapse", "survives the lease lapse"),
        ),
    )
    problems += _require(
        "finding-schema",
        texts["data_model.md"],
        (
            ("creator-time assembly finding",),
            ("persistent assembly exclusion",),
            ("survives lease lapse",),
            ("declaration-resolved `pm`",),
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
        "wm-31",
        wm31 + " " + wm31a,
        (
            ("assembly exception",),
            ("creator-time",),
            ("survives lapse",),
            ("pm-only",),
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
