#!/usr/bin/env python3
"""Check that decision 114's ruled shape is registered where a reader meets it.

Decision 114 rules that a rule binding an agent's behaviour is tied to the agent(s) it governs by a
`GOVERNS` graph edge, resolved by traversal — superseding the `scope`/`agent_sub` field pair, which is
read nowhere once the edge resolves (`conformance.md#the-register-of-open-design-decisions`, row 114;
`migration.md#gaps-and-contradictions-the-mapping-exposed`, G34; `vocabulary.md#rule`). Decision 120
later moved the rule to the core `rule` type and kept 114's binding, so the row this reads is still the
concepts table's *agent behavioural rule* row, whatever its entity-type cell names.

Marking the register row **ruled** without that row carrying the edge is false readiness: a reader (or the
daemon loader and session rule index the ruling names as its two consumers) finds the concepts table still
describing the pre-114 shape.

This binds three things:

1. Register row 114 is **ruled**.
2. The row's edges column carries a `GOVERNS` → `agent` entry whose own parenthetical is, verbatim, one of
   ``APPROVED_GOVERNS_AGENT_TEXTS``.
3. The row's `scope` and `agent_sub` field descriptions are, verbatim, one of
   ``APPROVED_SCOPE_TEXTS`` and ``APPROVED_AGENT_SUB_TEXTS`` respectively.

**Why exact text and not a reading of the prose.** Seven review rounds on PR #1321 each found a new way to
deny the claim in ordinary English — a negation word the list lacked, a negation too far from the claim,
a copula or hedge adverb ("seems", "arguably", "supposedly") in front of it, a future tense ("will be
superseded"), a pending-review aside — and each fix grew a denylist that the next round was one synonym
ahead of. There is no finite list of ways to deny a sentence, so a checker that reads free prose for a
denial cannot be complete. This one does not read the prose at all: the claim-bearing spans must be the
approved sentences exactly (whitespace-normalized), so no hedge, denial, or aside can be added to them
without the check going red. Changing the wording means changing the approved text in this file in the
same PR, which puts the new wording in front of review rather than past it.

**What it does not verify.** Prose elsewhere in the row — another field's description, another edge's
parenthetical, the derived-reads column — is not read. A contradiction written there is review's to
catch, not this check's; the check guarantees that the three spans that carry the ruled claim say
exactly what was ruled. It also requires every edges-column entry to be an edge of a type in
``ALLOWED_EDGE_TYPES`` (`` `TYPE` → target ``), so a bare prose entry cannot sit in the edge list, and
exactly one `GOVERNS` → `agent` entry, so a second one cannot carry a denial beside the approved one.
The fields column is held to the same entry-level exactness: every `;`-separated entry that names
`scope` or `agent_sub` must be, in full, `` `field` (<approved text>) ``, with nothing before the
backtick or after the closing parenthesis, and each field is described by exactly one entry.

**What a reader sees, not only the bytes.** The approved spans must be visible when the page renders.
HTML comments are blanked before either row is located (line numbers kept), so a row inside a comment
is not found; and the concepts row and register row 114 are refused outright if their raw line carries
an HTML comment delimiter, a raw HTML tag, or GFM strikethrough (``~~``) — markup is a finite grammar, so
refusing it is complete where reading prose for denials was not. Both rows are looked up only inside
their own section (``## Concepts``; ``## The register of open design decisions``), and a second match in
either section is a problem rather than a silent first-match, so a decoy row cannot mask or disable the
real one. Whether row 114 is ruled is read from its raw line, so text hidden in a comment can only turn
the check on, never off.

This is a corpus-shape check, the same kind `check_foundation_decision_101.py` and
`check_foundation_decision_117.py` already are: it takes on no traversal or loader-implementation scope.

Stdlib only; registered in ``conformance.md#mechanical-checks-on-this-directory``.
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from pathlib import Path

FOUNDATION_DIR = Path("docs/foundation")

DECISION_KEY = "114"
CONCEPT_KEY = "agent behavioural rule"

REGISTER_HEADING = "## The register of open design decisions"
CONCEPTS_HEADING = "## Concepts"

_HTML_COMMENT_RE = re.compile(r"<!--.*?(?:-->|\Z)", re.DOTALL)

# Markup that can hide or strike text from a reader while the raw bytes still carry it.
_HIDING_MARKUP_RE = re.compile(r"<!--|-->|~~|</?[A-Za-z][A-Za-z0-9-]*(?:\s[^<>]*)?/?>")


# CommonMark's line endings. `str.splitlines()` also splits on U+2028, U+0085 and others that a renderer
# keeps inside one line, so the checker and a reader would disagree on where a row starts.
_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")


def _lines(text: str) -> list[str]:
    return _LINE_BREAK_RE.split(text)


def _blank_html_comments(text: str) -> str:
    """Remove HTML comments (an unclosed one runs to the end), keeping every line break so line numbers
    in the result match the raw text."""
    return _HTML_COMMENT_RE.sub(lambda m: "".join(_LINE_BREAK_RE.findall(m.group(0))), text)


def _format_characters(line: str) -> list[str]:
    """Unicode format (category Cf) characters in ``line``: zero-width and bidi controls render as
    nothing, so a cell carrying one reads the same as a cell without it."""
    return sorted({f"U+{ord(ch):04X}" for ch in line if unicodedata.category(ch) == "Cf"})


def _normalize_key(text: str) -> str:
    """A table cell as a reader sees it: NFKC, format characters dropped, whitespace collapsed, casefolded."""
    text = "".join(ch for ch in unicodedata.normalize("NFKC", text) if unicodedata.category(ch) != "Cf")
    return " ".join(text.split()).casefold()


# --- Block structure ------------------------------------------------------------------------------
#
# A row is located only where CommonMark would render it as table text, and a section ends only at a
# `## ` heading a reader would see. Lines inside fenced code, HTML blocks, and indented code are raw:
# never a row, never a heading. Where this scanner and a renderer could disagree it errs toward raw,
# which can only hide a row from the strict lookup (then a problem, or ambiguity via the loose count
# below) and never end a section early.

_FENCE_OPEN_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
_HTML_BLOCK_STARTS = (
    (re.compile(r"^ {0,3}<(?:script|pre|style|textarea)(?:\s|>|$)", re.I),
     re.compile(r"</(?:script|pre|style|textarea)>", re.I)),
    (re.compile(r"^ {0,3}<!--"), re.compile(r"-->")),
    (re.compile(r"^ {0,3}<\?"), re.compile(r"\?>")),
    (re.compile(r"^ {0,3}<![A-Za-z]"), re.compile(r">")),
    (re.compile(r"^ {0,3}<!\[CDATA\["), re.compile(r"\]\]>")),
)
# Any other tag opening a line starts an HTML block that runs to the next blank line (CommonMark types
# 6 and 7; type 7 cannot interrupt a paragraph, but treating it as raw anyway only errs toward raw).
_HTML_BLOCK_TAG_RE = re.compile(r"^ {0,3}</?[A-Za-z][A-Za-z0-9-]*(?:\s|/?>|$)")


def _rendered_flags(lines: list[str]) -> list[bool]:
    """For each line, True when it is block content a reader sees as markdown (not raw code or HTML)."""
    flags: list[bool] = []
    fence: tuple[str, int] | None = None
    html_end: re.Pattern[str] | None = None
    in_blank_ended_html = False
    for line in lines:
        expanded = line.expandtabs(4)
        if fence is not None:
            flags.append(False)
            closing = re.match(r"^ {0,3}(`+|~+)\s*$", expanded)
            if closing and closing.group(1)[0] == fence[0] and len(closing.group(1)) >= fence[1]:
                fence = None
            continue
        if html_end is not None:
            flags.append(False)
            if html_end.search(line):
                html_end = None
            continue
        if in_blank_ended_html:
            if not line.strip():
                in_blank_ended_html = False
                flags.append(True)
            else:
                flags.append(False)
            continue
        opened = _FENCE_OPEN_RE.match(expanded)
        if opened and not (opened.group("fence")[0] == "`" and "`" in opened.group("info")):
            fence = (opened.group("fence")[0], len(opened.group("fence")))
            flags.append(False)
            continue
        started = next(
            ((m, end_re) for start_re, end_re in _HTML_BLOCK_STARTS if (m := start_re.match(expanded))),
            None,
        )
        if started is not None:
            flags.append(False)
            start_match, end_re = started
            if not end_re.search(expanded, start_match.end()):
                html_end = end_re
            continue
        if _HTML_BLOCK_TAG_RE.match(expanded):
            flags.append(False)
            in_blank_ended_html = True
            continue
        # Indented four or more columns: indented code, or a continuation no table row starts.
        flags.append(len(expanded) - len(expanded.lstrip(" ")) < 4 or not expanded.strip())
    return flags


def _hiding_markup(line: str) -> list[str]:
    return sorted(set(_HIDING_MARKUP_RE.findall(line)))

# The approved claim sentences. Each tuple is closed: a span passes only by equalling one entry after
# whitespace normalization. Add an entry (or replace one) in the same PR that changes the row's wording.
APPROVED_GOVERNS_AGENT_TEXTS = (
    "the agent this rule binds, resolved by traversal in the daemon loader and the session rule index "
    "— decision 114, ruled 2026-09-25, `migration.md#gaps-and-contradictions-the-mapping-exposed`, G34",
)
APPROVED_SCOPE_TEXTS = (
    "legacy, **closed**: `global`, `swarm`, or `agent` — superseded by the `GOVERNS` edge, decision "
    "114, and read nowhere once the edge resolves",
)
APPROVED_AGENT_SUB_TEXTS = (
    "legacy, superseded the same way: the agent a rule binds is the `GOVERNS` edge's target",
)


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _approved(text: str, approved: tuple[str, ...]) -> bool:
    return _normalize(text) in {_normalize(a) for a in approved}


def _extract_balanced_paren(text: str, open_at: int) -> str | None:
    """Return the content between a balanced ``(...)`` starting at ``text[open_at]``, or ``None``.

    Handles arbitrary nesting depth, unlike a fixed-depth regex group.
    """
    assert text[open_at] == "("
    depth = 0
    for i in range(open_at, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[open_at + 1 : i]
    return None


def _split_top_level(cell: str, sep: str = ";") -> list[str]:
    """Split ``cell`` on ``sep`` outside any parentheses."""
    parts: list[str] = []
    depth = 0
    start = 0
    for i, ch in enumerate(cell):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(depth - 1, 0)
        elif ch == sep and depth == 0:
            parts.append(cell[start:i])
            start = i + 1
    parts.append(cell[start:])
    return parts


# The concepts table row for the agent behavioural rule type, matched on the leading cell (the concept
# name): the stable anchor, since the entity-type cell changed from `agent_policy` to `rule` (decision 120).
_AGENT_POLICY_ROW_RE = re.compile(
    r"^ {0,3}\|(?P<key>[^|]*)\|(?P<entity_type>[^|]*)\|(?P<fields>[^|]*)\|"
    r"(?P<edges>[^|]*)\|"
)

# A table row's first cell, after up to three columns of indentation.
_FIRST_CELL_RE = re.compile(r"^ {0,3}\|(?P<key>[^|]*)\|")

# Anything that can precede a table row inside a container: indentation, blockquote markers, list markers.
_CONTAINER_PREFIX_RE = re.compile(r"^(?:[ \t>]|[-*+](?=[ \t])|\d{1,9}[.)](?=[ \t]))*")


def _loose_key(line: str) -> str | None:
    """The normalized first cell of anything shaped like a table row, wherever it sits (in code, in HTML,
    in a comment, behind container markers, with or without a leading pipe), else ``None``."""
    text = unicodedata.normalize("NFKC", line)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    text = _CONTAINER_PREFIX_RE.sub("", text)
    if "|" not in text:
        return None
    first = text.lstrip("|").split("|", 1)[0]
    return _normalize_key(first)

# The relationship types this row may carry, closed: an entry naming any other backticked token (a
# `NOTE`, a `CAVEAT`) is prose dressed as an edge. Add a type here in the same PR that adds it to the row.
ALLOWED_EDGE_TYPES = frozenset({"GOVERNS", "PART_OF", "SUPERSEDES", "REFERS_TO"})

# An edges-column entry: a backticked relationship type, an arrow, then the target.
_EDGE_ENTRY_RE = re.compile(r"^\s*`(?P<type>[A-Z_]+)`\s*(?:→|->|←|<-)\s*\S")

# The GOVERNS → agent entry. `(?!\w)` after the target rejects `agent_sub` and any other longer
# identifier, while allowing the closing backtick, whitespace, or `(`.
_GOVERNS_AGENT_RE = re.compile(r"^\s*`GOVERNS`\s*(?:→|->)\s*`?agent`?(?!\w)")

_FIELD_MENTION_RE = re.compile(r"`(scope|agent_sub)`")
_FIELD_ENTRY_RE = re.compile(r"^`(?P<field>scope|agent_sub)`\s*\(")


def _malformed_edge_entries(edges_cell: str) -> list[str]:
    malformed = []
    for entry in _split_top_level(edges_cell):
        if not entry.strip():
            continue
        match = _EDGE_ENTRY_RE.match(entry)
        if not match or match.group("type") not in ALLOWED_EDGE_TYPES:
            malformed.append(entry.strip())
    return malformed


def _governs_agent_entry_is_approved(entry_rest: str) -> bool:
    """``entry_rest`` is what follows the target: exactly one approved parenthetical, then nothing."""
    rest = entry_rest.strip()
    if not rest.startswith("("):
        return False
    own_paren = _extract_balanced_paren(rest, 0)
    if own_paren is None or rest[len(own_paren) + 2:].strip():
        return False
    return _approved(own_paren, APPROVED_GOVERNS_AGENT_TEXTS)


def _has_governs_edge_entry(edges_cell: str) -> bool:
    """True when the edges cell carries exactly one ``GOVERNS`` → ``agent`` entry, and it is approved.

    A second such entry fails it even beside an approved one, the same rule the fields column applies
    to a second `scope`/`agent_sub` description: otherwise a denial rides in as another entry.
    """
    entries = [
        entry[match.end():]
        for entry in _split_top_level(edges_cell)
        if (match := _GOVERNS_AGENT_RE.match(entry))
    ]
    return len(entries) == 1 and _governs_agent_entry_is_approved(entries[0])


def _field_entry_description(entry: str) -> tuple[str, str] | None:
    """``(field, description)`` when ``entry`` is, in full, `` `field` (description) `` — nothing before
    the backtick, nothing after the closing parenthesis — else ``None``."""
    entry = entry.strip()
    match = _FIELD_ENTRY_RE.match(entry)
    if not match:
        return None
    open_at = match.end() - 1
    description = _extract_balanced_paren(entry, open_at)
    if description is None or entry[open_at + len(description) + 2 :].strip():
        return None
    return match.group("field"), description


def _has_affirmative_superseded_claim(fields_cell: str) -> bool:
    """True when `scope` and `agent_sub` are each described by exactly one fields-column entry, and that
    entry is, in full, the field and an approved parenthetical.

    Every entry that names either field must be such an entry: text before the backtick (``not``, ``~~``),
    text after the parenthesis (``— though the loader still reads it``), or a second entry naming the
    field all fail it, the same entry-level exactness the edges column applies to `GOVERNS` → `agent`.
    """
    approved = {"scope": APPROVED_SCOPE_TEXTS, "agent_sub": APPROVED_AGENT_SUB_TEXTS}
    seen = {"scope": 0, "agent_sub": 0}
    for entry in _split_top_level(fields_cell):
        mentioned = set(_FIELD_MENTION_RE.findall(entry))
        if not mentioned:
            continue
        parsed = _field_entry_description(entry)
        if parsed is None:
            return False
        field, description = parsed
        if not _approved(description, approved[field]):
            return False
        seen[field] += 1
    return seen == {"scope": 1, "agent_sub": 1}


class CorpusProblem(Exception):
    """The decision-114 corpus files are missing or unreadable."""


class AmbiguousCorpusRow(Exception):
    """More than one candidate row was found where exactly one is required."""


def _section_ranges(text: str, heading: str) -> "list[range]":
    """0-based line index ranges of each ``heading`` section: from the heading to the next ``## ``
    heading a reader sees. A ``## `` line inside fenced code or an HTML block is not a heading, so it
    neither opens nor closes a section. Headings are recognized at column 0 only; an indented one is not
    a boundary, which can only widen a section, never shorten it."""
    lines = _lines(text)
    flags = _rendered_flags(lines)
    ranges: list[range] = []
    start: int | None = None
    for i, (line, rendered) in enumerate(zip(lines, flags)):
        if rendered and line.startswith("## "):
            if start is not None:
                ranges.append(range(start, i))
                start = None
            if line.strip() == heading:
                start = i + 1
    if start is not None:
        ranges.append(range(start, len(lines)))
    return ranges


def _locate_rows(text: str, heading: str, key: str, what: str) -> "list[int]":
    """1-based line numbers of the rendered rows keyed ``key`` inside the ``heading`` section.

    A row is found only where a reader sees a table row: outside fenced code, HTML blocks and indented
    code, HTML comments blanked, up to three columns of indentation, and its first cell compared after
    NFKC normalization with format characters dropped. Raises ``AmbiguousCorpusRow`` when anything in
    the section's raw text — code, HTML, comments included — is shaped like a row with that key more than
    once, so a copy placed where the strict lookup cannot see it still makes the check ambiguous rather
    than letting either one stand in for the other.
    """
    raw = _lines(text)
    blanked = _lines(_blank_html_comments(text))
    flags = _rendered_flags(raw)
    want = _normalize_key(key)
    found: list[int] = []
    loose: list[int] = []
    for section in _section_ranges(text, heading):
        for i in section:
            if _loose_key(raw[i]) == want:
                loose.append(i + 1)
            if not flags[i]:
                continue
            match = _FIRST_CELL_RE.match(blanked[i].expandtabs(4))
            if match and _normalize_key(match.group("key")) == want:
                found.append(i + 1)
    candidates = sorted(set(found) | set(loose))
    if len(candidates) > 1:
        raise AmbiguousCorpusRow(
            f"found more than one {what} within {heading} "
            f"(lines {', '.join(map(str, candidates))}); expected exactly one"
        )
    return found


def decision_114_row(conformance_text: str) -> tuple[int, list[str]] | None:
    """(line no, cells of the raw line) for register row 114.

    Requires exactly one row keyed ``114`` inside the register section (see ``_locate_rows``); a second
    one anywhere in that section's raw text raises ``AmbiguousCorpusRow``, so a non-ruled decoy cannot
    switch the check off. The cells come from the raw line, comments included, so ``**ruled**`` hidden
    in a comment still enables the check — hiding can only turn it on.
    """
    matches = _locate_rows(conformance_text, REGISTER_HEADING, DECISION_KEY, "register row keyed `114`")
    if not matches:
        return None
    no = matches[0]
    line = _lines(conformance_text)[no - 1]
    return no, [cell.strip() for cell in line.strip().strip("|").split("|")]


def agent_policy_concepts_row(
    data_model_text: str,
) -> tuple[int, str, str, str] | None:
    """Return (line no, fields cell, edges cell, whole row) for the row.

    Requires exactly one row keyed ``agent behavioural rule`` within ``## Concepts`` (see
    ``_locate_rows``). Zero returns ``None`` (handled by the caller as "no row"); more than one raises
    ``AmbiguousCorpusRow`` rather than silently taking the first — a compliant decoy must not be able
    to mask a broken real row.
    """
    matches = _locate_rows(
        data_model_text, CONCEPTS_HEADING, CONCEPT_KEY, "`agent behavioural rule` concepts-table row"
    )
    if not matches:
        return None
    no = matches[0]
    line = _lines(_blank_html_comments(data_model_text))[no - 1].expandtabs(4)
    match = _AGENT_POLICY_ROW_RE.match(line)
    if match is None:
        return no, "", "", line
    return no, match.group("fields"), match.group("edges"), line


def check_concepts_row(
    path: Path, row_no: int, fields_cell: str, edges_cell: str
) -> list[str]:
    problems: list[str] = []
    for entry in _malformed_edge_entries(edges_cell):
        problems.append(
            f"{path}:{row_no}: decision-114-data-model — agent behavioural rule row's edges column "
            f"has an entry that is not an edge of a type in ALLOWED_EDGE_TYPES (`` `TYPE` → target (...) ``): "
            f"{entry[:80]!r}"
        )
    if not _has_governs_edge_entry(edges_cell):
        problems.append(
            f"{path}:{row_no}: decision-114-data-model — agent behavioural rule row's edges column "
            "does not carry exactly one `GOVERNS` → `agent` entry with an approved parenthetical while "
            "register row 114 is **ruled**. The entry's parenthetical must equal one of APPROVED_GOVERNS_AGENT_TEXTS in "
            "execution/scripts/check_foundation_decision_114.py; to reword it, change the approved "
            "text there in the same PR"
        )
    if not _has_affirmative_superseded_claim(fields_cell):
        problems.append(
            f"{path}:{row_no}: decision-114-data-model — agent behavioural rule row's `scope` and "
            "`agent_sub` descriptions are not the approved statements that they are superseded by the "
            "`GOVERNS` edge. Each must equal one of APPROVED_SCOPE_TEXTS / APPROVED_AGENT_SUB_TEXTS in "
            "execution/scripts/check_foundation_decision_114.py; to reword it, change the approved "
            "text there in the same PR"
        )
    return problems


def check(root: Path) -> list[str]:
    fdir = root / FOUNDATION_DIR
    conformance_path = fdir / "conformance.md"
    data_model_path = fdir / "data_model.md"
    for path in (conformance_path, data_model_path):
        if not path.is_file():
            raise CorpusProblem(
                f"expected {conformance_path} and {data_model_path} under --root {root}"
            )

    conformance_text = conformance_path.read_text(encoding="utf-8")
    try:
        row = decision_114_row(conformance_text)
    except AmbiguousCorpusRow as exc:
        return [f"{conformance_path}:1: decision-114-register — {exc}"]
    if row is None:
        return [
            f"{conformance_path}:1: decision-114-register — no register row "
            f"keyed `114` within {REGISTER_HEADING}"
        ]

    row_no, cells = row
    if len(cells) < 5 or "**ruled**" not in cells[4].lower():
        # Not yet ruled: nothing downstream is required yet (mirrors the
        # decision-101/117 no-op-before-ruled pattern).
        return []

    data_model_text = data_model_path.read_text(encoding="utf-8")
    try:
        concepts_row = agent_policy_concepts_row(data_model_text)
    except AmbiguousCorpusRow as exc:
        return [f"{data_model_path}:1: decision-114-data-model — {exc}"]
    if concepts_row is None:
        return [
            f"{data_model_path}:1: decision-114-data-model — no concepts-table "
            "row for `agent behavioural rule` while register "
            "row 114 is **ruled**"
        ]

    concepts_row_no, fields_cell, edges_cell, _whole_row = concepts_row
    problems = []
    for path, no, raw in (
        (conformance_path, row_no, _lines(conformance_text)[row_no - 1]),
        (data_model_path, concepts_row_no, _lines(data_model_text)[concepts_row_no - 1]),
    ):
        format_chars = _format_characters(raw)
        if format_chars:
            problems.append(
                f"{path}:{no}: decision-114-format-character — the row carries Unicode format "
                f"characters ({', '.join(format_chars)}), which render as nothing; they are refused on "
                "the rows decision 114's check reads, so what it verifies is what renders"
            )
        markup = _hiding_markup(raw)
        if markup:
            problems.append(
                f"{path}:{no}: decision-114-markup — the row carries markup that can hide or strike "
                f"text from a reader ({', '.join(markup)}); HTML comments, raw HTML tags and `~~` are "
                "refused on the rows decision 114's check reads, so what it verifies is what renders"
            )
    return problems + check_concepts_row(
        data_model_path, concepts_row_no, fields_cell, edges_cell
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    args = parser.parse_args(argv)

    try:
        problems = check(args.root)
    except CorpusProblem as exc:
        print(f"decision 114 check: {exc}", file=sys.stderr)
        return 1

    for problem in problems:
        print(problem)
    print(f"decision 114 check: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
