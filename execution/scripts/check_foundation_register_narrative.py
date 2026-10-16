#!/usr/bin/env python3
"""Hold the decision register's PROSE to the table it describes.

The register in ``conformance.md#the-register-of-open-design-decisions`` has two
halves: a table, and the narrative around it that recounts how the rows got
there. The rule-coverage check binds a decision opened in a *document* to a row
in that table. **Nothing has ever read the narrative.** So the prose drifted
from the table standing three paragraphs below it, and kept drifting: a sentence
naming which rows are open goes stale the moment the next pass rules one, and a
"next free number is N" line was found wrong on five consecutive passes, each
pass correcting it and the next finding it stale again.

That last line is now fixed the right way — it states the *sweep* that finds the
number and writes no number at all (principle 9: a number written in prose is a
second source for something the table already answers). This check makes the
same discipline mechanical for the shapes that keep recurring.

What it reports:

``stale-open-rows``
    a **present-tense** claim naming which rows are open — ``The open rows: 72
    and 73`` — whose list does not equal the table's actual open rows. The fix is
    not to update the numbers; that is what failed five times. It is to point at
    the table instead of restating it.

``next-free-number``
    any prose stating a *next free number* as a value. There is no correct value
    to state: the number is established by sweeping every branch's copy of the
    table, and a concurrent branch can take it between the write and the read.
    Reported unconditionally, right or wrong.

``status-contradiction``
    prose asserting a specific decision's present status — ``decision 72 is
    open``, ``73 remains unruled`` — that the table's row for that number
    contradicts.

**What this check deliberately does not report.** A DATED, past-tense statement
of a pass's state at its time — "the open rows *were then* four — 33, 34, 55,
and 57", "The open rows after this pass: 40, 55, and 64" — is correct as
history and is the register's most valuable narrative content. Those are
recognized by their past-tense or pass-scoped framing (``PAST_FRAMING``) and
skipped. The consequence is real and worth stating plainly: **a historical
sentence whose numbers are simply wrong is invisible to this check.** It reads
tense, not archives; verifying a 2026-09-06 claim would need that day's table,
which this checkout does not have. What it catches is drift into the present —
which is the shape that actually recurred.

It also does not read prose in any document but ``conformance.md``, does not
judge the *argument* around a claim, and cannot see a stale claim phrased
without one of the shapes below. It is a narrow check on recurring shapes, not a
general prose-to-table consistency proof.

Stdlib only; registered in ``conformance.md#mechanical-checks-on-this-directory``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FOUNDATION_DIR = Path("docs/foundation")
REGISTER_DOC = "conformance.md"

# A register row: "| 73 | question | pointer | scope | **open** (date) |" or the
# combined "| 1–12 |" row. The status is the LAST cell.
ROW_RE = re.compile(r"^\|\s*(\d+(?:[–-]\d+)?)\s*\|(.*)\|\s*$")

# Status keywords, longest-first: "ruled in part" must beat "ruled".
STATUS_ORDER = ("ruled in part", "not a decision", "withdrawn", "open", "ruled")

# The status is the BOLD LEAD of the last cell — "**ruled** (2026-09-06): …" —
# never a word appearing later in that cell's prose. Matching by containment
# instead read "never reopened" and "before the step opens" as an open status
# and mislabelled nine ruled rows; the whole point of this check is that the
# table is authoritative, so parsing it loosely would be the same defect class
# one layer down.
STATUS_LEAD_RE = re.compile(r"^\s*\*\*([^*]+?)\*\*")

# A sentence is HISTORY, not a present claim, when it carries one of these.
# "were then" / "after this pass" / "at this pass's start" date the statement to
# a pass; "were" alone is past tense. Matched case-insensitively.
PAST_FRAMING = re.compile(
    r"\b(?:were\s+then|were\b|was\b|had\s+been|remained\b|left\b"
    r"|after\s+(?:this|that|the)\s+\w+\s+pass"
    r"|after\s+all\s+\w+\s+passes"
    r"|at\s+(?:this|that)\s+pass)",
    re.IGNORECASE,
)

# A present-tense assertion of the open set. "The open rows: 72 and 73." or
# "The open rows are 40, 55, and 64." Captures the number list that follows.
OPEN_ROWS_RE = re.compile(
    r"(?:the\s+)?open\s+rows?\s*(?::|\bare\b|\bis\b)\s*([^.]*?)(?:\.|\Z)",
    re.IGNORECASE,
)

# Any prose naming a next free number as a value. "The next free number is 79."
NEXT_FREE_RE = re.compile(
    r"next\s+free\s+(?:decision\s+)?number\s+(?:is|are|will\s+be)\s+(\d+)",
    re.IGNORECASE,
)

# "decision 72 is open", "73 remains unruled", "decision 55 stays open".
STATUS_CLAIM_RE = re.compile(
    r"\bdecision\s+(\d+)\s+(?:is|remains|stays)\s+"
    r"(open|unruled|ruled|withdrawn)\b",
    re.IGNORECASE,
)

# Numbers inside a captured list, ignoring "in part" parentheticals.
NUM_RE = re.compile(r"\b(\d+)\b")

# A markdown table row, so narrative scanning skips the table itself.
TABLE_LINE = re.compile(r"^\s*\|")

# "zero" as an open-row list is a real, checkable claim.
ZERO_RE = re.compile(r"^\W*(?:zero|none)\W*", re.IGNORECASE)


def parse_register(text: str) -> dict[str, str]:
    """Return {row number as written: status keyword} for every register row."""
    rows: dict[str, str] = {}
    for line in text.split("\n"):
        m = ROW_RE.match(line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(2).split("|")]
        if not cells:
            continue
        lead = STATUS_LEAD_RE.match(cells[-1])
        if not lead:
            continue
        status_lead = lead.group(1).strip().lower()
        for keyword in STATUS_ORDER:
            if status_lead.startswith(keyword):
                rows[m.group(1)] = keyword
                break
    return rows


def open_numbers(rows: dict[str, str]) -> set[int]:
    """The table's open rows, as integers. A combined row is never open."""
    out: set[int] = set()
    for num, status in rows.items():
        if status == "open" and num.isdigit():
            out.add(int(num))
    return out


