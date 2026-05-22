"""
lib/daemon_runtime/agent_loader.py — Load agent_definition from Neotoma at daemon startup.

Each T3 daemon calls AgentLoader(name).load() at startup to get its
configuration from Neotoma. No config files. Updating an agent's prompt
or tool_allowlist is a Neotoma correct() call — no code commit.

Falls back gracefully if Neotoma is unreachable (returns minimal default).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import httpx

log = logging.getLogger(__name__)

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")


@dataclass
class AgentDefinition:
    """Snapshot of an agent_definition entity from Neotoma."""

    entity_id: str = ""
    name: str = ""
    description: str = ""
    tier: str = ""
    genus: str = ""
    status: str = "active"
    prompt_markdown: str = ""
    tool_allowlist: str = "*"
    agent_grant: str = "service"
    override_policy: str = ""
    aauth_sub: str = ""
    version: str = "1.0.0"
    notes: str = ""
    raw: dict = field(default_factory=dict)

    @property
    def tools(self) -> list[str]:
        """Return tool_allowlist as a list. ['*'] means all tools."""
        if not self.tool_allowlist or self.tool_allowlist.strip() == "*":
            return ["*"]
        return [t.strip() for t in self.tool_allowlist.split(",") if t.strip()]

    @property
    def is_operator(self) -> bool:
        return self.agent_grant == "operator"

    @property
    def is_service(self) -> bool:
        return self.agent_grant == "service"


class AgentLoader:
    """
    Load an agent_definition entity from Neotoma by agent name.

    Priority of lookup:
      1. AGENT_DEFINITION_ID env var (e.g. MONEDULA_AGENT_DEFINITION_ID)
      2. Search by name field in agent_definition entities
      3. Return a stub AgentDefinition if Neotoma is unreachable
    """

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name.lower()
        self._prefix = agent_name.upper()

    def load(self) -> AgentDefinition:
        """
        Load agent_definition from Neotoma.
        Returns a stub AgentDefinition if Neotoma is unavailable.
        """
        if not NEOTOMA_BEARER_TOKEN:
            log.warning(
                f"[{self.agent_name}] NEOTOMA_BEARER_TOKEN not set — "
                "using stub AgentDefinition (Phase 1 setup incomplete)"
            )
            return self._stub()

        # Try explicit entity ID first
        explicit_id = os.environ.get(f"{self._prefix}_AGENT_DEFINITION_ID", "")
        if explicit_id:
            return self._load_by_id(explicit_id)

        # Fall back to name search
        return self._load_by_name()

    def _load_by_id(self, entity_id: str) -> AgentDefinition:
        url = f"{NEOTOMA_BASE_URL}/entities/{entity_id}"
        try:
            resp = httpx.get(
                url,
                headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            return self._parse(entity_id, data)
        except Exception as exc:
            log.warning(
                f"[{self.agent_name}] Could not load agent_definition {entity_id}: {exc}"
            )
            return self._stub()

    def _load_by_name(self) -> AgentDefinition:
        """Search for agent_definition by name field."""
        url = f"{NEOTOMA_BASE_URL}/entities"
        params = {
            "entity_type": "agent_definition",
            "search": self.agent_name,
            "limit": 5,
            "include_snapshots": "true",
        }
        try:
            resp = httpx.get(
                url,
                params=params,
                headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            entities = data.get("entities", [])
            for ent in entities:
                snap = ent.get("snapshot") or {}
                if snap.get("name", "").lower() == self.agent_name:
                    log.info(
                        f"[{self.agent_name}] Loaded agent_definition "
                        f"{ent['entity_id']} from Neotoma"
                    )
                    return self._parse(ent["entity_id"], {"snapshot": snap})
            log.warning(
                f"[{self.agent_name}] No agent_definition found in Neotoma — "
                "using stub (run Phase 1 setup)"
            )
        except Exception as exc:
            log.warning(
                f"[{self.agent_name}] Neotoma search failed: {exc} — using stub"
            )
        return self._stub()

    def _parse(self, entity_id: str, data: dict) -> AgentDefinition:
        snap = data.get("snapshot") or data.get("entity", {}).get("snapshot", {})
        return AgentDefinition(
            entity_id=entity_id,
            name=snap.get("name", self.agent_name),
            description=snap.get("description", ""),
            tier=snap.get("tier", ""),
            genus=snap.get("genus", ""),
            status=snap.get("status", "active"),
            prompt_markdown=snap.get("prompt_markdown", ""),
            tool_allowlist=snap.get("tool_allowlist", "*"),
            agent_grant=snap.get("agent_grant", "service"),
            override_policy=snap.get("override_policy", ""),
            aauth_sub=snap.get("aauth_sub", f"{self.agent_name}@ateles-swarm"),
            version=snap.get("version", "1.0.0"),
            notes=snap.get("notes", ""),
            raw=data,
        )

    def _stub(self) -> AgentDefinition:
        return AgentDefinition(
            name=self.agent_name,
            aauth_sub=f"{self.agent_name}@ateles-swarm",
            agent_grant="service",
            tool_allowlist="*",
            status="active",
        )
