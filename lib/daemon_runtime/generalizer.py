"""
lib/daemon_runtime/generalizer.py — Autonomous, agent-local generalization.

This is the runtime that turns clustered `strategy_drift_signal` evidence into
standing behaviour, without a per-change human gate, while keeping the change
safe and reversible:

  • CONFIDENCE   — a cluster must reach the agent's `drift_signal_threshold`
                   (independent corroborations) before anything is created.
  • AGENT-LOCAL  — only `scope: agent` policies are auto-applied. Anything that
                   reads as domain/strategy/constitution-level is routed to a
                   `strategy_revision_proposal` (operator-gated) instead.
  • REVERSIBLE   — new policies land as `status: provisional`. They graduate to
                   `active` only by EXPOSURE (clean applications), not by a
                   clock; a single contradicting signal suspends them and
                   re-opens a proposal. An unused provisional policy is never
                   applied, so it carries no risk and needs no expiry.
  • NOTIFY       — every autonomous create/promote/suspend emits a daemon_report
                   so Onychomys/the operator sees what changed and why.
  • BLAST RADIUS — capped count of auto-policies per agent, and a hard refusal
                   to supersede any operator- or Columba-authored policy.

Decision logic is pure and unit-tested; Neotoma I/O mirrors participation.py
(plain httpx against /store, /retrieve_entities, /correct with idempotency).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import httpx

from .drift import DriftCluster, DriftSignal, contradicts

log = logging.getLogger("daemon_runtime.generalizer")

_BEARER_ENV = "NEOTOMA_BEARER_TOKEN"  # gitleaks:allow
NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
).rstrip("/")

# ── Tunables ──────────────────────────────────────────────────────────────────
# How many independent same-theme signals justify an autonomous policy. Read
# per-agent from agent_strategy.drift_signal_threshold; this is the fallback.
DEFAULT_DRIFT_THRESHOLD = 3
# Clean applications required to graduate provisional → active. Expressed as a
# multiple of the confidence threshold so "how sure we get" scales with "how
# sure we needed to be to start" — and so it tracks USE, never wall-clock time.
MATURATION_MULTIPLIER = 3
# Hard ceiling on simultaneously-live auto-policies per agent. Bounds blast
# radius: past the cap, further clusters become proposals, not auto-applies.
DEFAULT_POLICY_CAP_PER_AGENT = 8

# Markers that lift a signal above agent-local scope. Conservative by design:
# anything plausibly cross-cutting stays operator-gated rather than auto-applied.
_HIGHER_LAYER_MARKERS = frozenset(
    {
        "strategy", "roadmap", "pricing", "price", "revenue", "business",
        "north", "star", "hiring", "hire", "budget", "legal", "compliance",
        "architecture", "security", "brand", "positioning", "market",
        "policy", "constitution", "company", "founding", "principle",
        "cross-cutting", "swarm", "other agents", "everyone",
    }
)

AUTO_SUB = "generalizer@ateles-swarm"
OVERRIDABLE_BY = ["columba@ateles-swarm", "operator"]


class Action(str, Enum):
    """What the decision core wants done with a cluster."""

    AUTO_APPLY = "auto_apply"          # create provisional agent-local policy
    PROPOSE = "propose"                # create operator-gated revision proposal
    NOOP = "noop"                      # below threshold / capped / conflicting


@dataclass
class Decision:
    action: Action
    reason: str
    cluster: DriftCluster
    affects_higher_layer: bool = False


@dataclass
class PolicyState:
    """Maturation metadata carried in an agent_policy's JSON `notes` field."""

    auto_generated: bool = False
    application_count: int = 0
    contradiction_count: int = 0
    maturation_threshold: int = DEFAULT_DRIFT_THRESHOLD * MATURATION_MULTIPLIER
    drift_signal_refs: list[str] = field(default_factory=list)
    confirmed_at: str | None = None

    @classmethod
    def from_notes(cls, notes: str) -> "PolicyState":
        try:
            d = json.loads(notes) if notes else {}
        except (ValueError, TypeError):
            return cls()
        if not isinstance(d, dict):
            return cls()
        return cls(
            auto_generated=bool(d.get("auto_generated", False)),
            application_count=int(d.get("application_count", 0)),
            contradiction_count=int(d.get("contradiction_count", 0)),
            maturation_threshold=int(
                d.get("maturation_threshold", DEFAULT_DRIFT_THRESHOLD * MATURATION_MULTIPLIER)
            ),
            drift_signal_refs=list(d.get("drift_signal_refs", [])),
            confirmed_at=d.get("confirmed_at"),
        )

    def to_notes(self) -> str:
        return json.dumps(
            {
                "auto_generated": self.auto_generated,
                "application_count": self.application_count,
                "contradiction_count": self.contradiction_count,
                "maturation_threshold": self.maturation_threshold,
                "drift_signal_refs": self.drift_signal_refs,
                "confirmed_at": self.confirmed_at,
            }
        )


