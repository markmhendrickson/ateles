"""Causal workboard and MCP App acceptance, with synthetic source records only."""

import os

os.environ["ATELES_SKIP_DOTENV"] = "1"

from datetime import datetime, timedelta, timezone
import sys
import json
import subprocess
import shutil
import pytest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import server as srv


def test_app_is_bound_to_a_declared_resource():
    tool = next(t for t in srv.TOOLS if t.name == "get_session_workboard")
    assert (
        tool.model_dump(by_alias=True)["_meta"]["ui"]["resourceUri"]
        == srv.WORKBOARD_RESOURCE_URI
    )
    resource = srv._workboard_resource()
    assert str(resource.uri) == srv.WORKBOARD_RESOURCE_URI
    assert resource.mimeType == "text/html;profile=mcp-app"


def test_old_activity_does_not_prove_working_now():
    records = {
        "/entities/ent_lease": {
            "entity_type": "lease",
            "snapshot": {
                "status": "held",
                "task_id": "ent_task",
                "holder": "worker",
                "runner_id": "runner-1",
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=5)
                ).isoformat(),
            },
        },
        "/entities/ent_task": {
            "entity_type": "task",
            "snapshot": {"status": "executing", "admission_class": "workflow_entering"},
        },
        "/entities/ent_activity": {
            "entity_type": "agent_session",
            "snapshot": {
                "kind": "autonomous",
                "task_id": "ent_task",
                "native_session_id": "runner-1",
                "runner_id": "runner-1",
                "last_activity_at": "2020-01-01T00:00:00Z",
                "observed_at": "2020-01-01T00:00:00Z",
            },
        },
    }
    edge = _lease_edge()
    edge["snapshot"]["runner_id"] = "runner-1"
    records["/entities/ent_task/relationships?expand_entities=true"] = (
        _relationship_response(edge)
    )
    with (
        patch.object(srv, "_retrieve_page", return_value={"entities": [], "total": 0}),
        patch.object(srv, "_get", side_effect=records.get),
    ):
        row = srv._claim_row(
            {
                "claim": "Recorded run",
                "evidence_pointers": ["ent_task"],
                "workboard": {
                    "live_execution_evidence": {
                        "lease_ref": "ent_lease",
                        "activity_ref": "ent_activity",
                    }
                },
            }
        )
    assert row["state"] == "Recorded state: executing"


def test_stored_claimable_boolean_is_not_a_canonical_predicate():
    assert not srv._is_explicitly_claimable({"status": "open", "claimable": True})


def test_broken_task_read_keeps_the_row_and_reports_unknown():
    claim = {
        "claim": "A claimed result",
        "status_claimed": "done",
        "evidence_pointers": ["ent_task"],
    }
    with patch.object(srv, "_get", return_value=None):
        row = srv._claim_row(claim)
    assert row["priority"] == "recorded"
    assert row["state_source"] == "session_digest_history"
    assert row["read_gaps"]


def test_current_id_only_digest_rows_are_not_dropped_or_promoted():
    claim = {
        "task_entity_id": "ent_task",
        "recorded_status": "done",
        "claimed": False,
        "executor": "historical-executor",
        "next_action": "Historical follow-up",
    }
    task = {
        "entity_type": "task",
        "snapshot": {
            "title": "Current task title",
            "status": "blocked",
            "executor": "session",
        },
    }
    with patch.object(
        srv,
        "_get",
        side_effect=lambda path: task if path == "/entities/ent_task" else None,
    ):
        row = srv._claim_row(claim)
    assert row["work"] == "Current task title"
    assert row["state"] == "Recorded state: blocked"
    assert row["runner"] == "unknown" and row["lease_holder"] == "unknown"
    assert row["recorded_executor"] == "session"
    assert row["recorded_session_next_action"] == "Historical follow-up"


def test_unreadable_id_only_digest_row_keeps_explicit_history():
    with patch.object(srv, "_get", return_value=None):
        row = srv._claim_row(
            {
                "task_entity_id": "ent_task",
                "recorded_status": "awaiting_approval",
                "claimed": False,
            }
        )
    assert row and row["state"] == "Recorded state: awaiting_approval"
    assert row["state_source"] == "session_digest_history" and row["read_gaps"]


