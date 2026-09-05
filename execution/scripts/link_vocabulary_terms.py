#!/usr/bin/env python3
"""Link the first mention of every defined term, in every entry and section of vocabulary.md.

`docs/foundation/vocabulary.md` defines its terms as `###` headings and expects a definition to link the
other terms it leans on **inline, at the point it uses them** — the link belongs where the reader meets
the word. Doing that by hand across ~98 entries does not hold: a revision that removed the per-entry
`**Related:**` lists left hundreds of unlinked mentions behind. This script does it mechanically, and
re-runs whenever a term is added.

**The rule it applies** (stated in the document itself, under "Scope"):

* The **first** mention of a term in an entry body is linked; later mentions in the same entry are not.
  Repeated links in one short entry are noise.
* A term is **never linked inside its own entry** — self-links say nothing.
* The introductory prose and the section-level prose (the paragraphs under `##` headings) are linked on
  the same first-mention-per-block basis.
* Mentions are matched on the word **as it appears**, including plurals and inflections: `task`/`tasks`,
  `claim`/`claims`/`claimed`, `sign-off`/`sign-offs`. A hyphenated term also matches its spaced spelling.
* **Multi-word terms win over their parts**: `step owner` is linked whole, never as `step` plus `owner`,
  and a word sitting inside a longer defined term is left alone.
* Nothing is linked inside a `**Never:**` or `**Not for:**` list, including its wrapped continuation
  lines — those name forbidden words, and a link would imply the word is canonical.
* Nothing is linked inside a code span, a fenced block, an existing link, a bold run-in label, a heading,
  a table row, a `**See:**` / `**Related:**` citation list, or an HTML comment.

**What it deliberately does not link.** Some terms are ordinary English words that this vocabulary
also binds to a specific sense — `status`, `active`, `held`, `created`, `condition`, `chain`, `stage`,
`coverage`, `delivery`, `grant`, `approval`, `initiative`, `record`, `subject`, and the two verb entries
`execute (a task)` and `take (an action)`. Linking their first occurrence mislabels the ordinary use:
"bans are held as regular expressions" is not the lease state, "the owner takes a view" is not the action
verb, and "the record a step owner writes" is not the store. Their
mentions are left to the author, who knows which sense is meant. `GENERIC` below is that list; a wrong
link is worse than none.

Usage:
    link_vocabulary_terms.py [--root DIR] [--check] [--report]

``--check`` writes nothing and exits 1 if any linkable first mention is unlinked (this is what CI runs).
``--report`` prints the per-block detail. Stdlib only.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FOUNDATION_DIR = Path("docs/foundation")
VOCABULARY = "vocabulary.md"

# Lines whose content is never linked. The citation lists are links already; headings and table rows are
# structure, not prose.
_SKIP_PREFIXES = ("**See:**", "**Related:**", "#", "|", ">", "- **Never:**", "- **Not for:**")
_BAN_MARKS = ("**Never:**", "**Not for:**")

# Inflections tried for each term, longest first so that `sign-offs` beats `sign-off`.
_SUFFIXES = ("", "s", "es", "d", "ed", "ing")

# Terms that are also ordinary English words. See the module docstring: their mentions are left to the
# author, because the script cannot tell the bound sense from the everyday one.
GENERIC = frozenset(
    {
        "active",
        "approval",
        "chain",
        "condition",
        "coverage",
        "created",
        "delivery",
        "execute (a task)",
        "grant",
        "held",
        "initiative",
        "record",
        "stage",
        "subject",
        "status",
        "take (an action)",
        "terminal",
        "unknown",
    }
)


def anchor(heading: str) -> str:
    """GitHub's anchor for a heading — the same rule check_foundation_anchors.py applies."""
    text = re.sub(r"`", "", heading)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\- ]", "", text)
    return text.replace(" ", "-")


def _base(term: str) -> str:
    """The term without its parenthetical disambiguator: ``execute (a task)`` -> ``execute``."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", term).strip()


def _variants(term: str) -> list[str]:
    """The surface forms of a term, longest first.

    A term written with a hyphen also matches the spaced spelling and vice versa, because the vocabulary
    itself says a phrase matches across a space or a hyphen.
    """
    base = _base(term)
    if not base:
        return []
    stems = {base}
    if base.endswith("e"):
        stems.add(base[:-1])  # execute -> execut(ing)
    if base.endswith("y"):
        stems.add(base[:-1] + "i")  # entity -> entit(ies)
    forms = {stem + suf for stem in stems for suf in _SUFFIXES}
    forms = {f for f in forms if len(f) >= len(base)}
    return sorted(forms, key=len, reverse=True)


def _pattern(forms: list[str]) -> re.Pattern[str]:
    alts = [r"[\s-]+".join(re.escape(p) for p in re.split(r"[\s-]+", f) if p) for f in forms]
    return re.compile(rf"(?<![\w-])(?:{'|'.join(alts)})(?![\w-])", re.IGNORECASE)


class Term:
    __slots__ = ("heading", "anchor", "pattern", "words", "generic")

    def __init__(self, heading: str) -> None:
        self.heading = heading
        self.anchor = anchor(heading)
        self.pattern = _pattern(_variants(heading)) if _variants(heading) else None
        self.words = len(re.split(r"[\s-]+", _base(heading))) if _base(heading) else 0
        self.generic = heading in GENERIC


def collect_terms(text: str) -> list[Term]:
    """Every ``###`` entry, ordered so that multi-word terms are tried before their parts."""
    terms = [Term(l.strip()[4:].strip()) for l in text.splitlines() if l.strip().startswith("### ")]
    terms = [t for t in terms if t.pattern is not None]
    terms.sort(key=lambda t: (-t.words, -len(t.heading)))
    return terms


