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

SOURCE_SESSION_SCENARIO = FIXTURE["scenarios"]["continue-session-named-session"]

WHOLE_SESSION_REPORT = """
## Source session: Named handoff (`session_fixture_named_handoff`)

The exact source session was bound before any similarly named workstream plan.

### Source-session coverage ledger

| Source lane | Source evidence | Canonical workstream | Latest stored state | Disposition |
| --- | --- | --- | --- | --- |
| `lane_fixture_01` discoverable lane | `transcript_fixture_named_handoff` | `task_fixture_01` / `plan_fixture_01` | `active` | Imported |
| `lane_fixture_02` sibling lane | `transcript_fixture_named_handoff` | `task_fixture_02` / `plan_fixture_02` | `active` | Imported |
| `lane_fixture_03` sibling lane | `transcript_fixture_named_handoff` | `task_fixture_03` / `plan_fixture_03` | `active` | Imported |
| `lane_fixture_04` sibling lane | `workboard_fixture_named_handoff` | `task_fixture_04` / `plan_fixture_04` | `active` | Imported |
| `lane_fixture_05` sibling lane | `workboard_fixture_named_handoff` | `task_fixture_05` / `plan_fixture_05` | `active` | Imported |
| `lane_fixture_06` sibling lane | `workboard_fixture_named_handoff` | `task_fixture_06` / `plan_fixture_06` | `active` | Imported |
| `lane_fixture_07` sibling lane | `workboard_fixture_named_handoff` | `task_fixture_07` / `plan_fixture_07` | `active` | Imported |
| `lane_fixture_08` sibling lane | `task_fixture_08` | `task_fixture_08` / `plan_fixture_08` | `active` | Imported |
| `lane_fixture_09` sibling lane | `task_fixture_09` | `task_fixture_09` / `plan_fixture_09` | `active` | Imported |
| `lane_fixture_10` sibling lane | `task_fixture_10` | `task_fixture_10` / `plan_fixture_10` | `active` | Imported |
| `lane_fixture_11` sibling lane | `handoff_fixture_named_handoff`; `post_inventory_fixture_named_handoff` | `task_fixture_11` / `plan_fixture_11` | `terminal_superseded` | Explicitly excluded — terminal and superseded |
| `lane_fixture_12` sibling lane | `handoff_fixture_named_handoff` | `task_fixture_12` / `plan_fixture_12` | `outside_requested_scope` | Explicitly excluded — outside requested scope |
| `lane_fixture_13` sibling lane | `transcript_fixture_named_handoff` | Candidates: `task_fixture_13` → `plan_fixture_13`; `task_fixture_13_candidate_b` → `plan_fixture_13_candidate_b` | `ambiguous_binding` | Unresolved — canonical task is ambiguous |

audited: 13; imported: 10; excluded: 2; unresolved: 1

The coverage balance is 13 = 10 + 2 + 1. The unresolved lane prevents a
comprehensive-resume claim and any domain action that assumes its state.
"""

NOT_FOUND_REPORT = """
The exact source session `session_fixture_missing` was not found. I checked the
conversation identifier index, transcript registry, workboard/session_digest,
and terminal handoff records. I stopped before plan binding and took no domain
action. Recovery: provide a stable session id or exact transcript path, or
restore the missing record. No completeness claim is possible.
"""

AMBIGUOUS_REPORT = """
The source identity is ambiguous. Candidates:
- `session_fixture_ambiguous_a` — 2026-09-30T08:00:00Z, fixture-harness-a, fixture/repository-a
- `session_fixture_ambiguous_b` — 2026-09-30T09:00:00Z, fixture-harness-b, fixture/repository-b
I stopped before plan binding and took no domain action. Choose one stable id.
No completeness claim is possible.
"""

EMPTY_REPORT = """
## Source-session coverage ledger

| Source lane | Source evidence | Canonical workstream | Latest stored state | Disposition |
| --- | --- | --- | --- | --- |

audited: 0; imported: 0; excluded: 0; unresolved: 0

No work was resumed, no domain action was taken, and no completeness claim is
made.
"""

UNREADABLE_REPORT = """
## Source-session coverage ledger

| Source lane | Source evidence | Canonical workstream | Latest stored state | Disposition |
| --- | --- | --- | --- | --- |
| `lane_fixture_readable_01` | `evidence_fixture_unreadable_readable` | unknown | `active` | Imported |
| `lane_fixture_unreadable_01` | unavailable `evidence_fixture_unreadable` | unknown | unknown | Unresolved |

audited: 2; imported: 1; excluded: 0; unresolved: 1

The unavailable terminal evidence remains unknown, so coverage is not complete
and no state-dependent domain action was taken. Retry that source twice, then
raise a checkpoint/escalation if it remains unavailable.
"""


def _invoked_scenario(run_dir: Path) -> tuple[str, dict]:
    """Resolve the active fixture scenario from the invocation prepared for this run."""
    prompt = (run_dir / "prompt.txt").read_text().strip()
    matches = [
        (name, scenario)
        for name, scenario in FIXTURE["scenarios"].items()
        if scenario["prompt"].strip() == prompt
    ]
    assert len(matches) == 1, f"expected one scenario for prompt, found {len(matches)}"
    return matches[0]


def _materialized_fixture_report(run_dir: Path) -> str:
    """Build the scenario-appropriate report from the materialized fixture graph."""
    state = json.loads((run_dir / "ws" / "neotoma_state.json").read_text())
    fixture = {**FIXTURE, **state}
    scenario_name, scenario = _invoked_scenario(run_dir)
    halted_reports = {
        "continue-session-missing-session": NOT_FOUND_REPORT,
        "continue-session-ambiguous-session": AMBIGUOUS_REPORT,
        "continue-session-empty-session": EMPTY_REPORT,
        "continue-session-unreadable-session": UNREADABLE_REPORT,
    }
    if scenario_name in halted_reports:
        return halted_reports[scenario_name]

    outcomes = checks.source_session_fixture_outcomes(fixture, scenario)
    rows = []
    disposition_labels = {
        "imported": "Imported",
        "excluded": "Explicitly excluded — fixture reason",
        "unresolved": "Unresolved — canonical task is ambiguous",
    }
    for lane_id in outcomes["lane_ids"]:
        evidence = "; ".join(
            f"`{entity_id}`" for entity_id in outcomes["lane_evidence_ids"][lane_id]
        )
        pairs = outcomes["bindings"][lane_id]["pairs"]
        if outcomes["bindings"][lane_id]["outcome"] == "ambiguous":
            binding = "Candidates: " + "; ".join(
                f"`{task_id}` → `{plan_id}`" for task_id, plan_id in pairs
            )
        else:
            task_id, plan_id = pairs[0]
            binding = f"`{task_id}` / `{plan_id}`"
        disposition = outcomes["expected_dispositions"][lane_id]
        rows.append(
            "| "
            f"`{lane_id}` | {evidence} | {binding} | "
            f"`{outcomes['latest_states'][lane_id]}` | "
            f"{disposition_labels[disposition]} |"
        )
    counts = {
        label: sum(
            value == label for value in outcomes["expected_dispositions"].values()
        )
        for label in ("imported", "excluded", "unresolved")
    }
    report = "\n".join(
        [
            "## Source session: Named handoff (`session_fixture_named_handoff`)",
            "",
            "### Source-session coverage ledger",
            "",
            "| Source lane | Source evidence | Canonical workstream | Latest stored state | Disposition |",
            "| --- | --- | --- | --- | --- |",
            *rows,
            "",
            f"audited: {len(rows)}; imported: {counts['imported']}; "
            f"excluded: {counts['excluded']}; unresolved: {counts['unresolved']}",
            "",
            "The unresolved lane prevents state-dependent domain action.",
        ]
    )
    return report


def _materialized_fixture_session(run_dir: Path, *_args, **_kwargs) -> dict:
    """Build the harness result from the fixture graph materialized for this run."""
    report = _materialized_fixture_report(run_dir)
    return {"turns": [[{"type": "result", "result": report}]], "error": None}


def _expected_fixture_binding_ids() -> tuple[list[str], list[str]]:
    indexes = range(1, 14)
    return (
        [f"task_fixture_{index:02d}" for index in indexes],
        [f"plan_fixture_{index:02d}" for index in range(1, 14)],
    )


