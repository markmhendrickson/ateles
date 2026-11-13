"""Human assignment survives real dispatch entrances without a status rewrite."""

from __future__ import annotations

import asyncio
import copy

import pytest

import apis
import routing
from lib.capabilities import neotoma_http
from lib.daemon_runtime.sse_client import NeotomaEvent
import test_noowner_escalation as _noowner

_Notifier = _noowner._Notifier
_world = _noowner._world
_isolated_ledger = _noowner._isolated_ledger


@pytest.fixture(autouse=True)
def _ownership_paths_have_no_gate_or_session_effect(monkeypatch):
    def forbidden(*_a, **_k):
        raise AssertionError("Ownership-only routing must not enter a gate, assessment or run session")

    for name in ("evaluate_gate", "write_checkpoint_brief", "write_assessment", "create_run_session"):
        monkeypatch.setattr(apis, name, forbidden)


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


@pytest.mark.parametrize("human_first", [True, False])
def test_one_identity_read_decides_both_skill_and_role(
    _world, _isolated_ledger, monkeypatch, human_first,
):
    calls = []
    human = {"entities": [{"snapshot": {"display_name": "Example Person"}}], "total": 1}
    absent = {"entities": [], "total": 0}

    def changing_read(*_a, **_k):
        calls.append(True)
        return human if (len(calls) == 1) == human_first else absent

    monkeypatch.setattr(neotoma_http, "request_json", changing_read)
    asyncio.run(apis.dispatch_task(
        "ent_one_identity_read", {"title": "Review notes", "status": "open",
                                 "assigned_to": "Example Person", "tags": []},
        trigger="created", notifier=_Notifier(), snapshot_hydrated=True,
    ))
    assert len(calls) == 1, "skill and role must share the same ownership answer"
    assert _world["started"] == [] and _world["spawns"] == []
    assert [status for status, _ in _world["statuses"]] == ([] if human_first else [apis.TaskStatus.BLOCKED])


_OTHER_PROFILE = {"entity_type": "operator_profile", "snapshot": {"display_name": "Other Person"}}
_R1_REPORTED = [
    {"entities": []},
    {"entities": [], "total": None},
    {"entities": [_OTHER_PROFILE]},
    {"entities": [], "total": 0, "next_cursor": 0},
    {"entities": [{"snapshot": {"display_name": 42, "handle": "other-person"}}], "total": 1},
    {"entities": [{"entity_type": "task", "snapshot": {"display_name": "Other Person"}}], "total": 1},
    {"entities": [_OTHER_PROFILE, {"snapshot": {}}], "total": 2},
    {"entities": [], "total": 0, "read_contract": {"version": "1", "coverage": {"state": "partial"}}},
    {"entities": [], "total": 0, "read_contract": {"version": "1", "coverage": {"state": "unknown"}}},
]


@pytest.mark.parametrize("response", _R1_REPORTED)
def test_r1_reported_envelope_never_proves_negative_ownership(
    _world, _isolated_ledger, monkeypatch, response,
):
    monkeypatch.setattr(neotoma_http, "request_json", lambda *_a, **_k: response)
    notifier = _Notifier()
    asyncio.run(apis.dispatch_task(
        "ent_r1_unknown_owner", {"title": "Review notes", "status": "open",
                                 "assigned_to": "Example Person", "tags": []},
        trigger="created", notifier=notifier, snapshot_hydrated=True,
    ))
    assert _world == {"statuses": [], "started": [], "spawns": []}
    assert len(notifier.sent) == 1 and "ownership could not be verified" in notifier.sent[0]


def _complete_profile_contract(count):
    # Same shape as the configured HTTP response, with synthetic identities.
    return {"version": "1", "surface": "entity_collection",
            "mode": {"actual": "structured", "fallback_reason": None},
            "applied_scope": {"owner": "authenticated", "entity_types": ["operator_profile"],
                              "include_merged": False, "include_deleted": False,
                              "include_snapshots": True, "predicates": [],
                              "pagination": {"kind": "keyset", "limit": 5, "offset": 0}},
            "request_options": {"applied": ["entity_type", "include_snapshots", "limit"],
                                "normalized": [], "ignored": []},
            "coverage": {"kind": "predicate_population", "state": "complete", "reasons": [],
                         "returned_count": count,
                         "total": {"value": count, "relation": "exact", "unit": "entities"},
                         "page_exhausted": True, "scope_exhausted": True,
                         "continuation": {"kind": "none", "next_cursor": None, "next_offset": None}},
            "coherence": {"kind": "read_interval", "common_view": False,
                          "collection_and_total": "separate_acquisitions"}}


