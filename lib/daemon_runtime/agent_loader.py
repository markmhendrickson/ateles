"""
lib/daemon_runtime/agent_loader.py — Load agent_definition from Neotoma at daemon startup.

Each T3 daemon calls AgentLoader(name).load() at startup to get its
configuration from Neotoma. No config files. Updating an agent's prompt
or tool_allowlist is a Neotoma correct() call — no code commit.

A definition that cannot be loaded FAILS CLOSED. ``AgentLoader.load()`` retries a
transient failure briefly (``LOAD_BACKOFFS_S``, capped by ``LOAD_TOTAL_CAP_S``) and,
if it still cannot load, raises ``AgentDefinitionUnavailable`` with a fixed reason
code and no response text. It never returns a stand-in definition: an agent
dispatched on one would run with an empty ``prompt_markdown`` and a wildcard
``tool_allowlist`` (no role instructions, unrestricted tools), and a daemon would
skip the status check that decides whether it may run at all.

Explicit ``allow_undefined=True`` returns an inert absence marker
(``is_stub=True``, no tools) ONLY when Neotoma answered and has no such
definition (``not_found``). It supplies no authority to dispatch. A transport
failure, a timeout or a server error can never produce it, with or without the opt-in.

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

Absent and unrecognized statuses refuse. Explicit stop statuses also refuse:
``retired`` and ``disabled``. ``planned`` warns loudly and keeps running, which
surfaces the data problem without acting on data known to be wrong. Correcting
those entities is tracked separately; once the data is accurate, ``planned``
can be promoted into ``REFUSING_STATUSES`` in one line.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import sys
import threading
import time
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


# A definition that cannot be loaded right now is retried briefly, then refused.
#
# The whole load is bounded by ONE DEADLINE computed when it starts
# (LOAD_TOTAL_CAP_S). Every request (the signed request, and the bearer request
# that follows a failed signed one) is launched with min(its default timeout, time
# left), and bounded as a whole, not per socket operation. A retry is started only
# if elapsed + its backoff + MIN_ATTEMPT_TIMEOUT_S still fits before the deadline.
# So the true worst case is LOAD_TOTAL_CAP_S plus process-teardown jitter.
#
# Reason codes that are retried say "the request did not get a usable answer"; any
# other answer is final at once.
LOAD_BACKOFFS_S = (0.5, 1.5)
LOAD_TOTAL_CAP_S = 25.0
SIGNED_REQUEST_TIMEOUT_S = 20.0  # the signing helper's own default
BEARER_REQUEST_TIMEOUT_S = 10.0
MIN_ATTEMPT_TIMEOUT_S = 2.0
_RETRYABLE_LOAD_CODES = frozenset(
    {"timeout", "transport_error", "server_error", "throttled", "busy"}
)
# The only HTTP status the lookup accepts. 201, 204, a redirect, a 4xx, a 5xx: all
# are refused before any body is read (see ``AgentLoader._neotoma``).
SUCCESS_STATUS = 200

# Request workers: a small FIXED pool owned by this module, one permit per worker, so
# a request is never queued behind another. With no free permit the load fails at once
# with the retryable reason "busy". A permit comes back when the WORKER finishes, not
# when the caller gives up, so live workers can never exceed LOAD_WORKERS however many
# loads time out. Each request's own timeouts come from the remaining budget, and a
# signed helper is killed and reaped at its timeout. (gate_waive's signing workers use
# the same approach.)
LOAD_WORKERS = 4
# Authoritative absence: a well-formed success response that positively says there
# is no definition. NOTHING else is absence (see ``_validated_entities``).
NOT_FOUND = "not_found"

_monotonic = time.monotonic
_sleep = time.sleep


class AgentDefinitionUnavailable(RuntimeError):
    """An agent_definition could not be loaded. The caller must NOT run the agent.

    ``code`` is one of a fixed set (timeout, transport_error, server_error,
    throttled, rejected, not_found, bad_response, invalid_name, unexpected). The message
    carries the agent name, the code and the attempt count and nothing from the
    response, the request or the underlying exception.
    """

    def __init__(self, agent_name: str, code: str, attempts: int) -> None:
        self.agent_name = agent_name
        self.code = code
        self.attempts = attempts
        super().__init__(
            f"agent_definition for {agent_name!r} is unavailable "
            f"({code}, {attempts} attempt(s)); the agent was not run"
        )

    @property
    def transient(self) -> bool:
        """True when a later attempt may succeed (everything except a definite 'no such definition')."""
        return self.code != NOT_FOUND


def load_failure_code(exc: BaseException) -> str:
    """Map a load exception to a fixed reason code. Never includes exception text."""
    if isinstance(exc, _LoadBusy):
        return "busy"
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, _TransportStatus):
        if exc.status == 429:
            return "throttled"
        if exc.status >= 500:
            return "server_error"
        return "rejected"
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        # A 404 (or any other 4xx or 3xx) on the lookup route is a ROUTE answer, not
        # "Neotoma has no such definition"; only a well-formed 200 can say that.
        if status == 429:
            return "throttled"
        if status >= 500:
            return "server_error"
        return "rejected"
    if isinstance(exc, httpx.TransportError):
        return "transport_error"
    if isinstance(exc, ValueError):  # includes json.JSONDecodeError
        return "bad_response"
    return "unexpected"


def evaluate_status(status: str) -> tuple[StatusAction, str]:
    """Map an agent_definition.status to an action and a human-readable reason.

    Absent and unrecognized statuses refuse. The known legacy planned, proposed
    and draft values retain their explicit warning treatment.
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
            StatusAction.REFUSE,
            "Neotoma has no admitted agent_definition for this agent",
        )
    if s in WARNING_STATUSES:
        return (
            StatusAction.WARN,
            f"agent_definition.status is {s!r} but the daemon is running — "
            "either the entity is stale or the daemon should not be deployed",
        )
    return (
        StatusAction.REFUSE,
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


def load_agent_definition_or_exit(daemon_name: str) -> "AgentDefinition":
    """Load a daemon's OWN definition at startup; exit non-zero if it cannot be loaded.

    The status check (``enforce_status_or_exit``) is what stops a retired agent
    from running, so a daemon that could not read its definition must not carry on
    as if the check had passed. Exit is non-zero, like the revoked-grant and
    refusing-status cases, so the service manager restarts it. An unregistered
    daemon also refuses: absence supplies no authority to start.
    """
    try:
        return AgentLoader(daemon_name).load()
    except AgentDefinitionUnavailable as exc:
        log.error(
            f"[{daemon_name}] Refusing to start: agent_definition unavailable "
            f"({exc.code}, {exc.attempts} attempt(s)). The status check could not "
            "run. Exiting so the service manager restarts the daemon."
        )
        sys.exit(1)


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
    tool_allowlist: "str | list[str]" = field(default_factory=list)
    agent_grant: str = "service"
    override_policy: str = ""
    aauth_sub: str = ""
    version: str = "1.0.0"
    notes: str = ""
    raw: dict = field(default_factory=dict)
    # Observation ID that produced the current snapshot (for dispatch pinning, ateles#22)
    last_observation_id: str = ""
    # True only for the "undefined" definition a caller explicitly opted into
    # (``load(allow_undefined=True)``) when Neotoma answered and has no definition
    # for this agent. It carries an empty prompt and no tools, so
    # such a caller must treat it as "no definition", never as a loaded one. A
    # FAILED load never produces it: that raises ``AgentDefinitionUnavailable``.
    is_stub: bool = False
    # Why there is no definition ("not_found"); empty on a real load.
    load_error: str = ""

    @property
    def tools(self) -> list[str]:
        """Return tool_allowlist as a list. ['*'] means all tools.

        Accepts tool_allowlist in any of the shapes Neotoma may store it:
          - explicit "*" -> all tools; missing/empty/invalid -> no tools
          - a JSON array / Python list (the canonical entity storage shape)
          - a comma-separated string (legacy / hand-authored shape)
        """
        raw = self.tool_allowlist
        if raw is None:
            return []
        # Array shape (canonical entity storage): list/tuple of tool names.
        if isinstance(raw, (list, tuple)):
            if not all(isinstance(t, str) for t in raw):
                return []
            return [t.strip() for t in raw if t.strip()]
        # Only an explicit "*" denotes wildcard authority.
        if not isinstance(raw, str):
            return []
        text = raw.strip()
        if text == "*":
            return ["*"]
        if not text:
            return []
        # Neotoma stores tool_allowlist as a JSON-array STRING (e.g.
        # '["Bash", "Bash(gh pr:*)", ...]'). Parse that shape FIRST — a naive
        # comma-split would keep the surrounding brackets/quotes on each token,
        # yielding garbage like '"Bash(gh pr:*)"' that the CLI rejects as a
        # malformed --allowedTools rule and fails the whole dispatch (the
        # Bash(...:*) grammar makes the rejection fatal, not silently ignored).
        if text.startswith("["):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, list) and all(isinstance(t, str) for t in parsed):
                    return [t.strip() for t in parsed if t.strip()]
            except (ValueError, TypeError):
                pass
            return []
        # Legacy / hand-authored comma-separated shape.
        return [t.strip() for t in text.split(",") if t.strip()]

    @property
    def is_operator(self) -> bool:
        return self.agent_grant == "operator"

    @property
    def is_service(self) -> bool:
        return self.agent_grant == "service"


