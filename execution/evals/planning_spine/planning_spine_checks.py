"""Observable-output checks for the planning-spine agentic eval.

The evaluator reads the final report and the fixture graph. It never scores a
skill body for containing policy phrases: the observable report must exhibit
the master-plan-first structure the policy requires.
"""

from __future__ import annotations

import re

_PHASE_ROW = re.compile(
    r"^\s*\|\s*(?P<phase>[A-Z](?:\d+)?(?:-prime)?)\s*\|"
    r"\s*(?P<gate>[a-z_ -]+?)\s*\|\s*$",
    re.IGNORECASE,
)
_H3 = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
_PHASE_HEADING = re.compile(r"^phase\s+(.+?)\s+workstreams$", re.IGNORECASE)
_COVERAGE_COUNT = re.compile(
    r"\b(?P<label>audited|imported|excluded|unresolved)\s*[:=]\s*(?P<count>\d+)\b",
    re.IGNORECASE,
)


def _fold(text: str) -> str:
    return " ".join(text.lower().split())


def _sections(text: str) -> list[tuple[str, str]]:
    """Return level-three heading blocks as ``(heading, body)`` pairs."""
    matches = list(_H3.finditer(text))
    return [
        (
            match.group(1).strip(),
            text[
                match.end() : matches[index + 1].start()
                if index + 1 < len(matches)
                else len(text)
            ],
        )
        for index, match in enumerate(matches)
    ]


def _part_of_parents(fixture: dict) -> dict[str, list[str]]:
    """Return every direct outbound ``PART_OF`` target in fixture order."""
    outbound: dict[str, list[str]] = {}
    for edge in fixture["relationships"]:
        if edge["relationship_type"].upper() != "PART_OF":
            continue
        outbound.setdefault(edge["source_entity_id"], []).append(
            edge["target_entity_id"]
        )
    return outbound


def phase_ancestry_outcomes(fixture: dict) -> dict[str, dict]:
    """Classify each workstream's graph-derived canonical phase ancestry."""
    entities = fixture["entities"]
    master_id = fixture["master_plan_id"]
    outbound = _part_of_parents(fixture)
    phase_names = {
        item["id"]: item["name"] for item in entities[master_id]["snapshot"]["phases"]
    }
    source_session_tasks = {
        entity_id
        for entity_id, entity in entities.items()
        if entity.get("entity_type") == "task"
        and entity.get("snapshot", {}).get("source_lane_id")
    }
    source_session_plans = {
        target
        for task_id in source_session_tasks
        for target in outbound.get(task_id, [])
    }
    outcomes: dict[str, dict] = {}
    for entity_id, entity in entities.items():
        if (
            entity_id == master_id
            or entity["entity_type"] != "plan"
            or entity_id in source_session_plans
        ):
            continue
        phases: set[str] = set()
        seen = {entity_id}
        pending = list(outbound.get(entity_id, []))
        while pending:
            ancestor = pending.pop()
            if ancestor in seen:
                continue
            seen.add(ancestor)
            if ancestor in phase_names:
                phases.add(ancestor)
            pending.extend(outbound.get(ancestor, []))
        phase_ids = sorted(phases, key=lambda item: list(phase_names).index(item))
        outcome = (
            "unique"
            if len(phase_ids) == 1
            else "missing"
            if not phase_ids
            else "duplicate"
        )
        outcomes[entity_id] = {
            "outcome": outcome,
            "phase_ids": phase_ids,
            "phases": [phase_names[item] for item in phase_ids],
        }
    return outcomes


def structural_placements(fixture: dict) -> dict[str, str | None]:
    """Return the unique graph-derived phase, or ``None`` when non-unique."""
    return {
        entity_id: outcome["phases"][0] if outcome["outcome"] == "unique" else None
        for entity_id, outcome in phase_ancestry_outcomes(fixture).items()
    }