@pytest.mark.parametrize("name_field", ["first_name", "display_name", "handle"])
@pytest.mark.parametrize("state", ["complete", "unknown"])
def test_valid_returned_identity_is_positive_without_requiring_all_aliases(
    _world, _isolated_ledger, monkeypatch, name_field, state,
):
    contract = _complete_profile_contract(1)
    if state == "unknown":
        contract["coverage"].update(state="unknown", reasons=["unsupported_continuation"],
                                    scope_exhausted=None)
    response = {"entities": [{"entity_type": "operator_profile",
                              "snapshot": {name_field: "Example Person"}}],
                "total": 1, "offset": 0, "limit": 5, "read_contract": contract}
    monkeypatch.setattr(neotoma_http, "request_json", lambda *_a, **_k: response)
    notifier = _Notifier()
    asyncio.run(apis.dispatch_task(
        "ent_r1_positive_owner", {"title": "Review notes", "status": "open",
                                  "assigned_to": "Example Person", "tags": []},
        trigger="created", notifier=notifier, snapshot_hydrated=True,
    ))
    assert _world == {"statuses": [], "started": [], "spawns": []}
    assert notifier.sent == []


def test_complete_current_contract_proves_empty_profile_collection(_world, _isolated_ledger, monkeypatch):
    response = {"entities": [], "total": 0, "offset": 0, "limit": 5,
                "read_contract": _complete_profile_contract(0)}
    monkeypatch.setattr(neotoma_http, "request_json", lambda *_a, **_k: response)
    notifier = _Notifier()
    asyncio.run(apis.dispatch_task(
        "ent_r1_complete_empty", {"title": "Review notes", "status": "open",
                                  "assigned_to": "Example Person", "tags": []},
        trigger="created", notifier=notifier, snapshot_hydrated=True,
    ))
    assert [status for status, _ in _world["statuses"]] == [apis.TaskStatus.BLOCKED]
    assert _world["started"] == [] and _world["spawns"] == []


@pytest.mark.parametrize("path,value", [
    (("total",), False),
    (("next_cursor",), False),
    (("next_cursor",), ""),
    (("next_offset",), 0),
    (("offset",), True),
    (("limit",), False),
    (("has_more",), 0),
    (("truncated",), ""),
    (("complete",), 1),
    (("read_contract",), None),
    (("read_contract", "version"), "2"),
    (("read_contract", "surface"), "relationship_collection"),
    (("read_contract", "applied_scope", "entity_types"), ["task"]),
    (("read_contract", "applied_scope", "owner"), "foreign"),
    (("read_contract", "applied_scope", "include_merged"), True),
    (("read_contract", "applied_scope", "include_snapshots"), False),
    (("read_contract", "applied_scope", "predicates"), [{"field": "display_name"}]),
    (("read_contract", "applied_scope", "pagination", "offset"), 1),
    (("read_contract", "applied_scope", "pagination", "kind"), False),
    (("read_contract", "request_options", "ignored"), ["entity_type"]),
    (("read_contract", "coverage", "state"), None),
    (("read_contract", "coverage", "state"), "paginated"),
    (("read_contract", "coverage", "kind"), "returned_page"),
    (("read_contract", "coverage", "reasons"), ["ignored_scope_option"]),
    (("read_contract", "coverage", "returned_count"), False),
    (("read_contract", "coverage", "total", "value"), False),
    (("read_contract", "coverage", "total", "relation"), "lower_bound"),
    (("read_contract", "coverage", "total", "unit"), "observations"),
    (("read_contract", "coverage", "page_exhausted"), 1),
    (("read_contract", "coverage", "scope_exhausted"), None),
    (("read_contract", "coverage", "continuation"), []),
    (("read_contract", "coverage", "continuation", "kind"), "unsupported"),
    (("read_contract", "coverage", "continuation", "next_cursor"), False),
    (("read_contract", "coverage", "continuation", "next_offset"), 0),
])
def test_whole_collection_contract_class_preserves_unknown(
    _world, _isolated_ledger, monkeypatch, path, value,
):
    response = {"entities": [], "total": 0, "offset": 0, "limit": 5,
                "read_contract": copy.deepcopy(_complete_profile_contract(0))}
    target = response
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    monkeypatch.setattr(neotoma_http, "request_json", lambda *_a, **_k: response)
    notifier = _Notifier()
    asyncio.run(apis.dispatch_task(
        "ent_r1_contract_class", {"title": "Review notes", "status": "open",
                                  "assigned_to": "Example Person", "tags": []},
        trigger="created", notifier=notifier, snapshot_hydrated=True,
    ))
    assert _world == {"statuses": [], "started": [], "spawns": []}
    assert len(notifier.sent) == 1 and "ownership could not be verified" in notifier.sent[0]


