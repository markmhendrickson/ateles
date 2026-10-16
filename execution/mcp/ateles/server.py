#!/usr/bin/env python3
"""
ateles — MCP server for Ateles swarm routing and checkpoint management.

Provides nine tools that wrap multi-step Neotoma/GitHub query patterns into
single calls, so any connected agent gets reliable swarm interaction without
re-deriving the roster/policy/checkpoint dance — or the entity-read plus
log-grep dance — each session.

Tools:
  get_swarm_roster    — full roster (roles → agent names)
  route_task          — resolve owning agent + definition + execution policy
  list_checkpoints    — pending checkpoint_briefs awaiting operator
  resolve_checkpoint  — approve/reject a checkpoint with validation
                        (the ONLY mutating tool)
  get_gate_status     — an issue's gate_status, owner, blocking gates, history,
                        and pipeline state                        [read-only]
  list_pipeline_queue — who holds the issue-pipeline slot, who is queued, and
                        how long each has waited                  [read-only]
  get_dispatch_health — dispatcher liveness, recent activity, failures
                                                                  [read-only]
  get_task_timeline   — one task's history, merged and time-ordered from the
                        records the swarm writes, with sources and gaps
                                                                  [read-only]
  watch_swarm         — bounded long-poll: what changed since a cursor for the
                        given tasks and checkpoints               [read-only]

The observability tools never write gate state — see the SELF-CERTIFICATION
BOUNDARY note above their implementations. Their reads fail CLOSED: a failed
read reports "unknown" with a reason, never an empty all-clear.

Environment (see README.md for the full operator-provisioning table):
  NEOTOMA_BASE_URL          (default: https://neotoma.markmhendrickson.com)
  NEOTOMA_BEARER_TOKEN      (required)
  NEOTOMA_BEARER_TOKEN_PROD (promoted over the local token for a remote URL)
  GITHUB_TOKEN              (required for queue visibility; also accepts
                             APIS_GITHUB_TOKEN / GH_TOKEN)
  SWARM_ROSTER_KEY          (default: default)
  APIS_CHECKPOINT_REQUIRED_APPROVER_SUB
                            (default: ateles@ateles-swarm)
  APIS_CHECKPOINT_REQUIRED_APPROVER_JKT
                            (required RFC 7638 key thumbprint; no default)
  APIS_CHECKPOINT_PRODUCER_JKT
                            (required RFC 7638 Apis key thumbprint; no default)

Transport: stdio (launched by Claude Code as an MCP server subprocess).
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import hmac
import inspect
import json
import logging
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    TextContent,
    Tool,
)

# This server is intentionally launched as a file (``python server.py``) by
# both the production wrapper and its blocking CI lane.  In that call shape
# Python places only this directory on ``sys.path``; repository packages such
# as ``lib.daemon_runtime`` are otherwise unavailable.  Approval signing now
# reuses the shared AAuth implementation, so make the repository root explicit
# before any checkpoint action can import it.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

log = logging.getLogger("ateles")

# Keep release-acceptance work alive if the MCP caller times out or cancels.
# Once a checkpoint is approved, cancelling the transport must not cancel the
# consumer that owns its durable dispatch claim.
_release_acceptance_tasks: set[asyncio.Task[bool]] = set()


def _release_acceptance_done(task: asyncio.Task[bool]) -> None:
    _release_acceptance_tasks.discard(task)
    if task.cancelled():
        return
    try:
        task.result()
    except Exception:
        log.exception("checkpoint release acceptance failed after caller detached")

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
ROSTER_KEY = os.environ.get("SWARM_ROSTER_KEY", "default")

DEFAULT_POLICY_ID = os.environ.get(
    "EXECUTION_POLICY_DEFAULT_ID", "ent_dfce6edecefe3eb7fc9e0337"
)

AGENT_POLICY_OVERRIDES: dict[str, str] = {
    "monedula": os.environ.get(
        "MONEDULA_POLICY_ID", "ent_c7f81385afbd993db3dd11ff"
    ),
}

CHECKPOINT_RESOLVER_ISSUER = os.environ.get(
    "APIS_CHECKPOINT_REQUIRED_APPROVER_ISS", "https://markmhendrickson.com"
).strip()
_CHECKPOINT_RESOLUTION_MAX_TTL_SECONDS = 300
_RESOLVER_SIGNATURE_HEADERS = frozenset(
    {
        "signature-key",
        "signature-input",
        "signature",
        "content-digest",
        "content-type",
    }
)

SERVER_INSTRUCTIONS = """\
You are connected to Ateles. Follow these operating rules:

