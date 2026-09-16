"""A finished task must never be re-dispatched (ateles#1038).

`dispatch_task` read `current_status = snapshot.get("status")` and never tested
it — the value was passed to `set_task_status(..., from_status=...)` as a label
only. So a `task.created` SSE event, including a redelivery or a replay, routed
a finished task through the readiness gate and the execution gate, and the
gate's `AWAITING_APPROVAL` write landed on top of `done`/`completed`.

Observed on Neotoma prod 2026-09-16 while auditing the operator's checkpoint
queue: pending `checkpoint_brief` entities sitting on tasks already terminal,
some carrying a `blocked_reason` that is a completion note naming the commits
that finished them. One batch showed the ordering exactly — brief written
09:31-09:32Z, task re-asserted `completed` at 09:34Z, the gate's reason string
left over work that was already done.

The operator's decision queue was therefore partly a queue of decisions about
finished work: noise that looks exactly like signal, diluting the queue that
genuinely irreversible actions sit in.

These tests assert the EFFECT at the dispatch layer — no status write, no
checkpoint brief, no spawn — rather than merely that `TERMINAL` contains the
right strings. The unit-level guarantees live in
`lib/daemon_runtime/test_task_lifecycle_terminal.py`. That split is deliberate:
this guard has to be threaded into the real dispatch path ahead of both gates,
and only an end-to-end call proves it is (the lesson of
`test_noowner_escalation.py`, which drives the same entrypoint for the same
reason).

What these looked like RED, before the guard:

    test_done_task_is_not_dispatched
        AssertionError: a terminal task was re-dispatched: status writes
        [('ent_done_1', <TaskStatus.ROUTED: 'routed'>)]

    test_completed_task_is_not_dispatched
        AssertionError: a terminal task was re-dispatched: status writes
        [('ent_completed_1', <TaskStatus.ROUTED: 'routed'>)]
        — this is the spelling the guard alone would still have missed
          (ateles#1039)

    test_terminal_task_writes_no_checkpoint_brief
        AssertionError: wrote a checkpoint brief for a finished task — this is
        the queue entry the operator was asked to decide

Run: pytest execution/daemons/apis/test_terminal_task_never_dispatches.py -v
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
from unroutable_ledger import UnroutableLedger  # noqa: E402


class _Notifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message, priority=None, handler=None):
        self.sent.append(message)


@pytest.fixture(autouse=True)
def _isolated_ledger(monkeypatch, tmp_path):
    """Never touch the operator's real on-disk ledger."""
    monkeypatch.setattr(apis, "_unroutable", UnroutableLedger(path=tmp_path / "l.json"))
    monkeypatch.setattr(apis, "_created_seen", {})


@pytest.fixture
def writes(monkeypatch):
    """Capture every status write instead of sending it to Neotoma."""
    calls: list[tuple] = []

    def _capture(entity_id, status, **kw):
        calls.append((entity_id, status))
        return True

    monkeypatch.setattr(apis, "set_task_status", _capture)
    return calls


@pytest.fixture
def briefs(monkeypatch):
    """Capture checkpoint briefs — the queue entries this defect manufactured."""
    written: list[str] = []
    monkeypatch.setattr(
        apis,
        "write_checkpoint_brief",
        lambda **kw: written.append(kw.get("task_entity_id")) or "ent_brief",
    )
    return written


class _SpawnResult:
    """Stand-in for the spawn machinery's result object.

    Returns ok=True deliberately: a stub that failed would send dispatch down
    the FAILED path and could mask the guard's absence behind an unrelated
    error. The success path is the one that proves a finished task was actually
    re-run.
    """

    ok = True
    pr_url = None
    detail = "stubbed"


@pytest.fixture(autouse=True)
def spawns(monkeypatch):
    """Capture any attempt to actually run an agent on the task."""
    ran: list = []

    async def _never(skill, entity_id, *a, **kw):
        ran.append((skill, entity_id))
        return _SpawnResult()

    monkeypatch.setattr(apis, "_spawn_harness_skill", _never)
    return ran