def test_checkpoint_must_belong_to_this_task():
    checkpoint = {
        "entity_type": "checkpoint_brief",
        "snapshot": {"status": "awaiting_operator", "task_id": "ent_other"},
    }
    records = {
        "/entities/ent_task": {"entity_type": "task", "snapshot": {"status": "open"}},
        "/entities/ent_checkpoint": checkpoint,
    }
    with (
        patch.object(srv, "_retrieve_page", return_value={"entities": [], "total": 0}),
        patch.object(srv, "_get", side_effect=records.get),
    ):
        row = srv._claim_row(
            {
                "claim": "Owned work",
                "evidence_pointers": ["ent_task"],
                "workboard": {"operator_checkpoint_ref": "ent_checkpoint"},
            }
        )
    assert row["priority"] == "recorded"


def test_resource_and_structured_fallback_through_real_stdio():
    import test_server_smoke as smoke

    proc = smoke._start_server()
    try:
        assert (
            smoke._initialize(proc)[1]["result"]["capabilities"]["resources"]
            is not None
        )
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        proc.stdin.flush()

        def call(rid, method, params):
            return smoke._send_and_collect(
                proc,
                [{"jsonrpc": "2.0", "id": rid, "method": method, "params": params}],
                {rid},
            )[rid]

        tools = call(2, "tools/list", {})["result"]["tools"]
        tool = next(t for t in tools if t["name"] == "get_session_workboard")
        uri = tool["_meta"]["ui"]["resourceUri"]
        listing = call(3, "resources/list", {})["result"]["resources"]
        assert listing[0]["uri"] == uri
        resource = call(4, "resources/read", {"uri": uri})["result"]["contents"][0]
        assert resource["mimeType"] == "text/html;profile=mcp-app"
        assert resource["_meta"]["ui"]["csp"]["connectDomains"] == []
        assert (
            "ui/initialize" in resource["text"]
            and "ui/notifications/tool-result" in resource["text"]
        )
        result = call(
            5,
            "tools/call",
            {
                "name": "get_session_workboard",
                "arguments": {
                    "session_key": "synthetic:missing",
                    "display_plan_id": "ent_plan",
                },
            },
        )["result"]
        assert result["isError"] is True
        assert result["structuredContent"]["error"]
        assert result["content"][0]["text"]
    finally:
        smoke._stop_server(proc)


def test_actual_view_bridge_behavior():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is unavailable for the bundled view bridge test")
    here = Path(__file__).parent
    result = subprocess.run(
        [node, str(here / "test_workboard_bridge.cjs"), str(here / "workboard.html")],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "cancellation and refresh passed" in result.stdout


def test_derived_result_through_real_stdio_with_synthetic_record_source(
    tmp_path, monkeypatch
):
    import test_server_smoke as smoke

    fixture = tmp_path / "synthetic_server.py"
    here = Path(__file__).parent
    fixture.write_text(
        "import sys,asyncio\nsys.path.insert(0,"
        + repr(str(here))
        + ")\nimport server as s\n"
        "s.render_server_instructions=lambda:'Synthetic workboard fixture'\n"
        "s._retrieve_page=lambda *a,**k:({'entities':[],'total':0} if a[0]=='checkpoint_brief' else {'entities':[{'entity_id':'ent_digest','entity_type':'session_digest','snapshot':{'session_key':'synthetic:inbox','tasks_claimed':[{'task_entity_id':'ent_task','recorded_status':'executing','claimed':False,'executor':'session','next_action':'Historical follow-up'}]}}],'total':1})\n"
        "records={'/entities/ent_plan':{'entity_type':'plan','snapshot':{'title':'Primary plan','status':'active'}},'/entities/ent_task':{'entity_type':'task','snapshot':{'title':'Current task','status':'open','assigned_to':'principal','next_action':'Review the current artifact'}},'/entities/ent_task/relationships?expand_entities=true':{'relationships':[{'relationship_type':'PART_OF','source_entity_id':'ent_task','target_entity_id':'ent_plan','is_live':1}],'total':1},'/entities/ent_plan/relationships?expand_entities=true':{'relationships':[],'total':0}}\n"
        "s._get=lambda path:records.get(path)\nasyncio.run(s.main())\n"
    )
    monkeypatch.setattr(smoke, "SERVER", fixture)
    proc = smoke._start_server()
    try:
        smoke._initialize(proc)
        proc.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n"
        )
        proc.stdin.flush()
        response = smoke._send_and_collect(
            proc,
            [
                {
                    "jsonrpc": "2.0",
                    "id": 7,
                    "method": "tools/call",
                    "params": {
                        "name": "get_session_workboard",
                        "arguments": {
                            "session_key": "synthetic:inbox",
                            "display_plan_id": "ent_plan",
                            "include_history": True,
                        },
                    },
                }
            ],
            {7},
        )[7]["result"]
        assert not response.get("isError")
        data = response["structuredContent"]
        assert data["rows"][1]["work"] == "Current task"
        assert data["rows"][1]["state"] == "Recorded state: open"
        assert data["rows"][1]["next_action"] == "Review the current artifact"
        assert data["rows"][1]["planning_ascent"][0]["entity_id"] == "ent_plan"
        fallback = response["content"][0]["text"]
        assert "Current task" in fallback
        assert "Historical follow-up" in fallback
        assert "Canonical claimability" in fallback
        assert "Planning context" in fallback
        assert data["history"]["tasks_claimed"][0]["task_entity_id"] == "ent_task"
        assert response["_meta"]["ui"]["resourceUri"] == srv.WORKBOARD_RESOURCE_URI
    finally:
        smoke._stop_server(proc)


