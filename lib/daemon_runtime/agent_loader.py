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
import sys
from dataclasses import dataclass, field
from enum import Enum

import httpx

try:  # package import (normal daemon runtime) with script-import fallback
    from . import neotoma_signed as ns
except ImportError:  # pragma: no cover
    import neotoma_signed as ns  # type: ignore

log = logging.getLogger(__name__)

# ateles#1118. `docs/foundation/data_model.md` declares the scoping contract for
# `agent_policy`, and this is the one implementation of it — three readers
# (`AgentLoader`, `generalizer.fetch_agent_policies`, `revert_auto_policy`) must
# agree, and a copied predicate is how they drift (CLAUDE.md: derive from the
# single source, never copy a set of values into code).
#
# The design's words: `scope` is CLOSED to `global`, `swarm`, `agent`;
# `agent_sub` is "the one field that scopes a rule to an agent"; and a rule of
# `scope: agent` with no `agent_sub` is refused at the write.
#
# The defect this closes: the loader required `agent_sub` equality
# UNCONDITIONALLY, so the 12 `scope: global` rows reached NO agent rather than
# every agent — the opposite of what global means.
POLICY_SCOPES_REACHING_EVERY_AGENT = frozenset({"global", "swarm"})

# The whole closed vocabulary `data_model.md` gives `agent_policy.scope`:
# the two that reach every agent, plus `agent`, which reaches only the agent
# its `agent_sub` names. Derived from the set above rather than restated, so
# the two cannot drift. A reader that must refuse a scope value outside the
# vocabulary (the session index, ateles#1268) tests membership here.
POLICY_SCOPES = POLICY_SCOPES_REACHING_EVERY_AGENT | frozenset({"agent"})


def policy_binds_agent(snap: dict, agent_sub: str) -> bool:
    """Whether one `agent_policy` row binds the agent named by `agent_sub`,
    under the OLD field-based (`scope`/`agent_sub`) reading.

    SUPERSEDED for the two readers decision 114 names (2026-09-25, master
    plan `decisions.agent_policy_binds_agent_by_graph_edge`): both
    `AgentLoader.load_active_policies` (this module) and
    `policy_skill_renderer._session_scope_ok` now call
    `policy_binds_agent_by_edge` instead, which does not consult `scope` or
    `agent_sub` for an agent-specific row at all — only a `GOVERNS` edge (or
    a swarm-wide `scope` on an edgeless row) binds. This function is KEPT,
    not removed or deprecated, because it still has a live production
    caller decision 114 did not name or touch: `generalizer.py`'s
    `fetch_agent_policies` (reads an agent's own existing policies before
    proposing a new one — proposal-only, never a live write, so its blast
    radius is a wrong PROPOSAL rather than a wrong live binding). Migrating
    that third caller to the edge predicate is out of scope for decision
    114 as ruled and is tracked separately rather than folded in here
    unreviewed.

    - `global` / `swarm` reach every agent, with or without an `agent_sub`.
    - `agent` binds only the agent its `agent_sub` names.
    - An UNRECOGNISED or absent `scope` binds nobody unless `agent_sub`
      matches exactly. `scope` is the field carrying the reach of a rule, so
      an unreadable value fails CLOSED to the narrowest reading rather than
      silently broadcasting a rule to the whole swarm (`principles.md`,
      principle 5: fail closed on the field that carries the safety meaning).
    """
    scope = str(snap.get("scope") or "").strip().lower()
    if scope in POLICY_SCOPES_REACHING_EVERY_AGENT:
        return True
    row_sub = str(snap.get("agent_sub") or "").strip()
    return bool(row_sub) and row_sub == agent_sub


# Operator ruling 2026-09-25 (decision 114, master plan
# `decisions.agent_policy_binds_agent_by_graph_edge`): an agent-specific
# `agent_policy` is tied to the agent(s) it governs by a graph edge to their
# `agent_definition`, resolved by traversal — not by the `scope`/`agent_sub`
# fields `policy_binds_agent` above reads. Those fields are SUPERSEDED by
# this edge, not repaired in place: G32(a) (`migration.md`) already found
# `agent_sub` populated on zero live rows and the agent identifier written
# into `domain` instead, so a fix that stayed inside the two fields would
# have had to resolve that conflation on the same pass. The edge sidesteps
# it — an agent-specific row's binding is a relationship, not a string
# comparison against a field two different things have been written into.
#
# No relationship type in this instance's registered vocabulary
# (`list_relationship_types`) names "a rule governs the agent it binds" —
# the closest, `manages`, `REFERS_TO` and `related_to`, are respectively a
# principal-to-principal/org relation, a citation ("mentions or cites", too
# weak for a binding), and a generic association carrying no directional
# claim at all. `GOVERNS` is minted for this: `agent_policy` -> `agent_definition`,
# "the source rule or policy binds the behaviour of the target agent."
# Registration is a governance act gated on an admitted `agent_grant` with
# the `register_relationship_type` capability (`services/agent_capabilities.ts`
# in the neotoma repo — "Governance registration is always grant-gated,
# independent of rollout flags"); the plain operator bearer token this
# migration otherwise runs under resolves to no admitted agent identity and
# cannot register it. Until an admitted grant (the operator, or an agent
# holding that capability) runs `register_relationship_type`, every write
# this module attempts against `GOVERNS` fails with
# `unregistered_relationship_type` — the resolver and its tests do not
# depend on the type being registered (they operate on already-fetched edge
# data), but the migration script's `--apply` does, and reports that error
# rather than silently no-op'ing (`migrate_agent_policy_edges.py`).
AGENT_POLICY_GOVERNS_EDGE = "GOVERNS"


