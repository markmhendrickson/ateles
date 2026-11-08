"""A Fly delivery task cut off by an Apis restart before launch is recovered once.

Seen on 2026-11-06: a checkpoint was approved, Apis logged the dispatch, and the
automatic redeploy restarted Apis 12 s later. The task was already EXECUTING but
its Fly delivery receipt was not yet reserved, so no run launched, the ledger
stayed empty and nothing ever picked the task up again.

``recover_unlaunched_fly_tasks`` runs at startup and re-dispatches exactly such
tasks. These tests drive the real ``_spawn_harness_skill`` → ``run_fly_delivery``
path with only ``run_skill`` (the Fly launch) faked, so a launch is counted where
it would really happen.

What these looked like RED on the head before the change (``apis`` had no
recovery sweep): every test errored or failed with

    AttributeError: module 'apis' has no attribute '_consumed_approval'

With the sweep present but its receipt check removed, four failed: the second
sweep in ``test_restart_between_dispatch_and_launch_launches_exactly_once``
dispatched the task again (``assert len(dispatched) == 1``), and the in-flight,
``launching`` and settled-receipt cases dispatched a task whose run was already
recorded (``assert launches == [] and dispatched == []``), which would move a
running task back to routed. The existing reservation still refused the second
launch itself, which is why the concurrent case passes either way.

Without the audit-file check, ``test_cleared_receipt_is_named_not_redispatched``
launched the task again (``assert (['ent_task_restart_before_launch'] == []``).
Taking any consumed approval instead of the newest checkpoint made
``test_consumed_approval_refuses_an_older_approval_behind_a_newer_checkpoint``
read the older approval for release (``Failed: a non-releasing checkpoint was
read for release``).

Correction round (PR #1684 review). How the added tests looked red:

* ``test_unreadable_ledger_is_named_not_left_silent``, on 649910f: the ledger
  read raised and the sweep counted the task "recorded" with no notice
  (``assert len(notifier.sent) == 1`` saw ``0``).
* ``test_cleared_receipt_notice_does_not_claim_it_never_launched``, on 649910f:
  the notice read "its Fly run never launched".
* ``test_task_with_only_the_executing_handoff_since_approval_is_recovered``, on
  649910f: the old check resolved the policy through the unfaked lookup and
  never released (``assert launches == [TASK]`` saw ``[]``).
* ``test_task_changed_after_approval_is_named_not_released`` and
  ``test_mismatched_authorization_binding_is_not_recovered`` pass on 649910f
  only because its unfaked policy lookup refuses everything, so they were
  checked by mutation instead: with the later-observation refusal removed,
  all four changed-task cases dispatched (``assert launches == []`` failed);
  with the ``user_id``, ``gate_action`` and ``blast_radius`` comparisons
  removed from ``_approval_binding_holds``, those three cases returned the
  checkpoint (``assert checkpoint is None`` failed).

Round 3 (PR #1684). ``test_unreadable_audit_file_is_held_and_named`` passes on
7a02329 and was checked by mutation: with the ``None`` branch of
``_fly_receipt_removed`` made to return without a notice, it failed
``assert len(notifier.sent) == 1`` (``0 == 1``); with ``None`` treated as "no
removal", the task was launched again (``assert launches == []`` failed).

Rebuild onto main (scope cut: every unrecovered notice says launch is
unknown). Against #1684's ``apis.py`` the updated file failed 12 tests:
``_Notifier`` now refuses any notice containing "never launched", so every
path that used #1684's "its Fly run never launched" wording failed, including
``test_failed_reread_after_a_removed_receipt_says_launch_is_unknown`` (the
re-read failure fell through to "examining it for recovery failed" after the
refused notice: ``assert 'could not be re-read' in ...`` failed), the four
``test_task_changed_after_approval_is_named_not_released`` cases, and all
seven ``test_every_unrecovered_notice_says_launch_is_unknown`` reasons.

Run: timeout 300 python3 -m pytest -q execution/daemons/apis/test_fly_restart_before_launch.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
import build_delivery  # noqa: E402
import fly_runner  # noqa: E402
from skill_runner import SkillResult  # noqa: E402

REPO = "owner/repo"
TASK = "ent_task_restart_before_launch"
CHECKPOINT = "ent_checkpoint_approved"


class _Notifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message, *args, **kwargs):
        # No notice anywhere in the recovery path may claim a run never
        # launched: the sweep cannot always know, so every notice says unknown.
        assert "never launched" not in str(message), message
        self.sent.append(str(message))


def _task(**over) -> dict:
    snap = {
        "title": "Build the thing",
        "status": "executing",
        "repo": REPO,
        "assigned_to": "cicada",
        "action_type": "open_or_merge_pr",
    }
    snap.update(over)
    return snap


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fly opted in, an empty ledger, Neotoma reads faked, and a dispatch that
    goes through the real spawn path with the Fly launch counted."""
    ledger = tmp_path / "fly-ledger.json"
    monkeypatch.setenv("APIS_FLY_LEDGER_FILE", str(ledger))
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")
    monkeypatch.setattr(apis, "_tasks_in_dispatch", {})
    monkeypatch.setattr(apis, "_detached_runs_in_progress", set())

    state = {"task": _task(), "approval": (CHECKPOINT, "")}
    launches: list[str] = []
    dispatched: list[dict] = []
    writes: list[tuple] = []

    monkeypatch.setattr(apis, "query_all_entities",
                        lambda *a, **k: [{"entity_id": TASK, "snapshot": dict(state["task"])}])
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda entity_id: dict(state["task"]))
    monkeypatch.setattr(apis, "_consumed_approval", lambda *a: state["approval"])
    monkeypatch.setattr(apis, "set_task_status", lambda *a, **k: writes.append((a, k)) or True)

    async def _run_skill(skill, prompt, **kwargs):
        assert kwargs.get("runner") == "fly"
        launches.append(kwargs["task_entity_id"])
        await asyncio.sleep(0)
        return SkillResult(skill, False, 0, "", "", fly_delivery_status="no_changes",
                           remote_run_id=f"run-{len(launches)}")

    async def _dispatch(entity_id, snapshot, trigger, notifier, **kwargs):
        dispatched.append({"trigger": trigger, **kwargs})
        return await apis._spawn_harness_skill("cicada", entity_id, snapshot, trigger, notifier)

    monkeypatch.setattr(apis, "run_skill", _run_skill)
    monkeypatch.setattr(apis, "dispatch_task", _dispatch)
    return ledger, state, launches, dispatched, writes


