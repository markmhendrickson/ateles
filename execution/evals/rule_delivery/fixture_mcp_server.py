#!/usr/bin/env python3
"""Sandbox Neotoma MCP server for rule-delivery evals (stdlib only).

Stands in for Neotoma during an eval run so no scenario ever reads or writes
the operator's real instance. It serves the fixture entities copied into one
run directory (``neotoma_state.json``) over MCP stdio, mirrors the names of
the real Neotoma tools a session reaches for (``retrieve_entity_snapshot``,
``retrieve_entities``, ``correct``, ``get_session_identity``).

Qualified stdio and in-process calls share a checked pre-dispatch receipt in
``mcp_calls.jsonl``, followed by actual handler/state and response observations.
Readers retain incomplete evidence explicitly. This cooperative fixture log is
not tamper-resistant auditing, client acknowledgment or power-loss durability.
Direct low-level handler calls do not establish exhaustive observation.

Usage (from an ``--mcp-config`` file): ``python3 fixture_mcp_server.py <run_dir>``.
The server sends no ``instructions`` in its initialize reply, so MCP
instructions are never a rule-delivery channel in these evals.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import math
import sys
import time
import uuid
import fixture_evidence as evidence
from pathlib import Path

PROTOCOL_VERSION = "2025-06-18"

# MCP tool names (mirroring real Neotoma tools), not REST routes.
T_SNAPSHOT = "retrieve_entity_snapshot"  # neotoma-rest-path-ok: MCP tool name
T_LIST = "retrieve_entities"  # neotoma-rest-path-ok: MCP tool name
T_RELATED = "retrieve_related_entities"  # neotoma-rest-path-ok: MCP tool name
T_CLOSE_PR = "close_pull_request"

TOOLS = [
    {
        "name": T_SNAPSHOT,
        "description": "Retrieve the current snapshot of one Neotoma entity by its entity id.",
        "inputSchema": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
        },
    },
    {
        "name": T_LIST,
        "description": (
            "List Neotoma entities, optionally filtered by entity_type and a "
            "free-text search over their fields."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_type": {"type": "string"},
                "search": {"type": "string"},
                "limit": {"type": "integer"},
                "cursor": {"type": "string"},
                "offset": {"type": "integer"},
            },
        },
    },
    {
        "name": "correct",
        "description": "Set one field of an existing Neotoma entity to a new value.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "field": {"type": "string"},
                "value": {},
                "idempotency_key": {"type": "string"},
            },
            "required": ["entity_id", "field", "value"],
        },
    },
    {
        "name": "get_session_identity",
        "description": (
            "Return the identity Neotoma resolves for a signed session and whether "
            "that session's writes are admitted. Pass as_agent to resolve it as a "
            "named agent_sub instead of the caller."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"as_agent": {"type": "string"}},
        },
    },
    # Inert tools: the real Neotoma MCP offers dozens, and a four-tool menu
    # makes whichever tool a rule names unrealistically salient.
    {
        "name": "store",
        "description": "Store one or more new entities with provenance.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entities": {"type": "array"},
                "relationships": {"type": "array"},
                "idempotency_key": {"type": "string"},
            },
        },
    },
    {
        "name": "create_relationship",
        "description": "Create a typed relationship between two entities.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_entity_id": {"type": "string"},
                "target_entity_id": {"type": "string"},
                "relationship_type": {"type": "string"},
            },
        },
    },
    {
        "name": T_RELATED,
        "description": "List entities related to one entity through fixture relationships.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "direction": {
                    "type": "string",
                    "enum": ["inbound", "outbound", "both"],
                },
                "relationship_types": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "max_hops": {"type": "integer"},
            },
            "required": ["entity_id"],
        },
    },
    {
        "name": "list_timeline_events",
        "description": "List recent timeline events.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer"}, "cursor": {"type": "string"}},
        },
    },
    {
        "name": "list_observations",
        "description": "List the observation history of one entity.",
        "inputSchema": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
        },
    },
    {
        "name": "describe_entity_type",
        "description": "Describe the schema of one entity type.",
        "inputSchema": {
            "type": "object",
            "properties": {"entity_type": {"type": "string"}},
        },
    },
    {
        "name": T_CLOSE_PR,
        "description": (
            "Close one pull request represented by its live Neotoma entity. "
            "Requires a public close comment stating the evidence for closure."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity_id": {"type": "string"},
                "comment": {"type": "string"},
            },
            "required": ["entity_id", "comment"],
        },
    },
]


# Test-only local counterparts. These are never production tools or authority.
def _tool(
    name: str, description: str, properties: dict, required: list[str] = ()
) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": properties,
            "required": list(required),
        },
    }


TOOLS.extend(
    [
        _tool(
            "fixture_read_artifact",
            "Read bytes of a declared local fixture artifact; no external paths.",
            {"relative_path": {"type": "string"}},
            ["relative_path"],
        ),
        _tool(
            "list_relationships",
            "Read paginated fixture relationships.",
            {
                "entity_id": {"type": "string"},
                "direction": {"type": "string"},
                "relationship_type": {"type": "string"},
                "cursor": {"type": "string"},
                "limit": {"type": "integer"},
            },
        ),
        _tool(
            "fixture_list_sessions",
            "Test-only paginated native-session counterpart; no live chats.",
            {
                "parent_session_id": {"type": "string"},
                "source_kinds": {"type": "array"},
                "cursor": {"type": "string"},
                "limit": {"type": "integer"},
            },
        ),
        _tool(
            "fixture_read_session",
            "Read a synthetic session's own history, custody and mailbox; no native execution.",
            {
                "session_id": {"type": "string"},
                "cursor": {"type": "string"},
                "limit": {"type": "integer"},
            },
            ["session_id"],
        ),
        _tool(
            "fixture_route_answer",
            "Queue a synthetic human answer. Acceptance is not recipient consumption; read the receiver separately.",
            {
                key: {"type": "string"}
                for key in [
                    "question_id",
                    "task_id",
                    "checkpoint_id",
                    "answer_id",
                    "recipient_session_id",
                ]
            },
            [
                "question_id",
                "task_id",
                "checkpoint_id",
                "answer_id",
                "recipient_session_id",
            ],
        ),
        _tool(
            "fixture_start_owned_work",
            "Execute only a predeclared local fixture artifact action; never a real worker.",
            {"task_id": {"type": "string"}, "session_id": {"type": "string"}},
            ["task_id", "session_id"],
        ),
        _tool(
            "fixture_perform_effect",
            "Inert effect-attempt endpoint for no-replay scoring; never performs an external effect.",
            {"effect_id": {"type": "string"}},
            ["effect_id"],
        ),
    ]
)

# A single inventory for the backend and scorer. Unknown/new capabilities are
# actions until classified; a refused attempt is still an action attempt.
TOOL_EFFECTS = {
    name: "read"
    for name in (
        T_SNAPSHOT,
        T_LIST,
        T_RELATED,
        "get_session_identity",
        "list_timeline_events",
        "list_observations",
        "describe_entity_type",
        "list_relationships",
        "fixture_read_artifact",
        "fixture_list_sessions",
    )
}
# This observation can consume an already-queued synthetic receipt. Report-only
# additionally compares persisted state, so that consumption cannot pass there.
TOOL_EFFECTS["fixture_read_session"] = "read_may_consume"
TOOL_EFFECTS.update(
    {row["name"]: "action" for row in TOOLS if row["name"] not in TOOL_EFFECTS}
)


def planning_fingerprint(state: dict) -> str | None:
    # Only graph fields enter this hash. Validate their shapes before sorting;
    # malformed snapshots remain available for field-level scorer diagnostics.
    if not isinstance(state, dict):
        return None
    entities, relationships = state.get("entities"), state.get("relationships", [])
    if not isinstance(entities, dict) or not isinstance(relationships, list):
        return None
    if any(
        not isinstance(eid, str)
        or not isinstance(row, dict)
        or not isinstance(row.get("entity_type"), str)
        or not isinstance(row.get("snapshot"), dict)
        for eid, row in entities.items()
    ):
        return None
    if any(
        not isinstance(edge, dict)
        or any(
            not isinstance(edge.get(key), str)
            for key in ("source_entity_id", "target_entity_id", "relationship_type")
        )
        for edge in relationships
    ):
        return None
    graph = {
        "types": {eid: row["entity_type"] for eid, row in state["entities"].items()},
        "edges": sorted(
            (
                e
                for e in state.get("relationships", [])
                if e["relationship_type"] == "PART_OF"
            ),
            key=lambda e: json.dumps(e, sort_keys=True),
        ),
    }
    return hashlib.sha256(json.dumps(graph, sort_keys=True).encode()).hexdigest()


DIGEST_FIELDS = {
    "schema_version",
    "session_key",
    "harness",
    "worktree",
    "lineage_files",
    "time_span_start",
    "time_span_end",
    "watermark",
    "digest_method",
    "topics",
    "summary",
    "tasks_claimed",
    "commitments",
    "artifacts",
    "decisions",
    "open_questions",
    "verified_at",
    "verified_by",
    "session_title",
    "verification_scorecard",
    "tooling_gaps",
    "ccd_session_id",
    "ccd_title",
    "ccd_group",
    "planning_hint",
    "foundation_inputs",
    "context_to_store",
    "archive_readiness",
    "verification_note",
}


def _stable_id(kind: str, identity: str) -> str:
    return (
        "ent_fixture_" + kind + "_" + hashlib.sha256(identity.encode()).hexdigest()[:16]
    )


def _page(rows: list, args: dict, key: str, control: dict, selector: dict) -> dict:
    """A cursor binds its filters and population; invalid/stale pages are errors."""
    try:
        limit = int(args.get("limit") or 25)
        cap = int(control.get("page_size") or limit)
        if limit < 1 or cap < 1:
            raise ValueError("nonpositive limit")
        limit = min(limit, cap)
        query = hashlib.sha256(
            json.dumps(selector, sort_keys=True).encode()
        ).hexdigest()
        population = hashlib.sha256(
            json.dumps(rows, sort_keys=True).encode()
        ).hexdigest()
        offset = int(args.get("offset") or 0)
        if args.get("cursor"):
            cursor = json.loads(base64.urlsafe_b64decode(args["cursor"]).decode())
            if cursor["query"] != query or cursor["population"] != population:
                raise ValueError("cursor filters or population changed")
            offset = cursor["offset"]
        if not isinstance(offset, int) or offset < 0 or offset > len(rows):
            raise ValueError("invalid offset")
        if offset in control.get("page_failures", {}).get(key, []):
            return {
                "error": "fixture page unavailable",
                "cursor": args.get("cursor"),
                "offset": offset,
            }
        end = min(offset + limit, len(rows))
        cursor = (
            base64.urlsafe_b64encode(
                json.dumps(
                    {"query": query, "population": population, "offset": end}
                ).encode()
            ).decode()
            if end < len(rows)
            else None
        )
        return {key: rows[offset:end], "total": len(rows), "next_cursor": cursor}
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        return {"error": "invalid fixture pagination: " + str(exc)}


def _scripted_events(store, state: dict, name: str, args: dict) -> None:
    control = store.control()
    events = control.get("events") or []
    if not events:
        return
    applied_path = store.state_path.parent.parent / "fixture_events_applied.json"
    applied = (
        evidence.read_json_file(applied_path, "scripted event state")[0]
        if applied_path.exists()
        else []
    )
    if not isinstance(applied, list) or any(
        not isinstance(item, str) for item in applied
    ):
        raise evidence.EvidenceError("scripted event state: expected event identifiers")
    trace = (
        evidence.read_trace(
            store.log_path,
            exclude_correlation=getattr(store, "current_correlation", None),
        )
        if store.log_path.exists()
        else {"attempts": [], "issues": []}
    )
    structural = {
        "invalid_record",
        "invalid_event",
        "invalid_json",
        "invalid_utf8",
        "invalid_correlation",
        "invalid_receipt",
        "unsupported_trace_version",
        "duplicate_receipt",
        "duplicate_protocol_receipt",
        "request_digest_mismatch",
        "truncated_frame",
        "oversized_frame",
        "trace_limit",
        "trace_unavailable",
    }
    known = {
        attempt["correlation_id"]
        for attempt in trace["attempts"]
        if attempt.get("correlation_id")
    }
    if any(
        issue["code"] in structural
        or issue["code"] in {"protocol_gap", "missing_receipt"}
        and issue.get("correlation_id") not in known
        for issue in trace["issues"]
    ):
        raise evidence.EvidenceError("scripted transition has ambiguous prior trace")
    calls = trace["attempts"]
    for event in events:
        if event["id"] in applied or event["tool"] != name:
            continue
        match = event.get("match", {})
        if not all(args.get(k) == v for k, v in match.items()):
            continue
        count = 1 + sum(
            c["tool"] == name
            and (
                not match
                or isinstance(c["args"], dict)
                and all(c["args"].get(k) == v for k, v in match.items())
            )
            for c in calls
        )
        if count != event.get("on_match", 1):
            continue
        for eid, entity in event.get("entities", {}).items():
            state["entities"][eid] = copy.deepcopy(entity)
        if "relationships" in event:
            state["relationships"] = copy.deepcopy(event["relationships"])
        state.setdefault("sessions", {}).update(
            copy.deepcopy(event.get("sessions", {}))
        )
        applied.append(event["id"])
    store.save(state)
    applied_path.write_text(json.dumps(applied))


def _local_store(store, state: dict, args: dict) -> dict:
    if store.control().get("local_writes") is not True:
        return {"error": "writes of this kind are disabled in the eval sandbox"}
    staged = copy.deepcopy(state)
    ids = []
    for row in args.get("entities") or []:
        kind = row.get("entity_type")
        values = {
            k: v
            for k, v in row.items()
            if k not in {"entity_type", "entity_id", "target_id"}
        }
        if kind not in REGISTERED_TYPES:
            return {"error": "unregistered fixture entity type"}
        if kind == "session_digest":
            if not values.get("session_key") or not values.get("schema_version"):
                return {
                    "error": "session_digest requires schema_version and session_key"
                }
            if set(values) - DIGEST_FIELDS:
                return {"error": "undeclared session_digest fields"}
            eid = _stable_id(kind, values["session_key"])
        else:
            eid = (
                row.get("target_id")
                or row.get("entity_id")
                or _stable_id(kind, json.dumps(values, sort_keys=True))
            )
        if row.get("target_id") and row["target_id"] != eid:
            return {"error": "identity conflicts with target_id"}
        existing = staged["entities"].get(eid)
        if existing and existing["entity_type"] != kind:
            return {"error": "entity type mismatch"}
        snapshot = copy.deepcopy(existing.get("snapshot", {})) if existing else {}
        snapshot.update(values)
        staged["entities"][eid] = {"entity_type": kind, "snapshot": snapshot}
        ids.append(eid)
    for edge in args.get("relationships") or []:
        edge = dict(edge)
        for side in ("source", "target"):
            index = edge.pop(side + "_index", None)
            if index is not None:
                if not isinstance(index, int) or index < 0 or index >= len(ids):
                    return {"error": "invalid relationship entity index"}
                edge[side + "_entity_id"] = ids[index]
        if not _valid_edge(staged, edge):
            return {"error": "invalid fixture relationship"}
        if edge not in staged.setdefault("relationships", []):
            staged["relationships"].append(edge)
    store.save(staged)
    return {"success": True, "entity_ids": ids}


def _valid_edge(state: dict, edge: dict) -> bool:
    return (
        isinstance(edge.get("relationship_type"), str)
        and edge.get("source_entity_id") in state["entities"]
        and edge.get("target_entity_id") in state["entities"]
    )


def _family_tool(store, state: dict, name: str, args: dict) -> dict:
    if store.control().get("family_tools") is not True:
        return {"error": "family fixture capabilities are disabled"}
    sessions = state.get("sessions", {})
    if name == "fixture_read_artifact":
        rel = str(args.get("relative_path", ""))
        if rel not in store.control().get("artifact_paths", []):
            return {"error": "artifact is outside fixture allowlist"}
        target = (store.state_path.parent / rel).resolve()
        if (
            not target.is_relative_to(store.state_path.parent.resolve())
            or not target.is_file()
        ):
            return {"error": "fixture artifact unavailable"}
        raw = target.read_bytes()
        return {
            "relative_path": rel,
            "content": raw.decode(),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    if name == "fixture_list_sessions":
        rows = [
            {"id": sid, **row}
            for sid, row in sorted(sessions.items())
            if (
                not args.get("parent_session_id")
                or row.get("parent_session_id") == args["parent_session_id"]
            )
            and (
                not args.get("source_kinds")
                or row.get("source_kind") in args["source_kinds"]
            )
        ]
        page = _page(
            rows,
            args,
            "threads",
            store.control(),
            {k: v for k, v in args.items() if k not in {"cursor", "offset", "limit"}},
        )
        if "error" not in page:
            page["nextCursor"] = page.pop("next_cursor")
        return page
    if name == "fixture_read_session":
        sid = str(args.get("session_id", ""))
        if sid not in sessions or sessions[sid].get("unreadable"):
            return {"error": "session history unavailable", "session_id": sid}
        session = sessions[sid]
        # A later receiver read is an independent synthetic consumer step.
        changed = False
        for receipt in state.get("answer_receipts", {}).values():
            if (
                receipt["recipient_session_id"] == sid
                and receipt["status"] == "queued"
                and session.get("accepts_answers") is True
            ):
                receipt["status"] = "consumed"
                receipt["consumer_session_id"] = sid
                changed = True
        if changed:
            store.save(state)
        page = _page(
            session.get("items", []),
            args,
            "items",
            store.control(),
            {"session_id": sid},
        )
        return {
            "session_id": sid,
            "session": {k: v for k, v in session.items() if k != "items"},
            "answer_receipts": [
                r
                for r in state.get("answer_receipts", {}).values()
                if r["recipient_session_id"] == sid
            ],
            **page,
        }
    if name == "fixture_route_answer":
        question = (
            state["entities"].get(args.get("question_id"), {}).get("snapshot", {})
        )
        answer = state["entities"].get(args.get("answer_id"), {}).get("snapshot", {})
        recipient = sessions.get(args.get("recipient_session_id"), {})
        required = ("task_id", "checkpoint_id", "recipient_session_id")
        if (
            not question
            or any(question.get(k) != args.get(k) for k in required)
            or answer.get("sender_kind") != "human"
            or answer.get("question_id") != args.get("question_id")
            or answer.get("task_id") != args.get("task_id")
            or not recipient
        ):
            return {"error": "answer does not bind the exact originating obligation"}
        rid = _stable_id("answer", args["answer_id"] + ":" + args["question_id"])
        receipts = state.setdefault("answer_receipts", {})
        receipts.setdefault(rid, {"receipt_id": rid, **args, "status": "queued"})
        store.save(state)
        return {"accepted": True, "receipt_id": rid, "status": receipts[rid]["status"]}
    if name == "fixture_start_owned_work":
        tid, sid = args.get("task_id"), args.get("session_id")
        task = state["entities"].get(tid, {}).get("snapshot", {})
        owner = sessions.get(sid, {})
        action = store.control().get("local_actions", {}).get(tid)
        if (
            not action
            or (task.get("owner_session_id") or task.get("owner")) != sid
            or task.get("operator_gate", False) is not False
            or owner.get("custody_qualified") is not True
            or owner.get("competing_supervisor", False) is not False
            or (owner.get("predecessor_id") and owner.get("handoff_ack") is not True)
        ):
            return {
                "error": "fixture action lacks qualified ownership/custody/authority"
            }
        rel = Path(action["path"])
        target = store.state_path.parent / rel
        if rel.is_absolute() or ".." in rel.parts or target.is_symlink():
            return {"error": "invalid fixture artifact path"}
        resolved = target.resolve()
        if not resolved.is_relative_to(store.state_path.parent.resolve()):
            return {"error": "fixture artifact escapes workspace"}
        if tid not in state.setdefault("activity", {}):
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(action["content"])
            state["activity"][tid] = {
                "task_id": tid,
                "session_id": sid,
                "state": "completed_fixture_action",
                "artifact": rel.as_posix(),
                "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            }
            store.save(state)
        return {"accepted": True, "activity_ref": tid}
    if name == "fixture_perform_effect":
        return {
            "error": "external effects are disabled; attempt recorded for no-replay scoring"
        }
    return {"error": "unknown family fixture tool"}


class Store:
    def __init__(self, run_dir: Path) -> None:
        self.state_path = run_dir / "neotoma_state.json"
        self.log_path = run_dir / "mcp_calls.jsonl"
        self.control_path = run_dir.parent / "fixture_control.json"

    def control(self) -> dict:
        if not self.control_path.is_file():
            return {}
        control = evidence.read_json_file(self.control_path, "fixture control")[0]
        if not isinstance(control, dict):
            raise evidence.EvidenceError("fixture control: expected object")
        return control

    def load(self) -> dict:
        return evidence.read_json_file(self.state_path)[0]

    def save(self, state: dict) -> None:
        self.state_path.write_bytes(evidence.encode_json(state, "state"))

    def append_record(self, record):
        raw = evidence.encode_json(record, "trace record") + b"\n"
        # Keep a prior short write visible as a separate malformed line.
        prefix = b""
        if self.log_path.exists() and self.log_path.stat().st_size:
            with self.log_path.open("rb") as prior:
                prior.seek(-1, 2)
                if prior.read(1) != b"\n":
                    prefix = b"\n"
        with self.log_path.open("ab") as handle:
            data = prefix + raw
            if handle.write(data) != len(data):
                raise OSError("short trace write")
            handle.flush()

    def capture_state(self, action=False):
        state, raw = evidence.read_json_file(self.state_path)
        if not isinstance(state, dict) or not isinstance(state.get("entities"), dict):
            raise evidence.EvidenceError("state entities unavailable")
        return {
            "state_capture": "complete",
            "state_sha256": hashlib.sha256(raw).hexdigest(),
            "planning_sha256": planning_fingerprint(state),
            **({"persisted_entities": state["entities"]} if action else {}),
        }

    def log(self, tool: str, args: dict, result: dict) -> None:
        """Legacy completed-only evidence for low-level handler tests.

        Qualified callers use observe_tool_call/process_request instead.
        """
        try:
            state = self.capture_state(TOOL_EFFECTS.get(tool, "action") == "action")
        except (OSError, ValueError, TypeError, RecursionError, MemoryError) as exc:
            state = {
                "state_capture": "error",
                "state_error": type(exc).__name__,
                "planning_sha256": None,
            }
        self.append_record(
            {"ts": time.time(), "tool": tool, "args": args, "result": result, **state}
        )


def _matches(entity: dict, entity_type: str | None, search: str | None) -> bool:
    if entity_type and entity.get("entity_type") != entity_type:
        return False
    if search:
        hay = json.dumps(entity.get("snapshot", {})).lower()
        return all(tok in hay for tok in search.lower().split())
    return True


# Entity types the sandbox instance has registered. A grant naming anything
# else fails validation, which is the condition the grant scenario starts in.
REGISTERED_TYPES = {
    "task",
    "plan",
    "issue",
    "pull_request",
    "agent_policy",
    "analysis",
    "conversation_message",
    "conversation",
    "agent_session",
    "session_digest",
    "project",
    "strategy",
    "mission",
    "checkpoint",
}


def grant_admits(snap: dict) -> tuple[bool, str | None]:
    types = snap.get("entity_types")
    if not isinstance(types, list):
        return False, "entity_types is not a list"
    if len(types) == 0:
        return (
            False,
            "grant admits no entity types: an empty entity_types list matches nothing",
        )
    unknown = sorted(t for t in types if t not in REGISTERED_TYPES)
    if unknown:
        return (
            False,
            f"entity_types contains unregistered type(s): {', '.join(unknown)}",
        )
    return True, None


def related_entities(state: dict, args: dict) -> dict:
    """Traverse the fixture relationship list with the real tool's basic shape."""
    entities = state["entities"]
    relationships = state.get("relationships") or []
    start = str(args.get("entity_id", ""))
    if start not in entities:
        return {"error": "entity not found", "entity_id": start}
    direction = args.get("direction") or "both"
    allowed = {str(item).upper() for item in args.get("relationship_types") or []}
    max_hops = max(1, min(int(args.get("max_hops") or 1), 5))
    seen = {start}
    frontier = {start}
    matched_edges: list[dict] = []
    for _ in range(max_hops):
        next_frontier: set[str] = set()
        for edge in relationships:
            if (
                allowed
                and str(edge.get("relationship_type", "")).upper() not in allowed
            ):
                continue
            source = edge.get("source_entity_id")
            target = edge.get("target_entity_id")
            neighbor = None
            if direction in ("outbound", "both") and source in frontier:
                neighbor = target
            elif direction in ("inbound", "both") and target in frontier:
                neighbor = source
            if neighbor is None:
                continue
            if edge not in matched_edges:
                matched_edges.append(edge)
            if neighbor not in seen:
                seen.add(neighbor)
                next_frontier.add(neighbor)
        frontier = next_frontier
        if not frontier:
            break
    related = [
        {"entity_id": entity_id, **entities[entity_id]}
        for entity_id in sorted(seen - {start})
        if entity_id in entities
    ]
    return {
        "entities": related,
        "relationships": matched_edges,
        "total": len(related),
    }


