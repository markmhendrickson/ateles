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
import unicodedata
from pathlib import Path

FOUNDATION_DIR = Path("docs/foundation")

INTAKE_SECTION_SHA256 = (
    "d8d8e27147fbef3d4ddaef9451bd96e0c43dc512680b9ccd3f2eb31f342f67d9"
)
BATCH_FORMATION_SECTION_SHA256 = (
    "3c9318488e76d1245b2867e0d868dcff6b558b03c81fa425b1147b71bb8d1b38"
)
DIRECT_MODEL_SECTION_SHA256 = (
    "baf247ddc33f69d6db844ec80805f704b6caf8f2c22b917543d6cf3be0c7f37e"
)
CLAIM_MODEL_SECTION_SHA256 = (
    "3d2ff563b78ecab399c0e58448817807686fa95762f4c5790cd358a2bbc2b59c"
)
VOCABULARY_CLAIMABLE_SECTION_SHA256 = (
    "aa9167b69df90282181b167fe6b5d237baf93f33ca24eebdbd97b5c2c81e3d1e"
)
LIVE_MODEL_SECTION_SHA256 = (
    "9c5bee6f7c856c94426ce3866af4aedbe67284a42a2770b41b1afac8b901fa57"
)
SCENARIO_J_SECTION_SHA256 = (
    "eb9ad8dc0777c4cc1d9d1a9ee5ea7ac84a1bcb9fb304d27f69cccf5e538d89e6"
)
HOLD_MODEL_SECTION_SHA256 = (
    "f14d9ba6700ea67fedd13ca47cfd680d9da667f852be0adfc48959b33997c1e7"
)
FINDING_SCHEMA_ROW_SHA256 = (
    "24056643959d3cb6f24b35943bb4caa242cca205e32c210c75181a7fd3831500"
)
WM14A_ROW_SHA256 = "1b3c03b571c2a44ccab30b99bb06746774b9eada13f86551d30066878914c76e"

WM13_REQUIREMENT = (
    "`work_model.md#intake-is-every-tasks-first-workflow`: every "
    "workflow-entering task atomically gets one intake batch and `ADDRESSED_BY` "
    "at creation; an aggregate parent gets neither and is not claimable"
)
INTAKE_ATOMIC_ENTRY_CLAUSE = (
    "**Entry condition:** every workflow-entering task enters with its intake "
    "batch and `ADDRESSED_BY` edge admitted atomically at creation."
)
INTAKE_ENTRY_PARAGRAPH = (
    INTAKE_ATOMIC_ENTRY_CLAUSE
    + " Creation is publication (`work_model.md#the-transition-vocabulary`), so "
    "this boundary is crossed once and no workflow-entering task enters intake "
    "twice. The aggregate parent is the explicit exception: it is not claimable, "
    "never enters a workflow, and has no intake batch. Decision 84's assembly "
    "exception differs only by adding the persistent `classify` hold finding to "
    "that same creation unit. A task is unrouted while its intake batch has no "
    "closing `route` verdict; there is no separate unrouted state "
    "(`work_model.md#intake-is-every-tasks-first-workflow`)."
)
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
AGGREGATE_PARENT_MODEL_CLAUSE = (
    "An **aggregate parent task is not claimable, never enters a workflow, and "
    "has no intake batch or `ADDRESSED_BY` edge** — it is a grouping, and a "
    "batch carries tasks that are executed, which an aggregate parent never is."
)
AGGREGATE_PARENT_MODEL_PARAGRAPH = (
    "Children `PART_OF` an aggregate parent (at most one parent). Parent completion "
    "is derived from children's terminal states. Children go through workflows "
    "independently. "
    + AGGREGATE_PARENT_MODEL_CLAUSE
    + " This is the deliberate exception to the workflow-entry creation rule; "
    "every child and every other workflow-entering peer task still opens its intake "
    "batch atomically at creation."
)
AGGREGATE_PARENT_ASCENT_PARAGRAPH = (
    "**A task's one `PART_OF` edge targets its parent task or a planning record, and "
    "the records above it are its ascent.** The same edge, with the same one-parent "
    "rule, relates a task to the plan it is under and a plan to whatever the instance "
    "holds above it; the walk upward from a task along `PART_OF` is a derived read, "
    "distinct from the chain, and what a step reads of it is declared and resolved at "
    "hydration "
    "(`planning_model.md#the-hierarchy-is-edges-and-a-task-has-one-line-upward`, "
    "`planning_model.md#upward-context-is-a-declared-read-resolved-along-the-ascent-at-hydration`). "
    "A task with no planning record above it is unplanned, a derived read and never a "
    "status, and it is admitted through intake like any task. Completion at every "
    "level above the task is the parent's rule applied again: derived from the "
    "descendants' terminal states, never stored "
    "(`planning_model.md#downward-state-is-derived-upward-content-is-authored-as-entities`)."
)
AGGREGATE_PARENT_MODEL_SECTION = (
    AGGREGATE_PARENT_MODEL_PARAGRAPH + "\n\n" + AGGREGATE_PARENT_ASCENT_PARAGRAPH
)
AGGREGATE_PARENT_SCENARIO_CLAUSE = (
    "**aggregate parent is the explicit workflow-entry exception: it is not "
    "claimable, never enters a workflow, and has no intake batch or `ADDRESSED_BY` "
    "edge.**"
)
AGGREGATE_PARENT_SCENARIO_PARAGRAPH = (
    "A parent task is created as the grouping of a piece of work; three child tasks "
    "each carry a `PART_OF` edge to it. Each child is claimed, executed, and goes "
    "through its own batch on its own schedule. The "
    + AGGREGATE_PARENT_SCENARIO_CLAUSE
    + " When a reader asks whether the parent is complete, the answer is derived "
    "from the children's terminal states at that moment and is stored nowhere."
)
AGGREGATE_PARENT_SCENARIO_DIAGRAM = """```mermaid
flowchart TD
    P[aggregate parent: not claimable, no intake batch, never in a workflow]
    C1[child 1] -->|PART_OF| P
    C2[child 2] -->|PART_OF| P
    C3[child 3] -->|PART_OF| P
    C1 -->|ADDRESSED_BY| R1[batch 1]
    C2 -->|ADDRESSED_BY| R2[batch 2]
    C3 -->|ADDRESSED_BY| R3[batch 3]
    R1 --> D{all children terminal?}
    R2 --> D
    R3 --> D
    D -->|derived at read| PC[parent reads complete]
```"""
AGGREGATE_PARENT_SCENARIO_INVARIANTS = (
    "**Invariants:** [`work_model.md#parent-and-child-tasks`]"
    "(work_model.md#parent-and-child-tasks); `principles.md` invariant 11."
)
AGGREGATE_PARENT_SCENARIO_SECTION = (
    AGGREGATE_PARENT_SCENARIO_PARAGRAPH
    + "\n\n"
    + AGGREGATE_PARENT_SCENARIO_DIAGRAM
    + "\n\n"
    + AGGREGATE_PARENT_SCENARIO_INVARIANTS
)
WM35_REQUIREMENT = (
    "`work_model.md#parent-and-child-tasks`: an aggregate parent is not claimable, "
    "never enters a workflow, and has no intake batch or `ADDRESSED_BY`; its "
    "children are workflow-entering tasks"
)


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