def test_restart_between_dispatch_and_launch_launches_exactly_once(env):
    ledger, _, launches, dispatched, _ = env
    notifier = _Notifier()
    # The process that dispatched died before reserving: EXECUTING, empty ledger.
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    # A further restart (or a replayed sweep) must not run it again.
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert launches == [TASK], f"expected exactly one Fly run, got {launches}"
    assert len(dispatched) == 1
    assert dispatched[0]["trigger"] == "restart_recovery"
    assert dispatched[0]["gate_override"] is True
    assert dispatched[0]["release_key"].startswith(CHECKPOINT)
    receipt = build_delivery._receipt_for(
        build_delivery.fly_delivery_key(REPO, None, TASK),
        fly_runner.FlyLedger(ledger).delivery_receipts())
    assert receipt and receipt["run_id"] == "run-1" and receipt["task_id"] == TASK
    assert notifier.sent == []


def test_concurrent_recovery_still_launches_exactly_once(env):
    _, _, launches, _, _ = env

    async def both():
        await asyncio.gather(apis.recover_unlaunched_fly_tasks(_Notifier()),
                             apis.recover_unlaunched_fly_tasks(_Notifier()))

    asyncio.run(both())
    assert launches == [TASK], f"expected exactly one Fly run, got {launches}"


def test_run_in_flight_is_not_dispatched_again(env):
    ledger, _, launches, dispatched, writes = env
    fly = fly_runner.FlyLedger(ledger)
    key = build_delivery.fly_delivery_key(REPO, None, TASK)
    assert fly.reserve_delivery(key, {"status": "launched", "branch": "b", "run_id": "run-live",
                                      "token": "x", "task_id": TASK})
    fly.record_untracked(machine_id="m-1", run_id="run-live", owner="apis",
                         config=fly_runner.FlyConfig.from_env(),
                         started_at=fly._clock(), max_run_seconds=600)
    assert "run-live" in fly.snapshot()["in_flight"]
    notifier = _Notifier()
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert launches == [] and dispatched == [] and writes == []
    assert notifier.sent == []