1. **Dispatch, don't do inline.** When route_task identifies an owning agent, \
delegate to that agent rather than doing the work yourself.
2. **Monitor dispatched work.** After dispatching a task via route_task, track \
its status in Neotoma (retrieve the task entity periodically). Report completion, \
failure, or checkpoint escalation back to the operator — do not fire-and-forget.
3. **Consent gate.** Never send anything public, email anyone, or take an \
irreversible external action without operator approval.
4. **Checkpoint protocol.** Pending checkpoints (list_checkpoints) are the \
operator's decision queue. Present each with its blast radius, confidence vs \
threshold, and reason. Act on the operator's decision via resolve_checkpoint — \
do NOT execute the held task yourself.
5. **Neotoma first.** Durable memory lives in Neotoma. Store, don't leave in \
conversation.
"""


# ── Rule delivery into the instructions block (foundation phase E2, task 3) ──
#
# The five rules above are STATIC — they ship with the code and say how to use
# this server. They are not the swarm's governing rules, which live on the
# record as `agent_policy` rows and change without a deploy.
#
# CORRECTED 2026-09-25 (ateles#1243, ateles#1254): the design below shipped
# under PR #1184 on the belief that MCP `instructions` was the channel with
# the most room. Measured evidence says the opposite:
#
#   * MCP `instructions` is capped at roughly 2,048 characters ACROSS ALL
#     CONNECTED SERVERS COMBINED, and the client truncates it SILENTLY. A
#     local Claude Code log reads `Server instructions truncated from 4989 to
#     2048 chars`; see also anthropics/claude-code#43474. This is real but
#     undocumented — it was not "no cap," it was an unmeasured one.
#   * The rendered rule corpus on `main` measured 17,996 characters — about
#     9x the shared budget — so nearly all of it was silently dropped before
#     ever reaching a session. What arrived was whatever survived truncation,
#     not the rules.
#   * "A SessionStart hook is SKIPPED at launch (including --continue/
#     --resume)" was FALSE: a resumed session received both
#     `SessionStart:resume` and `SessionStart:compact` hook output. Hooks do
#     cap below their full size — a 16.3 KB hook output was replaced with a
#     file pointer plus a 2 KB preview (ateles#1254) — but the exact
#     threshold is unmeasured, and it is a per-hook budget, not "skipped."
#
# So this field is no longer where the rule corpus is delivered. It carries
# only the static operating rules above plus a short pointer: standing rules
# live in Neotoma as `agent_policy` entities (each with an `applies_when`
# trigger) and should be retrieved from Neotoma before acting in a situation
# they cover. Real delivery of the corpus is moving to a SessionStart-hook-
# injected `applies_when` index — separate work, tracked under ateles#1254 —
# sized to that channel's own (unmeasured but real) budget instead of this
# one's ~2,048-character shared ceiling.
#
# The live resolve and edge-based scoping predicate (`policy_binds_agent_by_edge`,
# decision 114, 2026-09-25) stay in place below: `AgentLoader.render_policy_prompt`
# is still the dispatch-side renderer (ateles#1118) and other callers still use
# it. This function now only measures its output against a small fixed budget
# rather than forwarding it verbatim.
SESSION_PRINCIPAL = os.environ.get("ATELES_SESSION_PRINCIPAL", "ateles@ateles-swarm")

# Pointer sentence replacing the rendered rule corpus. Standing rules are
# retrieved from Neotoma on demand rather than pushed through this channel.
RULE_INDEX_POINTER = (
    "\n\nStanding rules live in Neotoma as `agent_policy` entities, each with "
    "an `applies_when` trigger condition. Retrieve them from Neotoma before "
    "acting in a situation one covers."
)

# Total budget for the rendered `instructions` field, measured in characters.
# Target ~1,200 to leave headroom under the ~2,048-character cap MCP clients
# apply across ALL connected servers combined (ateles#1243) — this server is
# never the only one connected.
INSTRUCTIONS_BUDGET_CHARS = 1200


def render_server_instructions(principal: str = "") -> str:
    """Static operating rules, plus a pointer to the live rule record.

    Fail-SOFT by design: an unreachable `agent_policy` resolve must not strip
    the static rules a session needs to use this server at all. It must not
    pass SILENTLY either — a resolve that raises is logged here, and the
    pointer sentence itself is what tells the session where the real rules
    live, since the corpus is no longer rendered inline (ateles#1243).

    The resolve below is a reachability probe ONLY, kept as a monitoring
    signal: its result — success, no rows, or exception — never affects the
    string this function returns. Both the success path and the `except`
    path fall through to the same `SERVER_INSTRUCTIONS + RULE_INDEX_POINTER`
    (or the budget fallback). Nothing here checks that the resolved record is
    bound to this principal, or even that it returned any rows — only that
    the call did not raise, which is logged on failure and otherwise
    discarded. That is deliberate: the whole point of this fix is that the
    corpus does not fit the channel, so nothing here should re-introduce a
    path where its TEXT could reach the instructions field and grow past the
    budget again.
    """
    sub = principal or SESSION_PRINCIPAL
    try:
        if str(_REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(_REPO_ROOT))
        from lib.daemon_runtime.agent_loader import AgentLoader

        # Reachability probe only — kept as a monitoring signal (logged on
        # failure below). Its return value is intentionally discarded and
        # never gates or feeds the served string; do not delete this as
        # "dead code" without also removing the log line it feeds.
        AgentLoader(sub.split("@")[0]).render_policy_prompt()
    except Exception as exc:  # noqa: BLE001 — never block the server on this
        log.error(
            "[ateles-mcp] could not resolve agent_policy for %r: %s — serving "
            "the static operating rules and pointer without confirming the "
            "live record is reachable.",
            sub,
            exc,
        )

    rendered = SERVER_INSTRUCTIONS + RULE_INDEX_POINTER
    if len(rendered) > INSTRUCTIONS_BUDGET_CHARS:
        # Must never happen with the fixed static text above, but a future
        # edit to SERVER_INSTRUCTIONS or the pointer could push this over —
        # fail loudly rather than let the MCP client silently truncate
        # mid-rule the way ateles#1243 documented.
        log.warning(
            "[ateles-mcp] rendered instructions (%d chars) exceed the "
            "%d-char budget — serving the static rules alone rather than "
            "risk client-side truncation.",
            len(rendered),
            INSTRUCTIONS_BUDGET_CHARS,
        )
        return SERVER_INSTRUCTIONS
    return rendered


# ── Neotoma HTTP helpers ─────────────────────────────────────────────────────

def _headers() -> dict[str, str]:
    # An explicit User-Agent: the hosted instance sits behind Cloudflare, which
    # refuses some library-default agents (error 1010).
    return {
        "Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}",
        "User-Agent": "ateles-mcp/1.0",
    }


# Last transport failure, so callers can tell "Neotoma said no rows" apart from
# "the request never succeeded". Both still surface as None/[] from the helpers
# — this records WHY, and tools echo it back to the agent.
#
# Motivating bug: _retrieve_entities posted to a 404 path, got None, returned
# [], and get_swarm_roster reported "swarm_roster not found" — a data-absence
# message for a transport failure. The URL fix alone would leave the next wrong
# endpoint, expired token, or outage just as silent.
#
# Held in a ContextVar, not a module global. get_task_timeline and watch_swarm
# run in worker threads (asyncio.to_thread copies the caller's context), so
# their requests set and clear this concurrently with every other tool. With a
# shared global, a watch poll's successful request could clear the error a
# failed get_swarm_roster read had just recorded, and that tool would report
# "not found" for what was an unreachable Neotoma — the failed-read-as-absence
# case this exists to prevent. A ContextVar gives each asyncio task and each
# worker thread its own value, so one call's success cannot erase another's
# failure, and within one call it behaves exactly as the global did.
_last_transport_error: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "ateles_last_transport_error", default=None
)


def _clear_transport_error() -> None:
    _last_transport_error.set(None)


def _record_transport_error(kind: str, method: str, path: str, detail: str) -> None:
    """kind is the agent-actionable class: no_token | not_found | request_failed."""
    _last_transport_error.set(f"{kind}: {method} {path} — {detail}")
    log.warning("neotoma %s %s failed (%s): %s", method, path, kind, detail)


def _describe_transport_error() -> str | None:
    return _last_transport_error.get()


def _request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    encoded_body: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
) -> dict | None:
    if not NEOTOMA_BEARER_TOKEN:
        _record_transport_error(
            "no_token", method, path,
            "NEOTOMA_BEARER_TOKEN is unset — escalate to the operator, retrying will not help",
        )
        return None
    try:
        headers = _headers()
        if extra_headers:
            headers.update(extra_headers)
        request_kwargs: dict[str, Any] = {
            "headers": headers,
            "params": params,
            "timeout": 15,
        }
        if encoded_body is not None:
            request_kwargs["content"] = encoded_body
        else:
            request_kwargs["json"] = body
        resp = httpx.request(
            method,
            f"{NEOTOMA_BASE_URL}{path}",
            **request_kwargs,
        )
        resp.raise_for_status()
        _clear_transport_error()
        return resp.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        kind = "not_found" if status == 404 else "request_failed"
        hint = (
            " — endpoint does not exist on this Neotoma instance (entity lists are POST /entities/query)"
            if status == 404
            else ""
        )
        _record_transport_error(kind, method, path, f"HTTP {status}{hint}")
        return None
    except Exception as exc:
        _record_transport_error("request_failed", method, path, f"{type(exc).__name__}: {exc}")
        return None


def _get(path: str, params: dict | None = None) -> dict | None:
    return _request("GET", path, params=params)


def _post(
    path: str,
    body: dict,
    *,
    extra_headers: dict[str, str] | None = None,
    encoded_body: bytes | None = None,
) -> dict | None:
    return _request(
        "POST",
        path,
        body=body,
        encoded_body=encoded_body,
        extra_headers=extra_headers,
    )


def _retrieve_page(
    entity_type: str,
    search: str | None = None,
    snapshot_filters: dict | None = None,
    limit: int = 100,
    include_snapshots: bool = True,
    cursor: str | None = None,
) -> dict | None:
    """One page, WITH the response's own `total` and `next_cursor` preserved.

    `_retrieve_entities` throws both away and hands back a bare list, which is
    how `list_checkpoints` came to report a 50-item page as a 372-deep queue
    (ateles#1037). A caller that needs to know how much it did NOT receive must
    use this; a caller that genuinely wants "up to N" can keep using the list
    form.

    Returns None — distinct from an empty page — when the read FAILED. Those
    must not be spelled the same way: a failed read reported as an empty queue
    tells the operator they have nothing to decide.
    """
    body: dict[str, Any] = {
        "entity_type": entity_type,
        "limit": limit,
        "include_snapshots": include_snapshots,
    }
    if search:
        body["search"] = search
    if snapshot_filters:
        body["snapshot_filters"] = snapshot_filters
    if cursor:
        body["cursor"] = cursor
    # POST /entities/query — NOT /retrieve, which 404s. The GET /entities list
    # endpoint does not exist either; see lib/daemon_runtime/agent_loader.py.
    return _post("/entities/query", body)


def _retrieve_entities(
    entity_type: str,
    search: str | None = None,
    snapshot_filters: dict | None = None,
    limit: int = 100,
    include_snapshots: bool = True,
) -> list[dict]:
    data = _retrieve_page(
        entity_type,
        search=search,
        snapshot_filters=snapshot_filters,
        limit=limit,
        include_snapshots=include_snapshots,
    )
    if data is None:
        return []
    return data.get("entities", [])


def _snapshot_of(entity: dict) -> dict:
    snap = (entity.get("snapshot") or {}).get("snapshot")
    if isinstance(snap, dict):
        snapshot = dict(snap)
    elif isinstance(entity.get("snapshot"), dict):
        snapshot = dict(entity["snapshot"])
    else:
        snapshot = dict(entity)

    # Neotoma may return tenant provenance on the entity envelope while the
    # checkpoint consumer works from the inner snapshot. Preserve it, but make
    # conflicting sources fail closed instead of choosing one.
    envelope_user_id = entity.get("user_id")
    snapshot_user_id = snapshot.get("user_id")
    if envelope_user_id and snapshot_user_id and envelope_user_id != snapshot_user_id:
        snapshot["user_id"] = None
    elif envelope_user_id and not snapshot_user_id:
        snapshot["user_id"] = envelope_user_id
    return snapshot


def _correction_body(
    entity_id: str,
    entity_type: str,
    field: str,
    value: Any,
    idem_key: str,
) -> dict[str, Any]:
    return {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "field": field,
        "value": value,
        "idempotency_key": idem_key,
    }


def _canonical_body_bytes(body: dict[str, Any]) -> bytes:
    from lib.daemon_runtime.checkpoint_protocol import canonical_json_bytes

    return canonical_json_bytes(body)


def _resolver_headers(value: object) -> dict[str, str] | None:
    if not isinstance(value, dict):
        return None
    headers = {
        str(key).strip().lower(): str(header_value).strip()
        for key, header_value in value.items()
        if isinstance(key, str) and isinstance(header_value, str)
    }
    if set(headers) != _RESOLVER_SIGNATURE_HEADERS or any(
        not headers[name] for name in _RESOLVER_SIGNATURE_HEADERS
    ):
        return None
    return headers


def _correct(
    entity_id: str,
    entity_type: str,
    field: str,
    value: Any,
    idem_key: str,
    *,
    resolver_aauth_headers: dict[str, str] | None = None,
) -> bool:
    body = _correction_body(entity_id, entity_type, field, value, idem_key)
    encoded_body: bytes | None = None
    signed_headers: dict[str, str] | None = None
    if resolver_aauth_headers is not None:
        signed_headers = _resolver_headers(resolver_aauth_headers)
        if signed_headers is None:
            return False
        # Preserve the exact canonical bytes the caller signed. Neotoma's
        # RFC 9421 verifier authenticates the body again before /correct runs.
        encoded_body = _canonical_body_bytes(body)
    result = _post(
        "/correct",
        body,
        extra_headers=signed_headers,
        encoded_body=encoded_body,
    )
    return result is not None


def _checkpoint_resolver_authority(
    checkpoint_id: str, checkpoint_record: dict
) -> tuple[str, str, str] | None:
    """Return the authenticated resolver subject, key pin, and tenant."""
    from lib.daemon_runtime.gating import read_authenticated_checkpoint_authorization

    authority = read_authenticated_checkpoint_authorization(
        checkpoint_id, checkpoint_record
    )
    if not isinstance(authority, dict):
        return None
    required_sub = str(authority.get("required_approver_sub") or "").strip()
    required_jkt = str(authority.get("required_approver_jkt") or "").strip()
    tenant_id = str(authority.get("user_id") or "").strip()
    snapshot_tenant = str(_snapshot_of(checkpoint_record).get("user_id") or "").strip()
    if (
        not required_sub
        or "@" not in required_sub
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", required_jkt)
        or not tenant_id
        or snapshot_tenant != tenant_id
    ):
        return None
    return required_sub, required_jkt, tenant_id


def _authenticate_checkpoint_resolver(
    resolver_aauth_headers: object,
    *,
    body: bytes,
    required_principal_sub: str,
    required_principal_jkt: str,
) -> dict | None:
    """Verify the caller's body-bound RFC 9421 request before forwarding it.

    The authority pins both subject and RFC 7638 thumbprint. A self-generated
    key that merely claims the required subject is therefore rejected. The
    same headers are forwarded to Neotoma, which independently verifies them
    and persists the authenticated identity on the immutable observation.
    """
    headers = _resolver_headers(resolver_aauth_headers)
    required_sub = str(required_principal_sub or "").strip()
    required_jkt = str(required_principal_jkt or "").strip()
    if (
        headers is None
        or not body
        or not required_sub
        or not re.fullmatch(r"[A-Za-z0-9_-]{43}", required_jkt)
        or not CHECKPOINT_RESOLVER_ISSUER
    ):
        return None
    try:
        import jwt

        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
        from lib.daemon_runtime.aauth_httpsig import content_digest, jwk_thumbprint

        key_match = re.fullmatch(r'aasig=jwt;jwt="([^"\s]+)"', headers["signature-key"])
        input_match = re.fullmatch(
            r'aasig=\("@method" "@authority" "@path" "content-type" '
            r'"content-digest" "signature-key"\);created=(\d+)',
            headers["signature-input"],
        )
        signature_match = re.fullmatch(
            r"aasig=:([A-Za-z0-9+/]+={0,2}):", headers["signature"]
        )
        if not key_match or not input_match or not signature_match:
            return None
        token = key_match.group(1)
        created = int(input_match.group(1))
        now = int(time.time())
        if created > now + 30 or now - created > _CHECKPOINT_RESOLUTION_MAX_TTL_SECONDS:
            return None
        expected_digest = content_digest(body)
        if headers["content-type"] != "application/json" or not hmac.compare_digest(
            headers["content-digest"], expected_digest
        ):
            return None

        header = jwt.get_unverified_header(token)
        unverified = jwt.decode(token, options={"verify_signature": False})
        cnf = unverified.get("cnf")
        public_jwk = cnf.get("jwk") if isinstance(cnf, dict) else None
        if header.get("typ") != "aa-agent+jwt" or not isinstance(public_jwk, dict):
            return None
        key = jwt.PyJWK.from_dict(public_jwk).key
        claims = jwt.decode(
            token,
            key,
            algorithms=["ES256"],
            issuer=CHECKPOINT_RESOLVER_ISSUER,
            options={
                "require": [
                    "sub",
                    "iss",
                    "iat",
                    "exp",
                    "jkt",
                    "cnf",
                ]
            },
        )
        issued_at = int(claims["iat"])
        expires_at = int(claims["exp"])
        if (
            issued_at > now + 30
            or expires_at - issued_at > _CHECKPOINT_RESOLUTION_MAX_TTL_SECONDS
            or issued_at != created
        ):
            return None
        actual_jkt = jwk_thumbprint(public_jwk)
        if claims.get("jkt") != actual_jkt or actual_jkt != required_jkt:
            return None
        if str(claims.get("sub") or "").strip() != required_sub:
            return None

        target = urlsplit(f"{NEOTOMA_BASE_URL.rstrip('/')}/correct")
        signature_params = headers["signature-input"].split("=", 1)[1]
        signature_base = "\n".join(
            (
                '"@method": POST',
                f'"@authority": {target.netloc}',
                f'"@path": {target.path or "/"}',
                f'"content-type": {headers["content-type"]}',
                f'"content-digest": {headers["content-digest"]}',
                f'"signature-key": {headers["signature-key"]}',
                f'"@signature-params": {signature_params}',
            )
        ).encode("utf-8")
        raw_signature = base64.b64decode(signature_match.group(1), validate=True)
        if len(raw_signature) != 64:
            return None
        r = int.from_bytes(raw_signature[:32], "big")
        s = int.from_bytes(raw_signature[32:], "big")
        key.verify(
            encode_dss_signature(r, s),
            signature_base,
            ec.ECDSA(hashes.SHA256()),
        )
        return claims
    except Exception as exc:  # noqa: BLE001 — invalid auth always denies
        log.warning(
            "checkpoint resolver authentication failed for required principal %s: %s",
            required_sub,
            type(exc).__name__,
        )
        return None


# Tie-break order for equal-length keyword matches, most specific first.
#
# Only consulted when two roles match a description with keywords of identical
# length; unequal lengths are always decided by length alone. Without this,
# equal-length ties fall to whichever role appears first in role_keywords —
# reintroducing the declaration-order dependence the length rule exists to
# remove (e.g. "payment" and "bug fix" are both 7 characters).
#
# Rationale for the order: money and irreversible external actions outrank
# generic implementation work, so an ambiguous description escalates toward the
# more consequential handler rather than silently landing on code.
ROLE_TIE_BREAK: tuple[str, ...] = (
    "payments",
    "tax",
    "release_manager",
    # `legal` outranks `compliance`: both can match a risk-flavoured
    # description, and misrouting a legal judgment to conformance review is the
    # more costly direction — compliance answers "does this meet the standard",
    # which is the wrong question when someone asks whether they may act at all.
    "legal",
    "compliance",
    "pr_steward",
    "issue_triage",
    # `architect` above `qa`/`code`: an interface or schema change wants the
    # arch judgment before implementation or coverage review, matching the gate
    # order the swarm already enforces (arch signs off before impl).
    "architect",
    "qa",
    "code",
)


def _role_priority(role: str) -> int:
    """Higher is more specific. Unlisted roles share the lowest priority."""
    try:
        return len(ROLE_TIE_BREAK) - ROLE_TIE_BREAK.index(role)
    except ValueError:
        return 0


# ── Tool implementations ─────────────────────────────────────────────────────

def _get_swarm_roster() -> dict:
    entities = _retrieve_entities(
        "swarm_roster",
        snapshot_filters={"roster_key": {"op": "eq", "value": ROSTER_KEY}},
        limit=1,
    )
    if not entities:
        # Distinguish "Neotoma has no such roster" from "the request failed" —
        # reporting the former for the latter is what hid the /retrieve 404.
        transport_error = _describe_transport_error()
        if transport_error:
            return {
                "error": f"could not reach Neotoma: {transport_error}",
                "roster_key": ROSTER_KEY,
                "transport_error": transport_error,
            }
        return {"error": "swarm_roster not found", "roster_key": ROSTER_KEY}

    snap = _snapshot_of(entities[0])
    roles_raw = snap.get("roles", "{}")
    if isinstance(roles_raw, str):
        try:
            roles = json.loads(roles_raw)
        except (json.JSONDecodeError, TypeError):
            roles = {}
    else:
        roles = roles_raw

    return {
        "entity_id": entities[0].get("entity_id", entities[0].get("id", "")),
        "roster_key": ROSTER_KEY,
        "swarm_domain": snap.get("swarm_domain", ""),
        "roles": roles,
    }


# Action types no agent may execute, whatever the policy sets say. Kept in
# sync with `lib.daemon_runtime.gating.NEVER_AUTO_EXECUTE_ACTION_TYPES`, which
# is the canonical definition and the enforcing one; duplicated here rather
# than imported so this MCP server keeps its zero-dependency-on-daemon-runtime
# posture. `test_route_task_blast_radius.py` asserts the two stay identical,
# so adding a member in one place and not the other fails CI (ateles#715).
NEVER_AUTO_EXECUTE_ACTION_TYPES = frozenset({"operator_only"})


def _route_task(task_description: str, action_type: str | None = None) -> dict:
    roster = _get_swarm_roster()
    if "error" in roster:
        return roster

    roles: dict[str, str] = roster.get("roles", {})

    best_role: str | None = None
    best_agent: str | None = None

    desc_lower = task_description.lower()
    # Declaration order is cosmetic — the longest matching keyword wins (see
    # the selection loop below), so specificity is decided by keyword length,
    # not by position in this table. Grouping here is for readability only.
    role_keywords: dict[str, list[str]] = {
        "pr_steward": ["review pr", "merge pr", "pull request review"],
        # `architect` (waxwing) had no entry at all, so every architecture
        # review fell through to the dispatcher — the most-trafficked review
        # path in the swarm silently unrouted. Keywords favour the review/design
        # framing over the bare word "architecture", which appears incidentally
        # in many descriptions that are not requests for an arch judgment.
        "architect": [
            "architectural",
            "architecture review",
            "arch review",
            "design review",
            "contract-first",
            "schema design",
            "interface change",
        ],
        "issue_triage": ["issue", "bug report", "github issue", "triage issue"],
        "email_triage": ["email", "inbox", "triage email", "mail"],
        "financial_analysis": ["financial analysis", "revenue", "forecast"],
        "customer_intelligence": ["customer", "lead", "prospect"],
        "strategy_adversary": ["strategy", "adversarial", "red team"],
        "release_manager": ["release", "deploy", "version"],
        "recurring_tasks": ["recurring", "scheduled task", "cron"],
        "neotoma_repo": ["neotoma", "neotoma repo"],
        # `legal` (buteo) and `compliance` (robin) are distinct roster roles and
        # were previously indistinguishable: compliance claimed "contract", so
        # "is this contract change legally risky" routed confidently to
        # compliance. A confident wrong match is worse than no match — a
        # fallback at least signals uncertainty via `matched_via`, while this
        # looked identical to a correct route. Legal owns the judgment calls
        # (is this risky, may we say this); compliance owns conformance to a
        # stated standard.
        "legal": ["legal", "legally", "liability", "licence", "license", "terms of service"],
        "compliance": ["compliance", "regulatory", "gdpr", "conformance"],
        # "contract" is deliberately claimed by neither: it is ambiguous across a
        # legal agreement, an API contract, and a contractor engagement. Left
        # unmatched it reaches the dispatcher, which is the honest answer.
        "screenshots": ["screenshot", "capture screen"],
        "designer": ["design", "mockup", "wireframe", "ui ", "ux "],
        "content": ["blog", "write post", "content", "article"],
        "briefings": ["meeting", "briefing", "agenda", "calendar"],
        "payments": ["payment", "invoice", "pay ", "transfer", "wage"],
        "health": ["workout", "exercise", "gym", "fitness", "health"],
        "tax": ["tax", "fiscal", "iva", "vat", "hacienda"],
        "mirror": ["mirror", "sync repo", "apus"],
        "gtm": ["go to market", "gtm", "launch"],
        "pm": ["product", "roadmap", "feature plan"],
        "crm": ["contact", "crm", "relationship"],
        "qa": ["test", "qa ", "quality"],
        "code": [
            "code", "implement", "build", "refactor",
            # Natural bug-fix phrasings. A rigid "fix bug" misses the far more
            # common "fix a bug" / "fix the bug", which then fell through to
            # the dispatcher fallback.
            "fix bug", "fix a bug", "fix the bug", "bugfix", "bug fix",
        ],
        "dispatcher": ["dispatch", "assign", "route"],
    }

    # Selection is (keyword length, role priority) — never dict order.
    #
    # Longest keyword wins: "refactor the payment module" must reach code via
    # "refactor" (8) rather than payments via "payment" (7).
    #
    # Ties are the subtle half. Length alone leaves equal-length matches to be
    # settled by whichever role is declared first, which is the same
    # order-dependence in a different disguise — e.g. "process payment for a
    # bug fix" matches payments' "payment" (7) and code's "bug fix" (7), and
    # silently resolved to payments purely by position. ROLE_TIE_BREAK states
    # the intent explicitly: when two roles match equally well, the more
    # consequential/specific handler wins. Roles absent from the list share the
    # lowest priority and then fall back to alphabetical order, so the result is
    # always deterministic and never depends on table position.
    best_key: tuple[int, int, str] | None = None
    matched_keyword: str | None = None
    for role, keywords in role_keywords.items():
        if role not in roles:
            continue
        for kw in keywords:
            if kw not in desc_lower:
                continue
            # Higher tuple sorts better: longer keyword, then higher priority,
            # then a stable alphabetical tiebreak (negated via reverse compare).
            priority = _role_priority(role)
            key = (len(kw), priority, role)
            if best_key is None or key > best_key:
                best_key = key
                matched_keyword = kw
                best_role = role
                best_agent = roles[role]

    # Fallback: nothing in the table matched this description at all.
    matched_via = "keyword" if matched_keyword else "fallback"
    if not best_agent:
        best_role = "dispatcher"
        best_agent = roles.get("dispatcher")

    agent_def = None
    if best_agent:
        agents = _retrieve_entities("agent_definition", search=best_agent, limit=1)
        if agents:
            snap = _snapshot_of(agents[0])
            agent_def = {
                "entity_id": agents[0].get("entity_id", agents[0].get("id", "")),
                "name": snap.get("name", ""),
                "description": snap.get("description", ""),
                "prompt_markdown": snap.get("prompt_markdown", ""),
                "context_entity_types": snap.get("context_entity_types", []),
                "operational_entity_types": snap.get("operational_entity_types", []),
                "tool_allowlist": snap.get("tool_allowlist", []),
                "tier": snap.get("tier", ""),
                "aauth_sub": snap.get("aauth_sub", ""),
            }

    policy_id = AGENT_POLICY_OVERRIDES.get(
        (best_agent or "").lower(), DEFAULT_POLICY_ID
    )
    policy_data = _get(f"/entities/{policy_id}")
    policy = None
    if policy_data:
        psnap = _snapshot_of(policy_data)
        policy = {
            "entity_id": policy_id,
            "title": psnap.get("title", ""),
            "confidence_threshold": psnap.get("confidence_threshold"),
            "blast_radius_default": psnap.get("blast_radius_default"),
            "high_blast_action_types": psnap.get("high_blast_action_types"),
            # Needed by _action_blast_radius: an action type must be checked
            # against BOTH sets, since "in neither" is now a distinct (and
            # never-auto-executable) verdict rather than a silent "low".
            "low_blast_action_types": psnap.get("low_blast_action_types"),
        }

    result: dict[str, Any] = {
        "matched_role": best_role,
        "matched_agent": best_agent,
        # Why this role won: the keyword that matched (longest match wins), or
        # "fallback" when nothing matched. Makes a misroute a one-field
        # diagnosis instead of a source dive through role_keywords.
        "matched_keyword": matched_keyword,
        "matched_via": matched_via,
        "swarm_domain": roster.get("swarm_domain", ""),
    }
    if agent_def:
        result["agent_definition"] = agent_def
    if policy:
        result["execution_policy"] = policy
    if action_type:
        result["action_type"] = action_type
        if policy:
            result["action_blast_radius"] = _action_blast_radius(action_type, policy)
    return result


def _action_blast_radius(action_type: str, policy: dict) -> str:
    """Classify an action type's blast radius for the routing advisory.

    Mirrors `lib.daemon_runtime.gating.ExecutionPolicy.blast_radius_for`, which
    is the enforcing path; this one is advisory (it tells a caller what the gate
    will decide). The two must agree, or the advice misleads.

    Ordering, per ateles#715:

    * `operator_only` → "never", ahead of both policy sets. Never auto-executable
      at any confidence or recurrence count.
    * explicitly low → "low"; explicitly high → "high".
    * declared but in neither set → "never", not "low". The previous
      `else "low"` was the same fail-open as the enforcing path: an action type
      nobody had classified was advertised as safe.
    """
    at = action_type.strip().lower()
    if at in NEVER_AUTO_EXECUTE_ACTION_TYPES:
        return "never"

    def _as_list(v) -> list[str]:
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except (json.JSONDecodeError, TypeError):
                return []
        return [str(x).strip().lower() for x in v] if isinstance(v, list) else []

    if at in _as_list(policy.get("low_blast_action_types")):
        return "low"
    if at in _as_list(policy.get("high_blast_action_types")):
        return "high"
    return "never"


#: Briefs returned per call. The queue is 372 deep as of 2026-09-16, so a
#: single response cannot both carry every brief and stay a readable tool
#: result. The cap is therefore kept — what changed is that the response now
#: STATES what it left out and how to reach it, instead of reporting the page
#: as the queue.
CHECKPOINT_PAGE_SIZE = 50


def _list_checkpoints(cursor: str | None = None, limit: int | None = None) -> dict:
    """Pending checkpoint briefs — the operator's decision queue.

    Reports the queue's TRUE depth (`total`) separately from what this page
    carries (`returned`), and hands back a `next_cursor` whenever there is more.

    The defect this closes (ateles#1037): the old version passed a hardcoded
    `limit=50`, never a cursor, and returned `{"count": len(checkpoints)}` —
    the page size under a name that reads as the queue size. Measured against
    prod on 2026-09-16 it reported `count: 50` against a real 372, and because
    entities come back by `entity_id` ascending and ids are immutable, the same
    50 reappeared on every call: 322 briefs were unreachable through this tool
    no matter how often it was called.

    A truncated list that cannot be distinguished from a complete one is worse
    than no list, because it is acted on with confidence. That is why
    `truncated` is stated explicitly rather than left to be inferred from
    comparing two numbers.
    """
    page_size = limit or CHECKPOINT_PAGE_SIZE
    data = _retrieve_page(
        "checkpoint_brief",
        snapshot_filters={"status": {"op": "eq", "value": "awaiting_operator"}},
        limit=page_size,
        cursor=cursor,
    )

    # A failed read must NOT be spelled the same way as an empty queue. Reported
    # as `count: 0` it would tell the operator they have nothing to decide,
    # which is the most dangerous output this tool can produce.
    if data is None:
        return {
            "error": "could not read the checkpoint queue from Neotoma",
            "detail": _describe_transport_error(),
            "checkpoints": [],
        }

    entities = data.get("entities", [])
    total = data.get("total")
    next_cursor = data.get("next_cursor")

    checkpoints = []
    for ent in entities:
        snap = _snapshot_of(ent)
        eid = ent.get("entity_id", ent.get("id", ""))

        task_id = snap.get("task_entity_id")
        task_title = None
        if task_id:
            task_data = _get(f"/entities/{task_id}")
            if task_data:
                tsnap = _snapshot_of(task_data)
                task_title = tsnap.get("title", "")

        checkpoints.append({
            "checkpoint_id": eid,
            "title": snap.get("title", ""),
            "status": snap.get("status", ""),
            "handler": snap.get("handler", ""),
            "task_entity_id": task_id,
            "task_title": task_title,
            "confidence": snap.get("confidence"),
            "confidence_threshold": snap.get("confidence_threshold"),
            "blast_radius": snap.get("blast_radius", ""),
            "gate_action": snap.get("gate_action", ""),
            "reason": snap.get("reason", ""),
            "proposed_alternatives": snap.get("proposed_alternatives", []),
        })

    returned = len(checkpoints)
    if total is None:
        # The endpoint did not report a total. Say so rather than substituting
        # the page size, which is the exact conflation this fix exists to end.
        total = returned if next_cursor is None else None

    result: dict[str, Any] = {
        # The queue's real depth. Named `total` so a caller reading one field
        # gets the alarming number, not the reassuring one.
        "total": total,
        # What THIS page carries. Never a synonym for the queue size.
        "returned": returned,
        # Retained so existing callers keep working, but aligned to `total`
        # rather than to the page: `count` read as the queue size for months,
        # and leaving it meaning the page would preserve the defect under its
        # original name.
        "count": total if total is not None else returned,
        "checkpoints": checkpoints,
    }
    if next_cursor:
        result["truncated"] = True
        result["next_cursor"] = next_cursor
        result["note"] = (
            f"Showing {returned} of {total if total is not None else 'an unknown number of'} "
            "pending checkpoints. Pass `cursor` to retrieve the next page."
        )
    return result


def _entity_type_of(data: dict) -> str:
    """Return the declared entity type, or empty when the read is ambiguous."""
    return str(data.get("entity_type") or data.get("type") or "").strip().lower()


_RELEASE_CONFIRMED_TASK_STATUSES = frozenset(
    {"routed", "executing", "verified", "done", "completed", "complete", "finished"}
)


def _load_apis_daemon():
    """Load the shared checkpoint consumer without duplicating its safety policy."""
    daemon_dir = Path(__file__).resolve().parents[2] / "daemons" / "apis"
    if str(daemon_dir) not in sys.path:
        sys.path.insert(0, str(daemon_dir))

    import apis as apis_daemon

    return apis_daemon


def _require_checkpoint_release_state() -> Path:
    """Fail MCP startup closed unless replay-denial state is durably bound."""
    daemon_dir = Path(__file__).resolve().parents[2] / "daemons" / "apis"
    if str(daemon_dir) not in sys.path:
        sys.path.insert(0, str(daemon_dir))
    from checkpoint_denial_store import require_checkpoint_denial_store

    return require_checkpoint_denial_store()


async def _consume_checkpoint_resolution(checkpoint_id: str, snapshot: dict) -> bool:
    """Run the existing Apis checkpoint consumer in-process."""
    apis_daemon = _load_apis_daemon()
    notifier = apis_daemon.Notifier.from_neotoma()
    return await apis_daemon.handle_checkpoint_brief(
        checkpoint_id,
        snapshot,
        notifier,
        detach_after_accept=True,
    )


async def _await_release_acceptance(checkpoint_id: str, snapshot: dict) -> bool:
    """Shield the durable release handshake from MCP transport cancellation."""
    task = asyncio.create_task(
        _consume_checkpoint_resolution(checkpoint_id, snapshot),
        name=f"checkpoint-release-{checkpoint_id}",
    )
    _release_acceptance_tasks.add(task)
    task.add_done_callback(_release_acceptance_done)
    return await asyncio.shield(task)


async def _resolve_checkpoint(
    checkpoint_id: str,
    action: str,
    resolver_aauth_headers: dict[str, str] | None = None,
) -> dict:
    action_lower = action.strip().lower()
    if action_lower not in ("approve", "reject"):
        return {"error": f"action must be 'approve' or 'reject', got '{action}'"}

    data = _get(f"/entities/{checkpoint_id}")
    if data is None:
        return {"error": f"checkpoint {checkpoint_id} not found or Neotoma unreachable"}
    if _entity_type_of(data) != "checkpoint_brief":
        return {
            "error": f"entity {checkpoint_id} is not a checkpoint_brief",
            "checkpoint_id": checkpoint_id,
        }

    snap = _snapshot_of(data)
    current_status = str(snap.get("status", "")).strip().lower()
    if current_status not in ("awaiting_operator",):
        return {
            "error": f"checkpoint is '{current_status}', not 'awaiting_operator' — cannot resolve",
            "checkpoint_id": checkpoint_id,
        }

    dispatched = snap.get("resolved_dispatched")
    if dispatched is True or str(dispatched).strip().lower() in {"true", "1", "yes"}:
        return {
            "error": "checkpoint already dispatched — this is a replay",
            "checkpoint_id": checkpoint_id,
        }

    headers = _resolver_headers(resolver_aauth_headers)
    if headers is None:
        return {
            "error": "authenticated resolver RFC 9421 headers are required",
            "checkpoint_id": checkpoint_id,
        }
    resolver_authority = _checkpoint_resolver_authority(checkpoint_id, data)
    if resolver_authority is None:
        return {
            "error": "checkpoint has no authenticated required-resolver authority",
            "checkpoint_id": checkpoint_id,
        }
    required_principal_sub, required_principal_jkt, checkpoint_user_id = (
        resolver_authority
    )
    new_status = "approved" if action_lower == "approve" else "rejected"
    idem_key = f"resolve-checkpoint-{checkpoint_id}-{new_status}"
    from lib.daemon_runtime.checkpoint_protocol import checkpoint_resolution_body

    correction_body = checkpoint_resolution_body(checkpoint_id, action_lower)
    resolver = _authenticate_checkpoint_resolver(
        headers,
        body=_canonical_body_bytes(correction_body),
        required_principal_sub=required_principal_sub,
        required_principal_jkt=required_principal_jkt,
    )
    if resolver is None:
        return {
            "error": "resolver authentication is missing, invalid, or mismatched",
            "checkpoint_id": checkpoint_id,
        }

    ok = _correct(
        checkpoint_id,
        "checkpoint_brief",
        "status",
        new_status,
        idem_key,
        resolver_aauth_headers=headers,
    )
    if not ok:
        return {"error": "failed to correct checkpoint status in Neotoma"}

    resolved_data = _get(f"/entities/{checkpoint_id}")
    resolved_snap = _snapshot_of(resolved_data or {})
    authenticated_resolution = None
    if resolved_data is not None and _entity_type_of(resolved_data) == "checkpoint_brief":
        from lib.daemon_runtime.gating import read_authenticated_checkpoint_resolution

        authenticated_resolution = read_authenticated_checkpoint_resolution(
            checkpoint_id,
            resolved_data,
            required_approver_sub=required_principal_sub,
            required_approver_jkt=required_principal_jkt,
            expected_user_id=checkpoint_user_id,
            expected_resolution=new_status,
        )
    if authenticated_resolution is None:
        return {
            "error": "checkpoint resolution attribution did not authenticate as the required resolver",
            "checkpoint_id": checkpoint_id,
        }

    task_id = snap.get("task_entity_id")
    action_taken = "rejected — task decline not confirmed by read-back"
    if action_lower == "reject":
        # The Apis consumer owns task decline as well as release. It verifies
        # the immutable rejected observation against the same pinned resolver
        # identity before changing the task, so no second, differently bound
        # signature is needed for the task correction.
        consumed = await _await_release_acceptance(checkpoint_id, resolved_snap)
        consumed_data = _get(f"/entities/{checkpoint_id}")
        consumed_snap = _snapshot_of(consumed_data or {})
        task_data = _get(f"/entities/{task_id}") if task_id else None
        task_snap = _snapshot_of(task_data or {})
        stamp_confirmed = (
            consumed_data is not None
            and _entity_type_of(consumed_data) == "checkpoint_brief"
            and (
                consumed_snap.get("resolved_dispatched") is True
                or str(consumed_snap.get("resolved_dispatched", "")).strip().lower()
                in {"true", "1", "yes"}
            )
        )
        if (
            consumed
            and stamp_confirmed
            and task_data is not None
            and _entity_type_of(task_data) == "task"
            and str(task_snap.get("status") or "").strip().lower() == "declined"
        ):
            action_taken = "rejected — task marked declined"

    if action_lower == "approve":
        # Read the write back before releasing anything. The original defect
        # returned success after the correction request even when no consumer
        # had acted on the approved brief.
        released = False
        if (
            resolved_data is not None
            and _entity_type_of(resolved_data) == "checkpoint_brief"
            and str(resolved_snap.get("status", "")).strip().lower() == "approved"
        ):
            released = await _await_release_acceptance(checkpoint_id, resolved_snap)

        # Claim a release only when BOTH the checkpoint claim and task lifecycle
        # read back. A 2xx from either correction is not that proof.
        consumed_data = _get(f"/entities/{checkpoint_id}")
        consumed_snap = _snapshot_of(consumed_data or {})
        stamp_confirmed = (
            consumed_data is not None
            and _entity_type_of(consumed_data) == "checkpoint_brief"
            and (
                consumed_snap.get("resolved_dispatched") is True
                or str(consumed_snap.get("resolved_dispatched", "")).strip().lower()
                in {"true", "1", "yes"}
            )
        )
        task_data = _get(f"/entities/{task_id}") if task_id else None
        task_snap = _snapshot_of(task_data or {})
        brief_user_id = resolved_snap.get("user_id")
        task_user_id = task_snap.get("user_id")
        same_tenant = bool(
            brief_user_id
            and task_user_id
            and brief_user_id == task_user_id
        )
        if (
            released
            and stamp_confirmed
            and task_data is not None
            and _entity_type_of(task_data) == "task"
            and same_tenant
            and str(task_snap.get("status", "")).strip().lower()
            in _RELEASE_CONFIRMED_TASK_STATUSES
            and task_snap.get("blocked_reason") == ""
        ):
            action_taken = "approved — task re-dispatched"
        elif str(resolved_snap.get("blast_radius", "")).strip().lower() == "never":
            action_taken = (
                "approved — never-tier checkpoint recorded; no agent dispatch"
            )
        else:
            action_taken = "approved — task release not confirmed by read-back"

    return {
        "checkpoint_id": checkpoint_id,
        "new_status": new_status,
        "task_entity_id": task_id,
        "action_taken": action_taken,
    }


# ── Swarm observability (read-only) ──────────────────────────────────────────
#
# These three tools answer "is the swarm going to continue this?" — the question
# that on 2026-08-19 required hand-retrieving an issue entity, grepping
# apis.log, running launchctl, then grepping the log again to discover a queue
# of six pipelines behind one serialized slot.
#
# READ-ONLY BY CONSTRUCTION. None of them writes gate state. Advancing a gate
# from a session would let a session sign off its own work — precisely the
# SELF-CERTIFICATION BOUNDARY the dispatcher maintains (ateles#230 arch §4, and
# the boundary comment in execution/daemons/apis/swarm_dispatch.py, where even
# a re-review never writes a gate: only the lens agent that owns a gate flips
# it). Visibility is the safe half and is where nearly all the value is; if a
# mutating counterpart is ever added it belongs behind the operator-approval
# path resolve_checkpoint already uses, not as a free-form gate setter.

# Gate order used for reporting. Mirrors the pre-impl → impl → review ordering
# the dispatcher enforces; an absent gate is unsigned, not cleared.
_GATE_ORDER = ("pm", "ux", "arch", "impl", "pr_review")

# States that count as "this gate is not waiting on anyone".
_CLEARED_GATE_STATES = {"signed_off", "not_required", "waived"}


def _parse_issue_ref(issue_ref: str) -> tuple[str | None, int | None, str | None]:
    """Split "owner/repo#123" into (repo, number, entity_id).

    An "ent_..." value is returned as an entity id instead. Returns all-None
    components it cannot parse, so the caller reports a usable error rather
    than silently querying for nothing.
    """
    ref = (issue_ref or "").strip()
    if not ref:
        return None, None, None
    if ref.startswith("ent_"):
        return None, None, ref
    if "#" in ref:
        repo, _, num = ref.partition("#")
        repo = repo.strip()
        num = num.strip()
        if repo and num.isdigit():
            return repo, int(num), None
    return None, None, None


def _issue_snapshot_matches(snap: dict, repo: str, number: int) -> bool:
    """True when *snap* is the issue entity for ``repo#number``.

    Tolerates the duplicated field names seen in prod — ``repo``/``repository``
    and ``issue_number``/``github_number``/``number`` — the same tolerance
    execution/daemons/apis/gate_waive.py needs. Matching on only one spelling
    silently misses entities that use the other.
    """
    snap_repo = snap.get("repo") or snap.get("repository") or ""
    if str(snap_repo) != str(repo):
        return False
    for key in ("issue_number", "github_number", "number"):
        value = snap.get(key)
        if value is not None and str(value) == str(number):
            return True
    return False


def _parse_owner_history(raw: Any) -> list[dict]:
    """Normalize a stored ``owner_history`` into a list of dicts.

    Prod stores this as either a list or a JSON-encoded string; mirrors
    gate_waive.parse_owner_history.
    """
    if isinstance(raw, list):
        return [e for e in raw if isinstance(e, dict)]
    if isinstance(raw, str) and raw.strip():
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError):
            return []
        if isinstance(decoded, list):
            return [e for e in decoded if isinstance(e, dict)]
    return []


def _dedupe_history(entries: list[dict]) -> list[dict]:
    """Drop exact duplicate history entries, preserving order.

    Real entities carry them: ent_d03638842effc4f76ea05a1a (neotoma#2169) has
    its legacy_gate_init and pr_review sign-off recorded twice, so a naive
    "last 3" would report one event as three.
    """
    seen: set[str] = set()
    out: list[dict] = []
    for entry in entries:
        key = json.dumps(entry, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out


def _blocking_gates(gate_status: dict) -> list[str]:
    """Gates that are not cleared, in gate order.

    A total function over _GATE_ORDER: a gate missing from *gate_status* is
    unsigned, not cleared. (The 2026-07-23 waive regression came from iterating
    the stored map instead of the full gate list.) Unknown extra gates are
    appended so a newly-added gate is never invisible here.
    """
    out = [g for g in _GATE_ORDER if str(gate_status.get(g, "")).strip().lower() not in _CLEARED_GATE_STATES]
    out += [
        g for g in sorted(gate_status)
        if g not in _GATE_ORDER
        and str(gate_status.get(g, "")).strip().lower() not in _CLEARED_GATE_STATES
    ]
    return out


def _get_gate_status(issue_ref: str, history_limit: int = 5) -> dict:
    repo, number, entity_id = _parse_issue_ref(issue_ref)
    if not entity_id and not repo:
        return {
            "error": (
                f"could not parse issue_ref '{issue_ref}' — expected "
                "'owner/repo#123' or an 'ent_...' entity id"
            )
        }

    entity: dict | None = None
    if entity_id:
        entity = _get(f"/entities/{entity_id}")
        if entity is None:
            return {
                "error": f"issue entity {entity_id} not found or Neotoma unreachable",
                "transport_error": _describe_transport_error(),
            }
    else:
        # Field names vary per entity (repo/repository, issue_number/
        # github_number), so the match is client-side. But an UNFILTERED scan is
        # not enough on its own: the issue set exceeds the page limit, and
        # neotoma#2169 sits past the first 500 rows — a plain scan reported
        # "no issue entity found" for an issue that plainly exists. Query by
        # number first (which does reach it), then fall back to the broad scan
        # for entities the search index does not surface.
        candidates = _retrieve_entities("issue", search=str(number), limit=100)
        for ent in candidates:
            if _issue_snapshot_matches(_snapshot_of(ent), repo or "", number or 0):
                entity = ent
                break
        if entity is None:
            for ent in _retrieve_entities("issue", limit=500):
                if _issue_snapshot_matches(_snapshot_of(ent), repo or "", number or 0):
                    entity = ent
                    break
        if entity is None:
            err = _describe_transport_error()
            return {
                "error": (
                    f"no issue entity found for {issue_ref}"
                    if not err
                    else f"could not read issue entities for {issue_ref}"
                ),
                # Distinguishing these matters: "no entity" invites creating
                # one, "transport failed" invites a retry or an escalation.
                "transport_error": err,
            }

    snap = _snapshot_of(entity)
    eid = entity.get("entity_id", entity.get("id", ""))

    gate_status = snap.get("gate_status") or {}
    if isinstance(gate_status, str):
        try:
            gate_status = json.loads(gate_status)
        except (ValueError, TypeError):
            gate_status = {}
    if not isinstance(gate_status, dict):
        gate_status = {}

    history = _dedupe_history(_parse_owner_history(snap.get("owner_history")))
    # Stored oldest-first; the recent entries are the ones that explain "who
    # has it now and why".
    recent = history[-history_limit:] if history_limit > 0 else history

    blocking = _blocking_gates(gate_status)
    repo_name = str(snap.get("repo") or snap.get("repository") or "")
    number_val = snap.get("github_number") or snap.get("issue_number") or snap.get("number")

    pipeline = _pipeline_state_for(repo_name, number_val)

    return {
        "entity_id": eid,
        "issue_ref": f"{repo_name}#{number_val}" if repo_name and number_val else issue_ref,
        "title": snap.get("title", ""),
        "status": snap.get("status", ""),
        "github_url": snap.get("github_url", ""),
        "current_owner": snap.get("current_owner", ""),
        "gate_status": gate_status,
        "blocking_gates": blocking,
        "all_gates_cleared": not blocking,
        "owner_history_recent": recent,
        "owner_history_total": len(history),
        "pipeline": pipeline,
        "interpretation": _gate_interpretation(snap, blocking, pipeline),
    }


def _gate_interpretation(snap: dict, blocking: list[str], pipeline: dict) -> str:
    """One line answering "is the swarm going to continue this?".

    Deliberately conservative: it reports what the records show and never
    promises the swarm will act.
    """
    owner = str(snap.get("current_owner") or "").strip()
    if str(snap.get("status", "")).strip().lower() == "closed":
        return "issue is closed"
    stage = pipeline.get("stage")
    if stage == "queued":
        return (
            "a pipeline is QUEUED for this issue — waiting on the issue-pipeline "
            "slot, not on a gate"
        )
    if stage == "inflight":
        return "a pipeline is INFLIGHT for this issue"
    if not blocking:
        return "all gates cleared — nothing gate-blocked here"
    gates = ", ".join(blocking)
    if owner:
        return f"waiting on {owner} for gate(s): {gates}"
    return f"waiting on gate(s): {gates} (no current_owner recorded)"


# Marker the dispatcher posts on the issue before/inside the pipeline slot.
# Format and regex mirror swarm_dispatch._PIPELINE_INFLIGHT_MARKER — the
# timestamp group must accept "+" and ":" because datetime.isoformat() emits
# "+00:00" rather than "Z". The stage group is optional so markers written by
# an older daemon build (no stage suffix) still parse, defaulting to inflight.
_PIPELINE_MARKER_RE = re.compile(
    r"<!-- apis-pipeline-inflight:([0-9T:.+\-]+Z?)(?::(queued|inflight))? -->"
)

GITHUB_API = os.environ.get("GITHUB_API_URL", "https://api.github.com")

# Bound + parallelism for the queue sweep: one GitHub request per candidate.
_PIPELINE_QUEUE_SCAN_LIMIT = int(os.environ.get("ATELES_PIPELINE_QUEUE_SCAN_LIMIT", "60"))
_PIPELINE_QUEUE_WORKERS = int(os.environ.get("ATELES_PIPELINE_QUEUE_WORKERS", "12"))

# Markers older than this are reported as stale rather than inflight. A real
# pipeline is minutes-to-hours; a marker surviving a day means its clear failed.
_PIPELINE_MARKER_STALE_SECONDS = float(
    os.environ.get("ATELES_PIPELINE_MARKER_STALE_SECONDS", str(6 * 3600))
)


def _github_headers() -> dict[str, str]:
    token = (
        os.environ.get("APIS_GITHUB_TOKEN")
        or os.environ.get("GITHUB_TOKEN")
        or os.environ.get("GH_TOKEN")
        or ""
    )
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _pipeline_markers(repo: str, number: Any) -> tuple[list[dict], str | None]:
    """Return (markers, error) for an issue, markers oldest first.

    Reads the durable GitHub marker rather than the daemon log: the log is
    local to the daemon host and rotates, while the marker is written before
    the semaphore is acquired specifically so a queued pipeline leaves a trace
    that survives a restart (ateles#323).

    The second element is the reason the read failed, or None on success. It
    exists because "no markers" and "I could not read the markers" are
    different answers and only one of them is safe to act on. Collapsing them
    into an empty list is the same fail-open shape this swarm's security work
    is about: a check that cannot tell absence from failure, reporting the
    permissive answer. An expired GitHub token would otherwise make every
    issue look idle.
    """
    # A bare repo name ("ateles") is not addressable on the GitHub API and only
    # yields 404 noise; require the owner/repo form.
    if not repo or not number or "/" not in str(repo):
        return [], None
    try:
        resp = httpx.get(
            f"{GITHUB_API}/repos/{repo}/issues/{number}/comments",
            headers=_github_headers(),
            params={"per_page": 100},
            timeout=15,
        )
        resp.raise_for_status()
        comments = resp.json()
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status == 404:
            # Genuinely absent (or invisible to this token) — not a read failure
            # we can distinguish, but an issue we cannot see has no marker we
            # could act on either. Reported as a read failure to stay honest.
            detail = f"HTTP 404 for {repo}#{number} (missing, or not visible to this token)"
        elif status in (401, 403):
            detail = (
                f"HTTP {status} — GitHub token missing, expired, or lacking scope; "
                "pipeline state is UNKNOWN, not absent"
            )
        else:
            detail = f"HTTP {status}"
        log.warning("github comments read failed for %s#%s: %s", repo, number, detail)
        return [], detail
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"
        log.warning("github comments read failed for %s#%s: %s", repo, number, detail)
        return [], detail

    markers: list[dict] = []
    for comment in comments if isinstance(comments, list) else []:
        match = _PIPELINE_MARKER_RE.search(str(comment.get("body") or ""))
        if match:
            markers.append({
                "started_at": match.group(1),
                "stage": match.group(2) or "inflight",
                "comment_id": comment.get("id"),
            })
    return markers, None


def _pipeline_state_for(repo: str, number: Any) -> dict:
    """Latest pipeline marker state for one issue.

    The dispatcher DELETES the marker when the pipeline finishes, so a marker
    still present means queued or inflight. Absence is reported as "none"
    rather than "finished": we cannot distinguish "completed" from "never
    started" from this signal alone, and claiming the stronger reading would
    be exactly the kind of confident-but-wrong status this tool exists to stop.
    """
    markers, read_error = _pipeline_markers(repo, number)
    if read_error:
        # Never assert absence from a failed read.
        return {
            "stage": "unknown",
            "error": read_error,
            "detail": (
                "could not determine pipeline state — the marker read failed. "
                "This is NOT the same as 'no pipeline running'."
            ),
        }
    if not markers:
        return {"stage": None, "detail": "no pipeline marker present (not queued or inflight)"}
    latest = markers[-1]
    waited = _age_seconds(latest.get("started_at"))
    stage = latest.get("stage")

    # A marker the daemon failed to delete outlives its pipeline. Real cases
    # exist: markmhendrickson/neotoma#2073 carries a marker from 2026-08-04
    # whose clear failed ("could not clear in-flight pipeline marker"), so a
    # naive read reports a fortnight-old marker as a running pipeline. Age it
    # out instead of asserting a pipeline that is certainly gone — reporting
    # "inflight" for a dead run is worse than reporting nothing.
    if waited is not None and waited > _PIPELINE_MARKER_STALE_SECONDS:
        return {
            "stage": "stale",
            "reported_stage": stage,
            "marked_at": latest.get("started_at"),
            "seconds_since_marked": waited,
            "detail": (
                f"marker is {waited / 3600:.1f}h old (> "
                f"{_PIPELINE_MARKER_STALE_SECONDS / 3600:.0f}h) — treating as STALE, "
                "not as a running pipeline; the daemon likely failed to clear it"
            ),
        }
    return {
        "stage": stage,
        "marked_at": latest.get("started_at"),
        "seconds_since_marked": waited,
        "detail": (
            f"pipeline marker present at stage '{stage}'"
            + (f", {waited:.0f}s ago" if waited is not None else "")
        ),
    }


def _age_seconds(timestamp: str | None) -> float | None:
    if not timestamp:
        return None
    try:
        ts = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()



def _swarm_repositories() -> list[str]:
    """Repos the dispatcher watches, as "owner/repo" strings.

    Mirrors the daemon's own configuration key so this tool follows the swarm
    rather than hardcoding a repo list that would silently rot.
    """
    raw = os.environ.get("APIS_SWARM_REPOSITORIES") or os.environ.get(
        "APIS_RESUME_REPOSITORIES", ""
    )
    repos = [r.strip() for r in raw.replace(",", " ").split() if "/" in r.strip()]
    if repos:
        return repos
    owner = os.environ.get("ATELES_GITHUB_OWNER", "markmhendrickson")
    return [f"{owner}/ateles", f"{owner}/neotoma"]


def _recent_open_issues(repo: str, limit: int) -> tuple[list[dict], bool, str | None]:
    """Most recently updated OPEN issues for *repo*, newest first.

    Returns (issues, more_available, error). Pull requests are filtered out:
    the GitHub issues endpoint returns both, and only issues carry pipeline
    markers. The error is non-None when the LISTING itself failed — without
    it, an unauthorized listing yields zero candidates and the sweep reports
    "nothing queued", which is the fail-open answer one level above the
    per-issue read.
    """
    try:
        resp = httpx.get(
            f"{GITHUB_API}/repos/{repo}/issues",
            headers=_github_headers(),
            params={
                "state": "open",
                "sort": "updated",
                "direction": "desc",
                "per_page": min(100, max(1, limit)),
            },
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        detail = f"HTTP {status}" if status else f"{type(exc).__name__}: {exc}"
        if status in (401, 403):
            detail += " — GitHub token missing, expired, or lacking scope"
        log.warning("github issue list failed for %s: %s", repo, detail)
        return [], False, f"{repo}: {detail}"
    issues = [
        i for i in (payload if isinstance(payload, list) else [])
        if isinstance(i, dict) and not i.get("pull_request") and i.get("number")
    ]
    return issues[:limit], len(issues) >= limit, None


def _list_pipeline_queue() -> dict:
    """What holds the issue-pipeline slot, and what is queued behind it.

    The signal that was previously only discoverable by grepping apis.log.
    Built from the durable per-issue markers across the open issue set, so it
    reflects state the daemon actually committed rather than log lines that
    may have rotated away.
    """
    # Candidate set comes from GITHUB, not from Neotoma issue entities.
    #
    # This is the correctness point of the whole tool. A newly-opened issue has
    # no Neotoma entity yet — the entity is created later in the pipeline — so
    # an entity-derived candidate list structurally misses the newest issues,
    # which are exactly the ones most likely to be sitting in the queue.
    # Observed directly: ateles#435 and #436 were both logged QUEUED by the
    # daemon and both carried queued markers, while neither had an issue entity
    # to be found by. GitHub is the authority on which issues are open.
    repos = _swarm_repositories()
    candidates: list[tuple[str, Any, dict]] = []
    truncated = False
    per_repo = max(1, _PIPELINE_QUEUE_SCAN_LIMIT // max(1, len(repos)))
    list_errors: list[str] = []
    for repo in repos:
        issues, more, list_error = _recent_open_issues(repo, per_repo)
        if list_error:
            list_errors.append(list_error)
        truncated = truncated or more
        candidates.extend((repo, i["number"], i) for i in issues)

    # Could not even enumerate the issues: report that, rather than an
    # all-clear derived from an empty candidate set.
    if list_errors and not candidates:
        return {
            "error": "could not list open issues — pipeline state is unknown, not idle",
            "detail": list_errors,
            "repositories": repos,
        }

    inflight: list[dict] = []
    queued: list[dict] = []
    stale: list[dict] = []
    unknown: list[dict] = []
    with ThreadPoolExecutor(max_workers=_PIPELINE_QUEUE_WORKERS) as pool:
        futures = {
            pool.submit(_pipeline_state_for, repo, number): (repo, number, issue)
            for repo, number, issue in candidates
        }
        for future in as_completed(futures):
            repo, number, issue = futures[future]
            try:
                state = future.result()
            except Exception as exc:  # a single bad issue must not sink the sweep
                log.warning("pipeline state read failed for %s#%s: %s", repo, number, exc)
                continue
            stage = state.get("stage")
            if stage == "unknown":
                unknown.append({
                    "issue_ref": f"{repo}#{number}",
                    "title": issue.get("title", ""),
                    "error": state.get("error"),
                })
                continue
            if stage not in ("queued", "inflight", "stale"):
                continue
            row = {
                "issue_ref": f"{repo}#{number}",
                "title": issue.get("title", ""),
                "html_url": issue.get("html_url", ""),
                "marked_at": state.get("marked_at"),
                "waited_seconds": state.get("seconds_since_marked"),
            }
            if stage == "stale":
                row["reported_stage"] = state.get("reported_stage")
                row["detail"] = state.get("detail")
                stale.append(row)
            elif stage == "inflight":
                inflight.append(row)
            else:
                queued.append(row)
    stale.sort(key=lambda r: r.get("waited_seconds") or 0, reverse=True)

    queued.sort(key=lambda r: r.get("waited_seconds") or 0, reverse=True)
    inflight.sort(key=lambda r: r.get("waited_seconds") or 0, reverse=True)

    # If every candidate failed to read, "no pipeline queued" is not a finding —
    # it is a total absence of evidence, and must not be reported as an
    # all-clear.
    if candidates and len(unknown) == len(candidates):
        return {
            "error": "could not read pipeline state for ANY issue — state is unknown, not idle",
            "detail": unknown[0].get("error") if unknown else None,
            "unreadable": unknown,
            "unreadable_count": len(unknown),
            "issues_scanned": len(candidates),
        }

    capacity = int(os.environ.get("APIS_MAX_CONCURRENT_ISSUE_PIPELINES", "3"))
    longest = queued[0]["waited_seconds"] if queued else None

    return {
        "slot_capacity": capacity,
        "inflight_count": len(inflight),
        "queued_count": len(queued),
        "inflight": inflight,
        "queued": queued,
        "stale_markers": stale,
        "unreadable": unknown,
        "unreadable_count": len(unknown),
        "listing_errors": list_errors,
        "longest_wait_seconds": longest,
        "issues_scanned": len(candidates),
        "scan_truncated": truncated,
        "interpretation": (
            (
                f"{len(inflight)} pipeline(s) holding the slot (capacity {capacity}), "
                f"{len(queued)} queued behind"
                + (f"; longest wait {longest:.0f}s" if longest else "")
                if (inflight or queued)
                else "no pipeline currently queued or inflight"
            )
            + (
                f"; {len(stale)} stale marker(s) ignored (daemon failed to clear them)"
                if stale
                else ""
            )
            + (
                f"; scan limited to the {len(candidates)} most recent open issues"
                if truncated
                else ""
            )
            + (
                f"; {len(unknown)} issue(s) UNREADABLE — their pipeline state is "
                "unknown, not idle"
                if unknown
                else ""
            )
        ),
        "note": (
            "Built from the durable apis-pipeline-inflight markers on each open "
            "issue. Reads FAIL CLOSED: if the GitHub token is missing or the "
            "issue listing/marker read fails, this returns an `error` (or lists "
            "the affected issues under `unreadable` / `listing_errors`) rather "
            "than an empty all-clear — an empty queue here means the queue is "
            "genuinely empty, never that state could not be read. Markers older "
            "than the staleness threshold are listed under stale_markers, NOT "
            "counted as running."
        ),
    }


def _get_dispatch_health() -> dict:
    """Is the dispatcher alive, when did it last dispatch, what recently failed."""
    log_dir = Path(
        os.environ.get(
            "ATELES_LOG_DIR", str(Path.home() / "Library" / "Logs" / "ateles")
        )
    )
    apis_log = log_dir / "apis.log"
    failure_dir = Path(
        os.environ.get("DISPATCH_FAILURE_LOG_DIR", str(log_dir / "dispatch-failures"))
    )

    result: dict[str, Any] = {"log_path": str(apis_log)}

    # launchd liveness. `launchctl list <label>` exits non-zero when unloaded.
    label = os.environ.get("ATELES_APIS_LAUNCHD_LABEL", "com.ateles.apis")
    result["launchd_label"] = label
    try:
        proc = subprocess.run(
            ["launchctl", "list", label],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode == 0:
            pid = None
            last_exit = None
            for line in proc.stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith('"PID"'):
                    pid = stripped.split("=")[-1].strip().rstrip(";").strip()
                elif stripped.startswith('"LastExitStatus"'):
                    last_exit = stripped.split("=")[-1].strip().rstrip(";").strip()
            result["loaded"] = True
            result["pid"] = pid
            result["last_exit_status"] = last_exit
            result["running"] = bool(pid and pid not in ("0", "-"))
        else:
            result["loaded"] = False
            result["running"] = False
            result["detail"] = f"launchctl list {label} exited {proc.returncode}"
    except Exception as exc:
        # Not fatal: the MCP may run on a host without this daemon.
        result["loaded"] = None
        result["running"] = None
        result["detail"] = f"launchctl unavailable: {type(exc).__name__}: {exc}"

    # Last log activity. Read the tail only — apis.log runs to hundreds of MB,
    # so reading it whole would stall the call.
    if apis_log.exists():
        try:
            size = apis_log.stat().st_size
            with apis_log.open("rb") as fh:
                fh.seek(max(0, size - 200_000))
                tail = fh.read().decode("utf-8", errors="replace").splitlines()
            result["log_size_bytes"] = size
            result["log_mtime_age_seconds"] = round(
                datetime.now(timezone.utc).timestamp() - apis_log.stat().st_mtime, 1
            )
            recent = [ln for ln in tail if "issue pipeline for" in ln]
            result["last_pipeline_log_lines"] = recent[-5:]
        except Exception as exc:
            result["log_read_error"] = f"{type(exc).__name__}: {exc}"
    else:
        result["log_size_bytes"] = None
        result["detail_log"] = "apis.log not present on this host"

    # Recent dispatch failures.
    failures: list[dict] = []
    if failure_dir.is_dir():
        try:
            entries = sorted(
                (p for p in failure_dir.iterdir() if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for path in entries[:10]:
                age = datetime.now(timezone.utc).timestamp() - path.stat().st_mtime
                failures.append({
                    "file": path.name,
                    "age_seconds": round(age, 1),
                    "size_bytes": path.stat().st_size,
                })
            result["dispatch_failure_total"] = len(entries)
            result["dispatch_failures_last_24h"] = sum(
                1
                for p in entries
                if datetime.now(timezone.utc).timestamp() - p.stat().st_mtime < 86400
            )
        except Exception as exc:
            result["dispatch_failure_read_error"] = f"{type(exc).__name__}: {exc}"
    else:
        result["dispatch_failure_total"] = 0
    result["dispatch_failure_dir"] = str(failure_dir)
    result["recent_dispatch_failures"] = failures

    # Label gate (bootstrap mode / canary lane): report whether the automatic
    # issue/PR pipelines are currently restricted to a single label, so
    # "nothing is being reviewed" reads as an active, deliberate posture
    # rather than a broken dispatcher. Read directly from the env var
    # swarm_dispatch.DispatchConfig also reads (ATELES_SWARM_REQUIRE_LABEL) —
    # this MCP does not import swarm_dispatch, so the env var is the single
    # source rather than a second copy of the default.
    require_label = os.environ.get("ATELES_SWARM_REQUIRE_LABEL", "").strip()
    result["label_gate_active"] = bool(require_label)
    result["label_gate_label"] = require_label or None

    running = result.get("running")
    if running is True:
        health = "dispatcher is loaded and running"
    elif running is False:
        health = "dispatcher is NOT running — nothing will be dispatched"
    else:
        health = "dispatcher liveness unknown (not launchd-managed on this host)"
    recent_failures = result.get("dispatch_failures_last_24h")
    if recent_failures:
        health += f"; {recent_failures} dispatch failure(s) logged in the last 24h"
    if require_label:
        health += (
            f"; label gate ACTIVE — automatic issue/PR pipelines run only for "
            f"work labelled {require_label!r} (operator overrides /swarm-run "
            "and /confirm-gates-clear still work regardless of label)"
        )
    result["interpretation"] = health
    return result



# ── Swarm watch: per-task timeline and change feed (read-only) ───────────────
#
# ateles#1275, slice 1. A session that starts a harness subagent gets a handle,
# a completion notice, readable output, and a way to follow up. A session that
# hands work to the swarm got none of these: the only way to learn what became
# of a task was to re-read the entity by hand. These two tools are the first
# half of that parity, and they are READ-ONLY like the observability tools
# above — nothing here writes, so nothing here needs authority.
#
#   get_task_timeline(task_entity_id)  one task's history, merged and
#                                      time-ordered from every record the swarm
#                                      writes today, each entry carrying its
#                                      source and timestamp
#   watch_swarm(cursor, task_ids, ...) a bounded long-poll: what changed since
#                                      the cursor for the given tasks (and,
#                                      optionally, every checkpoint), plus a new
#                                      cursor
#
# `watch.py` next to this file is the same watch code as a blocking CLI, so a
# Claude Code session can run it in the background and be told when it exits.
#
# What the record can and cannot show (measured 2026-09-25, design comment on
# ateles#1275). Every response states which sources it joined and names what it
# could not see under `gaps`, so an absence is never mistaken for a fact:
#
#   * Runner events are not addressable per task. `skill_runner` writes them as
#     observations of ~15 SHARED `harness_event` entities, roughly one per
#     agent, so a snapshot filter on `task_entity_id` finds nothing. They are
#     found here by scanning `harness_event` observations inside a BOUNDED time
#     window around the task's own history. Beyond that window they are not
#     looked for, and the response says so.
#   * A runner's start and end carry no common id; they pair by agent and time.
#   * A runner's output is a host-local file, and a timed-out run keeps none.
#   * Most issue and PR runs carry no task reference at all, so PR and review
#     events join only when the task is related to an issue or pull_request
#     entity.
#
# Reads fail CLOSED: a failed read of the task itself is an `error`, never an
# empty timeline; a failed read of a secondary source is listed under
# `sources` with status "error" and named in `gaps`.

#: How far around a task's own history to scan shared runner events, at most.
TIMELINE_WINDOW_HOURS = float(os.environ.get("ATELES_TIMELINE_WINDOW_HOURS", "72"))
#: Upper bound on harness_event observations read for one timeline. The window
#: is scanned newest-first, so what a cap cuts off is the OLDEST part of it.
TIMELINE_MAX_SCAN = int(os.environ.get("ATELES_TIMELINE_MAX_SCAN", "2000"))
#: A runner start with no end, older than this, is reported as not live: the
#: runner's own timeout (1800 s by default) has passed and the end was lost.
RUNNER_LIVE_SECONDS = float(os.environ.get("ATELES_RUNNER_LIVE_SECONDS", "2100"))

_OBS_PAGE = 500
_ENTITY_OBS_PAGE = 200
_WINDOW_LEAD_SECONDS = 60
_WINDOW_TAIL_SECONDS = 300
# Observations that land between the count and the scan shift every offset by
# one; start this many rows early so none of the window is skipped.
_SCAN_OFFSET_MARGIN = 25
_TIMELINE_VALUE_CHARS = 240

WATCH_MAX_WAIT_SECONDS = 45
WATCH_POLL_SECONDS = float(os.environ.get("ATELES_WATCH_POLL_SECONDS", "5"))
WATCH_MAX_TASKS = 10
# The cursor re-reads this many seconds behind its own high-water mark and
# drops what it has already seen by observation id. Streams are read one after
# another, so a row written to an earlier stream while a later one was being
# read must not fall behind the cursor.
_CURSOR_OVERLAP_SECONDS = 30
_CURSOR_MAX_SEEN = 300
#: Oldest cursor a poll will resume from. A cursor far in the past makes the
#: first poll re-read up to the scan caps and issue one GET per checkpoint it
#: meets — hundreds of reads in one call against a hosted instance that has
#: fallen over under bulk reads before. A watch that old is a new watch.
WATCH_MAX_CURSOR_AGE_HOURS = float(os.environ.get("ATELES_WATCH_MAX_CURSOR_AGE_HOURS", str(24 * 7)))
_CURSOR_PREFIX = "w1."

# Injectable so tests can drive the long-poll without real waiting.
_watch_sleep = time.sleep
_watch_clock = time.monotonic
_watch_now = lambda: datetime.now(timezone.utc)  # noqa: E731 — wall clock, for cursor age

_TASK_ID_RE = re.compile(r"^ent_[A-Za-z0-9]{8,64}$")
# Apis writes task status as `taskstatus-<handler>-<task>-<status>-<trigger>`
# (lib/daemon_runtime/task_lifecycle), and the reason/result companions alike.
_IDEM_HANDLER_RE = re.compile(r"^task(?:status|reason|result)-([a-z0-9_]+)-")

_TASK_FIELD_KINDS = {
    "status": "status",
    "blocked_reason": "reason",
    "result": "result",
    "assigned_to": "assignment",
}
_CREATE_MARKER_FIELDS = ("title", "description")

#: The task fields these tools report. An allowlist, not a denylist: a task
#: observation can carry any field an agent or operator wrote, and the task
#: schema itself declares payment-, contact- and email-shaped ones (amount,
#: currency, payment_method, beneficiary_name, contact_entity_id,
#: linked_email_*), plus free text (description, notes, details, summary,
#: context) that can hold anything. Only lifecycle, ownership, scheduling and
#: routing fields are reported; every other field is withheld, value AND name.
_TASK_REPORTED_FIELDS = frozenset(
    {
        # lifecycle
        "status",
        "blocked_reason",
        "result",
        "phase",
        "attempt_count",
        "action_type",
        "confidence",
        # ownership
        "assigned_to",
        "owner",
        "executor",
        # what it is
        "title",
        "priority",
        "urgency",
        "domain",
        "area",
        "component",
        "parent_task_id",
        # scheduling and timestamps
        "due_date",
        "start_date",
        "created_at",
        "updated_at",
        "updated_date",
        "completed_at",
        "completed_date",
        # what it touches in the repos
        "repository",
        "repository_name",
        "repo",
        "issue_number",
        "pr_number",
        "run_id",
    }
)
_WITHHELD_FIELDS_GAP = (
    "only task lifecycle, ownership, scheduling and routing fields are reported; "
    "changes to any other task field (free text, payment, contact or email fields) are withheld"
)


def _lifecycle():
    """The task status vocabulary, from its one definition.

    Imported rather than copied: two copies of the terminal set are how
    finished tasks kept being re-dispatched (ateles#1038, #1039).
    """
    from lib.daemon_runtime import task_lifecycle

    return task_lifecycle


def _status_view(raw: Any) -> dict:
    tl = _lifecycle()
    raw_s = str(raw or "").strip()
    normalized = tl.normalize(raw_s) if raw_s else None
    known = {s.value for s in tl.TaskStatus}
    return {
        "status": raw_s or None,
        "status_normalized": normalized,
        # Live tasks carry `ready` and `in_progress`, which the lifecycle does
        # not declare. Say so rather than guess what they mean.
        "status_known": bool(normalized) and normalized in known,
        "terminal": tl.is_terminal(raw_s),
    }


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _TIMELINE_VALUE_CHARS:
        return value[: _TIMELINE_VALUE_CHARS - 1] + "…"
    return value


def _truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {"true", "1", "yes"}


def _observation_actor(obs: dict) -> str | None:
    """Who wrote an observation, as far as the record says."""
    fields = obs.get("fields") or {}
    sub = fields.get("agent_sub")
    if sub:
        return str(sub).split("@")[0]
    match = _IDEM_HANDLER_RE.match(str(obs.get("idempotency_key") or ""))
    if match:
        return match.group(1)
    prov = obs.get("provenance")
    if isinstance(prov, dict):
        if prov.get("agent_sub"):
            return str(prov["agent_sub"]).split("@")[0]
        if prov.get("client_name"):
            return f"client:{prov['client_name']}"
    if fields.get("handler"):
        return str(fields["handler"])
    return None


def _obs_query(body: dict) -> dict | None:
    return _post("/observations/query", body)


def _read_error(what: str) -> str:
    return f"{what}: {_describe_transport_error() or 'request failed'}"


def _observations_of(
    entity_id: str, *, since: str | None = None, max_rows: int = 1000
) -> tuple[list[dict] | None, str | None, bool]:
    """Observations of one entity. Returns (rows, error, truncated)."""
    rows: list[dict] = []
    offset = 0
    while True:
        body: dict[str, Any] = {
            "entity_id": entity_id,
            "limit": _ENTITY_OBS_PAGE,
            "offset": offset,
        }
        if since:
            body["created_since"] = since
        data = _obs_query(body)
        if data is None:
            return None, _read_error(f"observations of {entity_id}"), False
        batch = data.get("observations") or []
        rows.extend(batch)
        offset += len(batch)
        total = data.get("total")
        if (
            not batch
            or len(batch) < _ENTITY_OBS_PAGE
            or (isinstance(total, int) and offset >= total)
        ):
            return rows, None, False
        if len(rows) >= max_rows:
            return rows, None, True


def _obs_count(entity_type: str, since: str) -> int | None:
    data = _obs_query({"entity_type": entity_type, "created_since": since, "limit": 1})
    if data is None:
        return None
    total = data.get("total")
    return int(total) if isinstance(total, (int, float)) else None


def _scan_type_window(
    entity_type: str, start: datetime, end: datetime, max_scan: int
) -> dict:
    """Every observation of *entity_type* with start <= observed_at <= end.

    `/observations/query` has a lower time bound and no upper one, and returns
    newest first. So count what is newer than `end`, skip that many rows, and
    read forward from there to the start of the window. That reads the window
    and not the days of traffic after it.
    """
    start_iso = _iso(start)
    n_start = _obs_count(entity_type, start_iso)
    if n_start is None:
        return {"rows": [], "error": _read_error(f"{entity_type} count since {start_iso}")}
    n_end = _obs_count(entity_type, _iso(end))
    if n_end is None:
        return {"rows": [], "error": _read_error(f"{entity_type} count since {_iso(end)}")}
    offset = max(0, n_end - _SCAN_OFFSET_MARGIN)
    rows: list[dict] = []
    scanned = 0
    complete = False
    while scanned < max_scan:
        limit = min(_OBS_PAGE, max_scan - scanned)
        data = _obs_query(
            {
                "entity_type": entity_type,
                "created_since": start_iso,
                "offset": offset,
                "limit": limit,
            }
        )
        if data is None:
            return {
                "rows": rows,
                "error": _read_error(f"{entity_type} scan at offset {offset}"),
            }
        batch = data.get("observations") or []
        scanned += len(batch)
        offset += len(batch)
        for obs in batch:
            at = _parse_ts(obs.get("observed_at"))
            if at is not None and start <= at <= end:
                rows.append(obs)
        if len(batch) < limit:
            complete = True
            break
    return {
        "rows": rows,
        "error": None,
        "in_window": max(0, n_start - n_end),
        "scanned": scanned,
        "complete": complete,
    }


def _runner_entry(obs: dict) -> dict:
    fields = obs.get("fields") or {}
    tool = str(fields.get("tool_name") or "")
    provider, _, agent = tool.partition(":")
    agent = agent or (str(fields.get("agent_sub") or "").split("@")[0] or None)
    at = _parse_ts(fields.get("event_at")) or _parse_ts(obs.get("observed_at"))
    success = str(fields.get("success") or "").strip().lower()
    entry: dict[str, Any] = {
        "at": _iso(at) if at else obs.get("observed_at"),
        "agent": agent,
        "provider": fields.get("provider") or provider or None,
        "by": agent,
        "source": {
            "type": "harness_event",
            "entity_id": obs.get("entity_id"),
            "observation_id": obs.get("id"),
        },
    }
    if success == "partial":
        entry.update(kind="runner_started", value=tool or None)
    else:
        summary = fields.get("output_summary")
        if success == "true":
            outcome = "ok"
        elif "timeout" in str(summary or "").lower():
            outcome = "timeout"
        else:
            outcome = "failed"
        entry.update(
            kind="runner_ended",
            value=_clip(summary) if summary else f"success={success or 'unknown'}",
            outcome=outcome,
            duration_ms=fields.get("duration_ms"),
        )
    return entry


def _is_runner_event(obs: dict, task_ids: set[str]) -> bool:
    fields = obs.get("fields") or {}
    return (
        str(fields.get("event_type") or "") == "subprocess"
        and str(fields.get("task_entity_id") or "") in task_ids
    )


def _task_obs_entries(
    observations: list[dict], task_id: str, *, detect_create: bool = True
) -> list[dict]:
    """Timeline entries from a task's own observations."""
    ordered = sorted(observations, key=lambda o: str(o.get("observed_at") or ""))
    entries: list[dict] = []
    for index, obs in enumerate(ordered):
        fields = obs.get("fields") or {}
        base = {
            "at": obs.get("observed_at"),
            "by": _observation_actor(obs),
            "source": {
                "type": "task",
                "entity_id": task_id,
                "observation_id": obs.get("id"),
            },
        }
        is_create = detect_create and index == 0 and any(f in fields for f in _CREATE_MARKER_FIELDS)
        if is_create:
            entries.append({**base, "kind": "created", "value": fields.get("status")})
            continue
        for name, value in fields.items():
            if name not in _TASK_REPORTED_FIELDS:
                continue
            kind = _TASK_FIELD_KINDS.get(name, "field")
            entry = {**base, "kind": kind, "value": _clip(value)}
            if kind == "field":
                entry["field"] = name
            if kind == "reason" and value in ("", None):
                entry["value"] = "(cleared)"
            if kind == "status":
                entry.update(
                    status_normalized=_status_view(value)["status_normalized"],
                )
            entries.append(entry)
    return entries


def _checkpoint_entries(checkpoint_id: str, observations: list[dict]) -> list[dict]:
    entries: list[dict] = []
    for obs in sorted(observations, key=lambda o: str(o.get("observed_at") or "")):
        fields = obs.get("fields") or {}
        base = {
            "at": obs.get("observed_at"),
            "by": _observation_actor(obs),
            "checkpoint_id": checkpoint_id,
            "source": {
                "type": "checkpoint_brief",
                "entity_id": checkpoint_id,
                "observation_id": obs.get("id"),
            },
        }
        status = str(fields.get("status") or "").strip().lower()
        if "task_entity_id" in fields or "checkpoint_name" in fields:
            entries.append(
                {
                    **base,
                    "kind": "checkpoint_raised",
                    "value": _clip(
                        f"{fields.get('checkpoint_name') or 'checkpoint'}: "
                        f"{fields.get('reason') or ''}".strip()
                    ),
                    "blast_radius": fields.get("blast_radius"),
                }
            )
        elif status in ("approved", "rejected"):
            entries.append({**base, "kind": "checkpoint_resolved", "value": status})
        elif status:
            entries.append({**base, "kind": "checkpoint_status", "value": status})
        if "resolved_dispatched" in fields and _truthy(fields.get("resolved_dispatched")):
            entries.append({**base, "kind": "checkpoint_released", "value": "resolved_dispatched"})
    return entries


def _subject_ref(snap: dict) -> str | None:
    repo = str(snap.get("repo") or snap.get("repository") or "").strip()
    number = None
    for key in ("pr_number", "issue_number", "github_number", "number"):
        if snap.get(key) not in (None, ""):
            number = snap.get(key)
            break
    if repo and "/" in repo and number is not None and str(number).isdigit():
        return f"{repo}#{number}"
    return None


def _history_time(entry: dict) -> str | None:
    for key in ("at", "timestamp", "ts", "changed_at", "time"):
        if entry.get(key):
            return str(entry[key])
    return None


class _TimelineBuilder:
    """Accumulates one task's timeline, source by source.

    Each `join_*` method reads one record, appends its entries to `timeline`,
    and records under `sources` whether it joined, found nothing, could not be
    joined, or failed — so the response can never present a failed read as an
    absence.
    """

    def __init__(self, task_id: str, task: dict, window_hours: float | None):
        self.task_id = task_id
        self.task = task
        self.snap = _snapshot_of(task)
        self.status = _status_view(self.snap.get("status"))
        self.now = datetime.now(timezone.utc)
        hours = float(window_hours) if window_hours else TIMELINE_WINDOW_HOURS
        self.hours = max(1.0, min(hours, 24 * 14))
        self.timeline: list[dict] = []
        self.sources: list[dict] = []
        self.gaps: list[str] = []
        self.related: list[dict] = []
        self.refs: dict[str, dict] = {}
        self.refs_known = True
        self.rel_checkpoint_ids: set[str] = set()
        self.checkpoints: list[dict] = []
        self.runner_entries: list[dict] = []
        self.github_rows: list[dict] = []
        self.steps: list[dict] = []
        self.window: dict[str, Any] = {}

    def source(self, name: str, status: str, count: int | None = None, detail: str | None = None) -> None:
        row: dict[str, Any] = {"source": name, "status": status}
        if count is not None:
            row["count"] = count
        if detail:
            row["detail"] = detail
        self.sources.append(row)

    @property
    def subject_refs(self) -> set[str]:
        refs = {r["ref"] for r in self.refs.values() if r.get("ref")}
        own = _subject_ref(self.snap)
        if own:
            refs.add(own)
        return refs

    def join_task_observations(self, observations: list[dict], truncated: bool) -> None:
        self.timeline.extend(_task_obs_entries(observations, self.task_id))
        self.source("task observations", "joined", len(observations))
        if truncated:
            self.gaps.append("the task has more observations than were read; the oldest are missing")

    def join_relationships(self) -> None:
        rels = _get(f"/entities/{self.task_id}/relationships?expand_entities=true")
        if rels is None:
            self.refs_known = False
            self.source("relationships", "error", detail=_read_error("relationships"))
            self.gaps.append("relationships could not be read, so issue and PR refs are unknown")
            return
        related_entities = rels.get("related_entities") or {}
        for rel in rels.get("relationships") or []:
            outgoing = rel.get("source_entity_id") == self.task_id
            other = rel.get("target_entity_id") if outgoing else rel.get("source_entity_id")
            other_ent = related_entities.get(other) or {}
            other_type = (
                rel.get("target_entity_type") if outgoing else rel.get("source_entity_type")
            ) or other_ent.get("entity_type")
            self.related.append(
                {
                    "relationship": rel.get("relationship_type"),
                    "direction": "outgoing" if outgoing else "incoming",
                    "entity_id": other,
                    "entity_type": other_type,
                }
            )
            if other_type == "checkpoint_brief":
                self.rel_checkpoint_ids.add(other)
            if other_type in ("issue", "pull_request"):
                osnap = _snapshot_of(other_ent) if other_ent else {}
                self.refs[other] = {
                    "entity_id": other,
                    "kind": other_type,
                    "ref": _subject_ref(osnap),
                    "snapshot": osnap,
                }
        self.source("relationships", "joined", len(self.related))

    def join_checkpoints(self) -> None:
        page = _retrieve_page(
            "checkpoint_brief",
            snapshot_filters={"task_entity_id": {"op": "eq", "value": self.task_id}},
            limit=50,
        )
        if page is None:
            self.source("checkpoint_brief", "error", detail=_read_error("checkpoint_brief"))
            self.gaps.append("checkpoints could not be read; an open checkpoint may be holding this task")
            return
        entities = list(page.get("entities") or [])
        listed = {e.get("entity_id") for e in entities}
        # A checkpoint linked by edge but missing its task_entity_id field.
        for cid in sorted(self.rel_checkpoint_ids - listed):
            ent = _get(f"/entities/{cid}")
            if ent is not None:
                entities.append(ent)
        errors = 0
        for ent in entities:
            cid = ent.get("entity_id", ent.get("id", ""))
            csnap = _snapshot_of(ent)
            cobs, cerr, _ = _observations_of(cid, max_rows=200)
            if cobs is None:
                errors += 1
                self.gaps.append(f"checkpoint {cid} history could not be read ({cerr})")
                cobs = []
            entries = _checkpoint_entries(cid, cobs)
            self.timeline.extend(entries)
            self.checkpoints.append(
                {
                    "checkpoint_id": cid,
                    "status": csnap.get("status"),
                    "resolved_dispatched": _truthy(csnap.get("resolved_dispatched")),
                    "blast_radius": csnap.get("blast_radius"),
                    "reason": _clip(csnap.get("reason")),
                    "raised_at": next(
                        (e["at"] for e in entries if e["kind"] == "checkpoint_raised"), None
                    ),
                }
            )
        status = "error" if errors else ("joined" if self.checkpoints else "none_found")
        self.source("checkpoint_brief", status, len(self.checkpoints))

    def _scan_bounds(self) -> tuple[datetime, datetime] | None:
        created = _parse_ts(self.task.get("created_at")) or (
            _parse_ts(self.timeline[0]["at"]) if self.timeline else None
        )
        if created is None:
            return None
        last = _parse_ts(self.task.get("last_observation_at"))
        start = created - timedelta(seconds=_WINDOW_LEAD_SECONDS)
        # A task that is still moving may have a runner that started after its
        # last status write, so scan up to now. A settled task's runner events
        # precede its last write by seconds.
        settled = self.status["terminal"] or self.status["status_normalized"] in ("failed", "blocked")
        end = (
            min(self.now, (last or self.now) + timedelta(seconds=_WINDOW_TAIL_SECONDS))
            if settled
            else self.now
        )
        if end - start > timedelta(hours=self.hours):
            start = end - timedelta(hours=self.hours)
            self.gaps.append(
                f"runner events were looked for only from {_iso(start)} "
                f"(the last {self.hours:g} h of the task's history); earlier ones were not scanned"
            )
        return start, end

    def join_harness_events(self) -> None:
        bounds = self._scan_bounds()
        if bounds is None:
            self.source("harness_event", "not_joinable", detail="task has no timestamps")
            return
        start, end = bounds
        scan = _scan_type_window("harness_event", start, end, TIMELINE_MAX_SCAN)
        self.window = {
            "start": _iso(start),
            "end": _iso(end),
            "harness_event_observations_in_window": scan.get("in_window"),
            "scanned": scan.get("scanned"),
            "complete": bool(scan.get("complete")) and not scan.get("error"),
        }
        if scan.get("error"):
            self.source("harness_event", "error", detail=scan["error"])
            self.gaps.append("runner events could not be read; who ran this task and how it ended is unknown")
            return
        if not scan.get("complete"):
            self.gaps.append(
                f"the window holds {scan.get('in_window')} harness_event observations and "
                f"only the newest {scan.get('scanned')} were scanned; older runner events "
                "in the window were not looked at"
            )
        refs = self.subject_refs
        runner_rows = []
        for obs in scan["rows"]:
            if _is_runner_event(obs, {self.task_id}):
                runner_rows.append(obs)
            elif refs and str((obs.get("fields") or {}).get("subject_ref") or "") in refs:
                self.github_rows.append(obs)
        self.runner_entries = [_runner_entry(o) for o in runner_rows]
        self.timeline.extend(self.runner_entries)
        self.source(
            "harness_event runner events",
            "joined" if runner_rows else "none_found",
            len(runner_rows),
            "matched by task_entity_id within the scanned window only",
        )

    def join_github_events(self) -> None:
        refs = self.subject_refs
        if not refs:
            if not self.refs_known:
                self.source("PR and review events", "error", detail="relationships unreadable")
                return
            self.source(
                "PR and review events",
                "not_joinable",
                detail="the task is related to no issue or pull_request entity and names no repo#number",
            )
            self.gaps.append(
                "no issue or pull request is linked to this task, so PR, review and issue-gate "
                "events could not be joined"
            )
            return
        scan_failed = any(
            row["source"] == "harness_event" and row["status"] in ("error", "not_joinable")
            for row in self.sources
        )
        if scan_failed or not self.window:
            self.source("PR and review events", "error", detail="the harness_event scan did not run")
            return
        for obs in self.github_rows:
            fields = obs.get("fields") or {}
            event_type = str(fields.get("event_type") or "")
            self.timeline.append(
                {
                    "at": fields.get("occurred_at") or obs.get("observed_at"),
                    "kind": "review" if "review" in event_type else "github_event",
                    "value": _clip(f"{event_type}: {fields.get('summary') or ''}".strip()),
                    "subject_ref": fields.get("subject_ref"),
                    "by": _observation_actor(obs),
                    "source": {
                        "type": "harness_event",
                        "entity_id": obs.get("entity_id"),
                        "observation_id": obs.get("id"),
                    },
                }
            )
        self.source(
            "PR and review events",
            "joined" if self.github_rows else "none_found",
            len(self.github_rows),
            f"harness_event rows whose subject_ref is one of {sorted(refs)}",
        )

    def join_participation(self) -> None:
        page = _retrieve_page(
            "participation_record",
            snapshot_filters={"work_entity_id": {"op": "eq", "value": self.task_id}},
            limit=100,
        )
        if page is None:
            self.source("participation_record", "error", detail=_read_error("participation_record"))
            return
        rows = page.get("entities") or []
        for ent in rows:
            psnap = _snapshot_of(ent)
            gate = psnap.get("gate_name")
            self.steps.append(
                {
                    "gate": gate,
                    "status": psnap.get("status"),
                    "agent": psnap.get("agent"),
                    "source": "participation_record",
                    "entity_id": ent.get("entity_id"),
                }
            )
            for key, kind in (
                ("dispatched_at", "step_opened"),
                ("satisfied_at", "step_closed"),
                ("skipped_at", "step_closed"),
            ):
                if psnap.get(key):
                    self.timeline.append(
                        {
                            "at": psnap[key],
                            "kind": kind,
                            "value": f"{gate}: {key.removesuffix('_at')}",
                            "by": psnap.get("agent"),
                            "source": {"type": "participation_record", "entity_id": ent.get("entity_id")},
                        }
                    )
        self.source("participation_record", "joined" if rows else "none_found", len(rows))

    def join_gate_status(self) -> None:
        if not self.refs:
            return
        joined = 0
        for ref in self.refs.values():
            rsnap = ref.get("snapshot") or {}
            gate_status = rsnap.get("gate_status")
            if isinstance(gate_status, str):
                try:
                    gate_status = json.loads(gate_status)
                except (ValueError, TypeError):
                    gate_status = None
            if not isinstance(gate_status, dict):
                continue
            joined += 1
            self.steps.append(
                {
                    "subject": ref.get("ref") or ref["entity_id"],
                    "gate_status": gate_status,
                    "blocking_gates": _blocking_gates(gate_status),
                    "current_owner": rsnap.get("current_owner"),
                    "source": f"{ref['kind']}.gate_status",
                    "entity_id": ref["entity_id"],
                }
            )
            for hist in _dedupe_history(_parse_owner_history(rsnap.get("owner_history"))):
                at = _history_time(hist)
                if at:
                    self.timeline.append(
                        {
                            "at": at,
                            "kind": "step",
                            "value": _clip(json.dumps(hist, sort_keys=True, default=str)),
                            "subject_ref": ref.get("ref"),
                            "source": {"type": f"{ref['kind']}.owner_history", "entity_id": ref["entity_id"]},
                        }
                    )
        self.source("issue / pull_request gate_status", "joined" if joined else "none_found", joined)

    def join_escalations(self) -> None:
        page = _retrieve_page(
            "escalation",
            snapshot_filters={"source_entity_id": {"op": "eq", "value": self.task_id}},
            limit=50,
        )
        if page is None:
            self.source("escalation", "error", detail=_read_error("escalation"))
            return
        rows = page.get("entities") or []
        for ent in rows:
            esnap = _snapshot_of(ent)
            self.timeline.append(
                {
                    "at": ent.get("created_at") or ent.get("last_observation_at"),
                    "kind": "escalation",
                    "value": _clip(esnap.get("title")),
                    "by": str(esnap.get("source_agent") or "").split("@")[0] or None,
                    "source": {"type": "escalation", "entity_id": ent.get("entity_id")},
                }
            )
        self.source("escalation", "joined" if rows else "none_found", len(rows))

    def current(self) -> dict:
        """What is happening now, derived from the joined records."""
        starts = [e for e in self.runner_entries if e["kind"] == "runner_started"]
        ends = [e for e in self.runner_entries if e["kind"] == "runner_ended"]
        last_start = starts[-1] if starts else None
        last_end = ends[-1] if ends else None
        runner_open = bool(
            last_start and (last_end is None or str(last_end["at"]) < str(last_start["at"]))
        )
        open_cp = next(
            (c for c in self.checkpoints if str(c.get("status") or "").lower() == "awaiting_operator"),
            None,
        )
        norm = self.status["status_normalized"]
        current: dict[str, Any] = {"phase": "unknown", "agent": None, "since": None, "live": False}
        if open_cp:
            current.update(
                phase="held_at_checkpoint",
                since=open_cp.get("raised_at"),
                open_checkpoint={
                    "id": open_cp["checkpoint_id"],
                    "reason": open_cp.get("reason"),
                    "blast_radius": open_cp.get("blast_radius"),
                },
            )
        elif runner_open and last_start:
            started = _parse_ts(last_start["at"])
            live = bool(started and (self.now - started).total_seconds() <= RUNNER_LIVE_SECONDS)
            current.update(phase="executing", agent=last_start.get("agent"), since=last_start["at"], live=live)
            if not live:
                self.gaps.append(
                    "a runner start has no matching end and is older than the runner timeout; "
                    "its end event was probably lost, so whether it is still running is unknown"
                )
        elif self.status["terminal"] or norm in ("failed", "blocked", "declined"):
            current.update(
                phase="ended",
                agent=(last_end or last_start or {}).get("agent"),
                since=(last_end or {}).get("at") or self.task.get("last_observation_at"),
            )
        elif norm in ("pending", "routed", "ready"):
            current.update(phase="waiting_to_claim")
        elif norm == "awaiting_approval":
            current.update(phase="held_at_checkpoint")
            self.gaps.append("the task says awaiting_approval but no open checkpoint was found for it")
        elif norm == "executing":
            current.update(phase="executing")
            self.gaps.append(
                "the task says executing but no runner start was found in the scanned window; "
                "stored status alone does not show that anything is running"
            )
        if not self.status["status_known"]:
            self.gaps.append(
                f"status {self.status['status']!r} is not a value the task lifecycle declares; "
                "reported as stored, not interpreted"
            )
        return current

    def result(self) -> dict:
        self.timeline.sort(
            key=lambda e: (_parse_ts(e.get("at")) or self.now, e.get("kind") != "created")
        )
        current = self.current()
        last_end = next(
            (e for e in reversed(self.runner_entries) if e["kind"] == "runner_ended"), None
        )
        self.gaps.extend(
            [
                "runner events live on shared per-agent harness_event entities and are found only "
                "inside the scanned window; a start and its end share no id and are paired by agent and time",
                "the runner's own output is a host-local file and is not in the record; a timed-out run keeps none",
                _WITHHELD_FIELDS_GAP,
            ]
        )
        return {
            "task": {
                "id": self.task_id,
                "title": self.snap.get("title"),
                **self.status,
                "assigned_to": self.snap.get("assigned_to") or None,
                "priority": self.snap.get("priority"),
                "created_at": self.task.get("created_at"),
                "last_observation_at": self.task.get("last_observation_at"),
            },
            "current": current,
            "timeline": self.timeline,
            "checkpoints": self.checkpoints,
            "steps": self.steps,
            "related": self.related,
            "output": {
                "result": _clip(self.snap.get("result")) or None,
                "blocked_reason": _clip(self.snap.get("blocked_reason")) or None,
                "runner_outcome": (last_end or {}).get("value"),
                "runner_output": None,
            },
            "window": self.window,
            "sources": self.sources,
            "gaps": self.gaps,
        }


def _not_a_task(entity_id: str, entity: dict) -> str | None:
    """Refusal text when *entity* is not a task, else None.

    Both tools read only tasks. Fails closed: an entity whose type the record
    does not state is refused too, since it cannot be shown to be a task —
    without this, either tool is a read-any-entity primitive.
    """
    etype = _entity_type_of(entity)
    if etype == "task":
        return None
    return f"entity {entity_id} is {('a ' + etype) if etype else 'of no stated type'}, not a task"


def _get_task_timeline(task_entity_id: str, window_hours: float | None = None) -> dict:
    task_id = str(task_entity_id or "").strip()
    if not _TASK_ID_RE.match(task_id):
        return {"error": f"task_entity_id must be an 'ent_…' id, got {task_entity_id!r}"}

    task = _get(f"/entities/{task_id}")
    if task is None:
        return {
            "error": f"task {task_id} not found or Neotoma unreachable",
            "transport_error": _describe_transport_error(),
        }
    refusal = _not_a_task(task_id, task)
    if refusal:
        return {"error": refusal}

    task_obs, err, truncated = _observations_of(task_id)
    if task_obs is None:
        # The task's own history is the spine of the timeline. Without it,
        # anything returned would be a partial picture presented as a whole.
        return {"error": f"could not read the task's observations: {err}"}

    builder = _TimelineBuilder(task_id, task, window_hours)
    builder.join_task_observations(task_obs, truncated)
    builder.join_relationships()
    builder.join_checkpoints()
    builder.join_harness_events()
    builder.join_github_events()
    builder.join_participation()
    builder.join_gate_status()
    builder.join_escalations()
    return builder.result()


# ── watch_swarm ──────────────────────────────────────────────────────────────


def _encode_cursor(since: str, seen: set[str] | list[str]) -> str:
    payload = json.dumps({"t": since, "s": sorted(seen)}, separators=(",", ":"))
    return _CURSOR_PREFIX + base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[str, set[str]] | None:
    raw = str(cursor or "").strip()
    if not raw.startswith(_CURSOR_PREFIX):
        return None
    body = raw[len(_CURSOR_PREFIX):]
    try:
        decoded = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    except (ValueError, TypeError):
        return None
    if not isinstance(decoded, dict):
        return None
    since = decoded.get("t")
    seen = decoded.get("s") or []
    if not isinstance(since, str) or _parse_ts(since) is None or not isinstance(seen, list):
        return None
    return since, {str(s) for s in seen}


def _advance_cursor(since: str, seen: set[str], rows: list[dict]) -> str:
    """New cursor over every row read this poll, matched or not.

    Advancing over unmatched rows keeps each later poll short; the overlap and
    the seen-id set keep a row that landed mid-poll from being skipped.
    """
    stamped = [(_parse_ts(r.get("observed_at")), str(r.get("id") or "")) for r in rows]
    stamped = [(t, i) for t, i in stamped if t is not None and i]
    old = _parse_ts(since)
    if not stamped or old is None:
        return _encode_cursor(since, seen)
    high = max(t for t, _ in stamped)
    new_since = max(old, high - timedelta(seconds=_CURSOR_OVERLAP_SECONDS))
    new_seen = {i for t, i in stamped if t >= new_since}
    if new_since == old:
        new_seen |= seen
    if len(new_seen) > _CURSOR_MAX_SEEN:
        new_since = high
        new_seen = {i for t, i in stamped if t >= high}
    return _encode_cursor(_iso(new_since), new_seen)


def _scan_since(entity_type: str, since: str, max_scan: int) -> tuple[list[dict] | None, str | None, bool]:
    rows: list[dict] = []
    offset = 0
    while offset < max_scan:
        limit = min(_OBS_PAGE, max_scan - offset)
        data = _obs_query(
            {"entity_type": entity_type, "created_since": since, "offset": offset, "limit": limit}
        )
        if data is None:
            return None, _read_error(f"{entity_type} since {since}"), False
        batch = data.get("observations") or []
        rows.extend(batch)
        offset += len(batch)
        if len(batch) < limit:
            return rows, None, False
    return rows, None, True


def _watch_poll(
    since: str, seen: set[str], task_ids: list[str], include_checkpoints: bool
) -> dict:
    """One pass over every stream. Returns changes, the rows read, and errors."""
    all_rows: list[dict] = []
    changes: list[dict] = []
    checkpoint_changes: list[dict] = []
    errors: list[str] = []
    gaps: list[str] = []
    watched = set(task_ids)

    if task_ids:
        gaps.append(_WITHHELD_FIELDS_GAP)
    for tid in task_ids:
        obs, err, truncated = _observations_of(tid, since=since, max_rows=500)
        if obs is None:
            errors.append(err or f"observations of {tid}")
            continue
        all_rows.extend(obs)
        fresh = [o for o in obs if str(o.get("id")) not in seen]
        for entry in _task_obs_entries(fresh, tid, detect_create=False):
            entry["subject"] = {"task": tid}
            changes.append(entry)
        if truncated:
            gaps.append(f"more changes on {tid} than one poll reads; the oldest were skipped")

    if task_ids:
        rows, err, truncated = _scan_since("harness_event", since, TIMELINE_MAX_SCAN)
        if rows is None:
            errors.append(err or "harness_event")
        else:
            all_rows.extend(rows)
            for obs in rows:
                if str(obs.get("id")) in seen or not _is_runner_event(obs, watched):
                    continue
                entry = _runner_entry(obs)
                entry["subject"] = {"task": str((obs.get("fields") or {}).get("task_entity_id"))}
                changes.append(entry)
            if truncated:
                gaps.append(
                    "more runner events since the cursor than one poll scans; older ones were skipped"
                )

    if include_checkpoints or task_ids:
        rows, err, truncated = _scan_since("checkpoint_brief", since, 1000)
        if rows is None:
            errors.append(err or "checkpoint_brief")
        else:
            all_rows.extend(rows)
            entity_cache: dict[str, dict | None] = {}
            for obs in rows:
                if str(obs.get("id")) in seen:
                    continue
                cid = str(obs.get("entity_id") or "")
                fields = obs.get("fields") or {}
                task_ref = fields.get("task_entity_id")
                raised_at = obs.get("observed_at") if "task_entity_id" in fields else None
                if task_ref is None or raised_at is None:
                    if cid not in entity_cache:
                        entity_cache[cid] = _get(f"/entities/{cid}")
                    ent = entity_cache[cid]
                    if ent is None:
                        errors.append(_read_error(f"checkpoint {cid}"))
                        continue
                    task_ref = task_ref or _snapshot_of(ent).get("task_entity_id")
                    raised_at = raised_at or ent.get("created_at")
                    if not raised_at:
                        gaps.append(f"checkpoint {cid} has no creation time; ordered last")
                if not include_checkpoints and str(task_ref) not in watched:
                    continue
                for entry in _checkpoint_entries(cid, [obs]):
                    entry["subject"] = {"checkpoint": cid, "task": task_ref}
                    entry["raised_at"] = raised_at
                    checkpoint_changes.append(entry)
            if truncated:
                gaps.append("more checkpoint changes since the cursor than one poll reads")

    changes.sort(key=lambda e: str(e.get("at") or ""))
    # Raised time first, never entity id: ids are random, so id order is noise.
    checkpoint_changes.sort(
        key=lambda e: (e.get("raised_at") is None, str(e.get("raised_at") or ""), str(e.get("at") or ""))
    )
    terminal = sorted(
        {
            e["subject"]["task"]
            for e in changes
            if e.get("kind") == "status" and _status_view(e.get("value"))["terminal"]
        }
    )
    return {
        "changes": changes,
        "checkpoints": checkpoint_changes,
        "terminal_subjects": terminal,
        "rows": all_rows,
        "errors": errors,
        "gaps": gaps,
    }


def _watch_baseline(task_ids: list[str], include_checkpoints: bool) -> dict:
    """First call: current state and a cursor at the record's newest write.

    The cursor is taken from the newest observation Neotoma holds, so it is on
    the record's own clock rather than this host's.
    """
    newest = _obs_query({"limit": 1})
    if newest is None:
        return {"error": _read_error("could not read the record's newest observation")}
    rows = newest.get("observations") or []
    if rows:
        cursor = _encode_cursor(str(rows[0].get("observed_at")), {str(rows[0].get("id"))})
    else:
        cursor = _encode_cursor(_iso(datetime.now(timezone.utc)), set())

    tasks: list[dict] = []
    pending: list[dict] = []
    errors: list[str] = []
    for tid in task_ids:
        ent = _get(f"/entities/{tid}")
        if ent is None:
            errors.append(_read_error(f"task {tid}"))
            continue
        refusal = _not_a_task(tid, ent)
        if refusal:
            return {"error": refusal, "refused": True}
        snap = _snapshot_of(ent)
        tasks.append(
            {
                "id": tid,
                "title": snap.get("title"),
                **_status_view(snap.get("status")),
                "last_observation_at": ent.get("last_observation_at"),
            }
        )
        page = _retrieve_page(
            "checkpoint_brief",
            snapshot_filters={
                "task_entity_id": {"op": "eq", "value": tid},
                "status": {"op": "eq", "value": "awaiting_operator"},
            },
            limit=20,
        )
        if page is None:
            errors.append(_read_error(f"checkpoints of {tid}"))
            continue
        for cp in page.get("entities") or []:
            cid = cp.get("entity_id", "")
            full = _get(f"/entities/{cid}") or cp
            csnap = _snapshot_of(full)
            pending.append(
                {
                    "checkpoint_id": cid,
                    "task": tid,
                    "raised_at": full.get("created_at"),
                    "blast_radius": csnap.get("blast_radius"),
                    "reason": _clip(csnap.get("reason")),
                }
            )
    if errors:
        return {"error": "; ".join(errors)}
    pending.sort(key=lambda c: (c.get("raised_at") is None, str(c.get("raised_at") or "")))
    result: dict[str, Any] = {
        "baseline": True,
        "tasks": tasks,
        "pending_checkpoints": pending,
        "pending_checkpoints_order": "raised_at ascending",
        "changes": [],
        "checkpoints": [],
        "terminal_subjects": sorted(t["id"] for t in tasks if t["terminal"]),
        "cursor": cursor,
        "gaps": [_WITHHELD_FIELDS_GAP] if tasks else [],
    }
    if include_checkpoints:
        total_page = _retrieve_page(
            "checkpoint_brief",
            snapshot_filters={"status": {"op": "eq", "value": "awaiting_operator"}},
            limit=1,
            include_snapshots=False,
        )
        result["pending_checkpoints_total"] = (total_page or {}).get("total")
        result["note"] = (
            "Every checkpoint raised or resolved after this cursor is reported by the next "
            "call, ordered by when it was raised. The existing queue is list_checkpoints'."
        )
    return result


def _watch_swarm(
    cursor: str | None = None,
    task_ids: list[str] | None = None,
    include_checkpoints: bool = False,
    wait_seconds: float = 0,
) -> dict:
    ids = [str(t).strip() for t in (task_ids or []) if str(t).strip()]
    bad = [t for t in ids if not _TASK_ID_RE.match(t)]
    if bad:
        return {"error": f"task_ids must be 'ent_…' ids, got {bad}"}
    ids = list(dict.fromkeys(ids))
    if len(ids) > WATCH_MAX_TASKS:
        return {"error": f"watch at most {WATCH_MAX_TASKS} tasks per call, got {len(ids)}"}
    if not ids and not include_checkpoints:
        return {"error": "nothing to watch: pass task_ids, include_checkpoints, or both"}
    try:
        wait = float(wait_seconds or 0)
    except (TypeError, ValueError):
        return {"error": f"wait_seconds must be a number, got {wait_seconds!r}"}
    wait = max(0.0, min(wait, float(WATCH_MAX_WAIT_SECONDS)))

    if not cursor:
        return _watch_baseline(ids, include_checkpoints)
    decoded = _decode_cursor(cursor)
    if decoded is None:
        return {"error": "cursor is not one this server issued; call without a cursor to start"}
    since, seen = decoded
    since_ts = _parse_ts(since)
    max_age = timedelta(hours=WATCH_MAX_CURSOR_AGE_HOURS)
    if since_ts is None or _watch_now() - since_ts > max_age:
        return {
            "error": (
                f"cursor is from {since}, older than the {WATCH_MAX_CURSOR_AGE_HOURS:g} h a watch "
                "may resume from; call without a cursor to start a fresh watch"
            ),
            "cursor_expired": True,
            "refused": True,
        }

    # Re-check on every resumed call, not only at the baseline: a cursor call
    # names its task ids afresh, and any of them may not be a task.
    type_errors: list[str] = []
    for tid in ids:
        ent = _get(f"/entities/{tid}")
        if ent is None:
            type_errors.append(_read_error(f"task {tid}"))
            continue
        refusal = _not_a_task(tid, ent)
        if refusal:
            return {"error": refusal, "refused": True}
    if type_errors:
        return {
            "error": "could not read the record for this watch",
            "detail": type_errors,
            "cursor": cursor,
        }

    started = _watch_clock()
    deadline = started + wait
    polls = 0
    while True:
        poll = _watch_poll(since, seen, ids, include_checkpoints)
        polls += 1
        if poll["errors"]:
            # Fail closed, and keep the caller's cursor: nothing was consumed,
            # so a retry sees everything this call could not.
            return {
                "error": "could not read the record for this watch",
                "detail": poll["errors"],
                "cursor": cursor,
            }
        new_cursor = _advance_cursor(since, seen, poll["rows"])
        if poll["changes"] or poll["checkpoints"]:
            return {
                "changes": poll["changes"],
                "checkpoints": poll["checkpoints"],
                "checkpoints_order": "raised_at ascending",
                "terminal_subjects": poll["terminal_subjects"],
                "cursor": new_cursor,
                "polls": polls,
                "waited_seconds": round(_watch_clock() - started, 1),
                "gaps": poll["gaps"],
            }
        cursor = new_cursor
        since, seen = _decode_cursor(new_cursor) or (since, seen)
        remaining = deadline - _watch_clock()
        if remaining <= 0:
            return {
                "changes": [],
                "checkpoints": [],
                "terminal_subjects": [],
                "cursor": new_cursor,
                "polls": polls,
                "waited_seconds": round(_watch_clock() - started, 1),
                "timed_out": True,
                "gaps": poll["gaps"],
            }
        _watch_sleep(min(WATCH_POLL_SECONDS, remaining))


# ── MCP Server setup ─────────────────────────────────────────────────────────

TOOLS = [
    Tool(
        name="get_swarm_roster",
        description=(
            "Returns the full Ateles swarm roster: a map of roles to agent names, "
            "plus the swarm domain and roster entity ID. Use this to discover which "
            "agents exist and what roles they fill."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="route_task",
        description=(
            "Given a task description, resolves the owning agent from the swarm "
            "roster by role, fetches its agent_definition (prompt, context types, "
            "tool allowlist), and the applicable execution_policy. Returns the "
            "complete dispatch context in one call. Optionally pass action_type to "
            "get the blast radius classification for that action."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_description": {
                    "type": "string",
                    "description": "What the task is about — used for keyword-based role matching.",
                },
                "action_type": {
                    "type": "string",
                    "description": (
                        "Optional action type (e.g. 'git_push', 'payment', 'publish') "
                        "to classify blast radius under the resolved policy."
                    ),
                },
            },
            "required": ["task_description"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="list_checkpoints",
        description=(
            "Returns a PAGE of pending checkpoint_briefs (status: awaiting_operator) "
            "with task title, assigned agent, blast radius, confidence vs threshold, "
            "and reason pre-joined. These are the operator's decision queue. "
            "`total` is the queue's real depth and `returned` is how many this page "
            "carries; when `truncated` is true, pass `next_cursor` back as `cursor` "
            "to reach the rest. Never treat one page as the whole queue — it used to "
            "say it returned 'all' while showing 50 of 372 (ateles#1037)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "cursor": {
                    "type": "string",
                    "description": "Opaque cursor from a previous response's `next_cursor`.",
                },
                "limit": {
                    "type": "integer",
                    "description": f"Briefs per page (default {CHECKPOINT_PAGE_SIZE}).",
                    "minimum": 1,
                    "maximum": 500,
                },
            },
            "additionalProperties": False,
        },
    ),
    Tool(
        name="resolve_checkpoint",
        description=(
            "Approves or rejects a pending checkpoint_brief by entity ID. Validates "
            "that the checkpoint is awaiting_operator and has not already been "
            "dispatched. The caller must supply body-bound RFC 9421 AAuth headers "
            "signed by the subject and key thumbprint pinned in the checkpoint "
            "authority. The service forwards that proof without loading the "
            "resolver's key, and acts only when immutable attribution reads back "
            "as that exact identity. "
            "operator_only approvals "
            "record the decision without dispatching an agent. On rejection, also marks the "
            "referenced task as declined."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "checkpoint_id": {
                    "type": "string",
                    "description": "Entity ID of the checkpoint_brief to resolve.",
                },
                "action": {
                    "type": "string",
                    "enum": ["approve", "reject"],
                    "description": "Whether to approve or reject the checkpoint.",
                },
                "resolver_aauth_headers": {
                    "type": "object",
                    "description": (
                        "RFC 9421 headers from HttpSigSigner.sign_headers for the "
                        "canonical POST /correct body described in the runbook."
                    ),
                    "properties": {
                        name: {"type": "string"}
                        for name in sorted(_RESOLVER_SIGNATURE_HEADERS)
                    },
                    "required": sorted(_RESOLVER_SIGNATURE_HEADERS),
                    "additionalProperties": False,
                },
            },
            "required": ["checkpoint_id", "action", "resolver_aauth_headers"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="get_gate_status",
        description=(
            "Read-only. For an issue (an 'owner/repo#123' ref or an 'ent_...' entity id), "
            "return its gate_status, current_owner, which gates are still blocking, the "
            "most recent owner_history entries, and whether a pipeline is currently "
            "queued or inflight for it. Answers 'is the swarm going to continue this, "
            "and what is it waiting on?' in one call instead of a manual entity read. "
            "This tool never writes gate state: a session must not sign off its own gates."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "issue_ref": {
                    "type": "string",
                    "description": "'owner/repo#123' or an 'ent_...' issue entity id",
                },
                "history_limit": {
                    "type": "integer",
                    "description": "How many recent owner_history entries to return (default 5).",
                    "default": 5,
                },
            },
            "required": ["issue_ref"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="list_pipeline_queue",
        description=(
            "Read-only. Report what currently holds the issue-pipeline slot, what is "
            "queued behind it, and how long each has waited. This is otherwise only "
            "discoverable by grepping the apis daemon log. Use it when work seems "
            "stalled but no gate explains why — a queued pipeline is waiting on the "
            "serialized slot, not on a reviewer."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="get_dispatch_health",
        description=(
            "Read-only. Report whether the apis dispatcher daemon is loaded and running, "
            "its recent issue-pipeline log activity, how many dispatch failures have "
            "been logged recently, and whether a label gate (ATELES_SWARM_REQUIRE_LABEL, "
            "bootstrap-mode canary lane) is currently restricting the automatic issue/PR "
            "pipelines to a single label. Use it to tell 'the swarm is working on it' apart "
            "from 'nothing is running at all', and 'nothing is being reviewed because the "
            "label gate is active by design' apart from a broken pipeline."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="get_task_timeline",
        description=(
            "Read-only. One swarm task's history, merged and time-ordered from what the "
            "swarm records: the task's own status/reason/result observations, its "
            "checkpoints (raised, resolved, released), runner start/end events found in "
            "shared harness_event rows within a bounded window around the task, PR and "
            "review events for a linked issue or PR, workflow-step rows, and escalations. "
            "Every entry carries its source and timestamp. `current` derives what is "
            "happening now; `sources` says which records were joined and `gaps` what the "
            "record could not show. A failed read of the task returns an error, never an "
            "empty timeline."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_entity_id": {
                    "type": "string",
                    "description": "The Neotoma task entity id (ent_...).",
                },
                "window_hours": {
                    "type": "number",
                    "description": (
                        "Most hours of the task's history to scan for runner events "
                        f"(default {TIMELINE_WINDOW_HOURS:g})."
                    ),
                    "minimum": 1,
                    "maximum": 336,
                },
            },
            "required": ["task_entity_id"],
            "additionalProperties": False,
        },
    ),
    Tool(
        name="watch_swarm",
        description=(
            "Read-only bounded long-poll. Call once without `cursor` to get the watched "
            "tasks' current state and a cursor; then call with that cursor and it waits up "
            f"to `wait_seconds` (max {WATCH_MAX_WAIT_SECONDS}) and returns what changed "
            "since: status, reason and result writes and runner start/end for `task_ids`, "
            "and checkpoint changes for those tasks (or for every checkpoint when "
            "`include_checkpoints` is true), ordered by when each checkpoint was raised. "
            "Always returns a new cursor; an empty `changes` with a cursor means nothing "
            "changed, and an `error` keeps your cursor so a retry loses nothing. Stateless: "
            "the cursor carries the position. Task ids only; a cursor older than "
            f"{WATCH_MAX_CURSOR_AGE_HOURS:g} h is refused (`cursor_expired`) and you start "
            "again without one."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "cursor": {
                    "type": "string",
                    "description": "Cursor from a previous watch_swarm or watch.py result.",
                },
                "task_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": WATCH_MAX_TASKS,
                    "description": "Task entity ids to watch.",
                },
                "include_checkpoints": {
                    "type": "boolean",
                    "description": "Also report every checkpoint raised or resolved (default false).",
                },
                "wait_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": WATCH_MAX_WAIT_SECONDS,
                    "description": "How long to wait for a change before returning (default 0).",
                },
            },
            "additionalProperties": False,
        },
    ),
]

TOOL_HANDLERS = {
    "get_swarm_roster": lambda args: _get_swarm_roster(),
    "route_task": lambda args: _route_task(
        args["task_description"], args.get("action_type")
    ),
    "list_checkpoints": lambda args: _list_checkpoints(
        cursor=args.get("cursor"), limit=args.get("limit")
    ),
    "resolve_checkpoint": lambda args: _resolve_checkpoint(
        args["checkpoint_id"], args["action"], args.get("resolver_aauth_headers")
    ),
    "get_gate_status": lambda args: _get_gate_status(
        args["issue_ref"], int(args.get("history_limit", 5) or 5)
    ),
    "list_pipeline_queue": lambda args: _list_pipeline_queue(),
    "get_dispatch_health": lambda args: _get_dispatch_health(),
    # Both run in a worker thread: the timeline makes several reads and the
    # watch sleeps between polls, and neither may stall the stdio event loop.
    "get_task_timeline": lambda args: asyncio.to_thread(
        _get_task_timeline, args["task_entity_id"], args.get("window_hours")
    ),
    "watch_swarm": lambda args: asyncio.to_thread(
        _watch_swarm,
        args.get("cursor"),
        args.get("task_ids"),
        bool(args.get("include_checkpoints", False)),
        args.get("wait_seconds", 0),
    ),
}


async def main():
    denial_store = _require_checkpoint_release_state()
    log.info("Checkpoint denial store ready at %s", denial_store)
    server = Server("ateles", instructions=render_server_instructions())

    @server.list_tools()
    async def handle_list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
        handler = TOOL_HANDLERS.get(name)
        if not handler:
            return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]
        result = handler(arguments or {})
        if inspect.isawaitable(result):
            result = await result
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    options = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, options)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    import asyncio
    asyncio.run(main())
