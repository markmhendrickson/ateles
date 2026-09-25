"""
lib/daemon_runtime/generalizer.py — Agent-local generalization, as proposals.

This is the runtime that turns clustered `strategy_drift_signal` evidence into
PROPOSED standing behaviour. It never writes a live `agent_policy` row
(operator ruling on ateles#1270, 2026-09-25): a rule proposal becomes a live
rule only when a different signed swarm identity approves it through a
checkpoint, plus the operator's approval when the rule reaches sessions. That
approval step is a follow-up; until it exists, proposals wait.

  • CONFIDENCE   — a cluster must reach the agent's `drift_signal_threshold`
                   (independent corroborations) before anything is proposed.
  • AGENT-LOCAL  — a cluster inside the agent-local envelope becomes an
                   `agent_policy` PROPOSAL (`strategy_revision_proposal` with
                   `target_entity_type: agent_policy`, the exact proposed row in
                   `proposed_change`). Anything that reads as
                   domain/strategy/constitution-level becomes a proposal against
                   the agent's `agent_definition` instead, as before.
  • CONTRADICTION — a signal that reverses a live auto-generated policy becomes
                   a proposal to suspend it, not a direct status write.
  • NOTIFY       — every proposal emits a daemon_report so Ateles/the operator
                   sees what was proposed and why.
  • BLAST RADIUS — capped count of auto-policies per agent, and a hard refusal
                   to propose superseding any operator- or Columba-authored
                   policy.

Writes go through `neotoma_signed.NeotomaWriter`, signing as Anthus (the daemon
this runs in) when `ATELES_SIGNED_WRITES_ANTHUS` is `shadow` or `on`; with the
switch unset they are bearer writes, as before. Reads stay on the bearer.
Decision logic is pure and unit-tested.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import httpx

try:  # package import (production) and bare import (in-dir pytest) both work
    from .drift import DriftCluster, DriftSignal, cluster_signals, contradicts
    from .agent_loader import policy_binds_agent
    from .neotoma_signed import NeotomaWriteError, NeotomaWriter
except ImportError:  # pragma: no cover
    from drift import DriftCluster, DriftSignal, cluster_signals, contradicts
    from agent_loader import policy_binds_agent
    from neotoma_signed import NeotomaWriteError, NeotomaWriter

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

# The generalizer runs inside Anthus, so it writes as Anthus: Anthus holds the
# key and the agent_grant. Its switch is ATELES_SIGNED_WRITES_ANTHUS.
WRITER_AGENT = "anthus"
PROPOSER_SUB = f"{WRITER_AGENT}@ateles-swarm"
PROPOSAL_ENTITY_TYPE = "strategy_revision_proposal"


class Action(str, Enum):
    """What the decision core wants done with a cluster."""

    PROPOSE_POLICY = "propose_policy"  # propose an agent-local agent_policy
    PROPOSE = "propose"                # propose a revision to the agent_definition
    NOOP = "noop"                      # below threshold / capped / conflicting


@dataclass
class Decision:
    action: Action
    reason: str
    cluster: DriftCluster
    affects_higher_layer: bool = False


@dataclass
class PolicyState:
    """Maturation metadata serialized to/from an agent_policy's JSON `body` field.

    (Method names use `notes` historically; they operate on a JSON string
    regardless of which schema field stores it. The store field is `body` —
    agent_policy v1.1.0 has no `notes` field.)
    """

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

    return Decision(
        Action.PROPOSE_POLICY, f"threshold met ({cluster.size}/{threshold})", cluster, higher
    )


def maturation_decision(state: PolicyState, new_contradiction: bool) -> Maturation:
    """
    Exposure-based lifecycle for a provisional policy. Confidence is a function
    of how many times the policy was actually exercised cleanly — not elapsed
    time — so a heavily-used policy graduates fast and a dormant one simply
    waits at zero risk.

    Pure and kept for the approval step to reuse: nothing in this module writes
    a promotion any more, because a promotion to `active` is a live-rule write
    and the generalizer only proposes (ateles#1270).
    """
    if new_contradiction or state.contradiction_count > 0:
        return Maturation.SUSPEND
    if state.application_count >= state.maturation_threshold:
        return Maturation.PROMOTE
    return Maturation.HOLD


# ── Neotoma I/O ─────────────────────────────────────────────────────────────────
# Reads go through `_post` on the bearer. Every write goes through `_write`, the
# signed client (neotoma_signed.NeotomaWriter).


def _bearer() -> str | None:
    return os.environ.get(_BEARER_ENV)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _headers(bearer: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"}


async def _post(path: str, body: dict, bearer: str) -> dict | None:
    """Bearer POST for READS only (`entities/query`). Writes use `_write`."""
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
        "entities/query",
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
        "entities/query",
        {"entity_type": "agent_policy", "limit": 200, "include_snapshots": True},
        bearer,
    )
    out: list[dict] = []
    for e in (data or {}).get("entities", []):
        snap = e.get("snapshot") or {}
        if not policy_binds_agent(snap, agent_sub):
            continue
        if snap.get("status") == "retired":
            continue
        snap["_entity_id"] = e.get("entity_id", "")
        out.append(snap)
    return out


def _is_operator_authored(policy: dict) -> bool:
    """A policy not flagged auto_generated is treated as human-authored."""
    return not PolicyState.from_notes(policy.get("body", "")).auto_generated


def count_live_auto_policies(policies: list[dict]) -> int:
    return sum(
        1
        for p in policies
        if PolicyState.from_notes(p.get("body", "")).auto_generated
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


def _writer(bearer: str) -> NeotomaWriter:
    """The one write client for everything the generalizer writes."""
    return NeotomaWriter(WRITER_AGENT, daemon=WRITER_AGENT, bearer=bearer)


async def _write(body: dict, bearer: str) -> dict | None:
    """Store through the signed client. Best-effort like `_post`, but loud.

    A refused write (including a signing failure once Anthus's switch is on)
    returns None after logging at ERROR, so nothing lands and the drift cluster
    stays as signals to be proposed again on a later tick.
    """
    try:
        return (await _writer(bearer).apost("store", body)).data
    except NeotomaWriteError as exc:
        if exc.status == 400 and "IDEMPOTENCY" in str(exc).upper():
            # The same proposal (same content digest) is already on record.
            log.debug(f"store: already recorded ({body.get('idempotency_key')})")
        else:
            log.error(f"store failed: {exc}")
        return None
    except Exception as exc:  # noqa: BLE001 — learning must never break dispatch
        log.error(f"store request failed: {exc}")
        return None


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _content_digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def proposed_policy_fields(cluster: DriftCluster) -> dict:
    """The exact agent_policy row this cluster would become if approved.

    Status is left out: the approval writes the row and decides its status.
    """
    rule_text = cluster.representative_text
    agent_sub = cluster.agent if "@" in cluster.agent else f"{cluster.agent}@ateles-swarm"
    return {
        "scope": "agent",
        "agent_sub": agent_sub,
        # `domain` is the SUBJECT a rule is about, for grouping rules a reader
        # selects together — "never an agent identifier, which is
        # `agent_sub`'s" (docs/foundation/data_model.md). Writing the agent id
        # here produced the two live rows ateles#1118 found smuggling
        # `<agent>@ateles-swarm` through `domain`. The cluster's own theme is
        # the actual subject.
        "domain": cluster.theme_key,
        # `rule_kind` is CLOSED to `mandatory` | `advisory`; absence or any
        # other value reads as `mandatory`, the restrictive branch. A generated
        # rule is explicitly `advisory`.
        "rule_kind": "advisory",
        "description": f"[auto] {rule_text}",
        "rule": rule_text,
        "overridable_by": ", ".join(OVERRIDABLE_BY),
        "body": PolicyState(
            auto_generated=True, drift_signal_refs=cluster.source_refs
        ).to_notes(),
    }


async def create_policy_proposal(cluster: DriftCluster, bearer: str) -> str | None:
    """Propose a new agent-local agent_policy. Never writes agent_policy itself.

    Reuses `strategy_revision_proposal` with `target_entity_type: agent_policy`.
    `proposed_change` is the canonical JSON of the proposed row, so an approver
    approves exact content and can hash it. `target_entity_id` is the agent the
    rule would bind, the same convention the agent_definition proposals use.
    The idempotency key carries the content digest, so re-seeing the same
    cluster on a later tick does not open a second proposal.
    """
    fields = proposed_policy_fields(cluster)
    change = {"op": "create", "entity_type": "agent_policy", "fields": fields}
    digest = _content_digest(change)
    payload = {
        "entity_type": PROPOSAL_ENTITY_TYPE,
        "proposing_agent_sub": PROPOSER_SUB,
        "target_entity_id": fields["agent_sub"],
        "target_entity_type": "agent_policy",
        "proposed_at": _now_iso(),
        "summary": f"Proposed agent_policy for {fields['agent_sub']}: {fields['rule']}",
        "drift_signal_refs": cluster.source_refs,
        "proposed_change": _canonical(change),
        "operator_decision": "pending",
        "status": "pending",
    }
    body = {
        "entities": [payload],
        "idempotency_key": f"policy-proposal-{digest[:32]}",
        "strict": True,
    }
    data = await _write(body, bearer)
    eid = _first_entity_id(data)
    if eid:
        await emit_report(
            "info",
            f"proposed agent_policy for {cluster.agent}: {fields['rule']}",
            bearer,
            detail={"proposal": eid, "evidence": cluster.size, "refs": cluster.source_refs},
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
        "strict": True,
    }
    data = await _write(body, bearer)
    eid = _first_entity_id(data)
    if eid:
        await emit_report(
            "info",
            f"opened revision proposal for {cluster.agent} ({decision.reason})",
            bearer,
            detail={"proposal": eid},
        )
    return eid


async def register_contradiction(policy: dict, signal: DriftSignal, bearer: str) -> str | None:
    """Propose suspending a contradicted live auto-policy. Never writes agent_policy.

    The proposal names the policy by entity id and carries the exact change
    (`status: suspended`) in `proposed_change`. One proposal per policy: the
    idempotency key is the policy id plus the content digest.
    """
    state = PolicyState.from_notes(policy.get("body", ""))
    if not state.auto_generated:
        return None
    entity_id = policy["_entity_id"]
    change = {
        "op": "correct",
        "entity_type": "agent_policy",
        "entity_id": entity_id,
        "fields": {"status": "suspended"},
    }
    payload = {
        "entity_type": PROPOSAL_ENTITY_TYPE,
        "proposing_agent_sub": PROPOSER_SUB,
        "target_entity_id": entity_id,
        "target_entity_type": "agent_policy",
        "proposed_at": _now_iso(),
        "summary": (
            f"Suspend auto-policy for {policy.get('agent_sub')} on a contradicting "
            f"signal: {signal.text}"
        ),
        "drift_signal_refs": [signal.source_ref] if signal.source_ref else [],
        "proposed_change": _canonical(change),
        "operator_decision": "pending",
        "status": "pending",
    }
    data = await _write(
        {
            "entities": [payload],
            "idempotency_key": f"policy-suspend-{entity_id}-{_content_digest(change)[:16]}",
            "strict": True,
        },
        bearer,
    )
    eid = _first_entity_id(data)
    if eid:
        await emit_report(
            "info",
            f"proposed suspending auto-policy for {policy.get('agent_sub')}: "
            f"{policy.get('rule')}",
            bearer,
            detail={"policy": entity_id, "proposal": eid, "contradicting_signal": signal.text},
        )
    return eid


async def emit_report(
    severity: str, summary: str, bearer: str, detail: dict | None = None
) -> None:
    """
    Notify-on-every-change: write a daemon_report Anthus already surfaces to
    Ateles. Autonomy with a paper trail — the operator sees each change.
    Fields match the canonical daemon_report schema (daemon_name/message/details).
    """
    payload = {
        "entity_type": "daemon_report",
        "daemon_name": "generalizer",
        "aauth_sub": AUTO_SUB,
        "severity": severity,
        "message": summary,
        "report_at": _now_iso(),
    }
    if detail:
        payload["details"] = json.dumps(detail)
    await _write(
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


# ── Drift-signal persistence (durable accumulation across time/work entities) ───

SIGNAL_ENTITY_TYPE = "strategy_drift_signal"


def signal_to_entity(signal: DriftSignal) -> dict:
    """
    Map a parsed DriftSignal to the canonical strategy_drift_signal schema
    (emitting_agent, observation, severity required; work_entity_id optional).
    """
    agent_sub = signal.agent if "@" in signal.agent else f"{signal.agent}@ateles-swarm"
    return {
        "entity_type": SIGNAL_ENTITY_TYPE,
        "emitting_agent": agent_sub,
        "observation": signal.text,
        "severity": "info",
        "work_entity_id": signal.source_ref,
    }


async def persist_signals(signals: list[DriftSignal], bearer: str) -> None:
    """
    Store each fresh drift signal idempotently. The idempotency key folds in the
    source_ref so the same comment line re-seen on a later tick is not counted
    twice toward a threshold — accumulation must reflect independent occurrences.
    """
    for s in signals:
        payload = signal_to_entity(s)
        key = f"drift-{s.theme_key}-{s.source_ref or s.text[:32]}"
        await _write(
            # strict: keep each signal a distinct row (the schema has no
            # canonical_name_fields, so without this they'd all coalesce into
            # one per-agent entity and occurrence counts would collapse).
            {"entities": [payload], "idempotency_key": key, "strict": True},
            bearer,
        )


async def fetch_recent_signals(
    agent_sub: str, bearer: str, limit: int = 300
) -> list[DriftSignal]:
    """
    Pull this agent's drift signals so clustering can accumulate evidence across
    many work entities and over time — not just within a single issue's comments.
    Reconstructs DriftSignal objects (theme_key recomputed from the observation
    text, keeping the fingerprint authoritative).
    """
    data = await _post(
        "entities/query",
        {"entity_type": SIGNAL_ENTITY_TYPE, "limit": limit, "include_snapshots": True},
        bearer,
    )
    out: list[DriftSignal] = []
    for e in (data or {}).get("entities", []):
        snap = e.get("snapshot") or {}
        if snap.get("emitting_agent") != agent_sub:
            continue
        text = snap.get("observation", "")
        if not text:
            continue
        agent = agent_sub.split("@")[0]
        out.append(
            DriftSignal(agent=agent, text=text, source_ref=snap.get("work_entity_id", ""))
        )
    return out


# ── High-level orchestration (called by Anthus) ─────────────────────────────────


async def _contradiction_sweep(
    cluster: DriftCluster, policies: list[dict], bearer: str
) -> None:
    """Suspend any live auto-policy a fresh signal in this cluster reverses."""
    for pol in policies:
        if pol.get("status") not in ("provisional", "active"):
            continue
        if not PolicyState.from_notes(pol.get("body", "")).auto_generated:
            continue
        for sig in cluster.signals:
            if contradicts(sig, pol.get("rule", "") or pol.get("description", "")):
                await register_contradiction(pol, sig, bearer)
                break


async def _act_on_cluster(
    cluster: DriftCluster, policies: list[dict], threshold: int, bearer: str
) -> Decision:
    """Run the contradiction sweep, decide, and act for a single cluster."""
    await _contradiction_sweep(cluster, policies, bearer)
    decision = decide(
        cluster,
        threshold=threshold,
        live_auto_policy_count=count_live_auto_policies(policies),
        conflicts_with_operator_policy=find_operator_conflict(cluster, policies),
    )
    if decision.action == Action.PROPOSE_POLICY:
        await create_policy_proposal(cluster, bearer)
    elif decision.action == Action.PROPOSE:
        await create_revision_proposal(decision, bearer)
    return decision


async def process_signals(
    clusters: list[DriftCluster], bearer: str | None = None
) -> list[Decision]:
    """
    Lower-level entrypoint: act on a set of already-built clusters. Fetches each
    agent's threshold + policies once and decides per cluster. Safe no-op without
    a token. (Anthus uses `harvest`, which persists + corpus-clusters first.)
    """
    bearer = bearer or _bearer()
    decisions: list[Decision] = []
    if not bearer:
        log.debug("No Neotoma bearer token — generalizer is a no-op this run.")
        return decisions

    policies_cache: dict[str, list[dict]] = {}
    threshold_cache: dict[str, int] = {}
    for cluster in clusters:
        agent_sub = cluster.agent if "@" in cluster.agent else f"{cluster.agent}@ateles-swarm"
        if agent_sub not in policies_cache:
            policies_cache[agent_sub] = await fetch_agent_policies(agent_sub, bearer)
            threshold_cache[agent_sub] = await fetch_threshold(agent_sub, bearer)
        decisions.append(
            await _act_on_cluster(
                cluster, policies_cache[agent_sub], threshold_cache[agent_sub], bearer
            )
        )
    return decisions


async def harvest(
    fresh_signals: list[DriftSignal], bearer: str | None = None
) -> list[Decision]:
    """
    Top-level loop Anthus calls each tick:

      1. persist the freshly-seen signals durably (idempotent)
      2. for each agent that emitted one, pull its full open-signal corpus
      3. cluster the corpus and act per cluster

    This is what lets evidence accumulate across many work entities and over
    time toward an agent's threshold, rather than only within one issue's
    comments. Safe no-op without a token.
    """
    bearer = bearer or _bearer()
    if not bearer or not fresh_signals:
        return []

    await persist_signals(fresh_signals, bearer)

    touched = {
        (s.agent if "@" in s.agent else f"{s.agent}@ateles-swarm") for s in fresh_signals
    }
    decisions: list[Decision] = []
    for agent_sub in touched:
        corpus = await fetch_recent_signals(agent_sub, bearer)
        if not corpus:
            continue
        threshold = await fetch_threshold(agent_sub, bearer)
        policies = await fetch_agent_policies(agent_sub, bearer)
        for cluster in cluster_signals(corpus):
            decisions.append(await _act_on_cluster(cluster, policies, threshold, bearer))
    return decisions