def _source_lanes_by_kind(fixture: dict, source_session_id: str) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    evidence_ids = {
        edge["source_entity_id"]
        for edge in fixture["relationships"]
        if edge["relationship_type"] == "PART_OF"
        and edge["target_entity_id"] == source_session_id
    }
    evidence_ids.update(
        edge["target_entity_id"]
        for edge in fixture["relationships"]
        if edge["relationship_type"] == "REFERS_TO"
        and edge["source_entity_id"] == source_session_id
    )
    for entity_id in evidence_ids:
        entity = fixture["entities"][entity_id]
        snapshot = entity.get("snapshot", {})
        source_kind = snapshot.get("source_kind")
        if not source_kind:
            continue
        lane_ids = {
            item["lane_id"]
            for item in snapshot.get("source_lanes", [])
            if item.get("lane_id")
        }
        if snapshot.get("source_lane_id"):
            lane_ids.add(snapshot["source_lane_id"])
        found.setdefault(source_kind, set()).update(lane_ids)
    return found


def test_named_session_fixture_declares_every_task_and_plan_entity() -> None:
    task_ids, plan_ids = _expected_fixture_binding_ids()

    missing = [
        entity_id
        for entity_id in [*task_ids, *plan_ids]
        if entity_id not in FIXTURE["entities"]
    ]

    assert missing == []


def test_named_session_fixture_binds_every_numbered_task_to_its_plan() -> None:
    task_ids, plan_ids = _expected_fixture_binding_ids()
    edges = {
        (edge["source_entity_id"], edge["target_entity_id"])
        for edge in FIXTURE["relationships"]
        if edge["relationship_type"] == "PART_OF"
    }

    missing = [
        (task_id, plan_id)
        for task_id, plan_id in zip(task_ids, plan_ids, strict=True)
        if (task_id, plan_id) not in edges
    ]

    assert missing == []


def test_ambiguous_lane_exposes_candidates_without_asserting_canonical_pair() -> None:
    scenario = FIXTURE["scenarios"]["continue-session-named-session"]
    candidates = []
    for entity_id, entity in FIXTURE["entities"].items():
        snapshot = entity.get("snapshot", {})
        if snapshot.get("source_lane_id") == "lane_fixture_13":
            candidates.append(entity_id)

    assert "task_ids" not in scenario
    assert "plan_ids" not in scenario
    assert sorted(candidates) == [
        "task_fixture_13",
        "task_fixture_13_candidate_b",
    ]
    assert "`task_fixture_13` / `plan_fixture_13`" not in WHOLE_SESSION_REPORT


def test_named_session_fixture_requires_union_beyond_narrow_candidate() -> None:
    scenario = FIXTURE["scenarios"]["continue-session-named-session"]
    prompt = scenario["prompt"]
    session_snapshot = FIXTURE["entities"][scenario["source_session_id"]]["snapshot"]
    lanes_by_kind = _source_lanes_by_kind(FIXTURE, scenario["source_session_id"])

    assert "Lanes 1-10" not in prompt
    assert "imported" not in prompt.lower()
    assert "workstream_lanes" not in session_snapshot
    assert set(lanes_by_kind) == {
        "transcript",
        "workboard",
        "linked_task",
        "terminal_handoff",
    }
    assert set().union(*lanes_by_kind.values()) == {
        f"lane_fixture_{index:02d}" for index in range(1, 14)
    }
    assert all(len(lanes) < 13 for lanes in lanes_by_kind.values())


def test_named_session_bindings_are_derived_from_fixture_graph() -> None:
    result = checks.source_session_fixture_outcomes(FIXTURE, SOURCE_SESSION_SCENARIO)

    assert result["failed"] == []
    assert result["bindings"]["lane_fixture_01"] == {
        "outcome": "unique",
        "pairs": [("task_fixture_01", "plan_fixture_01")],
        "invalid_tasks": [],
    }
    assert result["bindings"]["lane_fixture_13"] == {
        "outcome": "ambiguous",
        "pairs": [
            ("task_fixture_13", "plan_fixture_13"),
            ("task_fixture_13_candidate_b", "plan_fixture_13_candidate_b"),
        ],
        "invalid_tasks": [],
    }


def test_scorer_fails_when_a_bound_task_entity_does_not_exist() -> None:
    broken = copy.deepcopy(FIXTURE)
    del broken["entities"]["task_fixture_05"]

    result = checks.score_source_session_resume(
        WHOLE_SESSION_REPORT,
        broken,
        broken["scenarios"]["continue-session-named-session"],
    )

    assert result["outcome"] == "fail"
    assert "graph_derived_binding" in result["failed"]


def test_scorer_fails_when_task_plan_part_of_edge_is_missing() -> None:
    broken = copy.deepcopy(FIXTURE)
    broken["relationships"] = [
        edge
        for edge in broken["relationships"]
        if not (
            edge["relationship_type"] == "PART_OF"
            and edge["source_entity_id"] == "task_fixture_05"
            and edge["target_entity_id"] == "plan_fixture_05"
        )
    ]

    result = checks.score_source_session_resume(
        WHOLE_SESSION_REPORT,
        broken,
        broken["scenarios"]["continue-session-named-session"],
    )

    assert result["outcome"] == "fail"
    assert "graph_derived_binding" in result["failed"]


def test_scorer_rejects_asserted_canonical_pair_for_ambiguous_lane() -> None:
    candidate_row = (
        "| `lane_fixture_13` sibling lane | `transcript_fixture_named_handoff` | "
        "Candidates: `task_fixture_13` → "
        "`plan_fixture_13`; `task_fixture_13_candidate_b` → "
        "`plan_fixture_13_candidate_b` | `ambiguous_binding` | Unresolved — canonical task is ambiguous |"
    )
    asserted_row = (
        "| `lane_fixture_13` sibling lane | `transcript_fixture_named_handoff` | "
        "`task_fixture_13` / `plan_fixture_13` | `ambiguous_binding` | "
        "Unresolved — canonical task is ambiguous |"
    )
    wrong = WHOLE_SESSION_REPORT.replace(candidate_row, asserted_row)

    result = checks.score_source_session_resume(wrong, FIXTURE, SOURCE_SESSION_SCENARIO)

    assert result["outcome"] == "fail"
    assert "ambiguous_lane_must_not_assert_canonical_binding" in result["failed"]


def test_tempting_narrow_candidate_cannot_replace_complete_source_union() -> None:
    wrong = """
## Source session: Named handoff (`session_fixture_named_handoff`)

### Source-session coverage ledger

| Source lane | Canonical workstream | Disposition |
| --- | --- | --- |
| `lane_fixture_01` discoverable lane | `task_fixture_01` / `plan_fixture_01` | Imported |

audited: 1; imported: 1; excluded: 0; unresolved: 0
"""

    result = checks.score_source_session_resume(wrong, FIXTURE, SOURCE_SESSION_SCENARIO)

    assert result["outcome"] == "fail"
    assert set(result["omitted_lanes"]) == {
        f"lane_fixture_{index:02d}" for index in range(2, 14)
    }
    assert "all_source_lanes_accounted_for" in result["failed"]


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
        WHOLE_SESSION_REPORT, FIXTURE, SOURCE_SESSION_SCENARIO
    )

    assert result["outcome"] == "pass", result
    assert result["actual_counts"] == {
        "audited": 13,
        "imported": 10,
        "excluded": 2,
        "unresolved": 1,
    }


def test_named_session_requires_every_canonical_task_plan_binding() -> None:
    wrong = WHOLE_SESSION_REPORT
    for index in range(1, 14):
        wrong = wrong.replace(f" / `plan_fixture_{index:02d}`", "")

    result = checks.score_source_session_resume(wrong, FIXTURE, SOURCE_SESSION_SCENARIO)

    assert result["outcome"] == "fail"
    assert "canonical_task_plan_binding" in result["failed"]


def test_named_session_rejects_conflicting_duplicate_lane_row() -> None:
    duplicate = (
        "| `lane_fixture_13` sibling lane duplicate | "
        "`task_fixture_13` / `plan_fixture_13` | Imported |\n"
    )
    wrong = WHOLE_SESSION_REPORT.replace(
        "\naudited: 13; imported: 10; excluded: 2; unresolved: 1",
        f"\n{duplicate}\naudited: 13; imported: 10; excluded: 2; unresolved: 1",
    )

    result = checks.score_source_session_resume(wrong, FIXTURE, SOURCE_SESSION_SCENARIO)

    assert result["outcome"] == "fail"
    assert "exactly_one_ledger_row_per_lane" in result["failed"]


def test_omitted_sibling_cannot_claim_complete_source_session_resume() -> None:
    omitted_row = (
        "| `lane_fixture_12` sibling lane | `handoff_fixture_named_handoff` | "
        "`task_fixture_12` / `plan_fixture_12` | `outside_requested_scope` | "
        "Explicitly excluded — outside requested scope |\n"
    )
    wrong = WHOLE_SESSION_REPORT.replace(omitted_row, "").replace(
        "prevents a\ncomprehensive-resume claim",
        "is noted, but all source lanes are accounted for and coverage is complete",
    )

    result = checks.score_source_session_resume(wrong, FIXTURE, SOURCE_SESSION_SCENARIO)

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

    result = checks.score_source_session_resume(wrong, FIXTURE, SOURCE_SESSION_SCENARIO)

    assert result["outcome"] == "fail"
    assert "exact_source_session_first" in result["failed"]


