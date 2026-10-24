#!/usr/bin/env python3
"""Bind decision 84's persistent assembly exclusion across the corpus.

The assembly finding, not a currently-held lease, is what must keep incomplete
work out of the ordinary claim pool. Creation also confers no step ownership:
only the intake declaration's resolved PM owner may claim ``classify``.

Stdlib only; registered in ``conformance.md#mechanical-checks-on-this-directory``.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

FOUNDATION_DIR = Path("docs/foundation")

INTAKE_SECTION_SHA256 = (
    "60ba1875856d26721fa2b8d8938c3c9490ac4c653f813cc7c702966e5a2a86f7"
)
BATCH_FORMATION_SECTION_SHA256 = (
    "3c9318488e76d1245b2867e0d868dcff6b558b03c81fa425b1147b71bb8d1b38"
)
DIRECT_MODEL_SECTION_SHA256 = (
    "baf247ddc33f69d6db844ec80805f704b6caf8f2c22b917543d6cf3be0c7f37e"
)
CLAIM_MODEL_SECTION_SHA256 = (
    "6274f48f8f594a8a0e790fe299e2ede86595301a207e92693f9c8aa42a54bdbf"
)
VOCABULARY_CLAIMABLE_SECTION_SHA256 = (
    "5d521e8e5d208e61d118fae0dcb94db6ca1cb7c47e685d0710fbfe8da8122b28"
)
LIVE_MODEL_SECTION_SHA256 = (
    "d1fa230e8d7865a6b76e8387949433f7f0db5100fb24f4170efc749931f64236"
)
ACTIVE_MODEL_SECTION_SHA256 = (
    "20ef9986ce037e19d9241c9944215d887829c427e7a11dd537b731cd868f762d"
)
FRONTIER_MODEL_SECTION_SHA256 = (
    "862789aed2e09b568711c0e6a008cc2daeb2935911b923c599f7fec7cffa6399"
)
TASK_SCHEMA_ROW_SHA256 = (
    "9179a0086e9897d916a1e909720dfe59929787c0e296ed2440faa9fa19ac853a"
)
PARENT_VOCABULARY_SECTION_SHA256 = (
    "7a5f519f82dcd3a340ea12d4f33ea74463be173acd29d23d91b676dc4e64a14d"
)
PART_OF_ROW_SHA256 = "396c415f9ec68360b0eebd027250275128be06367e37c2dc4d413e91bf7ec5cb"
WM13_ROW_SHA256 = "50d5a2d64655f9f8e96c53dc0090bc81799661f38a8414ec5c953bc286d40e06"
WM35_ROW_SHA256 = "28156649ffaca0801e1e8df29f8a6c696f614a1dc3e91562e452f797d4024645"
SCENARIO_J_SECTION_SHA256 = (
    "7ff3b9b0cd3a0888d2ed6ec2d546648391fa690350e5c6d5313213e627e2098b"
)
HOLD_MODEL_SECTION_SHA256 = (
    "f14d9ba6700ea67fedd13ca47cfd680d9da667f852be0adfc48959b33997c1e7"
)
FINDING_SCHEMA_ROW_SHA256 = (
    "24056643959d3cb6f24b35943bb4caa242cca205e32c210c75181a7fd3831500"
)
WM14A_ROW_SHA256 = "1b3c03b571c2a44ccab30b99bb06746774b9eada13f86551d30066878914c76e"

WM13_REQUIREMENT = "`work_model.md#intake-is-every-tasks-first-workflow`: creator-time immutable `admission_class` positively selects `workflow_entering` or `aggregate_parent`; every workflow-entering task atomically gets one intake batch and `ADDRESSED_BY` at creation; an aggregate parent gets neither and is not claimable, even before children exist or after the last child is removed"
INTAKE_ENTRY_PARAGRAPH = """**Entry condition:** every workflow-entering task enters with its intake batch and `ADDRESSED_BY` edge admitted atomically at
creation. Its immutable creator-time `admission_class: workflow_entering` is part of that unit. The only
other admitted value is `aggregate_parent`, which has no batch or `ADDRESSED_BY`; missing or ambiguous
classification refuses admission rather than defaulting to intake. Creation is publication (`work_model.md#the-transition-vocabulary`), so this boundary is crossed
once and no workflow-entering task enters intake twice. The aggregate parent is the explicit exception: it
is not claimable, never enters a workflow, and has no intake batch. Decision 84's assembly exception differs
only by adding the persistent
`classify` hold finding to that same creation unit. A task is unrouted while its intake batch has no closing
`route` verdict; there is no separate unrouted state (`work_model.md#intake-is-every-tasks-first-workflow`)."""
INTAKE_PURPOSE_PARAGRAPH = (
    "**Purpose:** turn a created task into a routed one: classified, linked to the "
    "records it concerns, deduplicated, prioritized, and handed to exactly one "
    "successor workflow, to none, or to the operator."
)
INTAKE_SEMANTIC_BLOCK = (
    INTAKE_PURPOSE_PARAGRAPH + "\n\n" + INTAKE_ENTRY_PARAGRAPH + "\n\n**Steps**"
)
BATCH_CREATION_PARAGRAPH = (
    "**A batch comes into existence at one of two moments, and at no other: every "
    "workflow-entering task's creation, which opens its intake batch at creation, "
    "and a closing verdict naming a successor, which opens the successor's.** Two "
    "causes, both recorded, and no third. Intake's `route` step closes on a verdict "
    "naming one successor workflow, none, or operator-only; every later batch closes "
    "the same way (`gates_and_workflows.md#sequencing-is-data-successors-and-the-chain`). "
    "Where a successor is named, the batch for it opens and carries a `FOLLOWS` edge "
    "back to the batch that named it. Where none is named, the task's chain ends. "
    "Nothing else opens a batch: no daemon opens one because it noticed eligible "
    "tasks, no adapter opens one on an inbound event, and no scheduler sweeps for "
    "work to group. The one batch with no predecessor is a task's intake batch, "
    "opened on the task's creation, which is the universal entry "
    "(`#intake-is-every-tasks-first-workflow`, above) and the reason every chain has "
    "a first link."
)
BATCH_OPENING_CLAUSE = (
    "An intake batch opens in the admitted creation unit of the workflow-entering "
    "task, without a predecessor verdict."
)
BATCH_OPENING_PARAGRAPH = (
    "The consequence worth naming has two forms, not one. "
    + BATCH_OPENING_CLAUSE
    + " Every successor batch is opened **by a principal's recorded conclusion**, "
    "never by a process acting on its own reading of the record. That closing "
    "verdict names the successor, so the decision has an author, a timestamp, and "
    "a reason, and a reader asking why these tasks are in that later workflow is "
    "answered by a conclusion rather than by inferring what some sweeper's "
    "predicate must have matched."
)
BATCH_SUCCESSOR_TASKS_PARAGRAPH = (
    "**A successor batch's tasks are the tasks the closing verdict carried; an intake "
    "batch carries the one task whose creation opened it, and grouping beyond either "
    "is a step's judgement, recorded as one.** The default is the simple one: the "
    "tasks attached to the closing batch move together into the successor, and a "
    "batch of one stays a batch of one. Two operations change a task set, both "
    "already defined and both edges (principle 11): **detach**, which ends a task's "
    "`ADDRESSED_BY` edge and opens a new batch for it from the first step of its "
    "workflow, and **attach**, which writes that edge. What this section adds is who "
    "may do them and on what basis. Attaching a task to a batch that is already open, "
    "part-way through its steps, is a step owner's judgement written into that "
    "step's verdict — `tasks_attached[]` names them (`data_model.md#concepts`), so an "
    "`ADDRESSED_BY` edge written after the batch opened that no verdict names is the "
    "failing artefact — never an adapter's guess and never a matcher's inference — "
    "the adapter rule already forbids the first "
    "(`adapters.md#what-the-adapter-does-with-every-event`), and the second is the "
    "routing fallthrough the pull rule forbids. A task attached part-way through "
    "enters at the batch's current step and inherits the verdicts already written on "
    "it, which is exactly why the judgement is a recorded one: those verdicts were "
    "made against a task set that did not include it, and a step owner who attaches "
    "is asserting that they still hold. Where that assertion is not safe, the task "
    "is its own batch."
)
BATCH_FORMATION_SEMANTIC_BLOCK = (
    BATCH_CREATION_PARAGRAPH
    + "\n\n"
    + BATCH_OPENING_PARAGRAPH
    + "\n\n"
    + BATCH_SUCCESSOR_TASKS_PARAGRAPH
)
AGGREGATE_PARENT_MODEL_PARAGRAPH = """Children `PART_OF` an aggregate parent (at most one parent). Children go through workflows independently.
An **aggregate parent task is not claimable,
never enters a workflow, and has no intake batch or `ADDRESSED_BY` edge** — it is a grouping, and a batch
carries tasks that are executed, which an aggregate parent never is. This is the deliberate exception to
the workflow-entry creation rule; every child and every other workflow-entering peer task still opens its
intake batch atomically at creation. The task's immutable creator-time `admission_class: aggregate_parent`
positively identifies the exception before any child exists and after the last child is removed; child
`PART_OF` edges express membership, not admission class. Creation refuses a missing or ambiguous class.
The target of a child task's `PART_OF` edge must already carry `admission_class: aggregate_parent`; an
ordinary workflow peer cannot become a parent merely by receiving a child edge. Each child has at most
one parent on its ascent, and moving or ending that edge never changes either task's admission class.
An aggregate parent stays outside claims, live execution and the dependency-ready frontier at every child
count. Parent completion requires at least one current child; an initially childless parent or one whose
last child was removed remains incomplete and visible for planning, never vacuously complete. Once it has
children, parent completion is derived: every child is terminal and landed, and
where the parent is under a planning record its effect evidence satisfies the applicable
`completion_criteria[]`. A terminal child count alone cannot close its parent."""
AGGREGATE_PARENT_ASCENT_PARAGRAPH = """**A task's one `PART_OF` edge targets its parent task or a planning record, and the records above it are
its ascent.** The same edge, with the same one-parent rule, relates a task to the plan it is under and a
plan to whatever the instance holds above it; the walk upward from a task along `PART_OF` is a derived
read, distinct from the chain, and what a step reads of it is declared and resolved at hydration
(`planning_model.md#the-hierarchy-is-edges-and-a-task-has-one-line-upward`,
`planning_model.md#upward-context-is-a-declared-read-resolved-along-the-ascent-at-hydration`). A task with
no planning record above it is unplanned, a derived read and never a status, and it is admitted through
intake like any task. Completion at every level above the task is the parent's rule applied again: derived
from descendant terminal-and-landed state plus the planning record's criterion evidence, never stored
(`planning_model.md#downward-state-is-derived-upward-content-is-authored-as-entities`,
`planning_model.md#commissioning-a-planning-record-drives-one-dependency-ready-frontier`)."""
AGGREGATE_PARENT_MODEL_SECTION = (
    AGGREGATE_PARENT_MODEL_PARAGRAPH + "\n\n" + AGGREGATE_PARENT_ASCENT_PARAGRAPH
)
AGGREGATE_PARENT_SCENARIO_PARAGRAPH = """A parent task is created with immutable `admission_class: aggregate_parent` before any child exists. It is
visible but incomplete, outside claim, live execution and the dependency-ready frontier, with no intake
batch. Three child tasks each carry a `PART_OF`
edge to it. Each child is claimed, executed, and goes through its own batch on its own schedule. The
**aggregate parent is the explicit workflow-entry exception: it is not claimable, never enters a workflow,
and has no intake batch or `ADDRESSED_BY` edge.** Removing the last child does not change its class or
make it claimable or complete; it remains visible and incomplete. When a reader asks whether the parent is complete,
the answer requires at least one current child, every child terminal and landed, plus effect evidence
for the applicable planning `completion_criteria[]` where the parent is under a planning record.
Neither terminal status alone nor a stored parent status proves completion."""
AGGREGATE_PARENT_SCENARIO_DIAGRAM = """```mermaid
flowchart TD
    P[aggregate parent: admission_class set at creation, never claimable]
    P --> Z{at least one current child?}
    Z -->|no, including last child removed| NC[parent remains incomplete]
    C1[child 1] -->|PART_OF| P
    C2[child 2] -->|PART_OF| P
    C3[child 3] -->|PART_OF| P
    C1 -->|ADDRESSED_BY| R1[batch 1]
    C2 -->|ADDRESSED_BY| R2[batch 2]
    C3 -->|ADDRESSED_BY| R3[batch 3]
    Z -->|yes| D{all children terminal and landed?}
    R1 --> D
    R2 --> D
    R3 --> D
    D -->|no| NC[parent remains incomplete]
    D -->|yes| E{applicable planning criteria have effect evidence?}
    E -->|no| NC
    E -->|yes, derived at read| PC[parent reads complete]
```"""
AGGREGATE_PARENT_SCENARIO_INVARIANTS = """**Invariants:** [`work_model.md#parent-and-child-tasks`](work_model.md#parent-and-child-tasks);
`principles.md` invariant 11."""
AGGREGATE_PARENT_SCENARIO_SECTION = (
    AGGREGATE_PARENT_SCENARIO_PARAGRAPH
    + "\n\n"
    + AGGREGATE_PARENT_SCENARIO_DIAGRAM
    + "\n\n"
    + AGGREGATE_PARENT_SCENARIO_INVARIANTS
)
WM35_REQUIREMENT = "`work_model.md#parent-and-child-tasks`: immutable `admission_class: aggregate_parent` makes a parent nonclaimable, outside live execution and the frontier, without intake batch or `ADDRESSED_BY` before any child exists and after its last child is removed; children are workflow-entering tasks; completion requires at least one child, all children terminal and landed, and applicable criterion evidence"


class CorpusProblem(Exception):
    """The decision-84 corpus files are missing or unreadable."""


def _line(text: str, pattern: str) -> str:
    match = re.search(pattern, _active_prose(text), re.M)
    return match.group(0) if match else ""


def _table_cell(row: str, index: int) -> str:
    cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
    return cells[index] if index < len(cells) else ""


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _fingerprint_normalize(text: str) -> str:
    stable = text.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip(" \t") for line in stable.split("\n")).strip("\n")


def _blank(text: str) -> str:
    return "".join("\n" if char == "\n" else " " for char in text)


def _fence_opening(line: str) -> str:
    match = re.match(r"^ {0,3}(`{3,}|~{3,})(.*)$", line.rstrip("\r\n"))
    if not match:
        return ""
    marker, info = match.groups()
    if marker[0] == "`" and "`" in info:
        return ""
    return marker


def _is_fence_closer(line: str, fence_char: str, fence_size: int) -> bool:
    match = re.fullmatch(r" {0,3}(`{3,}|~{3,})[ \t]*", line.rstrip("\r\n"))
    return bool(
        match and match.group(1)[0] == fence_char and len(match.group(1)) >= fence_size
    )


def _raw_html_block_opening(line: str) -> str:
    """Return a CommonMark type-1 raw HTML block tag, if this line opens one."""

    match = re.match(
        r"^ {0,3}<(?P<tag>pre|script|style|textarea)(?:[ \t]|>|$)",
        line.rstrip("\r\n"),
        re.I,
    )
    return match.group("tag").lower() if match else ""


def _raw_html_block_closes(line: str, tag: str) -> bool:
    return bool(re.search(rf"</{re.escape(tag)}>", line, re.I))


def _mask_html_comments(line: str, in_comment: bool) -> tuple[str, bool]:
    """Blank HTML comments on one line while preserving offsets and newlines."""

    output: list[str] = []
    cursor = 0
    while cursor < len(line):
        if in_comment:
            stop = line.find("-->", cursor)
            if stop < 0:
                output.append(_blank(line[cursor:]))
                return "".join(output), True
            stop += len("-->")
            output.append(_blank(line[cursor:stop]))
            cursor = stop
            in_comment = False
            continue
        start = line.find("<!--", cursor)
        if start < 0:
            output.append(line[cursor:])
            break
        output.append(line[cursor:start])
        stop = line.find("-->", start + len("<!--"))
        if stop < 0:
            output.append(_blank(line[start:]))
            return "".join(output), True
        stop += len("-->")
        output.append(_blank(line[start:stop]))
        cursor = stop
    return "".join(output), in_comment


def _without_html_comments(text: str) -> str:
    """Blank active comments/raw HTML while preserving fenced source and offsets."""

    output: list[str] = []
    in_comment = False
    raw_html_tag = ""
    fence_char = ""
    fence_size = 0
    for line in text.splitlines(keepends=True):
        if fence_char:
            output.append(line)
            if _is_fence_closer(line, fence_char, fence_size):
                fence_char = ""
                fence_size = 0
            continue
        if raw_html_tag:
            output.append(_blank(line))
            if _raw_html_block_closes(line, raw_html_tag):
                raw_html_tag = ""
            continue
        if not in_comment:
            marker = _fence_opening(line)
            if marker:
                fence_char = marker[0]
                fence_size = len(marker)
                output.append(line)
                continue
            raw_html_tag = _raw_html_block_opening(line)
            if raw_html_tag:
                output.append(_blank(line))
                if _raw_html_block_closes(line, raw_html_tag):
                    raw_html_tag = ""
                continue
        visible, in_comment = _mask_html_comments(line, in_comment)
        output.append(visible)
    return "".join(output)


def _active_prose(text: str) -> str:
    """Return same-length Markdown with comments and fenced blocks blanked."""

    visible = _without_html_comments(text)
    output: list[str] = []
    fence_char = ""
    fence_size = 0
    for line in visible.splitlines(keepends=True):
        if fence_char:
            output.append(_blank(line))
            if _is_fence_closer(line, fence_char, fence_size):
                fence_char = ""
                fence_size = 0
            continue
        marker = _fence_opening(line)
        if marker:
            fence_char = marker[0]
            fence_size = len(marker)
            output.append(_blank(line))
            continue
        output.append(line)
    return "".join(output)


class _HeadingTextParser(HTMLParser):
    """Collect visible-ish fragment text while ignoring HTML syntax."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _heading_projection(title: str) -> str:
    """Normalize the complete raw heading source without parsing Markdown.

    The projection is monotonic: adding a link target, title, attribute, or
    other syntax cannot hide a protected-title ordered subsequence.  That
    intentionally rejects some unrelated decorated headings whose non-visible
    syntax names a protected title; false-positive ambiguity is safer at this
    boundary than a partial Markdown parser.
    """

    decoded = html.unescape(title)
    return "".join(char.casefold() for char in decoded if char.isalnum())


def _visible_html_projection(title: str) -> str:
    """Retain the prior fail-closed coverage for text split by HTML tags."""

    parser = _HeadingTextParser()
    parser.feed(title)
    parser.close()
    decoded = html.unescape("".join(parser.parts))
    return "".join(char.casefold() for char in decoded if char.isalnum())


def _is_ordered_subsequence(needle: str, haystack: str) -> bool:
    if not needle:
        return False
    position = 0
    for char in haystack:
        if char == needle[position]:
            position += 1
            if position == len(needle):
                return True
    return False


def _heading_is_ambiguous(candidate: str, protected: str) -> bool:
    # This separate, ruled section on created workflows contains the letters of
    # "intake" only across unrelated words. Keep the conservative alias guard
    # for every other title, including decorated or interleaved intake aliases.
    if protected == "intake" and candidate == (
        "A workflow is created for the task no declaration fits, and improved by every task it carries"
    ):
        return False
    protected_projection = _heading_projection(protected)
    return _is_ordered_subsequence(
        protected_projection, _heading_projection(candidate)
    ) or _is_ordered_subsequence(
        protected_projection, _visible_html_projection(candidate)
    )


def _heading_spans(text: str, heading: str) -> list[tuple[int, int]]:
    expected = re.fullmatch(r"(#{1,6})[ \t]+(.+)", heading)
    if not expected:
        return []
    level = len(expected.group(1))
    title = expected.group(2)
    candidates = [entry for entry in _active_headings(text) if entry[0] == level]
    exact = [entry for entry in candidates if entry[1] == title]
    ambiguous = [
        entry for entry in candidates if _heading_is_ambiguous(entry[1], title)
    ]
    if len(exact) != 1 or len(ambiguous) != 1:
        return []
    return [(exact[0][2], exact[0][3])]


def _active_headings(text: str) -> list[tuple[int, str, int, int]]:
    """Return active ATX headings as level, title, start, and end offsets."""

    active = _active_prose(text)
    headings: list[tuple[int, str, int, int]] = []
    offset = 0
    for line in active.splitlines(keepends=True):
        match = re.fullmatch(
            r" {0,3}(#{1,6})[ \t]+(.+?)(?:[ \t]+#+)?[ \t]*",
            line.rstrip("\r\n"),
        )
        if match:
            headings.append(
                (
                    len(match.group(1)),
                    match.group(2),
                    offset,
                    offset + len(line),
                )
            )
        offset += len(line)
    return headings


def _owning_heading_section(text: str, heading: str, end_heading: str) -> str:
    """Return one heading's raw body through its unique expected successor."""

    headings = _active_headings(text)
    starts = [
        (index, entry) for index, entry in enumerate(headings) if entry[1] == heading
    ]
    ends = [
        (index, entry)
        for index, entry in enumerate(headings)
        if entry[1] == end_heading
    ]
    if len(starts) != 1 or len(ends) != 1:
        return ""
    start_index, start = starts[0]
    end_index, end = ends[0]
    if end_index <= start_index or end[0] != start[0]:
        return ""
    start_ambiguity = [
        entry
        for entry in headings
        if entry[0] == start[0] and _heading_is_ambiguous(entry[1], heading)
    ]
    end_ambiguity = [
        entry
        for entry in headings
        if entry[0] == end[0] and _heading_is_ambiguous(entry[1], end_heading)
    ]
    if len(start_ambiguity) != 1 or len(end_ambiguity) != 1:
        return ""
    following = [
        candidate
        for candidate in headings[start_index + 1 :]
        if candidate[0] <= start[0]
    ]
    if not following or following[0] != end:
        return ""
    return text[start[3] : end[2]]


def _heading_section(text: str, start: str, end: str) -> str:
    """Return one active Markdown heading section, refusing ambiguity."""

    starts = _heading_spans(text, start)
    ends = _heading_spans(text, end)
    if len(starts) != 1 or len(ends) != 1 or starts[0][0] >= ends[0][0]:
        return ""
    visible = _without_html_comments(text)
    return visible[starts[0][1] : ends[0][0]]


def _require(label: str, text: str, groups: tuple[tuple[str, ...], ...]) -> list[str]:
    normalized = _normalize(text)
    missing = [
        "/".join(group)
        for group in groups
        if not any(token in normalized for token in group)
    ]
    return [f"decision-84-{label} — missing " + ", ".join(missing)] if missing else []


def _forbid(label: str, text: str, tokens: tuple[str, ...]) -> list[str]:
    normalized = _normalize(text)
    present = [token for token in tokens if token in normalized]
    return [f"decision-84-{label} — forbidden " + ", ".join(present)] if present else []


def _require_exact_block(
    label: str,
    text: str,
    start: str,
    end: str,
    expected: str,
    *,
    require_section_start: bool = False,
) -> list[str]:
    visible = _without_html_comments(text)
    active = _active_prose(text)
    starts = [match.start() for match in re.finditer(re.escape(start), active)]
    ends = [match.start() for match in re.finditer(re.escape(end), active)]
    if len(starts) != 1 or len(ends) != 1 or starts[0] >= ends[0]:
        return [f"decision-84-{label} — canonical semantic block is ambiguous"]
    if require_section_start and visible[: starts[0]].strip():
        return [f"decision-84-{label} — content precedes canonical semantic block"]
    block = visible[starts[0] : ends[0]]
    if _normalize(block) == _normalize(expected):
        return []
    return [f"decision-84-{label} — canonical semantic block changed"]


def _require_exact_section(label: str, text: str, expected: str) -> list[str]:
    if _normalize(_without_html_comments(text)) == _normalize(expected):
        return []
    return [f"decision-84-{label} — canonical semantic section changed"]


def _require_exact_section_digest(
    label: str, section: str, expected_sha256: str
) -> list[str]:
    if not section:
        return [f"decision-84-{label} — owning Markdown section is ambiguous"]
    actual = hashlib.sha256(_fingerprint_normalize(section).encode()).hexdigest()
    if actual == expected_sha256:
        return []
    return [f"decision-84-{label} — canonical owning section changed"]


def _require_exact_cell(label: str, cell: str, expected: str) -> list[str]:
    if _normalize(cell) == _normalize(expected):
        return []
    return [f"decision-84-{label} — canonical requirement cell changed"]


def check(root: Path) -> list[str]:
    fdir = root / FOUNDATION_DIR
    names = (
        "conformance.md",
        "vocabulary.md",
        "work_model.md",
        "data_model.md",
        "workflows.md",
        "scenarios.md",
        "conformance_suite.md",
        "planning_model.md",
    )
    texts: dict[str, str] = {}
    for name in names:
        path = fdir / name
        if not path.is_file():
            raise CorpusProblem(f"missing {path}")
        texts[name] = path.read_text(encoding="utf-8")

    register = _line(texts["conformance.md"], r"^\|\s*84\s*\|.*$")
    wm14a = _line(texts["conformance_suite.md"], r"^\|\s*WM-14a\s*\|.*$")
    wm31 = _line(texts["conformance_suite.md"], r"^\|\s*WM-31\s*\|.*$")
    wm31a = _line(texts["conformance_suite.md"], r"^\|\s*WM-31a\s*\|.*$")
    wm27 = _line(texts["conformance_suite.md"], r"^\|\s*WM-27\s*\|.*$")
    wm35 = _line(texts["conformance_suite.md"], r"^\|\s*WM-35\s*\|.*$")
    part_of = _line(texts["data_model.md"], r"^\|\s*`PART_OF`\s*\|.*$")
    wm39 = _line(texts["conformance_suite.md"], r"^\|\s*WM-39\s*\|.*$")
    creation_row_names = ("WM-13", "WM-14", "WM-21", "WM-32b", "WM-35a", "WM-39")
    creation_row_map = {
        row: _line(texts["conformance_suite.md"], rf"^\|\s*{row}\s*\|.*$")
        for row in creation_row_names
    }
    wm13_requirement = _table_cell(creation_row_map["WM-13"], 1)
    creation_rows = " ".join(creation_row_map.values())
    task_schema = _line(texts["data_model.md"], r"^\|\s*task\s*\|.*$")
    finding_schema = _line(texts["data_model.md"], r"^\|\s*finding\s*\|.*$")
    intake_model = _heading_section(
        texts["work_model.md"],
        "### Intake is every task's first workflow",
        "### What distinguishes a task being assembled from one intake has not reached",
    )
    direct_model = _heading_section(
        texts["work_model.md"],
        "### What distinguishes a task being assembled from one intake has not reached",
        "### What a claim predicate treats as claimable",
    )
    direct_model_owner = _owning_heading_section(
        texts["work_model.md"],
        "What distinguishes a task being assembled from one intake has not reached",
        "What a claim predicate treats as claimable",
    )
    claim_model_owner = _owning_heading_section(
        texts["work_model.md"],
        "What a claim predicate treats as claimable",
        "A task is live when some principal could claim it now",
    )
    vocabulary_claimable_owner = _owning_heading_section(
        texts["vocabulary.md"],
        "claimable",
        "terminal",
    )
    live_model_owner = _owning_heading_section(
        texts["work_model.md"],
        "A task is live when some principal could claim it now",
        "Priority orders the claimable pool; it does not enter it",
    )
    batch_formation = _heading_section(
        texts["work_model.md"],
        "### How a batch is formed, and what chooses its workflow",
        "### A batch may hold on a condition discovered mid-flight",
    )
    source_index = _heading_section(
        texts["work_model.md"],
        "### Where tasks come from: every source, indexed",
        "### An intake rule turns a described change in the record into a task, and nothing else",
    )
    hold_model_owner = _owning_heading_section(
        texts["work_model.md"],
        "A batch may hold on a condition discovered mid-flight",
        "A batch may depend on a task it created",
    )
    parent_model = _heading_section(
        texts["work_model.md"],
        "### Parent and child tasks",
        "### A recurring task is one live instance, and its completion creates the next",
    )
    intake_workflow = _heading_section(
        texts["workflows.md"],
        "## intake",
        "## feature",
    )
    intake_workflow_owner = _owning_heading_section(
        texts["workflows.md"], "intake", "feature"
    )
    scenario_f = _heading_section(
        texts["scenarios.md"],
        "## (f) A parent task with children in independent batches",
        "## (g) An operator-only task, claimed by the operator-facing agent",
    )
    scenario_j = _heading_section(
        texts["scenarios.md"],
        "## (j) A task created, routed by intake, and entering its successor",
        "## What the scenarios do not show",
    )
    scenario_j_owner = _owning_heading_section(
        texts["scenarios.md"],
        "(j) A task created, routed by intake, and entering its successor",
        "What the scenarios do not show",
    )
    batch_formation_owner = _owning_heading_section(
        texts["work_model.md"],
        "How a batch is formed, and what chooses its workflow",
        "A batch may hold on a condition discovered mid-flight",
    )
    purpose = _heading_section(texts["work_model.md"], "## Purpose", "## Scope")
    recurrence_model = _heading_section(
        texts["work_model.md"],
        "### A recurring task is one live instance, and its completion creates the next",
        "### Where tasks come from: every source, indexed",
    )
    parent_vocabulary = _heading_section(
        texts["vocabulary.md"], "### parent task", "### child task"
    )
    active_model = _owning_heading_section(
        texts["work_model.md"],
        "Liveness is derived from activity at read time, never declared",
        "No assignment log; history is the task's own observations",
    )
    frontier_model = _heading_section(
        texts["planning_model.md"],
        "### Commissioning a planning record drives one dependency-ready frontier",
        "### The swarm may not author a missing ancestor",
    )
    admission_surfaces = {
        "task-schema": task_schema,
        "parent-model": parent_model,
        "claim-model": claim_model_owner,
        "live-model": live_model_owner,
        "parent-vocabulary": parent_vocabulary,
        "wm-13": creation_row_map["WM-13"],
        "wm-35": wm35,
        "parent-scenario": scenario_f,
        "intake-workflow": intake_workflow,
        "frontier": frontier_model,
    }

    problems: list[str] = []
    for surface, source in admission_surfaces.items():
        problems += _require(
            f"parent-admission-{surface}",
            source,
            (("admission_class",),),
        )
    problems += _require(
        "parent-admission-schema-values",
        task_schema,
        (("`workflow_entering`",), ("`aggregate_parent`",), ("immutable",)),
    )
    problems += _require(
        "parent-admission-childless",
        parent_model,
        (("before any child",), ("last child",), ("incomplete",)),
    )
    problems += _require(
        "parent-admission-fail-closed",
        claim_model_owner + " " + live_model_owner,
        (("missing",), ("ambiguous",), ("refused", "excluded")),
    )
    problems += _require(
        "parent-admission-frontier",
        frontier_model,
        (("admission_class: workflow_entering",), ("claimable",)),
    )
    for label, source, digest in (
        ("parent-admission-active", active_model, ACTIVE_MODEL_SECTION_SHA256),
        (
            "parent-admission-frontier-contract",
            frontier_model,
            FRONTIER_MODEL_SECTION_SHA256,
        ),
        ("parent-admission-task-schema", task_schema, TASK_SCHEMA_ROW_SHA256),
        (
            "parent-admission-parent-vocabulary",
            parent_vocabulary,
            PARENT_VOCABULARY_SECTION_SHA256,
        ),
        ("parent-admission-part-of", part_of, PART_OF_ROW_SHA256),
        ("parent-admission-wm-13-cases", creation_row_map["WM-13"], WM13_ROW_SHA256),
        ("parent-admission-wm-35-cases", wm35, WM35_ROW_SHA256),
    ):
        problems += _require_exact_section_digest(label, source, digest)
    problems += _require(
        "register",
        register,
        (
            ("**ruled**",),
            ("persistent assembly exclusion",),
            ("lapsed", "lease lapse"),
            ("creation grants no lease",),
            ("`pm` step owner",),
            ("workflow-entering",),
            ("aggregate parent", "aggregate-parent"),
        ),
    )
    problems += _require(
        "model",
        direct_model,
        (
            ("persistent assembly exclusion",),
            ("lease lapse", "lapsed lease"),
            ("creation grants no lease",),
            ("declared `pm` step owner", "declaration's `pm` owner role"),
            ("multi-agent assembly",),
            ("workflow-entering",),
        ),
    )
    problems += _require_exact_section_digest(
        "creator-authority-model",
        direct_model_owner,
        DIRECT_MODEL_SECTION_SHA256,
    )
    problems += _require_exact_section_digest(
        "claim-model",
        claim_model_owner,
        CLAIM_MODEL_SECTION_SHA256,
    )
    problems += _require_exact_section_digest(
        "claimable-vocabulary",
        vocabulary_claimable_owner,
        VOCABULARY_CLAIMABLE_SECTION_SHA256,
    )
    problems += _require_exact_section_digest(
        "live-model",
        live_model_owner,
        LIVE_MODEL_SECTION_SHA256,
    )
    problems += _require_exact_section_digest(
        "scenario-workflow-entry",
        scenario_j_owner,
        SCENARIO_J_SECTION_SHA256,
    )
    universal_surfaces = {
        "intake-model": intake_model,
        "batch-formation": batch_formation,
        "source-index": source_index,
        "data-model": task_schema,
        "workflow": intake_workflow,
        "scenario": scenario_j,
        "creation-rows": creation_rows,
    }
    for surface, text in universal_surfaces.items():
        problems += _require(
            f"universal-entry-{surface}",
            text,
            (
                ("workflow-entering",),
                ("intake batch",),
                ("at creation", "on creation", "creation boundary"),
            ),
        )
    for row, text in creation_row_map.items():
        problems += _require(
            f"universal-entry-{row.lower()}",
            text,
            (("workflow-entering",), ("intake batch",), ("at creation", "on creation")),
        )
    universal_entry = " ".join(universal_surfaces.values())
    problems += _require(
        "universal-entry-assembly-difference",
        universal_entry,
        (
            ("assembly exception", "assembling task"),
            ("persistent assembly exclusion", "persistent `classify` hold"),
        ),
    )
    problems += _forbid(
        "universal-entry",
        universal_entry,
        (
            "ordinary complete tasks end creation with no intake batch",
            "ordinary task with a batch at creation",
            "complete task is created for ordinary intake; that is its publication. it has no intake batch",
            "every non-assembly task meets this condition once, at creation",
            "intake batch exists?",
            "no: unrouted by that fact",
            "task enters intake; batch record opens",
        ),
    )
    problems += _require_exact_section_digest(
        "intake-workflow-atomic-entry",
        intake_workflow_owner,
        INTAKE_SECTION_SHA256,
    )
    problems += _require_exact_section(
        "aggregate-parent-model",
        parent_model,
        AGGREGATE_PARENT_MODEL_SECTION,
    )
    problems += _require_exact_section(
        "aggregate-parent-scenario",
        scenario_f,
        AGGREGATE_PARENT_SCENARIO_SECTION,
    )
    problems += _require(
        "part-of-parent-completion",
        _table_cell(part_of, 3),
        (
            ("every child terminal and landed",),
            ("effect evidence",),
            ("`completion_criteria[]`",),
        ),
    )
    problems += _forbid(
        "part-of-parent-completion",
        _table_cell(part_of, 3),
        ("parent completion (all children terminal)",),
    )
    problems += _require(
        "parent-vocabulary-completion",
        parent_vocabulary,
        (
            ("every child is terminal and landed", "every child terminal and landed"),
            ("`completion_criteria[]`",),
            ("effect evidence",),
        ),
    )
    problems += _forbid(
        "parent-vocabulary-completion",
        parent_vocabulary,
        ("children's terminal states",),
    )
    problems += _require(
        "recurrence-parent-completion",
        recurrence_model,
        (("every child terminal and landed",), ("planning criterion evidence",)),
    )
    problems += _forbid(
        "recurrence-parent-completion",
        recurrence_model,
        ("completion when every child is terminal",),
    )
    problems += _require_exact_cell(
        "aggregate-parent-wm-35",
        _table_cell(wm35, 1),
        WM35_REQUIREMENT,
    )
    problems += _require(
        "aggregate-parent-wm-35-cases",
        " ".join(_table_cell(wm35, index) for index in (2, 3, 4)),
        (
            ("terminal but unlanded",),
            ("unmet `completion_criteria[]`",),
            ("refuses completion",),
        ),
    )
    problems += _require(
        "purpose-batch-opening",
        purpose,
        (
            ("intake batch opens atomically",),
            ("successor batch is opened by a closing verdict",),
        ),
    )
    problems += _forbid(
        "purpose-batch-opening",
        purpose,
        ("a batch is opened by a closing verdict naming a successor",),
    )
    problems += _require_exact_section_digest(
        "batch-opening-model",
        batch_formation_owner,
        BATCH_FORMATION_SECTION_SHA256,
    )
    problems += _require(
        "wm-27",
        wm27,
        (
            ("intake batch",),
            ("at creation", "task creation"),
            ("without a predecessor verdict",),
            ("successor batch",),
            ("closing verdict",),
        ),
    )
    problems += _require(
        "scenario-intake-node",
        scenario_j,
        (
            ("i[intake batch:",),
            ("f -.->|follows| i",),
        ),
    )
    problems += _require_exact_section_digest(
        "hold-model",
        hold_model_owner,
        HOLD_MODEL_SECTION_SHA256,
    )
    problems += _require_exact_section_digest(
        "finding-schema",
        finding_schema,
        FINDING_SCHEMA_ROW_SHA256,
    )
    problems += _require(
        "workflow-exception",
        intake_workflow,
        (("decision 84's assembly",), ("persistent",), ("intake batch",)),
    )
    problems += _require(
        "scenario-exception",
        texts["scenarios.md"],
        (("assembling task is the ruled exception",), ("resolved `pm` step owner",)),
    )
    problems += _require_exact_cell(
        "wm-13-atomic-entry",
        wm13_requirement,
        WM13_REQUIREMENT,
    )
    problems += _require_exact_section_digest(
        "wm-14a",
        wm14a,
        WM14A_ROW_SHA256,
    )
    problems += _require(
        "wm-31",
        wm31 + " " + wm31a,
        (
            ("assembly exception",),
            ("creator-time",),
            ("survives lapse",),
            ("pm-only",),
        ),
    )
    problems += _require(
        "wm-39",
        wm39,
        (("assembly exception",), ("ordinary",), ("declared `pm` step owner",)),
    )
    return problems


# Keep source locations beside the diagnostics so every protected mismatch has
# an actionable origin, including the checks that combine multiple surfaces.
_UNIVERSAL_ENTRY_LOCATIONS = (
    "docs/foundation/work_model.md — sections Intake is every task's first "
    "workflow, How a batch is formed, and Where tasks come from; "
    "docs/foundation/workflows.md — section intake; "
    "docs/foundation/scenarios.md — section (j); "
    "docs/foundation/data_model.md — row task; "
    "docs/foundation/conformance_suite.md — rows WM-13, WM-14, WM-21, "
    "WM-32b, WM-35a, WM-39"
)
_PROBLEM_LOCATIONS = {
    "register": "docs/foundation/conformance.md — row 84",
    "model": "docs/foundation/work_model.md — section What distinguishes a task being assembled",
    "creator-authority-model": "docs/foundation/work_model.md — section What distinguishes a task being assembled",
    "claim-model": "docs/foundation/work_model.md — section What a claim predicate treats as claimable",
    "claimable-vocabulary": "docs/foundation/vocabulary.md — section claimable",
    "live-model": "docs/foundation/work_model.md — section A task is live",
    "scenario-workflow-entry": "docs/foundation/scenarios.md — section (j)",
    "intake-workflow-atomic-entry": "docs/foundation/workflows.md — section intake",
    "aggregate-parent-model": "docs/foundation/work_model.md — section Parent and child tasks",
    "aggregate-parent-scenario": "docs/foundation/scenarios.md — section (f)",
    "part-of-parent-completion": "docs/foundation/data_model.md — row PART_OF",
    "parent-vocabulary-completion": "docs/foundation/vocabulary.md — parent task",
    "recurrence-parent-completion": "docs/foundation/work_model.md — recurring task section",
    "aggregate-parent-wm-35": "docs/foundation/conformance_suite.md — row WM-35",
    "aggregate-parent-wm-35-cases": "docs/foundation/conformance_suite.md — row WM-35",
    "purpose-batch-opening": "docs/foundation/work_model.md — Purpose",
    "batch-opening-model": "docs/foundation/work_model.md — section How a batch is formed",
    "wm-27": "docs/foundation/conformance_suite.md — row WM-27",
    "scenario-intake-node": "docs/foundation/scenarios.md — section (j)",
    "hold-model": "docs/foundation/work_model.md — section A batch may hold",
    "finding-schema": "docs/foundation/data_model.md — row finding",
    "workflow-exception": "docs/foundation/workflows.md — section intake",
    "scenario-exception": "docs/foundation/scenarios.md — section (j)",
    "wm-13-atomic-entry": "docs/foundation/conformance_suite.md — row WM-13",
    "wm-14a": "docs/foundation/conformance_suite.md — row WM-14a",
    "wm-31": "docs/foundation/conformance_suite.md — rows WM-31 and WM-31a",
    "wm-39": "docs/foundation/conformance_suite.md — row WM-39",
    "universal-entry": _UNIVERSAL_ENTRY_LOCATIONS,
    "universal-entry-assembly-difference": _UNIVERSAL_ENTRY_LOCATIONS,
    "universal-entry-intake-model": "docs/foundation/work_model.md — section Intake is every task's first workflow",
    "universal-entry-batch-formation": "docs/foundation/work_model.md — section How a batch is formed",
    "universal-entry-source-index": "docs/foundation/work_model.md — section Where tasks come from",
    "universal-entry-data-model": "docs/foundation/data_model.md — row task",
    "universal-entry-workflow": "docs/foundation/workflows.md — section intake",
    "universal-entry-scenario": "docs/foundation/scenarios.md — section (j)",
    "universal-entry-creation-rows": "docs/foundation/conformance_suite.md — rows WM-13, WM-14, WM-21, WM-32b, WM-35a, WM-39",
}
for _row in ("WM-13", "WM-14", "WM-21", "WM-32b", "WM-35a", "WM-39"):
    _PROBLEM_LOCATIONS[f"universal-entry-{_row.lower()}"] = (
        f"docs/foundation/conformance_suite.md — row {_row}"
    )
_PROBLEM_LOCATIONS.update(
    {
        "parent-admission-task-schema": "docs/foundation/data_model.md — task row",
        "parent-admission-parent-model": "docs/foundation/work_model.md — Parent and child tasks",
        "parent-admission-claim-model": "docs/foundation/work_model.md — What a claim predicate treats as claimable",
        "parent-admission-live-model": "docs/foundation/work_model.md — A task is live",
        "parent-admission-parent-vocabulary": "docs/foundation/vocabulary.md — parent task",
        "parent-admission-wm-13": "docs/foundation/conformance_suite.md — WM-13",
        "parent-admission-wm-35": "docs/foundation/conformance_suite.md — WM-35",
        "parent-admission-parent-scenario": "docs/foundation/scenarios.md — scenario (f)",
        "parent-admission-intake-workflow": "docs/foundation/workflows.md — intake",
        "parent-admission-frontier": "docs/foundation/planning_model.md — Commissioning a planning record",
        "parent-admission-schema-values": "docs/foundation/data_model.md — task row",
        "parent-admission-childless": "docs/foundation/work_model.md — Parent and child tasks",
        "parent-admission-fail-closed": "docs/foundation/work_model.md — claim and live reads",
        "parent-admission-active": "docs/foundation/work_model.md — Liveness is derived",
        "parent-admission-frontier-contract": "docs/foundation/planning_model.md — Commissioning a planning record",
        "parent-admission-part-of": "docs/foundation/data_model.md — PART_OF row",
        "parent-admission-wm-13-cases": "docs/foundation/conformance_suite.md — WM-13",
        "parent-admission-wm-35-cases": "docs/foundation/conformance_suite.md — WM-35",
    }
)


def _actionable_problem(problem: str) -> str:
    label = problem.partition(" — ")[0].removeprefix("decision-84-")
    location = _PROBLEM_LOCATIONS.get(label)
    if location is None:
        raise CorpusProblem(
            f"unmapped diagnostic {label} in execution/scripts/check_foundation_decision_84.py"
        )
    return (
        f"{problem} [{location}]. Next: inspect decision 84 in "
        "docs/foundation/conformance.md row 84 and the named source; for an "
        "intentional edit, update the matching expectation or SHA-256 guard in "
        "execution/scripts/check_foundation_decision_84.py, then rerun "
        "python3 execution/scripts/check_foundation_decision_84.py."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    args = parser.parse_args(argv)
    try:
        problems = check(args.root)
        diagnostics = [_actionable_problem(problem) for problem in problems]
    except CorpusProblem as exc:
        print(f"decision 84 check: {exc}", file=sys.stderr)
        return 1
    for diagnostic in diagnostics:
        print(diagnostic)
    print(f"decision 84 check: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
