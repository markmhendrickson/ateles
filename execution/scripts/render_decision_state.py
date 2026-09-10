#!/usr/bin/env python3
"""Project the decision register onto three axes: ruled, merged, implemented.

``conformance.md#the-register-of-open-design-decisions`` marks each row **open**
or **ruled**. That single axis conflates three genuinely different states, and
the conflation produces wrong reads rather than merely imprecise ones:

**ruled**
    the question has an answer, wherever that answer currently lives.

**merged**
    the ruling is on ``origin/main``, so an agent reading the corpus sees it. A
    ruling on an unmerged branch is ruled and not merged: the register on
    ``origin/main`` still says **open**, and an executor reading the corpus
    finds an open question where an answer exists.

**implemented**
    code or record state actually satisfies the ruling. This axis is **not**
    inferred from the other two. A ruling's existence is not evidence that
    anything implements it, and this script reports ``unknown`` — never a
    guess — for every row it cannot check against a concrete artefact.

The three collapse into one status field today, so the register cannot express
"ruled on a branch, unmerged, unimplemented". Decision 101 is the standing
instance: ruled 2026-09-10 on ``claude/foundation-decision-101-credential-binding``
(PR #916), while ``origin/main``'s register row reads **open**, and the
migration's one-way registration waits on it.

**Why this is generated and not authored.** The predecessor plan carried its
blockers in a hand-maintained ``decision_blockers`` array; a union-reducer defect
stacked three generations of entries, several of which had become false —
recording decisions as "NOT YET MERGED" that had merged. A hand-maintained view
of a moving register is wrong by construction, and re-authoring it is not the
repair. This script is re-run instead: the output is a render target held to its
source by ``--check``, the same discipline
``conformance.md#direction-of-truth-per-class-of-record`` already applies to
``render_plan_docs.py`` and ``render_reading_projection.py``.

**How each axis is decided.**

*ruled* and *merged* come from the register table itself, read on ``origin/main``
and on every ``origin/*`` branch that carries a copy of ``conformance.md``. A row whose
status on ``origin/main`` is open, and which some branch has moved to ruled, is
**ruled but not merged**, and the branch is named. Only forward movement counts:
a branch forked before a ruling landed carries a stale copy that reads *ruled* on
``origin/main`` and *open* on the branch, and reading that as a retraction would
invent a reopening no one performed. A row **reopened** on ``origin/main`` is not
ruled — the register's own status vocabulary carries ``reopened`` distinctly from
``ruled``, and collapsing the two would erase the fact that a settled question
was unsettled again.

**A branch's ruling counts unless main has since unsettled that row.** Decision
95 is why: it was ruled and reopened the same day, and branches forked in between
still carry the *superseded* ruling. Reading those as rulings main is missing
would report a reopened question as answered — inverting the very fact the
reopening recorded. So a branch's ``ruled`` is refused for any row main now reads
``reopened``, and the refusal is reported rather than silently dropped.

That test is on the row's content, deliberately. It was first written as commit
ancestry — admit a branch only if it descends from main's last commit touching
the register — and that was wrong in a way that took four disproved theories to
find. ``git rev-list -1 origin/main -- conformance.md`` returns a different commit
depending on how a checkout built its history, so CI marked 66 refs stale where a
developer's clone marked 2, and the same source rendered two different documents:
decision 93 ruled on one machine, unruled on the other. A projection whose output
depends on the machine that rendered it cannot be a ``--check`` gate. Staleness
was never a history question — a branch is carrying history when *that row's*
status has moved on, which the two copies of the row state directly.

*implemented* is checked only where a row's own ruling names a concrete artefact
this repository can be asked about, through the checkers registered in
``conformance.md#mechanical-checks-on-this-directory``. Everywhere else it is
``unknown``. ``unknown`` is a first-class value here and not a failure to try:
``principles.md`` keeps unknown distinct from a conclusion, and an implemented
axis that guessed would be worth less than no axis at all.

Stdlib only, apart from ``git``. Registered in
``conformance.md#mechanical-checks-on-this-directory``.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

FOUNDATION_DIR = Path("docs/foundation")
REGISTER_DOC = "conformance.md"
DEFAULT_OUT = FOUNDATION_DIR / "decision_state.md"
MAIN_REF = "origin/main"

# A register row: "| 73 | question | pointer | blocks | **open** (date) |", or
# the combined "| 1–12 |" row. The status is the LAST cell.
ROW_RE = re.compile(r"^\|\s*(\d+(?:[–-]\d+)?)\s*\|(.*)\|\s*$")

# The status is the BOLD LEAD of the last cell, never a word appearing later in
# that cell's prose. Matching by containment reads "never reopened" and "before
# the step opens" as statuses and mislabels ruled rows; the table is the
# authority, so parsing it loosely would be the same defect one layer down.
STATUS_LEAD_RE = re.compile(r"^\s*\*\*([^*]+?)\*\*")

# Longest-first: "ruled in part" must beat "ruled". "reopened" is a status of
# its own -- a question ruled and then unsettled is NOT ruled.
STATUS_ORDER = (
    "ruled in part",
    "not a decision",
    "withdrawn",
    "reopened",
    "open",
    "ruled",
)

# Statuses that answer "ruled?" with yes.
RULED_STATUSES = frozenset({"ruled", "ruled in part"})

# Statuses that carry no question, so no axis applies.
NON_QUESTION_STATUSES = frozenset({"withdrawn", "not a decision"})

# A branch whose register copy is worth diffing. Only `refs/remotes/origin/*`,
# the namespace the standard clone refspec (`+refs/heads/*:refs/remotes/origin/*`)
# fills on every machine.
#
# Deliberately NOT everything under `refs/remotes/`. A clone configured with an
# extra refspec for pull-request heads — `+refs/pull/*/head:refs/remotes/origin-pr/*`
# — carries refs a default CI checkout has never heard of, so a sweep over the
# whole namespace renders a different document per machine and `--check` goes red
# for a reason that has nothing to do with any decision. That is the failure this
# document exists to prevent, and it reached the generator: the committed file was
# rendered on a clone holding nine `origin-pr/*` refs and CI, holding none, read a
# different register set.
#
# Nothing is lost by the narrowing. A pull request's ruling lives on the branch
# the pull request is opened from, which is an `origin/*` ref like any other; the
# `origin-pr/*` copy of it is the same commit reached by a second name.
BRANCH_LIST_ARGS = (
    "git",
    "for-each-ref",
    "--format=%(refname)",
    "refs/remotes/origin/",
)

# Rows whose ruling names an artefact this repository can be asked about, mapped
# to the check that asks. Each entry is (script, what a pass establishes).
#
# This table is deliberately tiny, and it is the honest size. A row earns an
# entry only when a *mechanical* check binds the ruling to something observable
# -- not when a reader could argue the ruling is satisfied. Inferring
# implementation from a ruling's existence is the error this whole document
# exists to stop, and doing it here would be the same error with a generator's
# authority behind it.
#
# Everything absent from this table reports `unknown`, which is the correct
# answer for a row whose ruling names no artefact a script can check.
IMPLEMENTED_CHECKS: dict[str, tuple[str, str]] = {
    "78": (
        "execution/scripts/check_foundation_decision_78.py",
        "the ruling is written at its anchor and in the register row",
    ),
}


def run(args: list[str]) -> tuple[int, str]:
    """Run a command, returning (returncode, stdout).

    Never raises: a bad ref or a checker that exits nonzero is data here, not an
    error. The caller decides what a nonzero code means.
    """
    proc = subprocess.run(
        args, capture_output=True, text=True, errors="replace", check=False
    )
    return proc.returncode, proc.stdout


def parse_rows(text: str) -> dict[str, dict[str, str]]:
    """Return {row number as written: {question, argued_in, blocks, status, cell}}."""
    rows: dict[str, dict[str, str]] = {}
    for line in text.split("\n"):
        m = ROW_RE.match(line)
        if not m:
            continue
        cells = [c.strip() for c in m.group(2).split("|")]
        if len(cells) < 4:
            continue
        lead = STATUS_LEAD_RE.match(cells[-1])
        if not lead:
            continue
        status_lead = lead.group(1).strip().lower()
        status = next(
            (k for k in STATUS_ORDER if status_lead.startswith(k)), status_lead
        )
        rows[m.group(1)] = {
            "question": cells[0],
            "argued_in": cells[1],
            "blocks": cells[2],
            "status": status,
            "status_cell": cells[-1],
        }
    return rows


def requalify(text: str) -> str:
    """Rewrite bare ``#anchor`` citations to name the register document.

    In the register's own cells a bare ``#anchor`` means "a heading in this
    document" -- and this document is ``conformance.md``. Copied verbatim into a
    generated file it silently changes meaning to "a heading *here*", where no
    such heading exists. ``check_foundation_anchors.py`` catches it, which is how
    it was found; the repair is to say what the source meant rather than to
    weaken the check.
    """
    return re.sub(r"`#([\w.-]+)`", rf"`{REGISTER_DOC}#\1`", text)


def subject(question: str, words: int = 12) -> str:
    """A few words of the question, for a table that must stay readable.

    The register's questions run to several hundred characters. This is a
    label, never a substitute for the row -- reading the subject is not reading
    the question, and the output says so.
    """
    text = requalify(re.sub(r"\s+", " ", question).strip())
    if text in {"", "—", "-"}:
        return "—"
    parts = text.split(" ")
    if len(parts) <= words:
        return text
    return " ".join(parts[:words]) + " …"


def register_at(ref: str) -> dict[str, dict[str, str]] | None:
    """The register table as it stands at `ref`, or None if it has no copy."""
    code, out = run(["git", "show", f"{ref}:{FOUNDATION_DIR}/{REGISTER_DOC}"])
    if code != 0 or not out.strip():
        return None
    rows = parse_rows(out)
    return rows or None


def remote_branches(exclude: str = MAIN_REF) -> list[str]:
    code, out = run(list(BRANCH_LIST_ARGS))
    if code != 0:
        return []
    names = []
    for line in out.split("\n"):
        ref = line.strip()
        if not ref or ref.endswith("/HEAD"):
            continue
        short = ref.removeprefix("refs/remotes/")
        if short == exclude.removeprefix("refs/remotes/"):
            continue
        names.append(short)
    return sorted(names)


@dataclass
class Row:
    number: str
    question: str
    argued_in: str
    blocks: str
    main_status: str
    ruled_on_branches: list[str] = field(default_factory=list)
    implemented: str = "unknown"
    implemented_note: str = ""

    @property
    def is_question(self) -> bool:
        return self.main_status not in NON_QUESTION_STATUSES

    @property
    def ruled(self) -> str:
        if not self.is_question:
            return "n/a"
        if self.main_status in RULED_STATUSES:
            return "yes"
        return "yes" if self.ruled_on_branches else "no"

    @property
    def merged(self) -> str:
        if not self.is_question:
            return "n/a"
        return "yes" if self.main_status in RULED_STATUSES else "no"

    @property
    def ruling_lives(self) -> str:
        if self.main_status in RULED_STATUSES:
            return "—"
        if self.ruled_on_branches:
            return ", ".join(f"`{b}`" for b in self.ruled_on_branches)
        return "—"


def is_superseded(
    main_row: dict[str, str] | None, branch_row: dict[str, str]
) -> bool:
    """True when main has moved this row's status past what the branch carries.

    The question a branch's ruling has to answer is whether main has *since*
    unsettled that row -- decision 95 was ruled and reopened the same day, and a
    branch forked in between carries a ruling the reopening superseded. Reading
    it as a ruling main is missing would invert the fact the reopening recorded.

    This is judged on the row's own content, not on commit ancestry. Ancestry was
    the first implementation and it was wrong in a way that took four disproved
    theories to find: `git rev-list -1 origin/main -- <register>` returns a
    different commit depending on how a checkout built its history, so CI and a
    developer's clone disagreed about which branches were stale (66 versus 2) and
    the same source rendered two different documents. A projection whose output
    depends on the machine cannot be a `--check` gate, and staleness was never
    really a history question: a branch is carrying history when *this row's*
    status has moved on, which the two copies of the row say directly.
    """
    if main_row is None:
        return False
    # `reopened` is the status the supersession produces, and the register's
    # vocabulary carries it distinctly from `open` for exactly this reason.
    return main_row["status"] == "reopened"


def collect(refs_scanned: list[str]) -> tuple[list[Row], list[str], list[str]]:
    """Build every row's three axes.

    Returns (rows, refs read and current, refs read but stale).
    """
    main_rows = register_at(MAIN_REF)
    if main_rows is None:
        raise SystemExit(
            f"decision state: no register table at {MAIN_REF}:"
            f"{FOUNDATION_DIR}/{REGISTER_DOC} — fetch the remote first"
        )

    rows = {
        num: Row(
            number=num,
            question=data["question"],
            argued_in=data["argued_in"],
            blocks=data["blocks"],
            main_status=data["status"],
        )
        for num, data in main_rows.items()
    }

    read: list[str] = []
    stale: list[str] = []
    for ref in refs_scanned:
        branch_rows = register_at(ref)
        if branch_rows is None:
            continue
        read.append(ref)
        for num, data in branch_rows.items():
            if data["status"] not in RULED_STATUSES:
                continue
            existing = rows.get(num)
            if existing is None:
                # A row this branch opens and main has never seen. It is not a
                # ruling main is missing; it is a question main does not have.
                continue
            if existing.main_status in RULED_STATUSES:
                continue  # already merged; a branch copy adds nothing
            if existing.main_status in NON_QUESTION_STATUSES:
                continue
            if is_superseded(main_rows.get(num), data):
                # Decision 95's case: main has moved this row's own status on
                # since the branch's copy was written, so the branch carries a
                # ruling main deliberately unsettled. Admitting it would report
                # a reopened question as answered. Judged per row, on the row's
                # own content, because that is what the claim is about.
                stale.append(f"{ref} (row {num})")
                continue
            existing.ruled_on_branches.append(ref)

    for row in rows.values():
        row.ruled_on_branches.sort()
        check = IMPLEMENTED_CHECKS.get(row.number)
        if check is None:
            continue
        script, establishes = check
        if not Path(script).is_file():
            row.implemented = "unknown"
            row.implemented_note = f"{script} absent"
            continue
        code, _ = run([sys.executable, script])
        row.implemented = "yes" if code == 0 else "no"
        row.implemented_note = establishes

    return [rows[n] for n in sorted(rows, key=_row_sort_key)], read, stale


def _row_sort_key(num: str) -> tuple[int, str]:
    lead = re.match(r"^(\d+)", num)
    return (int(lead.group(1)) if lead else 0, num)


def render(rows: list[Row], refs_read: list[str], refs_stale: list[str]) -> str:
    """The generated document. Never edited in place -- regenerate instead."""
    out: list[str] = []
    add = out.append

    add(
        "<!-- GENERATED by execution/scripts/render_decision_state.py — do not "
        "edit. -->"
    )
    add(
        "<!-- Source: conformance.md#the-register-of-open-design-decisions, read "
        f"on {MAIN_REF} and every `origin/*` branch. -->"
    )
    add("")
    add("# Decision state: ruled, merged, implemented")
    add("")
    add(
        "**Kind:** foundation companion; generated, never authored. **Generated by:** "
        "`execution/scripts/render_decision_state.py`, held equal to its source by "
        "`--check` in `scripts/lint.sh`. **Source:** the register table at "
        "`conformance.md#the-register-of-open-design-decisions`, read on "
        f"`{MAIN_REF}` and on every `origin/*` branch carrying a copy of it."
    )
    add("")
    add(
        "Not keyed, not in the kernel, and never inlined into a review prompt: this "
        "document states no rule about how the swarm works. It is a projection of "
        "one that does."
    )
    add("")
    add("## What the three axes are, and why one status field is not enough")
    add("")
    add(
        "The register marks a row **open** or **ruled**. That conflates three states "
        "which come apart in practice:"
    )
    add("")
    add(
        "- **ruled?** — the question has an answer, wherever that answer currently "
        "lives. Read from the register's status on any `origin/*` ref that\n"
        "carries one."
    )
    add(
        "- **merged?** — the ruling is on "
        f"`{MAIN_REF}`, so an agent reading the corpus sees it. A ruling on an "
        "unmerged branch leaves the corpus reading **open** where an answer exists, "
        "and an executor reading it finds a question that has been settled."
    )
    add(
        "- **implemented?** — code or record state satisfies the ruling. This is "
        "**not** inferred from the other two: a ruling's existence is no evidence "
        "that anything implements it. Where this generator cannot check a row "
        "against a concrete artefact it reports `unknown`, which is a value and not "
        "a failure to try (`principles.md` keeps unknown distinct from a "
        "conclusion)."
    )
    add("")
    add(
        "A row **reopened** on "
        f"`{MAIN_REF}` is reported as not ruled. The register's status vocabulary "
        "carries `reopened` distinctly from `ruled`, and collapsing the two would "
        "erase that a settled question was unsettled again."
    )
    add("")
    add(
        "The **Subject** column is a label of a few words, never a substitute for "
        "the row: the question, its argument, and what would decide it live at the "
        "pointer in **Argued in**, and reading the label is not reading the "
        "question."
    )
    add("")

    unmerged = [r for r in rows if r.ruled == "yes" and r.merged == "no"]
    open_rows = [r for r in rows if r.ruled == "no"]

    add("## What this run found")
    add("")
    add(
        f"- **{len(unmerged)}** row(s) **ruled but not merged** — the corpus on "
        f"`{MAIN_REF}` reads open where a ruling exists on a branch."
    )
    add(f"- **{len(open_rows)}** row(s) genuinely open — no ruling anywhere scanned.")
    add(
        f"- **{sum(1 for r in rows if r.merged == 'yes')}** row(s) ruled and merged."
    )
    add(
        f"- **{sum(1 for r in rows if r.is_question and r.implemented == 'unknown')}** "
        "row(s) whose implemented axis is `unknown`."
    )
    add("")

    if unmerged:
        add("### Ruled but not merged")
        add("")
        add(
            "Each of these reads **open** to anything reading the corpus. The ruling "
            "is on the branch named."
        )
        add("")
        add("| # | Subject | Ruling lives on | Blocks |")
        add("|---|---|---|---|")
        for r in unmerged:
            add(
                f"| {r.number} | {subject(r.question)} | {r.ruling_lives} | "
                f"{subject(r.blocks, 14)} |"
            )
        add("")

    add("## The register on three axes")
    add("")
    add("| # | Subject | Ruled? | Merged? | Implemented? | Ruling lives | Blocks |")
    add("|---|---|---|---|---|---|---|")
    for r in rows:
        impl = r.implemented if r.is_question else "n/a"
        if r.implemented_note:
            impl = f"{impl} ({r.implemented_note})"
        add(
            f"| {r.number} | {subject(r.question)} | {r.ruled} | {r.merged} | "
            f"{impl} | {r.ruling_lives} | {subject(r.blocks, 14)} |"
        )
    add("")

    add("## What was read")
    add("")
    add(
        f"The register on `{MAIN_REF}`, and the copy carried by every "
        "`refs/remotes/origin/*` branch. That namespace and not every remote "
        "ref: a clone configured with an extra refspec (pull-request heads, a "
        "second mirror) carries refs another machine has never fetched, and a "
        "sweep over those renders a different document per machine. Only "
        "forward movement is counted: a branch whose copy predates a ruling "
        f"reads **open** where `{MAIN_REF}` reads **ruled**, and treating that "
        "as a retraction would invent a reopening no one performed. A row a "
        f"branch opens and `{MAIN_REF}` has never seen is not a ruling "
        f"`{MAIN_REF}` is missing, and is not carried here."
    )
    add("")
    add(
        "A branch's ruling is **refused for any row** "
        f"`{MAIN_REF}` now reads **reopened**. This is not fastidiousness: "
        "decision 95 was ruled and reopened the same day, and branches forked "
        "in between still carry the superseded ruling. Admitting those would "
        "report a reopened question as answered — inverting the fact the "
        "reopening recorded. The test is on that row's own status, never on "
        "commit ancestry, because ancestry answers differently depending on how "
        "a checkout built its history and the projection would stop being "
        "reproducible off one machine."
    )
    add("")
    add(
        "**No count of branches is written here.** A number would change every "
        "time anyone updated a branch, failing `--check` for a reason unrelated to "
        "any decision — and a check that goes red for reasons the reader learns to "
        "dismiss has stopped being a control (`principles.md`). The branch sweep's "
        "own figures are in the `--json` output, where a consumer that wants them "
        "reads them fresh rather than from a stored copy that ages. This document "
        "changes when a decision's state changes, and not otherwise."
    )
    add("")
    return "\n".join(out) + "\n"


def as_json(rows: list[Row], refs_read: list[str], refs_stale: list[str]) -> str:
    return (
        json.dumps(
            {
                "main_ref": MAIN_REF,
                "refs_read": refs_read,
                "refs_stale": refs_stale,
                "rows": [
                    {
                        "number": r.number,
                        "subject": subject(r.question),
                        "question": r.question,
                        "argued_in": r.argued_in,
                        "blocks": r.blocks,
                        "main_status": r.main_status,
                        "ruled": r.ruled,
                        "merged": r.merged,
                        "implemented": r.implemented,
                        "implemented_note": r.implemented_note,
                        "ruled_on_branches": r.ruled_on_branches,
                    }
                    for r in rows
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT, help=f"output path (default: {DEFAULT_OUT})"
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="do not write; exit 1 if the file on disk differs from a fresh render",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit JSON to stdout instead of writing"
    )
    parser.add_argument(
        "--no-branches",
        action="store_true",
        help=f"read only {MAIN_REF}; every ruling on a branch is then invisible",
    )
    args = parser.parse_args(argv)

    refs = [] if args.no_branches else remote_branches()
    rows, refs_read, refs_stale = collect(refs)

    if args.json:
        sys.stdout.write(as_json(rows, refs_read, refs_stale))
        return 0

    rendered = render(rows, refs_read, refs_stale)

    if args.check:
        if not args.out.is_file():
            print(f"decision state: {args.out} does not exist — run without --check")
            return 1
        current = args.out.read_text(encoding="utf-8")
        if current != rendered:
            print(
                f"decision state: {args.out} differs from a fresh render. "
                "Do not edit it in place — regenerate: "
                "python execution/scripts/render_decision_state.py"
            )
            return 1
        print(f"decision state: {args.out} matches its source")
        return 0

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(rendered, encoding="utf-8")
    print(f"decision state: wrote {args.out} ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
