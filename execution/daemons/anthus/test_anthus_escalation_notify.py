"""Effect tests for Anthus escalation Notifier dedupe (ateles#1165)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from lib.daemon_runtime import NeotomaEvent
from lib.notify import Notifier, Priority
from execution.daemons.anthus import anthus

# Empty silence bounds → never silent (see apis test_notify_dedupe_call_sites).
_NEVER_SILENT = {
    "timezone": "Europe/Madrid",
    "silence_start": "",
    "silence_end": "",
}


def _event(entity_id="ent_esc_1", snapshot=None, action="created"):
    return NeotomaEvent(
        entity_type="escalation",
        entity_id=entity_id,
        action=action,
        snapshot=snapshot or {},
    )


def _install_notifier(tmp_path, sent, deliver_kw=None):
    deliver_kw = deliver_kw if deliver_kw is not None else []

    def _deliver(m, force=False, email_eligible=True):
        deliver_kw.append({"email_eligible": email_eligible})
        sent.append(m)
        return True

    n = Notifier(rubric=_NEVER_SILENT)
    n._dedupe_path = tmp_path / "dedupe.json"
    n._digest_path = tmp_path / "digest.json"
    n._deliver = _deliver
    anthus._notifier = n
    return deliver_kw


@pytest.fixture(autouse=True)
def _reset_notifier():
    yield
    if hasattr(anthus, "_notifier"):
        del anthus._notifier


def test_anthus_escalation_empty_snapshot_email_eligible_false(
    tmp_path, caplog
):
    sent = []
    deliver_kw = _install_notifier(tmp_path, sent)
    with caplog.at_level(logging.WARNING):
        asyncio.run(anthus._handle_escalation(_event(snapshot={})))
    assert deliver_kw[0]["email_eligible"] is False
    assert "has no actionable text; email_eligible=False" in caplog.text
    assert "Escalation [unknown]:" in sent[0]


def test_anthus_escalation_title_fallback_used(tmp_path):
    sent = []
    _install_notifier(tmp_path, sent)
    asyncio.run(
        anthus._handle_escalation(_event(snapshot={"title": "disk full"}))
    )
    assert "disk full" in sent[0]


def test_anthus_escalation_reason_and_linked_task_fallbacks(tmp_path):
    sent = []
    _install_notifier(tmp_path, sent)
    asyncio.run(
        anthus._handle_escalation(_event(snapshot={"reason": "stuck deploy"}))
    )
    assert "stuck deploy" in sent[0]

    sent.clear()
    asyncio.run(
        anthus._handle_escalation(
            _event(
                entity_id="ent_esc_2",
                snapshot={"linked_task_title": "fix queue"},
            )
        )
    )
    assert "fix queue" in sent[0]


def test_anthus_escalation_second_observation_suppressed(tmp_path):
    sent = []
    _install_notifier(tmp_path, sent)
    ev = _event(snapshot={"summary": "needs operator"})
    asyncio.run(anthus._handle_escalation(ev))
    asyncio.run(anthus._handle_escalation(ev))
    assert len(sent) == 1


def test_anthus_escalation_resolved_clears_dedupe(tmp_path):
    sent = []
    _install_notifier(tmp_path, sent)
    ev = _event(snapshot={"summary": "open issue"})
    asyncio.run(anthus._handle_escalation(ev))
    asyncio.run(
        anthus._handle_escalation(
            _event(snapshot={"status": "resolved", "summary": "done"})
        )
    )
    assert len(sent) == 1
    asyncio.run(
        anthus._handle_escalation(
            _event(snapshot={"summary": "reopened issue"})
        )
    )
    assert len(sent) == 2
