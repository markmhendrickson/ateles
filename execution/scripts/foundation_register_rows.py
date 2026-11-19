"""Classify foundation register table lines before interpreting their number cells."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

REGISTER_HEADING = "## The register of open design decisions"
NUMBER = re.compile(r"(?:0|[1-9][0-9]*)(?:[–-](?:0|[1-9][0-9]*))?\Z")
STATUS_LEAD = re.compile(r"\*\*([^*]+)\*\*(?:[ \t].*)?\Z")
STATUSES = frozenset(
    {"ruled in part", "not a decision", "withdrawn", "reopened", "open", "ruled"}
)
SEPARATOR = re.compile(r":?-+:?\Z")

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
    content = line.strip(" \t")
    if content.startswith("|"):
        content = content[1:]
    if content.endswith("|") and _pipe_is_unescaped(content, len(content) - 1):
        content = content[:-1]
    cells: list[str] = []
    part: list[str] = []
    for index, char in enumerate(content):
        # GFM divides table cells before parsing inline code spans. Only an
        # escaped pipe remains inside a cell, including inside backticks.
        if char == "|" and _pipe_is_unescaped(content, index):
            cells.append("".join(part).strip(" \t"))
            part = []
        else:
            part.append(char)
    cells.append("".join(part).strip(" \t"))
    return tuple(cells)


def _visible_line(line: str) -> str:
    """Tolerate invisible prefixes at a structural marker, never in cell values."""
    line = line.lstrip(" \t")
    prefix = 0
    while prefix < len(line) and unicodedata.category(line[prefix]) == "Cf":
        prefix += 1
    if prefix and line[prefix:prefix + 1] in ("|", ">"):
        line = line[prefix:]
    return re.sub(r"^>[ \t]?", "", line).lstrip(" \t")


def _pipe_is_unescaped(line: str, index: int) -> bool:
    backslashes = 0
    while index - backslashes - 1 >= 0 and line[index - backslashes - 1] == "\\":
        backslashes += 1
    return backslashes % 2 == 0


def _has_unescaped_pipe(line: str) -> bool:
    return any(char == "|" and _pipe_is_unescaped(line, i) for i, char in enumerate(line))


def _markdown_lines(text: str) -> list[str]:
    """GFM recognizes CR, LF and CRLF as line endings, not Unicode separators."""
    return re.split(r"\r\n|\n|\r", text)


def _blank(line: str) -> bool:
    return line.strip(" \t") == ""


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


def _table_delimiter(cells: tuple[str, ...] | None, width: int) -> bool:
    return bool(cells and len(cells) == width and all(SEPARATOR.fullmatch(cell) for cell in cells))


def _outside_table_start(lines: list[str], index: int, cells: tuple[str, ...] | None) -> bool:
    return bool(
        cells
        and index + 1 < len(lines)
        and _table_delimiter(candidate_cells(lines[index + 1]), len(cells))
    )


def decision_rows(text: str) -> dict[str, dict[str, str]]:
    """Validate every line of the register table block, including pipe-less lines."""
    lines = _markdown_lines(text)
    start, end = _section_bounds(lines)
    rows: dict[str, dict[str, str]] = {}
    header = separator = in_table = outside_table = False
    for index, line in enumerate(lines):
        location = index + 1
        inside = start <= index < end
        cells = candidate_cells(line)
        if not inside:
            if _blank(line) or line.startswith("## "):
                outside_table = False
            elif _outside_table_start(lines, index, cells):
                outside_table = True
            if cells and _outside_number_cell(cells):
                raise ValueError(
                    f"line {location}: numbered table row outside the register section "
                    "(labels exempt from this rule are listed in OUTSIDE_LABEL_WIDTHS in "
                    "execution/scripts/foundation_register_rows.py)"
                )
            if outside_table and cells is None and _outside_number_cell((line.strip(" \t"),)):
                raise ValueError(f"line {location}: numbered table row outside the register section")
            continue
        if _blank(line):
            in_table = False
            continue
        if cells and len(cells) == 5 and cells[0] == "#":
            if header:
                raise ValueError(f"line {location}: duplicate register table header")
            header = in_table = True
            continue
        if _table_delimiter(cells, 5):
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
        if any(unicodedata.category(char) == "Cf" for char in cells[4]):
            raise ValueError(f"line {location}: malformed register row status")
        lead = STATUS_LEAD.fullmatch(cells[4])
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
    header = separator = in_table = other_table = False
    lines = _markdown_lines(text)
    for position, line in enumerate(lines):
        index = position + 1
        if _blank(line) or line.startswith("## "):
            in_table = other_table = False
            if line.startswith("## "):
                header = separator = False
            continue
        cells = candidate_cells(line)
        if cells and len(cells) == 3 and cells[0] == "Revision":
            header = in_table = True
            separator = False
            other_table = False
            continue
        if header and _table_delimiter(cells, 3):
            separator = True
            continue
        if not in_table:
            if _outside_table_start(lines, position, cells):
                other_table = True
            if cells and _outside_number_cell(cells):
                raise _row_error("revision", index)
            if other_table and cells is None and _outside_number_cell((line.strip(" \t"),)):
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