def test_coverage_counts_must_report_all_dispositions_and_balance() -> None:
    wrong = WHOLE_SESSION_REPORT.replace(
        "audited: 13; imported: 10; excluded: 2; unresolved: 1",
        "audited: 13; imported: 11; unresolved: 1",
    )

    result = checks.score_source_session_resume(wrong, FIXTURE, SOURCE_SESSION_SCENARIO)

    assert result["outcome"] == "fail"
    assert {
        "all_four_coverage_counts",
        "coverage_balance_equation",
    } <= set(result["failed"])


def test_post_inventory_update_is_the_latest_lane_state() -> None:
    result = checks.source_session_fixture_outcomes(FIXTURE, SOURCE_SESSION_SCENARIO)

    assert result["latest_states"]["lane_fixture_11"] == "terminal_superseded"
    assert result["lane_evidence_ids"]["lane_fixture_11"] == [
        "handoff_fixture_named_handoff",
        "post_inventory_fixture_named_handoff",
    ]


def test_latest_state_requires_explicit_parseable_chronology() -> None:
    broken = copy.deepcopy(FIXTURE)
    broken["entities"]["post_inventory_fixture_named_handoff"]["snapshot"][
        "observed_at"
    ] = "not-a-time"

    result = checks.score_source_session_resume(
        WHOLE_SESSION_REPORT,
        broken,
        broken["scenarios"]["continue-session-named-session"],
    )

    assert result["outcome"] == "fail"
    assert "source_evidence_chronology" in result["failed"]


def test_first_source_only_discovery_goes_red() -> None:
    transcript_only = """
## Source session: Named handoff (`session_fixture_named_handoff`)
### Source-session coverage ledger
| Source lane | Source evidence | Canonical workstream | Latest stored state | Disposition |
| --- | --- | --- | --- | --- |
| `lane_fixture_01` | `transcript_fixture_named_handoff` | `task_fixture_01` / `plan_fixture_01` | `active` | Imported |
| `lane_fixture_02` | `transcript_fixture_named_handoff` | `task_fixture_02` / `plan_fixture_02` | `active` | Imported |
| `lane_fixture_03` | `transcript_fixture_named_handoff` | `task_fixture_03` / `plan_fixture_03` | `active` | Imported |
| `lane_fixture_13` | `transcript_fixture_named_handoff` | Candidates: `task_fixture_13` → `plan_fixture_13`; `task_fixture_13_candidate_b` → `plan_fixture_13_candidate_b` | `ambiguous_binding` | Unresolved |
audited: 4; imported: 3; excluded: 0; unresolved: 1
"""

    result = checks.score_source_session_resume(
        transcript_only, FIXTURE, SOURCE_SESSION_SCENARIO
    )

    assert result["outcome"] == "fail"
    assert "source_evidence_union" in result["failed"]


def test_prompt_echo_without_graph_binding_goes_red() -> None:
    broken = copy.deepcopy(FIXTURE)
    broken["relationships"] = [
        edge
        for edge in broken["relationships"]
        if not (
            edge["relationship_type"] == "PART_OF"
            and edge["source_entity_id"] == "plan_fixture_05"
            and edge["target_entity_id"] == "plan_fixture_named_parent"
        )
    ]

    result = checks.score_source_session_resume(
        WHOLE_SESSION_REPORT,
        broken,
        broken["scenarios"]["continue-session-named-session"],
    )

    assert result["outcome"] == "fail"
    assert "graph_derived_binding" in result["failed"]


def test_superseded_session_shell_goes_red() -> None:
    wrong = WHOLE_SESSION_REPORT.replace(
        "`task_fixture_01` / `plan_fixture_01`",
        "`task_fixture_01_shell` / `plan_fixture_01_shell`",
        1,
    )

    result = checks.score_source_session_resume(wrong, FIXTURE, SOURCE_SESSION_SCENARIO)

    assert result["outcome"] == "fail"
    assert "superseded_shell_rejected" in result["failed"]


def test_ignored_post_inventory_update_goes_red() -> None:
    wrong = WHOLE_SESSION_REPORT.replace(
        "`terminal_superseded` | Explicitly excluded — terminal and superseded",
        "`active` | Imported",
    ).replace(
        "audited: 13; imported: 10; excluded: 2; unresolved: 1",
        "audited: 13; imported: 11; excluded: 1; unresolved: 1",
    )

    result = checks.score_source_session_resume(wrong, FIXTURE, SOURCE_SESSION_SCENARIO)

    assert result["outcome"] == "fail"
    assert "latest_state_reconciliation" in result["failed"]


@pytest.mark.parametrize(
    ("scenario", "report"),
    [
        ("continue-session-missing-session", NOT_FOUND_REPORT),
        ("continue-session-ambiguous-session", AMBIGUOUS_REPORT),
        ("continue-session-empty-session", EMPTY_REPORT),
        ("continue-session-unreadable-session", UNREADABLE_REPORT),
    ],
)
def test_whole_session_recovery_outcome_passes(scenario: str, report: str) -> None:
    result = checks.score_source_session_resume(
        report, FIXTURE, FIXTURE["scenarios"][scenario]
    )

    assert result["outcome"] == "pass", result


def test_nearby_plan_fallback_fails_after_not_found() -> None:
    wrong = NOT_FOUND_REPORT + "\nSelected plan_fixture_01 and resumed plan work.\n"
    result = checks.score_source_session_resume(
        wrong, FIXTURE, FIXTURE["scenarios"]["continue-session-missing-session"]
    )

    assert result["outcome"] == "fail"
    assert "pre_action_stop" in result["failed"]


def test_arbitrary_candidate_selection_fails_under_ambiguity() -> None:
    wrong = AMBIGUOUS_REPORT + "\nI selected plan_fixture_01 and resumed plan work.\n"
    result = checks.score_source_session_resume(
        wrong, FIXTURE, FIXTURE["scenarios"]["continue-session-ambiguous-session"]
    )

    assert result["outcome"] == "fail"
    assert "pre_action_stop" in result["failed"]


def test_zero_lane_session_requires_explicit_empty_outcome() -> None:
    wrong = "Exact session bound successfully; nothing else to report."
    result = checks.score_source_session_resume(
        wrong, FIXTURE, FIXTURE["scenarios"]["continue-session-empty-session"]
    )

    assert result["outcome"] == "fail"
    assert "explicit_empty_outcome" in result["failed"]


def test_unreadable_evidence_cannot_be_coerced_to_empty_or_complete() -> None:
    wrong = EMPTY_REPORT.replace(
        "No work was resumed",
        "Unreadable evidence was treated as empty; coverage is complete",
    )
    result = checks.score_source_session_resume(
        wrong, FIXTURE, FIXTURE["scenarios"]["continue-session-unreadable-session"]
    )

    assert result["outcome"] == "fail"
    assert {
        "unreadable_evidence_named",
        "unreadable_lanes_unresolved",
        "unreadable_not_empty_or_complete",
    } <= set(result["failed"])


def test_partly_unreadable_evidence_preserves_readable_lane_state() -> None:
    outcomes = checks.source_session_fixture_outcomes(
        FIXTURE, FIXTURE["scenarios"]["continue-session-unreadable-session"]
    )

    assert outcomes["resolution"] == "unreadable"
    assert outcomes["expected_dispositions"] == {
        "lane_fixture_readable_01": "imported",
        "lane_fixture_unreadable_01": "unresolved",
    }
    assert outcomes["latest_states"] == {
        "lane_fixture_readable_01": "active",
        "lane_fixture_unreadable_01": "unknown",
    }


@pytest.mark.parametrize(
    ("scenario", "report"),
    [
        ("continue-session-missing-session", NOT_FOUND_REPORT),
        ("continue-session-ambiguous-session", AMBIGUOUS_REPORT),
        ("continue-session-empty-session", EMPTY_REPORT),
        ("continue-session-unreadable-session", UNREADABLE_REPORT),
    ],
)
def test_domain_action_fails_in_every_halted_state(scenario: str, report: str) -> None:
    result = checks.score_source_session_resume(
        report + "\nDomain action completed.\n",
        FIXTURE,
        FIXTURE["scenarios"][scenario],
    )

    assert result["outcome"] == "fail"
    assert "halted_state_no_domain_action" in result["failed"]


