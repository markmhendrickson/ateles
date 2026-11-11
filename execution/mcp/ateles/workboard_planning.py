"""Transient session planning reads; this module owns no persisted state."""

from __future__ import annotations

from collections import Counter
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
    partial_paths: list[tuple[int, list[dict]]] = []
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
            elif (
                complete and isinstance(path[0]["entity_id"], str)
                and path[0]["entity_id"] not in disputed_tasks
                and any(record["entity_type"] in declarations for record in path[1:])
            ):
                # A known, coherent prefix that stops short (for example an
                # existing plan with no parent) stays visible as a branch
                # beneath Unplanned. It never counts as a qualified ascent.
                partial_paths.append((index, path))
            continue
        qualified.add(path[0]["entity_id"])
        _add_path(nodes, path, index, declarations)

    # Partial branches add no parent beyond the last observed record. A record
    # that a qualified branch also holds, or that partial paths give different
    # parents, is a contradiction in this read: those entries stay flat.
    partial_parents: dict[str, set[str | None]] = {}
    for _, path in partial_paths:
        for position, record in enumerate(path):
            parent_id = path[position + 1]["entity_id"] if position + 1 < len(path) else None
            partial_parents.setdefault(record["entity_id"], set()).add(parent_id)
    contested = {
        record_id for record_id, values in partial_parents.items()
        if len(values) != 1 or record_id in nodes
    }
    unplanned_nodes: dict[str, dict] = {}
    for index, path in partial_paths:
        if not any(record["entity_id"] in contested for record in path):
            _add_path(unplanned_nodes, path, index, declarations)
    _add_counts(rows, nodes)
    _add_counts(rows, unplanned_nodes)

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
        # Observed, coherent but incomplete ascents. Roots here are the highest
        # record observed, not a claim that the record has no parent.
        "unplanned_nodes": list(unplanned_nodes.values()),
        "unplanned_roots": [
            node["entity_id"] for node in unplanned_nodes.values() if node["parent_id"] is None
        ],
    }


def _add_path(nodes: dict[str, dict], path: list[dict], index: int, declarations: dict) -> None:
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


def _add_counts(rows: list[dict], nodes: dict[str, dict]) -> None:
    descendant_indexes: dict[str, set[int]] = {node_id: set() for node_id in nodes}
    for node in nodes.values():
        for index in node["row_indexes"]:
            current = node
            while current is not None:
                descendant_indexes[current["entity_id"]].add(index)
                current = nodes.get(current["parent_id"])
    for node_id, node in nodes.items():
        node["counts"] = session_state_counts(rows, list(descendant_indexes[node_id]))


def session_state_counts(rows: list[dict], row_indexes: list[int]) -> dict[str, int]:
    """Count unique task membership; contradictory observations stay recorded."""
    states: dict[str, set[str]] = {}
    for index in row_indexes:
        row = rows[index]
        key = row.get("entity_id") or f"unresolved:{index}"
        states.setdefault(key, set()).add(row["priority"])
    counts = Counter(next(iter(values)) if len(values) == 1 else "recorded" for values in states.values())
    return {"tasks": len(states), **dict(counts)}
