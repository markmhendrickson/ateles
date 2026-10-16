#!/usr/bin/env python3
"""Compare CLAUDE.md rule-by-rule between two revisions, so a divergence can
never again be summarised by hand and come out short.

The failure this exists to catch (ateles#973): a hand-assembled summary of a
bidirectional `CLAUDE.md` divergence named FOUR worktree-only rules.
Phrase-searching every bolded lead found EIGHT — and the four missed included
``NEVER `git stash` `` and `One worktree, one agent`, both safety rules. The
transferable lesson is that a divergence summary assembled by reading is not a
substitute for a mechanical per-rule comparison: the rules that go missing are
the ones nobody thought to look for.

A diff-line count is not sufficient either. A union merge of two diverged
copies can show six deletions that are all same-rule replacements, so a small
diff looks complete while a rule name has vanished.

So: enumerate rule identities mechanically. The **bolded lead** of a bullet
(`- **Rule name.** body…`) is the natural key, alongside every `#`-level
heading. Build the key set for each side, and report any key present on one
side and absent on the other, in BOTH directions.

Usage
-----
Three-way merge check (the acceptance gate for a union merge):

    scripts/verify_claude_md_merge.py \\
        --base origin/main:CLAUDE.md \\
        --other /path/to/other/CLAUDE.md \\
        --merged CLAUDE.md

CI regression check — **pass every side whose rules must survive**, not just
main. This is the invocation that binds:

    scripts/verify_claude_md_merge.py --check \\
        --base origin/main:CLAUDE.md \\
        --other HEAD:CLAUDE.md \\
        --merged CLAUDE.md \\
        --dedups scripts/claude_md_dedups.txt

A main-only base is **not sufficient** for a file that recovered rules main
never had, and getting this wrong reproduces the very bug being fixed. Proved
while building this script: deleting ``NEVER `git stash` `` from the merged
file passed a `--base origin/main` check cleanly, because that rule is
worktree-only and absent from main — so main cannot witness its loss. Adding
the file's own committed revision as an input side caught it by name. Every
rule set the merged file is supposed to contain must appear as an input side,
or the check is one-directional and blind exactly where the divergence lives.

Deliberate dedups
-----------------
A union merge legitimately drops a rule *name* when both sides stated the same
rule and one phrasing was kept. That is a judgement a human makes clause by
clause, so it cannot be inferred — but it also must not be silently tolerated,
or the gate stops binding. Record each one in `--dedups <file>`, one per line
as `<dropped lead> => <surviving lead>`:

    Proceed on your recommendation instead of stopping to ask. => Proceed with your recommendation — don't ask.

Both sides of the arrow are checked. The dropped lead must be absent from the
merged file and the surviving lead must be present — so a stale entry, or one
naming a survivor that later vanished too, fails the gate instead of excusing
it. `# comments` and blank lines are ignored. The repo's own record lives in
`scripts/claude_md_dedups.txt`.

A path argument is read from disk. An argument containing `:` and no leading
`/` or `.` is read as a git ref (`<ref>:<path>`) via `git show` — no network
access, no credentials, no remote URLs printed. A ref that cannot be resolved
is a hard error, never a silently skipped check: per
`docs/foundation/principles.md#5`, "could not verify" must fail the gate
rather than pass it.

Exit codes
----------
0  every rule on every input side is present in the merged file
1  at least one rule was lost (each named, with the side it came from)
2  a usage or input error (missing file, unresolvable ref) — the check did
   not run, which is NOT the same as passing

Stdlib only. No network. No Neotoma. Reads only the paths and refs it is
given.
"""

from __future__ import annotations

import argparse
import difflib
import re
import subprocess
import sys
from dataclasses import dataclass, field
from typing import NoReturn

# A rule bullet: optional indent, a list marker, then a bolded lead. The lead
# is everything inside the first `**…**` run. Markdown also permits `__…__`
# emphasis, so accept both.
_BULLET_LEAD = re.compile(
    r"""^\s*[-*+]\s+           # list marker
        (?:\*\*(?P<a>.+?)\*\*  # **lead**
          |__(?P<b>.+?)__)     # __lead__
    """,
    re.VERBOSE,
)

