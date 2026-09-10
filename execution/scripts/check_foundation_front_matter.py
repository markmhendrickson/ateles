#!/usr/bin/env python3
"""Check decision 74's two rules mechanically, so neither re-accretes.

Decision 74 (``conformance.md#a-documents-amendment-history-lives-in-revisionsmd-\
not-in-its-front-matter``) rules both halves of what a reader hits first:

1. **A document's amendment history lives in** ``revisions.md``. Its front matter
   carries a pointer to that table, never a revision chain. Before the rule, the
   chain on ``conformance.md`` had reached 28 clauses and 9,462 characters
   standing between the title and the first rule it states.

2. **A section stating three or more full-sentence rules opens with the list of
   them** — the existing bold lead sentences, verbatim, before the prose.

Both were fixed once by hand. Neither stays fixed on its own: the amendment
obligation makes a chain grow one clause per pass, and a section grows one rule
at a time until nobody notices there are eight. This check is what stops the
pattern re-accreting, which is the whole reason the rule was worth ruling.

What it reports:

``front-matter-chain``
    a document whose front matter still carries a ``Revised by …`` clause. The
    fix is to move the clause verbatim into that document's table in
    ``revisions.md`` and leave a pointer behind.

``missing-pointer``
    a document with no revision chain and no ``revisions.md`` pointer either, so
    a reader has nowhere to go for its history.

``missing-index``
    a section stating ``INDEX_MIN`` or more full-sentence rules with no
    ``**The rules in this section.**`` list opening it.

A LABEL lead (``**Tenant.**``, ``**Why.**``, ``**Matrix.**``) is not a rule and
is not counted: indexing labels would make the index noise, which is the failure
the rule's own wording guards against. Neither is a field label of a structured
entry (``**Definition:**`` in a vocabulary term), nor a connective that
continues the argument above it (``**So …**``, ``**And …**``).

``status.md`` is exempt from both rules: it is a dated report, never keyed and
never inlined, its revisions are its own sections rather than a front-matter
chain, and it is deliberately outside the reading list
(``conformance.md#read-when-these-paths-changed``). ``revisions.md`` is exempt
because it *is* the companion. ``decision_state.md`` is exempt because it is a
render target: regenerated rather than amended, so a revisions row per run would
record the generator's runs and not a change to any claim.

Stdlib only; registered in ``conformance.md#mechanical-checks-on-this-directory``.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FOUNDATION_DIR = Path("docs/foundation")

# Documents neither rule applies to, and why.
EXEMPT = {
    "status.md",  # a dated report; outside the reading list, revisions are sections
    "revisions.md",  # the companion the history moves into
    # A render target has no amendment history of its own: it is regenerated,
    # never amended, and a revisions row per regeneration would record the
    # generator's runs rather than a change to a claim. Its history is the
    # generator's, in git. Same ground as status.md -- generated, never keyed,
    # never inlined -- and it carries a GENERATED banner naming the script.
    "decision_state.md",
}

INDEX_LEAD = "**The rules in this section.**"
INDEX_MIN = 3

_CLAUSE_RE = re.compile(r"Revised (?:by |for |\d{4}-)")
_POINTER_RE = re.compile(r"revisions\.md#", re.I)
_HEADING_RE = re.compile(r"^(#{2,6}) +(.*?)\s*$", re.M)
_BOLD_LEAD_RE = re.compile(r"\*\*(.+?)\*\*", re.S)

# A structured entry's field label — the bold opener of a vocabulary term, a
# workflow spec, or a ruling block. Never a rule the section states in its own
# right.
_FIELD_LABEL_RE = re.compile(
    r"^(Definition|See|Never|Not for|Enforced by|Sources?|Prior art|Home|Kind|"
    r"Derived from|Keyed document|Status|Owner|Reads|Writes|Verdict|Instrument|"
    r"Expected|Rule|Test|Class|Anchor|Note|Example|Why|Matrix|Cost accepted|"
    r"What would reopen it|Purpose|Entry condition|Steps|Closes on|Step owner|"
    r"Produces|Consumes|Trigger|Preconditions|Postconditions|Inputs|Outputs|"
    r"Roles|Fields|Shape|Form|Default|Decided|Open|Ruled|Scope|Reason)\b[:. ]",
    re.I,
)

# A lead opening with a conjunction continues the paragraph above it.
_CONNECTIVE_RE = re.compile(
    r"^(So|And|But|Then|Also|Yet|Or|Because|Which|That|Hence|Therefore|"
    r"However|Still|Read together|Read from)\b",
    re.I,
)

# A finite-clause marker: what separates a claim from a noun-phrase label.
_CLAIM_RE = re.compile(
    r"\b(is|are|was|were|has|have|does|do|must|may|never|always|carries|holds|"
    r"counts|reads|writes|stays|remains|becomes|opens|raises|resolves|refuses|"
    r"requires|names|ruled|settled|precedes|applies|belongs|lives|runs|returns|"
    r"fails|stops|takes|makes|gives|sets|goes|cannot|not)\b",
    re.I,
)

# Short noun phrases used across the corpus as argument scaffolding.
_LABEL_WORDS = {
    "why",
    "matrix",
    "cost accepted",
    "what would reopen it",
    "the question",
    "the answer",
    "tenant",
    "ownership",
    "what stops",
    "who confirms",
    "who may propose",
    "the mapping down",
    "why the seat alone",
    "the limit this does not resolve",
    "what this does not settle",
    "what this leaves open",
    "the cost",
    "the alternative",
    "the finding",
}


def front_matter(text: str) -> str:
    """Everything before the first ``## `` heading."""
    m = re.search(r"^## ", text, re.M)
    return text[: m.start()] if m else text


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def is_rule(lead: str) -> bool:
    """True when a bold lead states a rule rather than labelling an argument."""
    t = lead.strip()
    if t == INDEX_LEAD.strip("*"):
        return False
    if _FIELD_LABEL_RE.match(t) or _CONNECTIVE_RE.match(t):
        return False
    if t.rstrip(".").strip().lower() in _LABEL_WORDS:
        return False
    if re.match(r"^(What|Which|Who|Whether|Why|How|Nothing|No)\b", t, re.I) and not re.search(
        r"\b(is|are|must|never|always|may)\b", t, re.I
    ):
        return False
    if len(re.findall(r"[\w'`-]+", t)) < 5:
        return False
    return bool(_CLAIM_RE.search(t))


