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

**Two documents, not one.** The committed ``decision_state.md`` is a pure
projection of the checked-out commit: it reads the register at ``origin/main``
only, and reports nothing that depends on any other branch. It is therefore
deterministic for a given commit, which is what makes it a ``--check`` gate a
PR can pass or fail on its own content. The cross-branch view — "a row ruled on
some branch but not yet merged" — is genuinely useful (decision 101 was ruled on
``claude/foundation-decision-101-credential-binding`` for weeks before ``main``
caught up) but it is **not** committed: it is rendered on demand by ``--branches``,
printed to stdout, never written to disk and never checked. Committing it was
the second defect this module had. The projection read ``origin/main`` plus
every ``origin/*`` branch, so the committed file changed whenever *any* branch
moved — an open PR's own copy went stale from someone else's unrelated push, the
required "foundation corpus checkers" CI job failed on PRs whose content had not
changed, and every regeneration moved the document's head and invalidated
whatever a review lens had already signed off on. A render target that a machine
cannot reproduce from the commit it is checked out at is not a projection of that
commit; it is a projection of whatever else happened to be on the remote at fetch
time. See ``collect_main`` (committed) and ``collect_branches`` (report-only)
below.

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

*ruled* and *merged*, in the committed document, come from the register table on
``origin/main`` alone. *ruled* and *merged* in the ``--branches`` report also
weigh every ``origin/*`` branch that carries a copy of ``conformance.md``: a row
whose status on ``origin/main`` is open, and which some branch has moved to
ruled, is **ruled but not merged**, and the branch is named. Only forward
movement counts: a branch forked before a ruling landed carries a stale copy
that reads *open* where ``origin/main`` reads *ruled*, and treating that as a
retraction would invent a reopening no one performed. A row **reopened** on
``origin/main`` is not ruled — the register's own status vocabulary carries
``reopened`` distinctly from ``ruled``, and collapsing the two would erase the
fact that a settled question was unsettled again.

**A branch's ruling counts unless main has since unsettled that row, and unless
the branch is ruling a different question under the same number.** Decision 95
is why the first clause exists: it was ruled and reopened the same day, and
branches forked in between still carry the *superseded* ruling. Reading those as
rulings main is missing would report a reopened question as answered — inverting
the very fact the reopening recorded. So a branch's ``ruled`` is refused for any
row main now reads ``reopened``, and the refusal is reported rather than
silently dropped.

The second clause exists because a row number is not a stable identity across
branches. ``ateles#1288`` and the then-open ``ateles#1088`` both used row 111 for
unrelated questions — ``#1288``'s question landed on ``main`` as row 111, and
``#1088`` was carrying a *different* ruling under the same number on its branch.
Matching by number alone reads that as "row 111 ruled on a branch, not yet
merged" — a real ruling, misreported as pending, when in fact nothing about
``main``'s row 111 was unmerged at all; the branch was answering a different
question that happened to reuse the number. So a branch's row is admitted as a
ruling of ``main``'s row only when the two rows are the **same subject**
(``same_subject``, below); a same-number row that is not is a **collision** —
reported by name, on both sides, and never read as a ruling of either.

**Same subject.** Two candidates both look, at first, like a stable name for a
row's question independent of its number: the question text itself, and
``argued_in``, the register's own pointer to where the ruling lives (e.g.
`` `data_model.md#whether-a-rules-end-is-a-date-a-condition-or-a-task` ``).
Proving this function against the corpus's *live* branches — not synthetic
cases alone — found both wrong on their own. Row 99's anchor moved from a
heading describing the open question to one stating the ruling the moment it
was ruled, with its question text untouched; rows 84, 102 and 107 carry
question text tightened mid-draft on an open PR branch, with ``argued_in``
never moving. A key built from either field alone misreads one of these
ordinary edits as a collision. So two rows are the same subject when
**either** field agrees — normalized question text equal, or normalized
``argued_in`` equal — and a collision is declared only when **both** disagree,
which is what actually happened between ``#1288`` and ``#1088``: neither their
question text nor their anchors had anything in common, because the two
authors never saw each other's row.

That staleness test (main-has-since-reopened) is on the row's content,
deliberately. It was first written as commit ancestry — admit a branch only if
it descends from main's last commit touching the register — and that was wrong
in a way that took four disproved theories to find. ``git rev-list -1
origin/main -- conformance.md`` returns a different commit depending on how a
checkout built its history, so CI marked 66 refs stale where a developer's clone
marked 2, and the same source rendered two different documents: decision 93
ruled on one machine, unruled on the other. A projection whose output depends on
the machine that rendered it cannot be a ``--check`` gate. Staleness was never a
history question — a branch is carrying history when *that row's* status has
moved on, which the two copies of the row state directly.

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


def _normalize(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip().casefold()
    return "" if text in {"", "—", "-"} else requalify(text)


def subject_key(row: dict[str, str]) -> tuple[str, str]:
    """A stable identity for what a row is *about*, independent of its number.

    Returns ``(question, argued_in)``, both normalized (whitespace collapsed,
    case-folded), never truncated -- comparisons need the full text, and
    truncating either side is how two different questions come to look alike.
    Compare with ``same_subject``, below, not with plain equality on this
    tuple: a row's own edits can move either field on its own, and neither one
    is reliably stable in isolation.

    Two supposedly-stable pointers, and neither is stable alone. The register
    already offers two candidates that both look like they name a question
    independent of its row number -- the question text itself, and
    ``argued_in``'s document anchor. Proving this function against the
    corpus's live branches (not synthetic cases alone) found both wrong on
    their own: row 99's anchor moved from
    `` `planning_model.md#what-this-does-not-reach-...` `` to
    `` `planning_model.md#every-planning-record-belongs-to-an-instance-...` ``
    the moment it was ruled -- the convention is to retitle the target heading
    to state the ruling -- while its question text stayed untouched; and rows
    84, 102 and 107 on open PR branches carry question text *tightened* mid-
    draft (typical prose editing while writing the ruling) under an
    `argued_in` anchor that never moved. A key built from either field alone
    misreads one of these two ordinary edits as a collision.

    ``same_subject`` therefore treats two rows as the same subject when
    **either** field agrees, and a row number is NOT part of this identity at
    all -- that is the whole point. Two branches can independently claim the
    same next-free number for unrelated questions (ateles#1288 and #1088 both
    used row 111, with both question text AND argued_in disagreeing between
    them), and a number-only match would read the second as "the first, not
    yet merged". Requiring both fields to disagree before calling it a
    collision is what keeps an ordinary edit from being misread as one, while
    still catching the case that actually happened.
    """
    return (_normalize(row.get("question", "")), _normalize(row.get("argued_in", "")))


def same_subject(a: tuple[str, str], b: tuple[str, str]) -> bool:
    """True when two ``subject_key`` results plausibly name the same question.

    Agreement on EITHER field is enough: a row's question text and its
    ``argued_in`` anchor each drift independently under ordinary editing (see
    ``subject_key``), so requiring both to match would call an ordinary edit a
    collision, and requiring only one chosen field to match picks the wrong
    one exactly when that field is the one that happened to drift. A true
    collision -- two branches independently reusing a number for unrelated
    questions -- disagrees on both, because nothing about the second
    question's authoring ever touched the first's text or its anchor.

    A blank field never counts as agreement with anything, including another
    blank: ``("", "")`` records that a row was too malformed to key on either
    field, not that it shares a subject with every other malformed row.
    """
    q_a, anchor_a = a
    q_b, anchor_b = b
    if q_a and q_b and q_a == q_b:
        return True
    if anchor_a and anchor_b and anchor_a == anchor_b:
        return True
    return False


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
    def ruled_on_main(self) -> str:
        """ruled?, from `main_status` alone -- what the committed document uses.

        Deliberately blind to `ruled_on_branches`: that field only ever gets
        populated by `collect_branches`, and the committed document must be a
        function of `origin/main` alone, so nothing that reads this property may
        see a branch's ruling leak through. `ruled` (below) is the
        branch-aware sibling, for the `--branches` report only.
        """
        if not self.is_question:
            return "n/a"
        return "yes" if self.main_status in RULED_STATUSES else "no"

    @property
    def merged(self) -> str:
        if not self.is_question:
            return "n/a"
        return "yes" if self.main_status in RULED_STATUSES else "no"

    @property
    def ruled(self) -> str:
        """ruled?, weighing `ruled_on_branches` too -- the --branches report only.

        Never used by `render()`/`as_json()` (the committed document): those call
        `ruled_on_main`, which cannot see this field. Used by `render_branches`/
        `as_json_branches` and by `ruling_lives`, both report-only.
        """
        if not self.is_question:
            return "n/a"
        if self.main_status in RULED_STATUSES:
            return "yes"
        return "yes" if self.ruled_on_branches else "no"

    @property
    def ruling_lives(self) -> str:
        if self.main_status in RULED_STATUSES:
            return "—"
        if self.ruled_on_branches:
            return ", ".join(f"`{b}`" for b in self.ruled_on_branches)
        return "—"


@dataclass
class Collision:
    """A branch row sharing a number with a main row, arguing a different question.

    Reported by name and never read as a ruling of either row -- admitting it as
    one is exactly the ateles#1288/#1088 defect this key exists to catch. Both
    subject keys are carried so the report can show what disagreed, not just
    that something did.
    """

    number: str
    branch: str
    main_subject_key: tuple[str, str]
    branch_subject_key: tuple[str, str]
    main_question: str
    branch_question: str


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


def collect_main() -> list[Row]:
    """Build every row's axes from ``origin/main`` alone.

    This is the ONLY input to the committed document and to ``--check``. It
    reads exactly one ref, so its output is a pure function of the commit
    ``origin/main`` points at -- nothing about any other branch, and nothing
    about which refs a particular clone happens to have fetched, can change it.
    That determinism is the fix for the defect that made this document
    committed-but-unreproducible: the previous version swept every
    `origin/*` branch into the committed file, so the file changed whenever
    *any* branch moved, `--check` failed on PRs whose own content was
    unchanged, and each regeneration invalidated whatever a review lens had
    already signed off on.

    Every row's ``ruled``/``merged`` therefore collapse to what ``main`` itself
    says: a row main has ruled is both ruled and merged, and a row main has not
    is neither. The "ruled on a branch but not yet merged" distinction needs a
    second ref to exist at all, so it is not knowable from this function --
    see ``collect_branches``.
    """
    main_rows = register_at(MAIN_REF)
    if main_rows is None:
        raise SystemExit(
            f"decision state: no register table at {MAIN_REF}:"
            f"{FOUNDATION_DIR}/{REGISTER_DOC} — fetch the remote first"
        )

    rows = [
        Row(
            number=num,
            question=data["question"],
            argued_in=data["argued_in"],
            blocks=data["blocks"],
            main_status=data["status"],
        )
        for num, data in main_rows.items()
    ]

    for row in rows:
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

    return sorted(rows, key=lambda r: _row_sort_key(r.number))


def collect_branches(
    refs_scanned: list[str],
) -> tuple[list[Row], list[str], list[str], list[Collision]]:
    """The cross-branch view: rulings ``main`` does not have yet.

    Report-only -- never committed, never part of ``--check``. Reads ``main``
    plus every ref in ``refs_scanned`` (ordinarily every ``origin/*`` branch),
    so unlike ``collect_main`` its output legitimately depends on which branches
    exist at the moment it runs; that is the whole point of the view, which is
    why it may never be the thing an open PR's own content is checked against.

    Returns (rows with ``ruled_on_branches``/``implemented`` populated, refs
    read and current, refs read but superseded, collisions found).
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
    main_keys = {num: subject_key(data) for num, data in main_rows.items()}

    read: list[str] = []
    stale: list[str] = []
    collisions: list[Collision] = []
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
            branch_key = subject_key(data)
            main_key = main_keys.get(num, ("", ""))
            if not same_subject(main_key, branch_key):
                # ateles#1288/#1088: two branches independently claimed the same
                # row number for unrelated questions. This is never a ruling of
                # main's row -- reporting it as one would repeat the exact
                # misread the collision produced live. Report it by name on
                # both sides instead. Agreement on EITHER field would have
                # avoided this branch (see same_subject); disagreement on BOTH
                # is what makes it a genuine collision rather than an ordinary
                # edit to one field.
                collisions.append(
                    Collision(
                        number=num,
                        branch=ref,
                        main_subject_key=main_key,
                        branch_subject_key=branch_key,
                        main_question=main_rows[num]["question"],
                        branch_question=data["question"],
                    )
                )
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

    ordered = sorted(rows.values(), key=lambda r: _row_sort_key(r.number))
    collisions.sort(key=lambda c: (_row_sort_key(c.number), c.branch))
    return ordered, read, stale, collisions


def _row_sort_key(num: str) -> tuple[int, str]:
    lead = re.match(r"^(\d+)", num)
    return (int(lead.group(1)) if lead else 0, num)


def render(rows: list[Row]) -> str:
    """The committed document. Never edited in place -- regenerate instead.

    A pure function of ``rows`` -- no branch name, no branch count, no
    cross-branch fact appears anywhere below. That is deliberate: this is the
    document ``--check`` holds a checkout to, and anything here that varied
    with another branch's state would make ``--check`` fail on a PR for a
    reason that has nothing to do with the PR's own content, which is the
    defect this render exists to no longer have. The cross-branch view lives in
    ``render_branches`` instead, and is never written to disk.
    """
    out: list[str] = []
    add = out.append

    add(
        "<!-- GENERATED by execution/scripts/render_decision_state.py — do not "
        "edit. -->"
    )
    add(
        "<!-- Source: conformance.md#the-register-of-open-design-decisions, read "
        f"on {MAIN_REF} only. Run with --branches for the cross-branch, "
        "report-only view (never committed). -->"
    )
    add("")
    add("# Decision state: ruled, merged, implemented")
    add("")
    add(
        "**Kind:** foundation companion; generated, never authored. **Generated by:** "
        "`execution/scripts/render_decision_state.py`, held equal to its source by "
        "`--check` in `scripts/lint.sh`. **Source:** the register table at "
        "`conformance.md#the-register-of-open-design-decisions`, read on "
        f"`{MAIN_REF}` only, so this document is a deterministic function of the "
        "commit it is generated from and does not change when any other branch "
        "moves."
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
        "- **ruled?** — the question has an answer. Read from the register's status "
        f"on `{MAIN_REF}`."
    )
    add(
        "- **merged?** — the ruling is on "
        f"`{MAIN_REF}`, so an agent reading the corpus sees it. In this document "
        "**ruled and merged always agree**, because both are read from the same "
        f"single ref (`{MAIN_REF}`); a ruling that exists only on another branch "
        "is invisible here by design — see **Rulings not yet on main**, below, "
        "for how to see it."
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
    add("## Rulings not yet on main")
    add("")
    add(
        "This document reports only what `origin/main` itself says, so a row ruled "
        "on an open branch or PR reads **open** here — correctly, for what this "
        "document is: a projection of the checked-out commit, not of every branch "
        "that exists at render time. Run `python3 "
        "execution/scripts/render_decision_state.py --branches` for the "
        "cross-branch view: which rows are ruled on a branch but not yet merged, "
        "and any collision where two branches claim the same row number for "
        "different questions. That view is generated on demand, printed to "
        "stdout, and never committed — its answer legitimately changes every time "
        "a branch moves, which is exactly why it cannot be this document."
    )
    add("")

    add("## The register on three axes")
    add("")
    add("| # | Subject | Ruled? | Merged? | Implemented? | Blocks |")
    add("|---|---|---|---|---|---|")
    for r in rows:
        impl = r.implemented if r.is_question else "n/a"
        if r.implemented_note:
            impl = f"{impl} ({r.implemented_note})"
        add(
            f"| {r.number} | {subject(r.question)} | {r.ruled_on_main} | "
            f"{r.merged} | {impl} | {subject(r.blocks, 14)} |"
        )
    add("")

    return "\n".join(out) + "\n"


def render_branches(
    rows: list[Row], refs_read: list[str], refs_stale: list[str],
    collisions: list[Collision],
) -> str:
    """The cross-branch report. Printed to stdout by --branches, never written.

    Carries exactly the view the committed document used to carry before it
    became machine- and branch-count-dependent: which rows are ruled on a
    branch ``main`` has not merged yet, named by branch, plus any collision
    where two branches independently claimed the same row number.
    """
    out: list[str] = []
    add = out.append

    unmerged = [r for r in rows if r.ruled == "yes" and r.merged == "no"]

    add("# Decision state — cross-branch view (report only, not committed)")
    add("")
    add(
        f"Source: `{MAIN_REF}` plus every `refs/remotes/origin/*` branch reachable "
        "at render time. This view is not written to disk and is not part of "
        "`--check`: its answer changes whenever any branch moves, which is "
        "exactly why the committed `decision_state.md` no longer carries it."
    )
    add("")
    add(f"- **{len(unmerged)}** row(s) ruled on a branch but not merged to main.")
    add(f"- **{len(collisions)}** row-number collision(s) found.")
    add(f"- **{len(refs_read)}** branch(es) read; **{len(refs_stale)}** superseded.")
    add("")

    if collisions:
        add("## Collisions — same row number, different question")
        add("")
        add(
            "Each of these is a branch claiming a row number "
            f"`{MAIN_REF}` already uses for a *different* question. Neither side "
            "is read as a ruling of the other; this is the ateles#1288/#1088 "
            "shape, and it must be resolved by renumbering one of the two rows, "
            "not by this generator."
        )
        add("")
        add("| # | Branch | Main's subject | Branch's subject |")
        add("|---|---|---|---|")
        for c in collisions:
            add(
                f"| {c.number} | `{c.branch}` | {subject(c.main_question)} | "
                f"{subject(c.branch_question)} |"
            )
        add("")

    if unmerged:
        add("## Ruled but not merged")
        add("")
        add(
            "Each of these reads **open** in the committed document and to anything "
            "reading the corpus at `origin/main`. The ruling is on the branch named."
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

    if refs_stale:
        add("## Superseded branch copies (not counted above)")
        add("")
        add(
            "Main has moved these rows to `reopened` since the branch's copy was "
            "written. Admitting the branch's ruling would report a reopened "
            "question as answered, so it is refused and listed here instead."
        )
        add("")
        for s in refs_stale:
            add(f"- {s}")
        add("")

    add(
        "**No count of branches is written into the committed document.** A "
        "number there would change every time anyone updated a branch, failing "
        "`--check` for a reason unrelated to any decision — and a check that goes "
        "red for reasons the reader learns to dismiss has stopped being a control "
        "(`principles.md`). This report is where that figure belongs: read fresh, "
        "on demand, never stored."
    )
    add("")
    return "\n".join(out) + "\n"


def as_json(rows: list[Row]) -> str:
    return (
        json.dumps(
            {
                "main_ref": MAIN_REF,
                "rows": [
                    {
                        "number": r.number,
                        "subject": subject(r.question),
                        "question": r.question,
                        "argued_in": r.argued_in,
                        "blocks": r.blocks,
                        "main_status": r.main_status,
                        "ruled": r.ruled_on_main,
                        "merged": r.merged,
                        "implemented": r.implemented,
                        "implemented_note": r.implemented_note,
                    }
                    for r in rows
                ],
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def as_json_branches(
    rows: list[Row], refs_read: list[str], refs_stale: list[str],
    collisions: list[Collision],
) -> str:
    return (
        json.dumps(
            {
                "main_ref": MAIN_REF,
                "refs_read": refs_read,
                "refs_stale": refs_stale,
                "collisions": [
                    {
                        "number": c.number,
                        "branch": c.branch,
                        "main_subject_key": {
                            "question": c.main_subject_key[0],
                            "argued_in": c.main_subject_key[1],
                        },
                        "branch_subject_key": {
                            "question": c.branch_subject_key[0],
                            "argued_in": c.branch_subject_key[1],
                        },
                        "main_question": c.main_question,
                        "branch_question": c.branch_question,
                    }
                    for c in collisions
                ],
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
        help="do not write; exit 1 if the file on disk differs from a fresh render "
        f"of {MAIN_REF} alone",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit JSON to stdout instead of writing"
    )
    parser.add_argument(
        "--branches",
        action="store_true",
        help="print the cross-branch report (rulings not yet on main, and any "
        "row-number collisions) to stdout instead of rendering the committed "
        "document. Never written to disk; not part of --check.",
    )
    args = parser.parse_args(argv)

    if args.branches:
        refs = remote_branches()
        rows, refs_read, refs_stale, collisions = collect_branches(refs)
        if args.json:
            sys.stdout.write(as_json_branches(rows, refs_read, refs_stale, collisions))
        else:
            sys.stdout.write(render_branches(rows, refs_read, refs_stale, collisions))
        return 1 if collisions else 0

    rows = collect_main()

    if args.json:
        sys.stdout.write(as_json(rows))
        return 0

    rendered = render(rows)

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
