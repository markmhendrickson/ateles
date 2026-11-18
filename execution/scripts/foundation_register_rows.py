"""Classify foundation register table lines before interpreting their number cells."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

REGISTER_HEADING = "## The register of open design decisions"
NUMBER = re.compile(r"(?:0|[1-9][0-9]*)(?:[–-](?:0|[1-9][0-9]*))?\Z")
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
    """Split a pipe-containing line, including GFM's optional leading pipe."""
    line = _visible_line(line)
    if not _has_unescaped_pipe(line):
        return None
    content = line.strip()
    if content.startswith("|"):
        content = content[1:]
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


def _visible_line(line: str) -> str:
    """Remove invisible formatting characters and one blockquote marker."""
    line = "".join(char for char in line if unicodedata.category(char) != "Cf")
    line = line.lstrip()
    return re.sub(r"^>\s?", "", line).lstrip()


def _has_unescaped_pipe(line: str) -> bool:
    return any(char == "|" and (i == 0 or line[i - 1] != "\\") for i, char in enumerate(line))


def _row_error(kind: str, index: int, reason: str = "malformed") -> ValueError:
    return ValueError(f"line {index}: {reason} {kind} row")


def _row_number(number: str, index: int, kind: str) -> str:
    if not NUMBER.fullmatch(number):
        raise _row_error(kind, index)
    return number.replace("-", "–")


def _invalid_indent(line: str) -> bool:
    return bool(re.match(r"^(?:\t| {4,})", line))


def _block_start(line: str) -> bool:
    return line.lstrip().startswith(">")


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
    """Validate every line of the register table block, including pipe-less lines."""
    lines = text.splitlines()
    start, end = _section_bounds(lines)
    rows: dict[str, dict[str, str]] = {}
    header = separator = in_table = False
    for index, line in enumerate(lines):
        location = index + 1
        inside = start <= index < end
        cells = candidate_cells(line)
        if not inside:
            if cells and _outside_number_cell(cells):
                raise ValueError(f"line {location}: numbered table row outside the register section")
            continue
        if not line.strip():
            in_table = False
            continue
        if cells and len(cells) == 5 and cells[0] == "#":
            if header:
                raise ValueError(f"line {location}: duplicate register table header")
            header = in_table = True
            continue
        if cells and len(cells) == 5 and all(SEPARATOR.fullmatch(cell) for cell in cells):
            if separator:
                raise ValueError(f"line {location}: duplicate register table separator")
            if not header:
                raise _row_error("register", location)
            separator = True
            continue
        if not header:
            if cells:
                raise _row_error("register", location)
            continue
        if not in_table and cells is None:
            continue
        if _invalid_indent(line) or _block_start(line):
            raise _row_error("register", location, "malformed indentation in")
        if cells is None or len(cells) != 5 or not all(cells[1:4]) or not separator:
            raise _row_error("register", location)
        # A row after a blank line is a second table or lost register text.
        if not in_table:
            raise _row_error("register", location)
        number = _row_number(cells[0], location, "register")
        lead = STATUS_LEAD.match(cells[4])
        if lead is None:
            raise ValueError(f"line {location}: malformed register row status")
        status = lead.group(1)
        if status not in STATUSES:
            raise ValueError(f"line {location}: unknown status in register row")
        if number in rows:
            raise ValueError(f"line {location}: duplicate register row {number}")
        rows[number] = {
            "question": cells[1],
            "argued_in": cells[2],
            "blocks": cells[3],
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
    """Read each revision table block without silently losing body lines."""
    rows: list[NumberedRow] = []
    header = separator = in_table = False
    for index, line in enumerate(text.splitlines(), 1):
        if not line.strip() or line.startswith("## "):
            in_table = False
            if line.startswith("## "):
                header = separator = False
            continue
        cells = candidate_cells(line)
        if cells and len(cells) == 3 and cells[0] == "Revision":
            header = in_table = True
            separator = False
            continue
        if header and cells and len(cells) == 3 and all(SEPARATOR.fullmatch(cell) for cell in cells):
            separator = True
            continue
        if not in_table:
            if cells and has_digit(cells[0]) and OUTSIDE_LABEL_WIDTHS.get(cells[0]) != len(cells):
                raise _row_error("revision", index)
            continue
        if _invalid_indent(line) or _block_start(line):
            raise _row_error("revision", index, "malformed indentation in")
        if not separator or cells is None or len(cells) != 3:
            raise _row_error("revision", index)
        if cells[0] == "—" and all(cells[1:]):
            continue  # historical revision without an assigned number
        legacy = re.fullmatch(r"([0-9]+),\s+.+", cells[0]) if cells else None
        number = _row_number(legacy.group(1) if legacy else cells[0], index, "revision")
        if not all(cells[1:]):
            raise _row_error("revision", index)
        rows.append(NumberedRow(number, cells, index))
    return rows
