"""Effect tests for Anthus escalation Notifier dedupe (ateles#1165)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from lib.daemon_runtime import NeotomaEvent
from lib.notify import Notifier, Priority
from execution.daemons.anthus import anthus

# Empty silence window — CI after 22:00 Europe/Madrid must not hold
# OPERATOR_DECISION sends (same footgun as apis notify-dedupe tests).
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


# ── ateles#1216: severity-upgrade delivery, per-entity + priority scope ────


def test_anthus_escalation_priority_upgrade_sends_blocker(tmp_path):
    """A non-blocking notify followed by an upgrade to blocking on the SAME
    entity must deliver the BLOCKER notice once — the ateles#1216 defect was
    deduping by entity id alone, so the upgrade was suppressed as a repeat.
    A third identical BLOCKER observation must still be suppressed (true
    repeat)."""
    sent = []
    _install_notifier(tmp_path, sent)

    # 1) Non-blocking observation on entity E.
    asyncio.run(
        anthus._handle_escalation(
            _event(snapshot={"summary": "needs review", "blocking": False})
        )
    )
    assert len(sent) == 1
    assert "Escalation [unknown]: needs review" in sent[0]

    # 2) Same entity, now blocking=True — must deliver a SECOND notice (the
    # upgrade), not be suppressed as a repeat of the first.
    asyncio.run(
        anthus._handle_escalation(
            _event(snapshot={"summary": "now blocking", "blocking": True})
        )
    )
    assert len(sent) == 2
    assert "now blocking" in sent[1]

    # 3) A third, identical BLOCKER observation on the same entity is a true
    # repeat at the SAME priority and must be suppressed.
    asyncio.run(
        anthus._handle_escalation(
            _event(snapshot={"summary": "still blocking", "blocking": True})
        )
    )
    assert len(sent) == 2


def test_anthus_escalation_same_priority_repeat_still_suppressed(tmp_path):
    """Two non-blocking observations on the same entity: still deduped."""
    sent = []
    _install_notifier(tmp_path, sent)
    asyncio.run(
        anthus._handle_escalation(
            _event(snapshot={"summary": "first", "blocking": False})
        )
    )
    asyncio.run(
        anthus._handle_escalation(
            _event(snapshot={"summary": "second", "blocking": False})
        )
    )
    assert len(sent) == 1


def test_anthus_escalation_resolved_clears_both_priority_keys(tmp_path):
    """After an OPERATOR_DECISION notify then a BLOCKER upgrade on entity E,
    resolving the escalation must clear BOTH priority keys — clearing only
    one would leave a later reopen at the uncleared priority silently
    suppressed."""
    sent = []
    n = Notifier(rubric=_NEVER_SILENT)
    n._dedupe_path = tmp_path / "dedupe.json"

    def _deliver(m, force=False, email_eligible=True):
        sent.append(m)
        return True

    n._deliver = _deliver
    anthus._notifier = n

    od_key = f"escalation:ent_esc_1:{Priority.OPERATOR_DECISION.value}"
    blocker_key = f"escalation:ent_esc_1:{Priority.BLOCKER.value}"

    asyncio.run(
        anthus._handle_escalation(
            _event(snapshot={"summary": "first", "blocking": False})
        )
    )
    asyncio.run(
        anthus._handle_escalation(
            _event(snapshot={"summary": "upgraded", "blocking": True})
        )
    )
    assert n._is_duplicate(od_key)
    assert n._is_duplicate(blocker_key)

    asyncio.run(
        anthus._handle_escalation(_event(snapshot={"status": "resolved"}))
    )
    assert not n._is_duplicate(od_key)
    assert not n._is_duplicate(blocker_key)

    # Reopen at OPERATOR_DECISION priority must notify again (neither key
    # left stuck).
    asyncio.run(
        anthus._handle_escalation(
            _event(snapshot={"summary": "reopened", "blocking": False})
        )
    )
    assert len(sent) == 3