def _lease_edge():
    now = datetime.now(timezone.utc)
    return {
        "relationship_type": "LEASE",
        "relationship_key": "LEASE:ent_worker:ent_task",
        "source_entity_id": "ent_worker",
        "target_entity_id": "ent_task",
        "is_live": 1,
        "snapshot": {
            "claimed_at": (now - timedelta(minutes=1)).isoformat(),
            "expires_at": (now + timedelta(minutes=1)).isoformat(),
            "runner_id": "run",
        },
    }


def _activity_edge():
    return {
        "relationship_type": "REFERS_TO",
        "source_entity_id": "ent_activity",
        "target_entity_id": "ent_task",
        "is_live": 1,
    }


def _relationship_response(*edges):
    return {"outgoing": [], "incoming": list(edges), "relationships": list(edges)}


@pytest.mark.parametrize(
    "variant",
    [
        "expired",
        "returned",
        "foreign_task",
        "wrong_runner",
        "future_claim",
        "naive_claim",
        "deleted",
        "duplicate",
        "stale_activity",
        "interactive_activity",
        "activity_before_claim",
        "foreign_activity",
        "missing_runner",
    ],
)
def test_lease_refusals_preserve_recorded_state(variant):
    edge = _lease_edge()
    activity = {
        "kind": "autonomous",
        "trigger_ref": "ent_task",
        "native_session_id": "run",
        "last_activity_at": datetime.now(timezone.utc).isoformat(),
    }
    if variant == "expired":
        edge["snapshot"]["expires_at"] = "2020-01-01T00:00:00Z"
    if variant == "returned":
        edge["snapshot"]["returned_at"] = datetime.now(timezone.utc).isoformat()
    if variant == "foreign_task":
        edge["target_entity_id"] = "ent_other"
    if variant == "wrong_runner":
        edge["snapshot"]["runner_id"] = "other"
    if variant == "future_claim":
        edge["snapshot"]["claimed_at"] = "2999-01-01T00:00:00Z"
    if variant == "naive_claim":
        edge["snapshot"]["claimed_at"] = "2026-01-01T00:00:00"
    if variant == "deleted":
        edge["is_live"] = 0
    if variant == "stale_activity":
        activity["last_activity_at"] = "2020-01-01T00:00:00Z"
    if variant == "interactive_activity":
        activity["kind"] = "interactive"
    if variant == "activity_before_claim":
        activity["last_activity_at"] = (
            datetime.now(timezone.utc) - timedelta(minutes=2)
        ).isoformat()
    if variant == "foreign_activity":
        activity["trigger_ref"] = "ent_other"
    if variant == "missing_runner":
        del edge["snapshot"]["runner_id"]
    edges = [edge, dict(edge)] if variant == "duplicate" else [edge]
    records = {
        "/entities/ent_task/relationships?expand_entities=true": _relationship_response(
            *edges, _activity_edge()
        ),
        "/entities/ent_activity": {
            "entity_type": "agent_session",
            "snapshot": activity,
        },
    }
    with patch.object(srv, "_get", side_effect=records.get):
        assert not srv._live_execution_evidence(
            {"live_execution_evidence": {"activity_ref": "ent_activity"}}, "ent_task"
        )