class Maturation(str, Enum):
    HOLD = "hold"        # still gathering exposure
    PROMOTE = "promote"  # enough clean applications → confirm (active)
    SUSPEND = "suspend"  # contradicted → suspend + re-open proposal


# ── Pure decision core (unit-tested, no I/O) ───────────────────────────────────


def affects_higher_layer(cluster: DriftCluster) -> bool:
    """
    True if the cluster's theme reads as domain/strategy/constitution-level and
    must therefore stay operator-gated rather than auto-applied agent-locally.
    """
    text = " ".join(s.text.lower() for s in cluster.signals)
    words = set(text.replace("/", " ").split())
    return bool(words & _HIGHER_LAYER_MARKERS) or "other agents" in text


def decide(
    cluster: DriftCluster,
    *,
    threshold: int,
    live_auto_policy_count: int,
    cap: int = DEFAULT_POLICY_CAP_PER_AGENT,
    conflicts_with_operator_policy: bool = False,
) -> Decision:
    """
    Decide what to do with one drift cluster. Pure function — all the swarm
    state it needs (threshold, current auto-policy count, operator-conflict)
    is passed in, so the policy is fully testable.
    """
    higher = affects_higher_layer(cluster)

    if cluster.size < threshold:
        return Decision(Action.NOOP, f"below threshold ({cluster.size}/{threshold})", cluster, higher)

    # Never autonomously overwrite a human decision: defer to the operator.
    if conflicts_with_operator_policy:
        return Decision(
            Action.PROPOSE,
            "conflicts with an operator/Columba-authored policy",
            cluster,
            higher,
        )

    # Cross-cutting concerns are out of the agent-local autonomy envelope.
    if higher:
        return Decision(
            Action.PROPOSE,
            "affects higher strategy layer — operator-gated",
            cluster,
            higher,
        )

    # Blast-radius cap: past the ceiling, surface as a proposal instead.
    if live_auto_policy_count >= cap:
        return Decision(
            Action.PROPOSE,
            f"auto-policy cap reached ({live_auto_policy_count}/{cap})",
            cluster,
            higher,
        )

    return Decision(Action.AUTO_APPLY, f"threshold met ({cluster.size}/{threshold})", cluster, higher)


def maturation_decision(state: PolicyState, new_contradiction: bool) -> Maturation:
    """
    Exposure-based lifecycle for a provisional policy. Confidence is a function
    of how many times the policy was actually exercised cleanly — not elapsed
    time — so a heavily-used policy graduates fast and a dormant one simply
    waits at zero risk.
    """
    if new_contradiction or state.contradiction_count > 0:
        return Maturation.SUSPEND
    if state.application_count >= state.maturation_threshold:
        return Maturation.PROMOTE
    return Maturation.HOLD


# ── Neotoma I/O (mirrors participation.py) ──────────────────────────────────────


def _bearer() -> str | None:
    return os.environ.get(_BEARER_ENV)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _headers(bearer: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}


async def _post(path: str, body: dict, bearer: str) -> dict | None:
    try:
        async with httpx.AsyncClient(headers=_headers(bearer), timeout=15) as client:
            resp = await client.post(f"{NEOTOMA_BASE_URL}/{path}", json=body)
            if resp.status_code >= 400:
                log.warning(f"{path} -> HTTP {resp.status_code}: {resp.text[:200]}")
                return None
            return resp.json()
    except Exception as exc:  # noqa: BLE001 — fire-and-forget like participation.py
        log.warning(f"{path} request failed: {exc}")
        return None


async def fetch_threshold(agent_sub: str, bearer: str) -> int:
    """Read drift_signal_threshold from the agent's agent_strategy, else default."""
    data = await _post(
        "retrieve_entities",
        {"entity_type": "agent_strategy", "limit": 50, "include_snapshots": True},
        bearer,
    )
    if not data:
        return DEFAULT_DRIFT_THRESHOLD
    for e in data.get("entities", []):
        snap = e.get("snapshot") or {}
        if snap.get("agent_sub") == agent_sub:
            t = snap.get("drift_signal_threshold")
            if isinstance(t, int) and t > 0:
                return t
    return DEFAULT_DRIFT_THRESHOLD


