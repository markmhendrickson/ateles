"""Effect-level tests for master-plan-first session reporting.

The live eval uses the repository's sandboxed Claude-session driver and fixture
Neotoma.  These offline tests pin the evaluator itself so every assertion can
be shown to fail for the behavior it claims to detect.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import planning_spine_checks as checks  # noqa: E402
import planning_spine_runner as runner  # noqa: E402
import fixture_mcp_server  # noqa: E402


FIXTURE = runner.load_fixture()

MISLEADING_REPORT = """
Planning spine policy note: display the selected master plan first, use each
canonical phase and exit-gate state, honor structural phase binding, label a
cross-phase prerequisite whose phase is not structurally derivable, keep
serial/parallel task execution underneath, and never infer from a task title.
Treat every subordinate workstream label as distinct from a canonical phase.
The task PART_OF ascent through plan, project, and strategy is the source of
truth; report missing ascent and duplicate ascent in the planning resume
ledger, with work captured, workboarded, and dispatched.

## Phases

1. Repair
2. Review
3. Merge
4. Deploy
"""

VALID_REPORT = """
## Selected master plan: Foundation rollout (`ent_fixture_master_plan`)

| Canonical phase | Exit gate |
| --- | --- |
| A | signed_off |
| A2 | signed_off |
| B | signed_off |
| B2 | active |
| B2-prime | pending |
| C | pending |
| D | pending |
| D2 | pending |
| E | pending |
| F | pending |
| G | pending |
| H | pending |
| I | pending |
| J | pending |
| K | pending |
| L | pending |
| M | pending |
| N | pending |
| O | pending |

### Phase B2 workstreams

- `ent_fixture_bound_plan` Rules delivery hardening — structurally bound to B2 through `PART_OF`.

### Cross-phase prerequisites

- `ent_fixture_e2_plan` E2 bootstrap repair — missing phase ancestry;
  cross-phase prerequisite; canonical phase is not structurally derivable.
- `ent_fixture_duplicate_plan` Split-phase validation — duplicate phase
  ancestry (B2, E); cross-phase prerequisite; canonical phase is not uniquely
  structurally derivable.

### Planning resume ledger

| Task | State | Ascent outcome |
| --- | --- | --- |
| `ent_fixture_missing_task` Unplanned audit | queued | missing ascent |
| `ent_fixture_duplicate_task` Split-parent verification | queued | duplicate ascent |

### Task mechanics

- Serial: repair -> review -> merge. Parallel after merge: deploy checks.

`reconcile-planning` remains retrospective repair, not the real-time source of
truth for this report.
"""

SOURCE_SESSION_SCENARIO = {
    "source_session_id": "session_fixture_named_handoff",
    "lane_ids": [f"lane_fixture_{index:02d}" for index in range(1, 14)],
    "plan_ids": [f"plan_fixture_{index:02d}" for index in range(1, 14)],
}

WHOLE_SESSION_REPORT = """
## Source session: Named handoff (`session_fixture_named_handoff`)

The exact source session was bound before any similarly named workstream plan.

### Source-session coverage ledger

| Source lane | Canonical workstream | Disposition |
| --- | --- | --- |
| `lane_fixture_01` discoverable lane | `plan_fixture_01` | Imported |
| `lane_fixture_02` sibling lane | `plan_fixture_02` | Imported |
| `lane_fixture_03` sibling lane | `plan_fixture_03` | Imported |
| `lane_fixture_04` sibling lane | `plan_fixture_04` | Imported |
| `lane_fixture_05` sibling lane | `plan_fixture_05` | Imported |
| `lane_fixture_06` sibling lane | `plan_fixture_06` | Imported |
| `lane_fixture_07` sibling lane | `plan_fixture_07` | Imported |
| `lane_fixture_08` sibling lane | `plan_fixture_08` | Imported |
| `lane_fixture_09` sibling lane | `plan_fixture_09` | Imported |
| `lane_fixture_10` sibling lane | `plan_fixture_10` | Imported |
| `lane_fixture_11` sibling lane | `plan_fixture_11` | Explicitly excluded — terminal and superseded |
| `lane_fixture_12` sibling lane | `plan_fixture_12` | Explicitly excluded — outside requested scope |
| `lane_fixture_13` sibling lane | `plan_fixture_13` | Unresolved — canonical task is ambiguous |