@pytest.mark.parametrize("row", [
    {"entity_type": None, "snapshot": {"display_name": "Other Person"}},
    {"entity_id": 0, "snapshot": {"display_name": "Other Person"}},
    {"entity_id": "not-an-entity-id", "snapshot": {"display_name": "Other Person"}},
    {"snapshot": {"display_name": None, "handle": "other-person"}},
    {"snapshot": {"display_name": [], "handle": "other-person"}},
])
def test_malformed_row_does_not_disappear_beside_a_usable_profile(
    _world, _isolated_ledger, monkeypatch, row,
):
    response = {"entities": [_OTHER_PROFILE, row], "total": 2}
    monkeypatch.setattr(neotoma_http, "request_json", lambda *_a, **_k: response)
    notifier = _Notifier()
    asyncio.run(apis.dispatch_task(
        "ent_r1_malformed_row", {"title": "Review notes", "status": "open",
                                 "assigned_to": "Example Person", "tags": []},
        trigger="created", notifier=notifier, snapshot_hydrated=True,
    ))
    assert _world == {"statuses": [], "started": [], "spawns": []}
    assert len(notifier.sent) == 1 and "ownership could not be verified" in notifier.sent[0]


# These are the state-critical parts of the producer's current v1 contract.
# Sorting, timestamps and common_view are deliberately outside this proof.
_REQUIRED_NEGATIVE_PROOF = [
    ("version",), ("surface",), ("mode",), ("mode", "actual"), ("mode", "fallback_reason"),
    ("applied_scope",), ("applied_scope", "owner"), ("applied_scope", "entity_types"),
    ("applied_scope", "include_snapshots"), ("applied_scope", "include_merged"),
    ("applied_scope", "include_deleted"), ("applied_scope", "predicates"),
    ("applied_scope", "pagination"), ("applied_scope", "pagination", "kind"),
    ("applied_scope", "pagination", "offset"), ("applied_scope", "pagination", "limit"),
    ("request_options",), ("request_options", "ignored"),
    ("coverage",), ("coverage", "kind"), ("coverage", "state"), ("coverage", "reasons"),
    ("coverage", "returned_count"), ("coverage", "total"), ("coverage", "total", "value"),
    ("coverage", "total", "relation"), ("coverage", "total", "unit"),
    ("coverage", "page_exhausted"), ("coverage", "scope_exhausted"),
    ("coverage", "continuation"), ("coverage", "continuation", "kind"),
    ("coverage", "continuation", "next_cursor"), ("coverage", "continuation", "next_offset"),
]


def _required_contract_cases():
    original = _complete_profile_contract(0)
    cases = []
    for path in _REQUIRED_NEGATIVE_PROOF:
        expected = original
        for key in path:
            expected = expected[key]
        cases.append(pytest.param(path, "delete", None, id="delete-" + ".".join(path)))
        if expected is not None:
            cases.append(pytest.param(path, "replace", None, id="null-" + ".".join(path)))
        wrong_type = 0 if isinstance(expected, bool) else False if isinstance(expected, (int, type(None))) else [] if isinstance(expected, (str, dict)) else {}
        cases.append(pytest.param(path, "replace", wrong_type, id="type-" + ".".join(path)))
    return cases


@pytest.mark.parametrize("path,mutation,value", _required_contract_cases())
def test_present_current_contract_requires_the_whole_negative_proof(
    _world, _isolated_ledger, monkeypatch, path, mutation, value,
):
    contract = copy.deepcopy(_complete_profile_contract(0))
    target = contract
    for key in path[:-1]:
        target = target[key]
    if mutation == "delete":
        del target[path[-1]]
    else:
        target[path[-1]] = value
    response = {"entities": [], "total": 0, "offset": 0, "limit": 5, "read_contract": contract}
    monkeypatch.setattr(neotoma_http, "request_json", lambda *_a, **_k: response)
    notifier = _Notifier()
    asyncio.run(apis.dispatch_task(
        "ent_r3_missing_proof", {"title": "Review notes", "status": "open",
                                "assigned_to": "Example Person", "tags": []},
        trigger="created", notifier=notifier, snapshot_hydrated=True,
    ))
    assert _world == {"statuses": [], "started": [], "spawns": []}
    assert len(notifier.sent) == 1 and "ownership could not be verified" in notifier.sent[0]


@pytest.mark.parametrize("contract", [None, {}, {"coverage": {"state": "complete"}},
                                      {"version": "1", "surface": "entity_collection", "coverage": {"state": "complete"}}])
