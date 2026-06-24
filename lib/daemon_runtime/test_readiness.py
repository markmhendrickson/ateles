"""Tests for the pre-execution readiness gate (E4)."""

from __future__ import annotations

from lib.daemon_runtime import readiness as rd


def test_well_specified_task_is_ready():
    a = rd.assess_readiness(
        {"title": "Email June invoice to the accountant",
         "description": "Send the June services invoice PDF. Done when sent and logged. "
                        "Must not include bank details.",
         "acceptance_criteria": "email sent + logged"},
        has_owner=True, relationship_count=2,
    )
    assert a.ready and a.score >= 0.75 and a.missing == []


def test_bare_task_not_ready_with_missing():
    a = rd.assess_readiness({"title": "fix it"}, has_owner=False, relationship_count=0)
    assert not a.ready
    assert "tooling_identified" in a.missing
    assert "acceptance_criteria" in a.missing


def test_missing_goal_is_floored():
    a = rd.assess_readiness({"description": ""}, has_owner=True, relationship_count=9)
    assert a.score <= 0.3 and not a.ready


def test_missing_criteria_capped_to_mid():
    a = rd.assess_readiness(
        {"title": "Refactor the dispatcher", "description": "Clean up routing. " * 6},
        has_owner=True, relationship_count=3,
    )
    assert a.score <= 0.5 and not a.ready


def test_threshold_override():
    snap = {"title": "Refactor the dispatcher", "description": "Clean up routing. " * 6,
            "acceptance_criteria": "tests pass"}
    strict = rd.assess_readiness(snap, has_owner=True, relationship_count=2, threshold=0.95)
    loose = rd.assess_readiness(snap, has_owner=True, relationship_count=2, threshold=0.5)
    assert not strict.ready and loose.ready


def test_missing_request_names_gaps():
    a = rd.assess_readiness({"title": "fix it"}, has_owner=False, relationship_count=0)
    req = rd.missing_request(a, "fix it")
    assert "fix it" in req and "•" in req and "re-assess" in req


def test_assessment_entity_links_task():
    a = rd.assess_readiness({"title": "x", "description": "y" * 50}, has_owner=True)
    body = rd.build_assessment_entity("ent_task", a)
    assert body["entities"][0]["entity_type"] == "task_readiness_assessment"
    rel = body["relationships"][0]
    assert rel["relationship_type"] == "REFERS_TO" and rel["target_entity_id"] == "ent_task"


def test_write_assessment_fail_open(monkeypatch):
    monkeypatch.setattr(rd, "NEOTOMA_BEARER_TOKEN", "")
    a = rd.assess_readiness({"title": "x"}, has_owner=True)
    assert rd.write_assessment("ent_task", a) is None
