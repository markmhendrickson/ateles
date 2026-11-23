#!/usr/bin/env python3
"""Console S5 read: the planning tree over a bounded window of tasks.

Design basis: docs/product/console.md#s5--the-hierarchy--the-planning-records-and-the-ascent

This module owns no state and writes nothing. It EXTENDS the workboard's
planning reads rather than re-deriving them:

  * the ascent (one live PART_OF edge upward, with every gap disclosed) is
    ``server._planning_ascent`` -- called, not copied;
  * "working now" is ``server._live_execution_evidence`` -- a held LEASE joined
    to a recent runner session -- and only that; anything else is the recorded
    state, exactly as the workboard shows it;
  * the bucket precedence (operator-needed > blocked > active > recorded) is
    ``server._bucket_for``;
  * per-node state counts are ``workboard_planning.session_state_counts``;
  * level labels come from the registry's planning declarations
    (``server._planning_registry``). No ladder of type names is hardcoded: when
    the registry read does not qualify, the tree is still drawn from the PART_OF
    edges and says that levels are unknown.

BOUNDED, ON PURPOSE. Hosted Neotoma holds tens of thousands of tasks and every
ascent is a relationship read. The read therefore takes the most recently
touched ``open_limit`` open tasks and ``done_limit`` done tasks, walks them
sequentially (never more than one read in flight, well under the two-read
ceiling), and reports the window as coverage (``coverage.open_total`` versus
``coverage.open_read``). A project summary is "within this window", and says so.

CLI (used by the dashboard server)::

    python3 -m execution.mcp.ateles.planning_tree --open-statuses open,pending
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from execution.mcp.ateles import server as S  # noqa: E402
from execution.mcp.ateles.workboard_planning import session_state_counts  # noqa: E402

DONE_STATUSES = ("done", "completed", "verified", "closed")
CUSTOMER_TYPES = ("organization", "company")
NO_PARENT_PREFIX = "No planning parent is recorded"
MULTI_PARENT_MARK = "Multiple planning parents"


def _record_label(record: dict | None, fallback: str) -> str:
    """Planning records name themselves `name`, `title` or only a canonical name."""
    snap = S._as_snapshot(record)
    for key in ("title", "name"):
        value = S._text(snap.get(key))
        if value:
            return value
    canonical = S._text((record or {}).get("canonical_name"))
    if canonical:
        return canonical.split(":", 1)[1].strip() if ":" in canonical else canonical
    return fallback


def classify_gaps(gaps: list[str], ascent: list[dict]) -> tuple[str, str | None]:
    """Return (placement, reason). placement: planned | unplanned | held | unknown.

    Unplanned is a design word, not an error: a task with no PART_OF edge. Two
    parents is a held case the design surfaces for judgement, never resolved by
    picking one. Anything else (budget, unreadable parent) is unknown coverage.
    """
    if not gaps and ascent:
        return "planned", None
    reason = "; ".join(gaps) if gaps else "The ascent was empty without a stated reason"
    if any(g.startswith(NO_PARENT_PREFIX) for g in gaps) and not ascent:
        return "unplanned", reason
    if any(MULTI_PARENT_MARK in g for g in gaps):
        return "held", reason
    return "unknown", reason


def assemble_tree(
    task_rows: list[dict],
    *,
    declarations: dict[str, dict],
    registry_complete: bool,
    node_labels: dict[str, dict],
    customers: dict[str, list[dict]],
    sessions: list[dict],
) -> dict:
    """Pure assembly of nodes, overlay, project summaries and the unplanned list.

    ``task_rows`` carry: entity_id, title, status, bucket, ascent, planning_gaps,
    working (dict or None). ``node_labels`` maps an ascent record id to
    {entity_type, title}. Nothing here reads a store.
    """
    nodes: dict[str, dict] = {}
    unplanned: list[dict] = []
    conflicts: list[str] = []

    def put(entity_id: str, entity_type: str, title: str, parent_id: str | None, **extra):
        mark = declarations.get(entity_type)
        existing = nodes.get(entity_id)
        if existing is not None:
            if existing["parent_id"] != parent_id:
                conflicts.append(entity_id)
            return
        nodes[entity_id] = {
            "entity_id": entity_id,
            "entity_type": entity_type,
            "title": title,
            "level": mark["level"] if mark else None,
            "parent_id": parent_id,
            "task_ids": [],
            **extra,
        }

    planned_rows: list[dict] = []
    for row in task_rows:
        placement, reason = classify_gaps(row["planning_gaps"], row["ascent"])
        if placement != "planned":
            unplanned.append(
                {
                    "entity_id": row["entity_id"],
                    "title": row["title"],
                    "status": row["status"],
                    "bucket": row["bucket"],
                    "placement": placement,
                    "reason": reason,
                }
            )
            continue
        planned_rows.append(row)
        chain = [
            {"entity_id": row["entity_id"], "entity_type": "task", "title": row["title"]}
        ] + [
            {
                "entity_id": p["entity_id"],
                "entity_type": p["entity_type"],
                "title": node_labels.get(p["entity_id"], {}).get("title") or p["title"],
            }
            for p in row["ascent"]
        ]
        for i, rec in enumerate(chain):
            parent = chain[i + 1]["entity_id"] if i + 1 < len(chain) else None
            extra = (
                {"status": row["status"], "bucket": row["bucket"], "working": row["working"]}
                if i == 0
                else {}
            )
            put(rec["entity_id"], rec["entity_type"], rec["title"], parent, **extra)

    # Descendant task membership per node (a record reached by two tasks counts
    # each once).
    by_task = {r["entity_id"]: r for r in planned_rows}
    count_rows = [{"entity_id": r["entity_id"], "priority": r["bucket"]} for r in planned_rows]
    index_of = {r["entity_id"]: i for i, r in enumerate(planned_rows)}
    for task_id in by_task:
        current = nodes.get(task_id)
        seen: set[str] = set()
        while current is not None and current["entity_id"] not in seen:
            seen.add(current["entity_id"])
            current["task_ids"].append(task_id)
            current = nodes.get(current["parent_id"]) if current["parent_id"] else None

    for node in nodes.values():
        indexes = [index_of[t] for t in node["task_ids"]]
        counts = session_state_counts(count_rows, indexes) if indexes else {"tasks": 0}
        done = counts.get("done", 0)
        working = sum(1 for t in node["task_ids"] if by_task[t]["working"])
        node["counts"] = {
            "tasks": counts["tasks"],
            "open": counts["tasks"] - done,
            "done": done,
            "blocked": counts.get("blocked", 0),
            "operator": counts.get("operator-needed", 0),
            "working": working,
        }
        node["children"] = []

    for node in nodes.values():
        parent = nodes.get(node["parent_id"]) if node["parent_id"] else None
        if parent is not None:
            parent["children"].append(node["entity_id"])
    for node in nodes.values():
        node["children"].sort(
            key=lambda c: (-nodes[c]["counts"]["working"], -nodes[c]["counts"]["open"], nodes[c]["title"])
        )
        del node["task_ids"]

    # Reverse index: recorded sessions per node. Native session rows are a
    # bounded recorded page; a link is NOT evidence of a held lease.
    session_index: dict[str, list[dict]] = {}
    for s in sessions:
        target = s.get("native_task_id")
        if isinstance(target, str) and target in nodes:
            session_index.setdefault(target, []).append(
                {
                    "entity_id": s.get("entity_id"),
                    "title": s.get("work"),
                    "harness": s.get("native_harness"),
                    "state": s.get("state"),
                    "last_activity_at": s.get("native_last_activity_at"),
                }
            )
    for node_id, node in nodes.items():
        node["sessions"] = session_index.get(node_id, [])

    projects = []
    for node in nodes.values():
        if node["entity_type"] == "project":
            projects.append(
                {
                    "entity_id": node["entity_id"],
                    "title": node["title"],
                    "customers": customers.get(node["entity_id"], []),
                    **node["counts"],
                    "sessions_linked": sum(
                        len(nodes[t]["sessions"]) for t in _descendants(nodes, node["entity_id"])
                    ),
                }
            )
    projects.sort(key=lambda p: (-p["working"], -p["operator"], -p["open"], p["title"]))

    return {
        "nodes": list(nodes.values()),
        "roots": [n["entity_id"] for n in nodes.values() if n["parent_id"] is None],
        "projects": projects,
        "unplanned": unplanned,
        "conflicts": sorted(set(conflicts)),
        "levels_known": registry_complete and bool(declarations),
    }


def _descendants(nodes: dict[str, dict], root: str) -> list[str]:
    out, stack = [], [root]
    while stack:
        current = stack.pop()
        out.append(current)
        stack.extend(nodes[current]["children"])
    return out


def _task_row(entity: dict, cache: dict, awaiting: set[str], coverage_gaps: list[str]) -> dict | None:
    task_id = entity.get("entity_id")
    if not isinstance(task_id, str):
        return None
    snap = S._as_snapshot(entity)
    status = (S._text(snap.get("status")) or "unknown").lower()
    gaps: list[str] = []
    ascent, planning_gaps = S._planning_ascent(task_id, cache)
    terminal = status in S._TERMINAL_TASK_STATES
    live: dict = {}
    if not terminal and snap.get("admission_class") == "workflow_entering":
        live = S._live_execution_evidence({}, task_id, cache, gaps)
    if terminal:
        bucket = "done" if status in DONE_STATUSES else "terminal-other"
    elif task_id in awaiting:
        bucket = "operator-needed"
    else:
        bucket = S._bucket_for(snap, {}, status, live, None, gaps)
    return {
        "entity_id": task_id,
        "title": S._text(snap.get("title")) or task_id,
        "status": status,
        "bucket": bucket,
        "ascent": ascent,
        "planning_gaps": planning_gaps,
        "read_gaps": gaps,
        "working": (
            {"lease_holder": live.get("lease_holder"), "runner": live.get("runner")}
            if live
            else None
        ),
    }


def _customers(project_ids: list[str], cache: dict) -> dict[str, list[dict]]:
    """A project REFERS_TO its customer organization (outgoing, live)."""
    out: dict[str, list[dict]] = {}
    for pid in project_ids:
        rels, error = S._workboard_relationships(pid, cache)
        if error or rels is None:
            continue
        found = []
        for target in dict.fromkeys(
            r.get("target_entity_id")
            for r in rels
            if r.get("source_entity_id") == pid
            and str(r.get("relationship_type", "")).upper() == "REFERS_TO"
        ):
            if not isinstance(target, str):
                continue
            path = f"/entities/{target}"
            if path not in cache:
                cache[path] = S._get(path)
            record = cache[path]
            if S._entity_type(record) in CUSTOMER_TYPES:
                found.append(
                    {"entity_id": target, "name": _record_label(record, target),
                     "entity_type": S._entity_type(record)}
                )
        if found:
            out[pid] = found
    return out


def build_planning_tree(
    open_statuses: list[str],
    *,
    open_limit: int = 60,
    done_limit: int = 20,
    deadline_s: float = 240.0,
) -> dict:
    started = time.monotonic()
    cache: dict = {}
    coverage_gaps: list[str] = []

    declarations, registry_complete, registry_gaps = S._planning_registry(cache)

    def page(statuses, limit):
        return S._retrieve_page(
            "task",
            snapshot_filters={"status": {"op": "in", "value": list(statuses)}},
            limit=limit,
        )

    open_page = page(open_statuses, open_limit)
    if open_page is None:
        return {"error": "Open tasks could not be read"}
    done_page = page(DONE_STATUSES, done_limit) if done_limit > 0 else {"entities": [], "total": None}
    if done_page is None:
        coverage_gaps.append("Done tasks could not be read; done counts are unknown")
        done_page = {"entities": [], "total": None}

    awaiting: set[str] = set()
    cp = S._retrieve_page(
        "checkpoint_brief",
        snapshot_filters={"status": {"op": "eq", "value": "awaiting_operator"}},
        limit=200,
    )
    operator_complete = False
    if cp is None:
        coverage_gaps.append("Open operator checkpoints could not be read; waiting-on-operator counts are unknown")
    else:
        for e in cp["entities"]:
            ref = S._as_snapshot(e).get("task_entity_id")
            if isinstance(ref, str):
                awaiting.add(ref)
        operator_complete = isinstance(cp.get("total"), int) and cp["total"] <= len(cp["entities"])
        if not operator_complete:
            coverage_gaps.append(
                "Open operator checkpoints exceed the read bound; waiting-on-operator counts may be low"
            )

    sessions, session_gaps, session_cov = S._workboard_session_rows()
    coverage_gaps.extend(session_gaps)

    rows: list[dict] = []
    truncated = False
    for entity in open_page["entities"] + done_page["entities"]:
        if time.monotonic() - started > deadline_s:
            truncated = True
            break
        row = _task_row(entity, cache, awaiting, coverage_gaps)
        if row:
            rows.append(row)
    if truncated:
        coverage_gaps.append("The read deadline passed; some tasks in the window were not read")

    labels: dict[str, dict] = {}
    for path, record in list(cache.items()):
        if path.startswith("/entities/") and "/" not in path[len("/entities/"):] and isinstance(record, dict):
            rid = path[len("/entities/"):]
            labels[rid] = {"title": _record_label(record, rid)}

    tree_preview = assemble_tree(
        rows, declarations=declarations, registry_complete=registry_complete,
        node_labels=labels, customers={}, sessions=sessions,
    )
    project_ids = [n["entity_id"] for n in tree_preview["nodes"] if n["entity_type"] == "project"]
    customers = _customers(project_ids, cache)
    tree = assemble_tree(
        rows, declarations=declarations, registry_complete=registry_complete,
        node_labels=labels, customers=customers, sessions=sessions,
    )

    return {
        "ok": True,
        "version": 1,
        "scope": "A bounded window of recently touched tasks, not whole-instance completion",
        **tree,
        "coverage": {
            "open_total": open_page.get("total"),
            "open_read": len(open_page["entities"]),
            "done_total": done_page.get("total"),
            "done_read": len(done_page["entities"]),
            "tasks_walked": len(rows),
            "truncated": truncated,
            "registry_complete": registry_complete,
            "registry_gaps": registry_gaps,
            "operator_checkpoints_complete": operator_complete,
            "sessions": session_cov,
            "gaps": coverage_gaps,
            "elapsed_s": round(time.monotonic() - started, 1),
        },
        "working_rule": (
            "Working now only from a held lease plus recent runner activity; "
            "every other state is the recorded state."
        ),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--open-statuses", required=True, help="comma-separated open task statuses")
    ap.add_argument("--open-limit", type=int, default=60)
    ap.add_argument("--done-limit", type=int, default=20)
    ap.add_argument("--deadline-s", type=float, default=240.0)
    args = ap.parse_args(argv)
    statuses = [s for s in args.open_statuses.split(",") if s]
    result = build_planning_tree(
        statuses,
        open_limit=max(1, min(args.open_limit, 200)),
        done_limit=max(0, min(args.done_limit, 100)),
        deadline_s=args.deadline_s,
    )
    sys.stdout.write(json.dumps(result, default=str) + "\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
