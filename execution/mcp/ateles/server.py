#!/usr/bin/env python3
"""
ateles — MCP server for Ateles swarm routing and checkpoint management.

Provides seven tools that wrap multi-step Neotoma/GitHub query patterns into
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
from datetime import datetime, timezone
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
# The live resolve and `policy_binds_agent` scoping predicate stay in place
# below: `AgentLoader.render_policy_prompt` is still the dispatch-side
# renderer (ateles#1118) and other callers still use it. This function now
# only measures its output against a small fixed budget rather than
# forwarding it verbatim.
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
    return {"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"}


# Last transport failure, so callers can tell "Neotoma said no rows" apart from
# "the request never succeeded". Both still surface as None/[] from the helpers
# — this records WHY, and tools echo it back to the agent.
#
# Motivating bug: _retrieve_entities posted to a 404 path, got None, returned
# [], and get_swarm_roster reported "swarm_roster not found" — a data-absence
# message for a transport failure. The URL fix alone would leave the next wrong
# endpoint, expired token, or outage just as silent.
_last_transport_error: str | None = None


def _clear_transport_error() -> None:
    global _last_transport_error
    _last_transport_error = None


def _record_transport_error(kind: str, method: str, path: str, detail: str) -> None:
    """kind is the agent-actionable class: no_token | not_found | request_failed."""
    global _last_transport_error
    _last_transport_error = f"{kind}: {method} {path} — {detail}"
    log.warning("neotoma %s %s failed (%s): %s", method, path, kind, detail)


def _describe_transport_error() -> str | None:
    return _last_transport_error


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