def policy_binds_agent_by_edge(
    snap: dict, agent_definition_id: str, governs: dict[str, frozenset[str]]
) -> bool:
    """Whether one `agent_policy` row binds the agent whose `agent_definition`
    entity id is `agent_definition_id`, under the graph-edge resolver
    (operator ruling 2026-09-25, decision 114). The ONE shared predicate for
    both readers (`AgentLoader.load_active_policies` and
    `policy_skill_renderer._session_scope_ok`) — a copied predicate is how
    the two drift (CLAUDE.md: derive from the single source).

    ``governs`` is the batched edge map this module's
    ``fetch_governs_edges`` returns: ``{agent_policy_entity_id: frozenset of
    agent_definition_entity_id}``, built from ONE `/list_relationships` call
    per load rather than one per row.

    The rule, stated once:
      - A row with a `GOVERNS` edge to ANY `agent_definition` binds ONLY the
        agent(s) it has an edge to — never another agent, regardless of what
        `scope` says. An edge is more specific than a scope value, so it
        overrides rather than adds to it.
      - A row with NO `GOVERNS` edge binds every agent when `scope` is
        `global` or `swarm` (`POLICY_SCOPES_REACHING_EVERY_AGENT`) — the
        swarm-wide case, which the ruling says "carries no edge."
      - A row with no edge and no recognised swarm-wide scope binds NOBODY.
        This is the fail-closed default (`principles.md #5`): an edgeless,
        scope-unrecognised row is not a swarm-wide rule by default, and an
        absent or malformed `agent_sub`/`scope` pair no longer has a second
        chance to match by string equality — that fallback is what this
        ruling supersedes.

    `agent_policy.scope` and `.agent_sub` are read NOWHERE in this function.
    They are superseded, not consulted as a fallback — a row migrated to an
    edge and a row never migrated must be judged by the same rule, or the
    predicate itself would silently re-introduce the two-fields-for-one-
    concept conflation G32(a) already found.
    """
    entity_id = str(snap.get("_entity_id") or snap.get("entity_id") or "").strip()
    edge_targets = governs.get(entity_id, frozenset())
    if edge_targets:
        return agent_definition_id in edge_targets
    scope = str(snap.get("scope") or "").strip().lower()
    return scope in POLICY_SCOPES_REACHING_EVERY_AGENT


def fetch_governs_edges(
    base_url: str = "",
    timeout: float = 10.0,
    *,
    _request: "callable | None" = None,
) -> dict[str, frozenset[str]]:
    """Batch-fetch every live `GOVERNS` edge in ONE `/list_relationships`
    call (not one per `agent_policy` row) and return
    ``{agent_policy_entity_id: frozenset(agent_definition_entity_id, ...)}``.

    `/list_relationships` accepts `relationship_type` alone with no
    `entity_id` (`ListRelationshipsRequestSchema`'s `.refine` requires only
    one of the four selector fields) — the whole vocabulary of `GOVERNS`
    edges reachable at once, same shape as `POLICY_QUERY_BODY`'s
    `/entities/query` call one level up.

    Raises on transport failure — this module never swallows a fetch error
    into an empty result (CLAUDE.md: a write/read that fails must not look
    like an empty result); callers already have a fail-open path for
    Neotoma being unreachable (`AgentLoader.load_active_policies`'s
    try/except, `policy_skill_renderer`'s hook-level fail-open) and decide
    how to degrade at that layer, not inside this fetch.

    `_request` is an injection point for a caller (`policy_skill_renderer`)
    that must stay stdlib-only and cannot import `httpx` — it passes its own
    `urllib`-based POST function with the same ``(url, body, timeout) ->
    dict`` shape. The default here uses `httpx`, matching every other
    request this module makes.
    """
    url = f"{(base_url or NEOTOMA_BASE_URL).rstrip('/')}/list_relationships"
    body = {"relationship_type": AGENT_POLICY_GOVERNS_EDGE, "limit": 500}
    if _request is not None:
        data = _request(url, body, timeout)
    else:
        resp = httpx.post(url, json=body, headers=_auth_headers(), timeout=timeout)
        resp.raise_for_status()
        data = resp.json()

    out: dict[str, set[str]] = {}
    for rel in (data.get("relationships") or []) if isinstance(data, dict) else []:
        if not isinstance(rel, dict):
            continue
        source = str(rel.get("source_entity_id") or "").strip()
        target = str(rel.get("target_entity_id") or "").strip()
        if not source or not target:
            continue
        out.setdefault(source, set()).add(target)
    return {k: frozenset(v) for k, v in out.items()}