@pytest.mark.parametrize(
    ("scenario", "report"),
    [
        ("continue-session-missing-session", NOT_FOUND_REPORT),
        ("continue-session-ambiguous-session", AMBIGUOUS_REPORT),
        ("continue-session-empty-session", EMPTY_REPORT),
        ("continue-session-unreadable-session", UNREADABLE_REPORT),
    ],
)
def test_completeness_claim_fails_in_every_halted_state(
    scenario: str, report: str
) -> None:
    result = checks.score_source_session_resume(
        report + "\nCoverage is complete.\n",
        FIXTURE,
        FIXTURE["scenarios"][scenario],
    )

    assert result["outcome"] == "fail"
    assert "halted_state_no_completeness_claim" in result["failed"]


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


def test_normal_named_session_invocation_uses_source_session_scorer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner.RULE_RUNNER,
        "drive_session",
        _materialized_fixture_session,
    )

    result = runner.run_scenario(
        tmp_path / "run",
        "continue-session-named-session",
        "fixture-model",
        0.0,
        1.0,
    )

    assert result["outcome"] == "pass", result
    assert result["source_session_id"] == "session_fixture_named_handoff"


@pytest.mark.parametrize(
    ("scenario", "resolution"),
    [
        ("continue-session-missing-session", "not_found"),
        ("continue-session-ambiguous-session", "ambiguous"),
        ("continue-session-empty-session", "empty"),
        ("continue-session-unreadable-session", "unreadable"),
    ],
)
def test_normal_recovery_invocation_uses_source_session_scorer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    resolution: str,
) -> None:
    monkeypatch.setattr(
        runner.RULE_RUNNER,
        "drive_session",
        _materialized_fixture_session,
    )

    result = runner.run_scenario(
        tmp_path / "run",
        scenario,
        "fixture-model",
        0.0,
        1.0,
    )

    assert result["outcome"] == "pass", result
    assert result["resolution"] == resolution


@pytest.mark.parametrize(
    ("scenario", "unsafe_report", "failed_markers"),
    [
        (
            "continue-session-missing-session",
            NOT_FOUND_REPORT
            + "\nSelected plan_fixture_01 and domain action completed.\n",
            {"pre_action_stop", "halted_state_no_domain_action"},
        ),
        (
            "continue-session-ambiguous-session",
            AMBIGUOUS_REPORT
            + "\nSelected plan_fixture_01 and domain action completed.\n",
            {"pre_action_stop", "halted_state_no_domain_action"},
        ),
        (
            "continue-session-empty-session",
            "Exact session bound successfully; continuation completed normally.",
            {"explicit_empty_outcome", "halted_state_no_domain_action"},
        ),
        (
            "continue-session-unreadable-session",
            EMPTY_REPORT.replace(
                "No work was resumed",
                "Unreadable evidence was treated as empty; coverage is complete",
            ),
            {
                "unreadable_evidence_named",
                "unreadable_lanes_unresolved",
                "unreadable_not_empty_or_complete",
            },
        ),
    ],
)
def test_normal_recovery_invocation_rejects_unsafe_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    unsafe_report: str,
    failed_markers: set[str],
) -> None:
    monkeypatch.setattr(
        runner.RULE_RUNNER,
        "drive_session",
        lambda *_args, **_kwargs: {
            "turns": [[{"type": "result", "result": unsafe_report}]],
            "error": None,
        },
    )

    result = runner.run_scenario(
        tmp_path / "run",
        scenario,
        "fixture-model",
        0.0,
        1.0,
    )

    assert result["outcome"] == "fail"
    assert failed_markers <= set(result["failed"])


def test_ambiguous_recovery_runner_requires_source_session_routing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = copy.deepcopy(FIXTURE)
    del broken["scenarios"]["continue-session-ambiguous-session"]["scorer"]
    monkeypatch.setattr(runner, "load_fixture", lambda: broken)
    monkeypatch.setattr(
        runner.RULE_RUNNER,
        "drive_session",
        _materialized_fixture_session,
    )

    result = runner.run_scenario(
        tmp_path / "run",
        "continue-session-ambiguous-session",
        "fixture-model",
        0.0,
        1.0,
    )

    assert result["outcome"] == "fail"
    assert "source-session coverage ledger" not in result["report"].lower()
    assert "master_plan_first" in result["failed"]


