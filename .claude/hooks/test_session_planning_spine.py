"""Red/green contract tests for the interactive-session planning spine.

The runtime remains in shadow-audit mode until the planning workflow from
ateles#965 exists.  These tests nevertheless pin the cutover contract now:
tasks carry one ascent, sessions do not write derived plan progress, and a
second workstream is either queued or admitted only after capture, workboard
refresh, and dispatch of the current stream.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _session_integrity import scan_transcript  # noqa: E402


PLAN_A = "ent_aaaaaaaaaaaaaaaaaaaaaaaa"
PLAN_B = "ent_bbbbbbbbbbbbbbbbbbbbbbbb"


def _tool(name: str, payload: dict) -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "tool_use", "name": name, "input": payload}],
    }


def _task(domain: str, *, status: str = "in_progress", parents: tuple[str, ...] = (PLAN_A,)) -> dict:
    return _tool(
        "mcp__mcpsrv_neotoma__store",
        {
            "entities": [{
                "entity_type": "task",
                "title": f"Deliver {domain}",
                "domain": domain,
                "status": status,
            }],
            "relationships": [
                {"relationship_type": "PART_OF", "target_entity_id": parent}
                for parent in parents
            ],
        },
    )


def _scan(tmp_path: Path, *events: dict) -> dict:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("".join(json.dumps(event) + "\n" for event in events))
    return scan_transcript(str(transcript))


def _codes(summary: dict) -> list[str]:
    return [finding["code"] for finding in summary["planning_spine_findings"]]


def test_task_without_part_of_ascent_is_red(tmp_path: Path) -> None:
    summary = _scan(tmp_path, _task("alpha", parents=()))

    assert "task_missing_ascent" in _codes(summary)
    assert summary["planning_spine_status"] == "violated"


def test_task_with_second_part_of_is_red(tmp_path: Path) -> None:
    summary = _scan(tmp_path, _task("alpha", parents=(PLAN_A, PLAN_B)))

    assert "task_multiple_ascents" in _codes(summary)
    assert summary["planning_spine_status"] == "violated"


def test_session_authored_plan_progress_is_red(tmp_path: Path) -> None:
    summary = _scan(
        tmp_path,
        _task("alpha"),
        _tool(
            "mcp__mcpsrv_neotoma__correct",
            {
                "entity_type": "plan",
                "entity_id": PLAN_A,
                "field": "next_steps",
                "value": ["keep going"],
            },
        ),
    )

    assert "session_authored_planning_progress" in _codes(summary)
    assert summary["planning_spine_status"] == "violated"


def test_new_workstream_remains_queued_before_checkpoint(tmp_path: Path) -> None:
    summary = _scan(
        tmp_path,
        _task("alpha", parents=(PLAN_A,)),
        _task("beta", status="queued", parents=(PLAN_B,)),
    )

    assert "workstream_admitted_before_checkpoint" not in _codes(summary)
    assert summary["queued_workstreams"] == ["beta"]
    assert summary["planning_spine_status"] == "ready"


def test_new_workstream_started_before_checkpoint_is_red(tmp_path: Path) -> None:
    summary = _scan(
        tmp_path,
        _task("alpha", parents=(PLAN_A,)),
        _task("beta", status="in_progress", parents=(PLAN_B,)),
    )

    assert "workstream_admitted_before_checkpoint" in _codes(summary)
    assert summary["planning_spine_status"] == "violated"


def test_new_workstream_can_start_after_workboard_and_dispatch(tmp_path: Path) -> None:
    summary = _scan(
        tmp_path,
        _task("alpha", parents=(PLAN_A,)),
        _tool(
            "mcp__mcpsrv_neotoma__store",
            {"entities": [{"entity_type": "session_digest", "summary": "workboard refreshed"}]},
        ),
        _tool("mcp__ateles__route_task", {"task_id": "ent_current"}),
        _task("beta", status="in_progress", parents=(PLAN_B,)),
    )

    assert "workstream_admitted_before_checkpoint" not in _codes(summary)
    assert summary["planning_spine_status"] == "ready"


def test_harness_task_chip_is_not_dispatch(tmp_path: Path) -> None:
    summary = _scan(
        tmp_path,
        _task("alpha", parents=(PLAN_A,)),
        _tool(
            "mcp__mcpsrv_neotoma__store",
            {"entities": [{"entity_type": "session_digest", "summary": "workboard refreshed"}]},
        ),
        _tool("mcp__ccd_session__spawn_task", {"title": "A suggestion"}),
        _task("beta", status="in_progress", parents=(PLAN_B,)),
    )

    assert "workstream_admitted_before_checkpoint" in _codes(summary)
    finding = next(
        item for item in summary["planning_spine_findings"]
        if item["code"] == "workstream_admitted_before_checkpoint"
    )
    assert finding["missing"] == ["dispatch"]


def test_multi_task_store_does_not_guess_relationship_assignment(tmp_path: Path) -> None:
    payload = {
        "entities": [
            {"entity_type": "task", "title": "A", "domain": "alpha", "status": "queued"},
            {"entity_type": "task", "title": "B", "domain": "beta", "status": "queued"},
        ],
        "relationships": [{"relationship_type": "PART_OF", "target_entity_id": PLAN_A}],
    }

    summary = _scan(tmp_path, _tool("mcp__mcpsrv_neotoma__store", payload))

    assert _codes(summary).count("task_ascent_unobservable") == 2
    assert summary["planning_spine_status"] == "violated"