def task_ascent_outcomes(fixture: dict) -> dict[str, str]:
    """Return explicit defects at the first task ``PART_OF`` ascent."""
    outbound = _part_of_parents(fixture)
    return {
        entity_id: "missing" if not outbound.get(entity_id) else "duplicate"
        for entity_id, entity in fixture["entities"].items()
        if entity["entity_type"] == "task"
        and not entity.get("snapshot", {}).get("source_lane_id")
        and len(outbound.get(entity_id, [])) != 1
    }


def _phase_rows(text: str) -> list[tuple[str, str]]:
    rows: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = _PHASE_ROW.match(line)
        if match and match.group("phase").lower() != "canonical phase":
            rows.append((match.group("phase"), match.group("gate").strip()))
    return rows


def _section_for_phase(text: str, phase: str) -> str:
    wanted = _fold(f"Phase {phase} workstreams")
    for heading, body in _sections(text):
        if _fold(heading) == wanted:
            return body
    return ""


def _phase_sections(text: str) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for heading, body in _sections(text):
        match = _PHASE_HEADING.fullmatch(heading.strip())
        if match:
            result.append((match.group(1).strip(), body))
    return result


def _cross_phase_section(text: str) -> str:
    for heading, body in _sections(text):
        if "cross-phase" in _fold(heading):
            return body
    return ""


def _heading_section(text: str, needle: str) -> str:
    wanted = _fold(needle)
    for heading, body in _sections(text):
        if wanted in _fold(heading):
            return body
    return ""


def _entity_pattern(entity_id: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(entity_id)}(?![A-Za-z0-9_])")


def _entity_item(section: str, entity_id: str) -> str:
    """Return the bullet or table row that identifies one entity."""
    pattern = _entity_pattern(entity_id)
    lines = section.splitlines()
    for index, line in enumerate(lines):
        if not pattern.search(line):
            continue
        if line.lstrip().startswith("|"):
            return line
        start = index
        while start > 0 and not lines[start].lstrip().startswith("- "):
            start -= 1
        end = index + 1
        while end < len(lines) and not lines[end].lstrip().startswith(("- ", "|")):
            end += 1
        return "\n".join(lines[start:end])
    return ""


_SOURCE_STATE_DISPOSITION = {
    "active": "imported",
    "terminal_superseded": "excluded",
    "outside_requested_scope": "excluded",
    "ambiguous_binding": "unresolved",
}