def test_launching_reservation_is_not_dispatched_again(env):
    ledger, _, launches, dispatched, _ = env
    key = build_delivery.fly_delivery_key(REPO, None, TASK)
    assert fly_runner.FlyLedger(ledger).reserve_delivery(
        key, {"status": "launching", "branch": "b", "run_id": "", "token": "x"})
    asyncio.run(apis.recover_unlaunched_fly_tasks(_Notifier()))
    assert launches == [] and dispatched == []


def test_settled_receipt_is_not_dispatched_again(env):
    ledger, _, launches, dispatched, _ = env
    key = build_delivery.fly_delivery_key(REPO, None, TASK)
    assert fly_runner.FlyLedger(ledger).reserve_delivery(
        key, {"status": "unknown", "branch": "b", "run_id": "", "token": "x", "task_id": TASK})
    asyncio.run(apis.recover_unlaunched_fly_tasks(_Notifier()))
    assert launches == [] and dispatched == []


@pytest.mark.parametrize("case", ["other_agent", "fly_not_opted_in"])
def test_task_not_routed_to_fly_is_untouched(env, monkeypatch, case):
    _, state, launches, dispatched, writes = env
    if case == "other_agent":
        state["task"] = _task(assigned_to="pavo", action_type="analysis")
    else:
        monkeypatch.delenv("APIS_FLY_RUNNER_CLASSES")
    notifier = _Notifier()
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert launches == [] and dispatched == [] and writes == []
    assert notifier.sent == [], "a task that is not Fly-routed must not be reported either"


def test_task_without_a_consumed_approval_is_named_not_released(env):
    _, state, launches, dispatched, _ = env
    state["approval"] = (None, "no approved checkpoint that released it could be verified")
    notifier = _Notifier()
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert launches == [] and dispatched == []
    assert len(notifier.sent) == 1 and TASK in notifier.sent[0]


def test_task_being_dispatched_in_this_process_is_left_alone(env, monkeypatch):
    _, _, launches, dispatched, _ = env
    monkeypatch.setattr(apis, "_tasks_in_dispatch", {TASK: 1})
    asyncio.run(apis.recover_unlaunched_fly_tasks(_Notifier()))
    assert launches == [] and dispatched == []


def test_cleared_receipt_is_named_not_redispatched(env):
    """A receipt the operator cleared leaves no receipt behind; its audit line
    still says a delivery may have launched, so the task is not re-run."""
    import fly_delivery_receipt

    ledger, _, launches, dispatched, _ = env
    fly_delivery_receipt._append_audit(fly_runner.FlyLedger(ledger), {
        "by": "operator", "action": "clear_delivery_receipt", "stage": "removed",
        "key": build_delivery.fly_delivery_key(REPO, None, TASK), "status": "unknown",
        "run_id": "run-old", "branch": "b", "source_task": TASK})
    notifier = _Notifier()
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert launches == [] and dispatched == []
    assert len(notifier.sent) == 1 and TASK in notifier.sent[0]


def test_consumed_approval_refuses_an_older_approval_behind_a_newer_checkpoint(monkeypatch):
    """Only the newest checkpoint can be the release a restart cut off; an older
    consumed approval never releases a later attempt."""
    monkeypatch.setattr(apis, "checkpoints_for_task", lambda task_id: [
        {"entity_id": "ent_old_approved", "created_at": "2026-11-01T10:00:00Z",
         "snapshot": {"status": "approved", "resolved_dispatched": True}},
        {"entity_id": "ent_new_pending", "created_at": "2026-11-06T10:00:00Z",
         "snapshot": {"status": "awaiting_operator"}},
    ])
    monkeypatch.setattr(apis, "fetch_checkpoint_record",
                        lambda cid: pytest.fail("a non-releasing checkpoint was read for release"))
    checkpoint, why = apis._consumed_approval(TASK, _task())
    assert checkpoint is None and "ent_new_pending" in why


