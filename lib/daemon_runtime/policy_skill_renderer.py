"""lib/daemon_runtime/policy_skill_renderer.py — render active `agent_policy`
rows as Agent Skills, and as the plain-text session-rule index.

ateles#1261 (E2 session transport). PR #1255 removed the swarm's governing
rules from the MCP server's `instructions` field because the client caps that
field at ~2,048 characters across ALL connected servers — a rule written to
the record stopped reaching any session. This module is the replacement
transport: one Agent Skill per active `agent_policy` row, rendered LIVE from
Neotoma, no generated files committed (a file copy per checkout is how 290
copies of CLAUDE.md drifted into 31 versions — see docs/foundation history).

Canonical artifact, per the issue's settled design:
  - `name`      — a stable slug derived from the rule's entity id.
  - `description` — the rule's `applies_when` trigger plus a one-line
                     imperative (what a model sees up front, before deciding
                     whether to fetch the full rule).
  - `body`      — the full rule text plus its entity id (fetched only when
                   needed — never logged wholesale, since some rules hold
                   operator payment details).

Two channels share this one artifact:
  1. Now: `.claude/hooks/session_rule_index.py` (SessionStart hook) prints
     `render_index_text()` — the preamble (always-applies rules) followed by
     one summary line per conditional rule.
  2. Later: the MCP Skills extension (SEP-2640), once an SDK/host supports
     it — `render_skills()` gives the same skill objects; this module is
     designed so adding that transport is additive, not a rewrite.

Scope filter: `policy_binds_agent` (`lib/daemon_runtime/agent_loader.py`) is
imported, never re-implemented — CLAUDE.md's "extend the mechanism that
already generalizes" rule, and the specific mechanism ateles#1118 fixed.
Reused for the SESSION case (not one dispatched agent) by evaluating it for
`scope in {global, swarm}` OR an explicit `agent_sub` naming the session
principal — the same row-level test the function already implements; a
session-wide index takes the union across all agents' visibility instead of
one agent's, so it is the `global`/`swarm` rows plus every `agent`-scoped row
(a session may act as any agent depending on what it dispatches into).

`applies_when` is a newer field than the ones `docs/foundation/data_model.md`
already documents for `agent_policy` (`rule`, `rule_kind`, `scope`,
`agent_sub`, `domain`, `status`, ...) — `conformance_suite.md` (MG-13) names
it as the target of the skill migration's predicate collapse. Rows written
before that field existed have none; this module degrades a missing
`applies_when` to the literal string "always" is NOT assumed — an absent
`applies_when` is read as "unconditional" only if `rule_kind` also reads
`mandatory` is NOT the rule either. The actual rule, stated once: a row with
no `applies_when` is treated as a CONDITIONAL rule whose trigger is simply
unstated ("(trigger not recorded)"), never silently promoted to the preamble
— promoting an unscoped rule to "always applies" on the strength of an
absent field is exactly the fail-open-on-the-safety-field mistake
`docs/foundation/principles.md#5` names. Only a row whose `applies_when`
case-insensitively equals "always" joins the preamble.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

try:  # package import (normal runtime) with script-import fallback
    from .agent_loader import policy_binds_agent  # type: ignore
except ImportError:  # pragma: no cover
    from agent_loader import policy_binds_agent  # type: ignore

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

# Statuses that count as "active" for the purpose of this index. Mirrors
# AgentLoader.load_active_policies (active + provisional), not a new
# vocabulary — provisional rules ARE surfaced, same rationale as there:
# exposure is what matures them.
_LIVE_STATUSES = frozenset({"active", "provisional"})

# The one literal spelling that promotes a rule into the preamble. Anything
# else — including an absent field — is a conditional rule.
_ALWAYS = "always"


class PolicyIndexError(Exception):
    """Raised when rendering fails in a way the caller must surface loudly
    (never swallowed silently) — a transport failure or a budget overflow.
    Caught only at the hook boundary, which turns it into the one-line
    fail-open notice; every other caller should let it propagate.
    """


@dataclass(frozen=True)
class PolicySkill:
    """One `agent_policy` row projected into Agent Skill shape."""

    entity_id: str
    name: str
    description: str
    body: str
    applies_when: str
    is_preamble: bool
    rule_kind: str = "mandatory"
    scope: str = ""


def _slug(entity_id: str, domain: str) -> str:
    """Stable slug: the entity id is already stable and unique, and is kept
    as the primary key of the name; `domain` is appended only for
    readability, and stripped to the plain slug charset so an operator-set
    `domain` string can never inject something unexpected into a skill name.
    """
    dom = re.sub(r"[^a-z0-9]+", "-", (domain or "").strip().lower()).strip("-")
    short_id = entity_id.replace("ent_", "")[:12] or "unknown"
    return f"policy-{dom}-{short_id}" if dom else f"policy-{short_id}"


def _imperative(rule: str) -> str:
    """First sentence of `rule`, used as the one-line imperative in
    `description`. Falls back to the whole rule (already short) when no
    sentence boundary is found.
    """
    rule = (rule or "").strip()
    if not rule:
        return "(no rule text recorded)"
    m = re.search(r"(.+?[.!?])(\s|$)", rule)
    return (m.group(1) if m else rule).strip()


def _unwrap(entity: dict) -> dict:
    """Same double-nesting as every other Neotoma reader in this repo
    (render_agent_docs.py, agent_loader.py): the field dict rides either
    flat under `snapshot` or nested one level deeper.
    """
    outer = entity.get("snapshot") or {}
    snap = outer.get("snapshot", outer) if isinstance(outer, dict) else {}
    return snap if isinstance(snap, dict) else {}


def _request(url: str, body: dict, timeout: float = 10.0) -> dict:
    headers = {"Content-Type": "application/json"}
    if NEOTOMA_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {NEOTOMA_BEARER_TOKEN}"
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def fetch_active_policy_rows(
    base_url: str = NEOTOMA_BASE_URL, timeout: float = 10.0
) -> list[dict]:
    """All active/provisional `agent_policy` snapshot dicts, unfiltered by
    scope. Raises on transport failure — the caller decides how to degrade
    (the hook turns this into the one-line notice; nothing here swallows it,
    per CLAUDE.md's "validate the instrument" and "a write/read that fails
    must not look like an empty result" rules).
    """
    data = _request(
        f"{base_url}/entities/query",
        {"entity_type": "agent_policy", "limit": 200, "include_snapshots": True},
        timeout=timeout,
    )
    entities = data.get("entities") or data.get("results") or []
    out: list[dict] = []
    for e in entities:
        snap = _unwrap(e) if isinstance(e, dict) else {}
        if not snap:
            continue
        if str(snap.get("status", "")).strip().lower() not in _LIVE_STATUSES:
            continue
        snap["_entity_id"] = e.get("entity_id") or snap.get("entity_id", "")
        out.append(snap)
    return out


def _session_scope_ok(snap: dict) -> bool:
    """Session-wide visibility test, built ONLY from `policy_binds_agent` —
    never a second predicate (CLAUDE.md: "reuse `policy_binds_agent`, do not
    write a second predicate"). A session is not one fixed agent, so it takes
    the union `policy_binds_agent` already computes per-agent: global/swarm
    rows bind unconditionally (any `agent_sub` value proves that), and an
    `agent`-scoped row binds because a session may dispatch as that agent.
    """
    scope = str(snap.get("scope") or "").strip().lower()
    if scope in ("global", "swarm"):
        return policy_binds_agent(snap, "")  # agent_sub irrelevant at this scope
    agent_sub = str(snap.get("agent_sub") or "").strip()
    if not agent_sub:
        # An `agent`-scoped row with no `agent_sub` is refused at the write
        # per data_model.md — but a live row of unknown scope must still fail
        # CLOSED (principles.md #5) rather than broadcast to every session.
        return False
    return policy_binds_agent(snap, agent_sub)


def to_skill(snap: dict) -> PolicySkill:
    entity_id = str(snap.get("_entity_id") or snap.get("entity_id") or "")
    rule = str(snap.get("rule") or snap.get("description") or "")
    domain = str(snap.get("domain") or "")
    applies_when = str(snap.get("applies_when") or "").strip()
    is_preamble = applies_when.lower() == _ALWAYS
    imperative = _imperative(rule)
    trigger = applies_when if applies_when else "(trigger not recorded)"
    description = imperative if is_preamble else f"When {trigger}: {imperative}"
    body = f"{rule}\n\nSource: agent_policy {entity_id}".strip()
    return PolicySkill(
        entity_id=entity_id,
        name=_slug(entity_id, domain),
        description=description[:500],
        body=body,
        applies_when=applies_when,
        is_preamble=is_preamble,
        rule_kind=str(snap.get("rule_kind") or "mandatory"),
        scope=str(snap.get("scope") or ""),
    )


def render_skills(rows: list[dict]) -> list[PolicySkill]:
    """Session-scoped rows, projected to PolicySkill, preamble first."""
    scoped = [r for r in rows if _session_scope_ok(r)]
    skills = [to_skill(r) for r in scoped]
    skills.sort(key=lambda s: (not s.is_preamble, s.entity_id))
    return skills


def render_index_text(skills: list[PolicySkill], budget_chars: int) -> str:
    """The plain-text session index: preamble first, then one line per
    conditional rule, then a closing line. Raises PolicyIndexError rather
    than truncating mid-rule when the result would exceed `budget_chars` —
    the issue's hard requirement. Never truncates; either the whole index
    fits or the caller is told loudly that it does not.
    """
    preamble = [s for s in skills if s.is_preamble]
    conditional = [s for s in skills if not s.is_preamble]

    lines: list[str] = []
    if preamble:
        lines.append("## Always-applies rules")
        for s in preamble:
            lines.append(f"- {s.description} [{s.entity_id}]")
        lines.append("")
    if conditional:
        lines.append("## Conditional rules")
        for s in conditional:
            lines.append(f"- {s.description} [{s.entity_id}]")
        lines.append("")
    lines.append(
        "Fetch the full rule from Neotoma by entity id before acting on a "
        "conditional rule above (agent_policy.rule; do not act on the "
        "one-line summary alone)."
    )
    text = "\n".join(lines)

    if len(text) > budget_chars:
        raise PolicyIndexError(
            f"agent_policy index is {len(text)} chars, over the "
            f"{budget_chars}-char budget ({len(skills)} rules: "
            f"{len(preamble)} preamble + {len(conditional)} conditional). "
            "Refusing to truncate mid-rule — trim rule count or shorten "
            "descriptions at the source (agent_policy.applies_when / "
            "imperative sentence), not here."
        )
    return text