def test_recent_bound_lease_and_activity_control():
    records = {
        "/entities/ent_task/relationships?expand_entities=true": _relationship_response(
            _lease_edge(), _activity_edge()
        ),
        "/entities/ent_activity": {
            "entity_type": "agent_session",
            "snapshot": {
                "kind": "autonomous",
                "trigger_ref": "ent_task",
                "native_session_id": "run",
                "last_activity_at": datetime.now(timezone.utc).isoformat(),
            },
        },
    }
    with patch.object(srv, "_get", side_effect=records.get):
        result = srv._live_execution_evidence(
            {"live_execution_evidence": {"activity_ref": "ent_activity"}}, "ent_task"
        )
    assert result["lease_holder"] == "ent_worker" and result["runner"] == "run"


@pytest.mark.parametrize(
    "page",
    [
        None,
        {},
        {"entities": [], "total": 1},
        {"entities": [], "total": 0, "next_cursor": "more"},
        {
            "entities": [
                {
                    "entity_type": "session_digest",
                    "snapshot": {"session_key": "different"},
                }
            ],
            "total": 1,
        },
    ],
)
def test_digest_read_failure_is_not_empty_success(page):
    with (
        patch.object(srv, "_retrieve_page", return_value=page),
        patch.object(srv, "_describe_transport_error", return_value=None),
    ):
        result = srv._get_session_workboard("synthetic:inbox", "ent_plan")
    assert result.get("error")


def test_optional_registry_rows_preserve_native_meanings():
    page = {
        "entities": [
            {
                "entity_id": "ent_session",
                "entity_type": "agent_session",
                "snapshot": {
                    "harness": "a-harness",
                    "native_session_id": "native",
                    "kind": "interactive",
                    "status": "active",
                    "holder": "native-holder",
                    "coordination_role": "desk",
                },
            }
        ],
        "total": 1,
    }
    with patch.object(srv, "_retrieve_page", return_value=page):
        rows, gaps, coverage = srv._workboard_session_rows()
    assert coverage["complete"] and not gaps
    row = rows[0]
    assert row["state"] == "Native state: active" and row["priority"] == "recorded"
    assert (
        row["native_kind"] == "interactive" and row["native_holder"] == "native-holder"
    )
    assert row["lease_holder"] == "unknown" and row["assigned_principal"] == "unknown"


def test_workboard_request_performs_only_read_operations():
    requests = []

    def request(method, path, **kwargs):
        requests.append((method, path))
        if (method, path) == ("POST", "/entities/query"):
            return {
                "entities": [
                    {
                        "entity_id": "ent_digest",
                        "entity_type": "session_digest",
                        "snapshot": {
                            "session_key": "synthetic:inbox",
                            "tasks_claimed": [],
                        },
                    }
                ],
                "total": 1,
            }
        if (method, path) == ("GET", "/entities/ent_plan"):
            return {
                "entity_type": "plan",
                "snapshot": {"title": "Current plan", "status": "draft"},
            }
        raise AssertionError(f"unexpected operation {method} {path}")

    with patch.object(srv, "_request", side_effect=request):
        result = srv._get_session_workboard("synthetic:inbox", "ent_plan")
    assert not result.get("error")
    assert requests == [("POST", "/entities/query"), ("GET", "/entities/ent_plan")]


@pytest.mark.parametrize(
    "change",
    [
        {"next_cursor": "more"},
        {"next_cursor": ""},
        {"next_cursor": False},
        {"next_cursor": 0},
        {"total": 2},
        {"complete": False},
        {"has_more": True},
    ],
)
def test_relationship_completeness_signals_agree(change):
    envelope = {**_relationship_response(_lease_edge()), **change}
    with patch.object(srv, "_get", return_value=envelope):
        rels, error = srv._workboard_relationships("ent_task", {})
    assert rels is None and error


