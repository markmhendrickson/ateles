"""Console S5 planning tree: assembly, honesty rules, and reuse of the workboard reads."""

import os

os.environ["ATELES_SKIP_DOTENV"] = "1"

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from execution.mcp.ateles import planning_tree as pt  # noqa: E402


def anc(kind, ident, title=None):
    return {"entity_id": ident, "entity_type": kind, "title": title or ident}


def trow(ident, bucket="recorded", ascent=None, gaps=None, working=None, status="open"):
    return {
        "entity_id": ident, "title": ident, "status": status, "bucket": bucket,
        "ascent": ascent if ascent is not None else [], "planning_gaps": gaps or [],
        "read_gaps": [], "working": working,
    }


LADDER = [anc("plan", "ent_plan"), anc("project", "ent_proj"), anc("strategy", "ent_strat"), anc("mission", "ent_mis")]


def build(rows, customers=None, sessions=None, declarations=None, complete=False):
    return pt.assemble_tree(
        rows, declarations=declarations or {}, registry_complete=complete,
        node_labels={}, customers=customers or {}, sessions=sessions or [],
    )


def node(tree, ident):
    return next(n for n in tree["nodes"] if n["entity_id"] == ident)


def test_tree_follows_part_of_upward_to_the_root_without_a_hardcoded_ladder():
    # Arbitrary type names: the shape comes from the edges, never from the names.
    ascent = [anc("zeta", "ent_z"), anc("omega", "ent_o")]
    tree = build([trow("ent_t", ascent=ascent)])
    assert tree["roots"] == ["ent_o"]
    assert node(tree, "ent_o")["children"] == ["ent_z"]
    assert node(tree, "ent_z")["children"] == ["ent_t"]
    assert tree["levels_known"] is False
    assert node(tree, "ent_z")["level"] is None


def test_levels_come_from_the_registry_declaration_only_when_given():
    decl = {"plan": {"level": 1, "read_only": False}}
    tree = build([trow("ent_t", ascent=LADDER)], declarations=decl, complete=True)
    assert node(tree, "ent_plan")["level"] == 1
    assert node(tree, "ent_proj")["level"] is None
    assert tree["levels_known"] is True


def test_counts_roll_up_and_done_is_not_open():
    rows = [
        trow("ent_a", "recorded", LADDER),
        trow("ent_b", "blocked", LADDER, status="blocked"),
        trow("ent_c", "operator-needed", LADDER),
        trow("ent_d", "done", LADDER, status="done"),
        trow("ent_e", "active", LADDER, working={"lease_holder": "ent_h", "runner": "r1"}),
    ]
    tree = build(rows)
    counts = node(tree, "ent_mis")["counts"]
    assert counts == {"tasks": 5, "open": 4, "done": 1, "blocked": 1, "operator": 1, "working": 1}
    (project,) = tree["projects"]
    assert (project["open"], project["done"], project["blocked"], project["operator"], project["working"]) == (4, 1, 1, 1, 1)


def test_working_now_requires_the_lease_evidence_not_an_active_bucket_alone():
    # A bucket of "active" without lease evidence in the row is not counted as working.
    tree = build([trow("ent_a", "active", LADDER, working=None)])
    assert node(tree, "ent_proj")["counts"]["working"] == 0
    assert node(tree, "ent_a")["working"] is None


def test_customer_is_shown_on_the_project_row():
    cust = {"ent_proj": [{"entity_id": "ent_org", "name": "Acme", "entity_type": "organization"}]}
    tree = build([trow("ent_a", ascent=LADDER)], customers=cust)
    assert tree["projects"][0]["customers"][0]["name"] == "Acme"


def test_unplanned_held_and_unknown_are_three_different_facts():
    rows = [
        trow("ent_u", gaps=["No planning parent is recorded for ent_u"]),
        trow("ent_h", gaps=["Multiple planning parents are recorded for ent_h; no parent was selected"]),
        trow("ent_k", gaps=["Planning read budget exhausted; ascent coverage is unknown"]),
        trow("ent_p", ascent=[anc("plan", "ent_plan")], gaps=["Planning parent ent_x could not be read"]),
    ]
    tree = build(rows)
    by_id = {u["entity_id"]: u["placement"] for u in tree["unplanned"]}
    assert by_id == {"ent_u": "unplanned", "ent_h": "held", "ent_k": "unknown", "ent_p": "unknown"}
    assert tree["nodes"] == []  # nothing unplaced is guessed into the tree


def test_a_record_with_two_parents_is_reported_not_silently_resolved():
    rows = [
        trow("ent_a", ascent=[anc("plan", "ent_plan"), anc("project", "ent_p1")]),
        trow("ent_b", ascent=[anc("plan", "ent_plan"), anc("project", "ent_p2")]),
    ]
    tree = build(rows)
    assert tree["conflicts"] == ["ent_plan"]


def test_sessions_reverse_index_is_recorded_links_only():
    sessions = [
        {"entity_id": "ent_s1", "work": "codex: s1", "native_harness": "codex",
         "state": "Native state: active", "native_last_activity_at": "x", "native_task_id": "ent_a"},
        {"entity_id": "ent_s2", "work": "other", "native_task_id": "ent_not_in_tree"},
    ]
    tree = build([trow("ent_a", ascent=LADDER)], sessions=sessions)
    assert [s["entity_id"] for s in node(tree, "ent_a")["sessions"]] == ["ent_s1"]
    assert tree["projects"][0]["sessions_linked"] == 1
    # A recorded link never makes the task "working now".
    assert node(tree, "ent_a")["working"] is None


