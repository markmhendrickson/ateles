"""
lib/daemon_runtime/confidence_scoring.py — populate confidence before the gate
reads it (ateles#902).

`gating.evaluate_gate` has always scored `confidence x blast_radius`, but until
this module nothing ever WROTE a `confidence` (or `confidence_score`) field
onto a task entity. `apis._read_confidence` read the absent field, `float(None)`
raised, and the fail-closed default (0.0) flowed into every checkpoint — 36 of
49 pending checkpoints carried that default stamped with a reason that read as
a judgment ("low confidence and high blast radius") nobody made.

Tasks are created by half a dozen independent producers (anthus, cotinga,
neotoma-agent, sylvia, turdus, apis's own reconciler/watchdog, …), and the
`confidence_rubric` (ent_22fd6f25159f1f2689726780, see
`.claude/skills/cicada/SKILL.md`) is itself judgment an AUTHORING agent applies
over its own retrieval and reasoning trace — retrieval_density,
action_familiarity, decision_consistency and prior_executions_successful are
not honestly recoverable from a task snapshot alone. Rather than duplicate a
partial, dishonest version of that judgment in every producer, this module
scores once, at the single choke point every dispatch path already passes
through (`apis.dispatch_task`, immediately before `_read_confidence`) — the
same choice `readiness.py` (E4) made for the sibling pre-execution gate.

This is a DETERMINISTIC, FIELDS-BASED APPROXIMATION of the rubric — not the
rubric itself. An authoring agent that scores itself per the full rubric (e.g.
Cicada, per its SKILL.md) still wins: `score_confidence` is only ever consulted
when the snapshot carries no explicit `confidence`/`confidence_score` already.
It exists so a task nobody scored is scored by *something* mechanical and
inspectable, instead of silently reading as "0.0 — low confidence".

Axes (named after the rubric's own signals so a reader can trace this back to
the rubric it approximates):

  retrieval_density        — REFERS_TO edges the task carries (context pulled in)
  required_inputs_present  — hard floor 0.4 (rubric) when an owner/skill could
                              not be resolved, i.e. dispatch itself lacks a
                              required input
  action_familiarity       — whether the resolved action_type is one the policy
                              has classified at all (recognized vs unclassified)
  decision_consistency      — hard floor 0.5 (rubric) on an unresolved conflict
                              marker in the task text; full credit otherwise —
                              a snapshot carries no first-class "conflict" field,
                              so this axis defaults optimistic rather than
                              guessing one into existence
  prior_executions_successful — the same `successful_recurrences` signal the
                              gate's own recurrence-graduation path already reads

Mirrors `gating.evaluate_gate` and `readiness.assess_readiness`: pure, no I/O,
fails toward the same conservative floor the gate already used as its default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Axis weights (sum to 1.0).
_WEIGHTS = {
    "retrieval_density": 0.20,
    "required_inputs_present": 0.25,
    "action_familiarity": 0.25,
    "decision_consistency": 0.10,
    "prior_executions_successful": 0.20,
}

_CONFLICT_MARKERS = re.compile(
    r"unresolved conflict|conflicting|contradicts|not sure which|"
    r"ambiguous requirement",
    re.I,
)


@dataclass
class ConfidenceScore:
    value: float
    axes: dict[str, float] = field(default_factory=dict)
    rationale: str = ""


def score_confidence(
    snapshot: dict,
    *,
    has_owner: bool,
    action_type_recognized: bool,
    relationship_count: int = 0,
    successful_recurrences: int = 0,
) -> ConfidenceScore:
    """Score a task's confidence (0..1) from its fields alone. Pure.

    Args:
        snapshot: the task snapshot.
        has_owner: whether dispatch resolved an owner/skill for this task —
            the same signal the readiness gate uses for `tooling_identified`.
        action_type_recognized: whether the resolved action_type is classified
            in the execution_policy's low/high blast sets (as opposed to
            falling through to NEVER as unclassified).
        relationship_count: number of REFERS_TO/PART_OF-style links the task
            carries (context pulled in — mirrors readiness's context_density).
        successful_recurrences: clean prior cycles of the same recurring
            series, already tracked by the gate's own recurrence path.
    """
    title = (snapshot.get("title") or "").strip()
    desc = (snapshot.get("body") or snapshot.get("description") or "").strip()
    blob = f"{title}\n{desc}"

    axes: dict[str, float] = {}

    # retrieval_density — context the task already carries.
    axes["retrieval_density"] = (
        1.0 if relationship_count >= 2 else 0.6 if relationship_count == 1 else 0.3
    )

    # required_inputs_present — hard floor territory: dispatch could not even
    # resolve an owner/skill, i.e. a required input for execution is missing.
    axes["required_inputs_present"] = 1.0 if has_owner else 0.4

    # action_familiarity — is the action type one the policy recognizes at all.
    axes["action_familiarity"] = 1.0 if action_type_recognized else 0.3

    # decision_consistency — default optimistic; only penalized when the task
    # text itself flags an unresolved conflict. No first-class field for this
    # exists on a task snapshot, so absence of a marker is not treated as
    # evidence of consistency beyond the default.
    axes["decision_consistency"] = 0.4 if _CONFLICT_MARKERS.search(blob) else 1.0

    # prior_executions_successful — clean recurring cycles, same signal the
    # gate's own graduation path reads.
    axes["prior_executions_successful"] = (
        1.0 if successful_recurrences >= 3
        else 0.7 if successful_recurrences >= 1
        else 0.5
    )

    value = sum(_WEIGHTS[k] * axes[k] for k in _WEIGHTS)

    # Hard floors mirror the rubric text verbatim (SKILL.md: "hard floor 0.4 if
    # a required input/credential/target is missing"; "hard floor 0.5 on
    # unresolved conflict").
    if axes["required_inputs_present"] <= 0.4:
        value = min(value, 0.4)
    if axes["decision_consistency"] <= 0.5:
        value = min(value, 0.5)

    value = max(0.0, min(1.0, value))
    rationale = (
        f"mechanical score={value:.2f} "
        + " ".join(f"{k}={axes[k]:.2f}" for k in _WEIGHTS)
    )
    return ConfidenceScore(value=round(value, 3), axes=axes, rationale=rationale)