def test_consumed_approval_refuses_an_unconsumed_approval(monkeypatch):
    monkeypatch.setattr(apis, "checkpoints_for_task", lambda task_id: [
        {"entity_id": "ent_unconsumed", "created_at": "2026-11-06T10:00:00Z",
         "snapshot": {"status": "approved"}}])
    monkeypatch.setattr(apis, "fetch_checkpoint_record",
                        lambda cid: pytest.fail("an unconsumed checkpoint was read for release"))
    checkpoint, why = apis._consumed_approval(TASK, _task())
    assert checkpoint is None and why


# ── Correction round: unknown receipts and the full approval binding ─────────

USER = "user-tenant-1"
_REAL_CONSUMED_APPROVAL = apis._consumed_approval


class _Policy:
    loaded = True
    entity_id = "ent_policy"


def _approval_records(monkeypatch, *, authorization: dict | None = None,
                      later: list[dict] | None = None, brief: dict | None = None,
                      newer: list | None = None):
    """Fixture records for the REAL ``_consumed_approval``: one consumed,
    authenticated approval of the task at observation count 3, then the task's
    observations since (newest first). Only Neotoma reads and the policy lookup
    are faked."""
    later = list(later or [])
    brief = {"status": "approved", "resolved_dispatched": True,
             "gate_action": apis.GateAction.CHECKPOINT.value, "blast_radius": "high",
             **(brief or {})}
    auth = {"task_entity_id": TASK, "user_id": USER, "action_type": "open_or_merge_pr",
            "policy_entity_id": "ent_policy", "policy_revision": "rev-1",
            "gate_action": apis.GateAction.CHECKPOINT.value, "blast_radius": "high",
            "task_observation_count": 3, "task_revision": "approved-digest",
            "task_last_observation_at": "2026-11-06T10:00:00Z",
            "required_approver_sub": "op@x", "required_approver_jkt": "j" * 43,
            **(authorization or {})}
    task_record = {"entity_id": TASK, "observation_count": 3 + len(later),
                   "last_observation_at": "2026-11-06T10:05:00Z", "snapshot": _task()}
    original = [{"id": f"obs-{i}", "fields": {"title": "Build the thing"}} for i in range(3)]
    decision = apis.GateDecision(action=apis.GateAction.CHECKPOINT,
                                 blast_radius=apis.BlastRadius.HIGH, confidence=0.5,
                                 threshold=0.85, policy_id="ent_policy", reason="")
    monkeypatch.setattr(apis, "checkpoints_for_task", lambda task_id: [
        {"entity_id": CHECKPOINT, "created_at": "2026-11-06T10:00:00Z", "snapshot": brief},
        *(newer or [])])
    monkeypatch.setattr(apis, "_checkpoint_denial_persisted", lambda cid: False)
    monkeypatch.setattr(apis, "fetch_checkpoint_record", lambda cid: {"entity_id": cid})
    monkeypatch.setattr(apis, "read_authenticated_checkpoint_authorization",
                        lambda cid, record, **k: dict(auth))
    monkeypatch.setattr(apis, "read_verified_entity_owner", lambda eid, record, **k: USER)
    monkeypatch.setattr(apis, "read_authenticated_checkpoint_resolution",
                        lambda *a, **k: {"principal_sub": "op@x"})
    monkeypatch.setattr(apis, "fetch_task_record", lambda task_id: dict(task_record))
    monkeypatch.setattr(apis, "_task_gate_context", lambda snap, **k: (
        "cicada", "open_or_merge_pr", _Policy(), decision))
    monkeypatch.setattr(apis, "execution_policy_revision", lambda policy: "rev-1")
    monkeypatch.setattr(apis, "fetch_entity_observations_strict",
                        lambda eid, **k: later + original)


def _handoff(status: str) -> dict:
    return {"id": f"handoff-{status}", "fields": {"status": status},
            "idempotency_key": f"taskstatus-apis-{TASK}-{status}-approved"}


_CLEAR_HOLD = {"id": "handoff-reason", "fields": {"blocked_reason": ""},
               "idempotency_key": f"taskreason-apis-{TASK}-routed-approved"}


def test_unreadable_ledger_is_named_not_left_silent(env, monkeypatch):
    _, _, launches, dispatched, _ = env

    def _broken():
        raise fly_runner.FlyLedgerError("ledger unreadable")

    monkeypatch.setattr(build_delivery, "_delivery_ledger", _broken)
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert launches == [] and dispatched == []
    assert len(notifier.sent) == 1, notifier.sent
    assert TASK in notifier.sent[0]
    assert "Fly receipt state is unknown" in notifier.sent[0]
    assert "ledger could not be read" in notifier.sent[0]
    assert "never launched" not in notifier.sent[0]
    assert counts["unrecovered"] == 1 and counts["recorded"] == 0


