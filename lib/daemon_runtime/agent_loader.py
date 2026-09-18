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

Status enforcement (ateles#562). ``agent_definition.status`` used to be read,
logged, and then ignored — setting an agent to ``retired`` had no runtime
effect. ``evaluate_status`` and ``enforce_status_or_exit`` below make it
load-bearing, with three outcomes rather than two: RUN, REFUSE, and WARN.

WARN exists because the field is decorative in BOTH directions today, so naive
enforcement would halt production. As of 2026-08-31, 18 of 40 agent_definition
entities are ``planned``, and several of them run continuously — ``neotoma-agent``
has a daemon and a live grant while marked ``planned``, and ``lanius`` is the
swarm's busiest dispatcher while marked ``planned``. Refusing on ``planned``
would take those down on deploy.

So only statuses that express an operator's intent to STOP the agent refuse:
``retired`` and ``disabled``. ``planned`` warns loudly and keeps running, which
surfaces the data problem without acting on data known to be wrong. Correcting
those entities is tracked separately; once the data is accurate, ``planned``
can be promoted into ``REFUSING_STATUSES`` in one line.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass, field
from enum import Enum

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


class StatusAction(str, Enum):
    """What a daemon should do given its agent_definition.status (#562).

    Three outcomes, not two. WARN is the state for "this status says the agent
    should not be running, but the data is known to be unreliable" — it keeps
    the daemon up while making the discrepancy visible, instead of either
    silently ignoring the field (the #562 defect) or halting production on data
    we already know is wrong.
    """

    RUN = "run"
    REFUSE = "refuse"
    WARN = "warn"


# Statuses that express an operator's intent to STOP the agent. Only these
# refuse to start. Keep this set narrow: every addition can halt a daemon.
REFUSING_STATUSES = frozenset({"retired", "disabled", "revoked"})

# Statuses that mean the agent is expected to run.
RUNNING_STATUSES = frozenset({"active", "active-pending-deploy", "provisional"})

# Statuses that SHOULD refuse on their plain meaning, but whose stored values
# are currently unreliable enough that acting on them would cause an outage.
#
# As of 2026-08-31, 18 of 40 agent_definition entities are "planned", including
# agents that demonstrably run in production (neotoma-agent has a daemon, a
# LaunchAgent, and a live agent_grant; lanius is the swarm's busiest dispatcher).
# The field is decorative in both directions — five "active" agents have never
# been invoked. Until the entities are corrected, "planned" warns rather than
# refuses. Promote it into REFUSING_STATUSES once the data is accurate.
WARNING_STATUSES = frozenset({"planned", "proposed", "draft"})

# Status assigned when no agent_definition could be loaded at all. Deliberately
# NOT "active": a stub is an absence of information, and #562 notes the stub
# previously defaulted to active with tool_allowlist="*", which made an
# unregistered daemon look fully configured.
UNDEFINED_STATUS = "undefined"


def evaluate_status(status: str) -> tuple[StatusAction, str]:
    """Map an agent_definition.status to an action and a human-readable reason.

    Unrecognised statuses WARN rather than refuse: a status nobody anticipated
    is missing information, and this function must not invent an outage from a
    typo or a value added by a future migration.
    """
    s = (status or "").strip().lower()
    if s in REFUSING_STATUSES:
        return (
            StatusAction.REFUSE,
            f"agent_definition.status is {s!r} — the agent is not permitted to run",
        )
    if s in RUNNING_STATUSES:
        return StatusAction.RUN, f"status={s}"
    if s == UNDEFINED_STATUS or not s:
        return (
            StatusAction.WARN,
            "no agent_definition could be loaded (running on a stub); the "
            "agent is unregistered and its tool allowlist is unscoped",
        )
    if s in WARNING_STATUSES:
        return (
            StatusAction.WARN,
            f"agent_definition.status is {s!r} but the daemon is running — "
            "either the entity is stale or the daemon should not be deployed",
        )
    return (
        StatusAction.WARN,
        f"agent_definition.status is {s!r}, which is not a recognised status",
    )


def enforce_status_or_exit(agent_def: "AgentDefinition", daemon_name: str) -> None:
    """Refuse to start when status says the agent must not run (#562).

    Exits non-zero on REFUSE — matching the revoked-grant convention already
    used at each of these call sites — and logs loudly on WARN. Call this
    immediately after the startup status log line, before subscribing or
    dispatching.
    """
    action, reason = evaluate_status(agent_def.status)
    if action is StatusAction.REFUSE:
        log.error(
            f"[{daemon_name}] Refusing to start: {reason}. "
            f"agent_definition={agent_def.entity_id or '<stub>'}. "
            "Correct the entity's status, or unload this daemon's LaunchAgent."
        )
        sys.exit(1)
    if action is StatusAction.WARN:
        log.warning(f"[{daemon_name}] Status check: {reason}")


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

        Its status is UNDEFINED_STATUS, not "active" (ateles#562). A stub is an
        absence of information; reporting it as active made an unregistered
        daemon indistinguishable from a configured one in logs and in every
        downstream status check. UNDEFINED_STATUS WARNs rather than refusing, so
        a transient Neotoma outage still cannot invent an outage of its own.
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
            status=UNDEFINED_STATUS,
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


# Rules 1, 2, and 6 bind only when this agent REFERS_TO a standing_rule.
# Pasting the sentences into prompt_markdown does not count (ateles#1102).
_REQUIRED_OPERATOR_RULES = (1, 2, 6)
_RULE_TITLE_RE = re.compile(r"^([0-9]+)\. ")
_RULES_HINT = (
    "hint=resolve the related entity on the agent; "
    "do not paste rule text into prompt_markdown or CLAUDE.md — docs/operator_rules.md"
)


@dataclass
class OperatorRules:
    """Resolved operator-rule block for one injecting surface.

    ``status`` is ``bound``, ``unbound``, or ``incomplete``. This is not an
    agent-definition stub: ``is_stub`` is never set here.
    """

    status: str
    missing: list[int]
    block: str


def _rules_token_line(kind: str, missing: list[int]) -> str:
    numbers = ",".join(str(n) for n in missing)
    return f"[{kind}] missing={numbers} {_RULES_HINT}"


def unbound_operator_rules() -> OperatorRules:
    missing = list(_REQUIRED_OPERATOR_RULES)
    return OperatorRules(
        status="unbound",
        missing=missing,
        block=_rules_token_line("rules-unbound", missing),
    )


def _incomplete_operator_rules(missing: list[int]) -> OperatorRules:
    ordered = sorted(missing)
    return OperatorRules(
        status="incomplete",
        missing=ordered,
        block=_rules_token_line("rules-incomplete", ordered),
    )


def _entity_fields(row: dict) -> dict:
    """Flatten the snapshot shapes /entities/query and /relationships return."""
    if not isinstance(row, dict):
        return {}
    outer_type = row.get("entity_type")
    snap = row.get("snapshot")
    if isinstance(snap, dict):
        inner = snap.get("snapshot")
        fields = dict(inner) if isinstance(inner, dict) else dict(snap)
    else:
        fields = dict(row)
    if outer_type and not fields.get("entity_type"):
        fields["entity_type"] = outer_type
    return fields


def _rule_enabled(fields: dict) -> bool:
    if "enabled" not in fields:
        return True
    value = fields.get("enabled")
    if value is False:
        return False
    if isinstance(value, str) and value.strip().lower() == "false":
        return False
    return True


def _related_rows(payload: dict, agent_id: str) -> list[dict]:
    """Standing-rule neighbors this agent is the source of a REFERS_TO edge to."""
    if not isinstance(payload, dict):
        return []
    related = payload.get("related_entities") or {}
    if not isinstance(related, dict):
        related = {}
    if payload.get("outgoing") is not None:
        edges = payload.get("outgoing") or []
    else:
        edges = payload.get("relationships") or []
    rows: list[dict] = []
    for rel in edges:
        if not isinstance(rel, dict):
            continue
        if str(rel.get("relationship_type", "")).upper() != "REFERS_TO":
            continue
        if rel.get("source_entity_id") != agent_id:
            continue
        target_id = rel.get("target_entity_id")
        row = related.get(target_id) if target_id else None
        if row is None and isinstance(rel.get("target"), dict):
            row = rel["target"]
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _kept_rule_text(rows: list[dict]) -> dict[int, str]:
    """Map rule number → text. Empty string means the neighbor passed filters
    but ``rule_text`` is blank. A number absent from the map did not pass.
    """
    texts: dict[int, list[str]] = {}
    for row in rows:
        fields = _entity_fields(row)
        if str(fields.get("entity_type") or "") != "standing_rule":
            continue
        if fields.get("scope") != "ateles":
            continue
        if not _rule_enabled(fields):
            continue
        match = _RULE_TITLE_RE.match(str(fields.get("title") or ""))
        if not match:
            continue
        number = int(match.group(1))
        texts.setdefault(number, []).append(str(fields.get("rule_text") or ""))
    kept: dict[int, str] = {}
    for number, values in texts.items():
        usable = [value.strip() for value in values if value.strip()]
        kept[number] = usable[0] if usable else ""
    return kept


def _get_related(loader: AgentLoader, entity_id: str) -> dict:
    """GET the related-entity route.

    The spec names ``/entities/<id>/related``. The registered Neotoma route is
    ``/entities/<id>/relationships`` (actions.ts). A 404 on the spec path falls
    back so a session can still receive the edges; any other error stays unbound.
    """
    primary = (
        f"{NEOTOMA_BASE_URL}/entities/{entity_id}/related?expand_entities=true"
    )
    try:
        return loader._neotoma("GET", primary)
    except httpx.HTTPStatusError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status != 404:
            raise
    fallback = (
        f"{NEOTOMA_BASE_URL}/entities/{entity_id}/relationships"
        "?expand_entities=true"
    )
    return loader._neotoma("GET", fallback)


def resolve_operator_rules(agent_name: str = "ateles") -> OperatorRules:
    """Resolve rules 1, 2, and 6 for ``agent_name`` from related standing_rule rows.

    Does not call ``_stub`` and does not read ``prompt_markdown``. A name-query
    failure, a missing entity, or a related-list failure is ``unbound``.
    """
    loader = AgentLoader(agent_name)
    try:
        data = loader._neotoma(
            "POST",
            f"{NEOTOMA_BASE_URL}/entities/query",
            {
                "entity_type": "agent_definition",
                "search": loader.agent_name,
                "limit": 5,
                "include_snapshots": True,
            },
        )
    except Exception:
        return unbound_operator_rules()

    entity_id = ""
    for ent in data.get("entities") or []:
        if not isinstance(ent, dict):
            continue
        outer = ent.get("snapshot") or {}
        snap = outer.get("snapshot", outer) if isinstance(outer, dict) else {}
        if not isinstance(snap, dict):
            continue
        if str(snap.get("name", "")).lower() == loader.agent_name:
            entity_id = str(ent.get("entity_id") or "")
            break
    if not entity_id:
        return unbound_operator_rules()

    try:
        related = _get_related(loader, entity_id)
    except Exception:
        return unbound_operator_rules()

    kept = _kept_rule_text(_related_rows(related, entity_id))
    present = [n for n in _REQUIRED_OPERATOR_RULES if n in kept]
    if not present:
        return unbound_operator_rules()
    missing = [n for n in _REQUIRED_OPERATOR_RULES if not kept.get(n, "").strip()]
    if missing:
        return _incomplete_operator_rules(missing)
    block = "\n".join(kept[n] for n in _REQUIRED_OPERATOR_RULES)
    return OperatorRules(status="bound", missing=[], block=block)
