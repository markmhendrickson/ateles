"""Actual notifier condition lifecycle, with external delivery confined to captures."""

from __future__ import annotations

import asyncio
import copy
import threading

import pytest

import apis
import test_noowner_escalation as _noowner
from lib.capabilities import neotoma_http

_world = _noowner._world
_isolated_ledger = _noowner._isolated_ledger


@pytest.fixture
def profile_read(monkeypatch):
    state = {"reply": None, "calls": []}

    def read(method, path, body, **kwargs):
        assert (method, path, body, kwargs) == (
            "POST", "/entities/query",
            {"entity_type": "operator_profile", "include_snapshots": True, "limit": 5},
            {"timeout": 5},
        )
        state["calls"].append(True)
        if state["reply"] is None:
            raise TimeoutError("PRIVATE_PROFILE_RESPONSE")
        return copy.deepcopy(state["reply"])

    def forbidden(*_a, **_k):
        raise AssertionError("Ownership recovery must not enter an execution effect")

    monkeypatch.setattr(neotoma_http, "request_json", read)
    monkeypatch.setattr(apis, "_fly_defer_holder", lambda *_a, **_k: None)
    monkeypatch.setattr(apis, "_fly_run_refuses_dispatch", lambda *_a, **_k: False)
    for name in ("evaluate_gate", "write_checkpoint_brief", "write_assessment", "create_run_session"):
        monkeypatch.setattr(apis, name, forbidden)
    return state


def _actual_notifier(path, silent):
    notifier = apis.ApisNotifier.__new__(apis.ApisNotifier)
    notifier._rubric = {"timezone": "UTC"}
    notifier._notification_lock = threading.RLock()
    notifier._digest_path = path / "held.json"
    notifier._dedupe_path = path / "dedupe.json"
    notifier._email_primary = False
    notifier._notify_to = ""
    notifier._apprise = None
    notifier._last_unrouted = 0
    notifier._maybe_flush_digest = lambda: None
    notifier._in_silence_window = lambda: silent
    delivered = []
    notifier._deliver = lambda message, **_k: delivered.append(message) or True
    return notifier, delivered


def _dispatch(notifier, *, task_id="ent_warning_task", trigger="created", assignee="Example Person", hydrated=True):
    task = ({"title": "PRIVATE_TASK_TITLE", "description": "PRIVATE_TASK_BODY", "status": "open",
             "assigned_to": assignee, "blocked_reason": "", "tags": ["finance"]}
            if hydrated else {})
    asyncio.run(apis.dispatch_task(task_id, task, trigger=trigger, notifier=notifier,
                                  snapshot_hydrated=hydrated))
    if hydrated:
        assert task["status"] == "open" and task["blocked_reason"] == ""


def _notices(notifier, delivered, silent):
    return notifier._digest_queue if silent else delivered


@pytest.mark.parametrize("silent", [False, True])
def test_unchanged_outage_is_one_actual_delivery_or_hold(_world, _isolated_ledger, profile_read, tmp_path, silent, caplog):
    notifier, delivered = _actual_notifier(tmp_path, silent)
    for trigger in ("created", "reconcile", "due_today"):
        _dispatch(notifier, trigger=trigger)
    notices = _notices(notifier, delivered, silent)
    assert len(profile_read["calls"]) == 3
    assert len(notices) == 1 and len(notifier._dedupe_journal) == 1
    assert all("PRIVATE_" not in item and "Example Person" not in item for item in notices)
    assert "PRIVATE_" not in caplog.text
    assert _world == {"statuses": [], "started": [], "spawns": []}


@pytest.mark.parametrize("silent", [False, True])
def test_qualified_recovery_clears_the_key_and_recurrence_reports_once(_world, _isolated_ledger, profile_read, tmp_path, silent):
    notifier, delivered = _actual_notifier(tmp_path, silent)
    _dispatch(notifier)
    _dispatch(notifier, trigger="reconcile")
    assert len(notifier._dedupe_journal) == 1
    profile_read["reply"] = {"entities": [{"entity_type": "operator_profile", "snapshot": {"display_name": "Example Person"}}], "total": 1}
    _dispatch(notifier, trigger="reconcile")
    assert notifier._dedupe_journal == {}
    assert len(_notices(notifier, delivered, silent)) == 1
    profile_read["reply"] = None
    for trigger in ("created", "reconcile", "due_today"):
        _dispatch(notifier, trigger=trigger)
    assert len(_notices(notifier, delivered, silent)) == 2
    assert len(notifier._dedupe_journal) == 1
    assert _world == {"statuses": [], "started": [], "spawns": []}


@pytest.mark.parametrize("silent", [False, True])
def test_conditions_and_recovery_are_isolated_per_task(_world, _isolated_ledger, profile_read, tmp_path, silent):
    notifier, delivered = _actual_notifier(tmp_path, silent)
    for _ in range(3):
        _dispatch(notifier, task_id="ent_warning_a")
        _dispatch(notifier, task_id="ent_warning_b")
    assert len(_notices(notifier, delivered, silent)) == 2
    assert len(notifier._dedupe_journal) == 2
    profile_read["reply"] = {"entities": [{"snapshot": {"display_name": "Example Person"}}], "total": 1}
    _dispatch(notifier, task_id="ent_warning_a")
    assert len(notifier._dedupe_journal) == 1
    profile_read["reply"] = None
    _dispatch(notifier, task_id="ent_warning_b")
    _dispatch(notifier, task_id="ent_warning_a")
    assert len(_notices(notifier, delivered, silent)) == 3
    assert len(notifier._dedupe_journal) == 2
    assert _world == {"statuses": [], "started": [], "spawns": []}


def test_partial_identity_or_unread_task_does_not_clear_the_condition(_world, _isolated_ledger, profile_read, tmp_path):
    notifier, delivered = _actual_notifier(tmp_path, False)
    _dispatch(notifier)
    profile_read["reply"] = {"entities": [], "total": 1}
    _dispatch(notifier, trigger="reconcile")
    _dispatch(notifier, hydrated=False)
    assert len(notifier._dedupe_journal) == 1 and len(delivered) == 1
    profile_read["reply"] = None
    _dispatch(notifier)
    assert len(delivered) == 1
    assert _world == {"statuses": [], "started": [], "spawns": []}


def test_configured_owner_can_resolve_the_condition_without_another_profile_read(_world, _isolated_ledger, profile_read, tmp_path):
    notifier, delivered = _actual_notifier(tmp_path, False)
    _dispatch(notifier)
    assert len(profile_read["calls"]) == 1
    _dispatch(notifier, assignee="operator")
    assert len(profile_read["calls"]) == 1 and notifier._dedupe_journal == {}
    _dispatch(notifier)
    assert len(delivered) == 2
    assert _world == {"statuses": [], "started": [], "spawns": []}