def test_present_sparse_contract_is_never_counted_legacy(_world, _isolated_ledger, monkeypatch, contract):
    response = {"entities": [], "total": 0, "offset": 0, "limit": 5, "read_contract": contract}
    monkeypatch.setattr(neotoma_http, "request_json", lambda *_a, **_k: response)
    notifier = _Notifier()
    asyncio.run(apis.dispatch_task(
        "ent_r3_sparse_proof", {"title": "Review notes", "status": "open",
                               "assigned_to": "Example Person", "tags": []},
        trigger="created", notifier=notifier, snapshot_hydrated=True,
    ))
    assert _world == {"statuses": [], "started": [], "spawns": []}
    assert len(notifier.sent) == 1 and "ownership could not be verified" in notifier.sent[0]


@pytest.mark.parametrize("foreign", ["row_type", "owner", "entity_types", "missing_scope"])
def test_a_matching_alias_cannot_hide_a_foreign_or_unqualified_identity_scope(
    _world, _isolated_ledger, monkeypatch, foreign,
):
    response = {"entities": [{"entity_type": "operator_profile", "snapshot": {"display_name": "Example Person"}}],
                "total": 1, "offset": 0, "limit": 5, "read_contract": _complete_profile_contract(1)}
    if foreign == "row_type":
        response["entities"][0]["entity_type"] = "task"
    elif foreign == "missing_scope":
        del response["read_contract"]["applied_scope"]
    else:
        response["read_contract"]["applied_scope"][foreign] = "foreign" if foreign == "owner" else ["task"]
    monkeypatch.setattr(neotoma_http, "request_json", lambda *_a, **_k: response)
    notifier = _Notifier()
    asyncio.run(apis.dispatch_task(
        "ent_r3_foreign_identity", {"title": "Review notes", "status": "open",
                                  "assigned_to": "Example Person", "tags": []},
        trigger="created", notifier=notifier, snapshot_hydrated=True,
    ))
    assert _world == {"statuses": [], "started": [], "spawns": []}
    assert len(notifier.sent) == 1 and "ownership could not be verified" in notifier.sent[0]


@pytest.mark.parametrize("missing", ["limit", "offset"])
def test_current_negative_proof_needs_the_actual_http_page_binding(_world, _isolated_ledger, monkeypatch, missing):
    response = {"entities": [], "total": 0, "offset": 0, "limit": 5, "read_contract": _complete_profile_contract(0)}
    del response[missing]
    monkeypatch.setattr(neotoma_http, "request_json", lambda *_a, **_k: response)
    notifier = _Notifier()
    asyncio.run(apis.dispatch_task(
        "ent_r3_page_binding", {"title": "Review notes", "status": "open",
                              "assigned_to": "Example Person", "tags": []},
        trigger="created", notifier=notifier, snapshot_hydrated=True,
    ))
    assert _world == {"statuses": [], "started": [], "spawns": []}
    assert len(notifier.sent) == 1 and "ownership could not be verified" in notifier.sent[0]


def test_a_full_keyset_page_cannot_claim_exhaustion(_world, _isolated_ledger, monkeypatch):
    contract = _complete_profile_contract(1)
    contract["applied_scope"]["pagination"]["limit"] = 1
    response = {"entities": [_OTHER_PROFILE], "total": 1, "offset": 0, "limit": 1, "read_contract": contract}
    monkeypatch.setattr(neotoma_http, "request_json", lambda *_a, **_k: response)
    notifier = _Notifier()
    asyncio.run(apis.dispatch_task(
        "ent_r3_full_page", {"title": "Review notes", "status": "open",
                           "assigned_to": "Example Person", "tags": []},
        trigger="created", notifier=notifier, snapshot_hydrated=True,
    ))
    assert _world == {"statuses": [], "started": [], "spawns": []}
    assert len(notifier.sent) == 1 and "ownership could not be verified" in notifier.sent[0]


def test_configured_human_guard_never_reads_a_malformed_profile(_world, _isolated_ledger, monkeypatch):
    def no_read(*_a, **_k):
        raise AssertionError("Configured operator recognition does not query profiles")

    monkeypatch.setenv("OPERATOR_NAME", "Example Person")
    monkeypatch.setattr(neotoma_http, "request_json", no_read)
    notifier = _Notifier()
    asyncio.run(apis.dispatch_task(
        "ent_r3_configured_human", {"title": "Review notes", "status": "open",
                                  "assigned_to": "Example Person", "tags": []},
        trigger="created", notifier=notifier, snapshot_hydrated=True,
    ))
    assert _world == {"statuses": [], "started": [], "spawns": []}
    assert notifier.sent == []
