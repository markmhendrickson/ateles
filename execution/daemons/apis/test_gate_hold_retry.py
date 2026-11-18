"""A delayed lifecycle write must be read back before checkpoint creation."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
from lib.daemon_runtime.gating import BlastRadius, ExecutionPolicy, GateAction, GateDecision


def _task(status="awaiting_approval", reason="gate reason"):
    return {
        "entity_id": "ent_task", "entity_type": "task",
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
def test_dispatch_creates_checkpoint_only_after_late_hold_lands(monkeypatch, lands):
    snapshot = {"title": "Generic task", "tags": ["ops"], "status": "pending", "confidence": 0.2}
    policy = ExecutionPolicy(entity_id="policy", loaded=True)
    decision = GateDecision(
        action=GateAction.CHECKPOINT, blast_radius=BlastRadius.HIGH,
        confidence=0.2, threshold=0.85, policy_id="policy", reason="gate reason",
    )
    writes = []
    briefs = []
    notices = []

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
    monkeypatch.setattr(apis, "fetch_task_record", lambda _id: next(reads) if lands else _task("routed", ""))
    monkeypatch.setattr(apis, "GATE_HOLD_PROOF_INTERVAL_SECONDS", 0)

    asyncio.run(apis.dispatch_task(
        "ent_task", snapshot, trigger="created", notifier=Notifier(), snapshot_hydrated=True
    ))
    assert any(
        a[1].value == "awaiting_approval" and kw["reason"] == "gate reason"
        for a, kw in writes
    )
    assert len(briefs) == int(lands)
    if lands:
        assert briefs[0]["task_record"]["snapshot"]["status"] == "awaiting_approval"
    else:
        assert any("retry sweep will raise the checkpoint" in message for message in notices)
