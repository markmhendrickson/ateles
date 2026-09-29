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
    record via `CheckpointWriter.raise_checkpoint` and stops there.
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
    returns `UNKNOWN` from `RecordReader` and `open_steps` treats that as
    the halt condition below, never as "no workflow" / "zero steps, carry
    on".
  * an unparseable structured field must not be coerced to `[]` and
    swallowed (orchestrator.py's `_coerce_list_field`) — here, a
    declaration that fails to parse into a `WorkflowDeclaration` is the
    record client's problem to signal as `UNKNOWN`, not this module's to
    guess at; nothing in this module ever substitutes an empty collection
    for a value it could not read.

Two findings from pm/qa review on PR #1310 (2026-09-26), both fixed here:

  * **Idempotent halt.** `open_steps()` used to call `raise_checkpoint()`
    unconditionally on every unreadable-declaration call, so polling the
    same still-unreadable workflow twice produced two `unreadable_workflow`
    checkpoints — the acceptance criterion is exactly one. The checkpoint
    now carries a deterministic `idempotency_key` built from the logical
    halt's identity (reason + task + declaration_scope + workflow_type, or
    + needed_input for `underdetermined_inputs`), and the writer
    (`FakeRecordClient.raise_checkpoint`) returns the existing checkpoint
    unchanged on a repeat key rather than creating a second one. See
    `test_repeated_poll_of_unreadable_workflow_raises_only_one_checkpoint`.
  * **Structural production-write refusal.** `open_steps()` now takes its
    checkpoint-writing capability as a `NonProductionCheckpointWriter`
    (record.py), which refuses at construction — before any write is
    attempted — unless the wrapped writer's `instance_label` is on an
    allow-list of non-production labels. This holds even once a real
    `CheckpointWriter` implementation exists; it does not depend on this
    PR only having a fake. See
    `test_engine_refuses_a_checkpoint_writer_labelled_production`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from lib.workflow_engine.record import (
    EMPTY,
    UNKNOWN,
    CheckpointRaised,
    NonProductionCheckpointWriter,
    ProductionWriteRefused,
    RecordReader,
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
    record: RecordReader, declaration_scope: str, workflow_type: str
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
    record: RecordReader, step: StepDeclaration
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


def _checkpoint_idempotency_key(
    *, reason: str, task_id: str, declaration_scope: str, workflow_type: str, needed_input: str
) -> str:
    """
    The identity of a logical halt, not of a single `open_steps()` call.
    Two calls with the same task, reason, declaration identity, and
    (where relevant) the same needed_input must produce the SAME key, so
    that repeated polling of a still-unreadable workflow — the exact
    scenario pm/qa's review named — finds the existing open checkpoint
    instead of minting a new one. Deliberately excludes anything that
    would vary call-to-call for the same underlying condition (a
    timestamp, a call counter); including either would silently
    reintroduce the duplicate-checkpoint bug this key exists to prevent.
    """
    return f"{reason}:{task_id}:{declaration_scope}:{workflow_type}:{needed_input}"


def open_steps(
    record: RecordReader,
    checkpoints: NonProductionCheckpointWriter,
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

    `record` is read-only by type (`RecordReader` carries no write method).
    `checkpoints` must be a `NonProductionCheckpointWriter` — the only
    write this function performs is routed through it, and its
    constructor already refused to exist if the wrapped writer is not
    labelled non-production.

    That constructor check is NOT sufficient on its own (PR #1310 pm/qa
    follow-up, 2026-09-26): `CheckpointWriter` and `NonProductionCheckpointWriter`
    are both `typing.Protocol`s, so Python's structural typing means any
    object exposing methods of the right shape — including a BARE
    `FakeRecordClient` labelled `"production"`, never passed through the
    wrapper's constructor at all — satisfies the `checkpoints` parameter's
    type hint with zero runtime enforcement. A type hint is not a check;
    only code that runs is. So this function itself verifies with
    `isinstance(checkpoints, NonProductionCheckpointWriter)` as its very
    first statement, before the declaration is even read, and refuses
    with `ProductionWriteRefused` on anything that is not a genuine
    instance constructed through that class's `__post_init__` (which is
    where the label allow-list check actually runs). This closes the
    exact bypass qa demonstrated: `open_steps(record, bare_prod, ...)`
    with `bare_prod` an unwrapped `FakeRecordClient(instance_label="production")`.

    Repeated calls with the same `task_id`/`declaration_scope`/
    `workflow_type` (a poller re-checking a still-unreadable workflow) are
    idempotent at the checkpoint layer: the same logical halt always
    derives the same `idempotency_key`, and the writer returns the
    existing checkpoint rather than creating a second one.
    """
    if not isinstance(checkpoints, NonProductionCheckpointWriter):
        raise ProductionWriteRefused(
            "open_steps() refuses to run: `checkpoints` must be an actual "
            f"NonProductionCheckpointWriter instance, got "
            f"{type(checkpoints).__name__}. A bare writer satisfies the "
            "type hint structurally (Protocol) but has never had its "
            "instance_label checked — wrap it: "
            "NonProductionCheckpointWriter(writer, writer.instance_label)."
        )

    declaration = read_workflow_declaration(record, declaration_scope, workflow_type)

    if declaration is UNKNOWN:
        needed_input = f"{declaration_scope}:{workflow_type}"
        key = _checkpoint_idempotency_key(
            reason=UNREADABLE_WORKFLOW,
            task_id=task_id,
            declaration_scope=declaration_scope,
            workflow_type=workflow_type,
            needed_input=needed_input,
        )
        checkpoint = checkpoints.raise_checkpoint(
            task_id, UNREADABLE_WORKFLOW, idempotency_key=key, needed_input=needed_input
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
        needed_input = empties[0].entity_type
        key = _checkpoint_idempotency_key(
            reason=UNDERDETERMINED_INPUTS,
            task_id=task_id,
            declaration_scope=declaration_scope,
            workflow_type=workflow_type,
            needed_input=needed_input,
        )
        checkpoint = checkpoints.raise_checkpoint(
            task_id,
            UNDERDETERMINED_INPUTS,
            idempotency_key=key,
            needed_input=needed_input,
        )
        return OpenStepsResult(steps=(), checkpoint=checkpoint, hydration_holds=holds)

    return OpenStepsResult(steps=(next_step,), hydration_holds=holds)
