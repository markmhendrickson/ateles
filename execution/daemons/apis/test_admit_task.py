"""One named existing task is admitted through the ordinary Apis path.

`admit_task.py` is the exact-ID first-admission entrance for a ready
PR-delivery task whose created event was never consumed, while the global
reconciler stays disabled. These tests drive it with a synthetic versioned
task store (the version-conditional write refuses a stale version with a
conflict, as Neotoma's ``expected_version`` does) and, for the effect test,
the real ``apis.dispatch_task`` in the shared stubbed dispatch world.

RED before the fix: the module did not exist, so every test failed at import
(no supported exact-task entrance; the reconciler exposes only a whole-table
sweep). Synthetic records only; no network.

Run: pytest execution/daemons/apis/test_admit_task.py -v
"""

from __future__ import annotations

import asyncio
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import admit_task as at  # noqa: E402
import apis  # noqa: E402
import task_reconciler  # noqa: E402
import test_producer_confidence_on_dispatch as _w  # noqa: E402
from lib.daemon_runtime import gating  # noqa: E402
from lib.daemon_runtime.task_lifecycle import FieldWrite, TaskStatus  # noqa: E402

world = _w.world

TASK = "ent_" + "a" * 24
OTHER = "ent_" + "b" * 24


def _ready(**over) -> dict:
    task = {
        "title": "Synthetic build",
        "status": "pending",
        "assigned_to": "cicada",
        "action_type": "open_or_merge_pr",
        "repository": "example-owner/example-repo",
        "user_id": "usr_test",
        "confidence": 0.95,
    }
    task.update(over)
    return task


class Store:
    """Versioned synthetic task store with conditional-write semantics."""

    def __init__(self, tasks: dict[str, dict]):
        self.tasks = {k: copy.deepcopy(v) for k, v in tasks.items()}
        self.versions = {k: 1 for k in tasks}
        self.reads: list[str] = []
        self.writes: list[tuple] = []
        self.checkpoint_reads: list[str] = []
        self.checkpoints: dict[str, list] = {}

    def read(self, task_id):
        self.reads.append(task_id)
        if task_id not in self.tasks:
            return None, None
        return copy.deepcopy(self.tasks[task_id]), f"v{self.versions[task_id]}"

    def field_if(self, task_id, field, value, *, expected_version, handler, key_suffix=""):
        if expected_version != f"v{self.versions.get(task_id)}":
            return FieldWrite(False, conflict=True)
        self.tasks[task_id][field] = value
        self.versions[task_id] += 1
        self.writes.append((task_id, field, value))
        return FieldWrite(True, version=f"v{self.versions[task_id]}")

    def list_checkpoints(self, task_id):
        self.checkpoint_reads.append(task_id)
        return list(self.checkpoints.get(task_id, []))


def _deps(store: Store, dispatched: list, *, fly=lambda s, t: False) -> at.AdmissionDeps:
    async def _dispatch(task_id, snapshot, trigger):
        dispatched.append((task_id, snapshot.get("status"), trigger))

    return at.AdmissionDeps(
        read_task=store.read,
        checkpoints=store.list_checkpoints,
        fly_refuses=fly,
        canonical_assignee=apis._canonical_assignee,
        dispatch_repo=apis._dispatch_repo_from_snapshot,
        conditional_status=store.field_if,
        dispatch=_dispatch,
    )


def _admit(store, dispatched, task_id=TASK, apply=True, **kw):
    return asyncio.run(at.admit(task_id, apply=apply, deps=_deps(store, dispatched, **kw)))


@pytest.fixture(autouse=True)
def _no_backlog_query(monkeypatch):
    """Any task-collection query fails the test."""
    def _boom(*_a, **_k):
        raise AssertionError("the backlog was queried")

    monkeypatch.setattr(task_reconciler, "_query_tasks", _boom)
    monkeypatch.setattr(gating, "query_all_entities", _boom, raising=False)


