"""Transient session planning reads; this module owns no persisted state."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
import math


def planning_declaration(value: object) -> dict | None:
    """Read the one SchemaDefinition declaration, without type-name inference."""
    if not isinstance(value, dict):
        return None
    level = value.get("level")
    if isinstance(level, bool) or not isinstance(level, (float, int)):
        return None
    try:
        if not math.isfinite(level):
            return None
    except OverflowError:
        return None
    if "read_only" in value and not isinstance(value["read_only"], bool):
        return None
    return {"level": level, "read_only": value.get("read_only", False)}


def session_planning_tree(
    rows: list[dict], declarations: dict[str, dict], *, complete: bool, gaps: list[str]
) -> dict:
    """Build a forest over session membership, never whole-plan completion.

    Registry completeness is a separate input: the highest encountered type
    cannot establish the highest level the instance actually declares.
    """
    levels = sorted({mark["level"] for mark in declarations.values()})
    memberships = [
        (index, row) for index, row in enumerate(rows)
        if row.get("kind") == "task" and row.get("priority") != "display-plan"
    ]
    paths: list[tuple[int, list[dict], str | None, bool]] = []
    reached: dict[float, set[str]] = {level: set() for level in levels}
    covered: dict[float, set[str]] = {level: set() for level in levels}
    for index, row in memberships:
        task_id = row.get("entity_id")
        path = [{"entity_id": task_id, "entity_type": "task", "title": row["work"]}]
        reason = None
        unknown = False
        if not isinstance(task_id, str):
            reason, unknown = "The current task identity could not be read", True
        elif not complete:
            reason, unknown = "The instance's declared planning levels could not be fully read", True
        elif not levels:
            reason = "The instance has no declared planning levels"
        ascent = row.get("planning_ascent", [])
        planning_gaps = row.get("planning_read_gaps", [])
        if not isinstance(ascent, list) or not isinstance(planning_gaps, list):
            reason, unknown = "The planning ascent is malformed", True
            ascent = []
        previous = None
        visited = {task_id}
        seen_levels: set[float] = set()
        for parent in ascent:
            if not isinstance(parent, dict) or parent.get("entity_id") in visited:
                reason, unknown = "The planning ascent contains an invalid or repeated parent", True
                break
            visited.add(parent.get("entity_id"))
            entity_type = parent.get("entity_type")
            if entity_type == "task" and previous is None:
                path.append(parent)
                continue
            mark = declarations.get(entity_type)
            if mark is None:
                reason = f"The registry has no valid planning declaration for {entity_type}"
                break
            level = mark["level"]
            if previous is not None and level <= previous:
                reason, unknown = "Planning parent levels do not strictly increase", True
                break
            seen_levels.add(level)
            previous = level
            path.append(parent)
            if isinstance(task_id, str):
                reached[level].add(task_id)
                if complete and all(lower in seen_levels for lower in levels if lower <= level):
                    covered[level].add(task_id)
        if planning_gaps:
            reason = "; ".join(str(gap) for gap in planning_gaps)
            unknown = not reason.startswith("No planning parent is recorded")
        if reason is None:
            if not ascent or previous is None:
                reason = "No planning parent is recorded"
            elif any(level not in seen_levels for level in levels):
                reason = "The ascent ends before, or skips, a declared planning level"
        paths.append((index, path, reason, unknown))

    # The same record must not acquire two parents merely because two rows
    # supplied contradictory paths during this read interval.
    parents: dict[str, set[str | None]] = {}
    for _, path, reason, _ in paths:
        if reason:
            continue
        for position, record in enumerate(path):
            parent_id = path[position + 1]["entity_id"] if position + 1 < len(path) else None
            parents.setdefault(record["entity_id"], set()).add(parent_id)
    conflicts = {record_id for record_id, values in parents.items() if len(values) != 1}
    versions: dict[str, set[tuple]] = {}
    disputed_tasks: set[str] = set()
    for _, path, reason, unknown in paths:
        task_id = path[0]["entity_id"]
        if isinstance(task_id, str):
            versions.setdefault(task_id, set()).add(
                (tuple(record["entity_id"] for record in path), reason, unknown)
            )
            if any(record["entity_id"] in conflicts for record in path):
                disputed_tasks.add(task_id)
    disputed_tasks.update(task_id for task_id, variants in versions.items() if len(variants) > 1)
    for values in covered.values():
        values.difference_update(disputed_tasks)
    nodes: dict[str, dict] = {}
    unplanned = []
    qualified: set[str] = set()
    unknown_ids: set[str] = set()
    for index, path, reason, unknown in paths:
        if any(record["entity_id"] in conflicts for record in path):
            reason, unknown = "The read contains conflicting planning parents", True
        elif reason is None and path[0]["entity_id"] in disputed_tasks:
            reason, unknown = "Session entries disagree about planning coverage", True
        if reason:
            unplanned.append({"row_index": index, "reason": reason, "unknown": unknown})
            if unknown and isinstance(rows[index].get("entity_id"), str):
                unknown_ids.add(rows[index]["entity_id"])
            continue
        qualified.add(path[0]["entity_id"])
        for position, record in enumerate(path):
            record_id = record["entity_id"]
            mark = declarations.get(record["entity_type"])
            node = nodes.setdefault(record_id, {
                "entity_id": record_id,
                "entity_type": record["entity_type"],
                "title": record["title"],
                "level": mark["level"] if mark else None,
                "read_only": mark["read_only"] if mark else False,
                "parent_id": path[position + 1]["entity_id"] if position + 1 < len(path) else None,
                "row_indexes": [],
            })
            if position == 0:
                node["row_indexes"].append(index)

    descendant_indexes: dict[str, set[int]] = {node_id: set() for node_id in nodes}
    for node in nodes.values():
        for index in node["row_indexes"]:
            current = node
            while current is not None:
                descendant_indexes[current["entity_id"]].add(index)
                current = nodes.get(current["parent_id"])
    for node_id, node in nodes.items():
        node["counts"] = session_state_counts(rows, list(descendant_indexes[node_id]))

    task_ids = {row["entity_id"] for _, row in memberships if isinstance(row.get("entity_id"), str)}
    return {
        "version": 1,
        "scope": "Only this session's task membership; not whole-plan completion",
        "registry_complete": complete,
        "registry_gaps": gaps,
        "membership_entries": len(memberships),
        "unique_tasks": len(task_ids),
        "unresolved_entries": sum(not isinstance(row.get("entity_id"), str) for _, row in memberships),
        "qualified_tasks": len(qualified),
        "unknown_tasks": len(unknown_ids),
        "level_coverage": [
            {"level": level,
             "entity_types": sorted(kind for kind, mark in declarations.items() if mark["level"] == level),
             "reached_tasks": len(reached[level]),
             "complete_tasks": len(covered[level]) if complete else None}
            for level in reversed(levels)
        ],
        "nodes": list(nodes.values()),
        "roots": [node["entity_id"] for node in nodes.values() if node["parent_id"] is None],
        "unplanned": unplanned,
    }


def session_state_counts(rows: list[dict], row_indexes: list[int]) -> dict[str, int]:
    """Count unique task membership; contradictory observations stay recorded."""
    states: dict[str, set[str]] = {}
    for index in row_indexes:
        row = rows[index]
        key = row.get("entity_id") or f"unresolved:{index}"
        states.setdefault(key, set()).add(row["priority"])
    counts = Counter(next(iter(values)) if len(values) == 1 else "recorded" for values in states.values())
    return {"tasks": len(states), **dict(counts)}


# Read-only planning-record hygiene section (operator decision d4, plan
# ent_b7a4ebbe5086bc1670ec4665: warnings and a visible list, never blocking).
# It is derived from the rows this read already built; it creates no store.
NEXT_STEPS_CAP_CHARS = 2000
STALE_IN_PROGRESS_DAYS = 7
_IN_PROGRESS = {"in_progress"}


def _parse_when(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def planning_record_findings(rows: list[dict], *, now: datetime) -> dict:
    """List planning records in shapes known to rot: an oversized `next_steps`
    on a plan, an `in_progress` task untouched for 7+ days, and an
    `in_progress` task with no owner. Read-only and bounded to this read's rows.

    The data path does not say which session wrote an oversized field, so
    plans and tasks are listed, not sessions. Plans reached only through a
    task's PART_OF ascent are not read here, so they are not checked.
    """
    oversized, stale, ownerless = [], [], []
    for row in rows:
        entity_id = row.get("entity_id")
        item = {"entity_id": entity_id, "title": row.get("work")}
        chars = row.get("next_steps_chars")
        if row.get("kind") == "plan" and isinstance(chars, int) and chars > NEXT_STEPS_CAP_CHARS:
            oversized.append({**item, "chars": chars})
        status = row.get("recorded_status")
        if row.get("kind") != "task" or not isinstance(status, str) or status.strip().lower() not in _IN_PROGRESS:
            continue
        touched = _parse_when(row.get("record_last_observation_at"))
        if touched is not None and now - touched >= timedelta(days=STALE_IN_PROGRESS_DAYS):
            stale.append({**item, "days_idle": (now - touched).days})
        if not any(
            isinstance(row.get(key), str) and row[key].strip().lower() not in {"", "unknown", "unassigned"}
            for key in ("assigned_principal", "record_owner", "recorded_executor", "declared_step_owner")
        ):
            ownerless.append(item)
    return {
        "scope": "Only records on this board; a read-only list, not a store or a gate",
        "next_steps_cap_chars": NEXT_STEPS_CAP_CHARS,
        "stale_after_days": STALE_IN_PROGRESS_DAYS,
        "oversized_next_steps": oversized,
        "stale_in_progress": stale,
        "ownerless_in_progress": ownerless,
        "limits": [
            "The writing session is not recorded cheaply, so plans and tasks are listed, not sessions",
            "Plans reached only through a task's PART_OF ascent are not read here",
        ],
    }
