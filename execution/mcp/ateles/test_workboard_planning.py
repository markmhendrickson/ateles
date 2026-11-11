"""Session planning uses declared ranks, current edges and explicit coverage."""

import os

os.environ["ATELES_SKIP_DOTENV"] = "1"

import sys
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
