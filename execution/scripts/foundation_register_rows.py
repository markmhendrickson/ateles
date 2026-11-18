"""Classify foundation register table lines before interpreting their number cells."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

REGISTER_HEADING = "## The register of open design decisions"
NUMBER = re.compile(r"[0-9]+(?:[–-][0-9]+)?\Z")
STATUS_LEAD = re.compile(r"\*\*([^*]+)\*\*")
STATUSES = frozenset(
    {"ruled in part", "not a decision", "withdrawn", "reopened", "open", "ruled"}
)
SEPARATOR = re.compile(r":?-{3,}:?\Z")

# Existing non-register table labels in conformance.md whose first cells carry
# digits. All other digit-bearing first cells outside the register fail closed.
OUTSIDE_LABEL_WIDTHS = {
    "`lib/daemon_runtime/agent_loader`, `grant_checker`, `aauth_signer`, "
    "`lib/approval`, `lib/notify`, `lib/daemon_runtime/checkpoint_posture`, "
    "`execution/mcp/ateles/`, `execution/mcp/mcp_tool_grant_proxy`, "
    "`execution/daemons/apis/a2a_`": 2,
    "Decision 78 ruling": 4,
    "Decision 84 ruling": 4,
    "Decision 101 ruling": 4,
    "Decision 117 ruling": 4,
    "Decision 119 commissioning": 4,
}


@dataclass(frozen=True)
class NumberedRow:
    number: str
    cells: tuple[str, ...]
    line: int


def candidate_cells(line: str) -> tuple[str, ...] | None:
    """Every pipe-led table line is a candidate, regardless of its first cell.

    More than three spaces (or a tab) is invalid Markdown table indentation,
    but still returned so a misplaced numbered row cannot disappear silently.
    """
    if not line.lstrip().startswith("|"):
        return None
    content = line.lstrip()[1:].rstrip()
    if content.endswith("|"):
        content = content[:-1]
    cells: list[str] = []
    part: list[str] = []
    in_code = False
    for index, char in enumerate(content):
        if char == "`" and (index == 0 or content[index - 1] != "\\"):
            in_code = not in_code
        if char == "|" and not in_code and (index == 0 or content[index - 1] != "\\"):
            cells.append("".join(part).strip())
            part = []
        else:
            part.append(char)
    cells.append("".join(part).strip())
    return tuple(cells)


def has_digit(cell: str) -> bool:
    """Find Unicode decimal/numeric digits even inside Markdown or HTML markup."""
    return any(
        unicodedata.digit(char, None) is not None
        for char in unicodedata.normalize("NFKC", cell)
    )


def _section_bounds(lines: list[str]) -> tuple[int, int]:
    start = next((i for i, line in enumerate(lines) if line == REGISTER_HEADING), None)
    if start is None:
        return 0, len(lines)  # bare table fixtures
    end = next(
        (i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")),
        len(lines),
    )
    return start, end


def _outside_number_cell(cells: tuple[str, ...]) -> bool:
    return has_digit(cells[0]) and OUTSIDE_LABEL_WIDTHS.get(cells[0]) != len(cells)


def decision_rows(text: str) -> dict[str, dict[str, str]]:
    """Validate and return the entire decision register; no candidate is skipped.

    A malformed line gets a fixed error and source line number. Never include
    source text in errors: malformed input can contain controls or secrets.
    """
    lines = text.splitlines()
    start, end = _section_bounds(lines)
    rows: dict[str, dict[str, str]] = {}
    header = separator = False
    for index, line in enumerate(lines):
        cells = candidate_cells(line)
        if cells is None:
            # A pipe-less numeric row is still malformed, never an omission.
            if re.match(r"^\s*[0-9]+\s*\|", line):
                if not start <= index < end:
                    raise ValueError(
                        f"line {index + 1}: numbered table row outside the register section"
                    )
                raise ValueError(f"line {index + 1}: malformed register row")
            continue
        location = f"line {index + 1}"
        if not start <= index < end:
            if cells and _outside_number_cell(cells):
                raise ValueError(
                    f"{location}: numbered table row outside the register section"
                )
            continue
        if not re.fullmatch(r" {0,3}\|.*", line):
            raise ValueError(f"{location}: malformed register row indentation")
        if not line.rstrip().endswith("|"):
            raise ValueError(f"{location}: malformed register row")
        if len(cells) == 5 and cells[0] == "#":
            if header:
                raise ValueError(f"{location}: duplicate register table header")
            header = True
            continue
        if len(cells) == 5 and all(SEPARATOR.fullmatch(cell) for cell in cells):
            if separator:
                raise ValueError(f"{location}: duplicate register table separator")
            separator = True
            continue
        if len(cells) != 5 or not NUMBER.fullmatch(cells[0]) or not all(cells[1:4]):
            raise ValueError(f"{location}: malformed register row")
        number = cells[0]
        lead = STATUS_LEAD.match(cells[4])
        if lead is None:
            raise ValueError(f"{location}: malformed register row status")
        status = lead.group(1)
        if status not in STATUSES:
            raise ValueError(f"{location}: unknown status in register row")
        if number in rows:
            raise ValueError(f"{location}: duplicate register row {number}")
        rows[number] = {
            "question": cells[1],
            "argued_in": cells[2],
            # Preserve the existing projection's bytes for a historical cell
            # containing an inline-code pipe; row validation still sees one cell.
            "blocks": cells[3].split("|", 1)[0].strip(),
            "status": status,
            "status_cell": cells[4],
        }
    if not header:
        raise ValueError("register table header is missing")
    if not separator:
        raise ValueError("register table separator is missing")
    if not rows:
        raise ValueError("register table has no numbered rows")
    return rows


def revision_rows(text: str) -> list[NumberedRow]:
    """Numbered revision rows, using the same candidate and number classifier."""
    rows: list[NumberedRow] = []
    for index, line in enumerate(text.splitlines(), 1):
        cells = candidate_cells(line)
        if cells is None:
            continue
        if not re.fullmatch(r" {0,3}\|.*", line):
            if has_digit(cells[0]):
                raise ValueError(f"line {index}: malformed revision row indentation")
            continue
        legacy = re.fullmatch(r"([0-9]+),\s+.+", cells[0]) if cells else None
        if cells and (NUMBER.fullmatch(cells[0]) or legacy):
            rows.append(
                NumberedRow(legacy.group(1) if legacy else cells[0], cells, index)
            )
        elif cells and has_digit(cells[0]):
            raise ValueError(f"line {index}: malformed revision row")
    return rows
