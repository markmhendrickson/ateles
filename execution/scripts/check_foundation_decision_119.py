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
CLAUSE_TITLES = {
    "control": "Control is an action and its durable result is a decision, not a status",
    "frontier": "The frontier is derived from dependencies and the controls above each leaf",
    "recurrence": "Continuous planning is not forever-recurring delivery work",
    "completion": "Completion is a proof over descendant effects, not a checkpoint or a count",
    "scope": "Discovered work is admitted through the statement before it joins execution",
    "recovery": "Recovery re-derives the frontier, and the commission has two stop conditions",
}


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


def named_clause(section: str, section_line: int, title: str) -> tuple[int, str] | None:
    marker = f"**{title}.**"
    start = section.find(marker)
    if start == -1:
        return None
    following = section.find("\n\n**", start + len(marker))
    end = len(section) if following == -1 else following
    line_no = section_line + section[:start].count("\n")
    return line_no, section[start:end]


def require_pattern(
    text: str, pattern: str, label: str, path: Path, line_no: int, region: str
) -> list[str]:
    if re.search(pattern, text, re.I | re.S):
        return []
    return [
        f"{path}:{line_no}: decision-119-{label} — {region} does not bind "
        f"{label.replace('-', ' ')}"
    ]


def conformance_rows(suite_text: str) -> dict[str, tuple[int, list[str]]]:
    rows: dict[str, tuple[int, list[str]]] = {}
    for line_no, line in enumerate(suite_text.splitlines(), 1):
        if not re.match(r"^\|\s*PM-\d+\s*\|", line):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows[cells[0]] = (line_no, cells)
    return rows


def check_clause(
    section: str,
    section_line: int,
    path: Path,
    clause_name: str,
    requirements: tuple[tuple[str, str], ...],
) -> list[str]:
    title = CLAUSE_TITLES[clause_name]
    clause = named_clause(section, section_line, title)
    if clause is None:
        return [
            f"{path}:{section_line}: decision-119-{clause_name}-clause — "
            f"commissioning section missing normative clause `{title}`"
        ]
    line_no, body = clause
    problems: list[str] = []
    for pattern, label in requirements:
        problems.extend(
            require_pattern(body, pattern, label, path, line_no, f"`{title}` clause")
        )
    return problems


