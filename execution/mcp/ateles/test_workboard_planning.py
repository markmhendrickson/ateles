"""Session planning uses declared ranks, current edges and explicit coverage."""

import os

os.environ["ATELES_SKIP_DOTENV"] = "1"

import sys
import copy
import asyncio
import json
import shutil
import socket
import subprocess
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import server as srv
from workboard_planning import planning_declaration, session_planning_tree, session_state_counts


def parent(kind, name=None):
    return {"entity_id": "ent_" + kind, "entity_type": kind, "title": name or kind}


def task(identifier="ent_work", ascent=None, **extra):
    return {"kind": "task", "entity_id": identifier, "work": identifier,
            "priority": "recorded", "state": "Recorded state: open", "next_action": "Review",
            "planning_ascent": ascent or [], "planning_read_gaps": [], **extra}


def test_arbitrary_registered_names_form_one_high_to_low_tree():
    marks = {"sprint": {"level": 3, "read_only": False},
             "purpose": {"level": 20, "read_only": True}}
    rows = [task(ascent=[parent("sprint"), parent("purpose")])]
    tree = session_planning_tree(rows, marks, complete=True, gaps=[])
    nodes = {node["entity_id"]: node for node in tree["nodes"]}
    assert tree["roots"] == ["ent_purpose"]
    assert nodes["ent_purpose"]["read_only"] is True
    assert nodes["ent_sprint"]["parent_id"] == "ent_purpose"
    assert nodes["ent_work"]["parent_id"] == "ent_sprint"
    assert nodes["ent_purpose"]["counts"] == {"tasks": 1, "recorded": 1}
    assert tree["qualified_tasks"] == tree["unique_tasks"] == 1


def test_missing_parent_is_unplanned_instead_of_a_fabricated_higher_record():
    marks = {"sprint": {"level": 3, "read_only": False},
             "purpose": {"level": 20, "read_only": True}}
    tree = session_planning_tree([task(ascent=[parent("sprint")])], marks, complete=True, gaps=[])
    assert not tree["nodes"] and not tree["qualified_tasks"]
    assert "ends before" in tree["unplanned"][0]["reason"]
    assert tree["level_coverage"][1]["complete_tasks"] == 1


def test_no_rank_is_inferred_from_familiar_type_names():
    tree = session_planning_tree([task(ascent=[parent("plan"), parent("mission")])], {}, complete=True, gaps=[])
    assert tree["nodes"] == []
    assert tree["unplanned"] and tree["qualified_tasks"] == 0


def test_incomplete_registry_does_not_make_encountered_type_the_highest():
    tree = session_planning_tree([task(ascent=[parent("sprint")])],
        {"sprint": {"level": 3, "read_only": False}}, complete=False, gaps=["Unknown registry coverage"])
    assert tree["nodes"] == [] and tree["unknown_tasks"] == 1
    assert tree["level_coverage"][0]["complete_tasks"] is None


def test_task_parent_prefix_is_supported_without_inventing_a_planning_rank():
    aggregate = {"entity_id": "ent_parent", "entity_type": "task", "title": "Aggregate task"}
    rows = [task(ascent=[aggregate, parent("purpose")])]
    tree = session_planning_tree(rows, {"purpose": {"level": 20, "read_only": True}}, complete=True, gaps=[])
    nodes = {node["entity_id"]: node for node in tree["nodes"]}
    assert nodes["ent_parent"]["level"] is None
    assert nodes["ent_work"]["parent_id"] == "ent_parent"
    assert nodes["ent_parent"]["parent_id"] == "ent_purpose"


def test_counts_use_unique_membership_and_do_not_promote_saved_completion():
    rows = [task(priority="recorded"), task(priority="recorded"), task("ent_other", priority="blocked")]
    assert session_state_counts(rows, [0, 1, 2]) == {"tasks": 2, "recorded": 1, "blocked": 1}
    rows[1]["priority"] = "active"
    assert session_state_counts(rows, [0, 1]) == {"tasks": 1, "recorded": 1}


def test_rank_validation_is_strict_and_accepts_finite_fractional_levels():
    assert planning_declaration({"level": 2.5}) == {"level": 2.5, "read_only": False}
    for invalid in [None, [], {}, {"level": True}, {"level": float("inf")},
                    {"level": "2"}, {"level": 2, "read_only": "yes"}]:
        assert planning_declaration(invalid) is None