def _replace_inline_tokens(text: str) -> tuple[str, dict[str, str]]:
    """Protect code spans, escapes, and entities from emphasis parsing."""

    replacements: dict[str, str] = {}

    def protect(value: str) -> str:
        token = f"\ue000{len(replacements)}\ue001"
        replacements[token] = value
        return token

    output: list[str] = []
    cursor = 0
    runs = list(re.finditer(r"`+", text))
    run_index = 0
    while run_index < len(runs):
        opening = runs[run_index]
        output.append(text[cursor : opening.start()])
        closing_index = run_index + 1
        while closing_index < len(runs):
            closing = runs[closing_index]
            if len(closing.group(0)) == len(opening.group(0)):
                content = text[opening.end() : closing.start()]
                content = re.sub(r"[ \t\r\n]+", " ", content)
                if (
                    len(content) >= 2
                    and content.startswith(" ")
                    and content.endswith(" ")
                    and content.strip(" ")
                ):
                    content = content[1:-1]
                output.append(protect(content))
                cursor = closing.end()
                run_index = closing_index + 1
                break
            closing_index += 1
        else:
            output.append(opening.group(0))
            cursor = opening.end()
            run_index += 1
    output.append(text[cursor:])
    protected = "".join(output)

    punctuation = r"[!\"#$%&'()*+,\-./:;<=>?@\[\\\]^_`{|}~]"
    protected = re.sub(
        rf"\\({punctuation})", lambda match: protect(match.group(1)), protected
    )
    entity = re.compile(r"&(?:#[0-9]+|#[xX][0-9A-Fa-f]+|[A-Za-z][A-Za-z0-9]+);")
    protected = entity.sub(
        lambda match: protect(html.unescape(match.group(0))), protected
    )
    return protected, replacements


def _rendered_heading_title(title: str) -> str:
    """Normalize the inline forms that render the same ATX heading text."""

    rendered, replacements = _replace_inline_tokens(title)
    rendered = _without_balanced_emphasis(rendered)
    for token, value in replacements.items():
        rendered = rendered.replace(token, value)
    return rendered


