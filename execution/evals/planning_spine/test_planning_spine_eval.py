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
        label: sum(value == label for value in outcomes["expected_dispositions"].values())
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


def _source_lanes_by_kind(
    fixture: dict, source_session_id: str
) -> dict[str, set[str]]:
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

    result = checks.score_source_session_resume(
        wrong, FIXTURE, SOURCE_SESSION_SCENARIO
    )

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

    result = checks.score_source_session_resume(
        wrong, FIXTURE, SOURCE_SESSION_SCENARIO
    )

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

    result = checks.score_source_session_resume(
        wrong, FIXTURE, SOURCE_SESSION_SCENARIO
    )

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

    result = checks.score_source_session_resume(
        wrong, FIXTURE, SOURCE_SESSION_SCENARIO
    )

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
        "No work was resumed", "Unreadable evidence was treated as empty; coverage is complete"
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
            NOT_FOUND_REPORT + "\nSelected plan_fixture_01 and domain action completed.\n",
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
