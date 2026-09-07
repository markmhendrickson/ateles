#!/usr/bin/env python3
"""Check that no foundation document cites a commit, issue, or pull request as state.

``conformance.md#mechanical-checks-on-this-directory`` registers this script under *Decision
citations*, and states what fails on it in two clauses:

    a commit hash in any document here but ``status.md``; an issue or pull-request number outside
    the positions ``#phases-and-implementation-state`` names

That section gives both clauses their syntactic form, so that this lint can read the rule rather
than a reviewer sensing it:

    a commit hash appears in no document in this directory but ``status.md``; an issue or
    pull-request number appears only in a document's ``**Derived from:**`` header, in a
    ``Sources:`` clause, or in its *Scope*, *Contradictions this document settles*, *Prior art*, or
    *Beyond the sources* section — the positions where a document names what it derived from — and a
    number anywhere else is a state claim and fails.

The underlying rule is the first of the two in that section: a foundation document never carries
"today", "on main", a commit hash, a count, or an open-issue reference as evidence a defect is live;
an issue or PR is cited only as the record of a decision, never as state. ``status.md`` is the one
document that records what a checkout implements, so it is exempt from both clauses.

Stdlib only; registered in ``conformance.md#mechanical-checks-on-this-directory``.

Scope, and what this check deliberately does not catch
------------------------------------------------------

**It reads position, not meaning.** Both clauses above are syntactic by design — the corpus wrote
them that way *so that* a script could enforce them. This check therefore decides only where a
number appears, never whether the sentence around it reads as a state claim. A PR number correctly
placed in a ``Sources:`` clause passes even if the sentence misuses it; a number in the body fails
even if the prose is careful. That is the contract's own trade, not a softening applied here.

**It does not check decision status against the register.** A natural neighbour of this check would
be to catch prose calling a decision *open* that ``conformance.md``'s register records as *ruled* —
the defect that put three keyed adapter documents out of step with register row 15 for two days
(issue #801). It is deliberately not implemented here, for two reasons.

The first is the contract: the row this script is registered under names commit hashes and
issue/PR numbers, and nothing else. Widening a registered check beyond the row that registers it
would make the register a description of what the script happened to grow into.

The second is that the register is not on one branch. The foundation corpus lives on a stack of
branches and the register's rows land on different ones — rows above the highest this checkout can
see exist on branches it cannot read. A decision-status check run here would have to treat a number
absent from the register as either a violation (a false positive on every row that has not landed
yet) or a pass (silence on exactly the rows most likely to be wrong). Neither is worth wiring, and
the same hazard is why the corpus states it explicitly under *Mechanical checks on this directory*:
"A check merged to ``main`` binds only branches descended from that merge."

If that check is wanted, it belongs in its own registered row, and its handling of an unseen
register row has to be argued in the register rather than chosen by a script.

**Historical narration is not distinguished from live claims**, and does not need to be under the
rule as written: a commit hash is banned outright outside ``status.md``, and an issue number is
judged by where it sits. A sentence recounting what a past pull request decided is legitimate in a
``Sources:`` clause and fails in the body, whichever tense it uses.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

FOUNDATION_DIR = Path("docs/foundation")

# status.md is the one document that records what a checkout implements, and both clauses exempt it.
EXEMPT = {"status.md"}

# The sections in which a document names what it derived from. conformance.md#phases-and-implementation-state
# enumerates these four by name; matched on the heading text, case-insensitively, so that a document
# whose heading carries trailing words ("Prior art and adjacent systems") still counts as that section.
SOURCE_SECTIONS = (
    "scope",
    "contradictions this document settles",
    "prior art",
    "beyond the sources",
)

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")

# An issue or pull-request number: "#744", "PR #745", "neotoma#1972", "issue #378". Two digits or
# more, so that a list item "#1" or an ordinal is not swept in. A leading word character or slash is
# excluded so that an anchor fragment (#the-adapter-...) and a colour literal are not numbers here.
_NUM_RE = re.compile(r"(?<![\w/])#(\d{2,})\b")

# A commit hash: a bare 7-to-40-character lowercase hex run. Bounded by non-word characters on both
# sides so that a hex substring inside a longer identifier is not a hash. Entity ids (ent_8104c8...)
# and anchors are excluded by the leading boundary; a run that is all digits is a number, not a
# hash, and is left to _NUM_RE.
_HASH_RE = re.compile(r"(?<![\w-])([0-9a-f]{7,40})(?![\w-])")
_ALL_DIGITS = re.compile(r"^\d+$")

# `Sources: ...` introduces a citation clause anywhere in a document; the corpus uses it inline at
# the end of an invariant's paragraph, not as a heading. The clause runs to the end of its
# paragraph and the corpus hard-wraps at ~110 characters, so it routinely spans several lines —
# principles.md invariant 12 opens "Sources: operator memo," and carries "PR #745" onto the next
# line. A clause is therefore treated as open until a blank line ends the paragraph; judging only
# the line the word "Sources:" appears on would fail every wrapped citation in the corpus.
_SOURCES_RE = re.compile(r"\bSources:", re.I)


class MissingCorpus(Exception):
    """The directory this check inspects is absent or empty.

    Exiting 0 on a missing corpus reports a pass for a check that never ran — the "reports without
    binding" defect the foundation documents name. The check fails closed instead, naming the root
    it inspected so a wrong ``--root`` is distinguishable from a genuinely missing directory.
    """


def _in_source_section(heading: str | None) -> bool:
    """True while the reader is inside one of the four sections that name a document's sources."""
    if heading is None:
        return False
    h = heading.strip().lower().strip("*_` ")
    return any(h.startswith(s) for s in SOURCE_SECTIONS)