def check_conformance_row(
    path: Path,
    row_id: str,
    row: tuple[int, list[str]],
    requirements: tuple[tuple[int, str, str], ...],
) -> list[str]:
    line_no, cells = row
    if len(cells) < 6:
        return [
            f"{path}:{line_no}: decision-119-{row_id.lower()}-shape — "
            f"`{row_id}` is not a six-cell conformance row"
        ]
    problems: list[str] = []
    for cell_index, pattern, label in requirements:
        problems.extend(
            require_pattern(
                cells[cell_index],
                pattern,
                label,
                path,
                line_no,
                f"`{row_id}` cell {cell_index + 1}",
            )
        )
    return problems


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
    problems.extend(
        check_clause(
            body,
            section_line,
            planning_path,
            "control",
            (
                (
                    r"`commission_plan`.*`pause_plan`.*`resume_plan`.*`cancel_plan`",
                    "control-actions",
                ),
                (
                    r"grant\s+that\s+admits\s+the\s+action\s+and\s+the\s+engine's\s+write",
                    "control-grant",
                ),
                (
                    r"`ownership_grant`.*required\s+checkpoint\s+seat",
                    "control-owner-seat",
                ),
                (
                    r"existing\s+action\s+gate\s+permits\s+the\s+action",
                    "control-action-gate",
                ),
                (
                    r"read-back\s+confirms\s+the\s+effect",
                    "control-confirmed-read-back",
                ),
                (
                    r"`decision`.*`PART_OF`.*`REFERS_TO`.*`SUPERSEDES`",
                    "control-decision-chain",
                ),
                (
                    r"no\s+`commission_status`.*cursor.*second\s+orchestration\s+store",
                    "no-parallel-status",
                ),
            ),
        )
    )
    problems.extend(
        check_clause(
            body,
            section_line,
            planning_path,
            "frontier",
            (
                (
                    r"`DEPENDS_ON`.*task\s+or\s+planning\s+record.*task\s+or\s+planning\s+record",
                    "planning-dependencies",
                ),
                (
                    r"satisfied\s+only\s+by.*proved\s+completion,\s+never\s+by.*cancellation",
                    "frontier-dependency-satisfaction",
                ),
                (
                    r"dependency-ready\s+frontier.*maximum\s+safe\s+parallelism",
                    "frontier-parallelism",
                ),
                (
                    r"lease\s+exclusivity.*assignment.*grants.*metered_resources\[\].*budgets",
                    "frontier-constraints",
                ),
            ),
        )
    )
    problems.extend(
        check_clause(
            body,
            section_line,
            planning_path,
            "recurrence",
            (
                (
                    r"planning`?\s+task.*control\s+loop.*outside\s+the\s+delivery\s+frontier",
                    "recurrence-control-loop",
                ),
                (
                    r"forever-recurring\s+delivery.*prevents\s+terminal\s+completion.*declared\s+end.*final\s+occurrence.*terminal\s+and\s+landed",
                    "recurrence-delivery-stop",
                ),
            ),
        )
    )
    problems.extend(
        check_clause(
            body,
            section_line,
            planning_path,
            "completion",
            (
                (
                    r"every.*`completion_criteria\[\]`.*descendant\s+effect\s+evidence",
                    "completion-evidence",
                ),
                (
                    r"checkpoint.*never\s+completion\s+evidence.*cannot\s+substitute",
                    "completion-checkpoint-negative",
                ),
                (
                    r"completion\s+decision.*unmet\s+criterion.*refused",
                    "completion-unmet-refusal",
                ),
                (
                    r"cancellation.*not\s+completion.*or\s+satisfaction\s+of\s+a\s+`DEPENDS_ON`\s+edge",
                    "completion-cancellation-negative",
                ),
            ),
        )
    )
    problems.extend(
        check_clause(
            body,
            section_line,
            planning_path,
            "scope",
            (
                (
                    r"already\s+within.*scope.*created\s+as\s+a\s+task\s+`PART_OF`.*by\s+`amend`",
                    "scope-in-scope-admission",
                ),
                (
                    r"change\s+the\s+scope.*`completion_criteria\[\]`.*`amend_<level>`\s+action\s+and\s+a\s+confirmed\s+planning\s+decision.*until.*task\s+is\s+not\s+admitted",
                    "scope-confirmed-amendment",
                ),
                (
                    r"without\s+silent\s+scope\s+expansion",
                    "scope-no-silent-expansion",
                ),
            ),
        )
    )
    problems.extend(
        check_clause(
            body,
            section_line,
            planning_path,
            "recovery",
            (
                (
                    r"resume\s+does\s+the\s+same.*never\s+replays.*local\s+cursor",
                    "safe-resumption",
                ),
                (
                    r"pause\s+admits\s+no\s+new.*delivery.*claim.*delivery\s+step.*delivery\s+action",
                    "recovery-delivery-quiescence",
                ),
                (
                    r"planning`?\s+task\s+is\s+control\s+work,\s+not\s+delivery\s+work",
                    "recovery-control-work",
                ),
                (
                    r"while\s+paused.*reachable\s+only.*`resume_<level>`\s+or\s+`cancel_<level>`",
                    "recovery-control-reachability",
                ),
                (
                    r"same\s+proposer\s+grant.*`action_policy`.*`ownership_grant`\s+checkpoint\s+seat.*action\s+gate.*confirmed\s+effect\s+read-back",
                    "recovery-control-authorization",
                ),
                (
                    r"interruption.*paused\s+state.*confirmed\s+`resume_plan`\s+writes\s+and\s+reads\s+back.*before\s+the\s+delivery\s+frontier\s+is\s+re-derived",
                    "recovery-interrupted-resume",
                ),
                (
                    r"stop\s+condition.*proved\s+completion.*confirmed\s+cancellation",
                    "stop-condition",
                ),
            ),
        )
    )

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
    rows = conformance_rows(suite_text)
    for conformance_id in CONFORMANCE_IDS:
        if conformance_id not in rows:
            problems.append(
                f"{suite_path}:1: decision-119-conformance — missing "
                f"conformance row `{conformance_id}`"
            )
    row_requirements = {
        "PM-13": (
            (
                1,
                r"commission.*pause.*resume.*cancel.*gated\s+actions.*`SUPERSEDES`",
                "pm-13-rule",
            ),
            (
                2,
                r"owner.*policy.*permitting\s+commission.*reserving\s+cancel",
                "pm-13-setup",
            ),
            (
                3,
                r"pause.*interrupt.*live\s+`planning`\s+control\s+task.*cancel.*unseated",
                "pm-13-action",
            ),
            (
                4,
                r"effect\s+without\s+the\s+action\s+gate\s+and\s+owner\s+seat",
                "pm-13-action-gate-owner-seat",
            ),
            (
                4,
                r"`resume_plan`\s+is\s+unreachable\s+while\s+paused",
                "pm-13-resume-reachability",
            ),
            (
                4,
                r"delivery\s+child.*claimed\s+while\s+paused",
                "pm-13-delivery-quiescence",
            ),
            (
                4,
                r"cancellation\s+is\s+treated\s+as\s+completion",
                "pm-13-cancellation-negative",
            ),
            (
                4,
                r"conflicting\s+controls.*without\s+a\s+checkpoint",
                "pm-13-conflict-refusal",
            ),
        ),
        "PM-14": (
            (
                1,
                r"frontier.*maximum\s+safe\s+parallelism.*`PART_OF`.*`DEPENDS_ON`.*leases.*assignments.*grants.*metered\s+resources.*budgets",
                "pm-14-rule",
            ),
            (
                2,
                r"independent\s+parallel\s+children.*blocked\s+child.*`PL2\s+DEPENDS_ON\s+PL3`",
                "pm-14-setup",
            ),
            (
                3,
                r"read\s+the\s+frontier.*no\s+shared\s+resource.*impose\s+one\s+metered-resource\s+slot.*cancel\s+`PL3`\s+without\s+completing\s+it",
                "pm-14-action",
            ),
            (
                4,
                r"blocked\s+dependency\s+enters\s+the\s+frontier",
                "pm-14-blocked-dependency",
            ),
            (
                4,
                r"`PL2`\s+enters\s+after\s+cancellation",
                "pm-14-cancellation-negative",
            ),
            (
                4,
                r"independent\s+parallel\s+children\s+are\s+serialized",
                "pm-14-independent-parallelism",
            ),
            (
                4,
                r"both\s+claim\s+under\s+the\s+one-slot\s+constraint",
                "pm-14-resource-limit",
            ),
            (
                4,
                r"frontier\s+or\s+concurrency\s+count\s+is\s+stored",
                "pm-14-derived-state",
            ),
        ),
        "PM-15": (
            (
                1,
                r"continuous\s+planning.*control\s+loop.*forever-recurring\s+delivery.*blocks\s+terminal\s+completion",
                "pm-15-rule",
            ),
            (
                2,
                r"live\s+planning\s+task.*forever-recurring\s+delivery\s+task",
                "pm-15-setup",
            ),
            (
                3,
                r"close\s+every\s+finite\s+descendant.*one\s+delivery\s+occurrence.*give\s+the\s+recurrence\s+an\s+end.*land\s+its\s+final\s+occurrence",
                "pm-15-action",
            ),
            (
                4,
                r"`PL1`\s+completes\s+while\s+the\s+delivery\s+recurrence\s+is\s+unbounded",
                "pm-15-unbounded-recurrence",
            ),
            (
                4,
                r"planning\s+maintenance\s+task.*prevents\s+completion",
                "pm-15-control-loop-negative",
            ),
            (4, r"occurrence\s+is\s+reopened.*`FOLLOWS`", "pm-15-occurrence-chain"),
        ),
        "PM-16": (
            (
                1,
                r"completion.*descendant\s+effect\s+evidence.*checkpoints\s+are\s+not\s+evidence.*out-of-scope.*amendment",
                "pm-16-rule",
            ),
            (
                2,
                r"unmet\s+criterion.*resolved\s+checkpoint.*outside\s+scope",
                "pm-16-setup",
            ),
            (
                3,
                r"attempt\s+completion.*attach.*confirm\s+`amend_plan`.*effect\s+evidence",
                "pm-16-action",
            ),
            (
                4,
                r"refuse\s+parent\s+completion\s+does\s+not\s+fire\s+with\s+an\s+unmet\s+criterion",
                "pm-16-refuse-parent-completion",
            ),
            (
                4,
                r"checkpoint\s+satisfies\s+the\s+criterion",
                "pm-16-checkpoint-negative",
            ),
            (4, r"task\s+joins\s+before\s+the\s+amendment", "pm-16-scope-admission"),
            (
                4,
                r"cancellation\s+satisfies\s+a\s+dependency",
                "pm-16-cancellation-negative",
            ),
        ),
        "PM-17": (
            (
                1,
                r"safe\s+resumption\s+re-derives\s+control.*dependencies.*leases.*confirmations.*idempotency\s+keys.*stop.*completion.*cancellation",
                "pm-17-rule",
            ),
            (
                2,
                r"paused\s+`PL1`\s+interrupted.*confirmed\s+action.*lapsed\s+lease.*unsatisfied\s+criterion.*empty\s+frontier",
                "pm-17-setup",
            ),
            (
                3,
                r"live\s+`planning`\s+control\s+task.*`resume_plan`.*grant.*owner\s+seat.*action\s+gate.*confirmed\s+read-back",
                "pm-17-action",
            ),
            (
                4,
                r"`resume_plan`\s+is\s+unreachable\s+while\s+delivery\s+is\s+quiesced\s+or\s+bypasses\s+authorization\s+or\s+confirmed\s+read-back",
                "pm-17-resume-control-path",
            ),
            (
                4,
                r"replays\s+the\s+confirmed\s+action.*local\s+cursor",
                "pm-17-replay-negative",
            ),
            (4, r"lapsed\s+lease\s+remains\s+held", "pm-17-lapsed-lease"),
            (4, r"empty\s+frontier\s+closes\s+the\s+parent", "pm-17-empty-frontier"),
            (
                4,
                r"execution\s+continues\s+after\s+the\s+completion\s+proof",
                "pm-17-stop-negative",
            ),
        ),
    }
    for row_id, requirements in row_requirements.items():
        row = rows.get(row_id)
        if row is not None:
            problems.extend(
                check_conformance_row(suite_path, row_id, row, requirements)
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