async def fetch_agent_policies(agent_sub: str, bearer: str) -> list[dict]:
    """Return snapshots of all non-retired agent_policy entities for an agent."""
    data = await _post(
        "retrieve_entities",
        {"entity_type": "agent_policy", "limit": 200, "include_snapshots": True},
        bearer,
    )
    out: list[dict] = []
    for e in (data or {}).get("entities", []):
        snap = e.get("snapshot") or {}
        if snap.get("agent_sub") != agent_sub:
            continue
        if snap.get("status") == "retired":
            continue
        snap["_entity_id"] = e.get("entity_id", "")
        out.append(snap)
    return out


def _is_operator_authored(policy: dict) -> bool:
    """A policy not flagged auto_generated is treated as human-authored."""
    return not PolicyState.from_notes(policy.get("notes", "")).auto_generated


def count_live_auto_policies(policies: list[dict]) -> int:
    return sum(
        1
        for p in policies
        if PolicyState.from_notes(p.get("notes", "")).auto_generated
        and p.get("status") in ("provisional", "active")
    )


def find_operator_conflict(cluster: DriftCluster, policies: list[dict]) -> bool:
    """True if any operator-authored policy shares this cluster's theme."""
    rep = cluster.representative_text
    for p in policies:
        if _is_operator_authored(p) and contradicts(
            DriftSignal(agent=cluster.agent, text=rep), p.get("rule", "") or p.get("description", "")
        ):
            return True
    return False


async def create_provisional_policy(cluster: DriftCluster, bearer: str) -> str | None:
    """Store a new agent-local agent_policy in `provisional` status."""
    threshold_state = PolicyState(
        auto_generated=True,
        drift_signal_refs=cluster.source_refs,
    )
    rule_text = cluster.representative_text
    payload = {
        "entity_type": "agent_policy",
        "scope": "agent",
        "agent_sub": cluster.agent if "@" in cluster.agent else f"{cluster.agent}@ateles-swarm",
        "rule_kind": "prefer",  # never auto-create deny/require
        "description": f"[auto] {rule_text}",
        "rule": rule_text,
        "overridable_by": ", ".join(OVERRIDABLE_BY),
        "status": "provisional",
        "effective_from": _now_iso(),
        "notes": threshold_state.to_notes(),
    }
    body = {
        "entities": [payload],
        "idempotency_key": f"auto-policy-{cluster.theme_key}",
    }
    data = await _post("store", body, bearer)
    eid = _first_entity_id(data)
    if eid:
        await emit_report(
            "info",
            f"auto-applied provisional policy for {cluster.agent}: {rule_text}",
            bearer,
            detail={"policy": eid, "evidence": cluster.size, "refs": cluster.source_refs},
        )
    return eid


async def create_revision_proposal(decision: Decision, bearer: str) -> str | None:
    """Store an operator-gated strategy_revision_proposal for a cluster."""
    cluster = decision.cluster
    agent_sub = cluster.agent if "@" in cluster.agent else f"{cluster.agent}@ateles-swarm"
    payload = {
        "entity_type": "strategy_revision_proposal",
        "proposing_agent_sub": agent_sub,
        "target_entity_id": agent_sub,
        "target_entity_type": "agent_definition",
        "proposed_at": _now_iso(),
        "summary": f"Recurring drift for {cluster.agent}: {cluster.representative_text}",
        "drift_signal_refs": cluster.source_refs,
        "proposed_change": cluster.representative_text,
        "affects_higher_layer": decision.affects_higher_layer,
        "operator_decision": "pending",
        "status": "pending",
    }
    body = {
        "entities": [payload],
        "idempotency_key": f"revision-proposal-{cluster.theme_key}",
    }
    data = await _post("store", body, bearer)
    eid = _first_entity_id(data)
    if eid:
        await emit_report(
            "info",
            f"opened revision proposal for {cluster.agent} ({decision.reason})",
            bearer,
            detail={"proposal": eid},
        )
    return eid


async def increment_application(policy: dict, bearer: str) -> None:
    """Record one clean application; promote to active once matured."""
    state = PolicyState.from_notes(policy.get("notes", ""))
    if not state.auto_generated:
        return
    state.application_count += 1
    decision = maturation_decision(state, new_contradiction=False)
    new_status = policy.get("status", "provisional")
    if decision == Maturation.PROMOTE and policy.get("status") != "active":
        new_status = "active"
        state.confirmed_at = _now_iso()
    await _correct_policy(policy["_entity_id"], state, new_status, bearer)
    if new_status == "active" and policy.get("status") != "active":
        await emit_report(
            "info",
            f"policy matured to active for {policy.get('agent_sub')}: {policy.get('rule')}",
            bearer,
            detail={"policy": policy["_entity_id"], "applications": state.application_count},
        )