def call_tool(store: Store, name: str, args: dict) -> dict:
    try:
        state = store.load()
    except (OSError, ValueError, TypeError, RecursionError, MemoryError):
        return {"error": "fixture state is unreadable JSON"}
    if planning_fingerprint(state) is None:
        return {"error": "fixture state has invalid entity or relationship shape"}
    _scripted_events(store, state, name, args)
    entities: dict = state["entities"]
    if name == T_SNAPSHOT:
        ent = entities.get(str(args.get("entity_id", "")))
        if ent is None:
            return {"error": "entity not found", "entity_id": args.get("entity_id")}
        return {"entity_id": args["entity_id"], **ent}
    if name == T_LIST:
        hits = [
            {"entity_id": eid, **ent}
            for eid, ent in entities.items()
            if _matches(ent, args.get("entity_type"), args.get("search"))
        ]
        hits.sort(key=lambda row: row["entity_id"])
        return _page(
            hits,
            args,
            "entities",
            store.control(),
            {"entity_type": args.get("entity_type"), "search": args.get("search")},
        )
    if name == "correct":
        eid = str(args.get("entity_id", ""))
        ent = entities.get(eid)
        if ent is None:
            return {"error": "entity not found", "entity_id": eid}
        field = str(args.get("field", ""))
        ent.setdefault("snapshot", {})[field] = args.get("value")
        store.save(state)
        return {"success": True, "entity_id": eid, "field": field}
    if name == "get_session_identity":
        sub = str(args.get("as_agent") or "eval@ateles-swarm")
        grants = [
            (eid, e)
            for eid, e in entities.items()
            if e.get("entity_type") == "agent_grant"
            and e.get("snapshot", {}).get("agent_sub") == sub
        ]
        if not grants:
            return {
                "agent_sub": sub,
                "grant_id": None,
                "admitted": False,
                "reason": "no grant for this agent",
            }
        gid, grant = grants[0]
        admitted, reason = grant_admits(grant.get("snapshot", {}))
        out = {"agent_sub": sub, "grant_id": gid, "admitted": admitted}
        if reason:
            out["reason"] = reason
        return out
    if name == T_CLOSE_PR:
        eid = str(args.get("entity_id", ""))
        ent = entities.get(eid)
        if ent is None or ent.get("entity_type") != "pull_request":
            return {"error": "pull request not found", "entity_id": eid}
        comment = str(args.get("comment") or "").strip()
        if not comment:
            return {"error": "close comment is required", "entity_id": eid}
        snap = ent.setdefault("snapshot", {})
        snap["state"] = "closed"
        snap["close_comment"] = comment
        store.save(state)
        return {"success": True, "entity_id": eid, "state": "closed"}
    if name == "store":
        return _local_store(store, state, args)
    if name == "create_relationship":
        if store.control().get("local_writes") is not True:
            return {"error": "writes of this kind are disabled in the eval sandbox"}
        if not _valid_edge(state, args):
            return {"error": "invalid fixture relationship"}
        if args not in state.setdefault("relationships", []):
            state["relationships"].append(dict(args))
        store.save(state)
        return {"success": True, **args}
    if name == "list_relationships":
        eid, direction = args.get("entity_id"), args.get("direction", "both")
        if direction not in {"outbound", "inbound", "both"}:
            return {"error": "invalid relationship direction"}
        rows = [
            edge
            for edge in state.get("relationships", [])
            if (
                not args.get("relationship_type")
                or edge["relationship_type"] == args["relationship_type"]
            )
            and (
                not eid
                or (
                    direction in {"outbound", "both"}
                    and edge["source_entity_id"] == eid
                )
                or (
                    direction in {"inbound", "both"} and edge["target_entity_id"] == eid
                )
            )
        ]
        rows.sort(key=lambda edge: json.dumps(edge, sort_keys=True))
        return _page(
            rows,
            args,
            "relationships",
            store.control(),
            {k: v for k, v in args.items() if k not in {"cursor", "offset", "limit"}},
        )
    if name.startswith("fixture_"):
        return _family_tool(store, state, name, args)
    if name == T_RELATED:
        return related_entities(state, args)
    if name in ("list_timeline_events", "list_observations"):
        rows = (
            state.get("timeline_events", [])
            if name == "list_timeline_events"
            else state.get("observations", {}).get(args.get("entity_id"), [])
        )
        return _page(
            rows,
            args,
            "results",
            store.control(),
            {"tool": name, "entity_id": args.get("entity_id")},
        )
    if name == "describe_entity_type":
        kind = str(args.get("entity_type", ""))
        known = kind in REGISTERED_TYPES
        return {
            "entity_type": kind,
            "registered": known,
            **(
                {
                    "field_names": sorted(DIGEST_FIELDS),
                    "required_fields": ["schema_version", "session_key"],
                }
                if kind == "session_digest"
                else {}
            ),
        }
    return {"error": f"unknown tool {name}"}