# ── preview and apply ───────────────────────────────────────────────────────


def test_dry_run_has_no_effects():
    store, dispatched = Store({TASK: _ready(), OTHER: _ready()}), []
    result = _admit(store, dispatched, apply=False)
    assert result.outcome == at.PREVIEW and result.reason == "eligible"
    assert store.writes == [] and dispatched == []
    assert set(store.reads) == {TASK}


def test_apply_claims_then_hands_to_dispatch_once():
    store, dispatched = Store({TASK: _ready(), OTHER: _ready()}), []
    result = _admit(store, dispatched)
    assert result.outcome == at.ADMITTED
    assert store.writes == [(TASK, "status", TaskStatus.ROUTED.value)]
    assert dispatched == [(TASK, "routed", "admit")]
    # The unrelated pending task was never read or changed.
    assert OTHER not in store.reads and OTHER not in store.checkpoint_reads
    assert store.tasks[OTHER] == _ready()


def test_replay_of_same_request_has_no_second_effect():
    store, dispatched = Store({TASK: _ready()}), []
    assert _admit(store, dispatched).outcome == at.ADMITTED
    again = _admit(store, dispatched)
    assert again.outcome == at.REFUSED and again.reason == "not_first_admission"
    assert len(store.writes) == 1 and len(dispatched) == 1


def test_concurrent_claim_loses_on_version_conflict():
    store, dispatched = Store({TASK: _ready()}), []
    real = store.field_if

    def _racing(task_id, *a, **kw):
        # Another caller commits first, between this caller's read and write.
        store.tasks[task_id]["status"] = "routed"
        store.versions[task_id] += 1
        return real(task_id, *a, **kw)

    store.field_if = _racing
    result = _admit(store, dispatched)
    assert result.outcome == at.CONFLICT
    assert dispatched == [] and store.writes == []


def test_interleaved_requests_produce_one_admission():
    store, dispatched = Store({TASK: _ready()}), []

    async def both():
        deps = _deps(store, dispatched)
        return await asyncio.gather(
            at.admit(TASK, apply=True, deps=deps), at.admit(TASK, apply=True, deps=deps)
        )

    results = asyncio.run(both())
    assert sorted(r.outcome for r in results) == [at.ADMITTED, at.REFUSED]
    assert len(dispatched) == 1 and len(store.writes) == 1


# ── fail closed ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "over,reason",
    [
        ({"status": "awaiting_approval"}, "not_first_admission"),
        ({"status": "failed"}, "not_first_admission"),
        ({"status": "done"}, "not_first_admission"),
        ({"status": "executing"}, "not_first_admission"),
        ({"status": ""}, "not_first_admission"),
        ({"blocked_reason": "held by a person"}, "held"),
        ({"disposition": "deferred_behind_build"}, "held_by_recovery"),
        ({"action_type": "operator_only"}, "not_pr_delivery"),
        ({"action_type": "local_edit"}, "not_pr_delivery"),
        ({"assigned_to": "pavo"}, "not_pr_delivery"),
        ({"owner": "someone-else"}, "foreign_owner"),
        ({"repository": "not a repo"}, "repository_invalid"),
        ({"user_id": None}, "tenant_unverified"),
    ],
)
def test_ineligible_task_refuses_with_no_effect(over, reason):
    store, dispatched = Store({TASK: _ready(**over)}), []
    result = _admit(store, dispatched)
    assert (result.outcome, result.reason) == (at.REFUSED, reason)
    assert store.writes == [] and dispatched == []


def test_missing_task_and_read_failure_refuse():
    store, dispatched = Store({}), []
    assert _admit(store, dispatched).reason == "task_unreadable"

    def _fail(_id):
        raise RuntimeError("secret detail")

    store.read = _fail
    result = _admit(store, dispatched)
    assert result.reason == "task_read_failed"
    assert "secret" not in json.dumps(result.to_dict())


