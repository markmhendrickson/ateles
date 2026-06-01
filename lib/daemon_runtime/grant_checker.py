"""
lib/daemon_runtime/grant_checker.py — Check agent_grant status before dispatch.

Loads agent_grant entities from Neotoma by aauth_sub and exposes:
  - is_active / is_suspended / is_revoked properties
  - check_capability(cap) — True if the named capability is active
  - suspend / restore / revoke — write state changes back to Neotoma

If Neotoma is unreachable the checker is permissive (allows all) and logs a
warning. Grant checks are advisory in Phase 5; hard-blocking enforcement is
Phase 6 once the PS-layer AAuth integration lands (#30).

Grant entity schema (agent_grant in Neotoma):
  {
    "aauth_sub": "formica@ateles-swarm",
    "capabilities": ["neotoma:write", "github:ateles:write"],
    "status": "active" | "suspended" | "revoked",
    "suspended_at": "2026-05-27T…",
    "suspended_reason": "…",
    "revoked_at": "…",
    "revoked_reason": "…"
  }
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

log = logging.getLogger(__name__)

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")


@dataclass
class AgentGrant:
    """
    Snapshot of a single agent_grant entity.

    The live agent_grant schema (v1.0.0) stores ``capabilities`` as an array of
    objects, each ``{op, entity_types, repos, ...}``. Identity matches on
    ``match_sub`` / ``match_iss``. We normalise that here:

      - ``ops`` — set of capability op strings (e.g. "store_structured",
        "github_harness:write", "tool:parquet:read_parquet").
      - ``tool_grants`` — map of "<server>:<tool>" → param-constraint dict, built
        from capability entries whose op starts with "tool:" (issue #26). The
        leading "tool:" prefix is stripped so keys read "<server>:<tool>".
    """

    entity_id: str = ""
    aauth_sub: str = ""  # populated from match_sub for backward compat
    match_sub: str = ""
    match_iss: str = ""
    capabilities: list = field(default_factory=list)  # raw capability objects
    ops: set = field(default_factory=set)
    tool_grants: dict = field(default_factory=dict)
    status: str = "active"
    suspended_at: str = ""
    suspended_reason: str = ""
    revoked_at: str = ""
    revoked_reason: str = ""

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def is_suspended(self) -> bool:
        return self.status == "suspended"

    @property
    def is_revoked(self) -> bool:
        return self.status == "revoked"

    def has_capability(self, capability: str) -> bool:
        """Return True if this grant includes the named capability op (or '*')."""
        return "*" in self.ops or capability in self.ops

    def tool_constraints(self, server: str, tool: str) -> Optional[dict]:
        """
        Return the param-constraint dict for "<server>:<tool>" if granted,
        else None (denied). An empty dict means allowed with no constraints.

        Wildcards: "tool:<server>:*" grants all tools on a server;
        "tool:*" grants every MCP tool.
        """
        key = f"{server}:{tool}"
        if key in self.tool_grants:
            return self.tool_grants[key]
        if f"{server}:*" in self.tool_grants:
            return self.tool_grants[f"{server}:*"]
        if "*" in self.tool_grants:
            return self.tool_grants["*"]
        return None


class GrantChecker:
    """
    Load and check agent_grant entities for a given aauth_sub.

    Permissive fallback if Neotoma is unreachable — checks advisory only until
    PS-layer AAuth enforcement lands (issue #30).
    """

    def __init__(self, aauth_sub: str) -> None:
        self.aauth_sub = aauth_sub
        self._grants: list[AgentGrant] = []
        self._loaded = False
        self._load_error: Optional[str] = None

    def load(self) -> GrantChecker:
        """Fetch all agent_grant entities for this sub from Neotoma."""
        if not NEOTOMA_BEARER_TOKEN:
            self._load_error = "NEOTOMA_BEARER_TOKEN not set"
            log.warning(f"[grant_checker:{self.aauth_sub}] {self._load_error} — permissive fallback")
            self._loaded = True
            return self

        url = f"{NEOTOMA_BASE_URL}/entities"
        params = {
            "entity_type": "agent_grant",
            "search": self.aauth_sub,
            "include_snapshots": "true",
            "limit": 50,
        }
        try:
            resp = httpx.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
                timeout=10,
            )
            resp.raise_for_status()
            entities = resp.json().get("entities", [])
            self._grants = [
                self._parse(e)
                for e in entities
                if self._snapshot_matches_sub(e.get("snapshot") or {})
            ]
            log.info(
                f"[grant_checker:{self.aauth_sub}] Loaded {len(self._grants)} grant(s)"
            )
        except Exception as exc:
            self._load_error = str(exc)
            log.warning(
                f"[grant_checker:{self.aauth_sub}] Could not load grants: {exc} — "
                "permissive fallback"
            )
        self._loaded = True
        return self

    def is_active(self) -> bool:
        """True if at least one active grant exists (or Neotoma was unreachable)."""
        if self._load_error or not self._loaded:
            return True
        if not self._grants:
            return True  # no grants recorded = permissive
        return any(g.is_active for g in self._grants)

    def is_suspended(self) -> bool:
        """True if ALL grants are suspended (not just one)."""
        if self._load_error or not self._grants:
            return False
        return all(g.is_suspended for g in self._grants)

    def is_revoked(self) -> bool:
        """True if ALL grants are revoked."""
        if self._load_error or not self._grants:
            return False
        return all(g.is_revoked for g in self._grants)

    def check_capability(self, capability: str) -> bool:
        """
        True if the agent has an active grant covering the named capability.
        Permissive if Neotoma was unreachable.
        """
        if self._load_error or not self._grants:
            return True
        return any(g.is_active and g.has_capability(capability) for g in self._grants)

    def check_tool(self, server: str, tool: str) -> tuple[bool, Optional[dict]]:
        """
        Check whether an MCP tool call is authorized (issue #26).

        Returns (allowed, constraints):
          - allowed: True if an active grant covers "<server>:<tool>".
          - constraints: the param-constraint dict to enforce (may be empty),
            or None when denied.

        Permissive fallback (allowed=True, constraints=None) when Neotoma was
        unreachable or no grants are recorded — enforcement is advisory until
        the proxy and grant tightening land for all agents.
        """
        if self._load_error or not self._grants:
            return True, None
        # If NO grant declares any tool_grants at all, fall back to permissive —
        # tool-level enforcement only kicks in once an agent has been migrated
        # to declare its tool capabilities (avoids breaking un-migrated agents).
        any_tool_grants = any(g.tool_grants for g in self._grants)
        if not any_tool_grants:
            return True, None
        for g in self._grants:
            if not g.is_active:
                continue
            constraints = g.tool_constraints(server, tool)
            if constraints is not None:
                return True, constraints
        return False, None

    @property
    def grants(self) -> list[AgentGrant]:
        return list(self._grants)

    def _snapshot_matches_sub(self, snap: dict) -> bool:
        """Match a grant snapshot to this checker's sub via match_sub/aauth_sub."""
        candidate = snap.get("match_sub") or snap.get("aauth_sub") or ""
        return candidate == self.aauth_sub

    @staticmethod
    def _parse(entity: dict) -> AgentGrant:
        snap = entity.get("snapshot") or {}
        raw_caps = snap.get("capabilities") or []

        # Normalise capabilities into op strings + tool-grant map.
        ops: set = set()
        tool_grants: dict = {}
        if isinstance(raw_caps, str):
            # Legacy comma-separated string form.
            for c in raw_caps.split(","):
                c = c.strip()
                if c:
                    ops.add(c)
        elif isinstance(raw_caps, list):
            for cap in raw_caps:
                if isinstance(cap, str):
                    ops.add(cap.strip())
                elif isinstance(cap, dict):
                    op = cap.get("op", "")
                    if op:
                        ops.add(op)
                    # Tool-grant entries: op == "tool:<server>:<tool>" with
                    # optional "param_constraints" dict. Key stored without the
                    # leading "tool:" prefix → "<server>:<tool>".
                    if op.startswith("tool:"):
                        key = op[len("tool:"):]
                        tool_grants[key] = cap.get("param_constraints") or {}

        match_sub = snap.get("match_sub") or snap.get("aauth_sub") or ""
        return AgentGrant(
            entity_id=entity.get("entity_id", ""),
            aauth_sub=match_sub,
            match_sub=match_sub,
            match_iss=snap.get("match_iss", ""),
            capabilities=raw_caps if isinstance(raw_caps, list) else [],
            ops=ops,
            tool_grants=tool_grants,
            status=snap.get("status", "active"),
            suspended_at=snap.get("suspended_at", ""),
            suspended_reason=snap.get("suspended_reason", ""),
            revoked_at=snap.get("revoked_at", ""),
            revoked_reason=snap.get("revoked_reason", ""),
        )


def check_param_constraints(
    constraints: dict, params: dict
) -> tuple[bool, str]:
    """
    Evaluate a tool call's params against a grant's param-constraint dict (#26).

    Returns (ok, reason). ok=True means the call satisfies all constraints.
    An empty constraints dict always passes. Unknown constraint keys are
    ignored (forward-compatible — a new constraint added to a grant won't
    hard-fail an older proxy, but see note below).

    Supported constraint keys:
      - "tables": [list]        → params["table"] / params["table_name"] must be in list
      - "max_amount_sats": int  → params["amount_sats"] / params["amount"] must be <=
      - "to_allowlist": true    → params["to"] must be present (allowlist membership
                                   is enforced by the tool itself; the grant only
                                   asserts the flag must be honoured)
      - "max_<field>": number   → params[<field>] must be <= value
      - "allowed_<field>": list → params[<field>] must be in list
    """
    if not constraints:
        return True, ""

    for ckey, cval in constraints.items():
        if ckey == "tables":
            table = params.get("table") or params.get("table_name") or params.get("name")
            if table is not None and table not in cval:
                return False, f"table {table!r} not in allowed tables {cval}"
        elif ckey == "max_amount_sats":
            amount = params.get("amount_sats")
            if amount is None:
                amount = params.get("amount")
            if isinstance(amount, (int, float)) and amount > cval:
                return False, f"amount {amount} exceeds max_amount_sats {cval}"
        elif ckey == "to_allowlist":
            if cval and not params.get("to"):
                return False, "to_allowlist requires a 'to' parameter"
        elif ckey.startswith("max_"):
            field_name = ckey[len("max_"):]
            v = params.get(field_name)
            if isinstance(v, (int, float)) and v > cval:
                return False, f"{field_name} {v} exceeds {ckey} {cval}"
        elif ckey.startswith("allowed_"):
            field_name = ckey[len("allowed_"):]
            v = params.get(field_name)
            if v is not None and isinstance(cval, list) and v not in cval:
                return False, f"{field_name} {v!r} not in {ckey} {cval}"
        # Unknown constraint keys: ignored (forward-compatible).

    return True, ""


def _write_grant_state(
    entity_id: str,
    status: str,
    reason: str,
    timestamp_field: str,
    reason_field: str,
) -> bool:
    """PATCH a grant entity's status via Neotoma corrections API."""
    if not NEOTOMA_BEARER_TOKEN:
        log.error("NEOTOMA_BEARER_TOKEN not set — cannot update grant")
        return False

    now = _iso_now()
    base = f"{NEOTOMA_BASE_URL}/entities/{entity_id}/corrections"
    headers = {"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}", "Content-Type": "application/json"}

    updates = [
        {"field": "status", "value": status, "idempotency_key": f"grant-status-{entity_id}-{now}"},
        {"field": reason_field, "value": reason, "idempotency_key": f"grant-reason-{entity_id}-{now}"},
        {"field": timestamp_field, "value": now, "idempotency_key": f"grant-ts-{entity_id}-{now}"},
    ]
    try:
        for update in updates:
            resp = httpx.post(base, json=update, headers=headers, timeout=10)
            resp.raise_for_status()
        return True
    except Exception as exc:
        log.error(f"Failed to update grant {entity_id}: {exc}")
        return False


def suspend_grant(entity_id: str, reason: str = "") -> bool:
    """Set grant status to suspended with reason and timestamp."""
    return _write_grant_state(entity_id, "suspended", reason, "suspended_at", "suspended_reason")


def restore_grant(entity_id: str) -> bool:
    """Restore a suspended grant back to active."""
    if not NEOTOMA_BEARER_TOKEN:
        return False
    now = _iso_now()
    base = f"{NEOTOMA_BASE_URL}/entities/{entity_id}/corrections"
    headers = {"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}", "Content-Type": "application/json"}
    try:
        resp = httpx.post(
            base,
            json={"field": "status", "value": "active", "idempotency_key": f"grant-restore-{entity_id}-{now}"},
            headers=headers,
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        log.error(f"Failed to restore grant {entity_id}: {exc}")
        return False


def revoke_grant(entity_id: str, reason: str = "") -> bool:
    """Set grant status to revoked (requires re-consent to restore)."""
    return _write_grant_state(entity_id, "revoked", reason, "revoked_at", "revoked_reason")


def _iso_now() -> str:
    import datetime
    return datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