def test_actual_workboard_read_exposes_session_tree_without_a_parallel_store():
    digest = {"entity_type": "session_digest", "entity_id": "ent_digest",
              "snapshot": {"session_key": "synthetic:tree", "tasks_claimed": ["ent_work"]}}
    rows = [task(ascent=[parent("purpose")])]
    registry = ({"purpose": {"level": 20, "read_only": True}}, True, [])
    with patch.object(srv, "_find_session_digest", return_value=digest), \
         patch.object(srv, "_get", return_value={"entity_type": "plan", "snapshot": {"title": "Display only"}}), \
         patch.object(srv, "_claim_row", return_value=rows[0]), \
         patch.object(srv, "_planning_registry", return_value=registry, create=True):
        result = srv._get_session_workboard("synthetic:tree", "ent_display")
    assert result.get("planning", {}).get("roots") == ["ent_purpose"]
    assert result["planning"]["qualified_tasks"] == 1
    assert result["planning"]["scope"].startswith("Only this session")


def test_ascent_depth_is_open_instead_of_eight_planning_levels():
    records = {}
    for i in range(12):
        edges = ([{"source_entity_id": "ent_" + str(i), "target_entity_id": "ent_" + str(i + 1),
                   "relationship_type": "PART_OF", "is_live": 1}] if i < 11 else [])
        records[f"/entities/ent_{i}/relationships?expand_entities=true"] = {"relationships": edges, "total": len(edges)}
        records[f"/entities/ent_{i}"] = {"entity_type": "stage_" + str(i), "snapshot": {"title": "Stage " + str(i)}}
    with patch.object(srv, "_get", side_effect=records.get):
        ascent, gaps = srv._planning_ascent("ent_0", {})
    assert not gaps and len(ascent) == 11
    assert ascent[-1]["entity_id"] == "ent_11"


def test_planning_read_budget_is_unknown_coverage_not_a_fabricated_root():
    with patch.object(srv, "_get") as read:
        ascent, gaps = srv._planning_ascent("ent_work", {"_planning_reads_remaining": 0})
    assert ascent == [] and gaps == ["Planning read budget exhausted; ascent coverage is unknown"]
    read.assert_not_called()


def test_duplicate_task_coverage_cannot_select_a_convenient_parent():
    rows = [task(ascent=[parent("purpose")]), task(ascent=[])]
    tree = session_planning_tree(rows, {"purpose": {"level": 20, "read_only": True}}, complete=True, gaps=[])
    assert not tree["nodes"] and not tree["qualified_tasks"]
    assert len(tree["unplanned"]) == 2
    assert tree["level_coverage"][0]["complete_tasks"] == 0


def registry_page():
    rows = [{"entity_type": "purpose", "schema_version": "1.0", "field_count": 3,
             "planning": {"level": 2.5, "read_only": True}},
            {"entity_type": "contact", "schema_version": "1.0", "field_count": 7}]
    return {"schemas": rows, "total": 2, "limit": 5000, "offset": 0,
            "registry_coverage": {"version": "1", "state": "complete", "source": "registry",
                "reasons": [], "exhausted": True, "total": 2, "returned_count": 2,
                "scope": {"owner": "authenticated", "keyword": None, "entity_type": None}}}


def test_registry_summary_uses_one_scoped_complete_declaration_location():
    response = registry_page()
    response["schemas"][1]["metadata"] = {"planning": {"level": 900}}
    with patch.object(srv, "_get", return_value=response) as read:
        cache = {}
        marks, complete, gaps = srv._planning_registry(cache)
        assert srv._planning_registry(cache) == (marks, complete, gaps)
    read.assert_called_once_with("/schemas?summary=true&limit=5000&offset=0")
    assert marks == {"purpose": {"level": 2.5, "read_only": True}}
    assert complete and not gaps


