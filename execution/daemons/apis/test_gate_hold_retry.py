"""A delayed lifecycle write must be read back before checkpoint creation."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
from lib.daemon_runtime import gating  # noqa: E402
from lib.daemon_runtime.test_gating import _FakeNeotoma, _http_signer  # noqa: E402
from lib.daemon_runtime.gating import BlastRadius, ExecutionPolicy, GateAction, GateDecision


def _task(status="awaiting_approval", reason="gate reason"):
    return {
        "entity_id": "ent_task", "entity_type": "task",
        "provenance": {"blocked_reason": "obs-hold"},
        "last_observation_at": "2026-01-01T00:00:00Z",
        "snapshot": {"status": status, "blocked_reason": reason},
    }


def test_late_landing_hold_is_proven_before_checkpoint(monkeypatch):
    reads = iter([_task("routed", ""), _task()])
    monkeypatch.setattr(apis, "fetch_task_record", lambda _id: next(reads))
    clock = [0]

    async def sleep(seconds):
        clock[0] += seconds

    record = asyncio.run(apis._prove_gate_hold(
        "ent_task", "gate reason", clock=lambda: clock[0], sleep=sleep
    ))
    assert record["snapshot"]["blocked_reason"] == "gate reason"
    assert clock[0] == apis.GATE_HOLD_PROOF_INTERVAL_SECONDS


def test_unproven_hold_is_bounded_and_never_authorizes(monkeypatch):
    reads = []

    def fetch(_id):
        reads.append(1)
        return _task("routed", "")

    monkeypatch.setattr(apis, "fetch_task_record", fetch)
    clock = [0]

    async def sleep(seconds):
        clock[0] += seconds

    assert asyncio.run(apis._prove_gate_hold(
        "ent_task", "gate reason", clock=lambda: clock[0], sleep=sleep
    )) is None
    assert len(reads) == apis.GATE_HOLD_PROOF_MAX_READS
    assert clock[0] <= apis.GATE_HOLD_PROOF_SECONDS


@pytest.mark.parametrize("lands", [True, False])
@pytest.mark.parametrize("title", ["Generic task", "A" * 80])
def test_dispatch_creates_checkpoint_only_after_late_hold_lands(monkeypatch, lands, title):
    snapshot = {"title": title, "tags": ["ops"], "status": "pending", "confidence": 0.2}
    policy = ExecutionPolicy(entity_id="policy", loaded=True)
    decision = GateDecision(
        action=GateAction.CHECKPOINT, blast_radius=BlastRadius.HIGH,
        confidence=0.2, threshold=0.85, policy_id="policy", reason="gate reason",
    )
    writes = []
    briefs = []
    notices = []
    sweep_outcomes = []

    class Notifier:
        def send(self, *_args, **_kwargs):
            notices.append(_args[0])

        def clear_dedupe(self, _key):
            pass

    async def status_still(*_args):
        return True

    monkeypatch.setattr(apis, "_task_status_still", status_still)
    monkeypatch.setattr(apis, "READINESS_GATE", False)
    monkeypatch.setattr(apis, "set_task_status", lambda *a, **kw: writes.append((a, kw)) or True)
    monkeypatch.setattr(apis, "resolve_policy_for_agent", lambda _skill: policy)
    monkeypatch.setattr(apis, "evaluate_gate", lambda **_kw: decision)
    monkeypatch.setattr(apis, "fetch_entity_user_id", lambda _id: "tenant")
    monkeypatch.setattr(apis, "write_checkpoint_brief", lambda **kw: briefs.append(kw) or "ent_brief")
    reads = iter([_task("routed", ""), _task()]) if lands else None
    def fetch(_id):
        if not sweep_outcomes:
            sweep_outcomes.append(apis._reconcile_missing_checkpoint("ent_task", apply=True))
        return next(reads) if lands else _task("routed", "")

    monkeypatch.setattr(apis, "fetch_task_record", fetch)
    monkeypatch.setattr(apis, "GATE_HOLD_PROOF_INTERVAL_SECONDS", 0)

    asyncio.run(apis.dispatch_task(
        "ent_task", snapshot, trigger="created", notifier=Notifier(), snapshot_hydrated=True
    ))
    assert any(
        a[1].value == "awaiting_approval" and kw["reason"] == "gate reason"
        for a, kw in writes
    )
    assert len(briefs) == int(lands)
    assert sweep_outcomes == ["skipped_in_flight"]
    assert notices[0].splitlines()[0] == f"PLAN checkpoint: {title[:70]}"
    assert "task=ent_task brief=" in notices[0]
    if lands:
        assert briefs[0]["task_record"]["snapshot"]["status"] == "awaiting_approval"
        assert briefs[0]["idempotency_context"] == "hold-obs-hold"
    else:
        assert any(
            "reissue_held_checkpoints.py --gate-hold-only --apply" in message
            and "default run is a dry run" in message
            for message in notices
        )


def test_gate_and_sweep_share_one_signed_checkpoint_identity(monkeypatch):
    server = _FakeNeotoma(monkeypatch, _http_signer())
    held = _task()
    held["snapshot"].update({
        "title": "Generic task", "assigned_to": "cicada",
        "action_type": "open_or_merge_pr", "confidence": 0.2,
    })
    incoming = {**held["snapshot"], "status": "pending"}
    policy = ExecutionPolicy(entity_id="policy", loaded=True)
    decision = GateDecision(
        action=GateAction.CHECKPOINT, blast_radius=BlastRadius.HIGH,
        confidence=0.2, threshold=0.85, policy_id="policy", reason="gate reason",
    )

    class Notifier:
        def send(self, *_args, **_kwargs):
            pass

        def clear_dedupe(self, _key):
            pass

    async def status_still(*_args):
        return True

    monkeypatch.setattr(apis, "_task_status_still", status_still)
    monkeypatch.setattr(apis, "READINESS_GATE", False)
    monkeypatch.setattr(apis, "set_task_status", lambda *a, **kw: True)
    monkeypatch.setattr(apis, "resolve_policy_for_agent", lambda _skill: policy)
    monkeypatch.setattr(apis, "evaluate_gate", lambda **_kw: decision)
    monkeypatch.setattr(apis, "fetch_entity_user_id", lambda _id: "tenant-a")
    monkeypatch.setattr(apis, "fetch_task_record", lambda _id: held)
    monkeypatch.setattr(apis, "write_" + "checkpoint" + "_brief", getattr(gating, "write_" + "checkpoint" + "_brief"))
    monkeypatch.setattr(apis, "checkpoints_for_task", lambda _id, strict: [])
    monkeypatch.setattr(gating, "checkpoints_for_task", lambda task_id, *, strict: [
        server.fetch_entity(entity_id)
        for entity_id, item in server.entities.items()
        if item["fields"]["task_entity_id"] == task_id
    ])
    monkeypatch.setattr(apis, "fetch_checkpoint_record", server.fetch_entity)
    monkeypatch.setattr(apis, "_checkpoint_authority_state", lambda _id, _record: "current")

    asyncio.run(apis.dispatch_task(
        "ent_task", incoming, trigger="created", notifier=Notifier(), snapshot_hydrated=True,
    ))
    assert len(server.entities) == 1
    assert apis._reconcile_missing_checkpoint("ent_task", apply=True) == "raised"
    assert len(server.entities) == 1
    first, second = (entry["body"] for entry in server.stores)
    assert first["idempotency_key"] == second["idempotency_key"]
    assert first["entities"][0]["title"] == second["entities"][0]["title"]
    assert first["entities"][0]["title"].startswith(
        "PLAN checkpoint: Generic task [ent_task] #"
    )
    assert first["entities"][0]["plan_summary"] != second["entities"][0]["plan_summary"]
    assert "Trigger: created." in first["entities"][0]["plan_summary"]
    assert "Gate hold for cicada." in second["entities"][0]["plan_summary"]
    for field in ("body", "task_entity_id", "reason", "policy_entity_id", "gate_action"):
        assert first["entities"][0][field] == second["entities"][0][field]


def test_reissue_checkpoint_title_names_the_task(monkeypatch):
    server = _FakeNeotoma(monkeypatch, _http_signer())
    snapshot = {"title": "Approve the recovery", "status": "awaiting_approval"}
    record = {
        "entity_id": "ent_task", "entity_type": "task",
        "observation_count": 4,
        "snapshot": snapshot,
    }
    policy = ExecutionPolicy(entity_id="policy", loaded=True)
    decision = GateDecision(
        action=GateAction.CHECKPOINT, blast_radius=BlastRadius.HIGH,
        confidence=0.2, threshold=0.85, policy_id="policy", reason="gate reason",
    )
    monkeypatch.setattr(apis, "_task_gate_context", lambda _snapshot: (
        "cicada", "open_or_merge_pr", policy, decision
    ))
    monkeypatch.setattr(apis, "fetch_task_record", lambda _id: record)
    monkeypatch.setattr(apis, "fetch_entity_user_id", lambda _id: "tenant-a")
    monkeypatch.setattr(apis, "write_checkpoint_brief", gating.write_checkpoint_brief)

    brief_id = apis._file_fresh_checkpoint(
        prior_checkpoint_id="ent_old", task_id="ent_task",
        task_snapshot=snapshot, notifier=None,
    )

    assert brief_id is not None
    assert server.entities[brief_id]["fields"]["title"].startswith(
        "PLAN checkpoint: Approve the recovery [ent_task] #"
    )


@pytest.mark.parametrize("failure", [RuntimeError("read failed"), asyncio.CancelledError()])
def test_gate_proof_failure_clears_in_flight_entry(monkeypatch, failure):
    snapshot = {"title": "Generic task", "tags": ["ops"], "status": "pending", "confidence": 0.2}
    policy = ExecutionPolicy(entity_id="policy", loaded=True)
    decision = GateDecision(
        action=GateAction.CHECKPOINT, blast_radius=BlastRadius.HIGH,
        confidence=0.2, threshold=0.85, policy_id="policy", reason="gate reason",
    )

    async def status_still(*_args):
        return True

    async def proof(*_args):
        assert apis._gate_hold_is_in_flight("ent_task")
        raise failure

    class Notifier:
        def send(self, *_args, **_kwargs):
            pass

        def clear_dedupe(self, _key):
            pass

    monkeypatch.setattr(apis, "_task_status_still", status_still)
    monkeypatch.setattr(apis, "READINESS_GATE", False)
    monkeypatch.setattr(apis, "set_task_status", lambda *a, **kw: True)
    monkeypatch.setattr(apis, "resolve_policy_for_agent", lambda _skill: policy)
    monkeypatch.setattr(apis, "evaluate_gate", lambda **_kw: decision)
    monkeypatch.setattr(apis, "_prove_gate_hold", proof)
    with pytest.raises(type(failure)):
        asyncio.run(apis.dispatch_task(
            "ent_task", snapshot, trigger="created", notifier=Notifier(), snapshot_hydrated=True,
        ))
    assert not apis._gate_hold_is_in_flight("ent_task")