def source_session_fixture_outcomes(fixture: dict, scenario: dict) -> dict:
    """Derive source-lane population and task/plan candidates from fixture graph."""
    entities = fixture["entities"]
    relationships = fixture.get("relationships", [])
    source_session_id = scenario["source_session_id"]
    failed: list[str] = []
    if source_session_id not in entities:
        failed.append("source_session_entity_exists")

    evidence_ids = {
        edge["source_entity_id"]
        for edge in relationships
        if edge["relationship_type"] == "PART_OF"
        and edge["target_entity_id"] == source_session_id
    }
    linked_task_ids = {
        edge["target_entity_id"]
        for edge in relationships
        if edge["relationship_type"] == "REFERS_TO"
        and edge["source_entity_id"] == source_session_id
    }
    lane_states: dict[str, set[str]] = {}
    lane_sources: dict[str, set[str]] = {}
    for entity_id in sorted(evidence_ids | linked_task_ids):
        entity = entities.get(entity_id)
        if entity is None:
            failed.append("source_evidence_entity_exists")
            continue
        snapshot = entity.get("snapshot", {})
        source_kind = snapshot.get("source_kind")
        records = list(snapshot.get("source_lanes", []))
        if snapshot.get("source_lane_id"):
            records.append(
                {
                    "lane_id": snapshot["source_lane_id"],
                    "state": snapshot.get("source_state"),
                }
            )
        for record in records:
            lane_id = record.get("lane_id")
            state = record.get("state")
            if not lane_id or not source_kind or state not in _SOURCE_STATE_DISPOSITION:
                failed.append("source_evidence_shape")
                continue
            lane_states.setdefault(lane_id, set()).add(state)
            lane_sources.setdefault(lane_id, set()).add(source_kind)

    observed_source_kinds = set().union(*lane_sources.values()) if lane_sources else set()
    required_source_kinds = set(scenario.get("required_source_kinds", []))
    if observed_source_kinds != required_source_kinds:
        failed.append("complete_source_union")

    expected_dispositions: dict[str, str] = {}
    for lane_id, states in lane_states.items():
        dispositions = {_SOURCE_STATE_DISPOSITION[state] for state in states}
        if len(dispositions) != 1:
            failed.append("consistent_source_lane_state")
        else:
            expected_dispositions[lane_id] = dispositions.pop()

    part_of: dict[str, list[str]] = {}
    for edge in relationships:
        source = edge["source_entity_id"]
        target = edge["target_entity_id"]
        if (
            edge["relationship_type"] == "PART_OF"
            and source in entities
            and target in entities
            and entities[source].get("entity_type") == "task"
            and entities[target].get("entity_type") == "plan"
        ):
            part_of.setdefault(source, []).append(target)
    bindings: dict[str, dict] = {}
    for lane_id in sorted(lane_states):
        task_ids = sorted(
            entity_id
            for entity_id, entity in entities.items()
            if entity.get("entity_type") == "task"
            and entity.get("snapshot", {}).get("source_lane_id") == lane_id
        )
        pairs = sorted(
            (task_id, plan_id)
            for task_id in task_ids
            for plan_id in part_of.get(task_id, [])
        )
        invalid_tasks = sorted(
            task_id for task_id in task_ids if len(part_of.get(task_id, [])) != 1
        )
        if invalid_tasks or not pairs:
            failed.append("fixture_graph_binding")
        outcome = "unique" if len(pairs) == 1 else "ambiguous" if pairs else "missing"
        bindings[lane_id] = {
            "outcome": outcome,
            "pairs": pairs,
            "invalid_tasks": invalid_tasks,
        }
        disposition = expected_dispositions.get(lane_id)
        if (outcome == "unique") != (disposition != "unresolved"):
            failed.append("fixture_ambiguity_matches_disposition")

    return {
        "failed": list(dict.fromkeys(failed)),
        "lane_ids": sorted(lane_states),
        "lane_sources": {
            lane_id: sorted(sources) for lane_id, sources in lane_sources.items()
        },
        "expected_dispositions": expected_dispositions,
        "bindings": bindings,
    }


