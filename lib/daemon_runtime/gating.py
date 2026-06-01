"""
lib/daemon_runtime/gating.py — Confidence × blast-radius execution gate.

Implements the swarm execution-gating doctrine (see Neotoma execution_policy
ent_dfce6edecefe3eb7fc9e0337 and confidence_rubric ent_22fd6f25159f1f2689726780)
as enforceable code for daemons that dispatch or execute tasks.

The gate is two-axis:

    confidence (0..1, agent self-scored)  ×  blast_radius (low | high)

    high confidence + low blast   → AUTO_EXECUTE
    everything else               → CHECKPOINT (blocking PLAN; await operator)

Per-agent overrides (e.g. Monedula-strict ent_c7f81385afbd993db3dd11ff) pin
financial actions to always-checkpoint by setting confidence_threshold=1.0 and
blast_radius_default=high.

Recurring tasks earn autonomy: after `auto_execute_after_n_successful_recurrences`
clean cycles of the same recurrence series, a below-threshold confidence may still
auto-execute (the prior_executions_successful rubric signal).

This module is intentionally dependency-light: it reuses the httpx + bearer-token
pattern from agent_loader and never raises on Neotoma being unreachable — it
fails CLOSED (defaults to CHECKPOINT) so a missing policy never lets a high-blast
action through unreviewed.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from enum import Enum

import httpx

log = logging.getLogger(__name__)

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

# Canonical default policy + rubric (overridable via env for tests / forks)
DEFAULT_POLICY_ID = os.environ.get(
    "EXECUTION_POLICY_DEFAULT_ID", "ent_dfce6edecefe3eb7fc9e0337"
)
CONFIDENCE_RUBRIC_ID = os.environ.get(
    "CONFIDENCE_RUBRIC_ID", "ent_22fd6f25159f1f2689726780"
)

# Conservative fallbacks used only when the policy entity can't be loaded.
_FALLBACK_THRESHOLD = 0.85
# Action types treated as high blast radius when a policy doesn't say otherwise.
_FALLBACK_HIGH_BLAST = frozenset(
    {
        "git_push",
        "open_or_merge_pr",
        "open_pr",
        "merge_pr",
        "payment",
        "transfer",
        "wage",
        "invoice_pay",
        "send_external_comms",
        "publish",
        "release",
        "delete_entity_or_data",
        "external_api_write",
    }
)


class BlastRadius(str, Enum):
    LOW = "low"
    HIGH = "high"


class GateAction(str, Enum):
    AUTO_EXECUTE = "auto_execute"
    CHECKPOINT = "checkpoint_plan_approval"
    CHECKPOINT_WITH_ALTERNATIVES = "checkpoint_plan_approval_with_alternatives"


@dataclass
class ExecutionPolicy:
    """Subset of an execution_policy entity relevant to the runtime gate."""

    entity_id: str = ""
    title: str = ""
    confidence_threshold: float = _FALLBACK_THRESHOLD
    blast_radius_default: BlastRadius = BlastRadius.LOW
    auto_execute_after_n_successful_recurrences: int | None = None
    high_blast_action_types: frozenset[str] = field(
        default_factory=lambda: frozenset(_FALLBACK_HIGH_BLAST)
    )
    low_blast_action_types: frozenset[str] = field(default_factory=frozenset)
    loaded: bool = False  # False = using fallbacks (Neotoma unreachable)

    def blast_radius_for(self, action_type: str | None) -> BlastRadius:
        """Classify an action type's blast radius under this policy."""
        if action_type:
            at = action_type.strip().lower()
            if at in self.low_blast_action_types:
                return BlastRadius.LOW
            if at in self.high_blast_action_types:
                return BlastRadius.HIGH
        # Unknown action type → fall back to the policy's default.
        return self.blast_radius_default


