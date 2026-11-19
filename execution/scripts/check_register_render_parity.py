#!/usr/bin/env python3
"""Compare foundation register rows with GitHub's rendered GFM tables."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Callable

from foundation_register_rows import (
    REGISTER_HEADING,
    _blank,
    _markdown_lines,
    _section_bounds,
    _table_delimiter,
    candidate_cells,
    decision_rows,
    revision_rows,
)

ROOT = Path(__file__).resolve().parents[2]
FILES = (ROOT / "docs/foundation/conformance.md", ROOT / "docs/foundation/revisions.md")


@dataclass(frozen=True)
class Table:
    name: str
    rows: tuple[tuple[str, ...], ...]


def classified_tables(kind: str, text: str) -> list[Table]:
    """Find the validated register/revision table blocks in source order."""
    lines = _markdown_lines(text)
    if kind == "register":
        decision_rows(text)
        start, end = _section_bounds(lines)
    else:
        revision_rows(text)
        start, end = 0, len(lines)
    tables: list[Table] = []
    position = start
    while position + 1 < end:
        cells = candidate_cells(lines[position])
        target = "#" if kind == "register" else "Revision"
        if (
            not cells
            or cells[0] != target
            or not _table_delimiter(candidate_cells(lines[position + 1]), len(cells))
        ):
            position += 1
            continue
        position += 2
        body: list[tuple[str, ...]] = []
        while (
            position < end
            and not _blank(lines[position])
            and not lines[position].startswith("## ")
        ):
            row = candidate_cells(lines[position])
            if row is None:
                raise ValueError(
                    f"{kind} table {len(tables) + 1} row {len(body) + 1}: malformed row"
                )
            body.append(row)
            position += 1
        tables.append(
            Table(
                "register"
                if kind == "register"
                else f"revision table {len(tables) + 1}",
                tuple(body),
            )
        )
    if not tables:
        raise ValueError(f"{kind} table 1: missing source table")
    return tables


def source_table_count(text: str) -> int:
    """Count every GFM delimiter pair, including tables outside the register section."""
    lines = _markdown_lines(text)
    return sum(
        bool(cells := candidate_cells(lines[index]))
        and _table_delimiter(candidate_cells(lines[index + 1]), len(cells))
        for index in range(len(lines) - 1)
    )


class RenderedTables(HTMLParser):
    def __init__(self, kind: str):
        super().__init__(convert_charrefs=True)
        self.kind = kind
        self.tables: list[list[list[str]]] = []
        self.total_tables = 0
        self.section = kind == "revision"
        self.heading = False
        self.heading_text: list[str] = []
        self.in_table = self.in_body = self.in_row = self.in_cell = False
        self.cell_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "h2":
            self.heading = True
            self.heading_text = []
        elif tag == "table":
            self.total_tables += 1
            self.in_table = self.section
            if self.in_table:
                self.tables.append([])
        elif tag == "tbody" and self.in_table:
            self.in_body = True
        elif tag == "tr" and self.in_body:
            self.in_row = True
            self.tables[-1].append([])
        elif tag == "td" and self.in_row:
            self.in_cell = True
            self.cell_text = []

    def handle_data(self, data: str) -> None:
        if self.heading:
            self.heading_text.append(data)
        if self.in_cell:
            self.cell_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "h2" and self.heading:
            self.section = (
                self.kind == "revision"
                or "".join(self.heading_text).strip() == REGISTER_HEADING[3:]
            )
            self.heading = False
        elif tag == "td" and self.in_cell:
            self.tables[-1][-1].append("".join(self.cell_text).strip(" \t\n\r"))
            self.in_cell = False
        elif tag == "tr":
            self.in_row = False
        elif tag == "tbody":
            self.in_body = False
        elif tag == "table":
            self.in_table = False


def _number(cell: str) -> str:
    match = re.match(r"[0-9]+(?:[–-][0-9]+)?", cell)
    return match.group(0) if match else cell


def check_parity(kind: str, source: str, html: str) -> None:
    expected = classified_tables(kind, source)
    rendered = RenderedTables(kind)
    rendered.feed(html)
    total = source_table_count(source)
    if rendered.total_tables != total:
        raise ValueError(
            f"{kind} total table count: classifier {total}, GitHub {rendered.total_tables}"
        )
    if len(rendered.tables) != len(expected):
        raise ValueError(
            f"{kind} table count: classifier {len(expected)}, GitHub {len(rendered.tables)}"
        )
    for table, actual_rows in zip(expected, rendered.tables):
        if len(table.rows) != len(actual_rows):
            raise ValueError(
                f"{table.name} row count: classifier {len(table.rows)}, GitHub {len(actual_rows)}"
            )
        for index, (cells, actual) in enumerate(zip(table.rows, actual_rows), 1):
            if len(cells) != len(actual):
                raise ValueError(f"{table.name} row {index}: cell count differs")
            if not actual or _number(cells[0]) != _number(actual[0]):
                raise ValueError(f"{table.name} row {index}: number cell differs")


def render_github(text: str) -> str:
    token = os.environ.get("GITHUB_TOKEN")
    body = json.dumps({"text": text, "mode": "gfm"}).encode()
    for attempt in range(2):
        try:
            if not token:
                result = subprocess.run(
                    ["gh", "api", "markdown", "--method", "POST", "--input", "-"],
                    input=body, capture_output=True, check=False, timeout=20,
                )
                if result.returncode:
                    detail = result.stderr.decode("utf-8", errors="replace").lower()
                    if attempt == 0 and any(
                        marker in detail for marker in (
                            "rate limit", "429", "502", "503", "504", "timeout",
                            "could not resolve", "connection", "temporary failure",
                        )
                    ):
                        time.sleep(1)
                        continue
                    raise RuntimeError("gh markdown API failed; parity check cannot pass")
                return result.stdout.decode("utf-8")
            request = urllib.request.Request(
                "https://api.github.com/markdown",
                data=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "text/html",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=20) as response:
                return response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            if attempt == 0 and (error.code == 429 or error.code >= 500 or error.headers.get("X-RateLimit-Remaining") == "0"):
                time.sleep(1)
                continue
            raise RuntimeError(f"GitHub markdown API returned HTTP {error.code}; parity check cannot pass") from error
        except (urllib.error.URLError, TimeoutError, subprocess.TimeoutExpired) as error:
            if attempt == 0:
                time.sleep(1)
                continue
            raise RuntimeError(
                f"GitHub markdown API failed ({type(error).__name__}); parity check cannot pass"
            ) from error
        except FileNotFoundError as error:
            raise RuntimeError("gh CLI is required when GITHUB_TOKEN is unset") from error
    raise AssertionError("unreachable")


def main(renderer: Callable[[str], str] = render_github) -> int:
    for kind, path in zip(("register", "revision"), FILES):
        try:
            source = path.read_text(encoding="utf-8")
            check_parity(kind, source, renderer(source))
        except (OSError, RuntimeError, ValueError) as error:
            print(f"register render parity: {error}", file=sys.stderr)
            return 1
    print("register render parity: both real files match GitHub GFM")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
