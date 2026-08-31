"""
lib/daemon_runtime/agent_loader.py — Load agent_definition from Neotoma at daemon startup.

Each T3 daemon calls AgentLoader(name).load() at startup to get its
configuration from Neotoma. No config files. Updating an agent's prompt
or tool_allowlist is a Neotoma correct() call — no code commit.

If Neotoma is unreachable the loader returns a STUB definition rather than
raising, so a daemon does not crash on a transient outage. A stub is a FAILURE,
not a degraded success: it carries an empty ``prompt_markdown`` and a wildcard
``tool_allowlist``, so an agent dispatched on one runs with no role instructions
and unrestricted tools. Every stub is logged at ERROR and flagged
``is_stub=True`` with a ``load_error`` reason. Callers MUST check ``is_stub``
before treating a definition as loaded.
"""

from __future__ import annotations

import json
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
    # True when this definition is the fallback stub rather than a real Neotoma
    # load — i.e. the load FAILED. A stub carries prompt_markdown="" and
    # tool_allowlist="*", so a caller that treats it as a definition dispatches
    # an agent with NO prompt and UNRESTRICTED tools while reporting success.
    # Callers MUST branch on this rather than assume load() succeeded.
    is_stub: bool = False
    # Why the load failed (transport error, 404, no matching entity). Empty on
    # a successful load. Distinguishes "Neotoma said no rows" from "the request
    # never succeeded" — the same distinction execution/mcp/ateles/server.py
    # records via _last_transport_error.
    load_error: str = ""

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
        # String shape: "*" / empty -> wildcard.
        text = str(raw).strip()
        if not text or text == "*":
            return ["*"]
        # Neotoma stores tool_allowlist as a JSON-array STRING (e.g.
        # '["Bash", "Bash(gh pr:*)", ...]'). Parse that shape FIRST — a naive
        # comma-split would keep the surrounding brackets/quotes on each token,
        # yielding garbage like '"Bash(gh pr:*)"' that the CLI rejects as a
        # malformed --allowedTools rule and fails the whole dispatch (the
        # Bash(...:*) grammar makes the rejection fatal, not silently ignored).
        if text.startswith("[") and text.endswith("]"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, (list, tuple)):
                    items = [str(t).strip() for t in parsed if str(t).strip()]
                    return items or ["*"]
            except (ValueError, TypeError):
                pass  # fall through to comma-split for a non-JSON bracketed string
        # Legacy / hand-authored comma-separated shape.
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
            return self._stub(
                f"GET /entities/{entity_id} failed: {type(exc).__name__}: {exc}"
            )

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
            return self._stub(
                f"no agent_definition named {self.agent_name!r} in "
                f"{len(entities)} result(s) from POST /entities/query"
            )
        except Exception as exc:
            return self._stub(
                f"POST /entities/query failed: {type(exc).__name__}: {exc}"
            )

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

    def _stub(self, reason: str = "unknown") -> AgentDefinition:
        """Fallback definition for a FAILED load — never a successful one.

        A stub has an EMPTY prompt_markdown and a WILDCARD tool_allowlist. An
        agent dispatched on one runs with no role instructions and unrestricted
        tools. That must never present as a normal load, so the stub is logged
        at ERROR and marked ``is_stub`` with the failure reason attached for the
        caller to branch on.
        """
        log.error(
            f"[{self.agent_name}] agent_definition load FAILED ({reason}) — "
            "falling back to a STUB with an EMPTY prompt and wildcard tools. "
            "Any agent dispatched on this definition has no role instructions."
        )
        return AgentDefinition(
            name=self.agent_name,
            aauth_sub=f"{self.agent_name}@ateles-swarm",
            agent_grant="service",
            tool_allowlist="*",
            status="active",
            is_stub=True,
            load_error=reason,
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
            # POST /entities/query is the canonical list route. /retrieve_entities
            # is the MCP TOOL name, not a REST path, and 404s on the hosted
            # instance — see _load_by_name and issue_spec.py for the same gotcha.
            data = self._neotoma(
                "POST",
                f"{NEOTOMA_BASE_URL}/entities/query",
                {
                    "entity_type": "agent_policy",
                    "limit": 200,
                    "include_snapshots": True,
                },
            )
        except Exception as exc:
            log.error(
                f"[{self.agent_name}] could not load agent_policy: {exc} — "
                "dispatching WITHOUT this agent's learned policies"
            )
            return []

        out: list[dict] = []
        for e in data.get("entities", []):
            # /entities/query returns the field dict either flat under
            # "snapshot" or nested one level deeper; accept both.
            outer = e.get("snapshot") or {}
            snap = outer.get("snapshot", outer) if isinstance(outer, dict) else {}
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