def test_partial_or_malformed_registry_never_qualifies_a_highest_level():
    mutations = [
        lambda p: p.pop("registry_coverage"),
        lambda p: p["registry_coverage"].update(state="unknown"),
        lambda p: p["registry_coverage"].update(source="code_fallback"),
        lambda p: p["registry_coverage"].update(reasons=["Unknown acquisition"]),
        lambda p: p["registry_coverage"].update(exhausted=False),
        lambda p: p["registry_coverage"].pop("exhausted"),
        lambda p: p["registry_coverage"].update(total=None),
        lambda p: p["registry_coverage"].update(total=True),
        lambda p: p["registry_coverage"].update(returned_count=1),
        lambda p: p["registry_coverage"]["scope"].update(owner="global"),
        lambda p: p["registry_coverage"]["scope"].update(keyword="purpose"),
        lambda p: p["registry_coverage"]["scope"].pop("entity_type"),
        lambda p: p["registry_coverage"]["scope"].update(entity_type="purpose"),
        lambda p: p.update(total=3),
        lambda p: p.update(offset=1),
        lambda p: p.update(next_cursor=""),
        lambda p: p.update(next_cursor=False),
        lambda p: p.update(has_more=True),
        lambda p: p.update(complete=False),
        lambda p: p["schemas"][0].update(planning={"level": True}),
        lambda p: p["schemas"][0].update(planning={"level": 2, "read_only": "yes"}),
        lambda p: p["schemas"][0].update(planning_error=None),
        lambda p: p["schemas"][1].update(entity_type="purpose"),
        lambda p: p["schemas"][0].update(entity_type="task"),
    ]
    for mutate in mutations:
        response = copy.deepcopy(registry_page())
        mutate(response)
        with patch.object(srv, "_get", return_value=response):
            marks, complete, gaps = srv._planning_registry({})
        assert marks == {} and complete is False and gaps


def test_registry_through_actual_mcp_handler_and_bundled_view(tmp_path):
    from mcp.types import CallToolRequest, CallToolRequestParams
    from lib.daemon_runtime.agent_loader import AgentLoader

    digest = {"entity_type": "session_digest", "entity_id": "ent_digest",
              "snapshot": {"session_key": "synthetic:inbox", "tasks_claimed": ["ent_work"]}}
    registry = registry_page()
    records = {
        "/schemas?summary=true&limit=5000&offset=0": registry,
        "/entities/ent_plan": {"entity_type": "plan", "snapshot": {"title": "Display context"}},
        "/entities/ent_work": {"entity_type": "task", "snapshot": {"title": "Current task", "status": "open"}},
        "/entities/ent_purpose": {"entity_type": "purpose", "snapshot": {"title": "Registered direction"}},
    }
    for key, edges in {
        "ent_work": [{"relationship_type": "PART_OF", "source_entity_id": "ent_work", "target_entity_id": "ent_purpose", "is_live": 1}],
        "ent_purpose": [],
    }.items():
        records[f"/entities/{key}/relationships?expand_entities=true"] = {"relationships": edges, "total": len(edges)}

    async def read():
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Network denied")), \
             patch.object(AgentLoader, "render_policy_prompt", return_value=""), \
             patch.object(srv, "render_server_instructions", return_value="Synthetic"), \
             patch.object(srv, "_find_session_digest", return_value=digest), \
             patch.object(srv, "_get", side_effect=records.get), \
             patch.object(srv, "_retrieve_page", return_value={"entities": [], "total": 0}):
            server = srv.create_server()
            response = await server.request_handlers[CallToolRequest](CallToolRequest(
                method="tools/call", params=CallToolRequestParams(name="get_session_workboard",
                    arguments={"session_key": "synthetic:inbox", "display_plan_id": "ent_plan", "include_history": True})))
            return response.root

    result = asyncio.run(read())
    assert not result.isError
    data = result.structuredContent
    assert data["planning"]["roots"] == ["ent_purpose"]
    assert data["planning"]["qualified_tasks"] == 1
    assert data["rows"][1]["state"] == "Recorded state: open"
    assert "Only this session" in result.content[0].text
    node = shutil.which("node")
    if node:
        fixture = tmp_path / "planning-result.json"
        fixture.write_text(json.dumps(data))
        here = Path(__file__).parent
        view = subprocess.run([node, str(here / "test_workboard_bridge.cjs"), str(here / "workboard.html"), str(fixture), "planning"], capture_output=True, text=True, timeout=15)
        assert view.returncode == 0, view.stdout + view.stderr


RANKS = {"sprint": {"level": 3, "read_only": False},
         "plan": {"level": 10, "read_only": False},
         "project": {"level": 20, "read_only": False},
         "mission": {"level": 30, "read_only": True}}


def partial(tree):
    return {node["entity_id"]: node for node in tree.get("unplanned_nodes", [])}


