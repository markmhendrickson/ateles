"""Re-issuing a signed checkpoint for tasks still held at the gate.

Background: from 2026-09-28 every signed checkpoint write was refused, leaving
tasks at ``awaiting_approval`` with no checkpoint the operator could see, while
the checkpoints from before signing carry no authority envelope and can never
be resolved.  ``reissue_held_task_checkpoint`` is the recovery.  These tests
drive it against a stateful stand-in for the record, so they assert the EFFECT
on tasks and checkpoints, not merely which functions were called.

What they looked like RED: with the re-issue guards removed, the done-task,
already-resolvable, and idempotency tests fail with a second checkpoint filed or
a finished task re-issued; with the signed write unfixed, nothing persists and
the first test fails on ``failed``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
import reissue_held_checkpoints as cli  # noqa: E402
from lib.daemon_runtime import gating  # noqa: E402
from lib.daemon_runtime.gating import ExecutionPolicy  # noqa: E402


class _Record:
    """Tasks and checkpoints as the record holds them."""

    def __init__(self, monkeypatch):
        self.tasks: dict[str, dict] = {}
        self.checkpoints: dict[str, dict] = {}
        self.by_key: dict[str, str] = {}
        self.writes = 0
        self.persist = True
        policy = ExecutionPolicy(entity_id="policy", loaded=True)
        monkeypatch.setattr(apis, "fetch_task_record", self.fetch_task)
        monkeypatch.setattr(apis, "fetch_entity_user_id", lambda _id: "tenant-a")
        monkeypatch.setattr(apis, "resolve_policy_for_agent", lambda _skill: policy)
        monkeypatch.setattr(apis, "checkpoints_for_task", self.for_task)
        monkeypatch.setattr(apis, "fetch_checkpoint_record", self.fetch_checkpoint)
        monkeypatch.setattr(
            apis, "read_authenticated_checkpoint_authorization", self.authority
        )
        monkeypatch.setattr(apis, "write_checkpoint_brief", self.write_brief)
        monkeypatch.setattr(apis, "supersede_checkpoint", self.supersede)

    def add_task(self, task_id, status="awaiting_approval", **fields):
        self.tasks[task_id] = {
            "entity_id": task_id,
            "entity_type": "task",
            "observation_count": 3,
            "snapshot": {
                "title": f"Task {task_id}",
                "status": status,
                "assigned_to": "cicada",
                **fields,
            },
        }

    def add_checkpoint(self, checkpoint_id, task_id, *, signed, status="awaiting_operator"):
        self.checkpoints[checkpoint_id] = {
            "entity_id": checkpoint_id,
            "entity_type": "checkpoint_" + "brief",
            "signed": signed,
            # The task revision the checkpoint's envelope was signed against.
            "revision": gating.entity_record_digest(self.tasks[task_id]),
            "snapshot": {"task_entity_id": task_id, "status": status},
        }

    def fetch_task(self, task_id):
        return self.tasks.get(task_id)

    def for_task(self, task_id):
        return [
            c
            for c in self.checkpoints.values()
            if c["snapshot"]["task_entity_id"] == task_id
        ]

    def fetch_checkpoint(self, checkpoint_id):
        return self.checkpoints.get(checkpoint_id)

    def authority(self, checkpoint_id, record):
        if not record.get("signed"):
            return None  # a pre-signing brief has no authority envelope
        return {"task_revision": record["revision"]}

    def write_brief(self, *, task_entity_id, idempotency_context, task_record, **_kw):
        self.writes += 1
        if not self.persist:
            return None
        key = f"{task_entity_id}-{idempotency_context}"
        if key not in self.by_key:
            checkpoint_id = f"ent_new_{len(self.by_key)}"
            self.by_key[key] = checkpoint_id
            self.add_checkpoint(checkpoint_id, task_entity_id, signed=True)
        return self.by_key[key]

    def supersede(self, checkpoint_id, *, handler, reason):
        self.checkpoints[checkpoint_id]["snapshot"]["status"] = "superseded"
        return True


@pytest.fixture
def record(monkeypatch):
    return _Record(monkeypatch)


def _awaiting(record):
    return [
        c["entity_id"]
        for c in record.checkpoints.values()
        if c["snapshot"]["status"] == "awaiting_operator" and c["signed"]
    ]


def test_held_task_with_no_checkpoint_gets_a_signed_resolvable_one(record):
    record.add_task("ent_t1")

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert result.outcome == "reissued"
    assert _awaiting(record) == [result.checkpoint_id]
    # The task itself is untouched: nothing is released or dispatched.
    assert record.tasks["ent_t1"]["snapshot"]["status"] == "awaiting_approval"


def test_legacy_pending_checkpoint_is_replaced_and_retired(record):
    record.add_task("ent_t1")
    record.add_checkpoint("ent_legacy", "ent_t1", signed=False)

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert result.outcome == "reissued"
    assert record.checkpoints["ent_legacy"]["snapshot"]["status"] == "superseded"
    assert result.superseded == ["ent_legacy"]
    assert _awaiting(record) == [result.checkpoint_id]


@pytest.mark.parametrize(
    "status", ["done", "completed", "declined", "superseded", "blocked", "executing"]
)
def test_a_task_that_is_not_held_is_never_reissued(record, status):
    record.add_task("ent_t1", status=status)
    record.add_checkpoint("ent_legacy", "ent_t1", signed=False)

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert result.outcome == "skipped"
    assert record.writes == 0
    assert record.checkpoints["ent_legacy"]["snapshot"]["status"] == "awaiting_operator"


def test_rerun_is_idempotent_one_checkpoint_per_task(record):
    record.add_task("ent_t1")

    first = apis.reissue_held_task_checkpoint("ent_t1", apply=True)
    again = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert first.outcome == "reissued"
    assert again.outcome == "skipped"
    assert again.checkpoint_id == first.checkpoint_id
    assert len(_awaiting(record)) == 1
    assert record.writes == 1


def test_changed_task_gets_a_new_checkpoint_and_the_stale_one_is_retired(record):
    record.add_task("ent_t1")
    first = apis.reissue_held_task_checkpoint("ent_t1", apply=True)
    record.tasks["ent_t1"]["observation_count"] = 9  # task moved on since

    second = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert second.outcome == "reissued"
    assert second.checkpoint_id != first.checkpoint_id
    assert second.superseded == [first.checkpoint_id]
    assert _awaiting(record) == [second.checkpoint_id]


def test_dry_run_writes_nothing(record):
    record.add_task("ent_t1")
    record.add_checkpoint("ent_legacy", "ent_t1", signed=False)

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=False)

    assert result.outcome == "would_reissue"
    assert record.writes == 0
    assert record.checkpoints["ent_legacy"]["snapshot"]["status"] == "awaiting_operator"


def test_unpersisted_replacement_fails_closed_and_retires_nothing(record):
    record.add_task("ent_t1")
    record.add_checkpoint("ent_legacy", "ent_t1", signed=False)
    record.persist = False

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert result.outcome == "failed"
    assert record.checkpoints["ent_legacy"]["snapshot"]["status"] == "awaiting_operator"
    assert record.tasks["ent_t1"]["snapshot"]["status"] == "awaiting_approval"


def test_unreadable_checkpoint_list_is_not_read_as_no_checkpoint(record, monkeypatch):
    record.add_task("ent_t1")
    monkeypatch.setattr(apis, "checkpoints_for_task", lambda _id: None)

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert result.outcome == "skipped"
    assert record.writes == 0


def test_an_approved_unreleased_checkpoint_is_left_to_the_release_path(record):
    record.add_task("ent_t1")
    record.add_checkpoint("ent_ok", "ent_t1", signed=True, status="approved")

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert result.outcome == "skipped"
    assert record.writes == 0


def test_enumeration_failure_is_an_error_not_an_empty_list(monkeypatch):
    monkeypatch.setattr(apis, "query_entities", lambda *a, **k: None)

    with pytest.raises(RuntimeError):
        list(apis.iter_held_task_ids())


def test_enumeration_pages_to_the_end(monkeypatch):
    pages = {
        None: {"entities": [{"entity_id": "ent_a"}], "next_cursor": "c1"},
        "c1": {"entities": [{"entity_id": "ent_b"}], "next_cursor": None},
    }
    monkeypatch.setattr(
        apis, "query_entities", lambda _t, *, snapshot_filters, cursor=None: pages[cursor]
    )

    assert list(apis.iter_held_task_ids()) == ["ent_a", "ent_b"]


def test_cli_dry_run_by_default_and_nonzero_on_failure(record, monkeypatch, capsys):
    record.add_task("ent_t1")
    record.persist = False
    monkeypatch.setattr(sys, "argv", ["x", "--task", "ent_t1"])
    assert cli.main() == 0  # dry run never fails a write
    assert record.writes == 0

    monkeypatch.setattr(sys, "argv", ["x", "--task", "ent_t1", "--apply"])
    assert cli.main() == 1
    out = capsys.readouterr().out
    assert '"outcome": "failed"' in out