def narrative_lines(text: str) -> list[tuple[int, str]]:
    """Every non-table line, with its 1-based line number."""
    return [
        (i + 1, line)
        for i, line in enumerate(text.split("\n"))
        if not TABLE_LINE.match(line)
    ]


def _sentence_around(line: str, start: int) -> str:
    """The sentence containing offset `start`, for tense judgement."""
    left = line.rfind(".", 0, start) + 1
    right = line.find(".", start)
    return line[left : right if right != -1 else len(line)]


def check(root: Path) -> list[str]:
    path = root / REGISTER_DOC
    if not path.is_file():
        return [f"{path}: register document absent"]

    text = path.read_text(encoding="utf-8")
    rows = parse_register(text)
    if not rows:
        return [f"{path}: no register rows parsed — the table's shape changed"]

    actual_open = open_numbers(rows)
    problems: list[str] = []

    for lineno, line in narrative_lines(text):
        # --- next free number: reported unconditionally ---
        for m in NEXT_FREE_RE.finditer(line):
            problems.append(
                f"{path}:{lineno}: next-free-number — prose states the next free "
                f"number is {m.group(1)}. No value is correct here: the number is "
                f"established by sweeping every branch's copy of the table, and a "
                f"concurrent branch can take it between the write and the read. "
                f"State the sweep, not a number (principle 9)."
            )

        # --- present-tense open-row lists ---
        for m in OPEN_ROWS_RE.finditer(line):
            sentence = _sentence_around(line, m.start())
            if PAST_FRAMING.search(sentence):
                continue  # dated history — correct as written, left alone
            claimed_text = m.group(1)
            if ZERO_RE.match(claimed_text.strip()):
                claimed: set[int] = set()
            else:
                claimed = {int(n) for n in NUM_RE.findall(claimed_text)}
                if not claimed:
                    continue  # no numbers and not "zero" — nothing asserted
            if claimed != actual_open:
                problems.append(
                    f"{path}:{lineno}: stale-open-rows — prose asserts the open "
                    f"rows are {sorted(claimed) or 'zero'}; the table's open rows "
                    f"are {sorted(actual_open)}. Do not update the numbers — that "
                    f"correction has failed on every pass that made it. Point at "
                    f"the table instead of restating it."
                )

        # --- a specific decision's present status ---
        for m in STATUS_CLAIM_RE.finditer(line):
            num, claimed_status = m.group(1), m.group(2).lower()
            actual = rows.get(num)
            if actual is None:
                problems.append(
                    f"{path}:{lineno}: status-contradiction — prose names decision "
                    f"{num}, which has no row in the register table."
                )
                continue
            wanted = "open" if claimed_status == "unruled" else claimed_status
            if wanted == "open" and actual != "open":
                problems.append(
                    f"{path}:{lineno}: status-contradiction — prose says decision "
                    f"{num} is {claimed_status}; the table's row says {actual!r}."
                )
            elif wanted == "ruled" and actual not in ("ruled", "ruled in part"):
                problems.append(
                    f"{path}:{lineno}: status-contradiction — prose says decision "
                    f"{num} is ruled; the table's row says {actual!r}."
                )
            elif wanted == "withdrawn" and actual != "withdrawn":
                problems.append(
                    f"{path}:{lineno}: status-contradiction — prose says decision "
                    f"{num} is withdrawn; the table's row says {actual!r}."
                )

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=FOUNDATION_DIR,
        help="foundation directory to check (default: docs/foundation)",
    )
    args = parser.parse_args(argv)

    if not args.root.is_dir():
        print(f"register narrative check: {args.root} is not a directory", file=sys.stderr)
        return 1

    problems = check(args.root)
    for p in problems:
        print(p)
    print(f"register narrative check: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
