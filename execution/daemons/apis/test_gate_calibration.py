"""
End-to-end calibration of the execution gate against the tasks it really gated
(ateles#682).

Every fixture below is a task Apis actually parked at `awaiting_approval` on
2026-09-01/02, with its real title and the load-bearing phrases from its real
body. The assertions state, for each, what the gate SHOULD conclude.

Two invariants this file exists to protect:

  1. Report-only work stops being classified as PR work.
  2. Genuinely consequential work KEEPS gating. The control case is the
     configuration-migration task (ent_3c262039d04b4319cc8e0f81) — the one that
     makes daemons read Neotoma at startup. If a change to the inference lets
     that through, the change is wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime.action_type import (  # noqa: E402
    infer_action_type,
    normalize_action_type,
)
from lib.daemon_runtime.block_kind import (  # noqa: E402
    BlockKind,
    classify_gate_block,
    may_sweep,
)
from lib.daemon_runtime.gating import (  # noqa: E402
    BlastRadius,
    ExecutionPolicy,
    GateAction,
    evaluate_gate,
)


# The production default policy (ent_dfce6edecefe3eb7fc9e0337), inlined so the
# test does not depend on Neotoma being reachable.
def _default_policy() -> ExecutionPolicy:
    return ExecutionPolicy(
        entity_id="ent_dfce6edecefe3eb7fc9e0337",
        title="Default swarm execution gate",
        confidence_threshold=0.85,
        blast_radius_default=BlastRadius.LOW,
        auto_execute_after_n_successful_recurrences=3,
        high_blast_action_types=frozenset(
            {
                "git_push",
                "open_or_merge_pr",
                "payment",
                "send_external_comms",
                "delete_entity_or_data",
                "external_api_write",
                "publish",
            }
        ),
        low_blast_action_types=frozenset(
            {
                "local_edit",
                "draft",
                "neotoma_read",
                "neotoma_internal_entity_update",
                "compute_only_analysis",
            }
        ),
        loaded=True,
    )


# Mirrors apis._AGENT_ACTION_TYPE after the ateles#682 change: single-remit
# specialists only, generalists deliberately absent.
_AGENT_ACTION_TYPE = {
    "monedula": "payment",
    "struthio": "publish",
    "corvus": "send_external_comms",
    "fringilla": "compute_only_analysis",
}


def _resolve(snapshot: dict, skill: str | None) -> tuple[str | None, bool]:
    """Mirror of apis._infer_action_type, kept in lockstep by the test below."""
    explicit = normalize_action_type(snapshot.get("action_type"))
    if explicit:
        return explicit, False
    inferred = infer_action_type(
        snapshot.get("title") or "",
        snapshot.get("body") or snapshot.get("description") or "",
    )
    if inferred:
        return inferred, True
    if skill:
        return _AGENT_ACTION_TYPE.get(skill.lower()), True
    return None, True


def _gate(snapshot: dict, skill: str | None):
    action_type, was_inferred = _resolve(snapshot, skill)
    try:
        confidence = max(0.0, min(1.0, float(snapshot.get("confidence"))))
    except (TypeError, ValueError):
        confidence = 0.0
    decision = evaluate_gate(
        confidence=confidence,
        action_type=action_type,
        policy=_default_policy(),
        successful_recurrences=0,
    )
    return decision, action_type, was_inferred


# ── The control case: this MUST still gate ───────────────────────────────────


def test_hard_dependency_task_still_gates():
    """ent_3c262039d04b4319cc8e0f81 — swarm config migration into Neotoma.

    This is the one call out of the eight that was CORRECT. It moves ~250
    config variables into Neotoma entities, putting a Neotoma read on daemon
    startup, and it has an open PR. It is genuinely high blast radius and must
    keep requiring the operator.
    """
    snapshot = {
        "title": "Migrate swarm configuration from env files to Neotoma entities",
        "description": (
            "Moves ~250 configuration variables out of env files and into "
            "daemon_configuration entities. PR #643 open. Resolution: env var "
            "-> Neotoma entity -> local cache -> declared default -> loud "
            "failure. A daemon that cannot start because its config store is "
            "degraded is strictly worse than one running slightly stale config."
        ),
    }
    decision, action_type, was_inferred = _gate(snapshot, "cicada")

    assert action_type == "open_or_merge_pr"
    assert decision.blast_radius is BlastRadius.HIGH
    assert decision.action is not GateAction.AUTO_EXECUTE

    # And no sweep may clear it, even though the action type was inferred:
    # the inference found real PR work, and re-running the gate on the same
    # text reaches the same HIGH answer. The bounded sweep budget stops it.
    kind, _ = classify_gate_block(
        blast_radius=decision.blast_radius.value,
        confidence=0.0,
        threshold=decision.threshold,
        action_type_was_inferred=was_inferred,
    )
    if kind is BlockKind.REEVALUABLE:
        redecision, _, _ = _gate(snapshot, "cicada")
        assert redecision.action is not GateAction.AUTO_EXECUTE, (
            "a re-evaluation sweep must not release the hard-dependency task"
        )


# ── The seven that were wrong on the merits ──────────────────────────────────


@pytest.mark.parametrize(
    "task_id, title, body",
    [
        (
            "ent_243f4dd443e715e027e94fd7",
            "Add a read-only reconciler comparing issue gate_status "
            "against participation_record",
            "Report through the existing daemon reporting path. Write NOTHING "
            "- this job's only output is a divergence report.",
        ),
        (
            "ent_1ef1e39e18ce0ee83a9a3400",
            "Extend the workflow drift check to compare declared gate "
            "sequence, not just owner names",
            "PURE REPORTING. No behaviour change, nothing gated on the result.",
        ),
    ],
)
def test_report_only_tasks_stop_being_pr_work(task_id, title, body):
    """These produce a document. They were gated as if they merged code."""
    decision, action_type, _ = _gate({"title": title, "description": body}, "cicada")
    assert action_type == "compute_only_analysis", task_id
    assert decision.blast_radius is BlastRadius.LOW, task_id


def test_report_only_task_still_needs_confidence_to_auto_execute():
    """LOW blast alone is not enough — the gate is still two-axis.

    This is what stops the fix from becoming "everything runs". A report-only
    task whose creator scored no confidence still checkpoints; it just does so
    for the honest reason ("below confidence threshold") rather than the false
    one ("high blast radius").
    """
    snapshot = {
        "title": "Read-only reconciler",
        "description": "Write NOTHING - this job's only output is a report.",
    }
    decision, _, _ = _gate(snapshot, "cicada")
    assert decision.blast_radius is BlastRadius.LOW
    assert decision.action is not GateAction.AUTO_EXECUTE
    assert "confidence" in decision.reason

    # With an honest confidence score from the creating agent, it proceeds.
    snapshot["confidence"] = 0.9
    decision, _, _ = _gate(snapshot, "cicada")
    assert decision.action is GateAction.AUTO_EXECUTE


# ── The generalist no longer speaks for the work ─────────────────────────────


def test_generalist_assignment_alone_no_longer_implies_high_blast():
    """The defect in one assertion: an unclassifiable Cicada task.

    Before: Cicada → open_or_merge_pr → HIGH → checkpoint_with_alternatives.
    After: no action type asserted → policy default (LOW) → still checkpoints,
    but on the confidence axis, and the block is re-evaluable rather than
    presented to the operator as a merge decision.
    """
    snapshot = {"title": "Fix the flaky test", "description": "It is flaky."}
    decision, action_type, was_inferred = _gate(snapshot, "cicada")

    assert action_type is None
    assert decision.blast_radius is BlastRadius.LOW
    assert decision.action is not GateAction.AUTO_EXECUTE
    assert was_inferred

    kind, _ = classify_gate_block(
        blast_radius=decision.blast_radius.value,
        confidence=0.0,
        threshold=decision.threshold,
        action_type_was_inferred=was_inferred,
    )
    assert kind is BlockKind.REEVALUABLE
    assert may_sweep(block_kind=kind)


def test_specialists_keep_their_priors():
    """Single-remit agents still classify correctly with no task text signal."""
    for skill, expected_high in (
        ("monedula", True),
        ("struthio", True),
        ("corvus", True),
        ("fringilla", False),
    ):
        decision, _, _ = _gate({"title": "Do the routine thing"}, skill)
        assert (decision.blast_radius is BlastRadius.HIGH) is expected_high, skill


# ── No-weakening invariants ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "title, body",
    [
        ("Pay the invoice from the landlord", "Amount is on the PDF."),
        ("Send the email to the client about the delay", ""),
        ("Publish the post to the website", ""),
        ("Open a PR with the fix", "Then merge the pull request."),
        ("Permanently delete the orphaned entities", ""),
        ("Analyze the logs and then open a pull request", "Report first."),
    ],
)
def test_consequential_work_never_auto_executes(title, body):
    """Even at maximal self-reported confidence, high-blast work checkpoints."""
    decision, _, _ = _gate(
        {"title": title, "description": body, "confidence": 1.0}, "cicada"
    )
    assert decision.blast_radius is BlastRadius.HIGH, title
    assert decision.action is not GateAction.AUTO_EXECUTE, title


def test_declared_action_type_wins_over_inference():
    """An agent that declares payment is believed even if the text looks tame."""
    decision, action_type, was_inferred = _gate(
        {
            "title": "Routine monthly handling",
            "description": "Produce a report of the balances.",
            "action_type": "payment",
            "confidence": 1.0,
        },
        "cicada",
    )
    assert action_type == "payment"
    assert was_inferred is False
    assert decision.blast_radius is BlastRadius.HIGH
    assert decision.action is not GateAction.AUTO_EXECUTE


def test_typo_action_type_does_not_earn_auto_execute():
    """An unrecognized spelling must not land on the LOW policy default.

    `blast_radius_for` returns blast_radius_default (LOW) for anything it does
    not recognize, so passing a typo straight through would be a live
    escalation path. It is discarded and inference runs instead.
    """
    decision, action_type, _ = _gate(
        {
            "title": "Ship it",
            "description": "Open a pull request and merge it.",
            "action_type": "open_pull_request",  # not the canonical spelling
            "confidence": 1.0,
        },
        "cicada",
    )
    assert action_type == "open_or_merge_pr"
    assert decision.blast_radius is BlastRadius.HIGH
    assert decision.action is not GateAction.AUTO_EXECUTE


def test_apis_resolver_matches_this_modules_mirror():
    """Guard against the test's local mirror drifting from production.

    If apis._infer_action_type changes shape, this fails rather than letting
    the calibration suite quietly test something that is no longer shipped.
    """
    apis_dir = str(Path(__file__).resolve().parent)
    if apis_dir not in sys.path:
        sys.path.insert(0, apis_dir)
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_apis_probe", Path(apis_dir) / "apis.py"
    )
    assert spec and spec.loader
    # Importing apis.py has import-time side effects (daemon config); we only
    # need to confirm the map's shape, so read it statically instead.
    source = (Path(apis_dir) / "apis.py").read_text()
    assert '"cicada": "open_or_merge_pr"' not in source, (
        "Cicada must not carry a blanket high-blast action type (ateles#682)"
    )
    assert '"monedula": "payment"' in source
    assert "def _infer_action_type" in source
    assert "-> tuple[str | None, bool]" in source