def _masked(line: str, longer: list[Term]) -> str:
    """The line with every span that must not be linked replaced by NULs of the same length.

    Offsets are preserved, so a match found in the mask is a valid offset in the real line. ``longer``
    is the multi-word terms already tried on this line: masking their occurrences is what stops ``step``
    from being linked inside ``step owner``.
    """
    out = list(line)

    def blank(start: int, end: int) -> None:
        for i in range(start, end):
            out[i] = "\x00"

    for rx in (
        r"`[^`]*`",  # code spans
        r"\[[^\]]*\]\([^)]*\)",  # existing links, text and target
        r"\*\*[^*]+\*\*",  # bold run-in labels (**Definition:**, **Use:**)
        r"<!--.*?-->",  # HTML comments
    ):
        for m in re.finditer(rx, line):
            blank(m.start(), m.end())
    for term in longer:
        for m in term.pattern.finditer(line):
            blank(m.start(), m.end())
    return "".join(out)


def _blocks(lines: list[str]) -> list[tuple[str, list[int]]]:
    """(block key, linkable line indexes) for every block that gets its own first-mention budget.

    A block is one ``###`` entry, or the run of prose under a ``##`` section heading before its first
    entry, or the file's introductory prose. Fenced code blocks and ban lists (with their wrapped
    continuation lines) are excluded outright.
    """
    blocks: list[tuple[str, list[int]]] = []
    key = "__intro__"
    current: list[int] = []
    in_fence = False
    in_ban = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped.startswith(_BAN_MARKS):
            in_ban = True
            continue
        if in_ban:
            # A ban list runs until a blank line, another bold label, a heading, or a table row.
            if stripped and not stripped.startswith(("**", "#", "|")):
                continue
            in_ban = False
        if stripped.startswith("### "):
            blocks.append((key, current))
            key = stripped[4:].strip()
            current = []
            continue
        if stripped.startswith("## "):
            blocks.append((key, current))
            key = "__section__" + stripped[3:].strip()
            current = []
            continue
        if stripped and not stripped.startswith(_SKIP_PREFIXES):
            current.append(i)
    blocks.append((key, current))
    return blocks


def link(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Return the relinked text and the (block, term) pairs that were newly linked."""
    terms = collect_terms(text)
    lines = text.splitlines()
    linked: list[tuple[str, str]] = []

    for key, idxs in _blocks(lines):
        own = key if not key.startswith("__") else None
        # A term already linked somewhere in this block keeps that link and gets no second one.
        block_text = "\n".join(lines[i] for i in idxs)
        done = {m.group(1) for m in re.finditer(r"\]\(#([\w-]+)\)", block_text)}
        for i in idxs:
            line = lines[i]
            longer: list[Term] = []
            for term in terms:
                if term.words > 1:
                    longer.append(term)  # mask this term's spans for every shorter term after it
                if term.generic or term.anchor in done:
                    continue
                if own is not None and term.heading == own:
                    continue  # never link a term inside its own entry
                m = term.pattern.search(_masked(line, [t for t in longer if t is not term]))
                if not m:
                    continue
                surface = line[m.start() : m.end()]
                line = f"{line[:m.start()]}[{surface}](#{term.anchor}){line[m.end():]}"
                done.add(term.anchor)
                linked.append((key, term.heading))
            lines[i] = line
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), linked


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    ap.add_argument("--check", action="store_true", help="write nothing; exit 1 if a first mention is unlinked")
    ap.add_argument("--report", action="store_true", help="print the per-block detail")
    args = ap.parse_args(argv)

    path = args.root / FOUNDATION_DIR / VOCABULARY
    if not path.is_file():
        print(f"no {path}; nothing to link")
        return 0
    text = path.read_text(encoding="utf-8")
    out, linked = link(text)

    if args.report:
        by_block: dict[str, list[str]] = {}
        for block, term in linked:
            by_block.setdefault(block, []).append(term)
        for block in sorted(by_block):
            print(f"{block}: {', '.join(sorted(by_block[block]))}")

    blocks = len({b for b, _ in linked})
    if args.check:
        if linked:
            print(
                f"link check: {len(linked)} unlinked first mention(s) across {blocks} block(s); "
                f"run link_vocabulary_terms.py to fix"
            )
            return 1
        print("link check: every linkable first mention is linked")
        return 0

    if out != text:
        path.write_text(out, encoding="utf-8")
    print(f"linked {len(linked)} first mention(s) across {blocks} block(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