def test_task_with_only_the_executing_handoff_since_approval_is_recovered(env, monkeypatch):
    """The real approval check over fixture records: the only writes since the
    approval are Apis's own routed and executing handoff, so it is released."""
    _, _, launches, dispatched, _ = env
    monkeypatch.setattr(apis, "_consumed_approval", _REAL_CONSUMED_APPROVAL)
    _approval_records(monkeypatch, later=[_handoff("executing"), _handoff("routed"),
                                          _CLEAR_HOLD])
    notifier = _Notifier()
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert launches == [TASK] and len(dispatched) == 1, notifier.sent
    assert dispatched[0]["release_key"].startswith(CHECKPOINT)
    assert notifier.sent == []


@pytest.mark.parametrize("change", [
    {"id": "edit", "fields": {"description": "do something else"},
     "idempotency_key": "operator-edit"},
    {"id": "notes", "fields": {"notes": "x"}, "idempotency_key": f"taskstatus-apis-{TASK}-x"},
    {"id": "other-writer", "fields": {"status": "executing"},
     "idempotency_key": f"taskstatus-sylvia-{TASK}-executing"},
    {"id": "mixed", "fields": {"status": "executing", "repo": "other/repo"},
     "idempotency_key": f"taskstatus-apis-{TASK}-executing"},
], ids=["description", "notes", "other_writer", "mixed_fields"])
def test_task_changed_after_approval_is_named_not_released(env, monkeypatch, change):
    _, _, launches, dispatched, _ = env
    monkeypatch.setattr(apis, "_consumed_approval", _REAL_CONSUMED_APPROVAL)
    _approval_records(monkeypatch, later=[_handoff("executing"), change, _handoff("routed")])
    notifier = _Notifier()
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert launches == [] and dispatched == []
    assert len(notifier.sent) == 1 and TASK in notifier.sent[0]
    assert "changed after checkpoint" in notifier.sent[0]


def test_unreadable_task_observations_are_named_not_released(env, monkeypatch):
    _, _, launches, dispatched, _ = env
    monkeypatch.setattr(apis, "_consumed_approval", _REAL_CONSUMED_APPROVAL)
    _approval_records(monkeypatch, later=[_handoff("executing")])
    monkeypatch.setattr(apis, "fetch_entity_observations_strict", lambda eid, **k: None)
    notifier = _Notifier()
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert launches == [] and dispatched == []
    assert len(notifier.sent) == 1 and TASK in notifier.sent[0]


@pytest.mark.parametrize("override", [
    {"authorization": {"user_id": "another-tenant"}},
    {"authorization": {"gate_action": apis.GateAction.CHECKPOINT_WITH_ALTERNATIVES.value}},
    {"authorization": {"blast_radius": "low"}},
    {"brief": {"blast_radius": "low"}, "authorization": {"blast_radius": "low"}},
], ids=["user_id", "gate_action", "blast_radius", "decision_inconsistent"])
def test_mismatched_authorization_binding_is_not_recovered(monkeypatch, override):
    """Each of user_id, gate_action and blast_radius must match as the normal
    release requires; the last case binds a blast radius the current gate
    decision (high) is not consistent with."""
    _approval_records(monkeypatch, later=[_handoff("executing")], **override)
    checkpoint, why = apis._consumed_approval(TASK, _task())
    assert checkpoint is None and "changed after checkpoint" in why


def test_cleared_receipt_notice_does_not_claim_it_never_launched(env):
    import fly_delivery_receipt

    ledger, _, launches, dispatched, _ = env
    fly_delivery_receipt._append_audit(fly_runner.FlyLedger(ledger), {
        "by": "operator", "action": "clear_delivery_receipt", "stage": "removed",
        "key": build_delivery.fly_delivery_key(REPO, None, TASK), "status": "unknown",
        "run_id": "run-old", "branch": "b", "source_task": TASK})
    notifier = _Notifier()
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert launches == [] and dispatched == []
    assert len(notifier.sent) == 1
    assert "never launched" not in notifier.sent[0]
    assert "whether its Fly run launched is unknown" in notifier.sent[0]


