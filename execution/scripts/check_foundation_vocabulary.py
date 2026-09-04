#!/usr/bin/env python3
"""Check docs/foundation/ prose against the banned words in vocabulary.md.

`docs/foundation/vocabulary.md` ends every term entry with two lists:

    **Never:** "chip", "work item", /\\bdispatch\\w*/, "executing".
    **Not for:** "ticket" for a task; "owner" alone; /\\bopen\\s+pool\\b/ ("open" for claimable).

Never items are banned in every sense: a hit anywhere in the foundation prose fails the check (exit 1),
printed as ``file:line``. Not-for items are banned in the stated sense only: a hit is advisory, printed
with the sense so the author can judge it, and never fails the check.

An item written ``/.../`` is a regular expression (case insensitive). Any other item is a whole-word,
case-insensitive match in which a space also matches a hyphen (``"in flight"`` matches ``in-flight``).

Lines not scanned: any line carrying a Never or Not-for list; any line containing "retired" (a retired
name may be named where it is retired); in vocabulary.md, the continuation lines of a wrapped Never or
Not-for list, the ``**Related:**`` link lists, and any table row (the Verbs, Owner, and Retired tables name
banned words on purpose); fenced code blocks are scanned (mermaid labels are prose).
``status.md`` is never scanned: it reports the checkout's names, which are the old ones.

Usage:
    check_foundation_vocabulary.py [--root DIR] [--quiet-advisory] [--top N]

Exit 1 on any Never hit; 0 otherwise. Stdlib only; ``conformance.md#mechanical-checks-on-this-directory``.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

FOUNDATION_DIR = Path("docs/foundation")
VOCABULARY = "vocabulary.md"
SKIPPED_FILES = {"status.md"}

_NEVER_MARK = "**Never:**"
_NOT_FOR_MARK = "**Not for:**"
_QUOTED_RE = re.compile(r'"([^"]+)"')
_REGEX_ITEM_RE = re.compile(r"(?<![\w`])/((?:\\/|[^/])+)/(?![\w`])")


@dataclass(frozen=True)
class Ban:
    term: str  # as written in vocabulary.md
    pattern: re.Pattern[str]
    sense: str  # "" for Never; the Not-for sense text otherwise
    entry: str  # the ### heading the item sits under


@dataclass(frozen=True)
class Hit:
    ban: Ban
    file: str
    line_no: int
    text: str


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    parts = [re.escape(p) for p in phrase.strip().split()]
    body = r"[\s-]+".join(parts)
    return re.compile(rf"(?<![\w_])(?:{body})(?![\w_])", re.IGNORECASE)


def _items(list_text: str, *, first_quoted_only: bool) -> list[tuple[str, re.Pattern[str]]]:
    """The banned items in one Never or Not-for list.

    Every ``/regex/`` is an item. In a Never list every quoted phrase is an item. In a Not-for list the
    items are the regexes plus, for each ``;``-separated clause without a regex, its FIRST quoted
    phrase; later quoted phrases in a clause are the sense text (``"ticket" for a task``), as are all
    quoted phrases in a clause that carries a regex.
    """
    out: list[tuple[str, re.Pattern[str]]] = []
    for m in _REGEX_ITEM_RE.finditer(list_text):
        raw = m.group(1).replace("\\/", "/")
        try:
            out.append((f"/{raw}/", re.compile(raw, re.IGNORECASE)))
        except re.error as exc:  # a bad regex in the vocabulary is a defect in the vocabulary
            raise SystemExit(f"vocabulary.md: bad regex /{raw}/: {exc}") from exc
    # Split clauses on ';' outside regex items (a regex may itself contain ';').
    masked = _REGEX_ITEM_RE.sub(lambda m: "\x00" * len(m.group(0)), list_text)
    clauses = masked.split(";") if first_quoted_only else [masked]
    for clause in clauses:
        if first_quoted_only and "\x00" in clause:
            continue  # the regex is the item; quoted words in that clause are its sense
        quoted = [q.strip() for q in _QUOTED_RE.findall(clause) if q.strip() and q.strip() != "—"]
        if first_quoted_only:
            quoted = quoted[:1]
        for q in quoted:
            out.append((q, _phrase_pattern(q)))
    return out


def _lists(text: str) -> list[tuple[str, str, str]]:
    """(kind, list_text, entry) for every Never / Not-for list, joined across wrapped lines."""
    found: list[tuple[str, str, str]] = []
    entry = ""
    current_kind = ""
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf, current_kind
        if current_kind and buf:
            found.append((current_kind, " ".join(buf), entry))
        buf, current_kind = [], ""

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("### "):
            flush()
            entry = line[4:].strip()
            continue
        if line.startswith(_NEVER_MARK):
            flush()
            current_kind = "never"
            buf = [line[len(_NEVER_MARK):]]
            continue
        if line.startswith(_NOT_FOR_MARK):
            flush()
            current_kind = "not_for"
            buf = [line[len(_NOT_FOR_MARK):]]
            continue
        if current_kind:
            if not line or line.startswith("**") or line.startswith("#") or line.startswith("|"):
                flush()
            else:
                buf.append(line)
    flush()
    return found


def _sense_for(list_text: str, item: str) -> str:
    """The clause (split on ';') that carries this Not-for item."""
    for clause in list_text.split(";"):
        if item.strip("/") in clause or item in clause:
            return " ".join(clause.split())
    return " ".join(list_text.split())


def parse_bans(vocab_text: str) -> tuple[list[Ban], list[Ban]]:
    never: list[Ban] = []
    not_for: list[Ban] = []
    for kind, list_text, entry in _lists(vocab_text):
        for term, pattern in _items(list_text, first_quoted_only=(kind == "not_for")):
            if kind == "never":
                never.append(Ban(term, pattern, "", entry))
            else:
                not_for.append(Ban(term, pattern, _sense_for(list_text, term), entry))
    return never, not_for


def _scannable(line: str, *, is_vocab: bool) -> bool:
    if _NEVER_MARK in line or _NOT_FOR_MARK in line:
        return False
    if "retired" in line.lower():
        return False
    if is_vocab and line.lstrip().startswith("|"):
        return False
    return True


def scan(root: Path, never: list[Ban], not_for: list[Ban]) -> tuple[list[Hit], list[Hit]]:
    never_hits: list[Hit] = []
    advisory: list[Hit] = []
    fdir = root / FOUNDATION_DIR
    for path in sorted(fdir.glob("*.md")):
        if path.name in SKIPPED_FILES:
            continue
        is_vocab = path.name == VOCABULARY
        rel = str(path.relative_to(root))
        skip_section = False
        in_ban_list = False
        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.startswith("## "):
                skip_section = is_vocab and "retired" in line.lower()
            if is_vocab:
                # A Never / Not-for list may wrap; its continuation lines are part of the list.
                stripped = line.strip()
                if stripped.startswith((_NEVER_MARK, _NOT_FOR_MARK)):
                    in_ban_list = True
                    continue
                if in_ban_list and stripped and not stripped.startswith(("**", "#", "|")):
                    continue
                in_ban_list = False
                if stripped.startswith("**Related:**") or (stripped.startswith("[") and "](#" in stripped):
                    continue  # link lists name terms, not prose
            if skip_section or not _scannable(line, is_vocab=is_vocab):
                continue
            for ban in never:
                if ban.pattern.search(line):
                    never_hits.append(Hit(ban, rel, no, line.strip()))
            for ban in not_for:
                if ban.pattern.search(line):
                    advisory.append(Hit(ban, rel, no, line.strip()))
    return never_hits, advisory


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    ap.add_argument("--quiet-advisory", action="store_true", help="print only the advisory summary")
    ap.add_argument("--top", type=int, default=5, help="most common advisory terms to list")
    args = ap.parse_args(argv)

    vocab_path = args.root / FOUNDATION_DIR / VOCABULARY
    if not vocab_path.is_file():
        print(f"no {vocab_path}; nothing to check")
        return 0
    never, not_for = parse_bans(vocab_path.read_text(encoding="utf-8"))
    if not never:
        print("vocabulary.md declares no Never items; the check would pass vacuously")
        return 1
    never_hits, advisory = scan(args.root, never, not_for)

    for h in never_hits:
        print(f"NEVER {h.file}:{h.line_no}: {h.ban.term} (entry: {h.ban.entry}): {h.text[:120]}")
    if not args.quiet_advisory:
        for h in advisory:
            print(f"not-for {h.file}:{h.line_no}: {h.ban.term} — {h.ban.sense[:80]}: {h.text[:100]}")
    counts = Counter(h.ban.term for h in advisory)
    top = ", ".join(f"{t} ({n})" for t, n in counts.most_common(args.top))
    print(
        f"vocabulary check: {len(never)} Never items, {len(not_for)} Not-for items; "
        f"{len(never_hits)} Never hit(s); {len(advisory)} Not-for advisory hit(s)"
        + (f"; most common: {top}" if top else "")
    )
    return 1 if never_hits else 0


if __name__ == "__main__":
    sys.exit(main())