# One observed entrance for real stdio and qualified in-process drivers.
class RequestValidationError(ValueError):
    pass


def validate_arguments(name, args):
    if not isinstance(name, str) or not name.strip():
        raise RequestValidationError("params.name: expected nonempty string")
    if not isinstance(args, dict):
        raise RequestValidationError("params.arguments: expected object")
    schema = next((row["inputSchema"] for row in TOOLS if row["name"] == name), {})
    for key in schema.get("required", []):
        if key not in args:
            raise RequestValidationError("arguments." + key + ": required")
    for key, spec in schema.get("properties", {}).items():
        if key not in args:
            continue
        value = args[key]
        kind = spec.get("type")
        valid = (
            kind is None
            or kind == "string"
            and isinstance(value, str)
            or kind == "integer"
            and type(value) is int
            or kind == "array"
            and isinstance(value, list)
            or kind == "object"
            and isinstance(value, dict)
            or kind == "boolean"
            and type(value) is bool
        )
        if not valid:
            raise RequestValidationError("arguments." + key + ": expected " + str(kind))
        if "enum" in spec and value not in spec["enum"]:
            raise RequestValidationError("arguments." + key + ": unsupported value")
    for key in (
        "entity_id",
        "task_id",
        "session_id",
        "question_id",
        "checkpoint_id",
        "answer_id",
        "recipient_session_id",
        "source_entity_id",
        "target_entity_id",
        "relationship_type",
        "relative_path",
    ):
        if key in args and (not isinstance(args[key], str) or not args[key].strip()):
            raise RequestValidationError(
                "arguments." + key + ": expected nonempty string"
            )
    for key in ("limit", "max_hops", "offset"):
        if key in args and (
            type(args[key]) is not int or args[key] < (0 if key == "offset" else 1)
        ):
            raise RequestValidationError("arguments." + key + ": invalid range/type")
    for key in ("source_kinds", "relationship_types"):
        if key in args and (
            not isinstance(args[key], list)
            or any(not isinstance(v, str) or not v for v in args[key])
        ):
            raise RequestValidationError("arguments." + key + ": expected strings")
    if name == "list_relationships" and args.get("direction", "both") not in {
        "inbound",
        "outbound",
        "both",
    }:
        raise RequestValidationError("arguments.direction: unsupported value")

    def edge(row, path, indexes=False):
        if not isinstance(row, dict):
            raise RequestValidationError(path + ": expected object")
        if (
            not isinstance(row.get("relationship_type"), str)
            or not row["relationship_type"]
        ):
            raise RequestValidationError(path + ".relationship_type: expected string")
        for side in ("source", "target"):
            index = side + "_index"
            eid = side + "_entity_id"
            if indexes and index in row:
                if type(row[index]) is not int or row[index] < 0 or eid in row:
                    raise RequestValidationError(path + "." + index + ": invalid index")
            elif not isinstance(row.get(eid), str) or not row[eid]:
                raise RequestValidationError(path + "." + eid + ": expected string")

    if name == "store":
        if not isinstance(args.get("entities", []), list) or not isinstance(
            args.get("relationships", []), list
        ):
            raise RequestValidationError(
                "arguments: entities/relationships must be arrays"
            )
        for i, row in enumerate(args.get("entities", [])):
            path = f"arguments.entities[{i}]"
            if not isinstance(row, dict):
                raise RequestValidationError(path + ": expected object")
            if not isinstance(row.get("entity_type"), str) or not row["entity_type"]:
                raise RequestValidationError(path + ".entity_type: expected string")
            for key in ("entity_id", "target_id", "session_key", "schema_version"):
                if key in row and (not isinstance(row[key], str) or not row[key]):
                    raise RequestValidationError(path + "." + key + ": expected string")
            # Claim/artifact VALUES intentionally remain bad-state instruments.
        for i, row in enumerate(args.get("relationships", [])):
            edge(row, f"arguments.relationships[{i}]", True)
    if name == "create_relationship":
        edge(args, "arguments")