def check(root: Path) -> list[str]:
    fdir = root / FOUNDATION_DIR
    if not fdir.is_dir():
        raise MissingCorpus(
            f"no {fdir} (looked under --root {root}); nothing was checked. "
            f"Run from the repo checkout, or pass --root pointing at one."
        )
    files = sorted(fdir.glob("*.md"))
    if not files:
        raise MissingCorpus(
            f"{fdir} contains no .md files (looked under --root {root}); nothing was checked. "
            f"Run from the repo checkout, or pass --root pointing at one."
        )

    violations: list[str] = []
    for path in files:
        if path.name in EXEMPT:
            continue
        rel = str(path.relative_to(root))
        heading: str | None = None
        in_front_matter = True  # from the title to the first ## heading
        in_fence = False
        in_sources = False  # inside a wrapped `Sources:` clause, until the paragraph ends

        for no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                continue
            if not line.strip():
                in_sources = False  # a blank line ends the paragraph, and with it the clause
                continue

            m = _HEADING_RE.match(line)
            if m:
                if len(m.group(1)) >= 2:
                    in_front_matter = False
                heading = m.group(2)
                in_sources = False
                continue

            if _SOURCES_RE.search(line):
                in_sources = True

            # Clause 1: a commit hash appears in no document here but status.md. Unconditional —
            # the rule names no position where one is allowed.
            for h in _HASH_RE.findall(line):
                if _ALL_DIGITS.match(h):
                    continue  # a run of digits is a number; clause 2 judges it by position
                violations.append(
                    f"{rel}:{no}: commit hash {h} — a commit hash appears in no document "
                    f"in this directory but status.md "
                    f"(conformance.md#phases-and-implementation-state)"
                )

            # Clause 2: an issue or pull-request number only where a document names its sources.
            nums = _NUM_RE.findall(line)
            if not nums:
                continue
            allowed = in_front_matter or in_sources or _in_source_section(heading)
            if allowed:
                continue
            where = f"section {heading!r}" if heading else "the body"
            for n in nums:
                violations.append(
                    f"{rel}:{no}: issue/PR number #{n} in {where} — a number outside the "
                    f"**Derived from:** header, a Sources: clause, or the Scope / Contradictions "
                    f"this document settles / Prior art / Beyond the sources sections is a state "
                    f"claim (conformance.md#phases-and-implementation-state)"
                )
    return violations


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[2])
    args = ap.parse_args(argv)
    try:
        violations = check(args.root)
    except MissingCorpus as exc:
        print(f"citation check: {exc}")
        return 1
    for v in violations:
        print(v)
    print(f"citation check: {len(violations)} citation violation(s)")
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
