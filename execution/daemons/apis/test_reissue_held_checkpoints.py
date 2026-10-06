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
        self.fail_supersede: set[str] = set()
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

    def add_checkpoint(
        self, checkpoint_id, task_id, *, signed, status="awaiting_operator"
    ):
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
        if checkpoint_id in self.fail_supersede:
            return False
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


def test_unreadable_checkpoint_list_is_a_failure_not_a_skip(record, monkeypatch):
    record.add_task("ent_t1")
    monkeypatch.setattr(apis, "checkpoints_for_task", lambda _id: None)

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert result.outcome == "failed"
    assert record.writes == 0


def test_unreadable_task_is_a_failure_not_a_skip(record):
    # ent_missing was never added: the read returns nothing.
    result = apis.reissue_held_task_checkpoint("ent_missing", apply=True)

    assert result.outcome == "failed"
    assert record.writes == 0


def test_failed_retirement_is_incomplete_and_a_rerun_finishes_it(record):
    record.add_task("ent_t1")
    record.add_checkpoint("ent_legacy", "ent_t1", signed=False)
    record.fail_supersede = {"ent_legacy"}

    first = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert first.outcome == "incomplete"
    assert first.remaining == ["ent_legacy"]
    assert first.checkpoint_id is not None
    writes_after_first = record.writes

    record.fail_supersede = set()  # the transient failure clears
    second = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert second.outcome == "retired_stale"
    assert second.checkpoint_id == first.checkpoint_id, "the replacement is reused"
    assert record.writes == writes_after_first, "no second replacement is created"
    assert record.checkpoints["ent_legacy"]["snapshot"]["status"] == "superseded"
    assert _awaiting(record) == [first.checkpoint_id]
    assert record.tasks["ent_t1"]["snapshot"]["status"] == "awaiting_approval"

    third = apis.reissue_held_task_checkpoint("ent_t1", apply=True)
    assert third.outcome == "skipped"


def test_replacement_is_never_retired_even_if_the_record_returns_its_id_as_stale(
    record, monkeypatch
):
    """A replacement that comes back under an id already pending must survive."""
    record.add_task("ent_t1")
    record.add_checkpoint("ent_same", "ent_t1", signed=False)
    retired: list[str] = []
    original = record.supersede

    def spy(checkpoint_id, **kwargs):
        retired.append(checkpoint_id)
        return original(checkpoint_id, **kwargs)

    monkeypatch.setattr(apis, "supersede_checkpoint", spy)
    # The writer hands back the already-pending id (identity collision).
    monkeypatch.setattr(apis, "write_checkpoint_brief", lambda **_kw: "ent_same")

    result = apis.reissue_held_task_checkpoint("ent_t1", apply=True)

    assert "ent_same" not in retired
    assert record.checkpoints["ent_same"]["snapshot"]["status"] == "awaiting_operator"
    assert result.checkpoint_id == "ent_same"


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
        apis,
        "query_entities",
        lambda _t, *, snapshot_filters, cursor=None: pages[cursor],
    )

    assert list(apis.iter_held_task_ids()) == ["ent_a", "ent_b"]


def _run_cli(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["reissue_held_checkpoints.py", *argv])
    return cli.main()


def test_cli_dry_run_by_default_and_nonzero_on_failure(record, monkeypatch, capsys):
    record.add_task("ent_t1")
    record.persist = False
    assert _run_cli(monkeypatch, "--task", "ent_t1") == 0  # dry run never fails a write
    assert record.writes == 0

    assert _run_cli(monkeypatch, "--task", "ent_t1", "--apply") == 1
    out = capsys.readouterr().out
    assert '"outcome": "failed"' in out


def test_cli_read_failures_exit_nonzero_and_confirmed_skips_exit_zero(
    record, monkeypatch, capsys
):
    record.add_task("ent_done", status="done")
    assert _run_cli(monkeypatch, "--task", "ent_done", "--apply") == 0  # confirmed skip

    assert _run_cli(monkeypatch, "--task", "ent_missing") == 1  # dry run too
    out = capsys.readouterr().out
    assert '"task_id": "ent_missing"' in out and '"outcome": "failed"' in out

    record.add_task("ent_t1")
    monkeypatch.setattr(apis, "checkpoints_for_task", lambda _id: None)
    assert _run_cli(monkeypatch, "--task", "ent_t1", "--apply") == 1


def test_cli_partial_retirement_exits_nonzero_then_zero_after_rerun(
    record, monkeypatch, capsys
):
    record.add_task("ent_t1")
    record.add_checkpoint("ent_legacy", "ent_t1", signed=False)
    record.fail_supersede = {"ent_legacy"}

    assert _run_cli(monkeypatch, "--task", "ent_t1", "--apply") == 1
    first = capsys.readouterr().out
    assert '"outcome": "incomplete"' in first and "ent_legacy" in first

    record.fail_supersede = set()
    assert _run_cli(monkeypatch, "--task", "ent_t1", "--apply") == 0
    assert '"outcome": "retired_stale"' in capsys.readouterr().out
    assert (
        _awaiting(record)
        and record.checkpoints["ent_legacy"]["snapshot"]["status"] == "superseded"
    )


def test_cli_enumeration_failure_is_reported_and_nonzero_not_an_empty_run(
    monkeypatch, capsys
):
    monkeypatch.setattr(apis, "query_entities", lambda *a, **k: None)

    assert _run_cli(monkeypatch) == 1
    out = capsys.readouterr().out
    assert "enumeration" in out and '"failed": 1' in out


def test_cli_empty_successful_enumeration_is_distinguishable(monkeypatch, capsys):
    monkeypatch.setattr(
        apis, "query_entities", lambda *a, **k: {"entities": [], "next_cursor": None}
    )

    assert _run_cli(monkeypatch) == 0
    assert '"examined": 0' in capsys.readouterr().out
