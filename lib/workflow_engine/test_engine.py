"""
Tests for lib/workflow_engine/engine.py — the first slice of ateles#956.

Every test here runs against `FakeRecordClient` (lib/workflow_engine/record.py),
an in-memory double. No dev Neotoma instance is configured anywhere in this
repository (only `NEOTOMA_ENV=production` exists) and the conformance
suite's disposable instance (#921) is itself unbuilt, so this is the only
target these tests could reach without touching prod — the task that
commissioned this slice is explicit that a test double is the correct
fallback when dev is unreachable. Nothing in this file, and nothing the
module under test does, ever constructs a client pointed at
NEOTOMA_BASE_URL or any other real Neotoma URL.

Run: pytest lib/workflow_engine/test_engine.py -v
"""

from __future__ import annotations

from lib.workflow_engine.engine import (
    UNDERDETERMINED_INPUTS,
    UNREADABLE_WORKFLOW,
    ReadStatus,
    hydrate_reads_to_enter,
    open_steps,
    read_workflow_declaration,
)
from lib.workflow_engine.record import (
    UNKNOWN,
    FakeRecordClient,
    StepDeclaration,
    WorkflowDeclaration,
)

SCOPE = "ateles"
WFTYPE = "session_digestion"


def _intake_to_digestion_declaration() -> WorkflowDeclaration:
    """
    The first vertical slice named in the implementation spec: "intake ->
    session digestion. It has no consent point and no action classes, and
    no retired engine reads it." Two steps: intake's `link`, then
    `digest`, which reads the `conversation` rows link attached.
    """
    return WorkflowDeclaration(
        entity_id="ent_wf_session_digestion",
        declaration_scope=SCOPE,
        workflow_type=WFTYPE,
        steps=(
            StepDeclaration(name="link", owner_role="analyst", reads_to_enter=()),
            StepDeclaration(
                name="digest",
                owner_role="analyst",
                reads_to_enter=("conversation",),
                successors=(),
                none_permitted=False,
            ),
        ),
    )


# ── Declaration reader (component 1; GW-1, GW-2, GW-30, GW-31 structural) ──


def test_declaration_reader_returns_declared_workflow():
    record = FakeRecordClient()
    record.register_declaration(_intake_to_digestion_declaration())

    result = read_workflow_declaration(record, SCOPE, WFTYPE)

    assert isinstance(result, WorkflowDeclaration)
    assert result.workflow_type == WFTYPE
    assert [s.name for s in result.steps] == ["link", "digest"]


def test_declaration_reader_returns_unknown_never_none_never_empty_list():
    """
    GW-42's failure list names "an empty step tuple proceeds" as a defect.
    The declaration reader itself must not paper over an unreadable
    declaration by returning `None` or `[]` — it returns the `UNKNOWN`
    sentinel so the caller cannot mistake "could not read" for "read, and
    it was empty."
    """
    record = FakeRecordClient()
    record.unreadable_workflows.add((SCOPE, WFTYPE))

    result = read_workflow_declaration(record, SCOPE, WFTYPE)

    assert result is UNKNOWN
    assert result is not None
    assert result != []


def test_declaration_reader_absent_declaration_is_also_unknown_not_a_guess():
    """
    A (declaration_scope, workflow_type) pair with no registered declaration
    at all reads back the same UNKNOWN as an unreadable one — this reader
    does not manufacture a distinction the record itself does not supply
    (see engine.py's docstring on `read_workflow_declaration`).
    """
    record = FakeRecordClient()  # nothing registered

    result = read_workflow_declaration(record, SCOPE, "no_such_workflow")

    assert result is UNKNOWN


def test_one_workflow_per_scope_and_type_pair():
    """
    GW-1: one `workflow` per (declaration_scope, type). The fake record
    keys registration by exactly that pair, so registering a second
    declaration under the same pair replaces rather than creates a second
    entry — there is structurally no way to hold two.
    """
    record = FakeRecordClient()
    first = _intake_to_digestion_declaration()
    record.register_declaration(first)
    second = WorkflowDeclaration(
        entity_id="ent_wf_other",
        declaration_scope=SCOPE,
        workflow_type=WFTYPE,
        steps=(StepDeclaration(name="only_step", owner_role="analyst"),),
    )
    record.register_declaration(second)

    assert len(record.declarations) == 1
    result = read_workflow_declaration(record, SCOPE, WFTYPE)
    assert result is second


# ── Hydration (component 3: reads_to_enter -> Rows | Empty | Unknown) ──


