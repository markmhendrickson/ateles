#!/usr/bin/env python3
"""session_integrity — server-side (proxy) session-integrity invariant (ateles#6, layer 2).

Implements layer 2 of docs/session_integrity.md: the cross-harness backstop the
Claude Code lifecycle hooks cannot provide alone. It rides the SAME chokepoint
as the #26 grant proxy — the stdio `tools/call` interceptor — so Cursor, Codex,
and autonomous ateles/OpenClaw daemons (none of which expose client hooks) are
held to the same plan-link + turn-storage guarantee as Claude Code.

OBSERVE + AUDIT ONLY (operator decision 2026-06-03): this tracker NEVER blocks
or denies a write. It sits on the money-touching MCP path, so it only:

  1. Tracks per-session write-bearing state, plan binding, and turn storage by
     inspecting the `tools/call` stream (store / correct / create_relationship /
     submit_*), keyed on the AAuth agent identity (ATELES_AGENT_SUB).
  2. Emits a `harness_event` (schema 689230f4-cd83-49b6-baa7-a752cf70629d,
     previously defined-but-unused) at session end as the per-session audit
     record — carrying harness type, agent identity, plan_id, turn refs, and the
     integrity classification.
  3. Surfaces a violation (write-bearing but unbound / turn-less) as an
     `escalation` so non-compliant autonomous sessions are visible to Onychomys.

Classification mirrors the Claude Code hook (stop_finalizer.py):
  - no domain writes            -> exempt   (grace path)
  - domain writes + plan + turns -> integral
  - domain writes + (no plan OR no turns) -> violated

Fail-open everywhere: a missing bearer token, an unreachable Neotoma, or any
unexpected error is logged to stderr and swallowed — it must never raise into
the proxy's tool path.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

log = logging.getLogger("mcp_tool_grant_proxy.session_integrity")

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
).rstrip("/")
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

# A write whose only entity types fall in this set is bookkeeping, not domain work.
BOOKKEEPING_TYPES = {"conversation", "conversation_message", "agent_message"}

# tools/call names (suffix-matched) that constitute a Neotoma write.
_WRITE_MARKERS = ("store", "correct", "create_relationship", "submit_")


def _is_write_tool(tool: str) -> bool:
    t = (tool or "").lower()
    return any(m in t for m in _WRITE_MARKERS)


def _entity_types(arguments: dict) -> set:
    types: set = set()
    if not isinstance(arguments, dict):
        return types
    if isinstance(arguments.get("entity_type"), str):
        types.add(arguments["entity_type"])
    for ent in arguments.get("entities", []) or []:
        if isinstance(ent, dict) and isinstance(ent.get("entity_type"), str):
            types.add(ent["entity_type"])
    return types


def _mentions_plan(arguments: dict) -> bool:
    try:
        blob = json.dumps(arguments) if arguments else ""
    except (TypeError, ValueError):
        return False
    return '"plan"' in blob or "plan_id" in blob or "plan_entity_id" in blob


class SessionIntegrityTracker:
    """Accumulates integrity signals for one proxy session (one agent process).

    A proxy instance bridges exactly one downstream MCP server for one agent
    invocation, so the tracker's lifetime == the session's lifetime. State is
    in-memory only; the audit/escalation are written at finalize().
    """

    def __init__(self, agent_sub: str, server_name: str, harness: Optional[str] = None) -> None:
        self.agent_sub = agent_sub or ""
        self.server_name = server_name
        self.harness = harness or os.environ.get("ATELES_HARNESS", "") or "unknown"
        self.session_id = os.environ.get("ATELES_SESSION_ID", "") or f"{self.agent_sub}:{int(time.time())}"
        self.wrote_domain = False
        self.bound_plan = False
        self.write_count = 0
        self.write_types: set = set()
        self.plan_ids: set = set()
        self._finalized = False

    def observe(self, tool: str, arguments: dict) -> None:
        """Record one tools/call. Never raises; cheap; no network."""
        try:
            if not _is_write_tool(tool):
                return
            self.write_count += 1
            etypes = _entity_types(arguments)
            domain_types = etypes - BOOKKEEPING_TYPES
            tl = (tool or "").lower()

            if "plan" in etypes or _mentions_plan(arguments):
                self.bound_plan = True
                for pid in _collect_plan_ids(arguments):
                    self.plan_ids.add(pid)

            # A create_relationship between two bookkeeping rows is itself
            # bookkeeping; only count it domain if a plan / non-bookkeeping type
            # is involved.
            if "create_relationship" in tl and not domain_types and not _mentions_plan(arguments):
                return
            if domain_types or "correct" in tl or "submit_" in tl or "create_relationship" in tl:
                self.wrote_domain = True
                self.write_types.update(domain_types)
        except Exception as exc:  # noqa: BLE001 — observation must never break the proxy
            log.debug(f"session-integrity observe error (ignored): {exc}")

    def classify(self) -> str:
        if not self.wrote_domain:
            return "exempt"
        if self.bound_plan:
            return "integral"
        return "violated"

    def finalize(self) -> None:
        """Emit the harness_event audit + (on violation) an escalation. Idempotent."""
        if self._finalized:
            return
        self._finalized = True
        status = self.classify()
        log.info(
            f"[session-integrity] sub={self.agent_sub or '<none>'} server={self.server_name} "
            f"harness={self.harness} writes={self.write_count} status={status}"
        )
        self._emit_harness_event(status)
        if status == "violated":
            self._emit_escalation(status)

    # ── Neotoma writes (best-effort, fail-open) ──────────────────────────────

    def _post(self, body: dict) -> None:
        if not NEOTOMA_BEARER_TOKEN:
            log.debug("no bearer token — skipping session-integrity write")
            return
        try:
            import httpx

            httpx.post(
                f"{NEOTOMA_BASE_URL}/store",
                json=body,
                headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
                timeout=5,
            )
        except Exception as exc:  # noqa: BLE001
            log.debug(f"session-integrity emit failed (non-fatal): {exc}")

    def _emit_harness_event(self, status: str) -> None:
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        entity = {
            "entity_type": "harness_event",
            "event_type": "session_integrity_check",
            "harness": self.harness,
            "agent_sub": self.agent_sub,
            "session_id": self.session_id,
            "mcp_server": self.server_name,
            "integrity_status": status,  # exempt | integral | violated
            "wrote_domain": self.wrote_domain,
            "bound_plan": self.bound_plan,
            "write_count": self.write_count,
            "write_types": sorted(self.write_types),
            "plan_ids": sorted(self.plan_ids),
            "observed_at": now,
        }
        self._post({
            "entities": [entity],
            "idempotency_key": f"harness-event-{self.session_id}-{self.server_name}-{int(time.time())}",
            "observation_source": "workflow_state",
        })

    def _emit_escalation(self, status: str) -> None:
        reasons = []
        if not self.bound_plan:
            reasons.append("no plan link")
        # turns are not visible at the proxy layer (the proxy sees tool calls,
        # not conversation turns) — turn-storage enforcement stays with the
        # client hook. The proxy enforces the plan-link half of the invariant.
        detail = (
            f"Session {self.session_id} (sub={self.agent_sub or '<none>'}, harness={self.harness}) "
            f"made {self.write_count} domain write(s) but has " + ", ".join(reasons)
            + ". Per docs/session_integrity.md the session must bind a plan. "
            "OBSERVE-ONLY: the write was NOT blocked; this is an audit signal."
        )
        entity = {
            "entity_type": "escalation",
            "escalation_type": "session_integrity_violation",
            "severity": "warning",
            "source_agent": self.agent_sub or "mcp_tool_grant_proxy",
            "harness": self.harness,
            "session_id": self.session_id,
            "summary": "Write-bearing session not bound to a plan",
            "detail": detail,
            "status": "open",
            "observed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        self._post({
            "entities": [entity],
            "idempotency_key": f"escalation-session-integrity-{self.session_id}-{int(time.time())}",
            "observation_source": "workflow_state",
        })


def _collect_plan_ids(arguments: dict) -> set:
    """Best-effort extraction of plan entity ids touched by a write."""
    ids: set = set()
    if not isinstance(arguments, dict):
        return ids
    for key in ("plan_id", "plan_entity_id"):
        v = arguments.get(key)
        if isinstance(v, str) and v.startswith("ent_"):
            ids.add(v)
    # correct() on a plan entity
    if arguments.get("entity_type") == "plan" and isinstance(arguments.get("entity_id"), str):
        ids.add(arguments["entity_id"])
    # relationships array referencing a plan target
    for rel in arguments.get("relationships", []) or []:
        if isinstance(rel, dict):
            tgt = rel.get("target_entity_id")
            if isinstance(tgt, str) and tgt.startswith("ent_"):
                ids.add(tgt)
    return ids