@pytest.mark.parametrize(
    "fields",
    [
        None,
        {},
        {"task_entity_id": "ent_other", "status": "awaiting_operator"},
        {"task_entity_id": "ent_task", "status": "unrecognized"},
        {
            "task_entity_id": "ent_task",
            "status": "awaiting_operator",
            "resolved_dispatched": "unreadable",
        },
    ],
)
def test_checkpoint_semantic_coverage_is_unknown(fields):
    checkpoint = {
        "entity_id": "ent_checkpoint",
        "entity_type": "checkpoint_brief",
        "snapshot": fields,
    }
    gaps = []
    with (
        patch.object(
            srv, "_retrieve_page", return_value={"entities": [checkpoint], "total": 1}
        ),
        patch.object(srv, "_get", return_value=checkpoint),
    ):
        assert not srv._open_operator_checkpoint(
            {}, {"operator_checkpoint_ref": "ent_checkpoint"}, "ent_task", gaps
        )
    assert any("unknown" in gap for gap in gaps)


def test_producer_session_is_discovered_without_digest_locator():
    from lib.daemon_runtime.session_finalize import build_run_session_payload

    payload = build_run_session_payload(task_id="ent_task", agent="role", run_key="run")
    session = {
        **payload["entities"][1],
        "last_activity_at": datetime.now(timezone.utc).isoformat(),
    }
    edge = _lease_edge()
    edge["snapshot"]["runner_id"] = session["native_session_id"]
    refers = {
        "relationship_type": "REFERS_TO",
        "source_entity_id": "ent_activity",
        "target_entity_id": "ent_task",
        "is_live": 1,
    }
    records = {
        "/entities/ent_task/relationships?expand_entities=true": _relationship_response(
            edge, refers
        ),
        "/entities/ent_activity": {
            "entity_id": "ent_activity",
            "entity_type": "agent_session",
            "snapshot": session,
        },
    }
    with patch.object(srv, "_get", side_effect=records.get):
        evidence = srv._live_execution_evidence({}, "ent_task")
    assert evidence["runner"] == session["native_session_id"]
    assert "task_id" not in session and "task_entity_id" not in session


def test_fallback_preserves_coverage_and_requested_history():
    import asyncio
    import socket
    from mcp.types import CallToolRequest, CallToolRequestParams
    from lib.daemon_runtime.agent_loader import AgentLoader

    result = {
        "session_key": "synthetic:inbox",
        "display_plan_id": "ent_plan",
        "rows": [],
        "table": "Empty table",
        "read_gaps": ["Supporting reads unknown"],
        "capability_gaps": ["Claimability unknown"],
        "history": {
            "tasks_claimed": [{"claim": "Historical work"}],
            "artifacts": ["Historical artifact"],
        },
    }

    async def call():
        with (
            patch.object(
                socket.socket, "connect", side_effect=AssertionError("Network denied")
            ),
            patch.object(AgentLoader, "render_policy_prompt", return_value=""),
            patch.object(srv, "render_server_instructions", return_value="Synthetic"),
            patch.dict(
                srv.TOOL_HANDLERS, {"get_session_workboard": lambda args: result}
            ),
        ):
            server = srv.create_server()
            response = await server.request_handlers[CallToolRequest](
                CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(
                        name="get_session_workboard",
                        arguments={
                            "session_key": "synthetic:inbox",
                            "display_plan_id": "ent_plan",
                        },
                    ),
                )
            )
            return response.root.content[0].text

    text = asyncio.run(call())
    for expected in [
        "Supporting reads unknown",
        "Claimability unknown",
        "Historical work",
        "Historical artifact",
    ]:
        assert expected in text