_load_pool = concurrent.futures.ThreadPoolExecutor(
    max_workers=LOAD_WORKERS, thread_name_prefix="agent-def-load"
)
_load_permits = threading.BoundedSemaphore(LOAD_WORKERS)


class _LoadBusy(Exception):
    """Every request worker is in use; nothing was started."""


class _TransportStatus(Exception):
    """The transport returned a status other than the one accepted."""

    def __init__(self, status: int) -> None:
        super().__init__(f"status {status}")
        self.status = status


def _on_worker(fn, deadline: float):
    """Run ``fn(remaining_seconds)`` on an owned worker, bounded by ``deadline``.

    The worker re-reads the clock when it actually BEGINS: if the deadline has passed,
    or the caller has already given up, it does nothing. The caller waits no longer
    than the deadline; when it gives up it marks the work abandoned and the worker's
    late result is discarded. The permit is released by the worker's completion.
    """
    if not _load_permits.acquire(blocking=False):
        raise _LoadBusy()
    abandoned = threading.Event()

    def run():
        if abandoned.is_set():
            raise httpx.ReadTimeout("abandoned before it began")
        remaining = deadline - _monotonic()
        if remaining < MIN_ATTEMPT_TIMEOUT_S - 1e-9:
            raise httpx.ReadTimeout("deadline passed before the request began")
        return fn(remaining)

    try:
        future = _load_pool.submit(run)
    except BaseException:
        _load_permits.release()
        raise
    future.add_done_callback(lambda _f: _load_permits.release())
    try:
        return future.result(timeout=max(deadline - _monotonic(), 0.0))
    except concurrent.futures.TimeoutError:
        if future.done() and not future.cancelled() and future.exception() is not None:
            raise future.exception()
        abandoned.set()
        future.cancel()
        raise httpx.ReadTimeout("request exceeded its time budget") from None
    except BaseException:
        abandoned.set()
        raise