def test_prior_checkpoint_or_unreadable_checkpoints_refuse():
    store, dispatched = Store({TASK: _ready()}), []
    store.checkpoints[TASK] = [{"entity_id": "ent_cp"}]
    assert _admit(store, dispatched).reason == "prior_checkpoint"
    store.list_checkpoints = lambda _t: None
    assert _admit(store, dispatched).reason == "checkpoint_read_failed"
    assert store.writes == [] and dispatched == []


def test_in_flight_or_unknown_delivery_refuses():
    store, dispatched = Store({TASK: _ready()}), []
    assert _admit(store, dispatched, fly=lambda s, t: True).reason == "delivery_in_flight_or_unknown"

    def _boom(s, t):
        raise OSError("ledger")

    assert _admit(store, dispatched, fly=_boom).reason == "delivery_ledger_unreadable"
    assert store.writes == [] and dispatched == []


def test_unconfirmed_claim_starts_nothing():
    store, dispatched = Store({TASK: _ready()}), []
    store.field_if = lambda *a, **k: FieldWrite(False)
    result = _admit(store, dispatched)
    assert (result.outcome, result.reason) == (at.UNKNOWN, "claim_write_unconfirmed")
    assert dispatched == []


@pytest.mark.parametrize("bad", ["", "ent_short", "../x", "ENT_" + "a" * 24, None])
def test_malformed_id_refuses_before_any_read(bad):
    store, dispatched = Store({TASK: _ready()}), []
    result = _admit(store, dispatched, task_id=bad)
    assert result.reason == "malformed_task_id"
    assert store.reads == []


def test_cli_malformed_invocation_touches_nothing(capsys, monkeypatch):
    monkeypatch.setattr(at, "default_deps", lambda *_a: pytest.fail("deps bound"))
    assert at.main(["--task", "nope"]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "malformed_task_id"


def test_reconciler_stays_disabled_by_default():
    assert task_reconciler.ENABLED is False


# ── effect: the ordinary gate holds a PR delivery at its checkpoint ─────────


def test_admitted_pr_task_reaches_ordinary_checkpoint_not_a_worker(world, monkeypatch):
    store = Store({TASK: _ready(), OTHER: _ready()})

    def _status(entity_id, status, **kw):
        world["statuses"].append((entity_id, status, kw.get("reason")))
        store.tasks[entity_id]["status"] = getattr(status, "value", status)
        return True

    monkeypatch.setattr(apis, "set_task_status", _status)
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda tid: store.read(tid)[0])
    monkeypatch.setattr(apis, "_fly_run_refuses_dispatch", lambda *a: False)
    monkeypatch.setattr(apis, "_fly_defer_holder", lambda *a: None)
    monkeypatch.setattr(gating, "fetch_task_snapshot_and_version", store.read)
    monkeypatch.setattr(gating, "checkpoints_for_task", lambda tid, strict=False: store.list_checkpoints(tid))
    monkeypatch.setattr(apis, "set_task_field_if_version", store.field_if)

    result = asyncio.run(
        at.admit(TASK, apply=True, deps=at.default_deps(_w._Notifier()))
    )

    assert result.outcome == at.ADMITTED
    # Ordinary gate path: routed, then held for an operator checkpoint.
    assert [s for _i, s, _r in world["statuses"]][-1] == TaskStatus.AWAITING_APPROVAL
    assert len(world["briefs"]) == 1 and world["briefs"][0]["task_entity_id"] == TASK
    # High blast: no worker started, no approval granted by this entrance.
    assert world["spawns"] == []
    assert result.status == TaskStatus.AWAITING_APPROVAL.value
    # The unrelated pending task was untouched.
    assert OTHER not in store.reads and store.tasks[OTHER] == _ready()
    # Replay: now held, so a second request is refused with nothing new.
    again = asyncio.run(at.admit(TASK, apply=True, deps=at.default_deps(_w._Notifier())))
    assert again.reason == "not_first_admission"
    assert len(world["briefs"]) == 1 and world["spawns"] == []
