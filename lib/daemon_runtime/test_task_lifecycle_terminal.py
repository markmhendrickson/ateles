"""Terminal statuses must be terminal under every spelling agents actually use.

Two defects, both found 2026-09-16 while auditing why the operator's checkpoint
queue held decisions about work that was already finished (ateles#1039):

1. `TERMINAL` and `ACTIVE` were defined correctly and consumed by nothing
   outside `can_transition` and two self-test assertions. A control that exists,
   is correct, and fails nothing is documentation.

2. `completed` was not in the vocabulary at all. `TaskStatus` declares
   `DONE = "done"`; `normalize()` only stripped and lowercased. Live tasks
   written by agents carry `completed`, which was in neither `TERMINAL` nor
   `ACTIVE` and was not a key in `_TRANSITIONS` — so `can_transition` took its
   permissive `f not in _TRANSITIONS -> return True` unknown-origin branch and
   treated a finished task as freely movable.

The second compounds the first: even a correctly wired terminal guard would
have let every task spelled `completed` straight through it.

What these looked like RED, before the fix:

    test_completed_is_terminal
        AssertionError: 'completed' is not terminal — a finished task reads as
        movable (assert 'completed' in frozenset({'declined','done','superseded'}))

    test_cannot_transition_out_of_completed
        AssertionError: completed -> awaiting_approval was allowed: a finished
        task can be re-opened by the dispatcher

    test_terminal_synonyms_are_terminal[cancelled]
        AssertionError: 'cancelled' is not terminal

Run: pytest lib/daemon_runtime/test_task_lifecycle_terminal.py -v
"""

from __future__ import annotations

import pytest

from lib.daemon_runtime.task_lifecycle import (
    ACTIVE,
    TERMINAL,
    TaskStatus,
    can_transition,
    is_terminal,
    normalize,
)


# ── The spelling gap ─────────────────────────────────────────────────────────


def test_completed_normalizes_to_done():
    """Agents write `completed`; the lifecycle's canonical spelling is `done`.

    Normalizing at the single chokepoint every comparison already passes
    through is what makes every consumer correct at once, rather than each
    one needing to remember the synonym.
    """
    assert normalize("completed") == TaskStatus.DONE.value
    assert normalize(" Completed ") == TaskStatus.DONE.value
    assert normalize("COMPLETE") == TaskStatus.DONE.value


def test_completed_is_terminal():
    """The defect in one line: 11 live tasks used a spelling TERMINAL could not see."""
    assert normalize("completed") in TERMINAL, (
        "'completed' is not terminal — a finished task reads as movable"
    )
    assert is_terminal("completed")


@pytest.mark.parametrize(
    "spelling", ["completed", "complete", "cancelled", "canceled", "rejected"]
)
def test_terminal_synonyms_are_terminal(spelling):
    """Every synonym of a finished state resolves terminal, under any casing.

    `cancelled` and `rejected` were observed on live tasks carrying pending
    checkpoints. A synonym nobody mapped is indistinguishable from a status
    nobody has heard of, and the unknown branch is the permissive one.
    """
    assert is_terminal(spelling), f"{spelling!r} is not terminal"
    assert is_terminal(f"  {spelling.upper()}  ")


def test_terminal_and_active_are_disjoint():
    """A value added to a safety vocabulary goes into BOTH sides of the
    classification. Overlap would make a status terminal for one check and
    dispatchable for another."""
    assert not (TERMINAL & ACTIVE), f"overlap: {TERMINAL & ACTIVE}"


# ── The transition graph ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "to_status",
    ["routed", "executing", "awaiting_approval", "pending", "blocked"],
)
def test_cannot_transition_out_of_completed(to_status):
    """This is the sweep, expressed as a unit.

    `completed -> awaiting_approval` is precisely the write the execution gate
    made over finished tasks: the brief was written, then the task re-asserted
    itself completed, leaving the gate's reason string on work that was done.
    """
    assert not can_transition("completed", to_status), (
        f"completed -> {to_status} was allowed: a finished task can be "
        "re-opened by the dispatcher"
    )


@pytest.mark.parametrize("terminal", sorted(TERMINAL))
@pytest.mark.parametrize("to_status", ["routed", "executing", "awaiting_approval"])
def test_no_terminal_state_moves(terminal, to_status):
    assert not can_transition(terminal, to_status)


def test_terminal_re_entry_is_still_idempotent():
    """A replay re-asserting the SAME terminal status must not be refused.

    Refusing this would turn an idempotent SSE redelivery into an error, which
    is the failure mode that makes a guard get disabled.
    """
    assert can_transition("done", "done")
    assert can_transition("completed", "done")
    assert can_transition("completed", "completed")


def test_unknown_origin_stays_permissive_for_genuinely_unknown_values():
    """The fix must narrow the unknown branch, not close it.

    A status nobody has ever heard of still records progress — the goal is to
    record, not to police. What changed is that a *finished* status is no
    longer mistaken for an unknown one.
    """
    assert can_transition("some_legacy_state", "routed")


# ── The binding ──────────────────────────────────────────────────────────────


def test_is_terminal_handles_absence_restrictively_where_it_matters():
    """Absent/empty is not terminal — an unset status is a new task, not a
    finished one. Stated as a test so the default cannot drift silently."""
    assert not is_terminal(None)
    assert not is_terminal("")
    assert not is_terminal("   ")