class AgentLoader:
    """
    Load an agent_definition entity from Neotoma by agent name.

    Priority of lookup:
      1. AGENT_DEFINITION_ID env var (e.g. MONEDULA_AGENT_DEFINITION_ID)
      2. Search by name field in agent_definition entities

    A load that cannot complete raises ``AgentDefinitionUnavailable`` (after a
    short bounded retry for a transient failure). There is no stand-in definition.
    """

    def __init__(self, agent_name: str) -> None:
        self.agent_name = agent_name.lower()
        self._prefix = agent_name.upper()

    def load(self, *, allow_undefined: bool = False) -> AgentDefinition:
        """
        Load agent_definition from Neotoma, or raise ``AgentDefinitionUnavailable``.

        A transient failure (timeout, transport error, 5xx, 429) is retried with
        the short backoff in ``LOAD_BACKOFFS_S`` under the single deadline
        ``LOAD_TOTAL_CAP_S``; any other failure is final at once.
        ``allow_undefined=True`` is an introspection opt-in: it turns ONLY
        authoritative absence (a well-formed 200 whose ``entities`` list is empty)
        into an inert "undefined" marker with no tools. It cannot authorize a
        dispatch or daemon startup. A malformed response, a route error, a redirect, a
        timeout: all of those raise, with or without the opt-in.
        """
        if not self.agent_name.strip():
            raise AgentDefinitionUnavailable(self.agent_name, "invalid_name", 0)
        if not NEOTOMA_BEARER_TOKEN:
            # Open-mode Neotoma instances accept unauthenticated reads. Proceed
            # without a token.
            log.info(
                f"[{self.agent_name}] NEOTOMA_BEARER_TOKEN not set — "
                "loading agent_definition without auth (open-mode Neotoma)"
            )

        try:
            return self._load_with_retry()
        except AgentDefinitionUnavailable as exc:
            if allow_undefined and exc.code == NOT_FOUND:
                return self._undefined_definition(exc.code)
            log.error(
                f"[{self.agent_name}] agent_definition load FAILED "
                f"({exc.code}, {exc.attempts} attempt(s)) — refusing: no stand-in "
                "definition, no wildcard tools, no empty prompt."
            )
            raise

    def _load_with_retry(self) -> AgentDefinition:
        started = _monotonic()
        deadline = started + LOAD_TOTAL_CAP_S
        attempt = 0
        while True:
            attempt += 1
            try:
                return self._load_once(deadline)
            except AgentDefinitionUnavailable as exc:
                # Raised by validation with a final code; restamp the attempt count.
                raise AgentDefinitionUnavailable(self.agent_name, exc.code, attempt) from None
            except Exception as exc:  # noqa: BLE001 — classified into a fixed code
                code = load_failure_code(exc)
                retries_done = attempt - 1
                if code not in _RETRYABLE_LOAD_CODES or retries_done >= len(LOAD_BACKOFFS_S):
                    raise AgentDefinitionUnavailable(
                        self.agent_name, code, attempt
                    ) from None
                delay = LOAD_BACKOFFS_S[retries_done]
                if _monotonic() + delay + MIN_ATTEMPT_TIMEOUT_S > deadline:
                    raise AgentDefinitionUnavailable(
                        self.agent_name, code, attempt
                    ) from None
                log.warning(
                    f"[{self.agent_name}] agent_definition load attempt {attempt} "
                    f"failed ({code}) — retrying in {delay:.1f}s"
                )
                _sleep(delay)

    def _load_once(self, deadline: float) -> AgentDefinition:
        explicit_id = os.environ.get(f"{self._prefix}_AGENT_DEFINITION_ID", "")
        if explicit_id:
            return self._load_by_id(explicit_id, deadline)
        return self._load_by_name(deadline)

    def _neotoma(
        self, method: str, url: str, body: "dict | None" = None, deadline: "float | None" = None
    ) -> object:
        """Fetch JSON from Neotoma and return the parsed body (shape not yet validated).

        Per-agent AAuth-signed when ``NEOTOMA_AAUTH_VIA_CLI`` is on and this agent
        has a key; otherwise the unsigned/bearer httpx path. If the signing helper
        itself FAILS (raises: no key, helper error, timeout) the bearer path is tried;
        a signed request that RETURNS a result is final either way.

        The transport RESULT is validated before any body is read, identically on both
        paths: it must be a well-formed result whose status is exactly
        ``SUCCESS_STATUS``. Any other status (another 2xx, a redirect, a 4xx, a 5xx),
        a missing or non-integer status, a result of the wrong shape, or a body that
        is not JSON is refused: ``AgentDefinitionUnavailable``, never found, never
        absent. Every request runs on an owned worker with min(its default timeout,
        time left before ``deadline``).
        """
        if deadline is None:
            deadline = _monotonic() + LOAD_TOTAL_CAP_S
        if ns.via_cli_enabled():
            # signed_request owns bounded identity preparation. A synchronous
            # key preflight here would escape the load deadline. Missing keys
            # retain the existing helper-error bearer fallback.
            try:
                result = _on_worker(
                    lambda remaining: ns.signed_request(
                        method, url, body, agent_name=self.agent_name,
                        timeout=min(SIGNED_REQUEST_TIMEOUT_S, remaining),
                        deadline=deadline, clock=_monotonic,
                    ),
                    deadline,
                )
            except _LoadBusy:
                raise
            except Exception as exc:
                if isinstance(exc, ns.SignedRequestError) and exc.response_received:
                    status = exc.http_status
                    if isinstance(status, int) and not isinstance(status, bool) and status != SUCCESS_STATUS:
                        raise _TransportStatus(status) from None
                    raise self._unavailable("bad_response") from None
                log.warning(
                    f"[{self.agent_name}] signed request failed ({type(exc).__name__}); "
                    "falling back to bearer"
                )
            else:
                return self._accept_signed(result)
        return self._accept_bearer(self._bearer_request(method, url, body, deadline))

    def _accept_signed(self, result: object) -> object:
        """Validate a signed request's ``(status, body)`` result, then return the body."""
        if not isinstance(result, (tuple, list)) or len(result) != 2:
            raise self._unavailable("bad_response")
        status, data = result
        if isinstance(status, bool) or not isinstance(status, int):
            raise self._unavailable("bad_response")
        if status != SUCCESS_STATUS:
            raise _TransportStatus(status)
        return data

    def _bearer_request(self, method: str, url: str, body: "dict | None", deadline: float):
        def send(remaining: float):
            timeout = min(BEARER_REQUEST_TIMEOUT_S, remaining)
            budget = httpx.Timeout(timeout)
            if method.upper() == "GET":
                return httpx.get(url, headers=_auth_headers(), timeout=budget)
            return httpx.post(url, json=body, headers=_auth_headers(), timeout=budget)

        return _on_worker(send, deadline)

    def _accept_bearer(self, resp: object) -> object:
        """Validate the bearer response object, then return its parsed body."""
        status = getattr(resp, "status_code", None)
        if isinstance(status, bool) or not isinstance(status, int):
            raise self._unavailable("bad_response")
        if status != SUCCESS_STATUS:
            raise _TransportStatus(status)
        headers = getattr(resp, "headers", None)
        content_type = headers.get("content-type") if hasattr(headers, "get") else None
        if isinstance(content_type, str) and content_type and "json" not in content_type.lower():
            raise self._unavailable("bad_response")
        return resp.json()  # a non-JSON body raises ValueError: bad_response

    def _unavailable(self, code: str) -> AgentDefinitionUnavailable:
        return AgentDefinitionUnavailable(self.agent_name, code, 1)

    def _validated_entities(self, data: object) -> list:
        """The ``entities`` list of a well-formed query response, or raise.

        Well-formed means: a JSON object, no error-shaped keys, an ``entities`` key
        holding a list, and no sign that the page is partial. Anything else is
        ``bad_response``, never absence.
        """
        if not isinstance(data, dict):
            raise self._unavailable("bad_response")
        if any(data.get(k) for k in ("error", "error_code", "errors", "detail")):
            raise self._unavailable("bad_response")
        entities = data.get("entities")
        if not isinstance(entities, list):
            raise self._unavailable("bad_response")
        total = data.get("total")
        if total is not None and (isinstance(total, bool) or not isinstance(total, int) or total < 0):
            raise self._unavailable("bad_response")
        if not entities:
            # Absence is a positive claim, so it must not be a partial page: any
            # sign of more results than were returned is not "none".
            if total:
                raise self._unavailable("bad_response")
            for flag in ("has_more", "truncated", "next_offset", "next_cursor"):
                if data.get(flag):
                    raise self._unavailable("bad_response")
        return entities

    def _validated_snapshot(self, snap: object) -> dict:
        """A definition snapshot with its fields typed, or raise ``bad_response``."""
        if not isinstance(snap, dict):
            raise self._unavailable("bad_response")
        name = snap.get("name")
        if not isinstance(name, str) or name.strip().lower() != self.agent_name:
            raise self._unavailable("bad_response")  # missing, mistyped or a different name
        for key in (
            "description", "tier", "genus", "status", "prompt_markdown", "agent_grant",
            "override_policy", "aauth_sub", "version", "notes",
        ):
            if snap.get(key) is not None and not isinstance(snap[key], str):
                raise self._unavailable("bad_response")
        allow = snap.get("tool_allowlist")
        if allow is not None and not (
            isinstance(allow, str)
            or (isinstance(allow, (list, tuple)) and all(isinstance(t, str) for t in allow))
        ):
            raise self._unavailable("bad_response")
        if not isinstance(snap.get("prompt_markdown"), str) or not snap["prompt_markdown"].strip():
            raise self._unavailable("bad_response")
        if (not isinstance(snap.get("status"), str)
                or snap["status"].strip().lower() not in
                (RUNNING_STATUSES | WARNING_STATUSES | REFUSING_STATUSES)):
            raise self._unavailable("bad_response")
        if not AgentDefinition(tool_allowlist=allow).tools:
            raise self._unavailable("bad_response")
        prov = snap.get("provenance")
        if prov is not None and not isinstance(prov, dict):
            raise self._unavailable("bad_response")
        return snap

    def _load_by_id(self, entity_id: str, deadline: float) -> AgentDefinition:
        url = f"{NEOTOMA_BASE_URL}/entities/{entity_id}"
        data = self._neotoma("GET", url, None, deadline)
        if not isinstance(data, dict):
            raise self._unavailable("bad_response")
        if any(data.get(k) for k in ("error", "error_code", "errors", "detail")):
            raise self._unavailable("bad_response")
        snap = data.get("snapshot")
        if snap is None and isinstance(data.get("entity"), dict):
            snap = data["entity"].get("snapshot")
        return self._parse(entity_id, {"snapshot": self._validated_snapshot(snap)})

    def _load_by_name(self, deadline: float) -> AgentDefinition:
        """Search for agent_definition by name field via POST /entities/query.

        GET /entities does not exist on local Neotoma (404); /entities/query is
        the canonical list route (same fix applied to the Anthus orchestrator in
        PR #58). Its response nests the field dict as entity.snapshot.snapshot.

        Absence is authoritative ONLY for a well-formed 200 whose ``entities`` list
        is empty. A result for a different name, several results, a malformed
        entry, an error-shaped or partial response: all ``AgentDefinitionUnavailable``.
        """
        url = f"{NEOTOMA_BASE_URL}/entities/query"
        body = {
            "entity_type": "agent_definition",
            "search": self.agent_name,
            "limit": 5,
            "include_snapshots": True,
        }
        entities = self._validated_entities(self._neotoma("POST", url, body, deadline))
        if not entities:
            raise self._unavailable(NOT_FOUND)
        # The search is fuzzy, so other agents' definitions may come back beside the
        # one asked for. Every entry must still be well-formed, exactly one must carry
        # THIS name, and that one's snapshot must validate. No exact match, or more
        # than one, is not absence: it is an unreadable answer.
        exact: list[tuple[str, dict]] = []
        for ent in entities:
            if not isinstance(ent, dict):
                raise self._unavailable("bad_response")
            entity_id = ent.get("entity_id")
            outer = ent.get("snapshot")
            if not isinstance(entity_id, str) or not entity_id.strip() or not isinstance(outer, dict):
                raise self._unavailable("bad_response")
            # Unwrap the doubly-nested snapshot to the flat field dict.
            snap = outer.get("snapshot", outer)
            if not isinstance(snap, dict):
                raise self._unavailable("bad_response")
            name = snap.get("name")
            if isinstance(name, str) and name.strip().lower() == self.agent_name:
                exact.append((entity_id, snap))
        if len(exact) != 1:
            raise self._unavailable("bad_response")
        entity_id, snap = exact[0]
        snap = self._validated_snapshot(snap)
        log.info(f"[{self.agent_name}] Loaded agent_definition {entity_id} from Neotoma")
        return self._parse(entity_id, {"snapshot": snap})

    def _parse(self, entity_id: str, data: dict) -> AgentDefinition:
        snap = data.get("snapshot") or data.get("entity", {}).get("snapshot", {})
        # Extract a representative observation_id from the reducer provenance map.
        # We use "name" as the anchor field; fall back to any non-null value.
        provenance = snap.get("provenance") or {}
        last_obs_id = provenance.get("name") or next(
            (v for v in provenance.values() if v), ""
        )

        def text(key: str, default: str) -> str:
            value = snap.get(key)
            return default if value is None else value

        allow = snap.get("tool_allowlist")
        return AgentDefinition(
            entity_id=entity_id,
            name=text("name", self.agent_name),
            description=text("description", ""),
            tier=text("tier", ""),
            genus=text("genus", ""),
            status=text("status", "active"),
            prompt_markdown=text("prompt_markdown", ""),
            tool_allowlist=allow,
            agent_grant=text("agent_grant", "service"),
            override_policy=text("override_policy", ""),
            aauth_sub=text("aauth_sub", f"{self.agent_name}@ateles-swarm"),
            version=text("version", "1.0.0"),
            notes=text("notes", ""),
            raw=data,
            last_observation_id=str(last_obs_id) if last_obs_id else "",
        )

    def _undefined_definition(self, reason: str) -> AgentDefinition:
        """The explicit opt-in result when Neotoma has NO definition for this agent.

        Reached only through ``load(allow_undefined=True)`` and only for a definite
        ``not_found``; a failed load raises instead. It has an EMPTY
        prompt_markdown and NO tools, so it is logged at ERROR
        and marked ``is_stub`` for the caller to branch on.

        Its status is UNDEFINED_STATUS, not "active" (ateles#562): an absence of
        information must not look like a configured agent.
        """
        log.error(
            f"[{self.agent_name}] no agent_definition exists ({reason}) — "
            "returning an inert absence marker with no tools."
        )
        return AgentDefinition(
            name=self.agent_name,
            aauth_sub=f"{self.agent_name}@ateles-swarm",
            agent_grant="service",
            tool_allowlist=[],
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
            ns.via_cli_enabled()
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

        Every row-derived field is sanitized before interpolation. These rows
        are not operator-authored only — `load_active_policies` (above) draws
        on rows `generalizer.py` writes autonomously, with no human review
        gate — the same lower-trust-write-reaching-model-context path
        `policy_skill_renderer.to_skill` already guards for `PolicySkill.body`
        via `_sanitize_body`. This method is a second, independent reader of
        the same raw `agent_policy.rule` text, feeding two live dispatch
        system prompts (`anthus.py`, `execution/mcp/ateles/server.py`), so it
        must run the SAME sanitizer rather than a divergent one (Falco, PR
        #1320 round 3 — CONFIRMED forged-heading/forged-tier-marker injection
        reproduced directly against this function). `rule_kind`/`status` are
        row-derived too (`to_skill`'s docstring already flags every
        row-derived field as untrusted), so they run through the single-line
        sanitizer alongside `rule` through the multi-line one.

        `_sanitize_body(..., list_item_safe=True)` (Falco, PR #1320 round 4):
        this method's own `- ({kind}, {status}) {rule}` template puts every
        row in ONE flat bullet list with no per-row wrapper, unlike
        `to_skill`'s `body` field (each row isolated under its own heading) —
        so an embedded newline in `rule` followed by ANY non-indented
        character puts attacker text at column 0 of that shared list,
        structurally indistinguishable from a genuine sibling policy bullet
        (CONFIRMED with a leading `+` and a Unicode dash `‐`, both outside
        `_LEADING_MARKDOWN`'s ASCII-only stripped class — but the fix is the
        structural indent boundary, not a wider glyph denylist, since a
        denylist is always one lookalike behind). `list_item_safe=True`
        indents every continuation line so it can never open a new list
        item, heading, or comment regardless of its leading character,
        while ordinary multi-paragraph rule text still reads as one
        indented block under its row's bullet rather than being collapsed
        to a single line.

        Imported lazily (not at module scope) because `policy_skill_renderer`
        imports FROM this module at import time (`policy_binds_agent_by_edge`
        et al.) — a module-level import here would be circular.
        """
        policies = self.load_active_policies()
        if not policies:
            return ""
        try:  # package import (normal daemon runtime) with script fallback
            from .policy_skill_renderer import (  # type: ignore
                _BODY_MAX,
                _sanitize_body,
                _sanitize_field,
            )
        except ImportError:  # pragma: no cover
            from policy_skill_renderer import (  # type: ignore
                _BODY_MAX,
                _sanitize_body,
                _sanitize_field,
            )
        lines = [
            "\n\n## Active agent policies (apply these)\n",
            "These standing policies were learned for you. `provisional` ones "
            "are being validated by use — follow them and emit a "
            "`strategy_drift_signal` if one is wrong.\n",
        ]
        for p in policies:
            kind = _sanitize_field(str(p.get("rule_kind", "prefer")), max_len=40) or "prefer"
            status = _sanitize_field(str(p.get("status", "active")), max_len=40) or "active"
            raw_rule = p.get("rule") or p.get("description", "")
            rule = _sanitize_body(str(raw_rule), max_len=_BODY_MAX, list_item_safe=True)
            if not rule:
                continue
            lines.append(f"- ({kind}, {status}) {rule}")
        return "\n".join(lines)