def test_orphan_plan_with_several_tasks_is_one_branch_under_unplanned():
    marks = {"plan": RANKS["plan"], "project": RANKS["project"]}
    rows = [task("ent_a", [parent("plan", "Orphan plan")]), task("ent_b", [parent("plan", "Orphan plan")], priority="blocked")]
    tree = session_planning_tree(rows, marks, complete=True, gaps=[])
    assert tree["nodes"] == [] and tree["roots"] == [] and tree["qualified_tasks"] == 0
    assert [entry["row_index"] for entry in tree["unplanned"]] == [0, 1]
    assert all(not entry["unknown"] and "ends before" in entry["reason"] for entry in tree["unplanned"])
    nodes = partial(tree)
    assert tree["unplanned_roots"] == ["ent_plan"]
    assert nodes["ent_plan"]["parent_id"] is None and nodes["ent_plan"]["level"] == 10
    assert nodes["ent_a"]["parent_id"] == nodes["ent_b"]["parent_id"] == "ent_plan"
    assert nodes["ent_plan"]["counts"] == {"tasks": 2, "recorded": 1, "blocked": 1}
    assert tree["level_coverage"][0]["complete_tasks"] == 0


def test_partial_higher_branch_keeps_its_observed_declared_prefix():
    aggregate = {"entity_id": "ent_parent", "entity_type": "task", "title": "Aggregate task"}
    rows = [task("ent_a", [aggregate, parent("sprint"), parent("plan"), parent("project")]),
            task("ent_full", [{"entity_id": "ent_s2", "entity_type": "sprint", "title": "S2"},
                              {"entity_id": "ent_p2", "entity_type": "plan", "title": "P2"},
                              {"entity_id": "ent_r2", "entity_type": "project", "title": "R2"},
                              parent("mission")])]
    tree = session_planning_tree(rows, RANKS, complete=True, gaps=[])
    assert tree["qualified_tasks"] == 1 and tree["roots"] == ["ent_mission"]
    nodes = partial(tree)
    assert tree["unplanned_roots"] == ["ent_project"]
    assert [nodes[i]["parent_id"] for i in ("ent_a", "ent_parent", "ent_sprint", "ent_plan")] == \
        ["ent_parent", "ent_sprint", "ent_plan", "ent_project"]
    assert nodes["ent_parent"]["level"] is None and "ent_full" not in nodes
    assert not set(nodes) & {node["entity_id"] for node in tree["nodes"]}


def test_identical_duplicate_membership_shares_one_task_node():
    rows = [task("ent_a", [parent("plan")]), task("ent_a", [parent("plan")])]
    tree = session_planning_tree(rows, RANKS, complete=True, gaps=[])
    assert partial(tree)["ent_a"]["row_indexes"] == [0, 1]
    assert partial(tree)["ent_plan"]["counts"] == {"tasks": 1, "recorded": 1}


def test_uncertain_or_contradictory_partial_paths_stay_flat_and_explained():
    cases = {
        "incomplete registry": ([task(ascent=[parent("plan")])], False),
        "undeclared only": ([task(ascent=[parent("initiative")])], True),
        "malformed": ([task(ascent=[parent("plan"), None])], True),
        "malformed list": ([task(ascent="plan")], True),
        "cycle": ([task(ascent=[parent("plan"), parent("plan")])], True),
        "read gap": ([task(ascent=[parent("plan")], planning_read_gaps=["Planning parent ent_x could not be read"])], True),
        "ambiguous": ([task(ascent=[parent("plan")], planning_read_gaps=["Multiple planning parents are recorded for ent_plan; no parent was selected"])], True),
        "conflicting partial parents": ([task("ent_a", [parent("sprint"), parent("plan")]),
                                         task("ent_b", [parent("sprint"), {"entity_id": "ent_q", "entity_type": "plan", "title": "Q"}])], True),
        "disputed duplicate": ([task("ent_a", [parent("plan")]), task("ent_a", [])], True),
        "record also in qualified tree": ([task("ent_a", [parent("plan")]),
                                           task("ent_b", [parent("plan"), parent("project"), parent("mission")])], True),
        "no task identity": ([task(None, [parent("plan")])], True),
    }
    for name, (rows, complete) in cases.items():
        tree = session_planning_tree(rows, RANKS if complete else {"plan": RANKS["plan"]}, complete=complete, gaps=[])
        assert tree["unplanned_nodes"] == [] and tree["unplanned_roots"] == [], name
        assert tree["unplanned"] and all(entry["reason"] for entry in tree["unplanned"]), name
        assert len(tree["unplanned"]) + sum(len(n["row_indexes"]) for n in tree["nodes"]) == len(rows), name