def score_source_session_resume(text: str, fixture: dict, scenario: dict) -> dict:
    """Score whole-session coverage derived from distributed source evidence."""
    source_session_id = scenario["source_session_id"]
    fixture_outcomes = source_session_fixture_outcomes(fixture, scenario)
    lane_ids = fixture_outcomes["lane_ids"]
    bindings = fixture_outcomes["bindings"]
    expected_dispositions = fixture_outcomes["expected_dispositions"]
    all_pairs = [pair for binding in bindings.values() for pair in binding["pairs"]]
    task_ids = [task_id for task_id, _ in all_pairs]
    plan_ids = [plan_id for _, plan_id in all_pairs]
    folded = _fold(text)
    failed: list[str] = list(fixture_outcomes["failed"])

    session_pos = text.find(source_session_id)
    plan_positions = [pos for plan_id in plan_ids if (pos := text.find(plan_id)) >= 0]
    if session_pos < 0 or (plan_positions and session_pos > min(plan_positions)):
        failed.append("exact_source_session_first")

    coverage = _heading_section(text, "source-session coverage ledger")
    dispositions: dict[str, str] = {}
    duplicate_dispositions: list[str] = []
    omitted_lanes: list[str] = []
    duplicate_lanes: list[str] = []
    binding_failures: list[str] = []
    ambiguous_binding_failures: list[str] = []
    coverage_lines = coverage.splitlines()
    for lane_id in lane_ids:
        lane_pattern = _entity_pattern(lane_id)
        rows = [
            line
            for line in coverage_lines
            if line.lstrip().startswith("|") and lane_pattern.search(line)
        ]
        item = rows[0] if len(rows) == 1 else ""
        item_folded = _fold(item)
        matched = [
            disposition
            for disposition, markers in {
                "imported": ("imported",),
                "excluded": ("explicitly excluded",),
                "unresolved": ("unresolved",),
            }.items()
            if any(marker in item_folded for marker in markers)
        ]
        if not rows:
            omitted_lanes.append(lane_id)
        elif len(rows) != 1:
            duplicate_lanes.append(lane_id)
        elif len(matched) != 1:
            duplicate_dispositions.append(lane_id)
        else:
            dispositions[lane_id] = matched[0]
            if matched[0] != expected_dispositions.get(lane_id):
                failed.append("source_lane_disposition")

        if len(rows) != 1:
            continue
        candidate_pairs = bindings[lane_id]["pairs"]
        candidate_tasks = [task_id for task_id, _ in candidate_pairs]
        candidate_plans = [plan_id for _, plan_id in candidate_pairs]
        observed_tasks = [
            task_id for task_id in task_ids if _entity_pattern(task_id).search(item)
        ]
        observed_plans = [
            plan_id for plan_id in plan_ids if _entity_pattern(plan_id).search(item)
        ]
        if bindings[lane_id]["outcome"] == "unique":
            if observed_tasks != candidate_tasks or observed_plans != candidate_plans:
                binding_failures.append(lane_id)
        elif (
            "candidate" not in item_folded
            or sorted(observed_tasks) != sorted(candidate_tasks)
            or sorted(observed_plans) != sorted(candidate_plans)
        ):
            ambiguous_binding_failures.append(lane_id)

    if omitted_lanes:
        failed.append("all_source_lanes_accounted_for")
    if duplicate_lanes:
        failed.append("exactly_one_ledger_row_per_lane")
    if duplicate_dispositions:
        failed.append("exactly_one_disposition_per_lane")
    if binding_failures:
        failed.append("canonical_task_plan_binding")
    if ambiguous_binding_failures:
        failed.append("ambiguous_lane_must_not_assert_canonical_binding")

    reported_counts = {
        match.group("label").lower(): int(match.group("count"))
        for match in _COVERAGE_COUNT.finditer(text)
    }
    actual_counts = {
        "audited": len(lane_ids),
        "imported": sum(value == "imported" for value in dispositions.values()),
        "excluded": sum(value == "excluded" for value in dispositions.values()),
        "unresolved": sum(value == "unresolved" for value in dispositions.values()),
    }
    if set(reported_counts) != set(actual_counts) or any(
        reported_counts.get(label) != count for label, count in actual_counts.items()
    ):
        failed.append("all_four_coverage_counts")
    if reported_counts.get("audited") != reported_counts.get(
        "imported", -1
    ) + reported_counts.get("excluded", -1) + reported_counts.get("unresolved", -1):
        failed.append("coverage_balance_equation")

    completeness_claimed = any(
        marker in folded
        for marker in (
            "comprehensively resumed",
            "complete resume",
            "coverage is complete",
            "all source lanes are accounted for",
        )
    )
    if omitted_lanes and completeness_claimed:
        failed.append("no_completeness_claim_with_omission")

    return {
        "outcome": "pass" if not failed else "fail",
        "failed": list(dict.fromkeys(failed)),
        "source_session_id": source_session_id,
        "lane_sources": fixture_outcomes["lane_sources"],
        "dispositions": dispositions,
        "omitted_lanes": omitted_lanes,
        "duplicate_lanes": duplicate_lanes,
        "duplicate_dispositions": duplicate_dispositions,
        "binding_failures": binding_failures,
        "ambiguous_binding_failures": ambiguous_binding_failures,
        "reported_counts": reported_counts,
        "actual_counts": actual_counts,
    }


