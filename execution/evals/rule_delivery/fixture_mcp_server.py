#!/usr/bin/env python3
"""Sandbox Neotoma MCP server for rule-delivery evals (stdlib only).

Stands in for Neotoma during an eval run so no scenario ever reads or writes
the operator's real instance. It serves the fixture entities copied into one
run directory (``neotoma_state.json``) over MCP stdio, mirrors the names of
the real Neotoma tools a session reaches for (``retrieve_entity_snapshot``,
``retrieve_entities``, ``correct``, ``get_session_identity``).

Every call is appended to ``mcp_calls.jsonl`` in the run directory. That log,
not the model's own account of what it did, is the ground truth the checks
read: it records which rule ids were fetched, in what order, and what was
written.

Usage (from an ``--mcp-config`` file): ``python3 fixture_mcp_server.py <run_dir>``.
The server sends no ``instructions`` in its initialize reply, so MCP
instructions are never a rule-delivery channel in these evals.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

PROTOCOL_VERSION = "2025-06-18"

# MCP tool names (mirroring real Neotoma tools), not REST routes.
T_SNAPSHOT = "retrieve_entity_snapshot"  # neotoma-rest-path-ok: MCP tool name
T_LIST = "retrieve_entities"  # neotoma-rest-path-ok: MCP tool name
T_RELATED = "retrieve_related_entities"  # neotoma-rest-path-ok: MCP tool name

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
            "properties": {"entities": {"type": "array"}},
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
        "description": "List entities related to one entity.",
        "inputSchema": {
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
        },
    },
    {
        "name": "list_timeline_events",
        "description": "List recent timeline events.",
        "inputSchema": {"type": "object", "properties": {"limit": {"type": "integer"}}},
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
]


class Store:
    def __init__(self, run_dir: Path) -> None:
        self.state_path = run_dir / "neotoma_state.json"
        self.log_path = run_dir / "mcp_calls.jsonl"

    def load(self) -> dict:
        return json.loads(self.state_path.read_text())

    def save(self, state: dict) -> None:
        self.state_path.write_text(json.dumps(state, indent=2))

    def log(self, tool: str, args: dict, result: dict) -> None:
        with self.log_path.open("a") as fh:
            fh.write(
                json.dumps(
                    {"ts": time.time(), "tool": tool, "args": args, "result": result}
                )
                + "\n"
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


def call_tool(store: Store, name: str, args: dict) -> dict:
    state = store.load()
    entities: dict = state["entities"]
    if name == T_SNAPSHOT:
        ent = entities.get(str(args.get("entity_id", "")))
        if ent is None:
            return {"error": "entity not found", "entity_id": args.get("entity_id")}
        return {"entity_id": args["entity_id"], **ent}
    if name == T_LIST:
        limit = int(args.get("limit") or 25)
        hits = [
            {"entity_id": eid, **ent}
            for eid, ent in entities.items()
            if _matches(ent, args.get("entity_type"), args.get("search"))
        ]
        return {"entities": hits[:limit], "total": len(hits)}
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
    if name in ("store", "create_relationship"):
        return {"error": "writes of this kind are disabled in the eval sandbox"}
    if name in (T_RELATED, "list_timeline_events", "list_observations"):
        return {"results": []}
    if name == "describe_entity_type":
        known = str(args.get("entity_type", "")) in REGISTERED_TYPES
        return {"entity_type": args.get("entity_type"), "registered": known}
    return {"error": f"unknown tool {name}"}


def main() -> int:
    run_dir = Path(sys.argv[1]).resolve()
    store = Store(run_dir)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        mid = msg.get("id")
        method = msg.get("method")
        if mid is None:
            continue  # notification
        if method == "initialize":
            result = {
                "protocolVersion": msg.get("params", {}).get(
                    "protocolVersion", PROTOCOL_VERSION
                ),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "neotoma", "version": "eval-fixture"},
            }
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            params = msg.get("params", {})
            name = params.get("name", "")
            args = params.get("arguments") or {}
            out = call_tool(store, name, args)
            store.log(name, args, out)
            result = {
                "content": [{"type": "text", "text": json.dumps(out, indent=1)}],
                "isError": "error" in out,
            }
        elif method == "ping":
            result = {}
        else:
            sys.stdout.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": mid,
                        "error": {"code": -32601, "message": "method not found"},
                    }
                )
                + "\n"
            )
            sys.stdout.flush()
            continue
        sys.stdout.write(
            json.dumps({"jsonrpc": "2.0", "id": mid, "result": result}) + "\n"
        )
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