# A heading: one to six hashes, then the text. Headings are rule identities
# too — a section silently renamed or dropped loses everything under it.
_HEADING = re.compile(r"^(?P<hashes>#{1,6})\s+(?P<text>.+?)\s*$")

# A fenced code block must not contribute keys: a bolded line inside an example
# is illustration, not a rule.
_FENCE = re.compile(r"^\s*(?:```|~~~)")

# Inline markup stripped during normalization, so `**Rule.**`, `**Rule**` and
# `` **`Rule`** `` all collapse to the same key.
_NORMALIZE_STRIP = re.compile(r"[`*_~]")
_PUNCT_TAIL = re.compile(r"[\s.,;:!?—–-]+$")
_WHITESPACE = re.compile(r"\s+")

# Leads similar enough to warrant a human's eye during a dedup review. Tuned
# so the real pair the issue keeps as two rules — `Dispatch, don't work inline`
# vs `Dispatch, don't drift inline`, which differ by one word — is surfaced.
# Surfacing is all this does: near-duplicates are never collapsed.
NEAR_DUPLICATE_RATIO = 0.85

# Exit 2 means the check did not run — a distinct signal from exit 1, "a rule
# was lost". Collapsing the two would let a broken invocation read as a
# finding, or worse, a typo'd path read as a pass.
EXIT_USAGE = 2


def die(message: str) -> NoReturn:
    """Report an input error and exit 2. Never exit 1 from here."""
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(EXIT_USAGE)


def normalize(lead: str) -> str:
    """Collapse a bolded lead to a comparison key.

    Case, inline emphasis, backticks, trailing punctuation and whitespace runs
    are all incidental to a rule's identity: a rule reflowed or re-punctuated
    between two copies is the same rule. Everything else is significant —
    `Dispatch, don't work inline` and `Dispatch, don't drift inline` are two
    rules and must never collapse to one key.
    """
    text = _NORMALIZE_STRIP.sub("", lead)
    text = _WHITESPACE.sub(" ", text).strip()
    text = _PUNCT_TAIL.sub("", text)
    return text.casefold()


@dataclass
class Rule:
    """One rule identity, with where it was found so a failure is actionable."""

    key: str
    lead: str
    line: int
    kind: str  # "bullet" | "heading"


@dataclass
class Source:
    """One side of the comparison."""

    label: str
    rules: dict[str, Rule] = field(default_factory=dict)
    duplicates: list[Rule] = field(default_factory=list)


def extract(text: str, label: str) -> Source:
    """Pull every rule identity out of one CLAUDE.md revision."""
    src = Source(label=label)
    in_fence = False
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if _FENCE.match(raw):
            in_fence = not in_fence
            continue
        if in_fence:
            continue

        lead: str | None = None
        kind = ""
        m = _BULLET_LEAD.match(raw)
        if m:
            lead = m.group("a") or m.group("b")
            kind = "bullet"
        else:
            h = _HEADING.match(raw)
            if h:
                lead = h.group("text")
                kind = "heading"
        if lead is None:
            continue

        key = normalize(lead)
        if not key:
            continue
        rule = Rule(key=key, lead=lead.strip(), line=lineno, kind=kind)
        if key in src.rules:
            src.duplicates.append(rule)
        else:
            src.rules[key] = rule
    return src