def test_unreadable_audit_file_is_held_and_named(env, monkeypatch):
    """The ledger reads (no receipt) but its removal audit file does not, so
    ``_fly_receipt_removed`` returns None: a cleared receipt cannot be ruled
    out, so the task is held and named, never re-launched."""
    _, _, launches, dispatched, writes = env
    audit_reads: list[object] = []

    def _unreadable_audit(ledger):
        audit_reads.append(ledger)
        raise OSError("audit file unreadable")

    monkeypatch.setattr(build_delivery, "_removal_audit_lines", _unreadable_audit)
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert audit_reads, "the sweep never reached the audit read"
    assert launches == [] and dispatched == [] and writes == []
    assert len(notifier.sent) == 1, notifier.sent
    assert TASK in notifier.sent[0]
    assert "whether its Fly run launched is unknown" in notifier.sent[0]
    assert "audit could not be read" in notifier.sent[0]
    assert "never launched" not in notifier.sent[0]
    assert counts["unrecovered"] == 1 and counts["redispatched"] == 0


def test_failed_reread_after_a_removed_receipt_says_launch_is_unknown(env, monkeypatch):
    """Empty ledger, a prior removal-audit entry for the task, and a failed
    ``fetch_task_snapshot``: the sweep stops before it reaches the audit, so
    it must not say the run never launched (a cleared receipt shows it may
    have). Nothing is dispatched and one notice says launch is unknown."""
    import fly_delivery_receipt

    ledger, _, launches, dispatched, writes = env
    fly_delivery_receipt._append_audit(fly_runner.FlyLedger(ledger), {
        "by": "operator", "action": "clear_delivery_receipt", "stage": "removed",
        "key": build_delivery.fly_delivery_key(REPO, None, TASK), "status": "unknown",
        "run_id": "run-old", "branch": "b", "source_task": TASK})
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda entity_id: None)
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert launches == [] and dispatched == [] and writes == []
    assert len(notifier.sent) == 1, notifier.sent
    assert TASK in notifier.sent[0]
    assert "whether its Fly run launched is unknown" in notifier.sent[0]
    assert "could not be re-read" in notifier.sent[0]
    assert "earlier Fly run" in notifier.sent[0]
    assert counts["unrecovered"] == 1 and counts["redispatched"] == 0


@pytest.mark.parametrize("why", [
    "it could not be re-read",
    apis._RECEIPT_UNKNOWN,
    "the Fly ledger's audit could not be read",
    "a receipt for it was cleared or superseded",
    "a correction's release is bound to its checkpoint and cannot be resumed from here",
    "it has no checkpoint that released it",
    "examining it for recovery failed",
])
def test_every_unrecovered_notice_says_launch_is_unknown(why):
    """Every reason the sweep gives for not relaunching produces a notice that
    names the task and reason, says launch is unknown, never claims the run
    never launched, and asks for an earlier run to be checked before refiling."""
    notifier = _Notifier()
    apis._unrecovered_notice(notifier, TASK, "Build the thing", why)
    (text,) = notifier.sent
    assert TASK in text and why in text
    assert "whether its Fly run launched is unknown" in text
    assert "never launched" not in text
    assert "earlier Fly run" in text and "before refiling" in text


# ── Correction round 2: an unreadable checkpoint set fails closed ────────────

_NEWER = "2026-11-07T10:00:00Z"


@pytest.mark.parametrize("malformed", [
    {"created_at": _NEWER, "snapshot": {"status": "awaiting_operator"}},
    {"entity_id": "", "created_at": _NEWER, "snapshot": {"status": "awaiting_operator"}},
    {"entity_id": "ent_newer", "snapshot": {"status": "awaiting_operator"}},
    {"entity_id": "ent_newer", "created_at": "yesterday-ish",
     "snapshot": {"status": "awaiting_operator"}},
    {"entity_id": "ent_newer", "created_at": _NEWER, "snapshot": {}},
    {"entity_id": "ent_newer", "created_at": _NEWER, "snapshot": {"status": "pending_review"}},
    {"entity_id": "ent_newer", "created_at": _NEWER, "snapshot": {"status": None}},
    {"entity_id": "ent_newer", "created_at": _NEWER},
    "ent_newer",
    None,
], ids=["no_entity_id", "empty_entity_id", "no_time", "unparseable_time", "no_status",
        "unrecognised_status", "null_status", "no_snapshot", "not_a_dict_str",
        "not_a_dict_none"])
