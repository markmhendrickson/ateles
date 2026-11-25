"""One named existing task is admitted through the ordinary Apis path.

`admit_task.py` is the exact-ID first-admission entrance for a ready
PR-delivery task whose created event was never consumed, while the global
reconciler stays disabled. These tests drive it with a synthetic versioned
task store (the version-conditional write refuses a stale version with a
conflict, as Neotoma's ``expected_version`` does) and, for the effect tests,
the real ``apis.dispatch_task`` in the shared stubbed dispatch world, then the
real checkpoint-release consumer.

RED before the fix: the module did not exist, so every test failed at import
(no supported exact-task entrance; the reconciler exposes only a whole-table
sweep). Corrections after the first review (head eb831527) are pinned by the
tests named in their sections: an ``admitted`` that did not depend on the
read-back, state the CLI process cannot see (build registry, tracking runs,
a missing Fly ledger) treated as "nothing in flight", a tenant that was only
"not conflicting", no release-to-one-delivery proof, and a race test whose two
callers never actually overlapped. Synthetic records only; no network.

Run: pytest execution/daemons/apis/test_admit_task.py -v
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import admit_task as at  # noqa: E402
import apis  # noqa: E402
import build_delivery  # noqa: E402
import fly_runner  # noqa: E402
import task_reconciler  # noqa: E402
import task_watchdog as tw  # noqa: E402
import test_producer_confidence_on_dispatch as _w  # noqa: E402
from test_checkpoint_release import (  # noqa: E402,F401  (the shared release fixtures)
    CHECKPOINT_TYPE,
    TEST_OWNER,
    TEST_RESOLVER_JKT,
    _cicada_delivers_a_pr,
    _resolve,
    release_store,
)
from lib.daemon_runtime import gating  # noqa: E402
from lib.daemon_runtime.gating import (  # noqa: E402
    ExecutionPolicy,
    build_checkpoint_authorization_envelope,
)
from lib.daemon_runtime.task_lifecycle import FieldWrite, TaskStatus  # noqa: E402

world = _w.world

TASK = "ent_" + "a" * 24
OTHER = "ent_" + "b" * 24
REPO = "example-owner/example-repo"
TENANT = "usr_test"


def _ready(**over) -> dict:
    task = {
        "title": "Synthetic build",
        "status": "pending",
        "assigned_to": "cicada",
        "action_type": "open_or_merge_pr",
        "repository": REPO,
        "user_id": TENANT,
        "confidence": 0.95,
    }
    task.update(over)
    return task


def _policy(**over) -> ExecutionPolicy:
    kw = dict(
        entity_id="synthetic",
        high_blast_action_types=frozenset({"open_or_merge_pr"}),
        low_blast_action_types=frozenset(),
        loaded=True,
    )
    kw.update(over)
    return ExecutionPolicy(**kw)


def _empty_ledger(path: Path) -> Path:
    """A valid Fly ledger holding no reservation (the file exists and parses)."""
    ledger = fly_runner.FlyLedger(path)
    with ledger._locked():
        ledger._save(ledger._snapshot(ledger._clock()).document())
    return path


class Store:
    """Versioned synthetic task store with conditional-write semantics."""

    def __init__(self, tasks: dict[str, dict]):
        self.tasks = {k: copy.deepcopy(v) for k, v in tasks.items()}
        self.versions = {k: 1 for k in tasks}
        self.reads: list[str] = []
        self.writes: list[tuple] = []
        self.checkpoint_reads: list[str] = []
        self.checkpoints: dict[str, list] = {}
        self.no_version = False

    def read(self, task_id):
        self.reads.append(task_id)
        if task_id not in self.tasks:
            return None, None
        version = None if self.no_version else f"v{self.versions[task_id]}"
        return copy.deepcopy(self.tasks[task_id]), version

    def field_if(self, task_id, field, value, *, expected_version, handler, key_suffix=""):
        # None models an unconditional write: the thing the version condition
        # exists to prevent. (The real writer refuses to write without a token.)
        if expected_version is not None and expected_version != f"v{self.versions.get(task_id)}":
            return FieldWrite(False, conflict=True)
        self.tasks[task_id][field] = value
        self.versions[task_id] += 1
        self.writes.append((task_id, field, value))
        return FieldWrite(True, version=f"v{self.versions[task_id]}")

    def list_checkpoints(self, task_id):
        self.checkpoint_reads.append(task_id)
        return list(self.checkpoints.get(task_id, []))


class YieldingStore(Store):
    """A store whose reads suspend after capturing the state they return, so two
    callers can both read the same version before either one writes."""

    async def read(self, task_id):  # type: ignore[override]
        captured = Store.read(self, task_id)
        await asyncio.sleep(0)
        return captured


@pytest.fixture(autouse=True)
def _no_backlog_query(monkeypatch):
    """Any task-collection query fails the test."""
    def _boom(*_a, **_k):
        raise AssertionError("the backlog was queried")

    monkeypatch.setattr(task_reconciler, "_query_tasks", _boom)
    monkeypatch.setattr(gating, "query_all_entities", _boom, raising=False)


@pytest.fixture
def ledger_file(tmp_path):
    return _empty_ledger(tmp_path / "fly-ledger.json")


def _deps(store, dispatched: list, ledger_file, *, fly=lambda s, t: False,
          leaves=TaskStatus.AWAITING_APPROVAL.value, **over) -> at.AdmissionDeps:
    """Fakes for every reader and writer. The dispatch stub leaves the task in
    ``leaves`` (what the ordinary gate would write), or untouched when None."""

    async def _dispatch(task_id, snapshot, trigger):
        dispatched.append((task_id, snapshot.get("status"), trigger))
        if leaves is not None:
            store.tasks[task_id]["status"] = leaves

    deps = at.AdmissionDeps(
        read_task=store.read,
        checkpoints=store.list_checkpoints,
        fly_refuses=fly,
        canonical_assignee=apis._canonical_assignee,
        dispatch_repo=apis._dispatch_repo_from_snapshot,
        conditional_status=store.field_if,
        dispatch=_dispatch,
        tenant_owner=lambda tid: TENANT,
        policy=lambda assignee: _policy(),
        ledger_path=lambda: ledger_file,
        issue_key=build_delivery.issue_key,
    )
    return dataclasses.replace(deps, **over) if over else deps


def _admit(store, dispatched, ledger_file, task_id=TASK, apply=True, **kw):
    return asyncio.run(
        at.admit(task_id, apply=apply, deps=_deps(store, dispatched, ledger_file, **kw))
    )


def _refused_with_no_effect(result, reason, store, dispatched):
    assert (result.outcome, result.reason) == (at.REFUSED, reason)
    assert store.writes == [] and dispatched == []


# ── preview and apply ───────────────────────────────────────────────────────


def test_dry_run_has_no_effects(ledger_file):
    store, dispatched = Store({TASK: _ready(), OTHER: _ready()}), []
    result = _admit(store, dispatched, ledger_file, apply=False)
    assert result.outcome == at.PREVIEW and result.reason == "passes_first_admission_checks"
    assert result.ok
    assert store.writes == [] and dispatched == []
    assert set(store.reads) == {TASK}


def test_apply_claims_then_hands_to_dispatch_once(ledger_file):
    store, dispatched = Store({TASK: _ready(), OTHER: _ready()}), []
    result = _admit(store, dispatched, ledger_file)
    assert result.outcome == at.ADMITTED and result.ok
    assert store.writes == [(TASK, "status", TaskStatus.ROUTED.value)]
    assert dispatched == [(TASK, "routed", "admit")]
    # The unrelated pending task was never read or changed.
    assert OTHER not in store.reads and OTHER not in store.checkpoint_reads
    assert store.tasks[OTHER] == _ready()


def test_replay_of_same_request_has_no_second_effect(ledger_file):
    store, dispatched = Store({TASK: _ready()}), []
    assert _admit(store, dispatched, ledger_file).outcome == at.ADMITTED
    again = _admit(store, dispatched, ledger_file)
    # A replay is a refusal (exit 1), not an idempotent success, and says so.
    assert again.outcome == at.REFUSED and again.reason == "not_first_admission"
    assert not again.ok
    assert len(store.writes) == 1 and len(dispatched) == 1


def test_concurrent_claim_loses_on_version_conflict(ledger_file):
    store, dispatched = Store({TASK: _ready()}), []
    real = store.field_if

    def _racing(task_id, *a, **kw):
        # Another caller commits first, between this caller's read and write.
        store.tasks[task_id]["status"] = "routed"
        store.versions[task_id] += 1
        return real(task_id, *a, **kw)

    store.field_if = _racing
    result = _admit(store, dispatched, ledger_file)
    assert result.outcome == at.CONFLICT
    assert dispatched == [] and store.writes == []


def test_overlapping_callers_read_one_version_and_only_the_condition_separates_them(ledger_file):
    # Both callers read version v1 (each read suspends after capturing it), so
    # neither is refused for "not pending": the version condition on the write
    # is the only thing that stops the second. Without it both claim and both
    # dispatch (revert check: pass expected_version=None and this fails).
    store, dispatched = YieldingStore({TASK: _ready()}), []

    async def both():
        deps = _deps(store, dispatched, ledger_file)
        return await asyncio.gather(
            at.admit(TASK, apply=True, deps=deps), at.admit(TASK, apply=True, deps=deps)
        )

    results = asyncio.run(both())
    assert sorted(r.outcome for r in results) == [at.ADMITTED, at.CONFLICT]
    assert len(dispatched) == 1 and len(store.writes) == 1
    # Both reads happened before any write: the overlap was real.
    assert store.reads[:2] == [TASK, TASK]


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
    ],
)
def test_ineligible_task_refuses_with_no_effect(over, reason, ledger_file):
    store, dispatched = Store({TASK: _ready(**over)}), []
    _refused_with_no_effect(_admit(store, dispatched, ledger_file), reason, store, dispatched)


def test_missing_task_and_read_failure_refuse(ledger_file):
    store, dispatched = Store({}), []
    assert _admit(store, dispatched, ledger_file).reason == "task_unreadable"

    def _fail(_id):
        raise RuntimeError("secret detail")

    store.read = _fail
    result = _admit(store, dispatched, ledger_file)
    assert result.reason == "task_read_failed"
    assert "secret" not in json.dumps(result.to_dict())


def test_task_without_a_version_token_refuses(ledger_file):
    store, dispatched = Store({TASK: _ready()}), []
    store.no_version = True
    _refused_with_no_effect(
        _admit(store, dispatched, ledger_file), "task_version_unavailable", store, dispatched
    )


def test_prior_checkpoint_or_unreadable_checkpoints_refuse(ledger_file):
    store, dispatched = Store({TASK: _ready()}), []
    store.checkpoints[TASK] = [{"entity_id": "ent_cp"}]
    assert _admit(store, dispatched, ledger_file).reason == "prior_checkpoint"
    store.list_checkpoints = lambda _t: None
    assert _admit(store, dispatched, ledger_file).reason == "checkpoint_read_failed"
    assert store.writes == [] and dispatched == []


def test_checkpoint_reader_that_raises_refuses_like_one_that_returns_none(ledger_file):
    store, dispatched = Store({TASK: _ready()}), []

    def _raises(_t):
        raise ConnectionError("checkpoint store down")

    result = _admit(store, dispatched, ledger_file, checkpoints=_raises)
    _refused_with_no_effect(result, "checkpoint_read_failed", store, dispatched)
    assert "down" not in json.dumps(result.to_dict())


def test_in_flight_or_unknown_delivery_refuses(ledger_file):
    store, dispatched = Store({TASK: _ready()}), []
    assert (
        _admit(store, dispatched, ledger_file, fly=lambda s, t: True).reason
        == "delivery_in_flight_or_unknown"
    )

    def _boom(s, t):
        raise OSError("ledger")

    assert _admit(store, dispatched, ledger_file, fly=_boom).reason == "delivery_ledger_unreadable"
    assert store.writes == [] and dispatched == []


def _real_fly(snapshot, task_id):
    return apis._fly_run_refuses_dispatch(snapshot, task_id, "admit")


def test_real_fly_ledger_refuses_a_task_with_a_live_run(tmp_path, monkeypatch):
    # Through the real apis._fly_run_refuses_dispatch, not a stand-in: a live
    # receipt on this task's delivery key refuses, and so does an unparsable
    # ledger. (Revert check: stub the ledger check out and this admits.)
    ledger = tmp_path / "fly-ledger.json"
    monkeypatch.setenv("APIS_FLY_LEDGER_FILE", str(ledger))
    key = apis._fly_receipt_key(_ready(), TASK)
    assert fly_runner.FlyLedger(ledger).reserve_delivery(
        key, {"status": "launching", "branch": "b", "run_id": "", "token": "x"})
    store, dispatched = Store({TASK: _ready()}), []
    result = _admit(store, dispatched, ledger, fly=_real_fly)
    _refused_with_no_effect(result, "delivery_in_flight_or_unknown", store, dispatched)

    ledger.write_text("{not json")
    result = _admit(store, dispatched, ledger, fly=_real_fly)
    _refused_with_no_effect(result, "delivery_in_flight_or_unknown", store, dispatched)


def test_real_fly_ledger_without_a_run_lets_the_task_through(tmp_path, monkeypatch):
    ledger = _empty_ledger(tmp_path / "fly-ledger.json")
    monkeypatch.setenv("APIS_FLY_LEDGER_FILE", str(ledger))
    store, dispatched = Store({TASK: _ready()}), []
    assert _admit(store, dispatched, ledger, fly=_real_fly).outcome == at.ADMITTED


def test_unconfirmed_claim_starts_nothing(ledger_file):
    store, dispatched = Store({TASK: _ready()}), []
    store.field_if = lambda *a, **k: FieldWrite(False)
    result = _admit(store, dispatched, ledger_file)
    assert (result.outcome, result.reason) == (at.UNKNOWN, "claim_write_unconfirmed")
    assert not result.ok and dispatched == []


def test_claim_write_that_raises_is_unknown_not_refused(ledger_file):
    store, dispatched = Store({TASK: _ready()}), []

    def _raises(*a, **k):
        raise TimeoutError("write timed out")

    store.field_if = _raises
    result = _admit(store, dispatched, ledger_file)
    assert (result.outcome, result.reason) == (at.UNKNOWN, "claim_write_unconfirmed")
    assert dispatched == []


@pytest.mark.parametrize("readback", ["other_status", "raises", "missing"])
def test_claim_readback_that_does_not_show_routed_starts_nothing(readback, ledger_file):
    store, dispatched = Store({TASK: _ready()}), []
    real_read = store.read
    calls = {"n": 0}

    def _read(task_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_read(task_id)
        if readback == "raises":
            raise ConnectionError("read-back failed")
        if readback == "missing":
            return None, None
        snap, version = real_read(task_id)
        snap["status"] = "pending"  # the claim did not read back as routed
        return snap, version

    result = _admit(store, dispatched, ledger_file, read_task=_read)
    assert (result.outcome, result.reason) == (at.UNKNOWN, "claim_readback_failed")
    assert not result.ok and dispatched == []


def test_dispatch_that_raises_is_unknown_with_a_non_zero_result(ledger_file):
    store, dispatched = Store({TASK: _ready()}), []

    async def _raises(task_id, snapshot, trigger):
        raise RuntimeError("secret detail from the dispatcher")

    result = _admit(store, dispatched, ledger_file, dispatch=_raises)
    assert (result.outcome, result.reason) == (at.UNKNOWN, "dispatch_error")
    assert result.status == "routed" and not result.ok
    assert "secret" not in json.dumps(result.to_dict())


@pytest.mark.parametrize("bad", ["", "ent_short", "../x", "ENT_" + "a" * 24, None])
def test_malformed_id_refuses_before_any_read(bad, ledger_file):
    store, dispatched = Store({TASK: _ready()}), []
    result = _admit(store, dispatched, ledger_file, task_id=bad)
    assert result.reason == "malformed_task_id"
    assert store.reads == []


def test_unexpected_error_is_a_result_not_a_traceback(ledger_file):
    store, dispatched = Store({TASK: _ready()}), []

    def _raises(_assignee):
        raise RuntimeError("secret detail")

    result = _admit(store, dispatched, ledger_file, canonical_assignee=_raises)
    assert (result.outcome, result.reason) == (at.REFUSED, "internal_error")
    assert "secret" not in json.dumps(result.to_dict())


# ── the result depends on the read-back ─────────────────────────────────────


@pytest.mark.parametrize(
    "leaves,reason",
    [
        ("awaiting_approval", "held_at_plan_checkpoint"),
        ("awaiting_input", "readiness_hold_awaiting_input"),
        ("executing", "dispatched"),
        ("done", "dispatched"),
    ],
)
def test_admitted_only_for_a_state_the_ordinary_gate_writes(leaves, reason, ledger_file):
    store, dispatched = Store({TASK: _ready()}), []
    result = _admit(store, dispatched, ledger_file, leaves=leaves)
    assert (result.outcome, result.reason, result.status) == (at.ADMITTED, reason, leaves)
    assert result.ok


def test_dispatch_that_leaves_the_task_routed_is_stalled_and_non_zero(ledger_file):
    # RED before the fix: the entrance reported "admitted" (exit 0) whenever
    # dispatch did not raise, whatever it did to the task.
    store, dispatched = Store({TASK: _ready()}), []
    result = _admit(store, dispatched, ledger_file, leaves=None)
    assert (result.outcome, result.reason, result.status) == (
        at.STALLED, "dispatch_left_task_routed", "routed",
    )
    assert not result.ok and len(dispatched) == 1


def test_dispatch_that_returns_the_task_to_pending_is_stalled(ledger_file):
    store, dispatched = Store({TASK: _ready()}), []
    result = _admit(store, dispatched, ledger_file, leaves="pending")
    assert (result.outcome, result.reason) == (at.STALLED, "dispatch_returned_task_to_pending")
    assert not result.ok


def test_unreadable_final_state_is_unknown_not_admitted(ledger_file):
    store, dispatched = Store({TASK: _ready()}), []
    real_read = store.read
    calls = {"n": 0}

    def _read(task_id):
        calls["n"] += 1
        if calls["n"] >= 3:  # validation, claim read-back, then the final read
            raise ConnectionError("final read failed")
        return real_read(task_id)

    result = _admit(store, dispatched, ledger_file, read_task=_read)
    assert (result.outcome, result.reason, result.status) == (at.UNKNOWN, "final_read_failed", None)
    assert not result.ok


@pytest.mark.parametrize("leaves", ["failed", "blocked", "declined"])
def test_unexpected_final_state_is_unknown(leaves, ledger_file):
    store, dispatched = Store({TASK: _ready()}), []
    result = _admit(store, dispatched, ledger_file, leaves=leaves)
    assert (result.outcome, result.reason, result.status) == (
        at.UNKNOWN, "unexpected_status_after_dispatch", leaves,
    )
    assert not result.ok


# ── state this process cannot see: fail closed ──────────────────────────────


@pytest.mark.parametrize(
    "over",
    [
        {"issue_number": 42},
        {"description": f"Implements {REPO}#42 end to end."},
        {"body": f"See https://github.com/{REPO}/issues/42 for the report."},
    ],
)
def test_task_about_an_issue_is_refused_because_a_build_may_hold_it(over, ledger_file):
    # A build registry entry lives only in the Apis daemon; this process always
    # sees it empty, so a task whose issue key resolves cannot be shown free of
    # a build. RED before the fix: it was admitted and held at a new checkpoint.
    assert build_delivery.active_build(REPO, 42) is None  # this process sees nothing
    store, dispatched = Store({TASK: _ready(**over)}), []
    result = _admit(store, dispatched, ledger_file)
    _refused_with_no_effect(result, "issue_build_state_unobservable", store, dispatched)


def test_issue_key_that_cannot_be_derived_refuses(ledger_file):
    store, dispatched = Store({TASK: _ready()}), []

    def _raises(snapshot, repository):
        raise ValueError("malformed")

    result = _admit(store, dispatched, ledger_file, issue_key=_raises)
    _refused_with_no_effect(result, "issue_build_state_unobservable", store, dispatched)


@pytest.mark.parametrize("field", ["description", "body", "notes", "title"])
def test_a_builds_own_tracking_task_is_refused(field, ledger_file):
    # The tracking-run table is in-process too; the marker in the text is what
    # a separate process can see. RED before the fix: it was moved to routed.
    assert build_delivery.tracking_run_id({field: "build-run: " + "0" * 16}) is None
    store, dispatched = Store({TASK: _ready(**{field: "build-run: " + "0" * 16})}), []
    result = _admit(store, dispatched, ledger_file)
    _refused_with_no_effect(result, "build_tracking_task", store, dispatched)


def test_missing_fly_ledger_file_refuses_instead_of_reading_as_empty(tmp_path):
    # A missing ledger file reads as an empty ledger, so the in-flight check
    # would pass vacuously (another HOME or APIS_FLY_LEDGER_FILE than the
    # daemon's). RED before the fix: it was admitted.
    missing = tmp_path / "no-such-ledger.json"
    store, dispatched = Store({TASK: _ready()}), []
    result = _admit(store, dispatched, missing)
    _refused_with_no_effect(result, "fly_ledger_unavailable", store, dispatched)
    assert not missing.exists()  # the dry check created nothing


def test_unreadable_or_unresolvable_fly_ledger_refuses(tmp_path):
    store, dispatched = Store({TASK: _ready()}), []
    directory = tmp_path / "a-directory"
    directory.mkdir()
    _refused_with_no_effect(
        _admit(store, dispatched, directory), "fly_ledger_unavailable", store, dispatched
    )

    def _raises():
        raise RuntimeError("config unreadable")

    result = _admit(store, dispatched, directory, ledger_path=_raises)
    _refused_with_no_effect(result, "fly_ledger_unavailable", store, dispatched)


def test_default_ledger_path_is_the_one_the_daemon_resolves(tmp_path, monkeypatch):
    monkeypatch.setenv("APIS_FLY_LEDGER_FILE", str(tmp_path / "elsewhere.json"))
    deps = at.default_deps()
    assert Path(deps.ledger_path()) == Path(build_delivery._delivery_ledger().path)


# ── tenant and authority: fail closed ───────────────────────────────────────


def test_missing_tenant_key_refuses(ledger_file):
    task = _ready()
    del task["user_id"]
    store, dispatched = Store({TASK: task}), []
    _refused_with_no_effect(
        _admit(store, dispatched, ledger_file), "tenant_unverified", store, dispatched
    )


@pytest.mark.parametrize("falsy", [None, "", 0, False])
def test_falsy_tenant_refuses(falsy, ledger_file):
    store, dispatched = Store({TASK: _ready(user_id=falsy)}), []
    _refused_with_no_effect(
        _admit(store, dispatched, ledger_file), "tenant_unverified", store, dispatched
    )


def test_tenant_that_differs_from_the_one_apis_derives_refuses(ledger_file):
    store, dispatched = Store({TASK: _ready(user_id="usr_other")}), []
    result = _admit(store, dispatched, ledger_file)
    _refused_with_no_effect(result, "tenant_mismatch", store, dispatched)


@pytest.mark.parametrize(
    "owner",
    [lambda t: None, lambda t: "", lambda t: 123, lambda t: {"id": TENANT}],
    ids=["none", "empty", "not-a-string", "malformed"],
)
def test_tenant_that_apis_cannot_establish_refuses(owner, ledger_file):
    store, dispatched = Store({TASK: _ready()}), []
    result = _admit(store, dispatched, ledger_file, tenant_owner=owner)
    _refused_with_no_effect(result, "tenant_unverified", store, dispatched)


def test_tenant_reader_that_raises_refuses(ledger_file):
    store, dispatched = Store({TASK: _ready()}), []

    def _raises(task_id):
        raise ConnectionError("ownership read failed")

    result = _admit(store, dispatched, ledger_file, tenant_owner=_raises)
    _refused_with_no_effect(result, "tenant_unverified", store, dispatched)


def test_unreadable_or_malformed_policy_refuses(ledger_file):
    store, dispatched = Store({TASK: _ready()}), []

    def _raises(_assignee):
        raise ConnectionError("policy read failed")

    cases = [
        _raises,
        lambda a: None,
        lambda a: SimpleNamespace(loaded=True),  # no classifier
        lambda a: ExecutionPolicy(entity_id="fallback", loaded=False),  # unreachable fallback
    ]
    for policy in cases:
        result = _admit(store, dispatched, ledger_file, policy=policy)
        _refused_with_no_effect(result, "policy_unreadable", store, dispatched)


def test_policy_that_would_not_hold_the_action_for_approval_refuses(ledger_file):
    store, dispatched = Store({TASK: _ready()}), []
    low = _policy(
        high_blast_action_types=frozenset(), low_blast_action_types=frozenset({"open_or_merge_pr"})
    )
    result = _admit(store, dispatched, ledger_file, policy=lambda a: low)
    _refused_with_no_effect(result, "policy_not_checkpointed", store, dispatched)


# ── the CLI ─────────────────────────────────────────────────────────────────


def test_cli_malformed_invocation_touches_nothing(capsys, monkeypatch):
    monkeypatch.setattr(at, "default_deps", lambda *_a: pytest.fail("deps bound"))
    assert at.main(["--task", "nope"]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "malformed_task_id"


def test_cli_preview_exits_zero_and_never_builds_a_notifier(capsys, monkeypatch, ledger_file):
    import red_conditions

    monkeypatch.setattr(
        red_conditions.ApisNotifier, "from_neotoma",
        classmethod(lambda cls: pytest.fail("a notifier was built for a dry run")),
    )
    store, dispatched = Store({TASK: _ready()}), []
    seen = []

    def _default_deps(notifier=None):
        seen.append(notifier)
        return _deps(store, dispatched, ledger_file)

    monkeypatch.setattr(at, "default_deps", _default_deps)
    assert at.main(["--task", TASK]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["outcome"], out["reason"]) == ("preview", "passes_first_admission_checks")
    assert seen == [None] and store.writes == [] and dispatched == []


def test_cli_apply_binds_the_apis_notifier_and_exits_by_the_result(capsys, monkeypatch, ledger_file):
    import red_conditions

    sentinel = object()
    monkeypatch.setattr(
        red_conditions.ApisNotifier, "from_neotoma", classmethod(lambda cls: sentinel)
    )
    store, dispatched = Store({TASK: _ready()}), []
    seen = []

    def _default_deps(notifier=None):
        seen.append(notifier)
        return _deps(store, dispatched, ledger_file)

    monkeypatch.setattr(at, "default_deps", _default_deps)
    assert at.main(["--task", TASK, "--apply"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["outcome"], out["status"]) == ("admitted", "awaiting_approval")
    assert seen == [sentinel] and len(dispatched) == 1

    # A stalled result exits non-zero through the CLI too.
    store, dispatched = Store({TASK: _ready()}), []
    monkeypatch.setattr(
        at, "default_deps", lambda notifier=None: _deps(store, dispatched, ledger_file, leaves=None)
    )
    assert at.main(["--task", TASK, "--apply"]) == 1
    assert json.loads(capsys.readouterr().out)["outcome"] == "stalled"


def test_cli_setup_failure_is_a_refusal_not_a_traceback(capsys, monkeypatch):
    import red_conditions

    def _raises(cls):
        raise RuntimeError("secret environment detail")

    monkeypatch.setattr(red_conditions.ApisNotifier, "from_neotoma", classmethod(_raises))
    assert at.main(["--task", TASK, "--apply"]) == 1
    out = capsys.readouterr().out
    assert json.loads(out) == {
        "outcome": "refused", "reason": "setup_failed", "status": None, "task_id": TASK,
    }
    assert "secret" not in out

    monkeypatch.setattr(at, "default_deps", lambda *_a: _raises(None))
    assert at.main(["--task", TASK]) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "setup_failed"


def test_reconciler_stays_disabled_by_default():
    assert task_reconciler.ENABLED is False


# ── what the watchdog does with a routed task that has no run ───────────────


def test_a_stalled_routed_task_is_picked_up_by_the_watchdog(monkeypatch):
    wd = tw.TaskWatchdog(stall_seconds=3600)
    assert wd.classify(TASK, "routed", 3599) == tw.WatchdogAction.NONE
    assert wd.classify(TASK, "routed", 3600) == tw.WatchdogAction.RETRY
    wd._state[TASK] = tw._AttemptState(attempts=tw.MAX_ATTEMPTS)
    assert wd.classify(TASK, "routed", 3600) == tw.WatchdogAction.ESCALATE

    wd = tw.TaskWatchdog(stall_seconds=3600)
    monkeypatch.setattr(tw, "_query_tasks", lambda limit: [(TASK, {"status": "routed", "updated_at": 0})])
    writes, dispatched = [], []
    monkeypatch.setattr(tw, "set_task_status", lambda *a, **k: writes.append((a, k)))

    async def dispatch_fn(task_id, snapshot, trigger):
        dispatched.append((task_id, trigger))

    asyncio.run(wd.sweep(object(), dispatch_fn))
    # Re-opened to routed and re-dispatched through the ordinary gates.
    assert dispatched == [(TASK, "watchdog_retry")]
    assert writes[0][0][1] == TaskStatus.ROUTED


# ── effect: the real dispatch, early returns after the claim ────────────────


def _real_world(world, monkeypatch, tmp_path, store):
    """The shared stubbed dispatch world over a versioned store; returns deps
    bound to the real ``apis`` helpers (built BEFORE a test patches anything
    only the dispatch should see)."""
    ledger = _empty_ledger(tmp_path / "fly-ledger.json")
    monkeypatch.setenv("APIS_FLY_LEDGER_FILE", str(ledger))

    def _status(entity_id, status, **kw):
        world["statuses"].append((entity_id, status, kw.get("reason")))
        store.tasks[entity_id]["status"] = getattr(status, "value", status)
        return True

    monkeypatch.setattr(apis, "set_task_status", _status)
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda tid: store.read(tid)[0])
    monkeypatch.setattr(apis, "resolve_policy_for_agent", lambda skill: _policy())
    monkeypatch.setattr(gating, "fetch_task_snapshot_and_version", store.read)
    monkeypatch.setattr(
        gating, "checkpoints_for_task", lambda tid, strict=False: store.list_checkpoints(tid)
    )
    monkeypatch.setattr(apis, "set_task_field_if_version", store.field_if)
    return at.default_deps(_w._Notifier())


def test_admitted_pr_task_reaches_ordinary_checkpoint_not_a_worker(world, monkeypatch, tmp_path):
    store = Store({TASK: _ready(), OTHER: _ready()})
    deps = _real_world(world, monkeypatch, tmp_path, store)

    result = asyncio.run(at.admit(TASK, apply=True, deps=deps))

    assert result.outcome == at.ADMITTED and result.reason == "held_at_plan_checkpoint"
    # Ordinary gate path: routed, then held for an operator checkpoint.
    assert [s for _i, s, _r in world["statuses"]][-1] == TaskStatus.AWAITING_APPROVAL
    assert len(world["briefs"]) == 1 and world["briefs"][0]["task_entity_id"] == TASK
    # High blast: no worker started, no approval granted by this entrance.
    assert world["spawns"] == []
    assert result.status == TaskStatus.AWAITING_APPROVAL.value
    # The unrelated pending task was untouched.
    assert OTHER not in store.reads and store.tasks[OTHER] == _ready()
    # Replay: now held, so a second request is refused with nothing new.
    again = asyncio.run(at.admit(TASK, apply=True, deps=deps))
    assert again.reason == "not_first_admission"
    assert len(world["briefs"]) == 1 and world["spawns"] == []


def _claim(number=7):
    return build_delivery.BuildClaim(
        repository=REPO, number=number, holder="task:ent_" + "c" * 24, run_id="d" * 16
    )


def _stalls(world, monkeypatch, tmp_path, *, patch, task=None, expect=at.STALLED):
    store = Store({TASK: task or _ready()})
    deps = _real_world(world, monkeypatch, tmp_path, store)
    patch(monkeypatch, store)  # after the deps exist: only the dispatch sees it
    result = asyncio.run(at.admit(TASK, apply=True, deps=deps))
    assert result.outcome == expect, result
    assert not result.ok
    # Nothing was held, briefed or spawned: the claim is the only effect.
    assert world["briefs"] == [] and world["spawns"] == []
    return result, store


def test_fly_defer_holder_return_is_stalled_not_admitted(world, monkeypatch, tmp_path):
    def patch(mp, store):
        mp.setattr(apis, "_fly_defer_holder", lambda snap, tid: _claim())

    result, store = _stalls(world, monkeypatch, tmp_path, patch=patch)
    assert (result.reason, result.status) == ("dispatch_left_task_routed", "routed")
    assert store.writes == [(TASK, "status", "routed")]


@pytest.mark.parametrize("deferral_state", ["routed", "pending"])
def test_build_holder_deferral_after_the_claim_is_stalled_not_admitted(
    deferral_state, world, monkeypatch, tmp_path
):
    deferred = []

    async def _record_deferral(entity_id, snapshot, holder, notifier):
        deferred.append(entity_id)
        store_ref["store"].tasks[entity_id]["status"] = deferral_state
        return SimpleNamespace(sweep_owns_it=False)

    store_ref = {}

    def patch(mp, store):
        store_ref["store"] = store
        # Only the dispatch resolves a key and a holder; the entrance's own
        # check uses the key function it was built with (no key: nothing it can see).
        mp.setattr(build_delivery, "issue_key", lambda snap, repo: (REPO, 7))
        mp.setattr(build_delivery, "active_build", lambda repo, number: _claim(number))
        mp.setattr(apis, "_record_deferral", _record_deferral)

    result, _ = _stalls(world, monkeypatch, tmp_path, patch=patch)
    assert deferred == [TASK]
    assert result.status == deferral_state
    assert result.reason == (
        "dispatch_left_task_routed" if deferral_state == "routed"
        else "dispatch_returned_task_to_pending"
    )


def test_tracking_run_return_is_stalled_not_admitted(world, monkeypatch, tmp_path):
    def patch(mp, store):
        mp.setattr(build_delivery, "tracking_run_id", lambda snap: "e" * 16)

    result, _ = _stalls(world, monkeypatch, tmp_path, patch=patch)
    assert result.reason == "dispatch_left_task_routed"


def test_detached_run_return_is_stalled_not_admitted(world, monkeypatch, tmp_path):
    def patch(mp, store):
        mp.setattr(apis, "_detached_runs_in_progress", {TASK})

    result, _ = _stalls(world, monkeypatch, tmp_path, patch=patch)
    assert result.reason == "dispatch_left_task_routed"


def test_status_changed_return_is_stalled_not_admitted(world, monkeypatch, tmp_path):
    async def _changed(entity_id, expected, step):
        return False

    def patch(mp, store):
        mp.setattr(apis, "_task_status_still", _changed)

    result, _ = _stalls(world, monkeypatch, tmp_path, patch=patch)
    assert result.reason == "dispatch_left_task_routed"


def _under_specified(monkeypatch, world):
    monkeypatch.setattr(apis, "READINESS_GATE", True)
    monkeypatch.setattr(apis, "RUN_EMAIL", False)
    monkeypatch.setattr(apis, "write_assessment", lambda *a, **k: None)


def test_readiness_hold_is_admitted_as_awaiting_input_not_as_a_worker(world, monkeypatch, tmp_path):
    _under_specified(monkeypatch, world)
    store = Store({TASK: _ready(title="x")})
    deps = _real_world(world, monkeypatch, tmp_path, store)
    result = asyncio.run(at.admit(TASK, apply=True, deps=deps))
    assert (result.outcome, result.reason, result.status) == (
        at.ADMITTED, "readiness_hold_awaiting_input", "awaiting_input",
    )
    assert world["briefs"] == [] and world["spawns"] == []


def test_readiness_write_that_loses_a_status_race_is_stalled(world, monkeypatch, tmp_path):
    _under_specified(monkeypatch, world)

    async def _changed_at_readiness(entity_id, expected, step):
        return step != "readiness write"

    def patch(mp, store):
        mp.setattr(apis, "_task_status_still", _changed_at_readiness)

    result, _ = _stalls(
        world, monkeypatch, tmp_path, patch=patch, task=_ready(title="x")
    )
    assert (result.reason, result.status) == ("dispatch_left_task_routed", "routed")


# ── effect: admission -> checkpoint -> release -> exactly one delivery ──────


@pytest.mark.asyncio
async def test_exact_id_admission_then_release_delivers_exactly_once(
    monkeypatch, release_store, tmp_path  # noqa: F811 -- pytest fixture
):
    """exact-ID request -> ordinary gate -> PLAN checkpoint -> existing qualified
    simulated release -> exactly ONE simulated delivery on the same task id.

    The release goes through ``server._resolve_checkpoint`` and the real
    ``handle_checkpoint_brief`` consumer, with the release fixtures of
    ``test_checkpoint_release`` (a qualified, attributed simulated approval);
    only the spawn itself is simulated. The unrelated task is never touched.
    """
    records, brief_id, old_task_id = release_store
    task_id = TASK  # the fixture's own id is not a canonical entity id
    records[task_id] = {**records.pop(old_task_id), "entity_id": task_id}
    records["_observations"][task_id] = records["_observations"].pop(old_task_id)
    other_id = OTHER
    other = {"title": "Unrelated", "status": "pending", "assigned_to": "cicada",
             "action_type": "open_or_merge_pr", "repository": REPO}
    records[other_id] = {"entity_id": other_id, "entity_type": "task",
                         "entity_version": "v1", "snapshot": copy.deepcopy(other)}
    records["_observations"][other_id] = [{"id": "obs-other", "user_id": TEST_OWNER}]

    policy = _policy(entity_id="default")
    # The fixture's simulated Cicada run names a PR in this repository.
    ready = _ready(user_id=TEST_OWNER, status="pending", repository="markmhendrickson/ateles")
    ready.pop("title")
    task_snapshot = records[task_id]["snapshot"]
    task_snapshot.clear()
    task_snapshot.update(ready, title="Implement the bounded change")
    records[task_id]["entity_version"] = "v1"

    ledger = _empty_ledger(tmp_path / "fly-ledger.json")
    monkeypatch.setenv("APIS_FLY_LEDGER_FILE", str(ledger))
    monkeypatch.setattr(apis, "resolve_policy_for_agent", lambda skill: policy)
    monkeypatch.setattr(apis, "READINESS_GATE", False)
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", False)
    monkeypatch.setattr(apis, "RUN_EMAIL", False)

    class _Job:  # the fixture's job stub knows only finished(); the gate also escalates
        def __getattr__(self, _name):
            return lambda *a, **k: None

    monkeypatch.setattr(apis._activity, "started", lambda message: _Job())

    reads: list[str] = []

    def read_task(tid):
        reads.append(tid)
        record = records.get(tid)
        if record is None or record["entity_type"] != "task":
            return None, None
        return {**copy.deepcopy(record["snapshot"]), "user_id": TEST_OWNER}, record["entity_version"]

    def field_if(tid, field, value, *, expected_version, handler, key_suffix=""):
        record = records[tid]
        if expected_version != record["entity_version"]:
            return FieldWrite(False, conflict=True)
        record["snapshot"][field] = value
        record["entity_version"] = f"v{int(record['entity_version'][1:]) + 1}"
        return FieldWrite(True, version=record["entity_version"])

    monkeypatch.setattr(gating, "fetch_task_snapshot_and_version", read_task)
    monkeypatch.setattr(gating, "checkpoints_for_task", lambda tid, strict=False: [])
    monkeypatch.setattr(apis, "set_task_field_if_version", field_if)

    # The hold files a real authenticated checkpoint (envelope bound to the
    # held task record and the policy), as the ordinary gate does.
    def write_brief(**kw):
        encoded = build_checkpoint_authorization_envelope(
            task_record=kw["task_record"], policy=kw["policy"], decision=kw["decision"],
            action_type=kw["action_type"], user_id=kw["user_id"],
            required_approver_jkt=TEST_RESOLVER_JKT,
        )
        authority = json.loads(encoded)
        records[brief_id] = {
            "entity_id": brief_id, "entity_type": CHECKPOINT_TYPE, "observation_count": 1,
            "last_observation_at": "2026-09-21T00:00:00Z", "entity_version": "v1",
            "snapshot": {
                "status": "awaiting_operator", "resolved_dispatched": False,
                "task_entity_id": kw["task_entity_id"], "gate_action": kw["decision"].action.value,
                "blast_radius": kw["decision"].blast_radius.value,
                "policy_entity_id": kw["policy"].entity_id, "title": kw["title"], "body": encoded,
            },
        }
        monkeypatch.setattr(
            apis, "read_authenticated_checkpoint_authorization",
            lambda checkpoint_id, record: dict(authority)
            if (record.get("snapshot") or {}).get("body") == encoded else None,
        )
        return brief_id

    monkeypatch.setattr(apis, "write_checkpoint_brief", write_brief)

    spawns: list[tuple] = []
    _Success = _cicada_delivers_a_pr(monkeypatch, records)

    async def spawn(skill, entity_id, *a, **kw):
        spawns.append((skill, entity_id))
        return _Success()

    monkeypatch.setattr(apis, "DRY_RUN", False)
    monkeypatch.setattr(apis, "_spawn_harness_skill", spawn)

    deps = at.default_deps(_w._Notifier())
    result = await at.admit(task_id, apply=True, deps=deps)

    # Admission: the ordinary gate held it at a PLAN checkpoint. No worker yet.
    assert (result.outcome, result.reason, result.status) == (
        at.ADMITTED, "held_at_plan_checkpoint", "awaiting_approval",
    )
    assert records[task_id]["snapshot"]["status"] == "awaiting_approval"
    assert records[brief_id]["snapshot"]["status"] == "awaiting_operator"
    assert spawns == []

    # Release through the existing consumer: exactly one delivery, same id.
    released = await _resolve(brief_id, "approve")
    assert "re-dispatched" in released["action_taken"]
    background = list(getattr(apis, "_background_dispatch_tasks", ()))
    if background:
        await asyncio.gather(*background)
    assert spawns == [("cicada", task_id)]
    assert records[task_id]["snapshot"]["status"] == "done"

    # The unrelated pending task was never read, written or delivered.
    assert other_id not in reads
    assert records[other_id]["snapshot"] == other and records[other_id]["entity_version"] == "v1"

    # Replays after release produce no second delivery: the same approval, the
    # consumer's own replay, and a second admission request.
    replay = await _resolve(brief_id, "approve")
    assert "re-dispatched" not in replay.get("action_taken", "")
    await apis.handle_checkpoint_brief(brief_id, records[brief_id]["snapshot"], _w._Notifier())
    again = await at.admit(task_id, apply=True, deps=deps)
    assert (again.outcome, again.reason) == (at.REFUSED, "not_first_admission")
    assert spawns == [("cicada", task_id)]
    assert other_id not in reads