def bold_leads(body: str) -> list[str]:
    """Bold openers of the paragraphs in ``body``."""
    out = []
    for para in re.split(r"\n\s*\n", body):
        p = para.strip()
        if p.startswith("**"):
            m = _BOLD_LEAD_RE.match(p)
            if m:
                out.append(m.group(1).strip())
    return out


def sections(text: str) -> list[tuple[str, int, str, list[str]]]:
    """``(title, heading_offset, body, level-3 subsection titles)`` per ``##``."""
    heads = [(len(m.group(1)), m.group(2), m.start(), m.end()) for m in _HEADING_RE.finditer(text)]
    out = []
    for i, (lvl, title, start, head_end) in enumerate(heads):
        if lvl != 2:
            continue
        end = len(text)
        for lvl2, _t, s2, _e in heads[i + 1 :]:
            if lvl2 == 2:
                end = s2
                break
        subs = [t for lvl2, t, s2, _e in heads[i + 1 :] if s2 < end and lvl2 == 3]
        out.append((title, start, text[head_end:end], subs))
    return out


def rule_count(body: str, subs: list[str]) -> int:
    """How many rules a section states, by the same reading the rule uses.

    A section whose subsections are a numbered series, or are short terms, is
    already its own list — a second copy above it would be the duplicate
    statement principle 9 forbids — so it counts as zero.
    """
    if subs:
        if sum(1 for t in subs if re.match(r"\d+\.", t)) >= 2:
            return 0
        if sum(1 for t in subs if len(t.split()) <= 3) >= max(3, len(subs) * 0.6):
            return 0
        return len(subs)
    return sum(1 for lead in bold_leads(body) if is_rule(lead))


def check(root: Path) -> list[str]:
    problems: list[str] = []
    for path in sorted(root.glob("*.md")):
        if path.name in EXEMPT:
            continue
        text = path.read_text(encoding="utf-8")
        fm = front_matter(text)

        chain = list(_CLAUSE_RE.finditer(fm))
        if chain:
            problems.append(
                f"{path}:{line_of(text, chain[0].start())}: front-matter-chain — "
                f"{len(chain)} revision clause(s) still in the front matter; move each "
                f"verbatim into revisions.md#{re.sub(r'[^a-z0-9_-]', '', path.name.lower())} "
                f"and leave a pointer (decision 74)"
            )
        elif not _POINTER_RE.search(fm):
            problems.append(
                f"{path}:1: missing-pointer — no revision chain and no "
                f"`revisions.md#…` pointer, so the document's amendment history "
                f"has no home a reader can reach (decision 74)"
            )

        for title, offset, body, subs in sections(text):
            n = rule_count(body, subs)
            if n >= INDEX_MIN and not body.lstrip().startswith(INDEX_LEAD):
                problems.append(
                    f"{path}:{line_of(text, offset)}: missing-index — "
                    f'"## {title}" states {n} rules with no "{INDEX_LEAD}" list '
                    f"opening it (decision 74)"
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
        print(f"front matter check: {args.root} is not a directory", file=sys.stderr)
        return 1

    problems = check(args.root)
    for p in problems:
        print(p)
    print(f"front matter check: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