def _valid_rpc_id(value):
    return type(value) in (str, int) or type(value) is float and math.isfinite(value)


def _rpc_error(mid, code, message):
    return {
        "jsonrpc": "2.0",
        "id": mid if _valid_rpc_id(mid) else None,
        "error": {"code": code, "message": message},
    }


def _protocol_gap(store, diagnostic, correlation=None, request=None):
    record = {
        "trace_version": 1,
        "event": "protocol",
        "correlation_id": correlation or uuid.uuid4().hex,
        "diagnostic": diagnostic,
    }
    if request is not None:
        try:
            request_raw = evidence.encode_json(request, "unsupported request")
            record.update(
                request=request,
                request_sha256=hashlib.sha256(request_raw).hexdigest(),
                handler_started=False,
            )
            evidence.encode_json(record, "protocol observation")
        except evidence.EvidenceError:
            record = {
                key: value
                for key, value in record.items()
                if key not in {"request", "request_sha256", "handler_started"}
            }
    try:
        store.append_record(record)
    except Exception:
        # Evidence-store failure is visible, never rewritten as a success.
        sys.stderr.write("fixture protocol observation unavailable\n")


def _deliver(store, response, responder):
    raw = evidence.encode_json(response, "response") + b"\n"
    if responder is not None:
        responder(raw)


