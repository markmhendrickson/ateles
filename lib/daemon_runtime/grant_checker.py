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
    """Snapshot of a single agent_grant entity."""

    entity_id: str = ""
    aauth_sub: str = ""
    capabilities: list[str] = field(default_factory=list)
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
        """Return True if this grant includes the named capability (or '*')."""
        return "*" in self.capabilities or capability in self.capabilities


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
                if (e.get("snapshot") or {}).get("aauth_sub") == self.aauth_sub
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

    @property
    def grants(self) -> list[AgentGrant]:
        return list(self._grants)

    @staticmethod
    def _parse(entity: dict) -> AgentGrant:
        snap = entity.get("snapshot") or {}
        caps = snap.get("capabilities") or []
        if isinstance(caps, str):
            caps = [c.strip() for c in caps.split(",") if c.strip()]
        return AgentGrant(
            entity_id=entity.get("entity_id", ""),
            aauth_sub=snap.get("aauth_sub", ""),
            capabilities=caps,
            status=snap.get("status", "active"),
            suspended_at=snap.get("suspended_at", ""),
            suspended_reason=snap.get("suspended_reason", ""),
            revoked_at=snap.get("revoked_at", ""),
            revoked_reason=snap.get("revoked_reason", ""),
        )


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