def test_newer_unreadable_checkpoint_is_not_skipped_for_an_older_approval(
        env, monkeypatch, malformed):
    """A newer checkpoint row that cannot take part in choosing the newest
    makes the whole set unknown: the older consumed approval beside it never
    releases the task, nothing is written, and one notice says launch is
    unknown."""
    _, _, launches, dispatched, writes = env
    monkeypatch.setattr(apis, "_consumed_approval", _REAL_CONSUMED_APPROVAL)
    _approval_records(monkeypatch, later=[_handoff("executing")], newer=[malformed])
    monkeypatch.setattr(apis, "fetch_checkpoint_record",
                        lambda cid: pytest.fail("a checkpoint was read for release"))
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert launches == [] and dispatched == [] and writes == []
    assert len(notifier.sent) == 1 and TASK in notifier.sent[0]
    assert "whether its Fly run launched is unknown" in notifier.sent[0]
    assert "its checkpoints could not all be read" in notifier.sent[0]
    assert counts["unrecovered"] == 1 and counts["redispatched"] == 0


def test_well_formed_set_still_relaunches_on_the_newest_approval(env, monkeypatch):
    """The fail-closed check does not refuse a readable set: an older
    superseded checkpoint beside the newest consumed approval still relaunches."""
    _, _, launches, dispatched, _ = env
    monkeypatch.setattr(apis, "_consumed_approval", _REAL_CONSUMED_APPROVAL)
    _approval_records(monkeypatch, later=[_handoff("executing")], newer=[
        {"entity_id": "ent_older", "created_at": "2026-11-01T10:00:00Z",
         "snapshot": {"status": "superseded"}}])
    notifier = _Notifier()
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert launches == [TASK] and len(dispatched) == 1, notifier.sent
    assert dispatched[0]["release_key"].startswith(CHECKPOINT)


def test_tied_newest_checkpoints_are_not_ordered(monkeypatch):
    _approval_records(monkeypatch, later=[_handoff("executing")], newer=[
        {"entity_id": "ent_twin", "created_at": "2026-11-06T10:00:00Z",
         "snapshot": {"status": "awaiting_operator"}}])
    checkpoint, why = apis._consumed_approval(TASK, _task())
    assert checkpoint is None and "cannot be told" in why


@pytest.mark.parametrize("row", [{"snapshot": {"status": "executing"}}, "garbage"],
                         ids=["no_entity_id", "not_a_dict"])
def test_unreadable_task_row_is_named_not_skipped(env, monkeypatch, row):
    _, _, launches, dispatched, _ = env
    monkeypatch.setattr(apis, "query_all_entities", lambda *a, **k: [row])
    notifier = _Notifier()
    counts = asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert launches == [] and dispatched == []
    assert len(notifier.sent) == 1 and "launched is unknown" in notifier.sent[0]
    assert counts["unrecovered"] == 1


def test_uninterpretable_ledger_entry_makes_the_receipt_state_unknown(monkeypatch):
    """A ledger entry that is not an object could be this task's receipt: the
    receipt state is unknown, not absent."""

    class _Ledger:
        def snapshot(self):
            return {"deliveries": {"some-key": "not-an-object"}}

    monkeypatch.setattr(apis, "_fly_run_state", lambda snap, eid: (False, None))
    monkeypatch.setattr(build_delivery, "_delivery_ledger", lambda: _Ledger())
    assert apis._fly_delivery_recorded(_task(), TASK) is None


def test_non_object_task_observation_is_unreadable_not_a_change(env, monkeypatch):
    _, _, launches, dispatched, _ = env
    monkeypatch.setattr(apis, "_consumed_approval", _REAL_CONSUMED_APPROVAL)
    _approval_records(monkeypatch, later=["not-an-observation"])
    notifier = _Notifier()
    asyncio.run(apis.recover_unlaunched_fly_tasks(notifier))
    assert launches == [] and dispatched == []
    assert len(notifier.sent) == 1 and "could not be read" in notifier.sent[0]