async def register_contradiction(policy: dict, signal: DriftSignal, bearer: str) -> None:
    """Suspend a contradicted provisional/active auto-policy and re-open a proposal."""
    state = PolicyState.from_notes(policy.get("notes", ""))
    if not state.auto_generated:
        return
    state.contradiction_count += 1
    await _correct_policy(policy["_entity_id"], state, "suspended", bearer)
    await emit_report(
        "info",
        f"auto-policy suspended on contradiction for {policy.get('agent_sub')}: "
        f"{policy.get('rule')}",
        bearer,
        detail={"policy": policy["_entity_id"], "contradicting_signal": signal.text},
    )


async def _correct_policy(entity_id: str, state: PolicyState, status: str, bearer: str) -> None:
    """Two field corrections (notes + status) with idempotency keys."""
    day = datetime.now(UTC).strftime("%Y-%m-%d-%H%M%S")
    for field_name, value in (("notes", state.to_notes()), ("status", status)):
        await _post(
            "correct",
            {
                "entity_id": entity_id,
                "field": field_name,
                "value": value,
                "idempotency_key": f"policy-{field_name}-{entity_id}-{day}",
            },
            bearer,
        )


async def emit_report(
    severity: str, summary: str, bearer: str, detail: dict | None = None
) -> None:
    """
    Notify-on-every-change: write a daemon_report Anthus already surfaces to
    Onychomys. Autonomy with a paper trail — the operator sees each change.
    """
    payload = {
        "entity_type": "daemon_report",
        "daemon": "generalizer",
        "severity": severity,
        "summary": summary,
        "reported_at": _now_iso(),
    }
    if detail:
        payload["detail"] = json.dumps(detail)
    await _post(
        "store",
        {"entities": [payload], "idempotency_key": f"genreport-{_now_iso()}-{summary[:40]}"},
        bearer,
    )


def _first_entity_id(data: dict | None) -> str | None:
    if not data:
        return None
    ents = data.get("entities") or data.get("stored") or []
    if ents and isinstance(ents, list):
        first = ents[0]
        return first.get("entity_id") if isinstance(first, dict) else None
    return data.get("entity_id")


# ── High-level orchestration (called by Anthus) ─────────────────────────────────


async def process_signals(clusters: list[DriftCluster], bearer: str | None = None) -> list[Decision]:
    """
    Run the full agent-local generalization loop over a set of clusters.

    For each cluster: read the agent's threshold + existing policies, decide,
    and act (auto-apply provisional policy, or open an operator-gated proposal).
    Also feeds each cluster's signals through the contradiction check against
    that agent's live auto-policies, suspending any that a fresh signal reverses.

    Returns the decisions for logging/observability. Safe no-op without a token.
    """
    bearer = bearer or _bearer()
    decisions: list[Decision] = []
    if not bearer:
        log.debug("No Neotoma bearer token — generalizer is a no-op this run.")
        return decisions

    for cluster in clusters:
        agent_sub = cluster.agent if "@" in cluster.agent else f"{cluster.agent}@ateles-swarm"
        threshold = await fetch_threshold(agent_sub, bearer)
        policies = await fetch_agent_policies(agent_sub, bearer)

        # Contradiction sweep: any fresh signal that reverses a live auto-policy
        # suspends it (evidence-based revert, no timer involved).
        for pol in policies:
            if pol.get("status") not in ("provisional", "active"):
                continue
            if not PolicyState.from_notes(pol.get("notes", "")).auto_generated:
                continue
            for sig in cluster.signals:
                if contradicts(sig, pol.get("rule", "") or pol.get("description", "")):
                    await register_contradiction(pol, sig, bearer)
                    break

        decision = decide(
            cluster,
            threshold=threshold,
            live_auto_policy_count=count_live_auto_policies(policies),
            conflicts_with_operator_policy=find_operator_conflict(cluster, policies),
        )
        decisions.append(decision)

        if decision.action == Action.AUTO_APPLY:
            await create_provisional_policy(cluster, bearer)
        elif decision.action == Action.PROPOSE:
            await create_revision_proposal(decision, bearer)
    return decisions