def _dispatch(entity_id, snapshot, notifier=None, **kw):
    asyncio.run(
        apis.dispatch_task(
            entity_id,
            snapshot,
            trigger=kw.pop("trigger", "created"),
            notifier=notifier or _Notifier(),
            snapshot_hydrated=True,
            **kw,
        )
    )


def _task(status: str) -> dict:
    """A well-formed, routable task that differs ONLY in being finished.

    Routable on purpose: if the task could not route, the test would pass for
    the wrong reason — an unroutable task is held by a different branch
    entirely, and the guard would go unexercised.
    """
    return {
        "title": "Reconcile the swarm dispatch ledger",
        "body": "Engineering work on the apis dispatcher and its gates.",
        "assigned_to": "cicada",
        "status": status,
        "confidence": 0.95,
    }


# ── The guard ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["done", "declined", "superseded"])
def test_terminal_task_is_not_dispatched(status, writes, briefs, spawns):
    """No status write, no brief, no spawn — the task is already finished."""
    _dispatch(f"ent_{status}_1", _task(status))
    assert writes == [], f"a terminal task was re-dispatched: status writes {writes}"
    assert briefs == []
    assert spawns == []


@pytest.mark.parametrize("status", ["completed", "cancelled", "rejected", "COMPLETED"])
def test_terminal_synonym_task_is_not_dispatched(status, writes, briefs, spawns):
    """The spelling the terminal guard ALONE would still have missed.

    `completed` is not in `TaskStatus`; before ateles#1039 it hit
    `can_transition`'s permissive unknown-origin branch. A guard testing
    `status in TERMINAL` against the raw snapshot string would let every one of
    these through — which is why `is_terminal()` normalizes first.
    """
    _dispatch(f"ent_{status.lower()}_1", _task(status))
    assert writes == [], f"a terminal task was re-dispatched: status writes {writes}"
    assert briefs == []
    assert spawns == []


def test_terminal_task_writes_no_checkpoint_brief(briefs, writes):
    """The queue entry, named as the harm.

    Each of these is a decision the operator was asked to make about work that
    was already done.
    """
    _dispatch("ent_sweep_1", _task("completed"))
    assert briefs == [], (
        "wrote a checkpoint brief for a finished task — this is the queue entry "
        "the operator was asked to decide"
    )


def test_terminal_task_does_not_page_the_operator(writes, briefs):
    """A finished task is not news. Holding it is the defect; paging about
    holding it is the defect reaching the operator's phone."""
    n = _Notifier()
    _dispatch("ent_quiet_1", _task("done"), notifier=n)
    assert n.sent == [], f"paged the operator about a finished task: {n.sent}"


def test_gate_override_does_not_reopen_a_terminal_task(writes, briefs, spawns):
    """Operator approval of some earlier checkpoint is not an instruction to
    re-run finished work.

    `gate_override` skips the execution gate, so a guard placed inside the gate
    branch would be bypassed on exactly the path where a stale approval replays.
    The guard must sit above it.
    """
    _dispatch("ent_override_1", _task("done"), trigger="approved", gate_override=True)
    assert writes == []
    assert briefs == []
    assert spawns == []


# ── The guard must not over-reach ────────────────────────────────────────────


@pytest.mark.parametrize(
    "status", ["pending", "routed", "executing", "failed", "blocked", "awaiting_input"]
)
def test_active_task_still_dispatches(status, writes):
    """`BLOCKED` is deliberately NOT terminal — operator remediation reopens it.

    Without this, the fix for a queue full of finished work would be a
    dispatcher that refuses live work, which is the larger outage.
    """
    _dispatch(f"ent_active_{status}", _task(status))
    assert writes, f"{status} task was not dispatched — the guard is too broad"


def test_task_with_no_status_still_dispatches(writes):
    """Absence is not terminal: an unset status is a new task, not a finished
    one. A guard that read absence as terminal would silently stop dispatching
    every newly created task."""
    snapshot = _task("pending")
    del snapshot["status"]
    _dispatch("ent_nostatus_1", snapshot)
    assert writes, "a task with no status was treated as finished"