@pytest.mark.parametrize(
    "claims",
    [
        [{}],
        [{"task_entity_id": "invalid"}],
        [{"claim": "Historical", "task_entity_id": "invalid"}],
        [[], {"task_entity_id": "ent_task"}],
    ],
)
def test_unrenderable_claims_report_partial_coverage(claims):
    digest = {
        "entity_id": "ent_digest",
        "entity_type": "session_digest",
        "snapshot": {"session_key": "synthetic:inbox", "tasks_claimed": claims},
    }
    with (
        patch.object(srv, "_find_session_digest", return_value=digest),
        patch.object(
            srv,
            "_get",
            return_value={"entity_type": "plan", "snapshot": {"title": "Plan"}},
        ),
        patch.object(srv, "_retrieve_page", return_value={"entities": [], "total": 0}),
    ):
        result = srv._get_session_workboard(
            "synthetic:inbox", "ent_plan", include_history=True
        )
    assert result["read_gaps"]
    assert result["history"]["tasks_claimed"] == claims


@pytest.mark.parametrize(
    "variant",
    [
        "wrong_type",
        "foreign_task",
        "missing_task",
        "conflicting_task",
        "malformed_snapshot",
        "unknown_state",
        "resolved_open",
        "malformed_resolution",
        "missing_read",
    ],
)
@pytest.mark.parametrize("direct", [False, True])
def test_checkpoint_all_semantic_variants(variant, direct):
    row = {
        "entity_id": "ent_checkpoint",
        "entity_type": "checkpoint_brief",
        "snapshot": {"task_entity_id": "ent_task", "status": "awaiting_operator"},
    }
    fields = row["snapshot"]
    if variant == "wrong_type":
        row["entity_type"] = "task"
    if variant == "foreign_task":
        fields["task_entity_id"] = "ent_other"
    if variant == "missing_task":
        del fields["task_entity_id"]
    if variant == "conflicting_task":
        fields["task_id"] = "ent_other"
    if variant == "malformed_snapshot":
        row["snapshot"] = []
    if variant == "unknown_state":
        fields["status"] = "unknown"
    if variant == "resolved_open":
        fields["resolved_dispatched"] = True
    if variant == "malformed_resolution":
        fields["resolved_at"] = "unreadable"
    if variant == "missing_read":
        row = None
    page = (
        {"entities": [], "total": 0}
        if direct
        else ({"entities": [row], "total": 1} if row is not None else None)
    )
    gaps = []
    with (
        patch.object(srv, "_get", return_value=row),
        patch.object(srv, "_retrieve_page", return_value=page),
    ):
        assert not srv._open_operator_checkpoint(
            {},
            {"operator_checkpoint_ref": "ent_checkpoint"} if direct else {},
            "ent_task",
            gaps,
        )
    assert any("unknown" in gap for gap in gaps)


@pytest.mark.parametrize(
    "status",
    [
        "awaiting_operator",
        "approved",
        "rejected",
        "approved_no_release",
        "approved_requires_fresh_approval",
    ],
)
def test_checkpoint_known_direct_state_controls(status):
    row = {
        "entity_type": "checkpoint_brief",
        "snapshot": {"task_entity_id": "ent_task", "status": status},
    }
    gaps = []
    with (
        patch.object(srv, "_get", return_value=row),
        patch.object(srv, "_retrieve_page", return_value={"entities": [], "total": 0}),
    ):
        found = srv._open_operator_checkpoint(
            {}, {"operator_checkpoint_ref": "ent_checkpoint"}, "ent_task", gaps
        )
    assert found == (status == "awaiting_operator")
    assert not gaps


@pytest.mark.parametrize(
    "variant",
    [
        "no_link",
        "wrong_trigger",
        "wrong_runner",
        "stale",
        "missing",
        "partial_relationships",
        "duplicate_session",
    ],
)
def test_actual_producer_discovery_negative_controls(variant):
    from lib.daemon_runtime.session_finalize import build_run_session_payload

    payload = build_run_session_payload(task_id="ent_task", agent="role", run_key="run")
    session = {
        **payload["entities"][1],
        "last_activity_at": datetime.now(timezone.utc).isoformat(),
    }
    edge = _lease_edge()
    edge["snapshot"]["runner_id"] = session["native_session_id"]
    refs = [_activity_edge()]
    if variant == "no_link":
        refs = []
    if variant == "wrong_trigger":
        session["trigger_ref"] = "ent_other"
    if variant == "wrong_runner":
        session["native_session_id"] = "other"
    if variant == "stale":
        session["last_activity_at"] = "2020-01-01T00:00:00Z"
    if variant == "duplicate_session":
        refs.append({**_activity_edge(), "source_entity_id": "ent_second"})
    relationships = _relationship_response(edge, *refs)
    if variant == "partial_relationships":
        relationships["next_cursor"] = "more"
    record = {"entity_type": "agent_session", "snapshot": session}
    records = {
        "/entities/ent_task/relationships?expand_entities=true": relationships,
        "/entities/ent_activity": record,
        "/entities/ent_second": record,
    }
    if variant == "missing":
        del records["/entities/ent_activity"]
    gaps = []
    with patch.object(srv, "_get", side_effect=records.get):
        assert not srv._live_execution_evidence({}, "ent_task", read_gaps=gaps)
    assert gaps


