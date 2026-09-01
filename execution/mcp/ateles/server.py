#!/usr/bin/env python3
"""
ateles — MCP server for Ateles swarm routing and checkpoint management.

Provides eight tools that wrap multi-step Neotoma/GitHub query patterns into
single calls, so any connected agent gets reliable swarm interaction without
re-deriving the roster/policy/checkpoint dance — or the entity-read plus
log-grep dance — each session.

Tools:
  get_swarm_roster    — full roster (roles → agent names)
  route_task          — resolve owning agent + definition + execution policy
  list_checkpoints    — pending checkpoint_briefs awaiting operator
  resolve_checkpoint  — approve/reject a checkpoint with validation
                        (the only tool that mutates Neotoma)
  get_gate_status     — an issue's gate_status, owner, blocking gates, history,
                        and pipeline state                        [read-only]
  list_pipeline_queue — who holds the issue-pipeline slot, who is queued, and
                        how long each has waited                  [read-only]
  get_dispatch_health — dispatcher liveness, recent activity, failures
                                                                  [read-only]
  merge_pr            — evaluate the full merge gate and merge only if it
                        passes; dry-run by default            [mutates GitHub]

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

Transport: stdio (launched by Claude Code as an MCP server subprocess).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    TextContent,
    Tool,
)

log = logging.getLogger("ateles")

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
6. **Merge through merge_pr, never `gh pr merge`.** merge_pr is the only path \
that checks the review verdict against the current head SHA, and the only one \
that distinguishes an unreviewed PR from an approved one — `gh pr merge` \
reports both as an empty reviewDecision. Reach for the shell here and you are \
merging on an assumption you have not tested.
"""


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