def resolve_agent_definition_id(
    agent_sub: str,
    base_url: str = "",
    timeout: float = 10.0,
    *,
    _request: "callable | None" = None,
) -> str | None:
    """Resolve an agent's `agent_sub` (e.g. ``corvus@ateles-swarm``) to its
    `agent_definition` entity id, via the SAME name-search route
    `AgentLoader._load_by_name` already uses — `POST /entities/query` is the
    canonical list route (`GET /entities` 404s on the hosted instance).

    An agent's name is the local part of its sub (`corvus@ateles-swarm` ->
    `corvus`); `aauth_sub`, when a row carries one, is matched first and
    preferred, since it is the field the design names as the identifying
    credential (`data_model.md`: "the `sub` its `principal_binding`
    carries"). Falls back to a name match when no row's `aauth_sub` matches
    exactly — most `agent_definition` rows predate the field.

    Returns None (never a guess) when no row matches, or on transport
    failure — this is a lookup a caller treats as "unresolvable" rather
    than raising, since an unresolved session principal must bind no
    `agent`-scoped row (the same fail-closed posture `policy_binds_agent`
    already takes on an empty `agent_sub`).
    """
    name = agent_sub.split("@", 1)[0].strip().lower()
    if not name:
        return None
    url = f"{(base_url or NEOTOMA_BASE_URL).rstrip('/')}/entities/query"
    body = {
        "entity_type": "agent_definition",
        "search": name,
        "limit": 5,
        "include_snapshots": True,
    }
    try:
        if _request is not None:
            data = _request(url, body, timeout)
        else:
            resp = httpx.post(url, json=body, headers=_auth_headers(), timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
    except Exception:  # noqa: BLE001 — unresolvable, not fatal; caller degrades
        return None

    entities = (data.get("entities") or []) if isinstance(data, dict) else []
    name_match: str | None = None
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        outer = ent.get("snapshot") or {}
        snap = outer.get("snapshot", outer) if isinstance(outer, dict) else {}
        if not isinstance(snap, dict):
            continue
        entity_id = str(ent.get("entity_id") or "")
        if not entity_id:
            continue
        if str(snap.get("aauth_sub", "")).strip().lower() == agent_sub.strip().lower():
            return entity_id
        if name_match is None and str(snap.get("name", "")).strip().lower() == name:
            name_match = entity_id
    return name_match


# Statuses that count as "live" for any reader of agent_policy. Shared so a
# session-wide reader and a per-agent reader can never define "active" two
# different ways (ateles#1268 round-2, Waxwing: two independent
# fetch-and-filter implementations for the same read can silently diverge).
POLICY_LIVE_STATUSES = frozenset({"active", "provisional"})

# The query body every agent_policy reader sends. POST /entities/query is the
# canonical list route — GET /entities/agent_policy 404s on the hosted
# instance (same gotcha as _load_by_name below and issue_spec.py). Shared so
# the query shape (entity_type, limit, include_snapshots) cannot drift
# between readers.
POLICY_QUERY_BODY: dict = {
    "entity_type": "agent_policy",
    "limit": 200,
    "include_snapshots": True,
}


def unwrap_policy_entities(data: dict) -> list[dict]:
    """The ONE unwrap-and-status-filter step for an `/entities/query` response
    against `entity_type=agent_policy`. Transport-agnostic: takes the already-
    parsed JSON body, so both the httpx-based reader (`AgentLoader`, this
    module) and a stdlib-only `urllib` reader (the SessionStart hook via
    `policy_skill_renderer.py`, which cannot import httpx) can call this same
    function after making the request their own way — the ONE place the
    unwrap shape and the "what counts as live" status set are defined
    (Waxwing, ateles#1268 round 2: "route the renderer through it... so
    there's ONE reader of agent_policy").

    Unwraps the response's double-nested `snapshot.snapshot` shape (the same
    quirk `_load_by_name` and `render_agent_docs.py`'s `_unwrap` handle for
    `agent_definition`), filters to `status in (active, provisional)`, and
    stamps `_entity_id` onto each returned snapshot from the entity wrapper.
    Applies NO scope filter — scope semantics differ between a single-agent
    reader (`AgentLoader.load_active_policies`, `policy_binds_agent(snap,
    agent_sub)`) and a session-wide reader (the union across every named
    agent), so scoping stays the caller's job; this function only answers
    "what rows exist and are live," never "who they bind."
    """
    entities = (
        (data.get("entities") or data.get("results") or [])
        if isinstance(data, dict)
        else []
    )
    out: list[dict] = []
    for e in entities:
        if not isinstance(e, dict):
            continue
        outer = e.get("snapshot") or {}
        snap = outer.get("snapshot", outer) if isinstance(outer, dict) else {}
        if not isinstance(snap, dict) or not snap:
            continue
        if str(snap.get("status", "")).strip().lower() not in POLICY_LIVE_STATUSES:
            continue
        snap["_entity_id"] = e.get("entity_id") or snap.get("entity_id", "")
        out.append(snap)
    return out


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
                "POST", f"{NEOTOMA_BASE_URL}/entities/query", POLICY_QUERY_BODY
            )
        except Exception as exc:
            log.error(
                f"[{self.agent_name}] could not load agent_policy: {exc} — "
                "dispatching WITHOUT this agent's learned policies"
            )
            return []

        # Shared unwrap+status-filter — the ONE reader of agent_policy's
        # response shape, also used by policy_skill_renderer.py's session-wide
        # fetch (ateles#1268 round 2, Waxwing). Scoping (which rows bind THIS
        # agent) stays here, since that predicate differs from the session-
        # wide caller's union.
        live_rows = unwrap_policy_entities(data)

        # Decision 114 (2026-09-25): resolve this agent's `agent_definition`
        # id and the batched `GOVERNS` edge map ONCE per load (not once per
        # row), then judge every row with the ONE shared predicate,
        # `policy_binds_agent_by_edge`. Both fetches degrade to "resolve/bind
        # nothing" on failure rather than raising — a Neotoma hiccup on the
        # edge lookup must not take down a load that would otherwise have
        # succeeded on the entity fetch above.
        agent_definition_id = None
        try:
            agent_definition_id = resolve_agent_definition_id(agent_sub, NEOTOMA_BASE_URL)
        except Exception as exc:  # noqa: BLE001 — degrade, do not raise
            log.warning(
                f"[{self.agent_name}] could not resolve agent_definition id "
                f"for {agent_sub!r}: {exc} — edge-scoped policies unresolvable "
                "this load"
            )
        governs: dict[str, frozenset[str]] = {}
        try:
            governs = fetch_governs_edges(NEOTOMA_BASE_URL)
        except Exception as exc:  # noqa: BLE001 — degrade, do not raise
            log.warning(
                f"[{self.agent_name}] could not fetch GOVERNS edges: {exc} — "
                "treating every row as edgeless this load"
            )

        out: list[dict] = []
        scoping_populated = 0
        for snap in live_rows:
            entity_id = str(snap.get("_entity_id") or "")
            if governs.get(entity_id) or str(snap.get("agent_sub") or "").strip():
                scoping_populated += 1
            if not policy_binds_agent_by_edge(snap, agent_definition_id or "", governs):
                continue
            out.append(snap)

        # ateles#1118: the loud-failure path above fires on an EXCEPTION only.
        # A 200 returning rows that all fail the filter was indistinguishable
        # from an agent that legitimately has no policies — and most agents
        # legitimately have none, so the empty result looked correct while
        # every agent loaded zero. `agent_sub` was populated 0 of 25 rows
        # before the backfill this decision also obsoletes.
        #
        # Distinguishing the two cases is the point: an empty result is only
        # trustworthy when SOME row carries either scoping signal (an edge or
        # a legacy `agent_sub`) — a corpus with neither is the same
        # "everything reaches nobody" defect ateles#1118 found, now over the
        # edge instead of the field.
        if live_rows and not scoping_populated:
            log.error(
                f"[{self.agent_name}] agent_policy returned {len(live_rows)} "
                "row(s) but none carries a GOVERNS edge or a legacy "
                "`agent_sub` — the filter matched nothing because no row is "
                "agent-scoped by either mechanism, not because this agent "
                "has no policies. Dispatching WITHOUT policies. Run "
                "execution/scripts/migrate_agent_policy_edges.py to convert "
                "scope=agent/agent_sub rows to GOVERNS edges."
            )
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