def test_actual_producer_through_tool_and_bundled_view(tmp_path):
    import asyncio
    import socket
    from mcp.types import CallToolRequest, CallToolRequestParams
    from lib.daemon_runtime.agent_loader import AgentLoader
    from lib.daemon_runtime.session_finalize import build_run_session_payload

    payload = build_run_session_payload(task_id="ent_task", agent="role", run_key="run")
    session = {
        **payload["entities"][1],
        "last_activity_at": datetime.now(timezone.utc).isoformat(),
    }
    lease = _lease_edge()
    lease["snapshot"]["runner_id"] = session["native_session_id"]
    parent = {
        "relationship_type": "PART_OF",
        "source_entity_id": "ent_task",
        "target_entity_id": "ent_plan",
        "is_live": 1,
    }
    incoming = [lease, _activity_edge()]
    records = {
        "/entities/ent_task": {
            "entity_type": "task",
            "snapshot": {
                "title": "Current producer-bound task",
                "status": "executing",
                "admission_class": "workflow_entering",
            },
        },
        "/entities/ent_activity": {"entity_type": "agent_session", "snapshot": session},
        "/entities/ent_plan": {"entity_type": "plan", "snapshot": {"title": "Plan"}},
        "/entities/ent_task/relationships?expand_entities=true": {
            "outgoing": [parent],
            "incoming": incoming,
            "relationships": [parent, *incoming],
        },
        "/entities/ent_plan/relationships?expand_entities=true": _relationship_response(),
    }
    digest = {
        "entity_id": "ent_digest",
        "entity_type": "session_digest",
        "snapshot": {
            "session_key": "synthetic:inbox",
            "tasks_claimed": [{"task_entity_id": "ent_task"}],
            "artifacts": ["Historical artifact"],
        },
    }

    def page(kind, **kwargs):
        return (
            {"entities": [digest], "total": 1}
            if kind == "session_digest"
            else {"entities": [], "total": 0}
        )

    async def read():
        with (
            patch.object(
                socket.socket, "connect", side_effect=AssertionError("Network denied")
            ),
            patch.object(AgentLoader, "render_policy_prompt", return_value=""),
            patch.object(srv, "render_server_instructions", return_value="Synthetic"),
            patch.object(srv, "_get", side_effect=records.get),
            patch.object(srv, "_retrieve_page", side_effect=page),
        ):
            server = srv.create_server()
            response = await server.request_handlers[CallToolRequest](
                CallToolRequest(
                    method="tools/call",
                    params=CallToolRequestParams(
                        name="get_session_workboard",
                        arguments={
                            "session_key": "synthetic:inbox",
                            "display_plan_id": "ent_plan",
                            "include_history": True,
                        },
                    ),
                )
            )
            return response.root

    result = asyncio.run(read())
    assert not result.isError
    data = result.structuredContent
    row = data["rows"][1]
    assert (
        row["state"] == "Working now" and row["runner"] == session["native_session_id"]
    )
    assert row["planning_ascent"][0]["entity_id"] == "ent_plan"
    assert not data["read_gaps"]
    assert "Claimability" not in row["state"]
    assert "Canonical claimability" in result.content[0].text
    node = shutil.which("node")
    if node:
        fixture = tmp_path / "synthetic-result.json"
        fixture.write_text(json.dumps(data))
        here = Path(__file__).parent
        view = subprocess.run(
            [
                node,
                str(here / "test_workboard_bridge.cjs"),
                str(here / "workboard.html"),
                str(fixture),
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert view.returncode == 0, view.stdout + view.stderr
        assert "cancellation and refresh passed" in view.stdout