def _request(method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> dict | None:
    if not NEOTOMA_BEARER_TOKEN:
        _record_transport_error(
            "no_token", method, path,
            "NEOTOMA_BEARER_TOKEN is unset — escalate to the operator, retrying will not help",
        )
        return None
    try:
        resp = httpx.request(
            method,
            f"{NEOTOMA_BASE_URL}{path}",
            headers=_headers(),
            params=params,
            json=body,
            timeout=15,
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


def _post(path: str, body: dict) -> dict | None:
    return _request("POST", path, body=body)


def _retrieve_entities(
    entity_type: str,
    search: str | None = None,
    snapshot_filters: dict | None = None,
    limit: int = 100,
    include_snapshots: bool = True,
) -> list[dict]:
    body: dict[str, Any] = {
        "entity_type": entity_type,
        "limit": limit,
        "include_snapshots": include_snapshots,
    }
    if search:
        body["search"] = search
    if snapshot_filters:
        body["snapshot_filters"] = snapshot_filters
    # POST /entities/query — NOT /retrieve, which 404s. The GET /entities list
    # endpoint does not exist either; see lib/daemon_runtime/agent_loader.py.
    data = _post("/entities/query", body)
    if data is None:
        return []
    return data.get("entities", [])


def _snapshot_of(entity: dict) -> dict:
    snap = (entity.get("snapshot") or {}).get("snapshot")
    if isinstance(snap, dict):
        return snap
    if isinstance(entity.get("snapshot"), dict):
        return entity["snapshot"]
    return entity


def _correct(entity_id: str, entity_type: str, field: str, value: Any, idem_key: str) -> bool:
    body = {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "field": field,
        "value": value,
        "idempotency_key": idem_key,
    }
    result = _post("/correct", body)
    return result is not None


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
            high_blast = policy.get("high_blast_action_types", [])
            if isinstance(high_blast, str):
                try:
                    high_blast = json.loads(high_blast)
                except (json.JSONDecodeError, TypeError):
                    high_blast = []
            result["action_blast_radius"] = (
                "high" if action_type.lower() in [h.lower() for h in (high_blast or [])]
                else "low"
            )
    return result


def _list_checkpoints() -> dict:
    entities = _retrieve_entities(
        "checkpoint_brief",
        snapshot_filters={"status": {"op": "eq", "value": "awaiting_operator"}},
        limit=50,
    )

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

    return {"count": len(checkpoints), "checkpoints": checkpoints}


def _resolve_checkpoint(checkpoint_id: str, action: str) -> dict:
    action_lower = action.strip().lower()
    if action_lower not in ("approve", "reject"):
        return {"error": f"action must be 'approve' or 'reject', got '{action}'"}

    data = _get(f"/entities/{checkpoint_id}")
    if data is None:
        return {"error": f"checkpoint {checkpoint_id} not found or Neotoma unreachable"}

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

    new_status = "approved" if action_lower == "approve" else "rejected"
    idem_key = f"resolve-checkpoint-{checkpoint_id}-{new_status}"

    ok = _correct(checkpoint_id, "checkpoint_brief", "status", new_status, idem_key)
    if not ok:
        return {"error": "failed to correct checkpoint status in Neotoma"}

    task_id = snap.get("task_entity_id")
    if action_lower == "reject" and task_id:
        _correct(task_id, "task", "status", "declined", f"decline-swarm-harness-{task_id}")

    return {
        "checkpoint_id": checkpoint_id,
        "new_status": new_status,
        "task_entity_id": task_id,
        "action_taken": (
            "approved — dispatcher will re-dispatch"
            if action_lower == "approve"
            else "rejected — task marked declined"
        ),
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
    result["interpretation"] = health
    return result



# ── PR merge gate (the one mutating GitHub tool) ─────────────────────────────
#
# WHY THIS EXISTS. On 2026-09-01 eight PRs were merged from a session by hand
# with `gh pr merge --squash`. Seven of the eight carried
# reviewDecision=CHANGES_REQUESTED at the moment they were merged. Five of
# those went through in a single shell loop that piped merge output to
# /dev/null and then read back only `state` — so the blocking verdict was never
# displayed, let alone weighed.
#
# Every one of those merges was probably DEFENSIBLE: in each case the blocking
# review sat on an older commit than the merged head, i.e. the findings had
# been fixed by later pushes. But nothing checked. The session inferred
# "stale, therefore fine" and was right by luck, and `gh pr merge` offers no
# way to tell a SUPERSEDED CHANGES_REQUESTED from a LIVE one — both print the
# same string. That distinction is a SHA comparison, and a SHA comparison is
# exactly the sort of thing a tool should do and a tired agent should not.
#
# The inverse error is worse and is the one the operator named. A PR with NO
# review at all reports reviewDecision="" — the same empty value as a PR whose
# checks are simply not required. Absence of a verdict reads as absence of a
# blocker, so an unreviewed, pipeline-bypassing PR looks SAFEST to any naive
# check. This tool refuses that reading: an unsigned gate is unsigned, never
# cleared, mirroring _blocking_gates above.
#
# DESIGN. This is a gate evaluator that happens to be able to merge, not a
# merge command that happens to check. _evaluate_merge_gate is pure and total
# over five conditions; _merge_pr merges ONLY on an unconditional pass. It
# defaults to dry_run=True so the default call is a question, not an act.
#
# FAIL CLOSED EVERYWHERE. Any signal that cannot be read blocks the merge. A
# 403 on the reviews endpoint is not "no blocking reviews" — the whole class of
# bug this server was written against is a check that cannot tell absence from
# failure and reports the permissive answer.
#
# This does NOT breach the self-certification boundary above. It writes no gate
# state and signs nothing off: it reads verdicts other agents wrote and either
# performs the mechanical merge or refuses. A session still cannot approve its
# own work — it can only act on an approval that already exists.

_MERGE_METHODS = ("squash", "merge", "rebase")

# Marker for a PR that reached review without a parent issue, so it skipped the
# gated issue → pm/arch → Cicada path. swarm_dispatch posts it and calls merge
# "operator-gated"; that phrase has no machine meaning unless something reads
# the marker, which is what this does.
_BYPASS_MARKER = "<!-- pipeline-bypass-notice -->"


def _parse_pr_ref(pr_ref: str) -> tuple[str | None, int | None]:
    """Parse 'owner/repo#123' into (repo, number). Bare numbers are rejected.

    A bare '#123' is ambiguous across the swarm's repositories, and today's
    damage included operating on the wrong target because an identifier was
    guessed rather than given. Requiring the owner/repo form makes that
    impossible instead of unlikely.
    """
    if not pr_ref:
        return None, None
    match = re.match(r"^([\w.\-]+/[\w.\-]+)#(\d+)$", str(pr_ref).strip())
    if not match:
        return None, None
    return match.group(1), int(match.group(2))


def _github_get(path: str, params: dict | None = None) -> tuple[Any, str | None]:
    """GET the GitHub API. Returns (payload, error) — never raises.

    Mirrors _pipeline_markers' two-value contract so callers cannot silently
    read a failure as an empty result.
    """
    try:
        resp = httpx.get(
            f"{GITHUB_API}{path}",
            headers=_github_headers(),
            params=params or {},
            timeout=20,
        )
        resp.raise_for_status()
        return resp.json(), None
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in (401, 403):
            detail = (
                f"HTTP {status} — GitHub token missing, expired, or lacking scope; "
                "this signal is UNKNOWN, not clear"
            )
        else:
            detail = f"HTTP {status}"
        return None, f"{path}: {detail}"
    except Exception as exc:
        return None, f"{path}: {type(exc).__name__}: {exc}"


def _standing_review_verdict(reviews: Any, head_sha: str) -> dict:
    """Reduce a review list to the standing verdict, keyed to the head SHA.

    Applies GitHub's own reviewDecision semantics — latest review per reviewer
    wins, COMMENTED/PENDING never change the standing decision — and then adds
    the part GitHub does not give you: whether each standing verdict was cast
    against the CURRENT head or an earlier commit.

    A CHANGES_REQUESTED on an older SHA is reported as `stale`, NOT as clear.
    Staleness is a fact for the caller to act on, not permission to proceed:
    the fixes may well have landed, but only a human or a fresh review can say
    the findings were actually addressed. Reporting it as clear here would
    re-create by code the same assumption that went unchecked by hand.
    """
    latest: dict[str, dict] = {}
    for review in reviews if isinstance(reviews, list) else []:
        if not isinstance(review, dict):
            continue
        state = str(review.get("state") or "").upper()
        # COMMENTED and PENDING never carry a decision, so they must not
        # displace a standing APPROVED or CHANGES_REQUESTED from the same
        # reviewer — this is why "latest review" alone is the wrong reduction.
        if state not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
            continue
        user = ((review.get("user") or {}).get("login")) or "?"
        prev = latest.get(user)
        if prev is None or str(review.get("submitted_at") or "") >= str(
            prev.get("submitted_at") or ""
        ):
            latest[user] = review

    approvals, blocking, stale_blocking = [], [], []
    for user, review in latest.items():
        state = str(review.get("state") or "").upper()
        if state == "DISMISSED":
            continue
        sha = str(review.get("commit_id") or "")
        entry = {
            "reviewer": user,
            "state": state,
            "commit": sha[:12] or None,
            "submitted_at": review.get("submitted_at"),
            "on_current_head": bool(sha) and sha == head_sha,
        }
        if state == "APPROVED":
            approvals.append(entry)
        elif entry["on_current_head"]:
            blocking.append(entry)
        else:
            stale_blocking.append(entry)

    return {
        "approvals": approvals,
        "blocking": blocking,
        "stale_blocking": stale_blocking,
        # An approval on an older SHA does not vouch for code pushed since.
        "approved_on_current_head": any(a["on_current_head"] for a in approvals),
        "reviewed_at_all": bool(latest),
    }


def _required_checks_state(repo: str, head_sha: str) -> dict:
    """Combined status + check-runs for one SHA, reduced to green/failing/pending.

    Both surfaces are read because a repo can use either or both, and a green
    result on one says nothing about the other.
    """
    status, status_err = _github_get(f"/repos/{repo}/commits/{head_sha}/status")
    runs, runs_err = _github_get(
        f"/repos/{repo}/commits/{head_sha}/check-runs", {"per_page": 100}
    )
    if status_err or runs_err:
        return {
            "state": "unknown",
            "error": status_err or runs_err,
            "detail": "check state could not be read — this is NOT 'no checks failing'",
        }

    failing, pending = [], []
    combined = str((status or {}).get("state") or "").lower()
    for ctx in (status or {}).get("statuses") or []:
        state = str(ctx.get("state") or "").lower()
        if state == "failure":
            failing.append(ctx.get("context"))
        elif state == "pending":
            pending.append(ctx.get("context"))

    for run in (runs or {}).get("check_runs") or []:
        conclusion = str(run.get("conclusion") or "").lower()
        if str(run.get("status") or "").lower() != "completed":
            pending.append(run.get("name"))
        elif conclusion in ("failure", "timed_out", "cancelled", "action_required"):
            failing.append(run.get("name"))

    # The legacy combined-status endpoint reports state="pending" for a commit
    # that has NO legacy statuses at all — which is every commit in a repo that
    # uses only check-runs (the modern Actions surface). Treating that as
    # pending blocks every such PR forever, so the combined state counts only
    # when there is at least one context behind it. Verified against
    # markmhendrickson/ateles#631: five green check-runs, zero statuses,
    # combined state "pending".
    combined_is_real = bool((status or {}).get("statuses"))
    if failing:
        state = "failing"
    elif pending or (combined == "pending" and combined_is_real):
        state = "pending"
    else:
        # No checks configured at all is green: a repo without CI is not a repo
        # with failing CI. Branch protection, read below, is what makes a check
        # required in the first place.
        state = "green"
    return {"state": state, "failing": failing[:10], "pending": pending[:10]}


def _pr_has_bypass_notice(repo: str, number: int) -> tuple[bool, str | None]:
    """Whether the pipeline-bypass notice is present on the PR."""
    comments, error = _github_get(
        f"/repos/{repo}/issues/{number}/comments", {"per_page": 100}
    )
    if error:
        return False, error
    for comment in comments if isinstance(comments, list) else []:
        if _BYPASS_MARKER in str((comment or {}).get("body") or ""):
            return True, None
    return False, None


def _evaluate_merge_gate(repo: str, number: int) -> dict:
    """Evaluate every merge condition for one PR. Pure read; never merges.

    Five conditions, ALL of which must hold. They are totalled over a fixed
    list rather than short-circuited so the caller sees every reason at once —
    fixing one blocker only to discover the next on the following call is the
    interaction that made the manual version expensive.
    """
    blockers: list[str] = []
    warnings: list[str] = []

    pr, error = _github_get(f"/repos/{repo}/pulls/{number}")
    if error:
        return {
            "pr": f"{repo}#{number}",
            "mergeable": False,
            "blockers": [f"could not read the PR: {error}"],
            "detail": "the PR could not be read, so nothing about it is known",
        }

    head_sha = str((pr.get("head") or {}).get("sha") or "")
    base_ref = str((pr.get("base") or {}).get("ref") or "")
    state = str(pr.get("state") or "")
    merged = bool(pr.get("merged"))

    if merged:
        return {
            "pr": f"{repo}#{number}",
            "mergeable": False,
            "already_merged": True,
            "blockers": ["already merged"],
            "head_sha": head_sha[:12],
        }
    if state != "open":
        blockers.append(f"PR is {state}, not open")
    if pr.get("draft"):
        blockers.append("PR is a draft")

    # 1. Base must be the repository default branch. A stacked PR whose base is
    #    another open PR's head merges its parent's unreviewed commits too.
    default_branch = None
    repo_meta, repo_err = _github_get(f"/repos/{repo}")
    if repo_err:
        blockers.append(f"could not read the default branch: {repo_err}")
    else:
        default_branch = str((repo_meta or {}).get("default_branch") or "")
        if base_ref != default_branch:
            blockers.append(
                f"base is '{base_ref}', not the default branch '{default_branch}' "
                "— merging a stacked PR would carry its parent's commits"
            )

    # 2. GitHub's own mergeability (conflicts, and branch-protection blocks).
    mergeable_state = str(pr.get("mergeable_state") or "unknown")
    if pr.get("mergeable") is False:
        blockers.append(f"GitHub reports the PR is not mergeable ({mergeable_state})")
    elif pr.get("mergeable") is None:
        # GitHub computes this asynchronously; null means "ask again", which is
        # not the same as yes.
        blockers.append(
            "GitHub has not finished computing mergeability (mergeable=null) — retry shortly"
        )
    if mergeable_state == "blocked":
        blockers.append(
            "branch protection reports the PR as blocked (a required check or "
            "review is missing)"
        )

    # 3. Required checks green, against THIS head SHA.
    checks = {"state": "unknown"}
    if head_sha:
        checks = _required_checks_state(repo, head_sha)
        if checks["state"] == "failing":
            blockers.append(f"required checks are failing: {', '.join(str(c) for c in checks.get('failing') or [])}")
        elif checks["state"] == "pending":
            blockers.append(f"checks are still running: {', '.join(str(c) for c in checks.get('pending') or [])}")
        elif checks["state"] == "unknown":
            blockers.append(f"check state is unknown: {checks.get('error')}")
    else:
        blockers.append("could not determine the PR head SHA")

    # 4. Review verdict, keyed to the head SHA. This is the condition the
    #    2026-09-01 merges skipped.
    reviews_payload, reviews_err = _github_get(
        f"/repos/{repo}/pulls/{number}/reviews", {"per_page": 100}
    )
    review: dict[str, Any] = {}
    if reviews_err:
        blockers.append(f"review state could not be read: {reviews_err}")
    else:
        review = _standing_review_verdict(reviews_payload, head_sha)
        if review["blocking"]:
            who = ", ".join(f"{b['reviewer']}@{b['commit']}" for b in review["blocking"])
            blockers.append(f"CHANGES_REQUESTED standing against the current head ({who})")
        if review["stale_blocking"]:
            who = ", ".join(
                f"{b['reviewer']}@{b['commit']}" for b in review["stale_blocking"]
            )
            # Blocking, deliberately. The findings were probably fixed by the
            # pushes since — but "probably" is what went unchecked by hand, and
            # only a re-review can retire a finding.
            blockers.append(
                f"CHANGES_REQUESTED on an earlier commit ({who}), not re-reviewed "
                f"against the current head {head_sha[:12]} — a stale block is not a cleared one; "
                "request a fresh review, or waive with acknowledge_stale_review"
            )
        if not review["reviewed_at_all"]:
            # The inverse error: no review reads as no blockers.
            blockers.append(
                "no review of any kind — an unreviewed PR is UNSIGNED, not cleared "
                "(empty reviewDecision looks identical to a clean bill of health)"
            )
        elif not review["approved_on_current_head"] and not review["blocking"]:
            blockers.append(
                f"no APPROVE against the current head {head_sha[:12]} "
                "(an approval of earlier code does not vouch for what was pushed since)"
            )

    # 5. Pipeline-bypass notice. swarm_dispatch calls merge "operator-gated"
    #    when this is present; that phrase is inert unless something reads it.
    bypassed, bypass_err = _pr_has_bypass_notice(repo, number)
    if bypass_err:
        blockers.append(f"could not check for the pipeline-bypass notice: {bypass_err}")
    elif bypassed:
        blockers.append(
            "pipeline-bypass-notice is present — this PR skipped the gated "
            "issue → pm/arch → implementation path and merge stays operator-gated"
        )

    return {
        "pr": f"{repo}#{number}",
        "title": pr.get("title"),
        "url": pr.get("html_url"),
        "head_sha": head_sha[:12],
        "base_ref": base_ref,
        "default_branch": default_branch,
        "mergeable": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "checks": checks,
        "review": review,
        "bypass_notice": bypassed,
    }


def _merge_pr(
    pr_ref: str,
    method: str = "squash",
    dry_run: bool = True,
    acknowledge_stale_review: bool = False,
) -> dict:
    """Evaluate the merge gate for a PR, and merge only if it passes.

    dry_run defaults to True: the default call reports whether the PR MAY be
    merged and why, without merging. Merging requires dry_run=false explicitly,
    so a merge is always something the caller asked for in as many words.

    acknowledge_stale_review retires ONLY the stale-CHANGES_REQUESTED blocker,
    and only when that is the sole thing standing in the way. It exists because
    the stale case is both common and usually legitimate — but it forces the
    caller to say "I looked at that older verdict and the findings are fixed"
    rather than never seeing the verdict at all. It cannot clear a live block,
    a failing check, a missing approval, or a bypass notice.
    """
    repo, number = _parse_pr_ref(pr_ref)
    if not repo or not number:
        return {
            "error": f"could not parse pr_ref {pr_ref!r}",
            "detail": "expected the fully-qualified form 'owner/repo#123'",
        }
    if method not in _MERGE_METHODS:
        return {"error": f"method must be one of {list(_MERGE_METHODS)}, got {method!r}"}

    gate = _evaluate_merge_gate(repo, number)

    waived: list[str] = []
    if acknowledge_stale_review and gate.get("blockers"):
        remaining = [b for b in gate["blockers"] if "not re-reviewed against the current head" not in b]
        waived = [b for b in gate["blockers"] if b not in remaining]
        if waived:
            gate["blockers"] = remaining
            gate["mergeable"] = not remaining
            gate["waived_blockers"] = waived
            gate["waiver"] = (
                "stale CHANGES_REQUESTED waived by the caller via "
                "acknowledge_stale_review — the caller asserts the findings were "
                "addressed by commits pushed since that review"
            )

    gate["method"] = method
    gate["dry_run"] = bool(dry_run)

    if not gate.get("mergeable"):
        gate["merged"] = False
        gate["action"] = "refused"
        return gate

    if dry_run:
        gate["merged"] = False
        gate["action"] = "would_merge"
        gate["detail"] = (
            "the gate passes; nothing was merged because dry_run is true. "
            "Call again with dry_run=false to merge."
        )
        return gate

    try:
        resp = httpx.put(
            f"{GITHUB_API}/repos/{repo}/pulls/{number}/merge",
            headers=_github_headers(),
            json={"merge_method": method},
            timeout=60,
        )
    except Exception as exc:
        gate["merged"] = False
        gate["action"] = "merge_request_failed"
        gate["error"] = f"{type(exc).__name__}: {exc}"
        return gate

    body: dict = {}
    try:
        body = resp.json() or {}
    except Exception:
        body = {}

    # 200 alone is not proof: GitHub returns merged=false in some paths, and a
    # status code is not a landed state.
    if resp.status_code == 200 and body.get("merged"):
        gate["merged"] = True
        gate["action"] = "merged"
        gate["merge_sha"] = body.get("sha")
    else:
        gate["merged"] = False
        gate["action"] = "merge_refused_by_github"
        gate["error"] = (
            f"HTTP {resp.status_code}: {body.get('message') or resp.text[:200]}"
        )
    return gate


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
            "Returns all pending checkpoint_briefs (status: awaiting_operator) with "
            "task title, assigned agent, blast radius, confidence vs threshold, and "
            "reason pre-joined. These are the operator's decision queue."
        ),
        inputSchema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    ),
    Tool(
        name="resolve_checkpoint",
        description=(
            "Approves or rejects a pending checkpoint_brief by entity ID. Validates "
            "that the checkpoint is awaiting_operator and has not already been "
            "dispatched. On rejection, also marks the referenced task as declined."
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
            },
            "required": ["checkpoint_id", "action"],
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
            "its recent issue-pipeline log activity, and how many dispatch failures have "
            "been logged recently. Use it to tell 'the swarm is working on it' apart from "
            "'nothing is running at all'."
        ),
        inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
    ),
    Tool(
        name="merge_pr",
        description=(
            "Evaluate the full merge gate for a pull request and merge it ONLY if "
            "the gate passes. Checks, in one call: the base is the repository "
            "default branch (not a stacked PR), GitHub reports the PR mergeable, "
            "required checks are green against the CURRENT head SHA, a standing "
            "APPROVE exists against that same head SHA, and no pipeline-bypass "
            "notice is present. Every unreadable signal BLOCKS — an unreviewed PR "
            "is unsigned, not cleared, and a CHANGES_REQUESTED on an earlier commit "
            "is stale, not retired. Defaults to dry_run=true, so the default call "
            "reports whether the PR may be merged and why, without merging. "
            "Use this instead of `gh pr merge`, which shows none of the above."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "pr_ref": {
                    "type": "string",
                    "description": (
                        "Fully-qualified pull request, 'owner/repo#123'. A bare "
                        "number is rejected as ambiguous across swarm repositories."
                    ),
                },
                "method": {
                    "type": "string",
                    "enum": list(_MERGE_METHODS),
                    "description": "Merge method. Defaults to squash.",
                },
                "dry_run": {
                    "type": "boolean",
                    "description": (
                        "When true (the default), evaluate and report only. Pass "
                        "false to actually merge a PR whose gate passes."
                    ),
                },
                "acknowledge_stale_review": {
                    "type": "boolean",
                    "description": (
                        "Retire ONLY a CHANGES_REQUESTED left on an earlier commit, "
                        "asserting its findings were fixed by commits pushed since. "
                        "Cannot clear a live block, a failing check, a missing "
                        "approval, or a bypass notice."
                    ),
                },
            },
            "required": ["pr_ref"],
            "additionalProperties": False,
        },
    ),
]

