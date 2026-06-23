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

try:  # package import (normal daemon runtime) with script-import fallback
    from . import neotoma_signed as ns
except ImportError:  # pragma: no cover
    import neotoma_signed as ns  # type: ignore

log = logging.getLogger(__name__)

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")


def _auth_headers() -> dict[str, str]:
    """Authorization header only when a bearer token is configured.

    Open-mode Neotoma instances accept unauthenticated requests and reject any
    bearer token, so sending an empty/stale token would 401.
    """
    return (
        {"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"}
        if NEOTOMA_BEARER_TOKEN
        else {}
    )


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
    tool_allowlist: "str | list[str]" = "*"
    agent_grant: str = "service"
    override_policy: str = ""
    aauth_sub: str = ""
    version: str = "1.0.0"
    notes: str = ""
    raw: dict = field(default_factory=dict)
    # Observation ID that produced the current snapshot (for dispatch pinning, ateles#22)
    last_observation_id: str = ""

    @property
    def tools(self) -> list[str]:
        """Return tool_allowlist as a list. ['*'] means all tools.

        Accepts tool_allowlist in any of the shapes Neotoma may store it:
          - "*" (string) or empty -> all tools
          - a JSON array / Python list (the canonical entity storage shape)
          - a comma-separated string (legacy / hand-authored shape)
        """
        raw = self.tool_allowlist
        if raw is None:
            return ["*"]
        # Array shape (canonical entity storage): list/tuple of tool names.
        if isinstance(raw, (list, tuple)):
            items = [str(t).strip() for t in raw if str(t).strip()]
            return items or ["*"]
        # String shape: "*" / empty -> wildcard; else split on commas.
        text = str(raw).strip()
        if not text or text == "*":
            return ["*"]
        return [t.strip() for t in text.split(",") if t.strip()]

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
            # Open-mode Neotoma instances accept unauthenticated reads. Proceed
            # without a token rather than falling back to the stub definition.
            log.info(
                f"[{self.agent_name}] NEOTOMA_BEARER_TOKEN not set — "
                "loading agent_definition without auth (open-mode Neotoma)"
            )

        # Try explicit entity ID first
        explicit_id = os.environ.get(f"{self._prefix}_AGENT_DEFINITION_ID", "")
        if explicit_id:
            return self._load_by_id(explicit_id)

        # Fall back to name search
        return self._load_by_name()

    def _neotoma(self, method: str, url: str, body: "dict | None" = None) -> dict:
        """Fetch JSON from Neotoma, returning the parsed body.

        Per-agent AAuth-signed when ``NEOTOMA_AAUTH_VIA_CLI`` is on and this agent
        has a key; otherwise the unsigned/bearer httpx path (behavior unchanged).
        Falls back to bearer on any signing failure or non-2xx, so enabling
        signing can never reduce availability. Raises on transport error — callers
        already handle that.
        """
        if ns.via_cli_enabled() and ns.agent_identity(self.agent_name):
            try:
                status, data = ns.signed_request(method, url, body, agent_name=self.agent_name)
                if 200 <= status < 300:
                    return data
                log.warning(
                    f"[{self.agent_name}] signed {method} {url} -> {status}; falling back to bearer"
                )
            except Exception as exc:
                log.warning(
                    f"[{self.agent_name}] signed request failed ({exc}); falling back to bearer"
                )
        if method.upper() == "GET":
            resp = httpx.get(url, headers=_auth_headers(), timeout=10)
        else:
            resp = httpx.post(url, json=body, headers=_auth_headers(), timeout=10)
        resp.raise_for_status()
        return resp.json()

    def _load_by_id(self, entity_id: str) -> AgentDefinition:
        url = f"{NEOTOMA_BASE_URL}/entities/{entity_id}"
        try:
            data = self._neotoma("GET", url)
            return self._parse(entity_id, data)
        except Exception as exc:
            log.warning(
                f"[{self.agent_name}] Could not load agent_definition {entity_id}: {exc}"
            )
            return self._stub()

    def _load_by_name(self) -> AgentDefinition:
        """Search for agent_definition by name field via POST /entities/query.

        GET /entities does not exist on local Neotoma (404); /entities/query is
        the canonical list route (same fix applied to the Anthus orchestrator in
        PR #58). Its response nests the field dict as entity.snapshot.snapshot.
        """
        url = f"{NEOTOMA_BASE_URL}/entities/query"
        body = {
            "entity_type": "agent_definition",
            "search": self.agent_name,
            "limit": 5,
            "include_snapshots": True,
        }
        try:
            data = self._neotoma("POST", url, body)
            entities = data.get("entities", [])
            for ent in entities:
                # Unwrap the doubly-nested snapshot to the flat field dict.
                outer = ent.get("snapshot") or {}
                snap = outer.get("snapshot", outer)
                if str(snap.get("name", "")).lower() == self.agent_name:
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
        # Extract a representative observation_id from the reducer provenance map.
        # We use "name" as the anchor field; fall back to any non-null value.
        provenance = snap.get("provenance") or {}
        last_obs_id = provenance.get("name") or next(
            (v for v in provenance.values() if v), ""
        )
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
            last_observation_id=str(last_obs_id) if last_obs_id else "",
        )

    def _stub(self) -> AgentDefinition:
        return AgentDefinition(
            name=self.agent_name,
            aauth_sub=f"{self.agent_name}@ateles-swarm",
            agent_grant="service",
            tool_allowlist="*",
            status="active",
        )

    def load_active_policies(self) -> list[dict]:
        """
        Fetch this agent's live agent_policy entities (status active or
        provisional) from Neotoma. These include autonomously-generalized,
        agent-local policies produced by the generalizer. Returns snapshot
        dicts; empty list if Neotoma is unreachable.

        Provisional policies ARE returned and applied — that exposure is
        exactly what matures them. Their effect remains agent-local and
        reversible (a contradicting drift signal suspends them).
        """
        # Need either a bearer token or per-agent signing to authenticate.
        if not NEOTOMA_BEARER_TOKEN and not (
            ns.via_cli_enabled() and ns.agent_identity(self.agent_name)
        ):
            return []
        agent_sub = f"{self.agent_name}@ateles-swarm"
        try:
            data = self._neotoma(
                "POST",
                f"{NEOTOMA_BASE_URL}/retrieve_entities",
                {
                    "entity_type": "agent_policy",
                    "limit": 200,
                    "include_snapshots": True,
                },
            )
        except Exception as exc:
            log.warning(f"[{self.agent_name}] could not load agent_policy: {exc}")
            return []

        out: list[dict] = []
        for e in data.get("entities", []):
            snap = e.get("snapshot") or {}
            if snap.get("agent_sub") != agent_sub:
                continue
            if snap.get("status") not in ("active", "provisional"):
                continue
            out.append(snap)
        return out

    def render_policy_prompt(self) -> str:
        """
        Render this agent's active/provisional policies as a markdown block to
        append to the dispatch system prompt — turning the advisory consultation
        protocol into reliable application. Returns "" when there are none.
        """
        policies = self.load_active_policies()
        if not policies:
            return ""
        lines = [
            "\n\n## Active agent policies (apply these)\n",
            "These standing policies were learned for you. `provisional` ones "
            "are being validated by use — follow them and emit a "
            "`strategy_drift_signal` if one is wrong.\n",
        ]
        for p in policies:
            kind = p.get("rule_kind", "prefer")
            status = p.get("status", "active")
            rule = p.get("rule") or p.get("description", "")
            lines.append(f"- ({kind}, {status}) {rule}")
        return "\n".join(lines)