def test_orphan_plan_branch_through_actual_mcp_handler_and_bundled_view(tmp_path):
    from mcp.types import CallToolRequest, CallToolRequestParams
    from lib.daemon_runtime.agent_loader import AgentLoader

    digest = {"entity_type": "session_digest", "entity_id": "ent_digest",
              "snapshot": {"session_key": "synthetic:inbox", "tasks_claimed": ["ent_one", "ent_two", "ent_full"]}}
    registry = registry_page()
    registry["schemas"] = [
        {"entity_type": "plan", "schema_version": "1.0", "field_count": 3, "planning": {"level": 10}},
        {"entity_type": "project", "schema_version": "1.0", "field_count": 3, "planning": {"level": 20, "read_only": True}},
    ]
    records = {
        "/schemas?summary=true&limit=5000&offset=0": registry,
        "/entities/ent_plan": {"entity_type": "plan", "snapshot": {"title": "Display context"}},
        "/entities/ent_one": {"entity_type": "task", "snapshot": {"title": "First orphan task", "status": "open"}},
        "/entities/ent_two": {"entity_type": "task", "snapshot": {"title": "Second orphan task", "status": "open"}},
        "/entities/ent_full": {"entity_type": "task", "snapshot": {"title": "Complete task", "status": "open"}},
        "/entities/ent_orphan": {"entity_type": "plan", "snapshot": {"title": "Existing orphan plan"}},
        "/entities/ent_planned": {"entity_type": "plan", "snapshot": {"title": "Planned plan"}},
        "/entities/ent_project": {"entity_type": "project", "snapshot": {"title": "Registered project"}},
    }
    edge = lambda source, target: {"relationship_type": "PART_OF", "source_entity_id": source, "target_entity_id": target, "is_live": 1}
    for key, edges in {"ent_one": [edge("ent_one", "ent_orphan")], "ent_two": [edge("ent_two", "ent_orphan")],
                       "ent_full": [edge("ent_full", "ent_planned")], "ent_planned": [edge("ent_planned", "ent_project")],
                       "ent_orphan": [], "ent_project": []}.items():
        records[f"/entities/{key}/relationships?expand_entities=true"] = {"relationships": edges, "total": len(edges)}

    async def read():
        with patch.object(socket.socket, "connect", side_effect=AssertionError("Network denied")), \
             patch.object(AgentLoader, "render_policy_prompt", return_value=""), \
             patch.object(srv, "render_server_instructions", return_value="Synthetic"), \
             patch.object(srv, "_find_session_digest", return_value=digest), \
             patch.object(srv, "_get", side_effect=records.get), \
             patch.object(srv, "_retrieve_page", return_value={"entities": [], "total": 0}):
            server = srv.create_server()
            response = await server.request_handlers[CallToolRequest](CallToolRequest(
                method="tools/call", params=CallToolRequestParams(name="get_session_workboard",
                    arguments={"session_key": "synthetic:inbox", "display_plan_id": "ent_plan", "include_history": True})))
            return response.root

    result = asyncio.run(read())
    assert not result.isError
    data = result.structuredContent["planning"]
    assert data["roots"] == ["ent_project"] and data["qualified_tasks"] == 1 and data["unique_tasks"] == 3
    assert data["unplanned_roots"] == ["ent_orphan"]
    assert partial(data)["ent_orphan"]["counts"]["tasks"] == 2
    assert "Unplanned observed branch" in result.content[0].text and "Existing orphan plan" in result.content[0].text
    node = shutil.which("node")
    assert node, "the bundled view must be exercised"
    fixture = tmp_path / "orphan-result.json"
    fixture.write_text(json.dumps(result.structuredContent))
    here = Path(__file__).parent
    view = subprocess.run([node, str(here / "test_workboard_bridge.cjs"), str(here / "workboard.html"), str(fixture), "orphan"], capture_output=True, text=True, timeout=15)
    assert view.returncode == 0, view.stdout + view.stderr