TOOL_HANDLERS = {
    "get_swarm_roster": lambda args: _get_swarm_roster(),
    "route_task": lambda args: _route_task(
        args["task_description"], args.get("action_type")
    ),
    "list_checkpoints": lambda args: _list_checkpoints(),
    "resolve_checkpoint": lambda args: _resolve_checkpoint(
        args["checkpoint_id"], args["action"]
    ),
    "get_gate_status": lambda args: _get_gate_status(
        args["issue_ref"], int(args.get("history_limit", 5) or 5)
    ),
    "list_pipeline_queue": lambda args: _list_pipeline_queue(),
    "get_dispatch_health": lambda args: _get_dispatch_health(),
    "merge_pr": lambda args: _merge_pr(
        args["pr_ref"],
        args.get("method", "squash") or "squash",
        bool(args.get("dry_run", True)),
        bool(args.get("acknowledge_stale_review", False)),
    ),
}


async def main():
    server = Server("ateles", instructions=SERVER_INSTRUCTIONS)

    @server.list_tools()
    async def handle_list_tools() -> list[Tool]:
        return TOOLS

    @server.call_tool()
    async def handle_call_tool(name: str, arguments: dict) -> list[TextContent]:
        handler = TOOL_HANDLERS.get(name)
        if not handler:
            return [TextContent(type="text", text=json.dumps({"error": f"unknown tool: {name}"}))]
        result = handler(arguments or {})
        return [TextContent(type="text", text=json.dumps(result, indent=2, default=str))]

    options = server.create_initialization_options()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, options)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    import asyncio
    asyncio.run(main())
