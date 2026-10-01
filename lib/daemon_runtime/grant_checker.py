"""
lib/daemon_runtime/grant_checker.py — Check agent_grant status before dispatch.

Loads agent_grant entities from Neotoma by aauth_sub and exposes:
  - is_active / is_suspended / is_revoked properties
  - check_capability(cap) — True if the named capability is active
  - suspend / restore / revoke — write state changes back to Neotoma

Fail posture (ateles#560). A check that cannot determine an answer must report
UNKNOWN, not PASS. Three verdicts, not two:

  ALLOW    — an active grant covers the request.
  DENY     — the store answered and the answer is "no": no grants exist for this
             sub, or every grant is suspended/revoked, or none covers the
             capability. A determinate negative; ALWAYS enforced.
  UNKNOWN  — the store could not be consulted (unreachable, timeout, no bearer
             token). This is NOT a pass. It resolves by boundary posture:
             privileged operations fail CLOSED, read-shaped operations degrade
             OPEN within a bounded staleness window.

The two failures have opposite risk profiles, which is why #560 asks for them to
be separated rather than flipped together. Absent-grant is a security fact — an
agent that was never granted anything, or whose grant was deleted (see #533,
which wiped capabilities on 37 agents), must not be indistinguishable from one
holding every capability. Store-unreachable is an availability event: hard-
failing every agent on a Neotoma outage would take the swarm down, and Neotoma
has run at 8-80s response times (#577).

See ``GrantVerdict`` and ``resolve_unknown`` below for how UNKNOWN is settled.
The posture vocabulary mirrors ``gating.CheckpointPosture`` from ateles#350.

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
from enum import Enum
from typing import Optional

import httpx

log = logging.getLogger(__name__)

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

# How long a successfully-loaded grant snapshot may be reused to answer an
# UNKNOWN (store-unreachable) check before the cache is considered too stale to
# vouch for anything. Beyond this, an unreachable store denies even reads.
GRANT_CACHE_MAX_STALENESS_SECONDS = int(
    os.environ.get("ATELES_GRANT_CACHE_MAX_STALENESS_SECONDS", "900")
)


class GrantVerdict(str, Enum):
    """Outcome of a grant check. Three states, not two (ateles#560).

    UNKNOWN is deliberately not a synonym for either ALLOW or DENY: it records
    that the store could not answer, so the caller resolves it against the
    boundary's posture instead of silently inheriting a default.
    """

    ALLOW = "allow"
    DENY = "deny"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class GrantDecision:
    """A verdict plus the reason that produced it.

    ``reason`` is a stable snake_case token intended for logs, audit rows, and
    call-site branching — never a free-text message. Callers that need to tell
    absent-grant from store-unreachable read this, not the boolean.
    """

    verdict: GrantVerdict
    reason: str
    detail: str = ""
    constraints: Optional[dict] = None

    @property
    def allowed(self) -> bool:
        """True only for a determinate ALLOW. UNKNOWN is never allowed here.

        Call sites that may degrade open must say so explicitly by calling
        ``resolve_unknown``; they cannot get an open degrade by accident.
        """
        return self.verdict is GrantVerdict.ALLOW

    @property
    def is_unknown(self) -> bool:
        return self.verdict is GrantVerdict.UNKNOWN


# Operations that must fail CLOSED when the grant store cannot be consulted.
# These either move money, reach outside the swarm, or mutate shared state that
# is expensive to unwind — the cases where "we could not check" must not become
# "we allowed it". Everything else (read-shaped work) may degrade OPEN within
# the staleness bound so a Neotoma outage does not halt the swarm.
PRIVILEGED_OPS = frozenset(
    {
        "store",
        "store_structured",
        "correct",
        "create_relationship",
        "delete_entity",
        "delete_relationship",
        "merge_entities",
        "split_entity",
        "github_harness:write",
        "a2a:task:create",
        "payment:send",
        "email:send",
        "publish",
    }
)

# Servers whose tools are privileged regardless of the specific tool named:
# anything that can move funds or send outbound comms.
PRIVILEGED_TOOL_SERVERS = frozenset({"btc-wallet", "gmail", "typefully"})


def is_privileged_op(op: str) -> bool:
    """True if ``op`` must fail closed on an UNKNOWN grant check."""
    if not op:
        return True  # unnamed operation: treat as privileged, not as free
    o = op.strip().lower()
    if o in PRIVILEGED_OPS:
        return True
    # Namespaced write ops, e.g. "github_harness:write", "neotoma:write".
    if o.endswith(":write") or o.endswith(":send") or o.endswith(":delete"):
        return True
    if o.startswith("tool:"):
        rest = o[len("tool:"):]
        server = rest.split(":", 1)[0]
        return server in PRIVILEGED_TOOL_SERVERS
    return False


def resolve_unknown(
    decision: GrantDecision,
    *,
    op: str,
    privileged: Optional[bool] = None,
) -> tuple[bool, str]:
    """Settle a possibly-UNKNOWN decision into (allowed, reason).

    ALLOW and DENY pass through unchanged — a determinate answer is never
    overridden by posture. Only UNKNOWN consults the posture:

      privileged op  → CLOSED (deny; we could not verify authority to act)
      read-shaped op → OPEN   (allow, loudly logged, availability degrade)

    The open degrade is bounded: ``GrantChecker`` only reports UNKNOWN with a
    usable cached snapshot inside ``GRANT_CACHE_MAX_STALENESS_SECONDS``. Past
    that it reports UNKNOWN with reason ``grant_cache_stale``, which this
    function denies for every op — a cache old enough to have missed a
    revocation is not evidence of anything.
    """
    if decision.verdict is GrantVerdict.ALLOW:
        return True, decision.reason
    if decision.verdict is GrantVerdict.DENY:
        return False, decision.reason

    if decision.reason == "grant_cache_stale":
        log.error(
            "[grant_checker] DENY %s — grant store unreachable and cached "
            "grants exceed the %ss staleness bound (%s)",
            op,
            GRANT_CACHE_MAX_STALENESS_SECONDS,
            decision.detail,
        )
        return False, "grant_cache_stale"

    priv = is_privileged_op(op) if privileged is None else privileged
    if priv:
        log.error(
            "[grant_checker] DENY privileged op %r — grant store unavailable "
            "(%s); privileged boundary fails CLOSED",
            op,
            decision.detail or decision.reason,
        )
        return False, "grant_store_unavailable_privileged_denied"

    log.warning(
        "[grant_checker] ALLOW read-shaped op %r despite unavailable grant "
        "store (%s) — availability degrade, NOT an authorization",
        op,
        decision.detail or decision.reason,
    )
    return True, "grant_store_unavailable_read_degraded"


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

    Returns tri-state ``GrantDecision``s (ateles#560). A store that answers
    "this agent has no grants" produces DENY; a store that cannot be reached
    produces UNKNOWN, which the caller settles via ``resolve_unknown``.

    The legacy boolean methods (``is_active``, ``check_capability``,
    ``check_tool``) are kept for call-site compatibility and now enforce
    absent-grant denial. They resolve UNKNOWN through the posture rules, so an
    unreachable store still degrades open for reads and closed for writes.
    """

    def __init__(self, aauth_sub: str) -> None:
        self.aauth_sub = aauth_sub
        self._grants: list[AgentGrant] = []
        self._loaded = False
        self._load_error: Optional[str] = None
        # Wall-clock time of the last SUCCESSFUL load. Bounds how long a cached
        # snapshot may answer an UNKNOWN check (see GRANT_CACHE_MAX_STALENESS).
        self._loaded_at: Optional[float] = None

    def load(self) -> GrantChecker:
        """Fetch all agent_grant entities for this sub from Neotoma."""
        if not NEOTOMA_BEARER_TOKEN:
            self._load_error = "NEOTOMA_BEARER_TOKEN not set"
            log.error(
                f"[grant_checker:{self.aauth_sub}] {self._load_error} — grant "
                "state UNKNOWN; privileged operations will be denied"
            )
            self._loaded = True
            return self

        # POST /entities/query — the GET /entities list endpoint does not exist
        # on the Neotoma server (returns 404).
        url = f"{NEOTOMA_BASE_URL}/entities/query"
        body = {
            "entity_type": "agent_grant",
            "search": self.aauth_sub,
            "include_snapshots": True,
            "limit": 50,
        }
        try:
            resp = httpx.post(
                url,
                json=body,
                headers={
                    "Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            resp.raise_for_status()
            entities = resp.json().get("entities", [])
            self._grants = [
                self._parse(e)
                for e in entities
                if self._snapshot_matches_sub(e.get("snapshot") or {})
            ]
            # Determinate answer from the store — clear any prior load error so
            # a recovered Neotoma stops producing UNKNOWN.
            self._load_error = None
            self._loaded_at = time.time()
            if not self._grants:
                log.error(
                    f"[grant_checker:{self.aauth_sub}] Store returned ZERO "
                    "grants — this agent is NOT authorised. Grant one via: "
                    "python execution/scripts/manage_grants.py list"
                )
            else:
                log.info(
                    f"[grant_checker:{self.aauth_sub}] Loaded {len(self._grants)} grant(s)"
                )
        except Exception as exc:
            self._load_error = str(exc)
            log.error(
                f"[grant_checker:{self.aauth_sub}] Could not load grants: {exc} — "
                "grant state UNKNOWN; privileged operations will be denied"
            )
        self._loaded = True
        return self

    # ── Tri-state core ──────────────────────────────────────────────────────

    def _unavailable(self) -> Optional[GrantDecision]:
        """Return an UNKNOWN decision if the store could not be consulted.

        Returns None when the store answered, in which case the caller may
        reason from ``self._grants`` — including the empty case, which is a
        determinate "not authorised" rather than an absence of information.
        """
        if not self._loaded:
            return GrantDecision(
                GrantVerdict.UNKNOWN,
                "grants_not_loaded",
                "load() was never called",
            )
        if self._load_error:
            age = None if self._loaded_at is None else time.time() - self._loaded_at
            if self._loaded_at is None or age > GRANT_CACHE_MAX_STALENESS_SECONDS:
                return GrantDecision(
                    GrantVerdict.UNKNOWN,
                    "grant_cache_stale",
                    f"{self._load_error}; no usable cached snapshot"
                    + ("" if age is None else f" (age {age:.0f}s)"),
                )
            return GrantDecision(
                GrantVerdict.UNKNOWN,
                "grant_store_unreachable",
                f"{self._load_error} (cached snapshot {age:.0f}s old)",
            )
        return None

    def decide_active(self) -> GrantDecision:
        """Tri-state form of ``is_active``."""
        unavailable = self._unavailable()
        if unavailable is not None:
            return unavailable
        if not self._grants:
            return GrantDecision(
                GrantVerdict.DENY,
                "no_grant",
                f"no agent_grant exists for {self.aauth_sub}",
            )
        if any(g.is_active for g in self._grants):
            return GrantDecision(GrantVerdict.ALLOW, "active_grant")
        return GrantDecision(
            GrantVerdict.DENY,
            "no_active_grant",
            "every grant is suspended or revoked",
        )

    def decide_capability(self, capability: str) -> GrantDecision:
        """Tri-state form of ``check_capability``."""
        unavailable = self._unavailable()
        if unavailable is not None:
            return unavailable
        if not self._grants:
            return GrantDecision(
                GrantVerdict.DENY,
                "no_grant",
                f"no agent_grant exists for {self.aauth_sub}",
            )
        if any(g.is_active and g.has_capability(capability) for g in self._grants):
            return GrantDecision(GrantVerdict.ALLOW, "capability_granted")
        return GrantDecision(
            GrantVerdict.DENY,
            "capability_not_granted",
            f"no active grant covers {capability!r}",
        )

    def decide_tool(self, server: str, tool: str) -> GrantDecision:
        """Tri-state form of ``check_tool``.

        The un-migrated-agent fallback (a grant exists but declares no
        ``tool:``-prefixed capabilities) is preserved deliberately and kept
        DISTINCT from absent-grant: #560 scopes that migration separately. It
        reports its own reason so it is visible in audit rows rather than
        blending into a generic allow.
        """
        unavailable = self._unavailable()
        if unavailable is not None:
            return unavailable
        if not self._grants:
            return GrantDecision(
                GrantVerdict.DENY,
                "no_grant",
                f"no agent_grant exists for {self.aauth_sub}",
            )
        if not any(g.tool_grants for g in self._grants):
            return GrantDecision(
                GrantVerdict.ALLOW,
                "tool_grants_not_declared_unmigrated",
                "grant exists but declares no tool capabilities",
            )
        for g in self._grants:
            if not g.is_active:
                continue
            constraints = g.tool_constraints(server, tool)
            if constraints is not None:
                return GrantDecision(
                    GrantVerdict.ALLOW, "tool_granted", constraints=constraints
                )
        return GrantDecision(
            GrantVerdict.DENY,
            "tool_not_granted",
            f"no active grant covers {server}:{tool}",
        )

    # ── Boolean compatibility layer ─────────────────────────────────────────
    #
    # These settle UNKNOWN through resolve_unknown() so existing call sites get
    # the posture behaviour without change. An absent grant now DENIES (#560).

    def is_active(self, *, op: str = "") -> bool:
        """True if an active grant exists. Absent grant is False, not True.

        ``op`` names the operation being gated so an UNKNOWN verdict can be
        settled by privilege. Callers that omit it get the conservative
        reading — an unnamed operation is treated as privileged.
        """
        allowed, _ = resolve_unknown(self.decide_active(), op=op)
        return allowed

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

    def has_no_grant(self) -> bool:
        """True when the store answered and holds ZERO grants for this sub.

        Distinct from ``is_revoked``: a revoked grant is a decision someone
        made, an absent grant is an agent that was never authorised (or whose
        grant was wiped — see #533). Startup paths report these differently.
        """
        return self._loaded and not self._load_error and not self._grants

    def check_capability(self, capability: str) -> bool:
        """True if an active grant covers ``capability``.

        Absent grant → False. Unreachable store → posture-resolved on whether
        ``capability`` is privileged.
        """
        allowed, _ = resolve_unknown(
            self.decide_capability(capability), op=capability
        )
        return allowed

    def check_tool(self, server: str, tool: str) -> tuple[bool, Optional[dict]]:
        """
        Check whether an MCP tool call is authorized (issue #26).

        Returns (allowed, constraints):
          - allowed: True if an active grant covers "<server>:<tool>".
          - constraints: the param-constraint dict to enforce (may be empty),
            or None when denied.

        Absent grant → (False, None). An unreachable store resolves by posture:
        privileged servers (funds, outbound comms) deny; reads degrade open
        within the staleness bound.
        """
        decision = self.decide_tool(server, tool)
        allowed, _ = resolve_unknown(decision, op=f"tool:{server}:{tool}")
        if not allowed:
            return False, None
        return True, decision.constraints

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