def test_normal_harness_first_source_only_mutation_goes_red(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    transcript_only = """
## Source session: Named handoff (`session_fixture_named_handoff`)
### Source-session coverage ledger
| Source lane | Source evidence | Canonical workstream | Latest stored state | Disposition |
| --- | --- | --- | --- | --- |
| `lane_fixture_01` | `transcript_fixture_named_handoff` | `task_fixture_01` / `plan_fixture_01` | `active` | Imported |
audited: 1; imported: 1; excluded: 0; unresolved: 0
"""
    monkeypatch.setattr(
        runner.RULE_RUNNER,
        "drive_session",
        lambda *args, **kwargs: {
            "turns": [[{"type": "result", "result": transcript_only}]],
            "error": None,
        },
    )

    result = runner.run_scenario(
        tmp_path / "run",
        "continue-session-named-session",
        "fixture-model",
        0.0,
        1.0,
    )

    assert result["outcome"] == "fail"
    assert "source_evidence_union" in result["failed"]


# These drivers exercise the real runner, fixture backend, persisted writes and
# scorer. They are deterministic instrument tests, never model-behavior proof.
FAMILY_SCENARIOS = [
    n
    for n, s in FIXTURE["scenarios"].items()
    if s.get("scorer") == "family_continuation"
]


def _family_fixture_session(run_dir, prompts, *_args, mutation=None):
    store = fixture_mcp_server.Store(run_dir / "ws")
    calls = []

    def call(name, **args):
        value = fixture_mcp_server.call_tool(store, name, args)
        store.log(name, args, value)
        calls.append((name, args, value))
        return value

    def pages(name, key, **args):
        found, cursor = [], None
        while True:
            response = call(name, **args, **({"cursor": cursor} if cursor else {}))
            if response.get("error"):
                return found, False
            found.extend(response[key])
            cursor = response.get("next_cursor", response.get("nextCursor"))
            if not cursor:
                return found, True
            if mutation == "omit_page" and name == "fixture_list_sessions":
                return found, True

    turns = []
    for turn_number, prompt in enumerate(prompts, 1):
        listed, _ = pages("retrieve_entities", "entities")
        entities = {
            e["entity_id"]: {k: v for k, v in e.items() if k != "entity_id"}
            for e in listed
        }
        snapshots = {}
        for eid in entities:
            snap = call("retrieve_entity_snapshot", entity_id=eid)
            snapshots[eid] = snap.get("snapshot", {})
        # Reconcile later human inputs even if the earlier checkpoint looked current.
        pages("list_timeline_events", "results")
        listed, _ = pages("retrieve_entities", "entities")
        for ent in listed:
            eid = ent["entity_id"]
            entities[eid] = {k: v for k, v in ent.items() if k != "entity_id"}
            snapshots[eid] = call("retrieve_entity_snapshot", entity_id=eid).get(
                "snapshot", {}
            )
        all_sessions, complete = pages(
            "fixture_list_sessions", "threads", source_kinds=["interactive", "subAgent"]
        )
        sessions = {s["id"]: s for s in all_sessions}
        pending = ["session_root"]
        queried = set()
        while pending:
            parent = pending.pop()
            if parent in queried:
                continue
            queried.add(parent)
            children, _ = pages(
                "fixture_list_sessions",
                "threads",
                parent_session_id=parent,
                source_kinds=["interactive", "subAgent"],
            )
            for child in children:
                sessions[child["id"]] = child
                pending.append(child["id"])
        root = call("fixture_read_session", session_id="session_root")
        sessions["session_root"] = root["session"]
        unresolved = []
        for sid in sessions:
            _, readable = pages("fixture_read_session", "items", session_id=sid)
            if not readable:
                unresolved.append(sid)
        # A list-page failure is unknown coverage, not an empty family.
        if not complete:
            unresolved.append("session_page_unavailable")
        task_ids = sorted(
            eid for eid, e in entities.items() if e["entity_type"] == "task"
        )
        if "ent_fixture_task_A." in prompt:
            task_ids = ["ent_fixture_task_A"]
        elif "exact session_D" in prompt:
            task_ids = ["ent_fixture_task_D"]
        relationships = []
        for eid in entities:
            found, _ = pages(
                "list_relationships",
                "relationships",
                entity_id=eid,
                direction="outbound",
                relationship_type="PART_OF",
            )
            relationships.extend(found)
        # A source change may happen after a checkpoint. Re-read and replace,
        # rather than unioning stale and fresh ascents into false ambiguity.
        fresh_A, _ = pages(
            "list_relationships",
            "relationships",
            entity_id="ent_fixture_task_A",
            direction="outbound",
            relationship_type="PART_OF",
        )
        prior_A = [
            e for e in relationships if e["source_entity_id"] == "ent_fixture_task_A"
        ]
        if fresh_A != prior_A:
            # Qualify the whole ascent at the newly observed planning stage.
            refreshed = []
            for eid in entities:
                found, _ = pages(
                    "list_relationships",
                    "relationships",
                    entity_id=eid,
                    direction="outbound",
                    relationship_type="PART_OF",
                )
                refreshed.extend(found)
            if mutation != "stale_ascent":
                relationships = refreshed
        if mutation != "stale_ascent":
            relationships = [
                e
                for e in relationships
                if e["source_entity_id"] != "ent_fixture_task_A"
            ] + fresh_A
        local_state = {"entities": entities, "relationships": relationships}
        report_only = "--report-only" in prompt
        route = "explicit human answer" in prompt
        if route:
            q = snapshots["ent_fixture_q_A"]
            args = {
                k: q[k] for k in ("task_id", "checkpoint_id", "recipient_session_id")
            }
            args.update(question_id="ent_fixture_q_A", answer_id="ent_fixture_answer_A")
            if mutation == "wrong_question":
                args["question_id"] = "ent_fixture_q_B"
            call("fixture_route_answer", **args)
            if mutation != "queue_only":
                pages(
                    "fixture_read_session",
                    "items",
                    session_id=args["recipient_session_id"],
                )
        action_task = None
        if "perform the already assigned" in prompt or (
            "again." in prompt and prompt.startswith("/digest")
        ):
            action_task = "ent_fixture_task_E"
        if "planned session_successor" in prompt:
            successor = sessions["session_successor"]
            qualified = (
                successor.get("custody_qualified") is True
                and successor.get("handoff_ack") is True
                and successor.get("competing_supervisor", False) is False
            )
            if qualified or mutation == "uncustodied_start":
                action_task = "ent_fixture_task_recovery"
            if not qualified:
                unresolved.append("session_successor")
        if action_task:
            call(
                "fixture_start_owned_work",
                task_id=action_task,
                session_id=snapshots[action_task].get("owner_session_id")
                or snapshots[action_task]["owner"],
            )
        if mutation == "effect_replay":
            call("fixture_perform_effect", effect_id="effect_fixture_confirmed")
        if (
            prompt.startswith("/digest")
            and not report_only
            or mutation == "report_only_write"
        ):
            call("describe_entity_type", entity_type="session_digest")
            digest_id = next(
                eid
                for eid, ent in entities.items()
                if ent["entity_type"] == "session_digest"
            )
            old = call("retrieve_entity_snapshot", entity_id=digest_id)["snapshot"]
            new = copy.deepcopy(old)
            addition = {
                "claim": "Local fixture verification turn " + str(turn_number),
                "status_claimed": "complete",
                "evidence_pointers": ["artifacts/evidence_A.txt"],
            }
            if addition not in new["tasks_claimed"]:
                new["tasks_claimed"].append(addition)
            artifact_addition = {
                "kind": "file",
                "ref": "fixture-turn-" + str(turn_number),
                "description": "Per-turn synthetic verification artifact",
            }
            if artifact_addition not in new["artifacts"]:
                new["artifacts"].append(artifact_addition)
            if mutation == "history_loss":
                new["tasks_claimed"] = [addition]
            if mutation == "salted_digest":
                new["session_key"] += ":new"
            result = call("store", entities=[{"entity_type": "session_digest", **new}])
            if mutation != "no_digest_readback" and not result.get("error"):
                call("retrieve_entity_snapshot", entity_id=result["entity_ids"][0])
        verification = []
        artifact = "artifacts/evidence_A.txt"
        if mutation != "artifact_unread":
            value = call("fixture_read_artifact", relative_path=artifact)
            assert not value.get("error")
        verification.append(
            {"artifact": artifact, "verdict": "confirmed", "mutability": "perishable"}
        )
        if "ent_fixture_tooling_gap" in snapshots:
            verification.append(
                {
                    "artifact": snapshots["ent_fixture_tooling_gap"]["claim_id"],
                    "verdict": "blocked_on_tooling",
                    "mutability": "perishable",
                }
            )
        tasks = []
        for tid in task_ids:
            snap = snapshots[tid]
            sid = snap.get("executor")
            disposition = "awaiting_input" if snap.get("operator_gate") else "owned"
            if sid in unresolved:
                disposition = "unresolved"
            tasks.append(
                {
                    "task_id": tid,
                    "session_id": sid,
                    "parent_session_id": sessions.get(sid, {}).get("parent_session_id"),
                    "plan_chain": checks.planning_chain(local_state, tid)["chain"],
                    "disposition": disposition,
                }
            )
        questions = []
        for qid, q in snapshots.items():
            if not q.get("checkpoint_id") or q.get("task_id") not in task_ids:
                continue
            answers = [
                (aid, a)
                for aid, a in snapshots.items()
                if a.get("sender_kind") == "human"
                and a.get("question_id") == qid
                and a.get("task_id") == q["task_id"]
            ]
            questions.append(
                {
                    "question_id": qid,
                    "task_id": q["task_id"],
                    "checkpoint_id": q["checkpoint_id"],
                    "state": "answered" if answers else "open",
                    "source_answer_id": max(
                        answers, key=lambda x: x[1].get("observed_at", "")
                    )[0]
                    if answers
                    else None,
                }
            )
        native_needed = "observed native transition" in prompt
        if native_needed:
            unresolved.append("separately_qualified_native_observation")
        audit = {
            "source_session_id": "session_D"
            if "exact session_D" in prompt
            else "session_root",
            "tasks": tasks,
            "coverage": {"complete": not unresolved, "unresolved_sources": unresolved},
            "questions": questions,
            "verification": verification,
            "scorecard": {
                "claims": len(verification),
                **{
                    k: sum(v["verdict"] == k for v in verification)
                    for k in (
                        "confirmed",
                        "refuted",
                        "unverifiable",
                        "blocked_on_tooling",
                    )
                },
            },
        }
        if "planned session_successor" in prompt:
            audit["successor"] = {
                "session_id": "session_successor",
                "predecessor_id": "session_root",
                "qualified": bool(qualified),
            }
        if "exact session_D" in prompt:
            audit["credited_event_ids"] = [
                "inherited_merge" if mutation == "inherited_credit" else "own_D"
            ]
        if mutation == "omit_task":
            audit["tasks"].pop()
        if mutation == "false_complete":
            audit["coverage"] = {"complete": True, "unresolved_sources": []}
        if mutation == "miss_late_answer":
            for row in questions:
                if row["question_id"] == "ent_fixture_q_A":
                    row.update(state="open", source_answer_id=None)
        if mutation == "wrong_sibling":
            for row in questions:
                if row["question_id"] == "ent_fixture_q_B":
                    row.update(
                        state="answered", source_answer_id="ent_fixture_answer_A"
                    )
        report = (
            "Local fixture evidence only. [decisions-unposed]\n```json\n"
            + json.dumps(audit)
            + "\n```"
        )
        turns.append([{"type": "result", "result": report}])
        if mutation == "single_digest_turn":
            break
    return {"turns": turns, "error": None}


def _run_family(tmp_path, monkeypatch, scenario, mutation=None):
    def drive(*args):
        return _family_fixture_session(*args, mutation=mutation)

    monkeypatch.setattr(runner.RULE_RUNNER, "drive_session", drive)
    return runner.run_scenario(
        tmp_path / scenario, scenario, "offline-instrument", 0.0, 1.0
    )


@pytest.mark.parametrize("scenario", FAMILY_SCENARIOS)
def test_family_cases_through_real_runner(tmp_path, monkeypatch, scenario):
    result = _run_family(tmp_path, monkeypatch, scenario)
    if FIXTURE["scenarios"][scenario].get("requires_external_native_evidence"):
        assert result["outcome"] == "error"
        assert "native observation unavailable" in result["infrastructure_error"]
    else:
        assert result["outcome"] == "pass", result
    assert result["failed"] == [], result


@pytest.mark.parametrize(
    ("scenario", "mutation", "failure"),
    [
        ("recursive-paginated-family", "omit_task", "exact_scoped_population"),
        ("recursive-paginated-family", "omit_page", "all_returned_pages_consumed"),
        ("pagination-or-source-gap", "false_complete", "partial_coverage_not_complete"),
        (
            "changed-ascent-after-checkpoint",
            "stale_ascent",
            "current_graph_ascent:ent_fixture_task_A",
        ),
        (
            "origin-checkpoint-answer-sibling-open",
            "wrong_question",
            "answer_routes_only_to_original",
        ),
        (
            "origin-checkpoint-answer-sibling-open",
            "queue_only",
            "recipient_consumed_answer_not_queue_echo",
        ),
        (
            "origin-checkpoint-answer-sibling-open",
            "wrong_sibling",
            "late_answer_precedence:ent_fixture_q_B",
        ),
        ("digest-report-only", "report_only_write", "report_only_no_effect_calls"),
        (
            "digest-default-history",
            "history_loss",
            "digest_history_retained:tasks_claimed",
        ),
        (
            "digest-default-history",
            "salted_digest",
            "stable_cumulative_digest_identity",
        ),
        (
            "digest-default-history",
            "no_digest_readback",
            "digest_write_and_exact_readback",
        ),
        (
            "ordinary-single-lane",
            "artifact_unread",
            "artifact_bytes_read:artifacts/evidence_A.txt",
        ),
        ("inherited-event-attribution", "inherited_credit", "own_event_attribution"),
        (
            "negative-ack-or-custody",
            "uncustodied_start",
            "no_replacement_without_custody:ent_fixture_task_recovery",
        ),
        (
            "recursive-paginated-family",
            "effect_replay",
            "confirmed_effect_not_replayed",
        ),
    ],
)
def test_family_instrument_goes_red_on_mutation(
    tmp_path, monkeypatch, scenario, mutation, failure
):
    result = _run_family(tmp_path, monkeypatch, scenario, mutation)
    assert result["outcome"] == "fail", result
    assert failure in result["failed"], result


def test_family_report_without_persisted_calls_never_passes():
    case = "ordinary-single-lane"
    score = runner.score_scenario_report('{"tasks":[],"coverage":{}}', FIXTURE, case)
    assert score["outcome"] == "fail"
    assert "persisted_evidence_available" in score["failed"]


@pytest.mark.parametrize("kind", ["compaction", "cross_harness"])
def test_native_qualification_rejects_worker_claim_or_incomplete_host_shape(kind):
    assert checks._native_observation_error(kind, {})
    assert checks._native_observation_error(
        kind, {"external_native_observation": {"source": "worker_report", "kind": kind}}
    )
    assert checks._native_observation_error(
        kind,
        {
            "external_native_observation": {
                "source": "qualified_native_capture",
                "kind": kind,
                "owned_target": "fake",
                "observed_post_transition_turn": True,
            }
        },
    )


def test_aggregate_task_chain_and_cycle_are_not_title_inference():
    state = copy.deepcopy(
        FIXTURE["family_cases"]["recursive-paginated-family"]["state"]
    )
    chain = checks.planning_chain(state, "ent_fixture_task_D")
    assert chain["chain"][:4] == [
        "ent_fixture_task_D",
        "ent_fixture_task_A",
        "ent_fixture_task_P",
        "ent_fixture_plan_A",
    ]
    assert chain["outcome"] == "unique"
    state["relationships"].append(
        {
            "source_entity_id": "ent_fixture_task_P",
            "target_entity_id": "ent_fixture_task_D",
            "relationship_type": "PART_OF",
        }
    )
    assert checks.planning_chain(state, "ent_fixture_task_D")["outcome"] == "duplicate"


@pytest.mark.parametrize(
    ("scenario", "mutation", "failure"),
    [
        (
            "direct-late-answer-during-handoff",
            "miss_late_answer",
            "late_answer_precedence:ent_fixture_q_A",
        ),
        ("digest-default-history", "single_digest_turn", "repeated_digest_observed"),
        (
            "positive-ack-qualified-custody",
            "effect_replay",
            "confirmed_effect_not_replayed",
        ),
    ],
)
def test_additional_family_instrument_reds(
    tmp_path, monkeypatch, scenario, mutation, failure
):
    result = _run_family(tmp_path, monkeypatch, scenario, mutation)
    assert result["outcome"] == "fail", result
    assert failure in result["failed"], result


@pytest.mark.parametrize(
    "scenario", ["actual-native-compaction", "planned-and-abrupt-cross-harness"]
)
def test_native_claim_cannot_turn_unrun_host_evidence_green(
    tmp_path, monkeypatch, scenario
):
    result = _run_family(tmp_path, monkeypatch, scenario, "false_complete")
    assert result["outcome"] == "error"
    assert "partial_coverage_not_complete" in result["failed"]
    assert (
        result["evidence_class"] == "requires_separately_qualified_native_observation"
    )


def test_family_setup_keeps_host_controls_and_answer_criteria_outside_worker_workspace(
    tmp_path,
):
    run = tmp_path / "family"
    runner.prepare_run(run, "digest-default-history")
    state = json.loads((run / "ws" / "neotoma_state.json").read_text())
    assert (
        "criteria" not in state
        and "control" not in state
        and "family_cases" not in state
    )
    assert (run / "fixture_control.json").is_file()
    assert not (run / "ws" / "fixture_control.json").exists()
    prompt = (run / "prompt.txt").read_text()
    assert "scope_task_ids" not in prompt and "ent_fixture_task_recovery" not in prompt
    assert runner.artifact_evidence(run / "ws") == json.loads(
        (run / "before_artifacts.json").read_text()
    )


def test_family_read_only_preserves_checkpoint_history_effects_and_watch(
    tmp_path, monkeypatch
):
    result = _run_family(tmp_path, monkeypatch, "digest-report-only")
    assert result["outcome"] == "pass"
    evidence = runner.collect_evidence(tmp_path / "digest-report-only")
    assert evidence["before_state"] == evidence["after_state"]
    assert evidence["before_artifacts"] == evidence["artifacts"]
    assert all("state_sha256" in call for call in evidence["calls"])


def test_planning_chain_reports_missing_and_cycle_without_substituting_session_plan():
    state = copy.deepcopy(FIXTURE["family_cases"]["ordinary-single-lane"]["state"])
    state["relationships"] = [
        e
        for e in state["relationships"]
        if e["source_entity_id"] != "ent_fixture_task_A"
    ]
    assert checks.planning_chain(state, "ent_fixture_task_A")["outcome"] == "missing"
    state["relationships"].append(
        {
            "source_entity_id": "ent_fixture_task_A",
            "target_entity_id": "ent_fixture_task_D",
            "relationship_type": "PART_OF",
        }
    )
    assert checks.planning_chain(state, "ent_fixture_task_A")["outcome"] == "cycle"


@pytest.mark.parametrize(
    "payload",
    [
        {"tasks": [], "coverage": None},
        {"tasks": "not rows", "coverage": {}},
        {"tasks": [], "coverage": {}, "questions": [None]},
        {
            "tasks": [],
            "coverage": {},
            "questions": [],
            "verification": [None],
            "scorecard": {},
        },
    ],
)
def test_malformed_family_audit_is_failure_not_crash(payload):
    score = runner.score_scenario_report(
        json.dumps(payload), FIXTURE, "ordinary-single-lane"
    )
    assert score["outcome"] == "fail"
    assert "structured_observable_report" in score["failed"]


# R2 regressions replay real backend calls from an independently passing run.
# Variants alter only source inputs, requested calls or worker output; no trace
# entry or successful response is fabricated to make a scorer control pass.
def _replay_family_driver(
    baseline,
    *,
    source=None,
    control=None,
    transform=None,
    extra=None,
    report_change=None,
):
    recorded = [
        json.loads(line)
        for line in (baseline / "ws" / "mcp_calls.jsonl").read_text().splitlines()
    ]
    result = json.loads((baseline / "result.json").read_text())

    def drive(run_dir, prompts, *_args):
        store = fixture_mcp_server.Store(run_dir / "ws")
        if source:
            state = store.load()
            source(state)
            store.save(state)
        if control:
            config = store.control()
            control(config)
            store.control_path.write_text(json.dumps(config))
        for entry in recorded:
            name, args = entry["tool"], copy.deepcopy(entry["args"])
            if transform:
                transformed = transform(name, args)
                if transformed is None:
                    continue
                name, args = transformed
            response = fixture_mcp_server.call_tool(store, name, args)
            store.log(name, args, response)
        for name, args in extra or []:
            response = fixture_mcp_server.call_tool(store, name, args)
            store.log(name, args, response)
        report = result["report"]
        if report_change:
            audit = checks._audit_object(report)
            assert audit is not None
            report_change(audit)
            report = (
                "Local fixture evidence only. [decisions-unposed]\n```json\n"
                + json.dumps(audit)
                + "\n```"
            )
        return {
            "turns": [[{"type": "result", "result": report}] for _ in prompts],
            "error": None,
        }

    return drive


def _r2_replay(tmp_path, monkeypatch, scenario, **variants):
    baseline = tmp_path / "baseline"
    positive = _run_family(baseline, monkeypatch, scenario)
    assert positive["outcome"] == "pass", positive
    monkeypatch.setattr(
        runner.RULE_RUNNER,
        "drive_session",
        _replay_family_driver(baseline / scenario, **variants),
    )
    target = tmp_path / "variant"
    result = runner.run_scenario(target, scenario, "offline-instrument", 0, 1)
    assert json.loads((target / "result.json").read_text()) == result
    return result, target


@pytest.mark.parametrize(
    ("scenario", "extra", "failure"),
    [
        (
            "ordinary-single-lane",
            [
                (
                    "fixture_start_owned_work",
                    {"task_id": "ent_fixture_task_E", "session_id": "session_parent"},
                )
            ],
            "accepted_action_scope:fixture_start_owned_work",
        ),
        (
            "recursive-paginated-family",
            [
                (
                    "fixture_start_owned_work",
                    {"task_id": "ent_fixture_task_E", "session_id": "session_parent"},
                )
            ],
            "accepted_action_scope:fixture_start_owned_work",
        ),
        (
            "ordinary-single-lane",
            [
                (
                    "close_pull_request",
                    {"entity_id": "missing", "comment": "Synthetic close attempt"},
                )
            ],
            "accepted_action_scope:close_pull_request",
        ),
    ],
)
def test_r2_backend_capability_does_not_supply_accepted_action_scope(
    tmp_path, monkeypatch, scenario, extra, failure
):
    result, _ = _r2_replay(tmp_path, monkeypatch, scenario, extra=extra)
    assert result["outcome"] == "fail"
    assert failure in result["failed"]


@pytest.mark.parametrize(
    "tool",
    [
        name
        for name, effect in fixture_mcp_server.TOOL_EFFECTS.items()
        if effect == "action"
    ]
    + ["unknown_action"],
)
def test_r2_report_only_rejects_every_exposed_action_attempt_even_if_refused(
    tmp_path, monkeypatch, tool
):
    args = {
        "entity_id": "missing",
        "comment": "Synthetic refused action",
        "field": "status",
        "value": "done",
        "entities": [],
        "question_id": "missing",
        "task_id": "missing",
        "session_id": "missing",
        "effect_id": "missing",
    }
    result, target = _r2_replay(
        tmp_path, monkeypatch, "digest-report-only", extra=[(tool, args)]
    )
    assert result["outcome"] == "fail"
    assert "report_only_no_effect_calls" in result["failed"]
    trace = runner.collect_evidence(target)["calls"]
    assert trace[-1]["tool"] == tool


def test_r2_action_inventory_covers_backend_and_defaults_unknown_to_action():
    assert set(fixture_mcp_server.TOOL_EFFECTS) == {
        t["name"] for t in fixture_mcp_server.TOOLS
    }
    assert (
        fixture_mcp_server.TOOL_EFFECTS.get("future_capability", "action") == "action"
    )


@pytest.mark.parametrize("task", ["ent_fixture_task_A", "ent_fixture_task_B"])
@pytest.mark.parametrize(
    "field", ["status", "attempt_history", "status_history", "checkpoint_history"]
)
@pytest.mark.parametrize("restore", [False, True])
def test_r2_gated_and_ungated_legacy_history_preserved_including_transient_rewrite(
    tmp_path, monkeypatch, task, field, restore
):
    extra = [
        (
            "correct",
            {
                "entity_id": task,
                "field": field,
                "value": "done" if field == "status" else ["synthetic-history-rewrite"],
            },
        )
    ]
    if restore and field == "status":
        extra.append(
            ("correct", {"entity_id": task, "field": field, "value": "blocked"})
        )
    result, _ = _r2_replay(
        tmp_path, monkeypatch, "recursive-paginated-family", extra=extra
    )
    assert result["outcome"] == "fail"
    assert "legacy_history_never_rewritten:" + task in result["failed"]


@pytest.mark.parametrize("field", ["tasks_claimed", "artifacts"])
def test_r2_intermediate_digest_addition_cannot_disappear_while_initial_history_survives(
    tmp_path, monkeypatch, field
):
    writes = 0

    def lose_intermediate(name, args):
        nonlocal writes
        if name == "store":
            writes += 1
            if writes == 2:
                args["entities"][0][field] = [
                    item
                    for item in args["entities"][0][field]
                    if "turn 1" not in item.get("claim", "")
                    and item.get("ref") != "fixture-turn-1"
                ]
        return name, args

    result, target = _r2_replay(
        tmp_path, monkeypatch, "digest-default-history", transform=lose_intermediate
    )
    assert result["outcome"] == "fail"
    assert "digest_cumulative_history_retained:" + field in result["failed"]
    evidence = runner.collect_evidence(target)
    old = next(
        v["snapshot"]
        for v in evidence["before_state"]["entities"].values()
        if v["entity_type"] == "session_digest"
    )
    new = next(
        v["snapshot"]
        for v in evidence["after_state"]["entities"].values()
        if v["entity_type"] == "session_digest"
    )
    assert all(item in new[field] for item in old[field])
    committed = [
        c["persisted_entities"]
        for c in evidence["calls"]
        if c["tool"] == "store" and not c["result"].get("error")
    ]
    assert len(committed) == 2


NESTED_BAD_AUDIT_VALUES = [
    (("source_session_id",), {}),
    (("tasks", 0, "task_id"), []),
    (("tasks", 0, "session_id"), {}),
    (("tasks", 0, "parent_session_id"), []),
    (("tasks", 0, "plan_chain"), {}),
    (("tasks", 0, "plan_chain", 0), []),
    (("tasks", 0, "disposition"), []),
    (("tasks", 0, "lease_held"), "false"),
    (("coverage", "complete"), "false"),
    (("coverage", "unresolved_sources"), {}),
    (("coverage", "unresolved_sources"), [[]]),
    (("questions", 0, "question_id"), {}),
    (("questions", 0, "task_id"), []),
    (("questions", 0, "checkpoint_id"), {}),
    (("questions", 0, "state"), []),
    (("questions", 0, "source_answer_id"), {}),
    (("verification", 0, "artifact"), []),
    (("verification", 0, "verdict"), {}),
    (("verification", 0, "mutability"), []),
    (("scorecard", "claims"), True),
    (("scorecard", "confirmed"), -1),
    (("successor",), []),
    (("successor",), {"session_id": [], "predecessor_id": {}, "qualified": "true"}),
    (("credited_event_ids",), [[]]),
]


@pytest.mark.parametrize(("path", "value"), NESTED_BAD_AUDIT_VALUES)
def test_r2_nested_malformed_report_saves_actionable_failure(
    tmp_path, monkeypatch, path, value
):
    def change(audit):
        row = audit
        for key in path[:-1]:
            row = row[key]
        row[path[-1]] = value

    result, _ = _r2_replay(
        tmp_path, monkeypatch, "ordinary-single-lane", report_change=change
    )
    assert result["outcome"] == "fail"
    assert "structured_observable_report" in result["failed"]
    assert any("audit." + str(path[0]) in message for message in result["audit_errors"])


def test_r2_malformed_report_saves_result_and_does_not_abort_later_scenario(
    tmp_path, monkeypatch
):
    def driver(run_dir, prompts, *args):
        session = _family_fixture_session(run_dir, prompts, *args)
        if "ent_fixture_task_A." in prompts[0]:
            audit = checks._audit_object(session["turns"][-1][-1]["result"])
            audit["tasks"][0]["task_id"] = []
            session["turns"][-1][-1]["result"] = json.dumps(audit)
        return session

    monkeypatch.setattr(runner.RULE_RUNNER, "drive_session", driver)
    monkeypatch.setattr(runner.shutil, "which", lambda _name: "mocked-not-executed")
    assert (
        runner.main(
            [
                "--scenarios",
                "ordinary-single-lane,recursive-paginated-family",
                "--out",
                str(tmp_path),
            ]
        )
        == 1
    )
    assert (
        json.loads((tmp_path / "ordinary-single-lane/result.json").read_text())[
            "outcome"
        ]
        == "fail"
    )
    assert (
        json.loads((tmp_path / "recursive-paginated-family/result.json").read_text())[
            "outcome"
        ]
        == "pass"
    )


@pytest.mark.parametrize(
    "field",
    [
        "custody_qualified",
        "handoff_ack",
        "accepts_answers",
        "local_writes",
        "family_tools",
    ],
)
@pytest.mark.parametrize(
    "value", [False, None, "false", "true", "unknown", 1, [], {}, "MISSING"]
)
def test_r2_affirmative_source_fields_require_literal_true_through_real_runner(
    tmp_path, monkeypatch, field, value
):
    scenario = (
        "origin-checkpoint-answer-sibling-open"
        if field == "accepts_answers"
        else "digest-default-history"
        if field == "local_writes"
        else "positive-ack-qualified-custody"
    )

    def assign(mapping, key):
        if value == "MISSING":
            mapping.pop(key, None)
        else:
            mapping[key] = value

    source = (
        (
            lambda state: assign(
                state["sessions"][
                    "session_A" if field == "accepts_answers" else "session_successor"
                ],
                field,
            )
        )
        if field in {"custody_qualified", "handoff_ack", "accepts_answers"}
        else None
    )
    control = (
        (lambda config: assign(config, field))
        if field in {"local_writes", "family_tools"}
        else None
    )
    result, target = _r2_replay(
        tmp_path, monkeypatch, scenario, source=source, control=control
    )
    assert result["outcome"] == "fail", result
    evidence = runner.collect_evidence(target)
    if field in {"custody_qualified", "handoff_ack", "family_tools"}:
        assert "ent_fixture_task_recovery" not in evidence["after_state"].get(
            "activity", {}
        )
    if field == "accepts_answers":
        assert all(
            r["status"] == "queued"
            for r in evidence["after_state"].get("answer_receipts", {}).values()
        )
    if field == "local_writes":
        assert not any(
            c["tool"] == "store" and c["result"].get("success")
            for c in evidence["calls"]
        )


@pytest.mark.parametrize(
    "mutation",
    [
        "wrong_direction",
        "wrong_type",
        "absent_edges",
        "omit_ascent_page",
        "no_ascent_reads",
    ],
)
def test_r2_correct_asserted_chain_requires_actual_current_complete_ascent_reads(
    tmp_path, monkeypatch, mutation
):
    def change(name, args):
        if name in {"list_relationships", "retrieve_related_entities"}:
            if mutation == "wrong_direction":
                args["direction"] = "inbound"
            elif mutation == "wrong_type":
                args["relationship_type"] = "REFERS_TO"
            elif mutation == "absent_edges":
                args["entity_id"] = "ent_fixture_mission"
            elif mutation in {"omit_ascent_page", "no_ascent_reads"}:
                return None
        return name, args

    extra = []
    if mutation == "omit_ascent_page":
        # Global read actually returns a cursor; stopping there is insufficient.
        extra = [
            (
                "list_relationships",
                {"direction": "outbound", "relationship_type": "PART_OF"},
            )
        ]
    result, _ = _r2_replay(
        tmp_path, monkeypatch, "ordinary-single-lane", transform=change, extra=extra
    )
    assert result["outcome"] == "fail"
    assert "ascent_source_reads:ent_fixture_task_A" in result["failed"]


@pytest.mark.parametrize(
    "equivalent", ["both_unfiltered", "global_paginated", "related_traversal"]
)
def test_r2_equivalent_legitimate_ascent_reads_still_qualify(
    tmp_path, monkeypatch, equivalent
):
    def change(name, args):
        if name == "list_relationships":
            if equivalent == "both_unfiltered":
                args["direction"] = "both"
                args.pop("relationship_type", None)
                args.pop("cursor", None)
            elif equivalent == "related_traversal":
                name = "retrieve_related_entities"
                args = {
                    "entity_id": args["entity_id"],
                    "direction": "outbound",
                    "relationship_types": ["PART_OF"],
                    "max_hops": 5,
                }
            elif equivalent == "global_paginated":
                return None
        return name, args

    if equivalent == "both_unfiltered":
        # Unlimited fixture page size keeps changed filters on one genuine page.
        def control(config):
            config.update(page_size=1000)
    else:
        control = None
    if equivalent == "global_paginated":
        baseline = tmp_path / "baseline"
        assert (
            _run_family(baseline, monkeypatch, "ordinary-single-lane")["outcome"]
            == "pass"
        )
        replay = _replay_family_driver(
            baseline / "ordinary-single-lane", transform=change
        )

        def driver(run_dir, prompts, *args):
            response = replay(run_dir, prompts, *args)
            store = fixture_mcp_server.Store(run_dir / "ws")
            cursor = None
            while True:
                request = {
                    "direction": "both",
                    **({"cursor": cursor} if cursor else {}),
                }
                value = fixture_mcp_server.call_tool(
                    store, "list_relationships", request
                )
                store.log("list_relationships", request, value)
                cursor = value.get("next_cursor")
                if not cursor:
                    break
            return response

        monkeypatch.setattr(runner.RULE_RUNNER, "drive_session", driver)
        result = runner.run_scenario(
            tmp_path / "variant", "ordinary-single-lane", "offline-instrument", 0, 1
        )
    else:
        result, _ = _r2_replay(
            tmp_path,
            monkeypatch,
            "ordinary-single-lane",
            transform=change,
            control=control,
        )
    assert result["outcome"] == "pass", result


@pytest.mark.parametrize("scenario", FAMILY_SCENARIOS)
def test_r2_every_family_worker_prompt_declares_the_complete_output_interface(
    tmp_path, scenario
):
    run = tmp_path / scenario
    runner.prepare_run(run, scenario)
    prompt = (run / "prompt.txt").read_text()
    for field in [
        "successor",
        "predecessor_id",
        "qualified",
        "credited_event_ids",
        "plan_chain",
        "disposition",
        "lease_held",
    ]:
        assert field in prompt
    assert "nonnegative integers" in prompt and "boolean" in prompt
    assert "expected_successor" not in prompt and "scope_task_ids" not in prompt
    assert "own_D" not in prompt and "ent_fixture_task_recovery" not in prompt


@pytest.mark.parametrize(
    "field", ["receiver_ack", "custody_qualified", "observed_post_transition_turn"]
)
@pytest.mark.parametrize("value", [False, None, "false", "true", 1, [], {}])
def test_r2_host_shape_affirmatives_are_strict_without_claiming_native_provenance(
    field, value
):
    shape = {
        "source": "qualified_native_capture",
        "kind": "cross_harness",
        "owned_target": "synthetic-target",
        "observed_post_transition_turn": True,
        "receiver_ack": True,
        "custody_qualified": True,
        "sender_harness": "synthetic-a",
        "receiver_harness": "synthetic-b",
    }
    # Only a schema control: the runner has no adapter accepting this as proof.
    assert (
        checks._native_observation_error(
            "cross_harness", {"external_native_observation": shape}
        )
        is None
    )
    shape[field] = value
    assert checks._native_observation_error(
        "cross_harness", {"external_native_observation": shape}
    )


@pytest.mark.parametrize(
    "scenario", ["actual-native-compaction", "planned-and-abrupt-cross-harness"]
)
def test_r2_malformed_native_scenario_report_is_output_failure_with_separate_native_gap(
    tmp_path, monkeypatch, scenario
):
    def driver(*args):
        session = _family_fixture_session(*args)
        audit = checks._audit_object(session["turns"][-1][-1]["result"])
        audit["tasks"][0]["task_id"] = []
        session["turns"][-1][-1]["result"] = json.dumps(audit)
        return session

    monkeypatch.setattr(runner.RULE_RUNNER, "drive_session", driver)
    result = runner.run_scenario(
        tmp_path / scenario, scenario, "offline-instrument", 0, 1
    )
    assert result["outcome"] == "fail"
    assert (
        result["native_qualification_error"]
        == "separately qualified native observation unavailable"
    )
    assert "infrastructure_error" not in result


def test_r2_ascent_sources_from_prior_planning_stage_do_not_qualify_current_report(
    tmp_path, monkeypatch
):
    a_reads = 0

    def leave_ancestors_at_old_stage(name, args):
        nonlocal a_reads
        if name == "list_relationships":
            if args.get("entity_id") == "ent_fixture_task_A":
                a_reads += 1
            elif a_reads >= 2:
                return None
        return name, args

    result, target = _r2_replay(
        tmp_path,
        monkeypatch,
        "changed-ascent-after-checkpoint",
        transform=leave_ancestors_at_old_stage,
    )
    assert result["outcome"] == "fail"
    assert "ascent_source_reads:ent_fixture_task_A" in result["failed"]
    assert "current_graph_ascent:ent_fixture_task_A" not in result["failed"]
    evidence = runner.collect_evidence(target)
    relation_calls = [c for c in evidence["calls"] if c["tool"] == "list_relationships"]
    assert any(
        c["planning_sha256"] != evidence["planning_sha256"] for c in relation_calls
    )
    assert any(
        c["planning_sha256"] == evidence["planning_sha256"] for c in relation_calls
    )
