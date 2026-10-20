"""What Sylvia's calendar-import task carries into the execution gate (ateles#682).

Characterization, not aspiration. Sylvia declares no `action_type` and no
`confidence`: a calendar title says nothing about the work, so the task is
UNSCORED and the gate scores it (ateles#902, #1142). The assignee comes from the
shared routing table, and with no declared action_type the gate's reading falls
to that agent's prior unless the title itself names an action.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import sylvia  # noqa: E402
from lib.daemon_runtime.action_type import resolve_gate_action_type  # noqa: E402
from lib.daemon_runtime.gating import ExecutionPolicy  # noqa: E402

_POLICY = ExecutionPolicy(entity_id="fallback", loaded=False)


def _verdict(action_type):
    return _POLICY.blast_radius_for(action_type).value


def _import(summary, routed_to):
    created = []
    event = {"summary": summary, "start": {"date": "2026-10-20"}}
    with (
        patch.object(sylvia, "_neotoma_create_task", created.append),
        patch.object(sylvia, "_route_to_agent", lambda title, body="": routed_to),
    ):
        assert sylvia.import_calendar_tasks([event], []) == 1
    assert len(created) == 1
    return created[0]


def _resolved(payload):
    return resolve_gate_action_type(
        payload.get("assigned_to"),
        {"title": payload["title"], "description": payload.get("notes", "")},
        verdict=_verdict,
    )


def test_calendar_import_declares_neither_action_type_nor_confidence():
    payload = _import("Quarterly planning review", "cicada")
    assert payload["entity_type"] == "task"
    assert payload["status"] == "pending"
    assert payload["assigned_to"] == "cicada"
    assert "action_type" not in payload
    assert "confidence" not in payload


def test_routed_event_with_no_signal_keeps_the_agent_prior_unchanged():
    resolved = _resolved(_import("Quarterly planning review", "cicada"))
    assert (resolved.value, resolved.source) == ("open_or_merge_pr", "agent_prior")


def test_unrouted_personal_event_resolves_to_nothing():
    payload = _import("Yoga class", None)
    assert payload["audience"] == "human"
    assert "assigned_to" not in payload
    assert _resolved(payload).value is None


def test_event_title_that_names_a_payment_is_read_as_high_blast_when_unassigned():
    payload = _import("Pay the invoice for the studio rent", None)
    resolved = _resolved(payload)
    assert (resolved.value, resolved.source) == ("payment", "content")
