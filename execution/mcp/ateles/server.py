#!/usr/bin/env python3
"""
ateles — MCP server for Ateles swarm routing and checkpoint management.

Provides four tools that wrap multi-step Neotoma query patterns into single
calls, so any connected agent gets reliable swarm interaction without
re-deriving the roster/policy/checkpoint dance each session.

Tools:
  get_swarm_roster   — full roster (roles → agent names)
  route_task         — resolve owning agent + definition + execution policy
  list_checkpoints   — pending checkpoint_briefs awaiting operator
  resolve_checkpoint — approve/reject a checkpoint with validation

Environment:
  NEOTOMA_BASE_URL       (default: https://neotoma.markmhendrickson.com)
  NEOTOMA_BEARER_TOKEN   (required)
  SWARM_ROSTER_KEY       (default: default)

Transport: stdio (launched by Claude Code as an MCP server subprocess).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    TextContent,
    Tool,
)

log = logging.getLogger("ateles")

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
ROSTER_KEY = os.environ.get("SWARM_ROSTER_KEY", "default")

DEFAULT_POLICY_ID = os.environ.get(
    "EXECUTION_POLICY_DEFAULT_ID", "ent_dfce6edecefe3eb7fc9e0337"
)

AGENT_POLICY_OVERRIDES: dict[str, str] = {
    "monedula": os.environ.get(
        "MONEDULA_POLICY_ID", "ent_c7f81385afbd993db3dd11ff"
    ),
}

SERVER_INSTRUCTIONS = """\
You are connected to Ateles. Follow these operating rules:

