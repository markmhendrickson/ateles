"""
lib/daemon_runtime/readiness.py — the pre-execution readiness gate (E4).

The SECOND of two sequential gates (docs/task_execution_loop.md). It runs BEFORE
the confidence×blast execution gate (gating.py) and asks a different question:

  readiness gate   — "is this task well-specified enough to START?"   (this module)
  execution gate   — "is my planned action safe to auto-run?"         (gating.py)

A task scores along five axes derived from its fields + relationships; below the
threshold it is parked in `awaiting_input` and the operator gets a TARGETED request
naming exactly what's missing — the task is clarified, not failed. A
`task_readiness_assessment` entity records the verdict, linked REFERS_TO the task.

This is a deterministic, fields-based scorer (pure `assess_readiness`), mirroring
gating.evaluate_gate. A future enhancement can let the responsible agent re-score
with judgment; the rubric here is the floor.

Hard floors mirror the confidence rubric (ent_22fd6f25159f1f2689726780): a missing
goal caps the score hard, missing acceptance criteria caps it to mid.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field

import httpx

log = logging.getLogger("daemon_runtime.readiness")

NEOTOMA_BASE_URL = os.environ.get("NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com")
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

DEFAULT_THRESHOLD = float(os.environ.get("APIS_READINESS_THRESHOLD", "0.75"))

# Axis weights (sum to 1.0). Goal + acceptance carry the most because a task you
# can't define or can't check "done" on is the least safe to start.
_WEIGHTS = {
    "goal_clarity": 0.25,
    "acceptance_criteria": 0.20,
    "constraints_present": 0.20,
    "tooling_identified": 0.20,
    "context_density": 0.15,
}

# Human-readable asks for each axis when it falls short (for the operator email).
_MISSING_PROMPTS = {
    "goal_clarity": "a clear goal — what specific outcome should this task achieve?",
    "acceptance_criteria": "acceptance criteria — how will we know it's done / done-when?",
    "constraints_present": "constraints — limits, guardrails, must/​must-not, scope boundaries.",
    "tooling_identified": "tooling/owner — which agent or tools should execute this?",
    "context_density": "context links — the entities (plan, prior work, references) this depends on.",
}

_ACCEPTANCE_MARKERS = re.compile(
    r"acceptance|done when|done-when|definition of done|criteria|- \[ \]|✅|☑", re.I
)
_CONSTRAINT_MARKERS = re.compile(
    r"constraint|must not|must-not|do not|don't|limit|budget|deadline|by \w+day|scope|only", re.I
)


@dataclass
class ReadinessAssessment:
    score: float
    ready: bool
    threshold: float
    axes: dict[str, float] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    rationale: str = ""


def _text(snapshot: dict) -> tuple[str, str]:
    title = (snapshot.get("title") or "").strip()
    desc = (snapshot.get("body") or snapshot.get("description") or "").strip()
    return title, desc


def assess_readiness(
    snapshot: dict,
    *,
    has_owner: bool,
    relationship_count: int = 0,
    threshold: float = DEFAULT_THRESHOLD,
) -> ReadinessAssessment:
    """Score whether a task is well-specified enough to start. Pure.

    Args:
        snapshot: the task snapshot (title, body/description, tags, constraints,
                  acceptance_criteria, …).
        has_owner: whether dispatch resolved an owner/skill (assigned_to or a
                   routable domain tag).
        relationship_count: number of links the task carries (plan, refs, deps).
        threshold: ready cutoff (default APIS_READINESS_THRESHOLD or 0.75).
    """
    title, desc = _text(snapshot)
    tags = snapshot.get("tags") or snapshot.get("labels") or []
    has_constraints_field = bool(snapshot.get("constraints"))
    has_criteria_field = bool(snapshot.get("acceptance_criteria"))
    blob = f"{title}\n{desc}"

    axes: dict[str, float] = {}

    # goal_clarity — a title plus a description of substance.
    if title and len(desc) >= 40:
        axes["goal_clarity"] = 1.0
    elif title and desc:
        axes["goal_clarity"] = 0.7
    elif title:
        axes["goal_clarity"] = 0.5
    else:
        axes["goal_clarity"] = 0.0

    # acceptance_criteria — a field, or markers in the text.
    axes["acceptance_criteria"] = (
        1.0 if has_criteria_field else 0.8 if _ACCEPTANCE_MARKERS.search(blob) else 0.3
    )

    # constraints_present — a field, tags, or markers in the text.
    axes["constraints_present"] = (
        1.0 if has_constraints_field
        else 0.7 if (tags or _CONSTRAINT_MARKERS.search(blob)) else 0.3
    )

    # tooling_identified — dispatch resolved an owner/skill.
    axes["tooling_identified"] = 1.0 if has_owner else 0.4

    # context_density — linked entities the work depends on.
    axes["context_density"] = (
        1.0 if relationship_count >= 2 else 0.6 if relationship_count == 1 else 0.3
    )

    score = sum(_WEIGHTS[k] * axes[k] for k in _WEIGHTS)

    # Hard floors (mirror the confidence rubric).
    if axes["goal_clarity"] == 0.0:
        score = min(score, 0.3)
    if axes["acceptance_criteria"] < 0.5:
        score = min(score, 0.5)

    missing = [k for k, v in axes.items() if v < 0.6]
    ready = score >= threshold and axes["goal_clarity"] > 0.0
    rationale = (
        f"score={score:.2f}/{threshold:.2f} "
        + " ".join(f"{k}={axes[k]:.2f}" for k in _WEIGHTS)
        + (f" | missing: {', '.join(missing)}" if missing else "")
    )
    return ReadinessAssessment(
        score=round(score, 3), ready=ready, threshold=threshold,
        axes=axes, missing=missing, rationale=rationale,
    )


def missing_request(assessment: ReadinessAssessment, title: str) -> str:
    """Compose the targeted operator request naming exactly what's missing."""
    lines = [
        f"Task “{title}” isn't ready to execute yet "
        f"(readiness {assessment.score:.2f} < {assessment.threshold:.2f}). "
        "To proceed, it needs:",
        "",
    ]
    lines += [f"• {_MISSING_PROMPTS[k]}" for k in assessment.missing if k in _MISSING_PROMPTS]
    lines += ["", "Reply with the missing detail and I'll re-assess and pick it back up."]
    return "\n".join(lines)


