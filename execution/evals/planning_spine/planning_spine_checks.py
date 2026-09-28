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


def structural_placements(fixture: dict) -> dict[str, str | None]:
    """Derive workstream placement only from fixture ``PART_OF`` edges."""
    entities = fixture["entities"]
    master_id = fixture["master_plan_id"]
    outbound: dict[str, list[str]] = {}
    for edge in fixture["relationships"]:
        if edge["relationship_type"].upper() != "PART_OF":
            continue
        outbound.setdefault(edge["source_entity_id"], []).append(
            edge["target_entity_id"]
        )

    placements: dict[str, str | None] = {}
    for entity_id, entity in entities.items():
        if entity_id == master_id or entity["entity_type"] != "plan":
            continue
        parents = outbound.get(entity_id, [])
        phase_parents = [
            parent
            for parent in parents
            if entities.get(parent, {}).get("entity_type") == "plan_phase"
        ]
        if len(phase_parents) == 1:
            placements[entity_id] = entities[phase_parents[0]]["snapshot"]["name"]
        else:
            placements[entity_id] = None
    return placements


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


def _cross_phase_section(text: str) -> str:
    for heading, body in _sections(text):
        if "cross-phase" in _fold(heading):
            return body
    return ""


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
    placements = structural_placements(fixture)
    folded = _fold(text)
    failed: list[str] = []

    master_pos = text.lower().find(master["title"].lower())
    mechanics_positions = [
        pos
        for needle in (
            "rules delivery hardening",
            "e2 bootstrap repair",
            "### task mechanics",
            "1. repair",
        )
        if (pos := text.lower().find(needle)) >= 0
    ]
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

    for entity_id, phase in placements.items():
        if phase is None:
            continue
        title = entities[entity_id]["snapshot"]["title"]
        block = _section_for_phase(text, phase)
        if title.lower() not in block.lower() or "structurally bound" not in _fold(
            block
        ):
            failed.append("structural_phase_binding")
            break

    cross_phase = _cross_phase_section(text)
    for entity_id, phase in placements.items():
        if phase is not None:
            continue
        title = entities[entity_id]["snapshot"]["title"]
        cross_folded = _fold(cross_phase)
        wrongly_phased = any(
            title.lower() in body.lower()
            for heading, body in _sections(text)
            if re.fullmatch(r"phase\s+.+?\s+workstreams", heading, re.IGNORECASE)
        )
        if (
            title.lower() not in cross_phase.lower()
            or "cross-phase prerequisite" not in cross_folded
            or "not structurally derivable" not in cross_folded
            or wrongly_phased
        ):
            failed.append("unbound_work_is_cross_phase")
            break

    if not (
        "reconcile-planning" in folded
        and "retrospective" in folded
        and "not the real-time source of truth" in folded
    ):
        failed.append("reconcile_is_retrospective")

    return {
        "outcome": "pass" if not failed else "fail",
        "failed": failed,
        "structural_placements": placements,
        "expected_phase_rows": expected_rows,
        "actual_phase_rows": actual_rows,
    }