@dataclass
class GateDecision:
    action: GateAction
    blast_radius: BlastRadius
    confidence: float
    threshold: float
    policy_id: str
    reason: str

    @property
    def may_auto_execute(self) -> bool:
        return self.action == GateAction.AUTO_EXECUTE


def _parse_policy(entity_id: str, data: dict) -> ExecutionPolicy:
    snap = (data.get("snapshot") or {}).get("snapshot") or data.get("snapshot") or data

    def _as_set(v) -> frozenset[str]:
        if isinstance(v, str):
            import json as _json

            try:
                v = _json.loads(v)
            except (ValueError, TypeError):
                return frozenset()
        if isinstance(v, list):
            return frozenset(str(x).strip().lower() for x in v if x)
        return frozenset()

    threshold = snap.get("confidence_threshold", _FALLBACK_THRESHOLD)
    try:
        threshold = float(threshold)
    except (TypeError, ValueError):
        threshold = _FALLBACK_THRESHOLD

    radius_default = str(snap.get("blast_radius_default", "low")).strip().lower()
    default_br = BlastRadius.HIGH if radius_default == "high" else BlastRadius.LOW

    n_recur = snap.get("auto_execute_after_n_successful_recurrences")
    try:
        n_recur = int(n_recur) if n_recur is not None else None
    except (TypeError, ValueError):
        n_recur = None

    high = _as_set(snap.get("high_blast_action_types")) or frozenset(_FALLBACK_HIGH_BLAST)
    low = _as_set(snap.get("low_blast_action_types"))

    return ExecutionPolicy(
        entity_id=entity_id,
        title=str(snap.get("title", "")),
        confidence_threshold=threshold,
        blast_radius_default=default_br,
        auto_execute_after_n_successful_recurrences=n_recur,
        high_blast_action_types=high,
        low_blast_action_types=low,
        loaded=True,
    )


