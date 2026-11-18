"""Late lifecycle writes and missing checkpoint recovery, using fakes only."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
try:
    import held_checkpoint_sweep as cli  # noqa: E402
except ImportError:
    cli = None  # The pre-change baseline has no sweep entry point.
from lib.daemon_runtime.gating import BlastRadius, ExecutionPolicy, GateAction, GateDecision


def task(status="awaiting_approval", reason="gate reason", observation="obs-hold", observed_at="2026-01-01T00:00:00Z"):
    return {
        "entity_id": "ent_task",
        "entity_type": "task",
        "provenance": {"blocked_reason": observation},
        "last_observation_at": observed_at,
        "snapshot": {"status": status, "blocked_reason": reason, "title": "Generic task", "confidence": 0.7},
    }


@pytest.fixture
def recovery(monkeypatch):
    state = {"task": task(), "checkpoints": [], "writes": 0, "reads": 0}
    policy = ExecutionPolicy(entity_id="policy", loaded=True)
    decision = GateDecision(
        action=GateAction.CHECKPOINT, blast_radius=BlastRadius.HIGH,
        confidence=0.7, threshold=0.85, policy_id="policy", reason="gate reason",
    )

    def fetch(_id):
        state["reads"] += 1
        return state["task"]

    def write(**kwargs):
        state["writes"] += 1
        assert kwargs["idempotency_context"] == "hold-obs-hold"
        state["checkpoints"] = [{
            "entity_id": "ent_brief",
            "snapshot": {"status": "awaiting_operator", "task_entity_id": "ent_task"},
        }]
        return "ent_brief"

    monkeypatch.setattr(apis, "fetch_task_record", fetch)
    monkeypatch.setattr(apis, "checkpoints_for_task", lambda _id, strict: state["checkpoints"])
    monkeypatch.setattr(apis, "_task_gate_context", lambda _snapshot: ("cicada", "open_or_merge_pr", policy, decision))
    monkeypatch.setattr(apis, "fetch_entity_user_id", lambda _id: "tenant")
    monkeypatch.setattr(apis, "write_checkpoint_brief", write)
    monkeypatch.setattr(apis, "fetch_checkpoint_record", lambda _id: state["checkpoints"][0])
    monkeypatch.setattr(apis, "_checkpoint_authority_state", lambda _id, _record: "current")
    return state, decision, policy


def test_missing_gate_checkpoint_is_raised_once_and_read_back(recovery):
    state, _, _ = recovery
    assert apis._reconcile_missing_checkpoint("ent_task", apply=True) == "raised"
    assert apis._reconcile_missing_checkpoint("ent_task", apply=True) == "held_with_checkpoint"
    assert state["writes"] == 1
    assert state["reads"] >= 3


def test_fresh_hold_is_skipped_and_old_hold_is_raised(recovery):
    state, _, _ = recovery
    now = datetime(2026, 11, 18, tzinfo=timezone.utc)
    state["task"] = task(observed_at=(now - timedelta(seconds=30)).isoformat())
    assert apis._reconcile_missing_checkpoint("ent_task", apply=True, now=now) == "skipped_fresh"
    assert state["writes"] == 0
    state["task"] = task(observed_at=(now - timedelta(seconds=apis.HELD_CHECKPOINT_SWEEP_MIN_AGE_SECONDS + 1)).isoformat())
    assert apis._reconcile_missing_checkpoint("ent_task", apply=True, now=now) == "raised"
    assert state["writes"] == 1


def test_sweep_age_floor_exceeds_proof_window():
    assert apis.HELD_CHECKPOINT_SWEEP_MIN_AGE_SECONDS >= 120
    assert (apis.HELD_CHECKPOINT_SWEEP_MIN_AGE_SECONDS - apis.GATE_HOLD_PROOF_SECONDS
            >= apis.HELD_CHECKPOINT_WRITE_RETRY_BUDGET_SECONDS
            + apis.HELD_CHECKPOINT_SWEEP_SAFETY_MARGIN_SECONDS)


def test_in_flight_hold_is_counted_without_a_write(recovery, monkeypatch):
    state, _, _ = recovery
    monkeypatch.setattr(apis, "query_entities", lambda *a, **kw: {
        "entities": [{"entity_id": "ent_task"}], "total": 1,
    })
    with apis._gate_hold_proving("ent_task"):
        counts, _ = apis.sweep_missing_gate_checkpoints(apply=True)
    assert counts["skipped_in_flight"] == 1
    assert state["writes"] == 0


def test_fresh_hold_is_counted_without_a_write(recovery, monkeypatch):
    state, _, _ = recovery
    state["task"] = task(observed_at=datetime.now(timezone.utc).isoformat())
    monkeypatch.setattr(apis, "query_entities", lambda *a, **kw: {
        "entities": [{"entity_id": "ent_task"}], "total": 1,
    })
    counts, _ = apis.sweep_missing_gate_checkpoints(apply=True)
    assert counts["skipped_fresh"] == 1
    assert state["writes"] == 0


@pytest.mark.parametrize("status", ["cancelled", "declined", "done", "routed", "executing", "blocked", "failed"])
def test_stale_hold_reason_on_nonheld_task_never_creates_authority(recovery, status):
    state, _, _ = recovery
    state["task"] = task(status)
    assert apis._reconcile_missing_checkpoint("ent_task", apply=True) == "skipped_by_reason"
    assert state["writes"] == 0


def test_status_change_before_write_is_skipped(recovery, monkeypatch):
    state, _, _ = recovery
    reads = iter([task(), task("cancelled")])
    monkeypatch.setattr(apis, "fetch_task_record", lambda _id: next(reads))
    assert apis._reconcile_missing_checkpoint("ent_task", apply=True) == "skipped_by_reason"
    assert state["writes"] == 0


def test_non_gate_hold_is_reported_without_write(recovery):
    state, _, _ = recovery
    state["task"] = task(reason="session hold")
    state["task"]["snapshot"].pop("confidence")
    assert apis._reconcile_missing_checkpoint("ent_task", apply=True) == "skipped_by_reason"
    assert state["writes"] == 0


def test_unreconstructable_hold_is_reported_without_write(recovery):
    state, _, _ = recovery
    state["task"] = task(observation="")
    assert apis._reconcile_missing_checkpoint("ent_task", apply=True) == "unreconstructable"
    assert state["writes"] == 0


def test_unstored_producer_score_is_not_guessed(recovery):
    state, _, _ = recovery
    state["task"]["snapshot"].pop("confidence")
    assert apis._reconcile_missing_checkpoint("ent_task", apply=True) == "unreconstructable"
    assert state["writes"] == 0


def test_unreadable_checkpoint_collection_denies(recovery, monkeypatch):
    state, _, _ = recovery
    monkeypatch.setattr(apis, "checkpoints_for_task", lambda _id, strict: None)
    assert apis._reconcile_missing_checkpoint("ent_task", apply=True) == "unreconstructable"
    assert state["writes"] == 0


@pytest.mark.parametrize("status", [
    "rejected", "approved_no_release", "approved",
    "approved_requires_fresh_approval",
])
def test_answered_checkpoint_is_not_reopened(recovery, status):
    state, _, _ = recovery
    state["checkpoints"] = [{
        "entity_id": "ent_approved",
        "created_at": "2026-01-01T00:01:00Z",
        "snapshot": {"status": status, "task_entity_id": "ent_task",
                     "resolved_dispatched": status == "approved"},
    }]
    assert apis._reconcile_missing_checkpoint("ent_task", apply=True) == "skipped_by_reason"
    assert state["writes"] == 0


def test_superseded_checkpoint_allows_recovery(recovery):
    state, _, _ = recovery
    state["checkpoints"] = [{
        "entity_id": "ent_retired", "created_at": "2026-01-01T00:01:00Z",
        "snapshot": {"status": "superseded", "task_entity_id": "ent_task"},
    }]
    assert apis._reconcile_missing_checkpoint("ent_task", apply=False) == "candidate"


def test_answer_landing_during_sweep_blocks_write(recovery, monkeypatch):
    state, _, _ = recovery
    reads = 0

    def checkpoints(_id, *, strict):
        nonlocal reads
        reads += 1
        if reads == 1:
            return []
        return [{"entity_id": "ent_answer", "created_at": "2026-01-01T00:01:00Z",
                 "snapshot": {"status": "rejected", "task_entity_id": "ent_task"}}]

    monkeypatch.setattr(apis, "checkpoints_for_task", checkpoints)
    assert apis._reconcile_missing_checkpoint("ent_task", apply=True) == "skipped_by_reason"
    assert state["writes"] == 0


def test_successful_write_without_readback_is_not_reported_raised(recovery, monkeypatch):
    state, _, _ = recovery
    monkeypatch.setattr(apis, "fetch_checkpoint_record", lambda _id: None)
    assert apis._reconcile_missing_checkpoint("ent_task", apply=True) == "unreconstructable"
    assert state["writes"] == 1


def test_sweep_page_size_and_time_budget(recovery, monkeypatch):
    state, _, _ = recovery
    requested = []
    monkeypatch.setattr(apis, "query_entities", lambda *a, **kw: (
        requested.append(kw) or {"entities": [{"entity_id": "ent_task"}] * 3, "total": 8}
    ))
    tick = iter([0, 0, 46])
    candidates = []
    counts, next_offset = apis.sweep_missing_gate_checkpoints(
        apply=False, offset=2, clock=lambda: next(tick), candidates=candidates,
    )
    assert requested[0]["limit"] == apis.HELD_CHECKPOINT_SWEEP_PAGE_SIZE
    assert requested[0]["offset"] == 2
    assert requested[0]["updated_since"]
    assert counts["scanned"] == 1
    assert counts["candidates"] == 1
    assert candidates == [{"task_id": "ent_task", "action": "would_raise_checkpoint"}]
    assert next_offset == 3  # resume at the unprocessed row on the next pass
    assert state["writes"] == 0


def test_partial_query_page_is_unknown_not_empty(recovery, monkeypatch):
    state, _, _ = recovery
    monkeypatch.setattr(apis, "query_entities", lambda *a, **kw: {
        "entities": [{"entity_id": "ent_task"}], "total": 5, "has_more": False,
    })
    counts, next_offset = apis.sweep_missing_gate_checkpoints(apply=True)
    assert counts["unreconstructable"] == 1
    assert counts["scanned"] == 0
    assert next_offset == 0
    assert state["writes"] == 0


def test_one_shot_dry_run_reports_counts_without_titles(monkeypatch, capsys):
    called = []

    def sweep(**kwargs):
        called.append(kwargs["apply"])
        kwargs["candidates"].append({"task_id": "ent_task", "action": "would_raise_checkpoint"})
        return {"scanned": 1, "candidates": 1, "unreconstructable": 0}, 1

    monkeypatch.setattr(cli, "sweep_missing_gate_checkpoints", sweep)
    monkeypatch.setattr(sys, "argv", ["held_checkpoint_sweep.py", "--once", "--dry-run"])
    assert cli.main() == 0
    assert called == [False]
    output = capsys.readouterr().out
    assert '"candidates": 1' in output
    assert '"action": "would_raise_checkpoint"' in output
    assert "Generic task" not in output