def score_report(
    text: str,
    fixture: dict,
    invoked_skill: str = "continue-session",
) -> dict:
    """Score one invoked skill's final report against fixture-observable effects."""
    if invoked_skill not in {"continue-session", "digest"}:
        raise ValueError(f"unsupported invoked skill: {invoked_skill}")

    entities = fixture["entities"]
    master = entities[fixture["master_plan_id"]]["snapshot"]
    expected_rows = [
        (item["name"], item["exit_gate_state"]) for item in master["phases"]
    ]
    actual_rows = _phase_rows(text)
    ancestry = phase_ancestry_outcomes(fixture)
    placements = structural_placements(fixture)
    task_outcomes = task_ascent_outcomes(fixture)
    folded = _fold(text)
    failed: list[str] = []

    master_pos = text.find(fixture["master_plan_id"])
    mechanics_positions = [
        pos for entity_id in ancestry if (pos := text.find(entity_id)) >= 0
    ]
    task_mechanics_pos = text.lower().find("### task mechanics")
    if task_mechanics_pos >= 0:
        mechanics_positions.append(task_mechanics_pos)
    first_phase_row = next(
        (
            text.find(line)
            for line in text.splitlines()
            if _PHASE_ROW.match(line) and "canonical phase" not in line.lower()
        ),
        -1,
    )
    if first_phase_row >= 0:
        mechanics_positions.append(first_phase_row)
    if master_pos < 0 or (
        mechanics_positions and master_pos > min(mechanics_positions)
    ):
        failed.append("master_plan_first")

    if actual_rows != expected_rows:
        failed.append("canonical_phases_in_record_order")

    mechanics_pos = text.lower().find("### task mechanics")
    row_positions = [
        text.find(line)
        for line in text.splitlines()
        if _PHASE_ROW.match(line) and "canonical phase" not in line.lower()
    ]
    if (
        mechanics_pos < 0
        or len(row_positions) != len(expected_rows)
        or any(pos < 0 or pos > mechanics_pos for pos in row_positions)
    ):
        failed.append("exit_gates_before_task_mechanics")

    phase_sections = _phase_sections(text)
    for entity_id, outcome in ancestry.items():
        if outcome["outcome"] != "unique":
            continue
        expected_phase = outcome["phases"][0]
        pattern = _entity_pattern(entity_id)
        occurrences = [
            phase for phase, body in phase_sections for _ in pattern.finditer(body)
        ]
        correct_block = _entity_item(
            _section_for_phase(text, expected_phase), entity_id
        )
        if expected_phase.lower() not in {item.lower() for item in occurrences} or not (
            "structurally bound" in _fold(correct_block)
            and "part_of" in _fold(correct_block)
        ):
            failed.append("structural_phase_binding")
        if len(occurrences) != 1 or occurrences[0].lower() != expected_phase.lower():
            failed.append("unique_graph_phase_placement")

    cross_phase = _cross_phase_section(text)
    for entity_id, outcome in ancestry.items():
        if outcome["outcome"] == "unique":
            continue
        item = _entity_item(cross_phase, entity_id)
        item_folded = _fold(item)
        wrongly_phased = any(
            _entity_pattern(entity_id).search(body) for _, body in phase_sections
        )
        if not item or "cross-phase prerequisite" not in item_folded or wrongly_phased:
            failed.append("unbound_work_is_cross_phase")
        ancestry_marker = f"{outcome['outcome']} phase ancestry"
        derivation_marker = (
            "not structurally derivable"
            if outcome["outcome"] == "missing"
            else "not uniquely structurally derivable"
        )
        phase_names_present = all(
            re.search(rf"\b{re.escape(phase)}\b", item, re.IGNORECASE)
            for phase in outcome["phases"]
        )
        if (
            ancestry_marker not in item_folded
            or derivation_marker not in item_folded
            or not phase_names_present
        ):
            failed.append("explicit_phase_ancestry_outcomes")

    ledger = _heading_section(text, "planning resume ledger")
    for entity_id, outcome in task_outcomes.items():
        row = _entity_item(ledger, entity_id)
        if f"{outcome} ascent" not in _fold(row):
            failed.append("explicit_task_ascent_outcomes")
            break

    if not (
        "reconcile-planning" in folded
        and "retrospective" in folded
        and "not the real-time source of truth" in folded
    ):
        failed.append("reconcile_is_retrospective")

    return {
        "outcome": "pass" if not failed else "fail",
        "failed": list(dict.fromkeys(failed)),
        "structural_placements": placements,
        "phase_ancestry_outcomes": ancestry,
        "task_ascent_outcomes": task_outcomes,
        "expected_phase_rows": expected_rows,
        "actual_phase_rows": actual_rows,
    }
