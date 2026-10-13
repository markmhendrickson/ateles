"""Parse a lens verdict into structured findings.

The lens brief fixes the shape of a blocking finding: its own line,
``[BLOCKING] <category>: <summary>``. Everything else in the verdict is prose.
A finding's file and line are read from the line's own text (``path/to/file.py``,
``path/to/file.py:107``, ``path/to/file.py:100-110``, ``lines 100-110``); a
finding that names no file keeps ``file=None`` and can only be matched on its
claim wording.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass

_FINDING_RE = re.compile(
    r"^\s*(?:[-*>]+\s*)?(?:\d+[.)]\s*)?\*{0,2}\[BLOCKING\]\*{0,2}\s*(?P<rest>.*)$"
)
_PATH_RE = re.compile(
    r"(?<![\w/.\-])(?P<path>(?:\.?[\w.\-]+/)*[\w.\-]+\.(?:py|md|ts|tsx|js|jsx|mjs|json|ya?ml|sh|toml|sql|html|css|txt|ini|cfg|plist))"
    r"(?::(?P<l1>\d+)(?:[-–](?P<l2>\d+))?)?"
)
_LINES_RE = re.compile(
    r"\b(?:lines?|L)\s*(?P<l1>\d+)(?:\s*[-–]\s*(?P<l2>\d+))?", re.IGNORECASE
)
_VERDICT_RE = re.compile(
    r"^\*\*(SIGNED_OFF|BLOCKED|APPROVE|REQUEST_CHANGES|COMMENT)\*\*\s*$"
)


@dataclass(frozen=True)
class Finding:
    file: str | None
    line: int | None
    claim: str
    category: str | None
    line_end: int | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _clean_path(raw: str) -> str:
    path = raw.strip("`'\"()[]<>,;")
    while path.startswith("./"):
        path = path[2:]
    return path


def parse_findings(verdict_text: str) -> list[Finding]:
    """Every ``[BLOCKING]`` line in *verdict_text*, in order."""
    findings: list[Finding] = []
    for raw_line in verdict_text.splitlines():
        m = _FINDING_RE.match(raw_line)
        if not m:
            continue
        rest = m.group("rest").strip()
        category = None
        claim = rest
        head, sep, tail = rest.partition(":")
        if (
            sep
            and 0 < len(head) <= 60
            and "/" not in head
            and "`" not in head
            and "." not in head
        ):
            category = head.strip().strip("*`").lower() or None
            claim = tail.strip()
        file = line = line_end = None
        # Prefer a path that carries a line number, then one with a directory.
        candidates = list(_PATH_RE.finditer(rest))
        candidates.sort(
            key=lambda c: (c.group("l1") is None, "/" not in c.group("path"))
        )
        if candidates:
            c = candidates[0]
            file = _clean_path(c.group("path"))
            if c.group("l1"):
                line = int(c.group("l1"))
                line_end = int(c.group("l2")) if c.group("l2") else None
        if line is None:
            lm = _LINES_RE.search(rest)
            if lm:
                line = int(lm.group("l1"))
                line_end = int(lm.group("l2")) if lm.group("l2") else None
        findings.append(Finding(file, line, claim[:600], category, line_end))
    return findings


def verdict_token(verdict_text: str, reader_verdict: str | None = None) -> str | None:
    """The verdict token: the swarm reader's own answer when it gave one,
    else the third line if it looks like a verdict, else ``None``."""
    if reader_verdict:
        return str(reader_verdict)
    lines = verdict_text.splitlines()
    if len(lines) >= 3:
        m = _VERDICT_RE.match(lines[2].strip())
        if m:
            return m.group(1)
    return None