def build_assessment_entity(task_id: str, assessment: ReadinessAssessment) -> dict:
    """Build the /store body for a task_readiness_assessment REFERS_TO the task. Pure."""
    return {
        "entities": [{
            "entity_type": "task_readiness_assessment",
            "task_entity_id": task_id,
            "score": assessment.score,
            "ready": assessment.ready,
            "threshold": assessment.threshold,
            "axes": assessment.axes,
            "missing": assessment.missing,
            "rationale": assessment.rationale,
        }],
        "relationships": [{
            "source_index": 0, "target_entity_id": task_id, "relationship_type": "REFERS_TO",
        }],
        "observation_source": "workflow_state",
        "idempotency_key": f"readiness-{task_id}-{assessment.score}",
    }


def write_assessment(task_id: str, assessment: ReadinessAssessment) -> str | None:
    """Persist a task_readiness_assessment. Fail-open → None."""
    if not NEOTOMA_BEARER_TOKEN:
        return None
    try:
        resp = httpx.post(
            f"{NEOTOMA_BASE_URL}/store",
            headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
            json=build_assessment_entity(task_id, assessment),
            timeout=15,
        )
        resp.raise_for_status()
        ents = resp.json().get("entities", [])
        return ents[0].get("entity_id") if ents else None
    except Exception as exc:  # noqa: BLE001 — never crash dispatch
        log.warning("[readiness] could not store assessment for %s: %s", task_id, exc)
        return None


# ── self-test (pure scorer) ──────────────────────────────────────────────────


def _selftest() -> int:
    checks: dict[str, bool] = {}

    well = assess_readiness(
        {"title": "Email June invoice to the accountant",
         "description": "Send the June services invoice PDF to the accountant. "
                        "Done when the email is sent and logged. Must not include bank details.",
         "acceptance_criteria": "email sent + logged"},
        has_owner=True, relationship_count=2,
    )
    checks["well_specified_ready"] = well.ready and well.score >= 0.75
    checks["well_no_missing"] = well.missing == []

    bare = assess_readiness({"title": "fix it"}, has_owner=False, relationship_count=0)
    checks["bare_not_ready"] = not bare.ready
    checks["bare_missing_many"] = len(bare.missing) >= 3

    no_goal = assess_readiness({"description": ""}, has_owner=True, relationship_count=5)
    checks["no_goal_floored"] = no_goal.score <= 0.3 and not no_goal.ready

    no_criteria = assess_readiness(
        {"title": "Refactor the dispatcher", "description": "Clean up apis.py routing a lot " * 5},
        has_owner=True, relationship_count=2,
    )
    checks["no_criteria_capped"] = no_criteria.score <= 0.5

    req = missing_request(bare, "fix it")
    checks["request_lists_missing"] = "needs:" in req and "•" in req

    body = build_assessment_entity("ent_t", well)
    checks["entity_refers_task"] = (
        body["relationships"][0]["relationship_type"] == "REFERS_TO"
        and body["relationships"][0]["target_entity_id"] == "ent_t"
    )
    checks["entity_type"] = body["entities"][0]["entity_type"] == "task_readiness_assessment"

    ok = all(checks.values())
    for k, v in checks.items():
        print(f"[{'PASS' if v else 'FAIL'}] {k}")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    sys.exit(_selftest())
