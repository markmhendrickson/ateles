"""
Tests for action_type inference and gate interaction (Apis dispatch gate).

Run with: pytest execution/daemons/apis/test_action_type.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

from apis import _infer_action_type, _read_confidence  # noqa: E402
from lib.daemon_runtime.gating import (  # noqa: E402
    ExecutionPolicy,
    GateAction,
    evaluate_gate,
)


def test_explicit_action_type_wins_over_agent_default() -> None:
    snap = {"action_type": "open_or_merge_pr"}
    assert _infer_action_type("cicada", snap) == "open_or_merge_pr"


def test_generalist_without_explicit_action_type_gets_low_ceiling() -> None:
    assert _infer_action_type("cicada", {}) == "compute_only_analysis"


def test_specialist_without_explicit_action_type_unchanged() -> None:
    assert _infer_action_type("monedula", {}) == "payment"
    assert _infer_action_type("struthio", {}) == "publish"
    assert _infer_action_type("corvus", {}) == "send_external_comms"


def test_read_only_cicada_task_auto_executes_with_confidence() -> None:
    """Effect: read-only analysis routed to cicada dispatches without checkpoint."""
    policy = ExecutionPolicy(entity_id="default", loaded=False)
    action = _infer_action_type("cicada", {})
    confidence = _read_confidence({"confidence": 0.9})
    decision = evaluate_gate(
        confidence=confidence, action_type=action, policy=policy
    )
    assert action == "compute_only_analysis"
    assert decision.action == GateAction.AUTO_EXECUTE


def test_pr_task_still_checkpoints() -> None:
    """Effect: explicit PR work checkpoints even with high confidence."""
    policy = ExecutionPolicy(entity_id="default", loaded=False)
    action = _infer_action_type(
        "cicada", {"action_type": "open_or_merge_pr", "confidence": 0.99}
    )
    confidence = _read_confidence({"action_type": "open_or_merge_pr", "confidence": 0.99})
    decision = evaluate_gate(
        confidence=confidence, action_type=action, policy=policy
    )
    assert action == "open_or_merge_pr"
    assert decision.action != GateAction.AUTO_EXECUTE


def test_missing_confidence_fails_closed_for_inferred_pr_action() -> None:
    """Explicit high-blast action with no confidence still checkpoints."""
    policy = ExecutionPolicy(entity_id="default", loaded=False)
    snap = {"action_type": "open_or_merge_pr"}
    decision = evaluate_gate(
        confidence=_read_confidence(snap),
        action_type=_infer_action_type("cicada", snap),
        policy=policy,
    )
    assert decision.action == GateAction.CHECKPOINT_WITH_ALTERNATIVES
