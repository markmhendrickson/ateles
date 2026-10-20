"""What Turdus's email-triage tasks mean to the execution gate (ateles#682).

Characterization of main, not a decision. Turdus declares `action_type:
compute_only_analysis` and `confidence: 0.8` on BOTH its branches, including the
invoice branch assigned to Monedula (whose prior is `payment`). A declared value
is passed through unchanged and beats the prior, so an invoice task is read as
LOW blast with a creator-supplied 0.8 (under the 0.85 threshold, so it still
checkpoints). Whether an invoice task should declare `payment` instead is an open
operator decision; this file makes today's reading visible so a change to it is
deliberate.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from lib.daemon_runtime.action_type import resolve_gate_action_type  # noqa: E402
from lib.daemon_runtime.gating import (  # noqa: E402
    BlastRadius,
    ExecutionPolicy,
    GateAction,
    evaluate_gate,
)
from test_turdus_store_migration import (  # noqa: E402
    _MESSAGE,
    _install_fake_client,
    _load_turdus,
)


def _entity(monkeypatch, message):
    turdus = _load_turdus(monkeypatch)
    capture: dict = {}
    _install_fake_client(
        monkeypatch, payload={"entities": [{"entity_id": "ent_task_1"}]}, capture=capture
    )
    asyncio.run(turdus._create_task_for_email(message, "ent_email_1"))
    return next(c for c in capture["calls"] if c["url"].endswith("/store"))["json"]["entities"][0]


def _decision(entity):
    resolved = resolve_gate_action_type(
        entity.get("assigned_to"),
        {
            "title": entity["title"],
            "description": entity["description"],
            "action_type": entity.get("action_type"),
        },
    )
    policy = ExecutionPolicy(entity_id="fallback", loaded=False)
    return resolved, evaluate_gate(
        confidence=entity["confidence"], action_type=resolved.value, policy=policy
    )


def test_invoice_task_is_read_as_its_declared_low_type_not_the_payment_prior(monkeypatch):
    entity = _entity(monkeypatch, _MESSAGE)
    assert entity["assigned_to"] == "monedula"
    resolved, decision = _decision(entity)
    assert (resolved.value, resolved.source) == ("compute_only_analysis", "declared")
    assert decision.blast_radius is BlastRadius.LOW
    # Its creator-supplied 0.8 is under the threshold, so it still checkpoints.
    assert decision.action is not GateAction.AUTO_EXECUTE


def test_plain_actionable_email_task_is_read_as_its_declared_low_type(monkeypatch):
    plain = dict(_MESSAGE, subject="Can we meet on Thursday?",
                 sender="Colleague <colleague@example.com>", snippet="Let me know.")
    entity = _entity(monkeypatch, plain)
    assert "assigned_to" not in entity
    resolved, decision = _decision(entity)
    assert (resolved.value, resolved.source) == ("compute_only_analysis", "declared")
    assert decision.blast_radius is BlastRadius.LOW