def test_classify_gaps():
    assert pt.classify_gaps([], [anc("plan", "p")]) == ("planned", None)
    assert pt.classify_gaps(["No planning parent is recorded for x"], [])[0] == "unplanned"
    # An empty ascent with no stated reason is never read as "unplanned": fail closed.
    assert pt.classify_gaps([], [])[0] == "unknown"


def test_the_ascent_is_the_workboards_own_function_not_a_copy():
    # Parity by construction: the module calls the workboard's ascent and
    # bucket, so a change there is a change here.
    assert pt.S._planning_ascent.__module__.endswith("ateles.server") or pt.S._planning_ascent.__module__ == "server"
    src = Path(pt.__file__).read_text()
    assert "S._planning_ascent(" in src and "S._live_execution_evidence(" in src and "S._bucket_for(" in src


def _entity(ident, status="open", **extra):
    return {"entity_id": ident, "entity_type": "task", "snapshot": {"title": ident, "status": status, **extra}}


def test_task_row_uses_the_workboard_ascent_and_only_leased_work_is_working():
    cache = {}
    with patch.object(pt.S, "_planning_ascent", return_value=([anc("plan", "ent_plan")], [])), \
         patch.object(pt.S, "_live_execution_evidence",
                      return_value={"lease_holder": "ent_h", "runner": "r"}) as live:
        leased = pt._task_row(_entity("ent_t", admission_class="workflow_entering"), cache, set(), [])
        plain = pt._task_row(_entity("ent_u"), cache, set(), [])
    assert leased["working"] == {"lease_holder": "ent_h", "runner": "r"}
    assert leased["bucket"] == "active"
    # Same gate as the workboard: no lease read for work that is not workflow-entering.
    assert plain["working"] is None and live.call_count == 1


def test_task_row_buckets_operator_blocked_and_done():
    cache = {}
    with patch.object(pt.S, "_planning_ascent", return_value=([], ["No planning parent is recorded for x"])):
        assert pt._task_row(_entity("ent_a"), cache, {"ent_a"}, [])["bucket"] == "operator-needed"
        assert pt._task_row(_entity("ent_b", status="blocked"), cache, set(), [])["bucket"] == "blocked"
        assert pt._task_row(_entity("ent_c", status="done"), cache, set(), [])["bucket"] == "done"
        assert pt._task_row(_entity("ent_d", status="cancelled"), cache, set(), [])["bucket"] == "terminal-other"


def test_customers_follow_outgoing_refers_to_to_an_organization_only():
    rels = [
        {"relationship_type": "REFERS_TO", "source_entity_id": "ent_p", "target_entity_id": "ent_org"},
        {"relationship_type": "REFERS_TO", "source_entity_id": "ent_p", "target_entity_id": "ent_person"},
        {"relationship_type": "REFERS_TO", "source_entity_id": "ent_other", "target_entity_id": "ent_p"},
    ]
    records = {
        "/entities/ent_org": {"entity_type": "organization", "snapshot": {"name": "Acme"}},
        "/entities/ent_person": {"entity_type": "person", "snapshot": {"name": "Pat"}},
    }
    with patch.object(pt.S, "_workboard_relationships", return_value=(rels, None)), \
         patch.object(pt.S, "_get", side_effect=lambda path, *a, **k: records[path]):
        out = pt._customers(["ent_p"], {})
    assert out == {"ent_p": [{"entity_id": "ent_org", "name": "Acme", "entity_type": "organization"}]}


def test_build_reports_a_failed_open_read_instead_of_an_empty_tree():
    with patch.object(pt.S, "_planning_registry", return_value=({}, False, ["x"])), \
         patch.object(pt.S, "_retrieve_page", return_value=None):
        assert pt.build_planning_tree(["open"]) == {"error": "Open tasks could not be read"}


def test_build_discloses_the_window_and_incomplete_coverage():
    def page(entity_type, snapshot_filters=None, limit=100, **kw):
        if entity_type == "task":
            statuses = snapshot_filters["status"]["value"]
            if "done" in statuses:
                return {"entities": [], "total": 3}
            return {"entities": [_entity("ent_t")], "total": 900}
        return {"entities": [], "total": 500}  # checkpoints exceed the page

    with patch.object(pt.S, "_planning_registry", return_value=({}, False, ["unknown"])), \
         patch.object(pt.S, "_retrieve_page", side_effect=page), \
         patch.object(pt.S, "_workboard_session_rows", return_value=([], [], {"complete": True})), \
         patch.object(pt.S, "_planning_ascent", return_value=([], ["No planning parent is recorded for ent_t"])):
        out = pt.build_planning_tree(["open"], open_limit=1, done_limit=1)
    assert out["ok"] is True
    assert out["coverage"]["open_total"] == 900 and out["coverage"]["open_read"] == 1
    assert out["coverage"]["operator_checkpoints_complete"] is False
    assert out["unplanned"][0]["placement"] == "unplanned"
    assert any("checkpoints exceed" in g for g in out["coverage"]["gaps"])
