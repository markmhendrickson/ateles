"""Observable-output checks for the planning-spine agentic eval.

The evaluator reads the final report and the fixture graph. It never scores a
skill body for containing policy phrases: the observable report must exhibit
the master-plan-first structure the policy requires.
"""

from __future__ import annotations

import re
from datetime import datetime

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


def _source_time(value: object) -> datetime | None:
    """Parse an evidence timestamp only when it is explicit and timezone-aware."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def source_session_fixture_outcomes(fixture: dict, scenario: dict) -> dict:
    """Derive source-lane population and task/plan candidates from fixture graph."""
    entities = fixture["entities"]
    relationships = fixture.get("relationships", [])
    lookup_key = scenario.get("source_session_lookup_key")
    if lookup_key:
        candidates = sorted(
            entity_id
            for entity_id, entity in entities.items()
            if entity.get("entity_type") == "conversation"
            and entity.get("snapshot", {}).get("lookup_key") == lookup_key
        )
        return {
            "resolution": "ambiguous" if len(candidates) > 1 else "not_found",
            "source_session_id": None,
            "candidate_ids": candidates,
            "failed": [],
            "lane_ids": [],
            "lane_sources": {},
            "lane_evidence_ids": {},
            "latest_states": {},
            "expected_dispositions": {},
            "bindings": {},
            "unreadable_evidence_ids": [],
            "unreadable_lane_ids": [],
            "superseded_shell_ids": [],
            "superseded_artifact_ids": [],
        }

    source_session_id = scenario["source_session_id"]
    if (
        source_session_id not in entities
        or entities[source_session_id].get("entity_type") != "conversation"
    ):
        return {
            "resolution": "not_found",
            "source_session_id": source_session_id,
            "candidate_ids": [],
            "failed": [],
            "lane_ids": [],
            "lane_sources": {},
            "lane_evidence_ids": {},
            "latest_states": {},
            "expected_dispositions": {},
            "bindings": {},
            "unreadable_evidence_ids": [],
            "unreadable_lane_ids": [],
            "superseded_shell_ids": [],
            "superseded_artifact_ids": [],
        }

    failed: list[str] = []
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
    unreadable_evidence_ids = sorted(
        entity_id
        for entity_id in evidence_ids
        if entities.get(entity_id, {}).get("snapshot", {}).get("readable") is False
    )
    unreadable_lane_ids = sorted(
        {
            lane_id
            for entity_id in unreadable_evidence_ids
            for lane_id in entities[entity_id]["snapshot"].get("affected_lane_ids", [])
        }
    )
    lane_observations: dict[str, list[tuple[datetime, str, str, str]]] = {}
    lane_sources: dict[str, set[str]] = {}
    lane_evidence_ids: dict[str, set[str]] = {}
    for entity_id in sorted(evidence_ids | linked_task_ids):
        if entity_id in unreadable_evidence_ids:
            continue
        entity = entities.get(entity_id)
        if entity is None:
            failed.append("source_evidence_entity_exists")
            continue
        snapshot = entity.get("snapshot", {})
        source_kind = snapshot.get("source_kind")
        observed_at = snapshot.get("observed_at", "")
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
            record_observed_at = record.get("observed_at", observed_at)
            parsed_observed_at = _source_time(record_observed_at)
            if parsed_observed_at is None:
                failed.append("source_evidence_chronology")
                continue
            lane_observations.setdefault(lane_id, []).append(
                (parsed_observed_at, entity_id, source_kind, state)
            )
            lane_sources.setdefault(lane_id, set()).add(source_kind)
            lane_evidence_ids.setdefault(lane_id, set()).add(entity_id)

    if not lane_observations and not unreadable_evidence_ids:
        return {
            "resolution": "empty",
            "source_session_id": source_session_id,
            "candidate_ids": [],
            "failed": list(dict.fromkeys(failed)),
            "lane_ids": [],
            "lane_sources": {},
            "lane_evidence_ids": {},
            "latest_states": {},
            "expected_dispositions": {},
            "bindings": {},
            "unreadable_evidence_ids": [],
            "unreadable_lane_ids": [],
            "superseded_shell_ids": [],
            "superseded_artifact_ids": [],
        }

    observed_source_kinds = (
        set().union(*lane_sources.values()) if lane_sources else set()
    )
    required_source_kinds = set(scenario.get("required_source_kinds", []))
    if required_source_kinds and observed_source_kinds != required_source_kinds:
        failed.append("source_evidence_union")

    expected_dispositions: dict[str, str] = {}
    latest_states: dict[str, str] = {}
    for lane_id, observations in lane_observations.items():
        latest_time = max(item[0] for item in observations)
        newest_states = {item[3] for item in observations if item[0] == latest_time}
        if len(newest_states) != 1:
            failed.append("latest_state_reconciliation")
            continue
        latest_state = newest_states.pop()
        latest_states[lane_id] = latest_state
        expected_dispositions[lane_id] = _SOURCE_STATE_DISPOSITION[latest_state]

    if unreadable_evidence_ids:
        for lane_id in unreadable_lane_ids:
            expected_dispositions[lane_id] = "unresolved"
            latest_states[lane_id] = "unknown"
            lane_evidence_ids.setdefault(lane_id, set()).update(
                entity_id
                for entity_id in unreadable_evidence_ids
                if lane_id
                in entities[entity_id]["snapshot"].get("affected_lane_ids", [])
            )
        return {
            "resolution": "unreadable",
            "source_session_id": source_session_id,
            "candidate_ids": [],
            "failed": list(dict.fromkeys(failed)),
            "lane_ids": sorted(expected_dispositions),
            "lane_sources": {
                lane_id: sorted(sources) for lane_id, sources in lane_sources.items()
            },
            "lane_evidence_ids": {
                lane_id: sorted(ids) for lane_id, ids in lane_evidence_ids.items()
            },
            "latest_states": latest_states,
            "expected_dispositions": expected_dispositions,
            "bindings": {},
            "unreadable_evidence_ids": unreadable_evidence_ids,
            "unreadable_lane_ids": unreadable_lane_ids,
            "superseded_shell_ids": [],
            "superseded_artifact_ids": [],
        }

    part_of: dict[str, list[str]] = {}
    aggregate_invalid: set[str] = set()
    for source, entity in entities.items():
        if entity.get("entity_type") == "task":
            plans, invalid = _task_plan_candidates(fixture, source)
            part_of[source] = plans
            if invalid:
                aggregate_invalid.add(source)
    bindings: dict[str, dict] = {}
    for lane_id in sorted(lane_observations):
        task_ids = sorted(
            entity_id
            for entity_id, entity in entities.items()
            if entity.get("entity_type") == "task"
            and entity.get("snapshot", {}).get("source_session_id") == source_session_id
            and entity.get("snapshot", {}).get("source_lane_id") == lane_id
        )
        pairs = sorted(
            (task_id, plan_id)
            for task_id in task_ids
            for plan_id in part_of.get(task_id, [])
        )
        invalid_tasks = []
        for task_id in task_ids:
            task_parents = part_of.get(task_id, [])
            if len(task_parents) != 1 or task_id in aggregate_invalid:
                invalid_tasks.append(task_id)
                continue
            plan_parents = [
                parent
                for parent in _part_of_parents(fixture).get(task_parents[0], [])
                if entities.get(parent, {}).get("entity_type")
                in {"plan", "project", "strategy"}
            ]
            if len(plan_parents) != 1:
                invalid_tasks.append(task_id)
        if invalid_tasks or not pairs:
            failed.append("graph_derived_binding")
        outcome = "unique" if len(pairs) == 1 else "ambiguous" if pairs else "missing"
        bindings[lane_id] = {
            "outcome": outcome,
            "pairs": pairs,
            "invalid_tasks": invalid_tasks,
        }
        disposition = expected_dispositions.get(lane_id)
        if (outcome == "unique") != (disposition != "unresolved"):
            failed.append("fixture_ambiguity_matches_disposition")

    superseded_shell_ids = sorted(
        entity_id
        for entity_id, entity in entities.items()
        if entity_id != source_session_id
        and entity.get("entity_type") == "conversation"
        and entity.get("snapshot", {}).get("source_session_id") == source_session_id
        and entity.get("snapshot", {}).get("status") == "superseded"
    )
    superseded_edges = {
        (edge["source_entity_id"], edge["target_entity_id"])
        for edge in relationships
        if edge["relationship_type"] == "SUPERSEDED_BY"
    }
    for shell_id in superseded_shell_ids:
        shell = entities[shell_id]["snapshot"]
        if (
            shell.get("canonical_replacement_id") != source_session_id
            or (shell_id, source_session_id) not in superseded_edges
        ):
            failed.append("superseded_shell_rejected")
    superseded_task_ids = sorted(
        entity_id
        for entity_id, entity in entities.items()
        if entity.get("entity_type") == "task"
        and entity.get("snapshot", {}).get("source_session_id") in superseded_shell_ids
    )
    superseded_plan_ids = sorted(
        plan_id
        for task_id in superseded_task_ids
        for plan_id in part_of.get(task_id, [])
    )

    return {
        "resolution": "bound",
        "source_session_id": source_session_id,
        "candidate_ids": [],
        "failed": list(dict.fromkeys(failed)),
        "lane_ids": sorted(lane_observations),
        "lane_sources": {
            lane_id: sorted(sources) for lane_id, sources in lane_sources.items()
        },
        "lane_evidence_ids": {
            lane_id: sorted(ids) for lane_id, ids in lane_evidence_ids.items()
        },
        "latest_states": latest_states,
        "expected_dispositions": expected_dispositions,
        "bindings": bindings,
        "unreadable_evidence_ids": [],
        "unreadable_lane_ids": [],
        "superseded_shell_ids": superseded_shell_ids,
        "superseded_artifact_ids": [
            *superseded_shell_ids,
            *superseded_task_ids,
            *superseded_plan_ids,
        ],
    }


def _halted_source_session_score(text: str, outcomes: dict) -> dict:
    """Score observable recovery contracts before plan binding can begin."""
    folded = _fold(text)
    resolution = outcomes["resolution"]
    failed: list[str] = list(outcomes.get("failed", []))
    unsafe_plan_binding = any(
        marker in folded
        for marker in ("bound plan", "plan bound", "selected plan", "resumed plan")
    )
    unsafe_domain_action = any(
        marker in folded
        for marker in (
            "domain action completed",
            "implemented the change",
            "deployment completed",
        )
    )
    explicit_no_domain_action = any(
        marker in folded
        for marker in ("no domain action", "no state-dependent domain action")
    )
    explicit_no_completeness = any(
        marker in folded
        for marker in (
            "no completeness claim",
            "coverage is not complete",
            "cannot claim completeness",
        )
    )
    unsafe_completeness_claim = any(
        marker in folded
        for marker in (
            "coverage is complete",
            "comprehensively resumed",
            "all work was resumed",
        )
    )

    if resolution == "not_found":
        checked = sum(
            marker in folded
            for marker in (
                "conversation",
                "transcript",
                "workboard",
                "terminal handoff",
            )
        )
        if (
            checked < 2
            or "checked" not in folded
            or outcomes["source_session_id"] not in text
        ):
            failed.append("not_found_checked_surfaces")
        if not any(
            marker in folded
            for marker in (
                "stable session id",
                "conversation id",
                "exact transcript path",
                "restore",
            )
        ):
            failed.append("not_found_recovery_action")
        if "stop" not in folded or "before plan binding" not in folded:
            failed.append("pre_action_stop")
    elif resolution == "ambiguous":
        for candidate_id in outcomes["candidate_ids"]:
            snapshot = outcomes["entities"][candidate_id]["snapshot"]
            if candidate_id not in text or not any(
                str(snapshot.get(field, "")) in text
                for field in ("started_at", "harness", "repository")
                if snapshot.get(field)
            ):
                failed.append("ambiguous_bounded_candidates")
        if "stop" not in folded or "before plan binding" not in folded:
            failed.append("pre_action_stop")
    elif resolution == "empty":
        expected = {"audited": 0, "imported": 0, "excluded": 0, "unresolved": 0}
        counts = {
            match.group("label").lower(): int(match.group("count"))
            for match in _COVERAGE_COUNT.finditer(text)
        }
        has_lane_row = re.search(r"^\s*\|\s*`?lane_", text, re.MULTILINE) is not None
        if (
            counts != expected
            or "source-session coverage ledger" not in folded
            or has_lane_row
        ):
            failed.append("explicit_empty_outcome")
        if "no work was resumed" not in folded:
            failed.append("explicit_empty_outcome")
    elif resolution == "unreadable":
        if not all(
            entity_id in text for entity_id in outcomes["unreadable_evidence_ids"]
        ):
            failed.append("unreadable_evidence_named")
        if not all(lane_id in text for lane_id in outcomes["unreadable_lane_ids"]):
            failed.append("unreadable_lanes_unresolved")
        counts = {
            match.group("label").lower(): int(match.group("count"))
            for match in _COVERAGE_COUNT.finditer(text)
        }
        expected_dispositions = outcomes["expected_dispositions"]
        expected = {
            "audited": len(expected_dispositions),
            "imported": sum(
                value == "imported" for value in expected_dispositions.values()
            ),
            "excluded": sum(
                value == "excluded" for value in expected_dispositions.values()
            ),
            "unresolved": sum(
                value == "unresolved" for value in expected_dispositions.values()
            ),
        }
        if counts != expected or "unresolved" not in folded:
            failed.append("unreadable_lanes_unresolved")
        if (
            "retry" not in folded
            or not any(marker in folded for marker in ("bounded", "at most", "twice"))
            or not any(marker in folded for marker in ("checkpoint", "escalat"))
        ):
            failed.append("unreadable_retry_escalation")
        if "treated as empty" in folded:
            failed.append("unreadable_not_empty_or_complete")

    if unsafe_plan_binding:
        failed.append("pre_action_stop")
    if unsafe_domain_action or not explicit_no_domain_action:
        failed.append("halted_state_no_domain_action")
    if unsafe_completeness_claim or (
        resolution == "unreadable" and not explicit_no_completeness
    ):
        failed.append("halted_state_no_completeness_claim")
    return {
        "outcome": "pass" if not failed else "fail",
        "failed": list(dict.fromkeys(failed)),
        "resolution": resolution,
        "source_session_id": outcomes.get("source_session_id"),
        "candidate_ids": outcomes.get("candidate_ids", []),
    }


def score_source_session_resume(text: str, fixture: dict, scenario: dict) -> dict:
    """Score whole-session coverage derived from distributed source evidence."""
    fixture_outcomes = source_session_fixture_outcomes(fixture, scenario)
    fixture_outcomes["entities"] = fixture["entities"]
    if fixture_outcomes["resolution"] != "bound":
        return _halted_source_session_score(text, fixture_outcomes)

    source_session_id = fixture_outcomes["source_session_id"]
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
    evidence_failures: list[str] = []
    latest_state_failures: list[str] = []
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
        if not all(
            evidence_id in item
            for evidence_id in fixture_outcomes["lane_evidence_ids"][lane_id]
        ):
            evidence_failures.append(lane_id)
        latest_state = fixture_outcomes["latest_states"].get(lane_id, "")
        if (
            latest_state not in item_folded
            and latest_state.replace("_", " ") not in item_folded
        ):
            latest_state_failures.append(lane_id)
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
        failed.append("source_evidence_union")
    if duplicate_lanes:
        failed.append("exactly_one_ledger_row_per_lane")
    if duplicate_dispositions:
        failed.append("exactly_one_disposition_per_lane")
    if binding_failures:
        failed.append("canonical_task_plan_binding")
        failed.append("graph_derived_binding")
    if ambiguous_binding_failures:
        failed.append("ambiguous_lane_must_not_assert_canonical_binding")
    if evidence_failures:
        failed.append("source_evidence_union")
    if latest_state_failures:
        failed.append("latest_state_reconciliation")
    if any(
        _entity_pattern(entity_id).search(coverage)
        for entity_id in fixture_outcomes["superseded_artifact_ids"]
    ):
        failed.append("superseded_shell_rejected")

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
        "evidence_failures": evidence_failures,
        "latest_state_failures": latest_state_failures,
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


# Family checks consume persisted fixture evidence as well as the report. They
# qualify this evaluator, never native execution, authorization or installation.
def planning_chain(state: dict, task_id: str) -> dict:
    parents = _part_of_parents(state)
    entities = state["entities"]
    chain, seen = [], set()
    node = task_id
    while node in entities and node not in seen:
        chain.append(node)
        seen.add(node)
        targets = list(dict.fromkeys(parents.get(node, [])))
        if len(targets) > 1:
            return {"outcome": "duplicate", "chain": chain, "targets": targets}
        if not targets:
            outcome = "missing" if entities[node]["entity_type"] == "task" else "unique"
            return {"outcome": outcome, "chain": chain, "targets": []}
        node = targets[0]
    return {
        "outcome": "cycle" if node in seen else "missing",
        "chain": chain,
        "targets": [node],
    }


def _task_plan_candidates(fixture: dict, task_id: str) -> tuple[list[str], bool]:
    """Resolve through aggregate tasks without weakening ambiguous source binds."""
    parents = _part_of_parents(fixture)
    pending, seen, plans = [task_id], set(), set()
    invalid = False
    while pending:
        node = pending.pop()
        if node in seen:
            invalid = True
            continue
        seen.add(node)
        ent = fixture["entities"].get(node, {})
        if ent.get("entity_type") == "plan":
            plans.add(node)
            continue
        targets = parents.get(node, [])
        if ent.get("entity_type") != "task" or len(targets) != 1:
            invalid = True
        pending.extend(targets)
    return sorted(plans), invalid


AUDIT_DISPOSITIONS = {
    "owned",
    "continuing",
    "result_awaiting_integration",
    "fixture_action_complete",
    "awaiting_input",
    "unresolved",
}
AUDIT_VERDICTS = {"confirmed", "refuted", "unverifiable", "blocked_on_tooling"}


def audit_validation_errors(value: object) -> list[str]:
    """Validate the entire worker output before hashing/indexing any value."""
    errors = []

    def expect(path, ok, description):
        if not ok:
            errors.append(path + ": expected " + description)

    def identifier(path, item, nullable=False):
        expect(
            path,
            (nullable and item is None)
            or (isinstance(item, str) and bool(item.strip())),
            "nonempty string" + (" or null" if nullable else ""),
        )

    def strings(path, item):
        expect(path, isinstance(item, list), "array of nonempty strings")
        if isinstance(item, list):
            for i, entry in enumerate(item):
                identifier(f"{path}[{i}]", entry)

    if not isinstance(value, dict):
        return ["audit: expected object"]
    identifier("audit.source_session_id", value.get("source_session_id"))
    schemas = {
        "tasks": ("task_id",),
        "questions": ("question_id", "task_id", "checkpoint_id"),
        "verification": ("artifact",),
    }
    for group, ids in schemas.items():
        rows = value.get(group)
        expect("audit." + group, isinstance(rows, list), "array of objects")
        if not isinstance(rows, list):
            continue
        for i, row in enumerate(rows):
            path = f"audit.{group}[{i}]"
            expect(path, isinstance(row, dict), "object")
            if not isinstance(row, dict):
                continue
            for key in ids:
                identifier(path + "." + key, row.get(key))
            if group == "tasks":
                for key in ("session_id", "parent_session_id"):
                    expect(path + "." + key, key in row, "present nullable identifier")
                    identifier(path + "." + key, row.get(key), True)
                strings(path + ".plan_chain", row.get("plan_chain"))
                expect(
                    path + ".disposition",
                    isinstance(row.get("disposition"), str)
                    and row["disposition"] in AUDIT_DISPOSITIONS,
                    "declared disposition",
                )
                if "lease_held" in row:
                    expect(
                        path + ".lease_held", type(row["lease_held"]) is bool, "boolean"
                    )
            elif group == "questions":
                expect(
                    path + ".source_answer_id",
                    "source_answer_id" in row,
                    "present nullable answer identifier",
                )
                expect(
                    path + ".state",
                    isinstance(row.get("state"), str)
                    and row["state"] in {"open", "answered"},
                    "open or answered",
                )
                identifier(
                    path + ".source_answer_id",
                    row.get("source_answer_id"),
                    row.get("state") == "open",
                )
                if row.get("state") == "open":
                    expect(
                        path + ".source_answer_id",
                        row.get("source_answer_id") is None,
                        "null for an open question",
                    )
            else:
                expect(
                    path + ".verdict",
                    isinstance(row.get("verdict"), str)
                    and row["verdict"] in AUDIT_VERDICTS,
                    "declared verification verdict",
                )
                expect(
                    path + ".mutability",
                    isinstance(row.get("mutability"), str)
                    and row["mutability"] in {"immutable", "perishable"},
                    "immutable or perishable",
                )
    coverage = value.get("coverage")
    expect("audit.coverage", isinstance(coverage, dict), "object")
    if isinstance(coverage, dict):
        expect(
            "audit.coverage.complete", type(coverage.get("complete")) is bool, "boolean"
        )
        strings("audit.coverage.unresolved_sources", coverage.get("unresolved_sources"))
    card = value.get("scorecard")
    expect("audit.scorecard", isinstance(card, dict), "object")
    if isinstance(card, dict):
        for key in ["claims", *sorted(AUDIT_VERDICTS)]:
            expect(
                "audit.scorecard." + key,
                type(card.get(key)) is int and card[key] >= 0,
                "nonnegative integer",
            )
    if "successor" in value:
        successor = value["successor"]
        expect("audit.successor", isinstance(successor, dict), "object")
        if isinstance(successor, dict):
            for key in ("session_id", "predecessor_id"):
                identifier("audit.successor." + key, successor.get(key))
            expect(
                "audit.successor.qualified",
                type(successor.get("qualified")) is bool,
                "boolean",
            )
    if "credited_event_ids" in value:
        strings("audit.credited_event_ids", value["credited_event_ids"])
    return errors


# Engineering bounds for this synthetic evaluator, not worker authority limits.
MAX_INPUT_BYTES = 1_048_576
MAX_INPUT_DEPTH = 64
MAX_INPUT_NODES = 1_000_000


def decode_input_json(text: str, path: str, errors: list[str]):
    """Contain bounded malformed JSON before decoding can exhaust recursion."""
    import json

    if not isinstance(text, str):
        errors.append(path + ": expected JSON text")
        return None
    try:
        if len(text) > MAX_INPUT_BYTES or len(text.encode("utf-8")) > MAX_INPUT_BYTES:
            errors.append(f"{path}: JSON input exceeds {MAX_INPUT_BYTES} bytes")
            return None
    except UnicodeError:
        errors.append(path + ": invalid Unicode in JSON input")
        return None
    depth, quoted, escaped = 0, False, False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_INPUT_DEPTH:
                errors.append(f"{path}: JSON nesting exceeds {MAX_INPUT_DEPTH} levels")
                return None
        elif char in "]}":
            depth -= 1
    try:
        return json.loads(text)
    except (ValueError, TypeError, RecursionError, MemoryError, OverflowError):
        errors.append(path + ": invalid JSON or unsupported decoder resource shape")
        return None


def _structure_errors(value: object, path: str) -> list[str]:
    # Iterative traversal protects direct scorer callers as well as decoded data.
    pending, visited = [(value, path, 0)], 0
    while pending:
        item, here, depth = pending.pop()
        visited += 1
        if depth > MAX_INPUT_DEPTH or visited > MAX_INPUT_NODES:
            return [here + ": unsupported input depth or collection size"]
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    return [here + ": object keys must be strings"]
                pending.append((child, here + "." + key, depth + 1))
        elif isinstance(item, list):
            pending.extend(
                (child, f"{here}[{i}]", depth + 1) for i, child in enumerate(item)
            )
        elif item is not None and not isinstance(item, (str, int, float, bool)):
            return [here + ": expected JSON-compatible value"]
    return []


def _audit_object(text: str, errors: list[str] | None = None) -> dict | None:
    invalid = []
    if not isinstance(text, str) or len(text) > MAX_INPUT_BYTES:
        invalid.append("audit: expected bounded JSON report text")
        value = None
    else:
        candidates = re.findall(r"```(?:json)?\s*\n(.*?)\n```", text, re.DOTALL)
        value = decode_input_json(
            candidates[-1] if candidates else text.strip(), "audit", invalid
        )
    if not invalid:
        invalid.extend(_structure_errors(value, "audit"))
    if not invalid:
        invalid.extend(audit_validation_errors(value))
    if errors is not None:
        errors.extend(invalid)
    return None if invalid else value


def family_evidence_errors(evidence: object) -> list[str]:
    """Validate every consumed source shape before iteration or key lookup.

    This is the existing synthetic fixture contract, not a production schema.
    Qualification flag values are deliberately left to the strict affirmative
    predicates: unknown/malformed flags remain observable negative test inputs.
    """
    errors = _structure_errors(evidence, "evidence")
    if errors:
        return errors

    def expect(path, ok, kind):
        if not ok and len(errors) < 128:
            errors.append(path + ": expected " + kind)

    def object_(value, path):
        expect(path, isinstance(value, dict), "object")
        return value if isinstance(value, dict) else {}

    def array(value, path):
        expect(path, isinstance(value, list), "array")
        return value if isinstance(value, list) else []

    def identifier(value, path, nullable=False):
        expect(
            path,
            (nullable and value is None)
            or (isinstance(value, str) and bool(value.strip())),
            "nonempty string" + (" or null" if nullable else ""),
        )

    def ids(row, path, keys, nullable=False):
        for key in keys:
            if key in row:
                identifier(row[key], path + "." + key, nullable)

    def objects(value, path):
        return [
            object_(item, f"{path}[{i}]") for i, item in enumerate(array(value, path))
        ]

    def edges(value, path):
        for i, edge in enumerate(objects(value, path)):
            for key in ("source_entity_id", "target_entity_id", "relationship_type"):
                identifier(edge.get(key), f"{path}[{i}].{key}")

    def snapshot(value, kind, path):
        row = object_(value, path)
        ids(
            row,
            path,
            (
                "executor",
                "owner",
                "owner_session_id",
                "source_session_id",
                "task_id",
                "task_entity_id",
                "question_id",
                "checkpoint_id",
                "recipient_session_id",
                "conversation_id",
            ),
            True,
        )
        for key in ("status", "state", "sender_kind", "observed_at"):
            if key in row:
                identifier(row[key], path + "." + key)
        if kind == "session_digest":
            for key in ("tasks_claimed", "artifacts"):
                objects(row.get(key), path + "." + key)
            for key in ("session_key", "schema_version", "harness"):
                if key in row:
                    identifier(row[key], path + "." + key)
            if "lineage_files" in row:
                for i, item in enumerate(
                    array(row["lineage_files"], path + ".lineage_files")
                ):
                    identifier(item, f"{path}.lineage_files[{i}]")
        return row

    def entities(value, path):
        for eid, entity in object_(value, path).items():
            identifier(eid, path + ".key")
            row = object_(entity, path + "." + eid)
            identifier(row.get("entity_type"), path + "." + eid + ".entity_type")
            if known_types:
                expect(
                    path + "." + eid + ".entity_type",
                    isinstance(row.get("entity_type"), str)
                    and row["entity_type"] in known_types,
                    "registered fixture entity type",
                )
            snapshot(
                row.get("snapshot"),
                row.get("entity_type"),
                path + "." + eid + ".snapshot",
            )

    def session(value, path):
        row = object_(value, path)
        ids(row, path, ("parent_session_id", "predecessor_id"), True)
        if "source_kind" in row:
            expect(
                path + ".source_kind",
                isinstance(row["source_kind"], str)
                and row["source_kind"] in {"interactive", "subAgent"},
                "interactive or subAgent",
            )
        if "items" in row:
            for i, item in enumerate(objects(row["items"], path + ".items")):
                ids(
                    item,
                    f"{path}.items[{i}]",
                    (
                        "id",
                        "entity_id",
                        "task_id",
                        "session_id",
                        "delegated_session_id",
                    ),
                    True,
                )

    def receipts(value, path):
        for i, row in enumerate(objects(value, path)):
            here = f"{path}[{i}]"
            ids(
                row,
                here,
                (
                    "receipt_id",
                    "answer_id",
                    "question_id",
                    "task_id",
                    "checkpoint_id",
                    "recipient_session_id",
                    "consumer_session_id",
                ),
                True,
            )
            expect(
                here + ".status",
                isinstance(row.get("status"), str)
                and row["status"] in {"queued", "consumed"},
                "queued or consumed",
            )

    def state(value, path):
        row = object_(value, path)
        entities(row.get("entities"), path + ".entities")
        edges(row.get("relationships"), path + ".relationships")
        for sid, child in object_(row.get("sessions", {}), path + ".sessions").items():
            identifier(sid, path + ".sessions.key")
            session(child, path + ".sessions." + sid)
        for key in ("effects", "watch"):
            if key in row:
                object_(row[key], path + "." + key)
        if "answer_receipts" in row:
            mapping = object_(row["answer_receipts"], path + ".answer_receipts")
            receipts(list(mapping.values()), path + ".answer_receipts.values")
        for tid, activity in object_(
            row.get("activity", {}), path + ".activity"
        ).items():
            identifier(tid, path + ".activity.key")
            entry = object_(activity, path + ".activity." + tid)
            for key in ("artifact", "sha256", "task_id", "session_id", "state"):
                identifier(entry.get(key), path + ".activity." + tid + "." + key)

    root = object_(evidence, "evidence")
    registered = root.get("registered_entity_types", [])
    expect(
        "registered_entity_types",
        isinstance(registered, list)
        and all(isinstance(item, str) for item in registered),
        "array of type names",
    )
    known_types = (
        set(registered)
        if isinstance(registered, list)
        and all(isinstance(item, str) for item in registered)
        else set()
    )
    for key in ("before_state", "after_state"):
        state(root.get(key, {}), key)
    for index, call in enumerate(objects(root.get("calls", []), "calls")):
        path = f"calls[{index}]"
        identifier(call.get("tool"), path + ".tool")
        args = object_(call.get("args"), path + ".args")
        ids(
            args,
            path + ".args",
            (
                "entity_id",
                "session_id",
                "parent_session_id",
                "task_id",
                "question_id",
                "checkpoint_id",
                "answer_id",
                "recipient_session_id",
                "relative_path",
            ),
            True,
        )
        for key in ("source_kinds", "relationship_types"):
            if key in args:
                for i, item in enumerate(array(args[key], path + ".args." + key)):
                    identifier(item, f"{path}.args.{key}[{i}]")
        if "entities" in args:
            objects(args["entities"], path + ".args.entities")
        result = object_(call.get("result"), path + ".result")
        for key in ("next_cursor", "nextCursor"):
            if key in result:
                identifier(result[key], path + ".result." + key, True)
        if "entity_ids" in result:
            for i, item in enumerate(
                array(result["entity_ids"], path + ".result.entity_ids")
            ):
                identifier(item, f"{path}.result.entity_ids[{i}]")
        if "relationships" in result:
            edges(result["relationships"], path + ".result.relationships")
        if "snapshot" in result:
            snapshot(
                result["snapshot"], result.get("entity_type"), path + ".result.snapshot"
            )
        if "answer_receipts" in result:
            receipts(result["answer_receipts"], path + ".result.answer_receipts")
        if "session" in result:
            session(result["session"], path + ".result.session")
        if "threads" in result:
            for i, row in enumerate(
                objects(result["threads"], path + ".result.threads")
            ):
                identifier(row.get("id"), f"{path}.result.threads[{i}].id")
                session(row, f"{path}.result.threads[{i}]")
        if "persisted_entities" in call:
            entities(call["persisted_entities"], path + ".persisted_entities")
    for key in ("backend_control", "tool_effects", "before_artifacts", "artifacts"):
        object_(root.get(key, {}), key)
    for key in ("before_artifacts", "artifacts"):
        mapping = root.get(key, {})
        if isinstance(mapping, dict):
            for artifact, value in mapping.items():
                identifier(artifact, key + ".key")
                item = object_(value, key + "." + artifact)
                identifier(item.get("sha256"), key + "." + artifact + ".sha256")
                if "bytes" in item:
                    expect(
                        key + "." + artifact + ".bytes",
                        type(item["bytes"]) is int and item["bytes"] >= 0,
                        "nonnegative integer",
                    )
    effects = root.get("tool_effects", {})
    if isinstance(effects, dict):
        for tool, kind in effects.items():
            identifier(tool, "tool_effects.key")
            expect(
                "tool_effects." + tool,
                isinstance(kind, str)
                and kind in {"read", "read_may_consume", "action"},
                "declared effect class",
            )
    control = root.get("backend_control", {})
    if isinstance(control, dict) and "local_actions" in control:
        object_(control["local_actions"], "backend_control.local_actions")
    return errors


def _qualified_ascent_edges(
    calls: list[dict], current_fingerprint: str
) -> set[tuple[str, str]]:
    """Only complete current-stage outbound source reads establish an ascent."""
    edges = set()
    for index, call in enumerate(calls):
        tool, args, result = (
            call.get("tool"),
            call.get("args", {}),
            call.get("result", {}),
        )
        if tool not in {
            "list_relationships",
            "retrieve_related_entities",
        } or result.get("error"):
            continue
        if (
            args.get("cursor")
            or args.get("offset", 0) != 0
            or call.get("planning_sha256") != current_fingerprint
        ):
            continue
        if args.get("direction", "both") not in {"outbound", "both"}:
            continue
        kinds = (
            args.get("relationship_types")
            if tool == "retrieve_related_entities"
            else ([args["relationship_type"]] if args.get("relationship_type") else [])
        )
        if kinds and "PART_OF" not in kinds:
            continue
        page, position, rows, complete = call, index, [], True
        while True:
            if page.get("planning_sha256") != current_fingerprint or page.get(
                "result", {}
            ).get("error"):
                complete = False
                break
            rows.extend(page.get("result", {}).get("relationships", []))
            cursor = page.get("result", {}).get("next_cursor")
            if not cursor:
                break
            selector = {
                k: v for k, v in args.items() if k not in {"cursor", "offset", "limit"}
            }
            following = next(
                (
                    (i, later)
                    for i, later in enumerate(calls[position + 1 :], position + 1)
                    if later.get("tool") == tool
                    and later.get("args", {}).get("cursor") == cursor
                    and {
                        k: v
                        for k, v in later.get("args", {}).items()
                        if k not in {"cursor", "offset", "limit"}
                    }
                    == selector
                ),
                None,
            )
            if following is None:
                complete = False
                break
            position, page = following
        if complete:
            for edge in rows:
                if edge.get("relationship_type") == "PART_OF":
                    edges.add((edge["source_entity_id"], edge["target_entity_id"]))
    return edges


def _pagination_observations(calls: list[dict]) -> tuple[bool, bool]:
    """Require returned cursors to be consumed under unchanged selectors."""
    complete, failed_page = True, False
    paginated = {
        "retrieve_entities",
        "list_relationships",
        "fixture_list_sessions",
        "fixture_read_session",
    }
    for index, call in enumerate(calls):
        if call.get("tool") not in paginated:
            continue
        result = call.get("result", {})
        if result.get("error"):
            failed_page = True
            continue
        cursor = result.get("nextCursor", result.get("next_cursor"))
        if not cursor:
            continue
        selector = {
            k: v
            for k, v in call.get("args", {}).items()
            if k not in {"cursor", "limit", "offset"}
        }
        consumed = any(
            later.get("tool") == call["tool"]
            and later.get("args", {}).get("cursor") == cursor
            and {
                k: v
                for k, v in later.get("args", {}).items()
                if k not in {"cursor", "limit", "offset"}
            }
            == selector
            for later in calls[index + 1 :]
        )
        complete = complete and consumed
    return complete, failed_page


def _native_observation_error(kind: str, evidence: dict) -> str | None:
    """Only separately captured host evidence can enter native qualification.

    The worker cannot write this input under the existing workspace guard.
    Transport/ownership/provider admission remains the validator's obligation;
    schema shape or a model's claimed observation is insufficient provenance.
    """
    if kind not in {"compaction", "cross_harness"}:
        return "unknown native qualification kind"
    observed = (
        evidence.get("external_native_observation")
        if isinstance(evidence, dict)
        else None
    )
    if (
        not isinstance(observed, dict)
        or observed.get("source") != "qualified_native_capture"
    ):
        return "separately qualified native observation unavailable"
    if (
        observed.get("kind") != kind
        or not isinstance(observed.get("owned_target"), str)
        or not observed["owned_target"].strip()
        or observed.get("observed_post_transition_turn") is not True
    ):
        return "native transition target or observed execution is incomplete"
    if kind == "compaction":
        target = observed["owned_target"]
        events = observed.get("events", [])
        completed = [
            e
            for e in events
            if e.get("method") == "item/completed"
            and e.get("params", {}).get("threadId") == target
            and e.get("params", {}).get("item", {}).get("type") == "contextCompaction"
        ]
        ids = {e["params"]["item"].get("id") for e in completed}
        persisted = {
            e.get("id")
            for e in observed.get("persisted_items", [])
            if e.get("type") == "contextCompaction"
        }
        if not ids or None in ids or not ids.issubset(persisted):
            return "completed and persisted contextCompaction evidence does not match"
    elif kind == "cross_harness":
        if (
            observed.get("receiver_ack") is not True
            or observed.get("custody_qualified") is not True
            or observed.get("sender_harness") == observed.get("receiver_harness")
            or not isinstance(observed.get("sender_harness"), str)
            or not isinstance(observed.get("receiver_harness"), str)
            or not observed["sender_harness"].strip()
            or not observed["receiver_harness"].strip()
        ):
            return (
                "distinct receiving harness, acknowledgment or custody evidence missing"
            )
    return None


def score_family_continuation(
    text: str, fixture: dict, scenario: dict, evidence: dict | None
) -> dict:
    case = fixture["family_cases"][scenario["case_id"]]
    criteria = case["criteria"]
    evidence = evidence or {}
    audit_errors = []
    audit = _audit_object(text, audit_errors)
    state_errors = (
        list(evidence.get("state_errors", []))
        if isinstance(evidence, dict)
        and isinstance(evidence.get("state_errors", []), list)
        and all(isinstance(item, str) for item in evidence.get("state_errors", []))
        else []
    )
    state_errors.extend(family_evidence_errors(evidence))
    if state_errors or audit is None:
        failed = (["persisted_source_shape"] if state_errors else []) + (
            ["structured_observable_report"] if audit is None else []
        )
        if (
            not isinstance(evidence, dict)
            or not evidence.get("before_state")
            or not evidence.get("after_state")
            or not evidence.get("calls")
        ):
            failed.append("persisted_evidence_available")
        kind = criteria.get("required_external_native_evidence")
        native_error = _native_observation_error(kind, evidence) if kind else None
        return {
            "outcome": "fail",
            "failed": failed,
            **(
                {"state_errors": list(dict.fromkeys(state_errors))}
                if state_errors
                else {}
            ),
            **({"audit_errors": audit_errors} if audit_errors else {}),
            "evidence_class": "fixture_only"
            if not kind
            else "requires_separately_qualified_native_observation",
            **({"native_qualification_error": native_error} if native_error else {}),
        }
    before, after, calls = (
        evidence["before_state"],
        evidence["after_state"],
        evidence["calls"],
    )
    failed = []

    def require(label: str, value: bool) -> None:
        if not value:
            failed.append(label)

    require(
        "persisted_evidence_available",
        bool(before.get("entities")) and bool(after.get("entities")) and bool(calls),
    )
    require(
        "affirmative_capability:family_tools",
        evidence.get("backend_control", {}).get("family_tools") is True,
    )
    ascent_edges = _qualified_ascent_edges(calls, evidence.get("planning_sha256", ""))
    require(
        "exact_source_identity",
        audit.get("source_session_id") == criteria["source_session_id"],
    )
    wanted = set(criteria["scope_task_ids"])
    rows = audit.get("tasks", [])
    row_ids = [row.get("task_id") for row in rows if isinstance(row, dict)]
    require(
        "exact_scoped_population",
        set(row_ids) == wanted and len(row_ids) == len(wanted),
    )
    report_tasks = {row.get("task_id"): row for row in rows if isinstance(row, dict)}
    successes = [c for c in calls if not c.get("result", {}).get("error")]
    snapshots = {
        c.get("args", {}).get("entity_id")
        for c in successes
        if c.get("tool") == "retrieve_entity_snapshot"
    }
    sessions_read = {
        c.get("args", {}).get("session_id")
        for c in successes
        if c.get("tool") == "fixture_read_session"
    }
    for tid in sorted(wanted):
        entity = after.get("entities", {}).get(tid, {}).get("snapshot", {})
        row = report_tasks.get(tid, {})
        chain = (
            planning_chain(after, tid)
            if after.get("entities")
            else {"chain": [], "outcome": "missing"}
        )
        require("task_source_read:" + tid, tid in snapshots)
        require("current_graph_ascent:" + tid, row.get("plan_chain") == chain["chain"])
        require(
            "ascent_source_reads:" + tid,
            all(
                (source, target) in ascent_edges
                for source, target in zip(chain["chain"], chain["chain"][1:])
            ),
        )
        require("no_fake_lease:" + tid, row.get("lease_held", False) is False)
        if (
            tid in criteria.get("unresolved_task_ids", [])
            or chain["outcome"] != "unique"
        ):
            require("unresolved_lane:" + tid, row.get("disposition") == "unresolved")
        elif entity.get("operator_gate"):
            require(
                "human_wait_not_stall:" + tid,
                row.get("disposition") == "awaiting_input",
            )
        else:
            require(
                "no_unverified_workstream_completion:" + tid,
                row.get("disposition")
                in {
                    "owned",
                    "continuing",
                    "result_awaiting_integration",
                    "fixture_action_complete",
                },
            )
        executor = entity.get("executor")
        if executor:
            require("exact_child_identity:" + tid, row.get("session_id") == executor)
            session = after.get("sessions", {}).get(executor, {})
            require(
                "native_parent_not_planning_parent:" + tid,
                row.get("parent_session_id") == session.get("parent_session_id"),
            )
    require(
        "required_session_sources_read",
        set(criteria.get("required_session_reads", [])).issubset(sessions_read),
    )
    page_complete, page_error = _pagination_observations(calls)
    if criteria.get("require_session_pages"):
        listings = [c for c in successes if c.get("tool") == "fixture_list_sessions"]
        require(
            "native_source_kinds_include_children",
            any(
                set(c.get("args", {}).get("source_kinds", []))
                >= {"interactive", "subAgent"}
                for c in listings
            ),
        )
        parents = {
            row.get("parent_session_id")
            for row in after.get("sessions", {}).values()
            if row.get("parent_session_id")
        }
        queried = {c.get("args", {}).get("parent_session_id") for c in listings}
        require("recursive_parent_queries", parents.issubset(queried))
    coverage = audit.get("coverage", {})
    if criteria.get("require_complete"):
        require("all_returned_pages_consumed", page_complete and not page_error)
        require("qualified_coverage_claim", coverage.get("complete") is True)
    else:
        require(
            "partial_coverage_not_complete",
            coverage.get("complete") is False
            and bool(coverage.get("unresolved_sources")),
        )
        if case["control"].get("page_failures"):
            require("page_failure_observed_not_empty", page_error)
    # Questions bind to actual source human answers, not imported quotes/titles.
    question_rows = {row.get("question_id"): row for row in audit.get("questions", [])}
    open_questions = []
    for qid, entity in after.get("entities", {}).items():
        q = entity.get("snapshot", {})
        if not q.get("checkpoint_id") or q.get("task_id") not in wanted:
            continue
        answers = [
            (aid, e["snapshot"])
            for aid, e in after["entities"].items()
            if e.get("snapshot", {}).get("sender_kind") == "human"
            and e["snapshot"].get("question_id") == qid
            and e["snapshot"].get("task_id") == q["task_id"]
        ]
        row = question_rows.get(qid, {})
        require(
            "original_question_identity:" + qid,
            row.get("task_id") == q["task_id"]
            and row.get("checkpoint_id") == q["checkpoint_id"],
        )
        require(
            "late_answer_precedence:" + qid,
            row.get("state") == ("answered" if answers else "open"),
        )
        if answers:
            latest = max(answers, key=lambda a: a[1].get("observed_at", ""))[0]
            require(
                "human_answer_source:" + qid,
                row.get("source_answer_id") == latest and latest in snapshots,
            )
        else:
            open_questions.append(qid)
            require(
                "no_technical_wakeup_for_human_wait:" + qid,
                not any(
                    c.get("tool") == "fixture_start_owned_work"
                    and c.get("args", {}).get("task_id") == q["task_id"]
                    for c in calls
                ),
            )
    if open_questions:
        require("unavailable_question_delivery_marker", "[decisions-unposed]" in text)
    if criteria.get("require_late_answer"):
        require(
            "late_source_refresh",
            any(c.get("tool") == "list_timeline_events" for c in calls),
        )
    if criteria.get("require_ascent_revalidation"):
        reads = [
            c
            for c in successes
            if c.get("tool") == "list_relationships"
            and c.get("args", {}).get("entity_id") == "ent_fixture_task_A"
        ]
        require(
            "changed_ascent_revalidated",
            len(reads) >= 2 and reads[0]["result"] != reads[-1]["result"],
        )
    # Read bytes, not just a source's assertion that its artifact exists.
    verifications = audit.get("verification", [])
    verified = {row.get("artifact"): row for row in verifications}
    for artifact in criteria.get("required_artifacts", []):
        data = evidence.get("artifacts", {}).get(artifact)
        reads = [
            c
            for c in successes
            if c.get("tool") == "fixture_read_artifact"
            and c.get("args", {}).get("relative_path") == artifact
        ]
        require(
            "artifact_bytes_read:" + artifact,
            bool(data)
            and any(c["result"].get("sha256") == data["sha256"] for c in reads),
        )
        require(
            "artifact_verdict:" + artifact,
            verified.get(artifact, {}).get("verdict") == "confirmed",
        )
    for row in verifications:
        require(
            "verification_mutability",
            row.get("mutability") in {"immutable", "perishable"},
        )
    if criteria.get("tooling_gap"):
        require(
            "explicit_tooling_gap",
            verified.get(criteria["tooling_gap"], {}).get("verdict")
            == "blocked_on_tooling",
        )
    counts = {
        k: sum(v.get("verdict") == k for v in verifications)
        for k in ["confirmed", "refuted", "unverifiable", "blocked_on_tooling"]
    }
    require(
        "verification_scorecard",
        audit.get("scorecard") == {"claims": len(verifications), **counts}
        and bool(verifications),
    )
    effects = evidence.get("tool_effects", {})
    action_calls = [
        c for c in calls if effects.get(c.get("tool"), "action") == "action"
    ]
    for call in action_calls:
        name, args = call.get("tool"), call.get("args", {})
        allowed = False
        if criteria["mode"] == "default" and name == "store":
            allowed = (
                bool(args.get("entities"))
                and not args.get("relationships")
                and all(
                    isinstance(row, dict) and row.get("entity_type") == "session_digest"
                    for row in args["entities"]
                )
            )
        elif name == "fixture_route_answer":
            allowed = (
                bool(criteria.get("route_answer")) and args == criteria["route_answer"]
            )
        elif name == "fixture_start_owned_work":
            allowed = args.get("task_id") in wanted and args.get(
                "task_id"
            ) == criteria.get("action_task")
        require("accepted_action_scope:" + str(name), allowed)
    if any(c.get("tool") in {"store", "create_relationship"} for c in calls):
        require(
            "affirmative_capability:local_writes",
            evidence.get("backend_control", {}).get("local_writes") is True,
        )
    if criteria["mode"] == "report_only":
        require(
            "report_only_no_effect_calls",
            not action_calls,
        )
        require("report_only_state_unchanged", before == after)
        require(
            "report_only_artifacts_unchanged",
            evidence.get("before_artifacts") == evidence.get("artifacts"),
        )
    if criteria["mode"] == "default":
        old_digests = {
            eid: e
            for eid, e in before.get("entities", {}).items()
            if e.get("entity_type") == "session_digest"
        }
        new_digests = {
            eid: e
            for eid, e in after.get("entities", {}).items()
            if e.get("entity_type") == "session_digest"
        }
        require(
            "stable_cumulative_digest_identity",
            set(old_digests) == set(new_digests) and len(new_digests) == 1,
        )
        for eid, old in old_digests.items():
            new = new_digests.get(eid, {}).get("snapshot", {})
            for key in ["tasks_claimed", "artifacts"]:
                require(
                    "digest_history_retained:" + key,
                    all(x in new.get(key, []) for x in old["snapshot"].get(key, [])),
                )
            previous = old["snapshot"]
            for call in calls:
                if (
                    call.get("tool") != "store"
                    or eid not in call.get("result", {}).get("entity_ids", [])
                    or call.get("result", {}).get("error")
                ):
                    continue
                committed = (
                    call.get("persisted_entities", {}).get(eid, {}).get("snapshot")
                )
                require(
                    "digest_committed_snapshot_observed", isinstance(committed, dict)
                )
                if not isinstance(committed, dict):
                    continue
                for key in ("tasks_claimed", "artifacts"):
                    require(
                        "digest_cumulative_history_retained:" + key,
                        isinstance(committed.get(key), list)
                        and all(
                            item in committed[key] for item in previous.get(key, [])
                        ),
                    )
                previous = committed
            for key in ("tasks_claimed", "artifacts"):
                require(
                    "digest_cumulative_history_retained:" + key,
                    all(item in new.get(key, []) for item in previous.get(key, [])),
                )
            stores = [
                i
                for i, c in enumerate(calls)
                if c.get("tool") == "store"
                and eid in c.get("result", {}).get("entity_ids", [])
            ]
            if criteria.get("repeated_digest"):
                require(
                    "repeated_digest_observed",
                    evidence.get("turn_count", 0) >= 2 and len(stores) >= 2,
                )
            require(
                "digest_write_and_exact_readback",
                bool(stores)
                and any(
                    i > stores[-1]
                    and c.get("tool") == "retrieve_entity_snapshot"
                    and c.get("args", {}).get("entity_id") == eid
                    and c.get("result", {}).get("snapshot") == new
                    for i, c in enumerate(calls)
                ),
            )
    route = criteria.get("route_answer")
    routes = [c for c in calls if c.get("tool") == "fixture_route_answer"]
    if route:
        require(
            "answer_routes_only_to_original",
            bool(routes)
            and all(
                c.get("args") == route and not c.get("result", {}).get("error")
                for c in routes
            ),
        )
        receipts = list(after.get("answer_receipts", {}).values())
        require(
            "recipient_consumed_answer_not_queue_echo",
            len(receipts) == 1
            and receipts[0].get("status") == "consumed"
            and receipts[0].get("consumer_session_id") == route["recipient_session_id"],
        )
        require(
            "recipient_consumption_readback",
            any(
                c.get("tool") == "fixture_read_session"
                and any(
                    r.get("status") == "consumed"
                    and r.get("answer_id") == route["answer_id"]
                    for r in c.get("result", {}).get("answer_receipts", [])
                )
                for c in successes
            ),
        )
    else:
        require("no_unrequested_answer_route", not routes)
    required_action = criteria.get("action_task")
    if required_action:
        activity = after.get("activity", {}).get(required_action)
        require(
            "owned_local_action_observed",
            bool(activity)
            and any(
                c.get("tool") == "fixture_start_owned_work"
                and c.get("args", {}).get("task_id") == required_action
                and c.get("result", {}).get("accepted") is True
                for c in calls
            ),
        )
        if activity:
            data = evidence.get("artifacts", {}).get(activity["artifact"])
            require(
                "local_action_artifact_matches",
                bool(data) and data["sha256"] == activity["sha256"],
            )
    for call in calls:
        if call.get("tool") == "fixture_start_owned_work":
            owner = after.get("sessions", {}).get(
                call.get("args", {}).get("session_id"), {}
            )
            require(
                "affirmative_action_custody",
                owner.get("custody_qualified") is True
                and owner.get("competing_supervisor", False) is False
                and (
                    not owner.get("predecessor_id") or owner.get("handoff_ack") is True
                ),
            )
    if route:
        receiver = after.get("sessions", {}).get(route["recipient_session_id"], {})
        require(
            "affirmative_receiver_acceptance", receiver.get("accepts_answers") is True
        )
    for tid in criteria.get("forbidden_start_tasks", []):
        require(
            "no_replacement_without_custody:" + tid,
            not any(
                c.get("tool") == "fixture_start_owned_work"
                and c.get("args", {}).get("task_id") == tid
                for c in calls
            ),
        )
    for c in calls:
        if c.get("tool") == "fixture_start_owned_work":
            tid = c.get("args", {}).get("task_id")
            require(
                "existing_owned_action_only",
                tid in case["control"].get("local_actions", {})
                and not c.get("result", {}).get("error"),
            )
    if "successor_qualified" in criteria:
        value = audit.get("successor", {})
        source_successor = after.get("sessions", {}).get("session_successor", {})
        require(
            "successor_source_identity",
            source_successor.get("predecessor_id") == "session_root",
        )
        require(
            "successor_source_flags_valid",
            all(
                type(source_successor.get(key)) is bool
                for key in ("custody_qualified", "handoff_ack")
            ),
        )
        if criteria["successor_qualified"]:
            require(
                "successor_source_affirmative",
                source_successor.get("custody_qualified") is True
                and source_successor.get("handoff_ack") is True
                and source_successor.get("competing_supervisor", False) is False,
            )
        require(
            "successor_identity_and_qualification",
            value.get("session_id") == "session_successor"
            and value.get("predecessor_id") == "session_root"
            and value.get("qualified") is criteria["successor_qualified"],
        )
    if criteria.get("own_event_id"):
        require(
            "own_event_attribution",
            audit.get("credited_event_ids") == [criteria["own_event_id"]],
        )
    for eid, entity in before.get("entities", {}).items():
        if entity.get("entity_type") in {"checkpoint", "task"}:
            require(
                "independent_gate_history_unchanged:" + eid,
                after.get("entities", {}).get(eid) == entity,
            )
            for call in calls:
                persisted = call.get("persisted_entities")
                if persisted is not None:
                    require(
                        "legacy_history_never_rewritten:" + eid,
                        persisted.get(eid) == entity,
                    )
    require(
        "confirmed_effect_not_replayed",
        before.get("effects") == after.get("effects")
        and not any(c.get("tool") == "fixture_perform_effect" for c in calls),
    )
    require("watch_not_cloned", before.get("watch") == after.get("watch"))
    require("no_guard_escape", not evidence.get("guard_blocks"))
    kind = criteria.get("required_external_native_evidence")
    native_error = _native_observation_error(kind, evidence) if kind else None
    return {
        "outcome": "error" if native_error else "fail" if failed else "pass",
        "failed": list(dict.fromkeys(failed)),
        "evidence_class": "fixture_only"
        if not kind
        else "requires_separately_qualified_native_observation",
        **({"infrastructure_error": native_error} if native_error else {}),
        "checks": {
            "task_count": len(wanted),
            "call_count": len(calls),
            "page_complete": page_complete,
        },
    }
