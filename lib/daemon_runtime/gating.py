"""
lib/daemon_runtime/gating.py — Confidence × blast-radius execution gate.

Implements the swarm execution-gating doctrine (see Neotoma execution_policy
ent_dfce6edecefe3eb7fc9e0337 and confidence_rubric ent_22fd6f25159f1f2689726780)
as enforceable code for daemons that dispatch or execute tasks.

The gate is two-axis:

    confidence (0..1, agent self-scored)  ×  blast_radius (low | high | never)

    high confidence + low blast   → AUTO_EXECUTE
    everything else               → CHECKPOINT (blocking PLAN; await operator)

``BlastRadius.NEVER`` is a third tier, not a louder HIGH. HIGH means "risky
enough that a human should look"; a HIGH action can still auto-execute once a
recurring series clears ``auto_execute_after_n_successful_recurrences``. NEVER
means **an agent structurally cannot do this** — the swarm does not hold the
credential, the consent, or the standing to act. No number of prior successes
changes that, so NEVER bypasses the recurrence path entirely rather than
scoring above a threshold (ateles#715).

Two action types resolve to NEVER:

  * ``operator_only`` — the task schema's own marking for consent-gated and
    irreversible-outward work.
  * any action type present in neither ``low_blast_action_types`` nor
    ``high_blast_action_types``, when the policy's ``blast_radius_default``
    has not been widened. An unclassified action type is a missing
    classification, not a safe one; it logs a warning naming the value.

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
import hashlib
import json
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
        # Defense in depth (ateles#715). `operator_only` is resolved to
        # BlastRadius.NEVER before these sets are consulted, so this entry is
        # unreachable in normal operation. It is here so that a future refactor
        # which loses the NEVER tier degrades this action type to HIGH rather
        # than back to the LOW default that caused the original fail-open.
        "operator_only",
    }
)

# Action types treated as LOW blast when a policy can't be loaded. Mirrors the
# default execution_policy entity's `low_blast_action_types`.
#
# Before ateles#715 this set did not exist: the fallback policy carried an
# EMPTY low set and relied on `blast_radius_default` = LOW to wave everything
# through. Once an unclassified action type stopped defaulting to LOW, that
# emptiness would have failed *every* action closed whenever Neotoma was
# unreachable — turning a policy-fetch blip into a swarm-wide halt. Naming the
# genuinely-low actions keeps the fallback conservative about the unknown
# without being useless about the known.
_FALLBACK_LOW_BLAST = frozenset(
    {
        "local_edit",
        "draft",
        "neotoma_read",
        "neotoma_internal_entity_update",
        "compute_only_analysis",
    }
)


# Action types that an agent structurally cannot perform, regardless of
# confidence or prior successes. These resolve to BlastRadius.NEVER, which the
# gate refuses to auto-execute on every path including recurrence graduation.
#
# `operator_only` is the task schema's marking for consent-gated and
# irreversible-outward work (credential handling, payments, external comms).
# Before ateles#715 it was in neither blast set, so `blast_radius_for()` fell
# through to `blast_radius_default` = LOW and the field contributed nothing —
# the more confident an authoring agent was that a task needed a human, the
# more certainly it auto-executed.
NEVER_AUTO_EXECUTE_ACTION_TYPES = frozenset({"operator_only"})


class BlastRadius(str, Enum):
    LOW = "low"
    HIGH = "high"
    #: Never auto-executable. Not "very high" — a distinct tier that no
    #: confidence score and no number of clean recurrences can clear.
    NEVER = "never"


class CheckpointPosture(str, Enum):
    """Per-boundary posture when a checkpoint's *check itself* errors out
    (exception, timeout, unloadable policy/deps, malformed response) — NOT
    the same as the check running cleanly and returning "blocked".

    OPEN   — proceed, but always log (never a silent degrade).
    CLOSED — block, file a checkpoint_brief, notify, and wait for an
             operator verdict (never a silent hang, never a silent proceed).

    Absent from a policy's ``checkpoint_postures`` map == OPEN (additive;
    matches today's behavior with zero config, per ateles#350).
    """

    OPEN = "open"
    CLOSED = "closed"


# Boundaries that are fail-closed by default when a policy doesn't override
# them via ``checkpoint_postures``. Per ateles#350 (PM ent_9fad4e409e3fe3ed48e0b599,
# arch ADR ent_7ea55d51d88553f358bc29f0): pre_merge is included because
# `_required_ci_state`'s live call site (execution/daemons/apis/swarm_dispatch.py
# — the pre-merge CI-status check) reaches merged code under
# APIS_AUTONOMY_AUTO_MERGE=1, so fail-open there is unacceptable.
# Session-integrity hooks are deliberately NOT in this set — they stay open.
DEFAULT_CLOSED_BOUNDARIES = frozenset(
    {
        "pre_payment",
        "pre_release",
        "pre_comms",
        "pre_irreversible",
        "pre_merge",
    }
)


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
    low_blast_action_types: frozenset[str] = field(
        default_factory=lambda: frozenset(_FALLBACK_LOW_BLAST)
    )
    checkpoint_postures: dict[str, CheckpointPosture] = field(default_factory=dict)
    loaded: bool = False  # False = using fallbacks (Neotoma unreachable)
    # Canonical content/revision fingerprint of the entity record used to make
    # the gate decision.  Checkpoint release re-reads the policy and requires
    # this exact value so a same-id policy edit cannot inherit old authority.
    authorization_revision: str = ""

    def blast_radius_for(self, action_type: str | None) -> BlastRadius:
        """Classify an action type's blast radius under this policy.

        Resolution order (ateles#715):

        1. ``NEVER_AUTO_EXECUTE_ACTION_TYPES`` wins over every policy set. A
           policy cannot demote ``operator_only`` by listing it as low blast —
           that is not a tuning knob, it is the marking that says an agent
           structurally cannot do the work.
        2. Explicit ``low_blast_action_types`` / ``high_blast_action_types``.
        3. An action type in *neither* set is **unrecognized**: it logs a
           warning naming the value and resolves to NEVER — never silently to
           LOW, and not to ``blast_radius_default`` either, since that default
           is what produced the ateles#715 fail-open. A missing classification
           is a gap in the policy, and the safe reading of a gap is that
           nobody has vouched for this action being agent-executable.
        4. Absent/empty ``action_type`` keeps the policy default — that is the
           "nothing was declared" case the default exists for, distinct from
           "something was declared and nobody classified it".
        """
        if action_type:
            at = action_type.strip().lower()
            if at in NEVER_AUTO_EXECUTE_ACTION_TYPES:
                return BlastRadius.NEVER
            if at in self.low_blast_action_types:
                return BlastRadius.LOW
            if at in self.high_blast_action_types:
                return BlastRadius.HIGH
            # Declared, but classified by neither set. Fail closed and say so.
            log.warning(
                "[gating] unrecognized action_type %r under policy %r — "
                "present in neither low_blast_action_types nor "
                "high_blast_action_types; treating as never-auto-executable. "
                "Add it to one of the policy's action-type sets to classify it.",
                at,
                self.entity_id or "(fallback)",
            )
            return BlastRadius.NEVER
        # No action type declared at all → the policy's default.
        return self.blast_radius_default

    def posture_for(self, boundary: str | None) -> CheckpointPosture:
        """Resolve a checkpoint boundary's on-check-failure posture.

        Explicit ``checkpoint_postures[boundary]`` wins. Otherwise a
        boundary in ``DEFAULT_CLOSED_BOUNDARIES`` is CLOSED; every other
        (including unrecognized/absent) boundary is OPEN — additive, no
        behavior change for boundaries nobody has configured (ateles#350).
        """
        if boundary:
            b = boundary.strip().lower()
            if b in self.checkpoint_postures:
                return self.checkpoint_postures[b]
            if b in DEFAULT_CLOSED_BOUNDARIES:
                return CheckpointPosture.CLOSED
        return CheckpointPosture.OPEN


@dataclass
class GateDecision:
    action: GateAction
    blast_radius: BlastRadius
    confidence: float
    threshold: float
    policy_id: str
    reason: str
    # True when `confidence` is the fail-closed default because no producer
    # ever scored this task (ateles#902), as opposed to a real score that
    # happens to be low. Callers must not let the two read the same: an
    # unscored task says so; a scored-low task keeps saying "low confidence".
    confidence_unscored: bool = False

    @property
    def may_auto_execute(self) -> bool:
        """True only for an AUTO_EXECUTE decision that is not never-tier.

        The blast-radius clause is redundant against `evaluate_gate`, which
        short-circuits NEVER before it can produce AUTO_EXECUTE. It is kept as
        a belt-and-braces check on the value callers actually branch on, so a
        hand-constructed or future-refactored GateDecision cannot present a
        never-auto-executable action as dispatchable (ateles#715).
        """
        return (
            self.action == GateAction.AUTO_EXECUTE
            and self.blast_radius != BlastRadius.NEVER
        )


def _canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def entity_record_digest(record: dict) -> str:
    """Fingerprint a complete Neotoma entity revision deterministically."""
    return hashlib.sha256(_canonical_json(record).encode()).hexdigest()


def execution_policy_revision(policy: ExecutionPolicy) -> str:
    """Return the loaded entity revision, or a deterministic fallback revision."""
    if policy.authorization_revision:
        return policy.authorization_revision
    content = {
        "entity_id": policy.entity_id,
        "title": policy.title,
        "confidence_threshold": policy.confidence_threshold,
        "blast_radius_default": policy.blast_radius_default.value,
        "auto_execute_after_n_successful_recurrences": (
            policy.auto_execute_after_n_successful_recurrences
        ),
        "high_blast_action_types": sorted(policy.high_blast_action_types),
        "low_blast_action_types": sorted(policy.low_blast_action_types),
        "checkpoint_postures": {
            key: value.value
            for key, value in sorted(policy.checkpoint_postures.items())
        },
        "loaded": policy.loaded,
    }
    return hashlib.sha256(_canonical_json(content).encode()).hexdigest()


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
    if radius_default == "never":
        default_br = BlastRadius.NEVER
    elif radius_default == "high":
        default_br = BlastRadius.HIGH
    else:
        default_br = BlastRadius.LOW

    n_recur = snap.get("auto_execute_after_n_successful_recurrences")
    try:
        n_recur = int(n_recur) if n_recur is not None else None
    except (TypeError, ValueError):
        n_recur = None

    high = _as_set(snap.get("high_blast_action_types")) or frozenset(
        _FALLBACK_HIGH_BLAST
    )
    low = _as_set(snap.get("low_blast_action_types")) or frozenset(_FALLBACK_LOW_BLAST)
    postures = _parse_checkpoint_postures(snap.get("checkpoint_postures"), entity_id)

    return ExecutionPolicy(
        entity_id=entity_id,
        title=str(snap.get("title", "")),
        confidence_threshold=threshold,
        blast_radius_default=default_br,
        auto_execute_after_n_successful_recurrences=n_recur,
        high_blast_action_types=high,
        low_blast_action_types=low,
        checkpoint_postures=postures,
        loaded=True,
        authorization_revision=entity_record_digest(data),
    )


class InvalidCheckpointPosture(ValueError):
    """Raised when an execution_policy declares a checkpoint posture outside
    {open, closed}. Fails HARD at policy-load time (per ateles#350 spec) —
    a typo'd posture must never silently resolve to a default, since that
    could turn an intended `closed` boundary into a silent `open`.
    """


def _parse_checkpoint_postures(
    raw: object, policy_entity_id: str
) -> dict[str, CheckpointPosture]:
    if raw is None:
        return {}
    if isinstance(raw, str):
        import json as _json

        try:
            raw = _json.loads(raw)
        except (ValueError, TypeError):
            return {}
    if not isinstance(raw, dict):
        return {}

    postures: dict[str, CheckpointPosture] = {}
    for boundary, value in raw.items():
        v = str(value).strip().lower()
        try:
            postures[str(boundary).strip().lower()] = CheckpointPosture(v)
        except ValueError:
            allowed = ", ".join(p.value for p in CheckpointPosture)
            raise InvalidCheckpointPosture(
                f"execution_policy {policy_entity_id!r}: checkpoint_postures"
                f"[{boundary!r}] = {value!r} is not a valid posture "
                f"(allowed: {allowed})"
            ) from None
    return postures


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
        log.warning(f"[gating] could not fetch entity {entity_id}: {exc}")
        return None


def _fetch_entity_observations(entity_id: str, *, limit: int = 100) -> list[dict]:
    if not NEOTOMA_BEARER_TOKEN:
        return []
    try:
        resp = httpx.get(
            f"{NEOTOMA_BASE_URL}/entities/{entity_id}/observations",
            headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
            params={"limit": limit},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        observations = data.get("observations") if isinstance(data, dict) else None
        return observations if isinstance(observations, list) else []
    except Exception as exc:  # noqa: BLE001
        log.warning("[gating] could not fetch observations for %s: %s", entity_id, exc)
        return []


def fetch_entity_user_id(entity_id: str) -> str | None:
    """Resolve tenant provenance from immutable observation ownership."""
    user_ids = {
        str(item.get("user_id")).strip()
        for item in _fetch_entity_observations(entity_id)
        if item.get("user_id")
    }
    return next(iter(user_ids)) if len(user_ids) == 1 else None


def load_policy(policy_id: str | None = None) -> ExecutionPolicy:
    """
    Load an execution_policy entity. Returns a fallback policy (loaded=False)
    when Neotoma is unreachable OR malformed, so the gate always functions
    (fails closed) and callers never need their own try/except around this.

    A policy with an invalid `checkpoint_postures` entry (see
    `InvalidCheckpointPosture`) is a config error the operator must fix, but
    it must not itself crash whatever daemon happened to call load_policy()
    next — that would turn a typo in an unrelated boundary's posture into an
    outage of the whole gate. We log it loudly (visible, with the
    allowed-values hint from `_parse_checkpoint_postures`) and degrade to the
    same conservative fallback used for an unreachable policy.
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
    try:
        return _parse_policy(pid, data)
    except InvalidCheckpointPosture as exc:
        log.error(
            f"[gating] policy {pid} has an invalid checkpoint_postures entry "
            f"— using conservative fallback until fixed: {exc}"
        )
        return ExecutionPolicy(entity_id=pid, loaded=False)


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
    confidence_unscored: bool = False,
) -> GateDecision:
    """
    Apply the gate matrix. Returns a GateDecision.

    Auto-execute requires high confidence AND low blast radius — UNLESS the task
    is a recurring series that has cleared the policy's recurrence-graduation
    count (and the policy enables graduation, i.e. n is not None).

    ``BlastRadius.NEVER`` short-circuits before any of that (ateles#715). It is
    checked first and returns unconditionally, so no confidence score, no
    threshold tuning, and no number of clean recurrences can produce
    AUTO_EXECUTE for it. That is the whole point of the tier: `operator_only`
    does not mean "risky", it means an agent cannot do this — and a hundred
    prior successes do not hand the swarm a credential it was designed not to
    hold.

    ``confidence_unscored`` (ateles#902): True when no producer ever scored
    this task, so `confidence` is either the fail-closed default (0.0) or a
    mechanical approximation (`confidence_scoring.score_confidence`) filled
    in ahead of this call — never an agent's own judgment. It changes ONLY
    the REASON text on a CHECKPOINT*/AUTO_EXECUTE decision, stamping
    `GateDecision.confidence_unscored` so "never scored" and "scored low"
    never again read identically to the operator. It does NOT gate the
    action axis: a well-specified, unscored, LOW-blast task can still clear
    `confidence_threshold` on its mechanical score and AUTO_EXECUTE exactly
    as it would if an agent had scored it — this flag does not force a
    checkpoint. Fail-closed is preserved by the blast-radius axis alone:
    ``NEVER`` never auto-executes regardless of this flag (see above), and a
    HIGH-blast unscored task still checkpoints because the fallback
    mechanical score does not clear HIGH's bar, not because of this flag.
    """
    blast = policy.blast_radius_for(action_type)
    threshold = policy.confidence_threshold
    high_conf = confidence >= threshold

    # Never-auto-executable: returns before the confidence axis and before
    # recurrence graduation are consulted at all.
    if blast == BlastRadius.NEVER:
        return GateDecision(
            action=GateAction.CHECKPOINT,
            blast_radius=blast,
            confidence=confidence,
            threshold=threshold,
            policy_id=policy.entity_id,
            reason=(
                "operator-only action — never auto-executable at any "
                "confidence or recurrence count"
            ),
            confidence_unscored=confidence_unscored,
        )

    # Recurrence graduation: a proven recurring series may auto-execute below
    # threshold, but ONLY when the policy enables it (n is not None) and the
    # action is not high blast (money/publish never graduate — those policies
    # set n=None).
    graduated = (
        policy.auto_execute_after_n_successful_recurrences is not None
        and successful_recurrences >= policy.auto_execute_after_n_successful_recurrences
        and blast == BlastRadius.LOW  # NEVER and HIGH never graduate
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
            confidence_unscored=confidence_unscored,
        )

    # Otherwise: checkpoint. Low-confidence + high-blast also proposes alternatives.
    # An unscored task gets a distinct reason (ateles#902): a producer never
    # judged it, and that must not read as the judgment "low confidence" claims
    # to be. The wording says what is true after Part 2 — a score is present and
    # it is mechanical — rather than claiming no confidence was recorded, which
    # the mechanical score contradicts.
    if not high_conf and blast == BlastRadius.HIGH:
        reason = (
            "not scored by a producer (score is a mechanical estimate) and "
            "high blast radius — propose alternatives"
            if confidence_unscored
            else "low confidence and high blast radius — propose alternatives"
        )
        return GateDecision(
            action=GateAction.CHECKPOINT_WITH_ALTERNATIVES,
            blast_radius=blast,
            confidence=confidence,
            threshold=threshold,
            policy_id=policy.entity_id,
            reason=reason,
            confidence_unscored=confidence_unscored,
        )

    if blast == BlastRadius.HIGH:
        reason = "high blast radius — operator approval required"
    elif confidence_unscored:
        reason = "not scored by a producer — score is a mechanical estimate"
    else:
        reason = "below confidence threshold"
    return GateDecision(
        action=GateAction.CHECKPOINT,
        blast_radius=blast,
        confidence=confidence,
        threshold=threshold,
        policy_id=policy.entity_id,
        reason=reason,
        confidence_unscored=confidence_unscored,
    )


CHECKPOINT_AUTHORIZATION_VERSION = 2
_TRUSTED_AAUTH_TIERS = frozenset({"software", "operator_attested", "hardware"})


def build_checkpoint_authorization_envelope(
    *,
    task_record: dict,
    policy: ExecutionPolicy,
    decision: GateDecision,
    action_type: str,
    user_id: str,
) -> str:
    """Serialize the exact task and policy revisions shown for approval."""
    payload = {
        "version": CHECKPOINT_AUTHORIZATION_VERSION,
        "producer": "apis@ateles-swarm",
        "task_entity_id": str(task_record.get("entity_id") or ""),
        "task_revision": entity_record_digest(task_record),
        "task_observation_count": task_record.get("observation_count"),
        "task_last_observation_at": task_record.get("last_observation_at"),
        "user_id": str(user_id),
        "action_type": str(action_type).strip().lower(),
        "policy_entity_id": policy.entity_id,
        "policy_revision": execution_policy_revision(policy),
        "gate_action": decision.action.value,
        "blast_radius": decision.blast_radius.value,
    }
    return _canonical_json(payload)


def read_authenticated_checkpoint_authorization(
    checkpoint_id: str, checkpoint_record: dict
) -> dict | None:
    """Read an authorization envelope only from its AAuth-backed observation.

    The current snapshot fields are mutable reducer output.  Trust comes from
    the immutable creation observation named by per-field provenance, after
    Neotoma has verified and recorded Apis's AAuth identity.
    """
    snapshot = _snapshot_of(checkpoint_record)
    encoded = snapshot.get("body")
    provenance = checkpoint_record.get("provenance")
    if not isinstance(encoded, str) or not isinstance(provenance, dict):
        return None
    observation_id = provenance.get("body")
    if not isinstance(observation_id, str) or not observation_id:
        return None
    protected_fields = (
        "body",
        "task_entity_id",
        "policy_entity_id",
        "blast_radius",
        "gate_action",
        "handler",
    )
    if any(provenance.get(field) != observation_id for field in protected_fields):
        return None
    observation = next(
        (
            item
            for item in _fetch_entity_observations(checkpoint_id)
            if item.get("id") == observation_id
        ),
        None,
    )
    if not isinstance(observation, dict):
        return None
    fields = observation.get("fields")
    auth = observation.get("provenance")
    if not isinstance(fields, dict) or fields.get("body") != encoded:
        return None
    if not isinstance(auth, dict):
        return None
    if (
        auth.get("agent_sub") != "apis@ateles-swarm"
        or not auth.get("agent_thumbprint")
        or auth.get("attribution_tier") not in _TRUSTED_AAUTH_TIERS
    ):
        return None
    try:
        payload = json.loads(encoded)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != 2:
        return None
    if payload.get("producer") != "apis@ateles-swarm":
        return None
    return payload


def write_checkpoint_brief(
    *,
    task_entity_id: str,
    decision: GateDecision,
    title: str,
    plan_summary: str,
    handler: str,
    alternatives: list[str] | None = None,
    user_id: str | None = None,
    action_type: str | None = None,
    idempotency_context: str | None = None,
    task_record: dict | None = None,
    policy: ExecutionPolicy | None = None,
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
                "confidence_unscored": decision.confidence_unscored,
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
    normalized_action = str(action_type or "").strip().lower()
    authorization_expected = task_record is not None and policy is not None
    expected_authorization: dict | None = None
    if authorization_expected:
        if (
            task_record.get("entity_id") != task_entity_id
            or str(task_record.get("entity_type", "")).strip().lower() != "task"
            or not user_id
        ):
            log.warning("[gating] incomplete task provenance for checkpoint authority")
            return None
        encoded_authorization = build_checkpoint_authorization_envelope(
            task_record=task_record,
            policy=policy,
            decision=decision,
            action_type=normalized_action,
            user_id=user_id,
        )
        body["entities"][0]["body"] = encoded_authorization
        expected_authorization = json.loads(encoded_authorization)
    if idempotency_context:
        body["idempotency_key"] += f"-{idempotency_context}"
    try:
        headers = {"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"}
        if authorization_expected:
            from .aauth_signer import AAuthSigner

            signer = AAuthSigner.from_key_file(handler)
            if signer.is_stub:
                log.error("[gating] no AAuth signer for checkpoint authorization")
                return None
            headers.update(signer.headers("POST", "/store"))
        resp = httpx.post(
            f"{NEOTOMA_BASE_URL}/store",
            headers=headers,
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        ents = data.get("entities") or []
        entity_id = ents[0].get("entity_id") if ents else None
        if entity_id and authorization_expected:
            readback = _fetch_entity(entity_id)
            authorization = (
                read_authenticated_checkpoint_authorization(entity_id, readback)
                if isinstance(readback, dict)
                else None
            )
            if authorization != expected_authorization:
                log.warning(
                    "[gating] checkpoint %s authenticated authorization did not "
                    "materialize exactly on read-back",
                    entity_id,
                )
                return None
        return entity_id
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[gating] failed to persist checkpoint_brief: {exc}")
        return None


# ── Checkpoint resolution ───────────────────────────────────────────────────
#
# A checkpoint_brief written by the gate sits at status="awaiting_operator"
# until the operator (via Ateles) flips it. The terminal states the dispatch
# daemon acts on:
#
#   approved  → operator override: re-dispatch the referenced task, bypassing
#               the gate (the human IS the approval the gate was waiting for).
#   rejected  → mark the task declined; do not execute.
#
# Any other status (awaiting_operator, or an unknown value) is a no-op.

# Statuses that resolve a checkpoint, and how the dispatcher should treat them.
CHECKPOINT_APPROVED_STATES = frozenset({"approved", "approve", "accepted"})
CHECKPOINT_REJECTED_STATES = frozenset({"rejected", "reject", "declined", "denied"})
CHECKPOINT_APPROVED_NO_RELEASE = "approved_no_release"
CHECKPOINT_REQUIRES_FRESH_APPROVAL = "approved_requires_fresh_approval"


def checkpoint_authorization_digest(
    *,
    task_entity_id: str,
    user_id: str,
    action_type: str,
    gate_action: str,
    blast_radius: str,
    policy_id: str,
) -> str:
    """Return the retired v1 sibling-field checksum for legacy detection."""
    canonical = json.dumps(
        {
            "action_type": str(action_type).strip().lower(),
            "blast_radius": str(blast_radius).strip().lower(),
            "gate_action": str(gate_action).strip().lower(),
            "policy_id": str(policy_id).strip(),
            "task_entity_id": str(task_entity_id).strip(),
            "user_id": str(user_id).strip(),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _snapshot_of(data: dict) -> dict:
    """Unwrap the nested snapshot shape returned by /entities/{id}."""
    snap = (data.get("snapshot") or {}).get("snapshot")
    if isinstance(snap, dict):
        return snap
    if isinstance(data.get("snapshot"), dict):
        return data["snapshot"]
    return data


def _snapshot_with_tenant(data: dict) -> dict:
    """Return a snapshot without dropping tenant provenance on the envelope."""
    snapshot = dict(_snapshot_of(data))
    envelope_user_id = data.get("user_id")
    snapshot_user_id = snapshot.get("user_id")
    if envelope_user_id and snapshot_user_id and envelope_user_id != snapshot_user_id:
        # Preserve the conflict as absent authorization rather than choosing
        # either source. Checkpoint consumers require a truthy matching value.
        log.warning(
            "[gating] entity tenant provenance conflicts between envelope and snapshot"
        )
        snapshot["user_id"] = None
    elif envelope_user_id and not snapshot_user_id:
        snapshot["user_id"] = envelope_user_id
    return snapshot


def read_checkpoint_resolution(snapshot: dict) -> str | None:
    """
    Classify a checkpoint_brief snapshot's status into 'approved' | 'rejected' |
    None (still pending / unknown). Case-insensitive.
    """
    status = str(snapshot.get("status", "")).strip().lower()
    if status in CHECKPOINT_APPROVED_STATES:
        return "approved"
    if status in CHECKPOINT_REJECTED_STATES:
        return "rejected"
    return None


def fetch_task_record(task_entity_id: str) -> dict | None:
    """Fetch the complete typed task record, including revision provenance."""
    data = _fetch_entity(task_entity_id)
    if data is None:
        return None
    entity_type = str(data.get("entity_type") or data.get("type") or "").strip().lower()
    if entity_type != "task":
        log.warning(
            "[gating] entity %s is type %r, not task — refusing checkpoint release",
            task_entity_id,
            entity_type or "unknown",
        )
        return None
    return data


def fetch_task_snapshot(task_entity_id: str) -> dict | None:
    """Fetch the current typed task snapshot for dispatch."""
    data = fetch_task_record(task_entity_id)
    return _snapshot_with_tenant(data) if data is not None else None


def fetch_checkpoint_record(checkpoint_entity_id: str) -> dict | None:
    """Fetch a complete typed checkpoint record, including provenance."""
    data = _fetch_entity(checkpoint_entity_id)
    if data is None:
        return None
    entity_type = str(data.get("entity_type") or data.get("type") or "").strip().lower()
    if entity_type != "checkpoint_" + "brief":
        log.warning(
            "[gating] entity %s is type %r, not a checkpoint brief",
            checkpoint_entity_id,
            entity_type or "unknown",
        )
        return None
    return data


def fetch_checkpoint_snapshot(checkpoint_entity_id: str) -> dict | None:
    """Fetch a checkpoint snapshot, rejecting ambiguous types."""
    data = fetch_checkpoint_record(checkpoint_entity_id)
    return _snapshot_with_tenant(data) if data is not None else None


def checkpoint_already_dispatched(snapshot: dict) -> bool:
    """
    True if this checkpoint_brief has already been acted on by the dispatcher
    (resolved_dispatched stamped). Used to make SSE replays of the same
    approved/rejected event no-ops.
    """
    val = snapshot.get("resolved_dispatched")
    return val is True or str(val).strip().lower() in {"true", "1", "yes"}


def stamp_checkpoint_dispatched(checkpoint_entity_id: str, *, handler: str) -> bool:
    """
    Claim a checkpoint_brief resolution with `resolved_dispatched: true` once
    the consumer has decided it can act, so SSE replays are no-ops. The caller
    must not dispatch if this write fails. Best-effort; logs and returns False
    on failure.
    """
    if not NEOTOMA_BEARER_TOKEN:
        log.warning("[gating] no bearer token — cannot stamp checkpoint dispatched")
        return False
    body = {
        "entity_id": checkpoint_entity_id,
        "entity_type": "checkpoint_brief",
        "field": "resolved_dispatched",
        "value": True,
        "idempotency_key": f"checkpoint-dispatched-{handler}-{checkpoint_entity_id}",
    }
    try:
        resp = httpx.post(
            f"{NEOTOMA_BASE_URL}/correct",
            headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        response_body = resp.json()
        if not isinstance(response_body, dict) or response_body.get("snapshot") is None:
            # Neotoma's idempotent duplicate path returns snapshot=null. The
            # field may read true because another consumer won, but this caller
            # did not acquire the claim and therefore must not dispatch.
            log.info(
                "[gating] checkpoint %s stamp was an idempotent replay — claim not acquired",
                checkpoint_entity_id,
            )
            return False
        # A successful correction response is not proof that the field landed:
        # Neotoma can accept a write that does not materialize on the entity.
        # Read the brief itself back before treating the stamp as a replay claim.
        data = _fetch_entity(checkpoint_entity_id)
        if data is None:
            log.warning(
                "[gating] checkpoint %s stamp could not be read back",
                checkpoint_entity_id,
            )
            return False
        entity_type = (
            str(data.get("entity_type") or data.get("type") or "").strip().lower()
        )
        snapshot = _snapshot_of(data)
        if entity_type != "checkpoint_" + "brief" or not checkpoint_already_dispatched(
            snapshot
        ):
            log.warning(
                "[gating] checkpoint %s stamp was not materialized on read-back",
                checkpoint_entity_id,
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(
            f"[gating] failed to stamp checkpoint {checkpoint_entity_id} dispatched: {exc}"
        )
        return False


def _transition_checkpoint_status(
    checkpoint_entity_id: str,
    *,
    handler: str,
    reason: str,
    status: str,
    idempotency_label: str,
) -> bool:
    """Write and read back a terminal/non-releasable checkpoint status."""
    if not NEOTOMA_BEARER_TOKEN:
        log.warning(
            "[gating] no bearer token — cannot transition checkpoint to %s", status
        )
        return False
    body = {
        "entity_id": checkpoint_entity_id,
        "entity_type": "checkpoint_brief",
        "field": "status",
        "value": status,
        "idempotency_key": f"checkpoint-{idempotency_label}-{handler}-{checkpoint_entity_id}",
    }
    try:
        resp = httpx.post(
            f"{NEOTOMA_BASE_URL}/correct",
            headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        data = _fetch_entity(checkpoint_entity_id)
        if data is None:
            log.warning(
                "[gating] checkpoint %s transition to %s could not be read back",
                checkpoint_entity_id,
                status,
            )
            return False
        entity_type = (
            str(data.get("entity_type") or data.get("type") or "").strip().lower()
        )
        snapshot = _snapshot_of(data)
        if (
            entity_type != "checkpoint_" + "brief"
            or str(snapshot.get("status", "")).strip().lower() != status
        ):
            log.warning(
                "[gating] checkpoint %s transition to %s did not materialize",
                checkpoint_entity_id,
                status,
            )
            return False
        log.info(
            "[gating] checkpoint %s transitioned to %s (%s)",
            checkpoint_entity_id,
            status,
            reason,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "[gating] failed to transition checkpoint %s to %s: %s",
            checkpoint_entity_id,
            status,
            exc,
        )
        return False


def close_checkpoint_without_release(
    checkpoint_entity_id: str, *, handler: str, reason: str
) -> bool:
    """Durably consume an approval that is not authorized to release work."""
    return _transition_checkpoint_status(
        checkpoint_entity_id,
        handler=handler,
        reason=reason,
        status=CHECKPOINT_APPROVED_NO_RELEASE,
        idempotency_label="no-release",
    )


def require_fresh_checkpoint_approval(
    checkpoint_entity_id: str, *, handler: str, reason: str
) -> bool:
    """Retire stale authority while preserving the task for a new approval."""
    return _transition_checkpoint_status(
        checkpoint_entity_id,
        handler=handler,
        reason=reason,
        status=CHECKPOINT_REQUIRES_FRESH_APPROVAL,
        idempotency_label="fresh-approval",
    )


def mark_task_declined(task_entity_id: str, *, reason: str, handler: str) -> bool:
    """
    Correct a task's status to 'declined' after an operator rejects its
    checkpoint. Returns True on success. Best-effort; logs and returns False on
    failure (never raises into the dispatch loop).
    """
    if not NEOTOMA_BEARER_TOKEN:
        log.warning("[gating] no bearer token — cannot mark task declined")
        return False
    body = {
        "entity_id": task_entity_id,
        "entity_type": "task",
        "field": "status",
        "value": "declined",
        "idempotency_key": f"decline-{handler}-{task_entity_id}",
    }
    try:
        resp = httpx.post(
            f"{NEOTOMA_BASE_URL}/correct",
            headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        log.info(f"[gating] task {task_entity_id} marked declined ({reason})")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[gating] failed to mark task {task_entity_id} declined: {exc}")
        return False
