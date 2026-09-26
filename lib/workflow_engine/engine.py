"""
lib/workflow_engine/engine.py — declaration reader and the unreadable-
workflow halt.

This is the first slice of ateles#956 ("Build the workflow engine: nothing
opens a declared step as claimable work"), scoped per the engine task's own
acceptance criterion: "an unreadable declaration must halt with one
checkpoint (reason unreadable_workflow), not degrade" — and per the
implementation spec's component 1 ("Declaration reader. Returns one of
Declared, Absent or Unknown, never an empty tuple.") and component 3
("Opening and hydration").

What this slice deliberately does NOT do (later slices, per the spec's
phase 0 ordering and the engine task's own dependency list — #962, #961,
#957, #958, #959, #960, #921, each landing only as thick as this slice
needs):

  * no LEASE write (that's #957) — `open_steps` returns the steps that
    WOULD become claimable; nothing here claims one or writes a lease.
  * no verdict read/write (#958) — step close and batch advancement are not
    implemented.
  * no checkpoint QUEUE/resolution UI (#959) — this raises the checkpoint
    record via `RecordClient.raise_checkpoint` and stops there.
  * no adapter-sourced intake (#960) — `open_steps` is called with an
    already-formed batch; batch *formation* from an incoming task is out
    of scope here.

Two fail-open defects the implementation spec names on Anthus's existing
`fetch_workflow_definitions` (execution/daemons/anthus/orchestrator.py) are
avoided here by construction rather than patched in place, because this is
a new module, not a retrofit of Anthus — the retrofit (moving Anthus's
reader into this package and retiring the old one) is the cutover slice's
job, not this one's:

  * an unset bearer / unreachable record must not silently become `[]`
    (orchestrator.py lines ~346-348) — here, an unreadable declaration read
    returns `UNKNOWN` from `RecordClient` and `open_steps` treats that as
    the halt condition below, never as "no workflow" / "zero steps, carry
    on".
  * an unparseable structured field must not be coerced to `[]` and
    swallowed (orchestrator.py's `_coerce_list_field`) — here, a
    declaration that fails to parse into a `WorkflowDeclaration` is the
    record client's problem to signal as `UNKNOWN`, not this module's to
    guess at; nothing in this module ever substitutes an empty collection
    for a value it could not read.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from lib.workflow_engine.record import (
    EMPTY,
    UNKNOWN,
    CheckpointRaised,
    RecordClient,
    StepDeclaration,
    Unknown,
    WorkflowDeclaration,
)

UNREADABLE_WORKFLOW = "unreadable_workflow"
UNDECLARED_DEPENDENCY = "undeclared_dependency"
UNDERDETERMINED_INPUTS = "underdetermined_inputs"


class ReadStatus(Enum):
    """
    The three-way result of resolving a step's `reads_to_enter` dependency
    against the record, per the implementation spec's component 3:
    "`reads_to_enter` resolves to Rows, Empty or Unknown." `Unknown` holds
    the batch; `Empty` raises `underdetermined_inputs`. The two must never
    collapse into one another (GW-11).
    """

    ROWS = "rows"
    EMPTY = "empty"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class HydrationResult:
    status: ReadStatus
    entity_type: str
    rows: list[dict] | None = None


@dataclass(frozen=True)
class OpenStepsResult:
    """
    The outcome of asking the engine to open claimable steps for a batch.

    Exactly one of `steps` or `checkpoint` is populated on a halt; a halt
    never also reports steps (GW-42: "any step opens ... no checkpoint
    unreadable_workflow; more than one; an empty step tuple proceeds" are
    all listed as failure conditions — the last one is why `steps=()` on a
    halt is represented distinctly from `steps=()` because the declaration
    genuinely has none).
    """

    steps: tuple[StepDeclaration, ...]
    checkpoint: CheckpointRaised | None = None
    hydration_holds: tuple[HydrationResult, ...] = ()


def read_workflow_declaration(
    record: RecordClient, declaration_scope: str, workflow_type: str
) -> WorkflowDeclaration | Unknown:
    """
    The declaration reader (implementation spec component 1). Returns the
    declaration, or `UNKNOWN` — never `None`, never `[]`, never a partially
    guessed declaration. A caller that wants "no such workflow" as a
    distinct case from "could not read" must ask the record for that
    itself; this reader does not manufacture the distinction, because for
    a registered declaration_scope+workflow_type pair the record is the
    only party that knows whether it is absent or merely unreadable right
    now.
    """
    return record.get_workflow_declaration(declaration_scope, workflow_type)


def hydrate_reads_to_enter(
    record: RecordClient, step: StepDeclaration
) -> tuple[HydrationResult, ...]:
    """
    Resolve every type in `step.reads_to_enter` against the record.
    Returns one `HydrationResult` per declared read type, in declared
    order. Never short-circuits on the first hold, because a caller
    escalating `undeclared_dependency` needs to name every unreadable
    dependency at once, not just the first (failure_posture.md's
    `undeclared_dependency` and `underdetermined_inputs` are stated as
    distinct classes that must not be collapsed).
    """
    results: list[HydrationResult] = []
    for entity_type in step.reads_to_enter:
        read = record.read_rows(entity_type, scope=step.name)
        if read is UNKNOWN:
            results.append(HydrationResult(ReadStatus.UNKNOWN, entity_type))
        elif read is EMPTY:
            results.append(HydrationResult(ReadStatus.EMPTY, entity_type))
        else:
            results.append(HydrationResult(ReadStatus.ROWS, entity_type, rows=read))
    return tuple(results)


def open_steps(
    record: RecordClient,
    *,
    task_id: str,
    declaration_scope: str,
    workflow_type: str,
    completed_steps: tuple[str, ...] = (),
) -> OpenStepsResult:
    """
    Determine which steps of the named workflow are ready to become
    claimable work for a task, given the steps already completed.

    This is intentionally the narrowest possible "opens claimable work"
    behaviour: it reads the declaration, halts on an unreadable one exactly
    as GW-42 requires, and otherwise returns the first step past
    `completed_steps` whose `reads_to_enter` all resolve to `ROWS` or
    `EMPTY`-permitted (a step with no declared reads always opens). A step
    whose hydration holds on `UNKNOWN` returns zero steps and no
    checkpoint from *this* call — the bounded-retry/backoff and the
    eventual `undeclared_dependency` escalation are `work_model.md`'s
    hold-bound mechanic, which is explicitly out of scope for this slice
    (no batch/time state is threaded through here yet); the hold is
    reported back to the caller via `hydration_holds` so a later slice's
    poller can act on it without this function pretending the step opened.
    """
    declaration = read_workflow_declaration(record, declaration_scope, workflow_type)

    if declaration is UNKNOWN:
        checkpoint = record.raise_checkpoint(
            task_id, UNREADABLE_WORKFLOW, needed_input=f"{declaration_scope}:{workflow_type}"
        )
        return OpenStepsResult(steps=(), checkpoint=checkpoint)

    # A `WorkflowDeclaration` with zero declared steps is not the same
    # thing as an unreadable one — GW-42's failure list is explicit that
    # "an empty step tuple proceeds" (silently) is the defect, not that an
    # empty tuple may never legitimately occur. Distinguish them by source:
    # here it comes from a real, read-back declaration, so it is reported
    # as zero steps with no checkpoint — a declaration bug, not a read
    # failure. (A parity/validation check that a declared workflow must
    # have at least one step belongs to the declaration-authoring path,
    # not to this reader.)
    remaining = [s for s in declaration.steps if s.name not in completed_steps]
    if not remaining:
        return OpenStepsResult(steps=())

    next_step = remaining[0]
    hydration = hydrate_reads_to_enter(record, next_step)

    holds = tuple(h for h in hydration if h.status is not ReadStatus.ROWS)
    if any(h.status is ReadStatus.UNKNOWN for h in holds):
        # Unreadable dependency: hold, do not open, do not raise the
        # workflow-level checkpoint (that is GW-42's condition, a different
        # failure than a readable workflow with an unreadable dependency).
        return OpenStepsResult(steps=(), hydration_holds=holds)

    empties = tuple(h for h in holds if h.status is ReadStatus.EMPTY)
    if empties and not next_step.none_permitted:
        # A read that resolved to nothing, for a step that requires input:
        # underdetermined_inputs, per failure_posture.md. Distinct from the
        # unreadable-dependency hold above and from the unreadable-
        # workflow halt: the workflow declaration itself was perfectly
        # readable, and the read succeeded — it just found no rows.
        checkpoint = record.raise_checkpoint(
            task_id,
            UNDERDETERMINED_INPUTS,
            needed_input=empties[0].entity_type,
        )
        return OpenStepsResult(steps=(), checkpoint=checkpoint, hydration_holds=holds)

    return OpenStepsResult(steps=(next_step,), hydration_holds=holds)
