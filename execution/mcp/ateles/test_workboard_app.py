"""Causal workboard and MCP App acceptance, with synthetic source records only."""

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
            "entity_type": "harness_event",
            "snapshot": {
                "task_id": "ent_task",
                "runner_id": "runner-1",
                "observed_at": "2020-01-01T00:00:00Z",
            },
        },
    }
    with patch.object(srv, "_get", side_effect=records.get):
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


def test_checkpoint_must_belong_to_this_task():
    checkpoint = {
        "entity_type": "checkpoint_brief",
        "snapshot": {"status": "awaiting_operator", "task_id": "ent_other"},
    }
    records = {
        "/entities/ent_task": {"entity_type": "task", "snapshot": {"status": "open"}},
        "/entities/ent_checkpoint": checkpoint,
    }
    with patch.object(srv, "_get", side_effect=records.get):
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
        "s._retrieve_page=lambda *a,**k:{'entities':[{'entity_id':'ent_digest','entity_type':'session_digest','snapshot':{'session_key':'synthetic:inbox','tasks_claimed':[{'claim':'Old claim','status_claimed':'executing','evidence_pointers':['ent_task']}]}}],'total':1}\n"
        "records={'/entities/ent_plan':{'entity_type':'plan','snapshot':{'title':'Primary plan','status':'active'}},'/entities/ent_task':{'entity_type':'task','snapshot':{'title':'Current task','status':'open','assigned_to':'principal','next_action':'Review the current artifact'}},'/entities/ent_task/relationships?expand_entities=true':{'relationships':[{'relationship_type':'PART_OF','source_entity_id':'ent_task','target_entity_id':'ent_plan'}],'total':1},'/entities/ent_plan/relationships?expand_entities=true':{'relationships':[],'total':0}}\n"
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
        assert "Current task" in response["content"][0]["text"]
        assert response["_meta"]["ui"]["resourceUri"] == srv.WORKBOARD_RESOURCE_URI
    finally:
        smoke._stop_server(proc)


@pytest.mark.parametrize(
    "change",
    [
        {"expires_at": "2020-01-01T00:00:00Z"},
        {"status": "returned"},
        {"task_id": "ent_other"},
        {"runner_id": "other"},
    ],
)
def test_lease_refusals_preserve_recorded_state(change):
    lease = {
        "status": "held",
        "task_id": "ent_task",
        "holder": "worker",
        "runner_id": "run",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
        **change,
    }
    records = {
        "/entities/ent_lease": {"entity_type": "lease", "snapshot": lease},
        "/entities/ent_activity": {
            "entity_type": "agent_session",
            "snapshot": {
                "kind": "autonomous",
                "task_id": "ent_task",
                "native_session_id": "run",
                "last_activity_at": datetime.now(timezone.utc).isoformat(),
            },
        },
    }
    with patch.object(srv, "_get", side_effect=records.get):
        assert not srv._live_execution_evidence(
            {
                "live_execution_evidence": {
                    "lease_ref": "ent_lease",
                    "activity_ref": "ent_activity",
                }
            },
            "ent_task",
        )


def test_recent_bound_lease_and_activity_control():
    records = {
        "/entities/ent_lease": {
            "entity_type": "lease",
            "snapshot": {
                "status": "held",
                "task_id": "ent_task",
                "holder": "worker",
                "runner_id": "run",
                "expires_at": (
                    datetime.now(timezone.utc) + timedelta(minutes=1)
                ).isoformat(),
            },
        },
        "/entities/ent_activity": {
            "entity_type": "agent_session",
            "snapshot": {
                "kind": "autonomous",
                "task_id": "ent_task",
                "native_session_id": "run",
                "last_activity_at": datetime.now(timezone.utc).isoformat(),
            },
        },
    }
    with patch.object(srv, "_get", side_effect=records.get):
        result = srv._live_execution_evidence(
            {
                "live_execution_evidence": {
                    "lease_ref": "ent_lease",
                    "activity_ref": "ent_activity",
                }
            },
            "ent_task",
        )
    assert result["lease_holder"] == "worker" and result["runner"] == "run"


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
