"""Human assignment survives real dispatch entrances without a status rewrite."""

from __future__ import annotations

import asyncio

import pytest

import apis
import routing
from lib.capabilities import neotoma_http
from lib.daemon_runtime.sse_client import NeotomaEvent
import test_noowner_escalation as _noowner

_Notifier = _noowner._Notifier
_world = _noowner._world
_isolated_ledger = _noowner._isolated_ledger


@pytest.fixture
def profile(monkeypatch):
    calls = []

    def read(method, path, body, **kwargs):
        calls.append((method, path, body, kwargs))
        return {"entities": [{"snapshot": {
            "display_name": "Example Person", "first_name": "Example",
            "handle": "example-person",
        }}], "total": 1}

    monkeypatch.setattr(neotoma_http, "request_json", read)
    monkeypatch.delenv("OPERATOR_NAME", raising=False)
    monkeypatch.delenv("APIS_OPERATOR_LOGIN", raising=False)
    return calls


def test_configured_operator_display_name_is_human(monkeypatch):
    monkeypatch.setenv("OPERATOR_NAME", "Example Person")
    assert routing.resolve_skill(["finance"], assigned_to=" Example Person ") == routing.HUMAN_OWNED


def test_profile_display_name_and_handle_are_human(profile):
    for name in ("Example Person", "example-person", "Example"):
        assert routing.resolve_skill(["finance"], assigned_to=name) == routing.HUMAN_OWNED
    assert profile
    for method, path, body, kwargs in profile:
        assert (method, path) == ("POST", "/entities/query")
        assert body["entity_type"] == "operator_profile"
        assert 0 < kwargs["timeout"] <= 5


@pytest.mark.parametrize("trigger,override", [
    ("created", False), ("due_today", False), ("reconcile", False),
    ("watchdog_retry", False), ("build_released", False), ("approved", True),
])
@pytest.mark.parametrize("assignee", ["operator", "Example Person"])
def test_human_assignment_is_left_open_on_every_dispatch_entrance(
    _world, _isolated_ledger, profile, trigger, override, assignee,
):
    task = {"title": "Review the payment and merge the PR", "status": "open",
            "assigned_to": assignee, "tags": ["finance"], "blocked_reason": ""}
    notifier = _Notifier()
    asyncio.run(apis.dispatch_task(
        "ent_human_preserved", task, trigger=trigger, notifier=notifier,
        gate_override=override, snapshot_hydrated=True,
    ))
    assert task["status"] == "open" and task["blocked_reason"] == ""
    assert _world == {"statuses": [], "started": [], "spawns": []}
    assert not any("unroutable" in notice for notice in notifier.sent)
    assert not apis._unroutable._seen and not apis._unroutable._pending


@pytest.mark.parametrize("action", ["created", "due_today"])
def test_real_sse_handler_preserves_human_task(_world, _isolated_ledger, profile, monkeypatch, action):
    async def hydrated(event):
        event.hydrated = True

    monkeypatch.setattr(apis, "hydrate_snapshot", hydrated)
    monkeypatch.setattr(apis, "AUTO_EXECUTE", True)
    event = NeotomaEvent(entity_type="task", entity_id="ent_human_sse", action=action,
                        snapshot={"title": "Review the payment", "assigned_to": "Example Person",
                                  "status": "open", "tags": ["finance"]})
    asyncio.run(apis.handle_event(event, _Notifier()))
    assert _world == {"statuses": [], "started": [], "spawns": []}


def test_unrecognized_assignee_remains_unroutable(_world, _isolated_ledger, profile):
    notifier = _Notifier()
    asyncio.run(apis.dispatch_task(
        "ent_unknown_person", {"title": "Review notes", "status": "open",
                               "assigned_to": "Unregistered Person", "tags": []},
        trigger="created", notifier=notifier, snapshot_hydrated=True,
    ))
    assert [status for status, _ in _world["statuses"]] == [apis.TaskStatus.BLOCKED]
    assert _world["started"] == [] and _world["spawns"] == []
    assert any("unroutable" in notice for notice in notifier.sent)


def test_agent_route_does_not_read_profile(profile):
    assert routing.resolve_skill([], assigned_to="cicada") == "cicada"
    assert profile == []


def test_unreadable_profile_is_not_no_owner(_world, _isolated_ledger, monkeypatch, caplog):
    def unavailable(*_a, **_k):
        raise TimeoutError("synthetic private response must not be logged")

    monkeypatch.setattr(neotoma_http, "request_json", unavailable)
    notifier = _Notifier()
    asyncio.run(apis.dispatch_task(
        "ent_unknown_profile", {"title": "Review payment", "status": "open",
                                "assigned_to": "Example Person", "tags": ["finance"]},
        trigger="created", notifier=notifier, snapshot_hydrated=True,
    ))
    assert _world == {"statuses": [], "started": [], "spawns": []}
    assert "operator identity is unreadable" in caplog.text
    assert "synthetic private response" not in caplog.text
    assert len(notifier.sent) == 1 and "ent_unknown_profile" in notifier.sent[0]
    assert "ownership could not be verified" in notifier.sent[0]
    assert "synthetic private response" not in notifier.sent[0]


def test_human_branch_forgets_previously_unroutable_task(_world, _isolated_ledger, profile):
    apis._unroutable.note("ent_human_recovered", "Review notes", [], "Example Person")
    asyncio.run(apis.dispatch_task(
        "ent_human_recovered", {"title": "Review notes", "assigned_to": "Example Person", "status": "open"},
        trigger="created", notifier=_Notifier(), snapshot_hydrated=True,
    ))
    assert not apis._unroutable._seen and not apis._unroutable._pending
    assert _world["statuses"] == []


@pytest.mark.parametrize("response", [
    {"entities": [], "total": 1},
    {"entities": [{"snapshot": {"display_name": 42}}], "total": 1},
    {"entities": [{"snapshot": {"display_name": "Other Person"}}], "total": 9,
     "next_cursor": "more"},
])
def test_incomplete_identity_read_is_not_no_owner(_world, _isolated_ledger, monkeypatch, response):
    monkeypatch.setattr(neotoma_http, "request_json", lambda *_a, **_k: response)
    notifier = _Notifier()
    asyncio.run(apis.dispatch_task(
        "ent_partial_profile", {"title": "Review notes", "status": "open",
                                "assigned_to": "Example Person", "tags": []},
        trigger="created", notifier=notifier, snapshot_hydrated=True,
    ))
    assert _world == {"statuses": [], "started": [], "spawns": []}
    assert len(notifier.sent) == 1 and "ownership could not be verified" in notifier.sent[0]