1. **Dispatch, don't do inline.** When route_task identifies an owning agent, \
delegate to that agent rather than doing the work yourself.
2. **Monitor dispatched work.** After dispatching a task via route_task, track \
its status in Neotoma (retrieve the task entity periodically). Report completion, \
failure, or checkpoint escalation back to the operator — do not fire-and-forget.
3. **Consent gate.** Never send anything public, email anyone, or take an \
irreversible external action without operator approval.
4. **Checkpoint protocol.** Pending checkpoints (list_checkpoints) are the \
operator's decision queue. Present each with its blast radius, confidence vs \
threshold, and reason. Act on the operator's decision via resolve_checkpoint — \
do NOT execute the held task yourself.
5. **Neotoma first.** Durable memory lives in Neotoma. Store, don't leave in \
conversation.
"""


# ── Neotoma HTTP helpers ─────────────────────────────────────────────────────

def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"}


def _get(path: str, params: dict | None = None) -> dict | None:
    if not NEOTOMA_BEARER_TOKEN:
        return None
    try:
        resp = httpx.get(
            f"{NEOTOMA_BASE_URL}{path}",
            headers=_headers(),
            params=params,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.warning("neotoma GET %s failed: %s", path, exc)
        return None


def _post(path: str, body: dict) -> dict | None:
    if not NEOTOMA_BEARER_TOKEN:
        return None
    try:
        resp = httpx.post(
            f"{NEOTOMA_BASE_URL}{path}",
            headers=_headers(),
            json=body,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.warning("neotoma POST %s failed: %s", path, exc)
        return None


def _retrieve_entities(
    entity_type: str,
    search: str | None = None,
    snapshot_filters: dict | None = None,
    limit: int = 100,
    include_snapshots: bool = True,
) -> list[dict]:
    body: dict[str, Any] = {
        "entity_type": entity_type,
        "limit": limit,
        "include_snapshots": include_snapshots,
    }
    if search:
        body["search"] = search
    if snapshot_filters:
        body["snapshot_filters"] = snapshot_filters
    # POST /entities/query — NOT /retrieve, which 404s. The GET /entities list
    # endpoint does not exist either; see lib/daemon_runtime/agent_loader.py.
    data = _post("/entities/query", body)
    if data is None:
        return []
    return data.get("entities", [])


def _snapshot_of(entity: dict) -> dict:
    snap = (entity.get("snapshot") or {}).get("snapshot")
    if isinstance(snap, dict):
        return snap
    if isinstance(entity.get("snapshot"), dict):
        return entity["snapshot"]
    return entity


def _correct(entity_id: str, entity_type: str, field: str, value: Any, idem_key: str) -> bool:
    body = {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "field": field,
        "value": value,
        "idempotency_key": idem_key,
    }
    result = _post("/correct", body)
    return result is not None


# ── Tool implementations ─────────────────────────────────────────────────────

def _get_swarm_roster() -> dict:
    entities = _retrieve_entities(
        "swarm_roster",
        snapshot_filters={"roster_key": {"op": "eq", "value": ROSTER_KEY}},
        limit=1,
    )
    if not entities:
        return {"error": "swarm_roster not found", "roster_key": ROSTER_KEY}

    snap = _snapshot_of(entities[0])
    roles_raw = snap.get("roles", "{}")
    if isinstance(roles_raw, str):
        try:
            roles = json.loads(roles_raw)
        except (json.JSONDecodeError, TypeError):
            roles = {}
    else:
        roles = roles_raw

    return {
        "entity_id": entities[0].get("entity_id", entities[0].get("id", "")),
        "roster_key": ROSTER_KEY,
        "swarm_domain": snap.get("swarm_domain", ""),
        "roles": roles,
    }


def _route_task(task_description: str, action_type: str | None = None) -> dict:
    roster = _get_swarm_roster()
    if "error" in roster:
        return roster

    roles: dict[str, str] = roster.get("roles", {})

    best_role: str | None = None
    best_agent: str | None = None

    desc_lower = task_description.lower()
    # Ordered most-specific first: multi-word keywords must match before
    # single-word substrings (e.g. "review pr" → pr_steward, not code).
    role_keywords: dict[str, list[str]] = {
        "pr_steward": ["review pr", "merge pr", "pull request review"],
        "issue_triage": ["issue", "bug report", "github issue", "triage issue"],
        "email_triage": ["email", "inbox", "triage email", "mail"],
        "financial_analysis": ["financial analysis", "revenue", "forecast"],
        "customer_intelligence": ["customer", "lead", "prospect"],
        "strategy_adversary": ["strategy", "adversarial", "red team"],
        "release_manager": ["release", "deploy", "version"],
        "recurring_tasks": ["recurring", "scheduled task", "cron"],
        "neotoma_repo": ["neotoma", "neotoma repo"],
        "compliance": ["compliance", "legal review", "contract"],
        "screenshots": ["screenshot", "capture screen"],
        "designer": ["design", "mockup", "wireframe", "ui ", "ux "],
        "content": ["blog", "write post", "content", "article"],
        "briefings": ["meeting", "briefing", "agenda", "calendar"],
        "payments": ["payment", "invoice", "pay ", "transfer", "wage"],
        "health": ["workout", "exercise", "gym", "fitness", "health"],
        "tax": ["tax", "fiscal", "iva", "vat", "hacienda"],
        "mirror": ["mirror", "sync repo", "apus"],
        "gtm": ["go to market", "gtm", "launch"],
        "pm": ["product", "roadmap", "feature plan"],
        "crm": ["contact", "crm", "relationship"],
        "qa": ["test", "qa ", "quality"],
        "code": ["code", "implement", "build", "fix bug", "refactor"],
        "dispatcher": ["dispatch", "assign", "route"],
    }

    for role, keywords in role_keywords.items():
        for kw in keywords:
            if kw in desc_lower and role in roles:
                best_role = role
                best_agent = roles[role]
                break
        if best_role:
            break

    if not best_agent:
        best_role = "dispatcher"
        best_agent = roles.get("dispatcher")

    agent_def = None
    if best_agent:
        agents = _retrieve_entities("agent_definition", search=best_agent, limit=1)
        if agents:
            snap = _snapshot_of(agents[0])
            agent_def = {
                "entity_id": agents[0].get("entity_id", agents[0].get("id", "")),
                "name": snap.get("name", ""),
                "description": snap.get("description", ""),
                "prompt_markdown": snap.get("prompt_markdown", ""),
                "context_entity_types": snap.get("context_entity_types", []),
                "operational_entity_types": snap.get("operational_entity_types", []),
                "tool_allowlist": snap.get("tool_allowlist", []),
                "tier": snap.get("tier", ""),
                "aauth_sub": snap.get("aauth_sub", ""),
            }

    policy_id = AGENT_POLICY_OVERRIDES.get(
        (best_agent or "").lower(), DEFAULT_POLICY_ID
    )
    policy_data = _get(f"/entities/{policy_id}")
    policy = None
    if policy_data:
        psnap = _snapshot_of(policy_data)
        policy = {
            "entity_id": policy_id,
            "title": psnap.get("title", ""),
            "confidence_threshold": psnap.get("confidence_threshold"),
            "blast_radius_default": psnap.get("blast_radius_default"),
            "high_blast_action_types": psnap.get("high_blast_action_types"),
        }

    result: dict[str, Any] = {
        "matched_role": best_role,
        "matched_agent": best_agent,
        "swarm_domain": roster.get("swarm_domain", ""),
    }
    if agent_def:
        result["agent_definition"] = agent_def
    if policy:
        result["execution_policy"] = policy
    if action_type:
        result["action_type"] = action_type
        if policy:
            high_blast = policy.get("high_blast_action_types", [])
            if isinstance(high_blast, str):
                try:
                    high_blast = json.loads(high_blast)
                except (json.JSONDecodeError, TypeError):
                    high_blast = []
            result["action_blast_radius"] = (
                "high" if action_type.lower() in [h.lower() for h in (high_blast or [])]
                else "low"
            )
    return result


def _list_checkpoints() -> dict:
    entities = _retrieve_entities(
        "checkpoint_brief",
        snapshot_filters={"status": {"op": "eq", "value": "awaiting_operator"}},
        limit=50,
    )

    checkpoints = []
    for ent in entities:
        snap = _snapshot_of(ent)
        eid = ent.get("entity_id", ent.get("id", ""))

        task_id = snap.get("task_entity_id")
        task_title = None
        if task_id:
            task_data = _get(f"/entities/{task_id}")
            if task_data:
                tsnap = _snapshot_of(task_data)
                task_title = tsnap.get("title", "")

        checkpoints.append({
            "checkpoint_id": eid,
            "title": snap.get("title", ""),
            "status": snap.get("status", ""),
            "handler": snap.get("handler", ""),
            "task_entity_id": task_id,
            "task_title": task_title,
            "confidence": snap.get("confidence"),
            "confidence_threshold": snap.get("confidence_threshold"),
            "blast_radius": snap.get("blast_radius", ""),
            "gate_action": snap.get("gate_action", ""),
            "reason": snap.get("reason", ""),
            "proposed_alternatives": snap.get("proposed_alternatives", []),
        })

    return {"count": len(checkpoints), "checkpoints": checkpoints}


def _resolve_checkpoint(checkpoint_id: str, action: str) -> dict:
    action_lower = action.strip().lower()
    if action_lower not in ("approve", "reject"):
        return {"error": f"action must be 'approve' or 'reject', got '{action}'"}

    data = _get(f"/entities/{checkpoint_id}")
    if data is None:
        return {"error": f"checkpoint {checkpoint_id} not found or Neotoma unreachable"}

    snap = _snapshot_of(data)
    current_status = str(snap.get("status", "")).strip().lower()
    if current_status not in ("awaiting_operator",):
        return {
            "error": f"checkpoint is '{current_status}', not 'awaiting_operator' — cannot resolve",
            "checkpoint_id": checkpoint_id,
        }

    dispatched = snap.get("resolved_dispatched")
    if dispatched is True or str(dispatched).strip().lower() in {"true", "1", "yes"}:
        return {
            "error": "checkpoint already dispatched — this is a replay",
            "checkpoint_id": checkpoint_id,
        }

    new_status = "approved" if action_lower == "approve" else "rejected"
    idem_key = f"resolve-checkpoint-{checkpoint_id}-{new_status}"

    ok = _correct(checkpoint_id, "checkpoint_brief", "status", new_status, idem_key)
    if not ok:
        return {"error": "failed to correct checkpoint status in Neotoma"}

    task_id = snap.get("task_entity_id")
    if action_lower == "reject" and task_id:
        _correct(task_id, "task", "status", "declined", f"decline-swarm-harness-{task_id}")

    return {
        "checkpoint_id": checkpoint_id,
        "new_status": new_status,
        "task_entity_id": task_id,
        "action_taken": (
            "approved — dispatcher will re-dispatch"
            if action_lower == "approve"
            else "rejected — task marked declined"
        ),
    }


# ── MCP Server setup ─────────────────────────────────────────────────────────

TOOLS = [
    Tool(
        name="get_swarm_roster",
        description=(
            "Returns the full Ateles swarm roster: a map of roles to agent names, "
            "plus the swarm domain and roster entity ID. Use this to discover which "
            "agents exist and what roles they fill."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="route_task",
        description=(
            "Given a task description, resolves the owning agent from the swarm "
            "roster by role, fetches its agent_definition (prompt, context types, "
            "tool allowlist), and the applicable execution_policy. Returns the "
            "complete dispatch context in one call. Optionally pass action_type to "
            "get the blast radius classification for that action."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_description": {
                    "type": "string",
                    "description": "What the task is about — used for keyword-based role matching.",
                },
                "action_type": {
                    "type": "string",
                    "description": (
                        "Optional action type (e.g. 'git_push', 'payment', 'publish') "
                        "to classify blast radius under the resolved policy."
                    ),
                },
            },
            "required": ["task_description"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="list_checkpoints",
        description=(
            "Returns all pending checkpoint_briefs (status: awaiting_operator) with "
            "task title, assigned agent, blast radius, confidence vs threshold, and "
            "reason pre-joined. These are the operator's decision queue."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="resolve_checkpoint",
        description=(
            "Approves or rejects a pending checkpoint_brief by entity ID. Validates "
            "that the checkpoint is awaiting_operator and has not already been "
            "dispatched. On rejection, also marks the referenced task as declined."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "checkpoint_id": {
                    "type": "string",
                    "description": "Entity ID of the checkpoint_brief to resolve.",
                },
                "action": {
                    "type": "string",
                    "enum": ["approve", "reject"],
                    "description": "Whether to approve or reject the checkpoint.",
                },
            },
            "required": ["checkpoint_id", "action"],
            "additionalProperties": False,
        },
    ),
]

TOOL_HANDLERS = {
    "get_swarm_roster": lambda args: _get_swarm_roster(),
    "route_task": lambda args: _route_task(
        args["task_description"], args.get("action_type")
    ),
    "list_checkpoints": lambda args: _list_checkpoints(),
    "resolve_checkpoint": lambda args: _resolve_checkpoint(
        args["checkpoint_id"], args["action"]
    ),
}


async def main():
    server = Server("ateles", instructions=SERVER_INSTRUCTIONS)

    @server.list_tools()
    async def handle_list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
        handler = TOOL_HANDLERS.get(name)
        if not handler:
            return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]
        result = handler(arguments or {})
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    options = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, options)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    import asyncio
    asyncio.run(main())