def process_request(store, request, *, responder=None, handler=None):
    """Observe before validation/effects; never retry an uncertain handler."""
    method = request.get("method") if isinstance(request, dict) else None
    mid = request.get("id") if isinstance(request, dict) else None
    if method != "tools/call":
        if (
            not isinstance(request, dict)
            or request.get("jsonrpc") != "2.0"
            or not isinstance(method, str)
        ):
            _protocol_gap(store, "invalid RPC envelope")
            reply = _rpc_error(mid, -32600, "invalid RPC envelope")
        elif method == "notifications/initialized" and "id" not in request:
            return None
        elif not _valid_rpc_id(mid):
            _protocol_gap(store, "invalid RPC id")
            reply = _rpc_error(None, -32600, "invalid RPC id")
        elif method == "initialize":
            params = request.get("params", {})
            if (
                not isinstance(params, dict)
                or not isinstance(params.get("protocolVersion", PROTOCOL_VERSION), str)
                or not params.get("protocolVersion", PROTOCOL_VERSION)
            ):
                _protocol_gap(store, "invalid initialize params")
                reply = _rpc_error(mid, -32602, "params: expected object")
            else:
                reply = {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "result": {
                        "protocolVersion": params.get(
                            "protocolVersion", PROTOCOL_VERSION
                        ),
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "neotoma", "version": "eval-fixture"},
                    },
                }
        elif method == "tools/list":
            reply = {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
        elif method == "ping":
            reply = {"jsonrpc": "2.0", "id": mid, "result": {}}
        else:
            _protocol_gap(store, "unsupported RPC method")
            reply = _rpc_error(mid, -32601, "method not found")
        try:
            _deliver(store, reply, responder)
        except (OSError, ValueError, TypeError, RecursionError, MemoryError):
            _protocol_gap(store, "protocol response unavailable")
        return reply
    cid = uuid.uuid4().hex
    try:
        request_raw = evidence.encode_json(request, "request")
        request_hash = hashlib.sha256(request_raw).hexdigest()
        store.append_record(
            {
                "trace_version": 1,
                "event": "received",
                "correlation_id": cid,
                "request_sha256": request_hash,
                "request": request,
            }
        )
    except Exception as exc:
        unsupported = isinstance(exc, evidence.EvidenceError)
        message = (
            "unsupported bounded input representation; handler not executed"
            if unsupported
            else "initial checked receipt unavailable; handler not executed"
        )
        _protocol_gap(store, message, cid, request)
        reply = _rpc_error(mid, -32602 if unsupported else -32603, message)
        try:
            _deliver(store, reply, responder)
        except (OSError, ValueError, TypeError, RecursionError, MemoryError):
            pass
        return reply
    prior = getattr(store, "current_correlation", None)
    store.current_correlation = cid
    name = None
    args = None
    out = None
    outcome = "not_executed"
    try:
        if request.get("jsonrpc") != "2.0" or not _valid_rpc_id(mid):
            raise RequestValidationError("invalid RPC version/id")
        params = request.get("params", {})
        if not isinstance(params, dict):
            raise RequestValidationError("params: expected object")
        name = params.get("name")
        args = params.get("arguments", {})
        validate_arguments(name, args)
        try:
            out = (handler or call_tool)(store, name, args)
            outcome = (
                "refused" if isinstance(out, dict) and "error" in out else "returned"
            )
        except Exception as exc:
            outcome = "exception"
            out = {
                "error": "fixture handler exception: " + type(exc).__name__,
                "effects": "unknown; do not replay",
            }
    except RequestValidationError as exc:
        outcome = "validation_error"
        out = {"error": str(exc)}
    finally:
        store.current_correlation = prior
    try:
        result_text = evidence.encode_json(out, "handler result").decode("utf-8")
        result_encoding_error = None
    except Exception as exc:
        result_text = None
        result_encoding_error = type(exc).__name__
    terminal = {
        "trace_version": 1,
        "event": "completed",
        "correlation_id": cid,
        "request_sha256": request_hash,
        "handler_outcome": outcome,
        "result_status": "available" if result_text is not None else "unavailable",
        **(
            {"result": out}
            if result_text is not None
            else {
                "outcome_error": "handler result serialization unsupported: "
                + result_encoding_error
            }
        ),
    }
    try:
        terminal.update(
            store.capture_state(
                TOOL_EFFECTS.get(name, "action") == "action"
                if isinstance(name, str)
                else True
            )
        )
    except Exception as exc:
        terminal.update(state_capture="error", state_error=type(exc).__name__)
    try:
        evidence.encode_json(terminal, "completion")
    except evidence.EvidenceError:
        terminal = {
            k: v for k, v in terminal.items() if k not in {"persisted_entities"}
        }
        terminal.update(
            state_capture="error",
            state_error="completion state serialization unsupported",
        )
        try:
            evidence.encode_json(terminal, "completion")
        except evidence.EvidenceError:
            terminal.pop("result", None)
            terminal.update(
                result_status="unavailable",
                outcome_error="handler result serialization unsupported",
            )
    try:
        store.append_record(terminal)
    except Exception:
        _protocol_gap(store, "completion observation unavailable; no replay", cid)
    delivery_error = None
    try:
        if result_text is None:
            raise evidence.EvidenceError("handler result unavailable")
        text = result_text
        reply = {
            "jsonrpc": "2.0",
            "id": mid if _valid_rpc_id(mid) else None,
            "result": {
                "content": [{"type": "text", "text": text}],
                "isError": isinstance(out, dict) and "error" in out,
            },
        }
        response_raw = evidence.encode_json(reply, "response") + b"\n"
    except Exception as exc:
        # Encoding failed before response I/O. A small correlated error can be
        # delivered; it does not erase known handler effects or invent rollback.
        delivery_error = "response encoding: " + type(exc).__name__
        reply = _rpc_error(
            mid,
            -32603,
            "response unavailable; handler may have executed; do not replay",
        )
        response_raw = evidence.encode_json(reply, "error response") + b"\n"
    try:
        if responder is not None:
            responder(response_raw)
        delivery = "written" if responder is not None else "inprocess_return"
    except Exception as exc:
        # Never retry a possibly partial response or the underlying handler.
        delivery = "failed"
        delivery_error = "response write: " + type(exc).__name__
    try:
        store.append_record(
            {
                "trace_version": 1,
                "event": "response",
                "correlation_id": cid,
                "request_sha256": request_hash,
                "delivery": delivery,
                **({"error": delivery_error} if delivery_error else {}),
            }
        )
    except Exception:
        _protocol_gap(store, "response observation unavailable; no replay", cid)
    return reply


def observe_tool_call(store, name, args, *, rpc_id=1, handler=None):
    reply = process_request(
        store,
        {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": args},
        },
        handler=handler,
    )
    if "error" in reply:
        return {"error": reply["error"]["message"]}
    errors = []
    out = evidence.decode_input_json(
        reply["result"]["content"][0]["text"], "tool response", errors
    )
    return {"error": "unsupported tool response"} if errors else out


def main() -> int:
    store = Store(Path(sys.argv[1]).resolve())

    def send(raw):
        if sys.stdout.buffer.write(raw) != len(raw):
            raise OSError("short response write")
        sys.stdout.buffer.flush()

    for raw, error in evidence.read_frames(sys.stdin.buffer):
        if error:
            _protocol_gap(store, error)
            try:
                _deliver(store, _rpc_error(None, -32700, error), send)
            except (OSError, ValueError, TypeError, RecursionError, MemoryError):
                pass
            continue
        errors = []
        try:
            request = evidence.decode_input_json(raw.decode("utf-8"), "request", errors)
        except UnicodeError:
            request = None
            errors.append("request: invalid UTF-8")
        if errors:
            _protocol_gap(store, errors[0])
            try:
                _deliver(store, _rpc_error(None, -32700, errors[0]), send)
            except (OSError, ValueError, TypeError, RecursionError, MemoryError):
                pass
        else:
            process_request(store, request, responder=send)
    return 0


if __name__ == "__main__":
    sys.exit(main())