def _fetch_entity(entity_id: str) -> dict | None:
    if not NEOTOMA_BEARER_TOKEN:
        return None
    url = f"{NEOTOMA_BASE_URL}/entities/{entity_id}"
    try:
        resp = httpx.get(
            url,
            headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:  # noqa: BLE001 — fail closed, never crash dispatch
        log.warning(f"[gating] could not fetch policy {entity_id}: {exc}")
        return None


def load_policy(policy_id: str | None = None) -> ExecutionPolicy:
    """
    Load an execution_policy entity. Returns a fallback policy (loaded=False)
    when Neotoma is unreachable, so the gate still functions (fails closed).
    """
    pid = policy_id or DEFAULT_POLICY_ID
    data = _fetch_entity(pid)
    if data is None:
        log.warning(
            f"[gating] policy {pid} unavailable — using conservative fallback "
            "(threshold=%.2f, default blast=low, unknown actions→default)",
            _FALLBACK_THRESHOLD,
        )
        return ExecutionPolicy(entity_id=pid, loaded=False)
    return _parse_policy(pid, data)


def resolve_policy_for_agent(
    assigned_to: str | None,
    *,
    agent_policy_overrides: dict[str, str] | None = None,
) -> ExecutionPolicy:
    """
    Resolve the policy that governs a task assigned to `assigned_to`.

    `agent_policy_overrides` maps agent name → override execution_policy entity id.
    Monedula is wired by default to its strict override; callers may pass their
    own map (e.g. loaded from Neotoma) to avoid hardcoding.
    """
    overrides = {
        "monedula": os.environ.get(
            "MONEDULA_POLICY_ID", "ent_c7f81385afbd993db3dd11ff"
        ),
    }
    if agent_policy_overrides:
        overrides.update({k.lower(): v for k, v in agent_policy_overrides.items()})

    if assigned_to and assigned_to.strip().lower() in overrides:
        return load_policy(overrides[assigned_to.strip().lower()])
    return load_policy(DEFAULT_POLICY_ID)


def evaluate_gate(
    *,
    confidence: float,
    action_type: str | None,
    policy: ExecutionPolicy,
    successful_recurrences: int = 0,
) -> GateDecision:
    """
    Apply the gate matrix. Returns a GateDecision.

    Auto-execute requires high confidence AND low blast radius — UNLESS the task
    is a recurring series that has cleared the policy's recurrence-graduation
    count (and the policy enables graduation, i.e. n is not None).
    """
    blast = policy.blast_radius_for(action_type)
    threshold = policy.confidence_threshold
    high_conf = confidence >= threshold

    # Recurrence graduation: a proven recurring series may auto-execute below
    # threshold, but ONLY when the policy enables it (n is not None) and the
    # action is not high blast (money/publish never graduate — those policies
    # set n=None).
    graduated = (
        policy.auto_execute_after_n_successful_recurrences is not None
        and successful_recurrences
        >= policy.auto_execute_after_n_successful_recurrences
        and blast == BlastRadius.LOW
    )

    if blast == BlastRadius.LOW and (high_conf or graduated):
        reason = (
            "high confidence, low blast radius"
            if high_conf
            else f"recurrence-graduated ({successful_recurrences} clean cycles)"
        )
        return GateDecision(
            action=GateAction.AUTO_EXECUTE,
            blast_radius=blast,
            confidence=confidence,
            threshold=threshold,
            policy_id=policy.entity_id,
            reason=reason,
        )

    # Otherwise: checkpoint. Low-confidence + high-blast also proposes alternatives.
    if not high_conf and blast == BlastRadius.HIGH:
        return GateDecision(
            action=GateAction.CHECKPOINT_WITH_ALTERNATIVES,
            blast_radius=blast,
            confidence=confidence,
            threshold=threshold,
            policy_id=policy.entity_id,
            reason="low confidence and high blast radius — propose alternatives",
        )

    reason = (
        "high blast radius — operator approval required"
        if blast == BlastRadius.HIGH
        else "below confidence threshold"
    )
    return GateDecision(
        action=GateAction.CHECKPOINT,
        blast_radius=blast,
        confidence=confidence,
        threshold=threshold,
        policy_id=policy.entity_id,
        reason=reason,
    )


def write_checkpoint_brief(
    *,
    task_entity_id: str,
    decision: GateDecision,
    title: str,
    plan_summary: str,
    handler: str,
    alternatives: list[str] | None = None,
) -> str | None:
    """
    Store a blocking checkpoint_brief entity in Neotoma and link it to the task.

    Returns the new entity_id, or None if Neotoma is unreachable (caller should
    still NOT execute — the gate already decided CHECKPOINT).
    """
    if not NEOTOMA_BEARER_TOKEN:
        log.warning("[gating] no bearer token — checkpoint_brief not persisted")
        return None

    body = {
        "entities": [
            {
                "entity_type": "checkpoint_brief",
                "checkpoint_name": "PLAN",
                "blocking": True,
                "task_entity_id": task_entity_id,
                "title": f"PLAN checkpoint: {title}",
                "plan_summary": plan_summary,
                "confidence": decision.confidence,
                "confidence_threshold": decision.threshold,
                "blast_radius": decision.blast_radius.value,
                "gate_action": decision.action.value,
                "reason": decision.reason,
                "policy_entity_id": decision.policy_id,
                "proposed_alternatives": alternatives or [],
                "status": "awaiting_operator",
                "handler": handler,
            }
        ],
        "relationships": [
            {
                "relationship_type": "REFERS_TO",
                "source_index": 0,
                "target_entity_id": task_entity_id,
            }
        ],
        "idempotency_key": f"checkpoint-{handler}-{task_entity_id}-plan",
    }
    try:
        resp = httpx.post(
            f"{NEOTOMA_BASE_URL}/store",
            headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        ents = data.get("entities") or []
        return ents[0].get("entity_id") if ents else None
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[gating] failed to persist checkpoint_brief: {exc}")
        return None