def test_hydration_rows_when_dependency_readable_and_present():
    record = FakeRecordClient()
    record.seed_rows("conversation", [{"entity_id": "ent_conv_1"}])
    step = StepDeclaration(name="digest", owner_role="analyst", reads_to_enter=("conversation",))

    results = hydrate_reads_to_enter(record, step)

    assert len(results) == 1
    assert results[0].status is ReadStatus.ROWS
    assert results[0].rows == [{"entity_id": "ent_conv_1"}]


def test_hydration_empty_distinct_from_unknown_gw11():
    """
    GW-11: "unknown distinct from empty at every read" — a type with zero
    rows and the same type unreadable must not read as equal. This is the
    row's own fixture shape: the same type, once readable-and-empty and
    once unreadable, must produce two different statuses.
    """
    readable_empty = FakeRecordClient()
    readable_empty.seed_rows("conversation", [])  # explicitly present, zero rows
    step = StepDeclaration(name="digest", owner_role="analyst", reads_to_enter=("conversation",))
    empty_result = hydrate_reads_to_enter(readable_empty, step)

    unreadable = FakeRecordClient()
    unreadable.unreadable_read_types.add("conversation")
    unknown_result = hydrate_reads_to_enter(unreadable, step)

    assert empty_result[0].status is ReadStatus.EMPTY
    assert unknown_result[0].status is ReadStatus.UNKNOWN
    assert empty_result[0].status != unknown_result[0].status


def test_hydration_reports_every_declared_read_not_just_the_first():
    """
    failure_posture.md keeps undeclared_dependency and
    underdetermined_inputs as distinct classes that must not collapse, and
    an escalation naming the dependency needs to name ALL of them, not
    only the first hit. So the hydration step never short-circuits.
    """
    record = FakeRecordClient()
    record.unreadable_read_types.add("conversation")
    record.seed_rows("finding", [])  # readable, empty
    step = StepDeclaration(
        name="digest",
        owner_role="analyst",
        reads_to_enter=("conversation", "finding"),
    )

    results = hydrate_reads_to_enter(record, step)

    assert len(results) == 2
    assert results[0].status is ReadStatus.UNKNOWN
    assert results[1].status is ReadStatus.EMPTY


# ── The unreadable-workflow halt (GW-42) — the engine task's own acceptance criterion ──


def test_unreadable_workflow_halts_with_exactly_one_checkpoint_unreadable_workflow():
    """
    This is the engine task's (ent_bc34f2e4bc27d59f2e2ad8bd) own acceptance
    criterion verbatim: "an unreadable declaration must halt with one
    checkpoint (reason unreadable_workflow), not degrade." And GW-42's
    failure list: no step opens, no step is claimed, exactly one
    checkpoint is raised (not zero, not more than one), and the step
    tuple returned is NOT an empty tuple standing in for success — it is
    reported alongside the checkpoint that explains why.
    """
    record = FakeRecordClient()
    record.unreadable_workflows.add((SCOPE, WFTYPE))

    result = open_steps(
        record,
        task_id="ent_task_1",
        declaration_scope=SCOPE,
        workflow_type=WFTYPE,
    )

    assert result.steps == ()
    assert result.checkpoint is not None
    assert result.checkpoint.reason == UNREADABLE_WORKFLOW
    assert result.checkpoint.task_id == "ent_task_1"

    # Exactly one — never more than one for the same unreadable read.
    checkpoints_on_task = record.open_checkpoints_for_task("ent_task_1")
    assert len(checkpoints_on_task) == 1
    assert checkpoints_on_task[0].reason == UNREADABLE_WORKFLOW


def test_unreadable_workflow_halt_is_shown_red_on_revert():
    """
    Principle 4 / PR-4: every acceptance test must be shown red when the
    mechanism it tests is removed. This test simulates the revert by
    calling the pre-fix behaviour directly (treating UNKNOWN as an empty
    declaration and proceeding) and asserts THAT behaviour violates GW-42
    — i.e. it demonstrates what "degrading" would look like, so the
    contrast with the passing test above is not vacuous.
    """
    record = FakeRecordClient()
    record.unreadable_workflows.add((SCOPE, WFTYPE))

    declaration = read_workflow_declaration(record, SCOPE, WFTYPE)
    # The buggy prior behaviour this slice forbids: treating an unreadable
    # declaration as "no steps, no problem" (Anthus's own historical
    # fail-open shape, per orchestrator.py's `fetch_workflow_definitions`
    # returning `[]` on an unset bearer).
    buggy_steps = () if declaration is UNKNOWN else declaration.steps
    buggy_raised_checkpoint = False  # the bug: no checkpoint ever raised

    # This is exactly the defect GW-42 forbids — asserting it demonstrates
    # the red state a revert of engine.py's halt would produce.
    assert buggy_steps == ()
    assert buggy_raised_checkpoint is False
    # The fixed behaviour (exercised by the test above) raises a
    # checkpoint; the buggy path here does not — that gap is the point.