audited: 13; imported: 10; excluded: 2; unresolved: 1

The coverage balance is 13 = 10 + 2 + 1. The unresolved lane prevents a
comprehensive-resume claim and any domain action that assumes its state.
"""


def test_original_task_stage_report_fails_the_effect_check() -> None:
    result = checks.score_report(MISLEADING_REPORT, FIXTURE)

    assert result["outcome"] == "fail"
    assert {
        "master_plan_first",
        "canonical_phases_in_record_order",
        "exit_gates_before_task_mechanics",
        "structural_phase_binding",
        "unbound_work_is_cross_phase",
        "reconcile_is_retrospective",
    } <= set(result["failed"])


def test_named_session_accounts_for_one_lane_and_twelve_siblings() -> None:
    result = checks.score_source_session_resume(
        WHOLE_SESSION_REPORT, SOURCE_SESSION_SCENARIO
    )

    assert result["outcome"] == "pass", result
    assert result["actual_counts"] == {
        "audited": 13,
        "imported": 10,
        "excluded": 2,
        "unresolved": 1,
    }


def test_omitted_sibling_cannot_claim_complete_source_session_resume() -> None:
    omitted_row = (
        "| `lane_fixture_12` sibling lane | `plan_fixture_12` | "
        "Explicitly excluded — outside requested scope |\n"
    )
    wrong = WHOLE_SESSION_REPORT.replace(omitted_row, "").replace(
        "prevents a\ncomprehensive-resume claim",
        "is noted, but all source lanes are accounted for and coverage is complete",
    )

    result = checks.score_source_session_resume(wrong, SOURCE_SESSION_SCENARIO)

    assert result["outcome"] == "fail"
    assert "lane_fixture_12" in result["omitted_lanes"]
    assert {
        "all_source_lanes_accounted_for",
        "all_four_coverage_counts",
        "no_completeness_claim_with_omission",
    } <= set(result["failed"])


def test_plan_first_shortcut_fails_exact_source_session_binding() -> None:
    wrong = (
        WHOLE_SESSION_REPORT.replace(
            "## Source session: Named handoff (`session_fixture_named_handoff`)\n\n",
            "",
        )
        + "\nSource session: `session_fixture_named_handoff`\n"
    )

    result = checks.score_source_session_resume(wrong, SOURCE_SESSION_SCENARIO)

    assert result["outcome"] == "fail"
    assert "exact_source_session_first" in result["failed"]


def test_coverage_counts_must_report_all_dispositions_and_balance() -> None:
    wrong = WHOLE_SESSION_REPORT.replace(
        "audited: 13; imported: 10; excluded: 2; unresolved: 1",
        "audited: 13; imported: 11; unresolved: 1",
    )

    result = checks.score_source_session_resume(wrong, SOURCE_SESSION_SCENARIO)

    assert result["outcome"] == "fail"
    assert {
        "all_four_coverage_counts",
        "coverage_balance_equation",
    } <= set(result["failed"])


@pytest.mark.parametrize("skill", ["continue-session", "digest"])
def test_master_plan_first_report_passes_for_both_invoked_skills(skill: str) -> None:
    result = checks.score_report(VALID_REPORT, FIXTURE, invoked_skill=skill)

    assert result["outcome"] == "pass", result


def test_title_changes_cannot_move_a_structurally_bound_workstream() -> None:
    changed = copy.deepcopy(FIXTURE)
    changed["entities"]["ent_fixture_bound_plan"]["snapshot"]["title"] = (
        "Phase O deployment"
    )
    changed["entities"]["ent_fixture_master_plan"]["snapshot"]["title"] = (
        "Renamed master"
    )
    changed["entities"]["ent_fixture_e2_plan"]["snapshot"]["title"] = (
        "Renamed prerequisite"
    )
    changed["entities"]["ent_fixture_duplicate_plan"]["snapshot"]["title"] = (
        "Renamed split plan"
    )
    title_variant = (
        VALID_REPORT.replace(
            "Rules delivery hardening", "Completely different workstream label"
        )
        .replace("E2 bootstrap repair", "Unlabelled prerequisite")
        .replace("Split-phase validation", "Another split label")
        .replace("Foundation rollout", "Arbitrary master label")
    )

    original = checks.score_report(VALID_REPORT, FIXTURE)
    renamed = checks.score_report(title_variant, changed)

    assert original["structural_placements"] == renamed["structural_placements"]
    assert renamed["outcome"] == "pass", renamed


def test_fixture_derives_unique_missing_and_duplicate_phase_ancestry() -> None:
    outcomes = checks.phase_ancestry_outcomes(FIXTURE)

    assert outcomes["ent_fixture_bound_plan"] == {
        "outcome": "unique",
        "phase_ids": ["ent_fixture_phase_b2"],
        "phases": ["B2"],
    }
    assert outcomes["ent_fixture_e2_plan"] == {
        "outcome": "missing",
        "phase_ids": [],
        "phases": [],
    }
    assert outcomes["ent_fixture_duplicate_plan"] == {
        "outcome": "duplicate",
        "phase_ids": ["ent_fixture_phase_b2", "ent_fixture_phase_e"],
        "phases": ["B2", "E"],
    }


def test_fixture_derives_missing_and_duplicate_task_ascent() -> None:
    assert checks.task_ascent_outcomes(FIXTURE) == {
        "ent_fixture_missing_task": "missing",
        "ent_fixture_duplicate_task": "duplicate",
    }


def test_title_based_phase_placement_goes_red() -> None:
    wrong = VALID_REPORT.replace(
        "### Phase B2 workstreams\n\n- `ent_fixture_bound_plan` Rules delivery hardening",
        "### Phase O workstreams\n\n- `ent_fixture_bound_plan` Rules delivery hardening",
    )

    result = checks.score_report(wrong, FIXTURE)

    assert result["outcome"] == "fail"
    assert "structural_phase_binding" in result["failed"]


@pytest.mark.parametrize("skill", ["continue-session", "digest"])
def test_workstream_reported_under_correct_and_incorrect_phases_goes_red(
    skill: str,
) -> None:
    wrong = VALID_REPORT.replace(
        "### Cross-phase prerequisites",
        "### Phase E workstreams\n\n"
        "- `ent_fixture_bound_plan` Alternate copy — structurally bound through "
        "`PART_OF`.\n\n### Cross-phase prerequisites",
    )

    result = checks.score_report(wrong, FIXTURE, invoked_skill=skill)

    assert result["outcome"] == "fail"
    assert "unique_graph_phase_placement" in result["failed"]


@pytest.mark.parametrize("skill", ["continue-session", "digest"])
@pytest.mark.parametrize(
    ("entity_id", "outcome"),
    [
        ("ent_fixture_missing_task", "missing ascent"),
        ("ent_fixture_duplicate_task", "duplicate ascent"),
    ],
)
def test_each_ascent_defect_requires_its_own_explicit_outcome(
    skill: str, entity_id: str, outcome: str
) -> None:
    wrong = VALID_REPORT.replace(outcome, "ascent unresolved", 1)

    result = checks.score_report(wrong, FIXTURE, invoked_skill=skill)

    assert result["outcome"] == "fail"
    assert "explicit_task_ascent_outcomes" in result["failed"]


@pytest.mark.parametrize("skill", ["continue-session", "digest"])
@pytest.mark.parametrize(
    ("entity_id", "outcome"),
    [
        ("ent_fixture_e2_plan", "missing phase ancestry"),
        ("ent_fixture_duplicate_plan", "duplicate phase"),
    ],
)
def test_each_non_unique_phase_ascent_requires_its_own_explicit_outcome(
    skill: str, entity_id: str, outcome: str
) -> None:
    item_start = VALID_REPORT.index(f"- `{entity_id}`")
    item_end = VALID_REPORT.find("\n- `", item_start + 1)
    if item_end < 0:
        item_end = VALID_REPORT.find("\n\n###", item_start)
    item = VALID_REPORT[item_start:item_end]
    wrong = VALID_REPORT.replace(item, item.replace(outcome, "ancestry unresolved"))

    result = checks.score_report(wrong, FIXTURE, invoked_skill=skill)

    assert result["outcome"] == "fail"
    assert "explicit_phase_ancestry_outcomes" in result["failed"]


def test_master_plan_heading_after_phase_table_goes_red() -> None:
    wrong = VALID_REPORT.replace(
        "## Selected master plan: Foundation rollout (`ent_fixture_master_plan`)\n\n",
        "",
    ).replace(
        "### Phase B2 workstreams",
        "## Selected master plan: Foundation rollout (`ent_fixture_master_plan`)\n\n"
        "### Phase B2 workstreams",
    )

    result = checks.score_report(wrong, FIXTURE)

    assert result["outcome"] == "fail"
    assert "master_plan_first" in result["failed"]


def test_unbound_e2_cannot_be_presented_as_canonical_phase_e() -> None:
    wrong = VALID_REPORT.replace(
        "- `ent_fixture_e2_plan` E2 bootstrap repair — missing phase ancestry;\n"
        "  cross-phase prerequisite; canonical phase is not structurally derivable.",
        "- `ent_fixture_e2_plan` E2 bootstrap repair.",
    )

    result = checks.score_report(wrong, FIXTURE)

    assert result["outcome"] == "fail"
    assert "unbound_work_is_cross_phase" in result["failed"]


def test_review_bundle_is_attributable_public_safe_and_hash_complete() -> None:
    result = runner.verify_review_bundle()

    assert result == []

    manifest = json.loads((HERE / "fixtures" / "review_bundle.json").read_text())
    assert manifest["canonical_source"] == "Neotoma prod skill entities"
    assert (
        manifest["artifact_role"]
        == "generated review evidence; not an authoring source"
    )
    assert {item["slug"] for item in manifest["skills"]} == {
        "continue-session",
        "digest",
        "reconcile-planning",
    }


def test_normal_eval_setup_materializes_skill_mirrors_and_fixture_mcp(
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "continue-session"

    runner.prepare_run(run_dir, "continue-session")

    for slug in ("continue-session", "digest", "reconcile-planning"):
        assert (run_dir / "ws" / ".claude" / "skills" / slug / "SKILL.md").is_file()
    state = json.loads((run_dir / "ws" / "neotoma_state.json").read_text())
    assert state["relationships"] == FIXTURE["relationships"]
    settings = json.loads((run_dir / "settings.json").read_text())
    assert "PreToolUse" in settings["hooks"]


def test_fixture_mcp_exposes_structural_ascent_in_order() -> None:
    result = fixture_mcp_server.related_entities(
        {
            "entities": FIXTURE["entities"],
            "relationships": FIXTURE["relationships"],
        },
        {
            "entity_id": "ent_fixture_bound_task",
            "direction": "outbound",
            "relationship_types": ["PART_OF"],
            "max_hops": 3,
        },
    )

    assert [row["entity_id"] for row in result["entities"]] == sorted(
        [
            "ent_fixture_bound_plan",
            "ent_fixture_phase_b2",
            "ent_fixture_master_plan",
        ]
    )
    assert [edge["target_entity_id"] for edge in result["relationships"]] == [
        "ent_fixture_bound_plan",
        "ent_fixture_phase_b2",
        "ent_fixture_master_plan",
    ]


def test_transport_failure_is_not_scored_as_skill_behavior(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner.RULE_RUNNER,
        "drive_session",
        lambda *args, **kwargs: {
            "turns": [[{"type": "result", "result": "API Error: ENOTFOUND"}]],
            "error": None,
        },
    )

    result = runner.run_scenario(tmp_path / "run", "digest", "fixture-model", 0.0, 1.0)

    assert result["outcome"] == "error"
    assert result["infrastructure_error"] == "API Error: ENOTFOUND"