def read_source(spec: str) -> str:
    """Read a revision, from disk or from a git ref.

    `<ref>:<path>` goes through `git show`. Anything else is a filesystem
    path. Either way a read failure exits 2 — the check did not run.
    """
    if ":" in spec and not spec.startswith(("/", ".", "~")):
        ref, _, path = spec.partition(":")
        try:
            proc = subprocess.run(
                ["git", "show", f"{ref}:{path}"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:  # git absent
            die(f"cannot run git to resolve {spec!r}: {exc}. "
                "The check did NOT run.")
        if proc.returncode != 0:
            # Print the ref name only, never a remote URL — a remote can carry
            # an embedded credential.
            die(
                f"cannot resolve git ref {spec!r} "
                f"(git exited {proc.returncode}). The check did NOT run."
            )
        return proc.stdout
    try:
        with open(spec, encoding="utf-8") as fh:
            return fh.read()
    except OSError as exc:
        die(f"cannot read {spec!r}: {exc}. The check did NOT run.")


def near_duplicates(src: Source) -> list[tuple[Rule, Rule]]:
    """Rule pairs similar enough to be worth a human's eye during a dedup.

    Advisory only. These are *reported*, never collapsed: treating two
    near-identical leads as one satisfied requirement would silently
    reintroduce the exact bug this script exists to prevent.
    """
    pairs: list[tuple[Rule, Rule]] = []
    items = list(src.rules.values())
    for i, a in enumerate(items):
        for b in items[i + 1 :]:
            if a.kind != b.kind:
                continue
            ratio = difflib.SequenceMatcher(None, a.key, b.key).ratio()
            if ratio >= NEAR_DUPLICATE_RATIO:
                pairs.append((a, b))
    return pairs


@dataclass
class Dedup:
    """A human-confirmed same-rule replacement: `dropped` yielded to `kept`."""

    dropped_key: str
    dropped_lead: str
    kept_key: str
    kept_lead: str
    line: int


def read_dedups(spec: str) -> list[Dedup]:
    """Parse the deliberate-dedup record. A malformed line exits 2."""
    text = read_source(spec)
    out: list[Dedup] = []
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=>" not in line:
            die(
                f"{spec} line {lineno}: expected "
                f"'<dropped lead> => <surviving lead>', got {line!r}. "
                "The check did NOT run."
            )
        dropped, _, kept = line.partition("=>")
        dropped, kept = dropped.strip(), kept.strip()
        if not dropped or not kept:
            die(
                f"{spec} line {lineno}: both sides of '=>' are "
                "required. The check did NOT run."
            )
        out.append(
            Dedup(
                dropped_key=normalize(dropped),
                dropped_lead=dropped,
                kept_key=normalize(kept),
                kept_lead=kept,
                line=lineno,
            )
        )
    return out


def check_dedups(
    dedups: list[Dedup], merged: Source, label: str
) -> list[str]:
    """Validate every recorded dedup against the merged file.

    A dedup excuses a missing rule name only while it stays true: the dropped
    lead must really be gone, and the surviving lead must really be there. An
    entry that has gone stale fails the gate rather than quietly widening it.
    """
    problems: list[str] = []
    for d in dedups:
        if d.dropped_key in merged.rules:
            problems.append(
                f"  {label} line {d.line}: **{d.dropped_lead}** is recorded "
                f"as deduped away but is still present in {merged.label} at "
                f"line {merged.rules[d.dropped_key].line}. Remove the stale "
                "entry."
            )
        if d.kept_key not in merged.rules:
            problems.append(
                f"  {label} line {d.line}: the surviving rule "
                f"**{d.kept_lead}** is ABSENT from {merged.label}. The dedup "
                "claimed this rule carries the dropped one's clauses, so its "
                "loss loses both."
            )
    return problems


def compare(
    sides: list[Source], merged: Source, dedups: list[Dedup] | None = None
) -> tuple[list[tuple[Source, Rule]], list[tuple[Source, Rule, Dedup]]]:
    """Split rules missing from the merged file into lost and deduped.

    Returns `(lost, deduped)`. Anything in `lost` fails the gate; `deduped` is
    reported for the record, because a merge that drops a rule name is worth
    seeing even when a human already signed off on it.
    """
    by_dropped = {d.dropped_key: d for d in (dedups or [])}
    lost: list[tuple[Source, Rule]] = []
    deduped: list[tuple[Source, Rule, Dedup]] = []
    for side in sides:
        for key, rule in side.rules.items():
            if key in merged.rules:
                continue
            if key in by_dropped:
                deduped.append((side, rule, by_dropped[key]))
            else:
                lost.append((side, rule))
    return lost, deduped


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="verify_claude_md_merge.py",
        description=(
            "Compare CLAUDE.md rule-by-rule between revisions and fail if any "
            "rule is present on one side and absent from the merged file."
        ),
    )
    p.add_argument(
        "--base",
        required=True,
        help="First input revision: a path, or <git-ref>:<path>.",
    )
    p.add_argument(
        "--other",
        action="append",
        default=[],
        help=(
            "Additional input revision to union in. Repeatable. Omit for a "
            "two-way parity check of --merged against --base."
        ),
    )
    p.add_argument(
        "--merged",
        required=True,
        help="The merged/candidate revision that must contain every rule.",
    )
    p.add_argument(
        "--dedups",
        help=(
            "Record of human-confirmed same-rule replacements, one "
            "'<dropped lead> => <surviving lead>' per line. Both sides are "
            "verified against --merged, so a stale entry fails the gate."
        ),
    )
    p.add_argument(
        "--check",
        action="store_true",
        help=(
            "CI mode: print only the verdict and any lost rules, suppressing "
            "the advisory inventory and near-duplicate report."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    sides = [
        extract(read_source(spec), spec)
        for spec in [args.base, *args.other]
    ]
    merged = extract(read_source(args.merged), args.merged)
    dedups = read_dedups(args.dedups) if args.dedups else []

    lost, deduped = compare(sides, merged, dedups)
    dedup_problems = check_dedups(dedups, merged, args.dedups or "")

    if not args.check:
        print("Rule inventory")
        for side in sides:
            bullets = sum(1 for r in side.rules.values() if r.kind == "bullet")
            heads = sum(1 for r in side.rules.values() if r.kind == "heading")
            print(
                f"  {side.label}: {len(side.rules)} rules "
                f"({bullets} bullets, {heads} headings)"
            )
        mb = sum(1 for r in merged.rules.values() if r.kind == "bullet")
        mh = sum(1 for r in merged.rules.values() if r.kind == "heading")
        print(
            f"  {merged.label}: {len(merged.rules)} rules "
            f"({mb} bullets, {mh} headings)"
        )

        pairs = near_duplicates(merged)
        if pairs:
            print(
                f"\nNear-duplicate leads in {merged.label} "
                f"({len(pairs)}) — ADVISORY, not collapsed. Each is counted "
                "as its own rule; review only whether a dedup is wanted:"
            )
            for a, b in pairs:
                print(f"  line {a.line}: **{a.lead}**")
                print(f"  line {b.line}: **{b.lead}**")
        for side in [*sides, merged]:
            for dup in side.duplicates:
                print(
                    f"\nnote: {side.label} line {dup.line} repeats an earlier "
                    f"lead **{dup.lead}** — counted once."
                )
        print()

    # Unsound dedup records are reported FIRST. A dedup whose surviving rule
    # has itself vanished must not be able to suppress its own failure report.
    if dedup_problems:
        print(f"FAIL: {len(dedup_problems)} stale or unsound dedup record(s)")
        for problem in dedup_problems:
            print(problem)
        print()

    if deduped:
        print(
            f"Deliberate dedups honoured ({len(deduped)}) — rule names dropped "
            "by a human-confirmed same-rule replacement:"
        )
        for side, rule, d in deduped:
            survivor = merged.rules.get(d.kept_key)
            where = (
                f"present at line {survivor.line}"
                if survivor
                else "but that rule is ABSENT — see the failure above"
            )
            print(
                f"  **{rule.lead}** ({side.label} line {rule.line})\n"
                f"      yielded to **{d.kept_lead}**, {where}"
            )
        print()

    if lost:
        print(f"FAIL: {len(lost)} rule(s) lost in {merged.label}")
        for side, rule in lost:
            print(
                f"  [{rule.kind}] **{rule.lead}**\n"
                f"      present in {side.label} at line {rule.line}, "
                f"absent from {merged.label}"
            )
        print(
            "\nEach line above is a rule name that exists on one side and not "
            "in the merged file. Add it back — do not reword the merged file "
            "to match, and do not treat a near-duplicate as covering it. If a "
            "drop really is a same-rule replacement, record it in --dedups "
            "after checking the surviving rule carries every clause."
        )

    if lost or dedup_problems:
        return 1

    suffix = f" ({len(deduped)} recorded dedup(s) honoured)" if deduped else ""
    print(
        "OK: no rule lost — every rule on every input side is present in "
        f"{merged.label}{suffix}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