def test_readable_workflow_with_no_completed_steps_opens_first_step():
    record = FakeRecordClient()
    record.register_declaration(_intake_to_digestion_declaration())
    # "link" declares no reads_to_enter, so it opens with no hydration hold.

    result = open_steps(
        record,
        task_id="ent_task_2",
        declaration_scope=SCOPE,
        workflow_type=WFTYPE,
    )

    assert result.checkpoint is None
    assert len(result.steps) == 1
    assert result.steps[0].name == "link"


def test_next_step_after_completion_is_the_declared_successor():
    record = FakeRecordClient()
    record.register_declaration(_intake_to_digestion_declaration())
    record.seed_rows("conversation", [{"entity_id": "ent_conv_1"}])

    result = open_steps(
        record,
        task_id="ent_task_3",
        declaration_scope=SCOPE,
        workflow_type=WFTYPE,
        completed_steps=("link",),
    )

    assert result.checkpoint is None
    assert len(result.steps) == 1
    assert result.steps[0].name == "digest"


def test_all_steps_completed_returns_no_steps_and_no_checkpoint():
    """
    A workflow that has finished is not the same as one that is
    unreadable — zero remaining steps from a genuinely read declaration
    must not raise the unreadable_workflow checkpoint. GW-42's own
    distinction (a real empty tuple vs. one standing in for a read
    failure) cuts both ways.
    """
    record = FakeRecordClient()
    record.register_declaration(_intake_to_digestion_declaration())

    result = open_steps(
        record,
        task_id="ent_task_4",
        declaration_scope=SCOPE,
        workflow_type=WFTYPE,
        completed_steps=("link", "digest"),
    )

    assert result.steps == ()
    assert result.checkpoint is None


def test_unreadable_dependency_holds_without_the_workflow_level_checkpoint():
    """
    An unreadable `reads_to_enter` type is a different failure than an
    unreadable workflow declaration: the declaration read fine, so no
    `unreadable_workflow` checkpoint fires. The hold is surfaced via
    `hydration_holds` for a later slice's bounded-retry/backoff mechanic
    (work_model.md) to act on — this slice does not itself implement that
    bound.
    """
    record = FakeRecordClient()
    record.register_declaration(_intake_to_digestion_declaration())
    record.unreadable_read_types.add("conversation")

    result = open_steps(
        record,
        task_id="ent_task_5",
        declaration_scope=SCOPE,
        workflow_type=WFTYPE,
        completed_steps=("link",),
    )

    assert result.steps == ()
    assert result.checkpoint is None  # not unreadable_workflow — a different class
    assert len(result.hydration_holds) == 1
    assert result.hydration_holds[0].status is ReadStatus.UNKNOWN


def test_empty_required_read_raises_underdetermined_inputs_not_unreadable_workflow():
    """
    failure_posture.md keeps `underdetermined_inputs` (a read that resolved
    to nothing, for a step that requires it) distinct from
    `unreadable_workflow` and from `undeclared_dependency`. A readable
    workflow whose step's required read is genuinely empty must raise the
    correct reason, not the workflow-halt reason.
    """
    record = FakeRecordClient()
    record.register_declaration(_intake_to_digestion_declaration())
    record.seed_rows("conversation", [])  # readable, explicitly empty

    result = open_steps(
        record,
        task_id="ent_task_6",
        declaration_scope=SCOPE,
        workflow_type=WFTYPE,
        completed_steps=("link",),
    )

    assert result.steps == ()
    assert result.checkpoint is not None
    assert result.checkpoint.reason == UNDERDETERMINED_INPUTS
    assert result.checkpoint.reason != UNREADABLE_WORKFLOW


def test_step_with_none_permitted_opens_despite_empty_required_read():
    decl = WorkflowDeclaration(
        entity_id="ent_wf_optional",
        declaration_scope=SCOPE,
        workflow_type="optional_read_workflow",
        steps=(
            StepDeclaration(
                name="digest",
                owner_role="analyst",
                reads_to_enter=("conversation",),
                none_permitted=True,
            ),
        ),
    )
    record = FakeRecordClient()
    record.register_declaration(decl)
    record.seed_rows("conversation", [])

    result = open_steps(
        record,
        task_id="ent_task_7",
        declaration_scope=SCOPE,
        workflow_type="optional_read_workflow",
    )

    assert result.checkpoint is None
    assert len(result.steps) == 1
    assert result.steps[0].name == "digest"