def _without_balanced_emphasis(text: str) -> str:
    """Remove exactly the delimiter characters CommonMark uses as emphasis.

    This is the bounded ``*``/``_`` delimiter-stack algorithm from CommonMark
    0.30's emphasis processing.  Heading-title equivalence is a security
    boundary here: merely pairing whole runs both misses rendered-equivalent
    headings and can erase delimiter residue that remains visible.
    """

    class _Delimiter:
        __slots__ = (
            "can_close",
            "can_open",
            "char",
            "consumed_end",
            "consumed_start",
            "count",
            "next",
            "original_count",
            "previous",
            "run",
        )

        def __init__(
            self,
            run: re.Match[str],
            can_open: bool,
            can_close: bool,
        ) -> None:
            self.run = run
            self.char = run.group(0)[0]
            self.count = len(run.group(0))
            self.original_count = self.count
            self.can_open = can_open
            self.can_close = can_close
            self.consumed_start = 0
            self.consumed_end = 0
            self.previous: _Delimiter | None = None
            self.next: _Delimiter | None = None

    runs = list(re.finditer(r"\*+|_+", text))
    delimiters: list[_Delimiter] = []

    def punctuation(char: str) -> bool:
        return bool(char) and unicodedata.category(char)[0] in {"P", "S"}

    for run in runs:
        marker = run.group(0)
        char = marker[0]
        before = text[run.start() - 1] if run.start() else ""
        after = text[run.end()] if run.end() < len(text) else ""
        before_whitespace = not before or before.isspace()
        after_whitespace = not after or after.isspace()
        before_punctuation = punctuation(before)
        after_punctuation = punctuation(after)
        left_flanking = not after_whitespace and (
            not after_punctuation or before_whitespace or before_punctuation
        )
        right_flanking = not before_whitespace and (
            not before_punctuation or after_whitespace or after_punctuation
        )
        if char == "_":
            can_open = left_flanking and (not right_flanking or before_punctuation)
            can_close = right_flanking and (not left_flanking or after_punctuation)
        else:
            can_open = left_flanking
            can_close = right_flanking

        delimiter = _Delimiter(run, can_open, can_close)
        if delimiters:
            delimiter.previous = delimiters[-1]
            delimiters[-1].next = delimiter
        delimiters.append(delimiter)

    def remove(delimiter: _Delimiter) -> None:
        if delimiter.previous is not None:
            delimiter.previous.next = delimiter.next
        if delimiter.next is not None:
            delimiter.next.previous = delimiter.previous

    openers_bottom: dict[str, _Delimiter | None] = {"*": None, "_": None}
    closer = delimiters[0] if delimiters else None
    while closer is not None:
        if not closer.can_close:
            closer = closer.next
            continue

        opener = closer.previous
        opener_found = False
        odd_match = False
        while opener is not None and opener is not openers_bottom[closer.char]:
            odd_match = (
                (closer.can_open or opener.can_close)
                and closer.original_count % 3 != 0
                and (opener.original_count + closer.original_count) % 3 == 0
            )
            if opener.char == closer.char and opener.can_open and not odd_match:
                opener_found = True
                break
            opener = opener.previous

        old_closer = closer
        if not opener_found:
            closer = closer.next
            if not odd_match:
                openers_bottom[old_closer.char] = old_closer.previous
                if not old_closer.can_open:
                    remove(old_closer)
            continue

        assert opener is not None
        use_delimiters = 2 if closer.count >= 2 and opener.count >= 2 else 1
        opener.count -= use_delimiters
        closer.count -= use_delimiters
        opener.consumed_end += use_delimiters
        closer.consumed_start += use_delimiters

        # Delimiters inside the newly formed emphasis node remain visible but
        # no longer participate in matches outside that node.
        opener.next = closer
        closer.previous = opener
        if opener.count == 0:
            remove(opener)
        if closer.count == 0:
            next_closer = closer.next
            remove(closer)
            closer = next_closer

    output: list[str] = []
    cursor = 0
    for delimiter in delimiters:
        run = delimiter.run
        output.append(text[cursor : run.start()])
        end = len(run.group(0)) - delimiter.consumed_end
        output.append(run.group(0)[delimiter.consumed_start : end])
        cursor = run.end()
    output.append(text[cursor:])
    return "".join(output)


def _heading_spans(text: str, heading: str) -> list[tuple[int, int]]:
    expected = re.fullmatch(r"(#{1,6})[ \t]+(.+)", heading)
    if not expected:
        return []
    level = len(expected.group(1))
    title = _rendered_heading_title(expected.group(2))
    return [
        (start, end)
        for candidate_level, candidate_title, start, end in _active_headings(text)
        if candidate_level == level and candidate_title == title
    ]


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
                    _rendered_heading_title(match.group(2)),
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

    problems: list[str] = []
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
            ("aggregate parent",),
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
    problems += _require_exact_cell(
        "aggregate-parent-wm-35",
        _table_cell(wm35, 1),
        WM35_REQUIREMENT,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    args = parser.parse_args(argv)
    try:
        problems = check(args.root)
    except CorpusProblem as exc:
        print(f"decision 84 check: {exc}", file=sys.stderr)
        return 1
    for problem in problems:
        print(problem)
    print(f"decision 84 check: {len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
