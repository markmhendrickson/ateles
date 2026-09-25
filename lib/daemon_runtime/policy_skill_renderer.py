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

TIERED RENDERING (added after the first live measurement showed the full
corpus — 50 session-scoped rules — renders to 12,633 chars, over budget).
Operator ruling: size must DEGRADE, never fail open. Fail-open is reserved
for Neotoma being unreachable or rendering itself raising — never merely for
being over budget, since a session with zero rules and a session with 50
rules it never saw are different failures and must not read the same.
`render_index_text` tries three tiers in order and returns whichever first
fits, tagging the output with a trailing `<!-- tier: A|B|C -->` comment line
so a session (and a test) can see which one ran without re-deriving it:

  Tier A — preamble WITH a short imperative (there are only ever a
           handful of always-applies rules), conditional lines as
           `- When <applies_when>: <imperative> [<entity_id>]`.
  Tier B — same preamble, conditional lines DROP the imperative:
           `- When <applies_when>: [<entity_id>]`. The trigger IS the
           recognition key and the full rule is fetched by id — the
           per-rule imperative was always optional, per the ruled design.
  Tier C — only reached if B still doesn't fit (a much larger future
           corpus). Preamble plus as many WHOLE conditional lines (tier-B
           shape) as fit, mandatory rules first (`rule_kind == "mandatory"`
           before `advisory`), then one line stating how many rules were
           omitted and how to query Neotoma for the rest. Never cuts a
           line mid-way. Logs a WARNING — dropping rules from the index is
           real information loss, and silence about it is exactly the
           failure this file exists to avoid.

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
before that field existed have none.

THE RULE, STATED ONCE: a row is preamble if and only if `applies_when`
case-insensitively equals "always". Every other case — a different value, or
the field absent entirely — is conditional. A conditional row with no
`applies_when` renders its trigger as the literal placeholder
"(trigger not recorded)" and is never promoted to the preamble on the
strength of an absent field; doing so would be the fail-open-on-the-
safety-field mistake `docs/foundation/principles.md#5` names — the field
that decides "unconditional vs. conditional" must default to the narrower
reading when unset, not the broader one.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
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
    """Raised only when rendering itself cannot produce ANY fitting output —
    the budget is too small even at tier C, with every conditional rule
    dropped (preamble + closing line alone overflow it). A merely large
    corpus does NOT raise this: `render_index_text` degrades through tiers
    A -> B -> C first (operator ruling, ateles#1261 follow-up: size must
    degrade, never fail open). Caught only at the hook boundary, which
    turns it into the one-line fail-open notice — the one case left where
    there is genuinely nothing smaller to render.
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


_CLOSING_LINE = (
    "Fetch the full rule from Neotoma by entity id before acting on a "
    "conditional rule above (agent_policy.rule; do not act on the "
    "one-line summary alone)."
)


def _preamble_lines(preamble: list[PolicySkill]) -> list[str]:
    """Tier A/B share the SAME preamble rendering — only the conditional
    section's shape differs between tiers, since there are only ever a
    handful of always-applies rules and dropping their imperative buys
    negligible budget at real cost to a reader.
    """
    if not preamble:
        return []
    lines = ["## Always-applies rules"]
    for s in preamble:
        lines.append(f"- {s.description} [{s.entity_id}]")
    lines.append("")
    return lines


def _conditional_line_full(s: PolicySkill) -> str:
    """Tier A: trigger + short imperative + id."""
    return f"- {s.description} [{s.entity_id}]"


def _conditional_line_trigger_only(s: PolicySkill) -> str:
    """Tier B/C: trigger + id only — the imperative is dropped. The trigger
    IS the recognition key and the full rule is fetched by id, so the
    per-rule imperative was always optional (operator ruling, ateles#1261
    follow-up).
    """
    trigger = s.applies_when if s.applies_when else "(trigger not recorded)"
    return f"- When {trigger}: [{s.entity_id}]"


def _assemble(
    preamble: list[PolicySkill], conditional_lines: list[str], tier: str
) -> str:
    lines = list(_preamble_lines(preamble))
    if conditional_lines:
        lines.append("## Conditional rules")
        lines.extend(conditional_lines)
        lines.append("")
    lines.append(_CLOSING_LINE)
    lines.append(f"<!-- tier: {tier} -->")
    return "\n".join(lines)


def render_index_text(skills: list[PolicySkill], budget_chars: int) -> str:
    """The plain-text session index, degrading through three tiers rather
    than failing open on size (operator ruling, ateles#1261 follow-up: size
    must DEGRADE, never fail open — fail-open is reserved for Neotoma being
    unreachable or rendering itself raising, never merely for being over
    budget).

    Tier A: full conditional lines (trigger + imperative + id).
    Tier B: trigger + id only, same preamble.
    Tier C: as many WHOLE tier-B lines as fit — mandatory rules first — plus
            one line naming how many were omitted. Never cuts a line
            mid-way; only DROPS whole lines, and only ever at tier C.

    The chosen tier is tagged in a trailing `<!-- tier: A|B|C -->` comment so
    a session or a test can see which one ran. Always returns a string that
    fits `budget_chars` — the empty-corpus case (no preamble, no
    conditional) trivially fits at tier A and is not a special case here.
    """
    preamble = [s for s in skills if s.is_preamble]
    conditional = [s for s in skills if not s.is_preamble]

    # --- Tier A ---
    tier_a = _assemble(preamble, [_conditional_line_full(s) for s in conditional], "A")
    if len(tier_a) <= budget_chars:
        return tier_a

    # --- Tier B ---
    tier_b = _assemble(
        preamble, [_conditional_line_trigger_only(s) for s in conditional], "B"
    )
    if len(tier_b) <= budget_chars:
        log.warning(
            "agent_policy index: tier A (%d chars) exceeded the %d-char "
            "budget; degraded to tier B (%d chars, %d conditional rules, "
            "imperative dropped, trigger + id kept).",
            len(tier_a), budget_chars, len(tier_b), len(conditional),
        )
        return tier_b

    # --- Tier C: mandatory first, keep whole lines only, state the cut ---
    ordered = sorted(
        conditional, key=lambda s: (s.rule_kind != "mandatory", s.entity_id)
    )

    def _omitted_line(n: int) -> str:
        return (
            f"- {n} more mandatory/advisory rule(s) omitted for space. "
            "Query Neotoma directly: entity_type=agent_policy, status in "
            "(active, provisional), scope in (global, swarm) or agent_sub "
            "matching this session."
        )

    # The probe at each step must use the SAME text the final append will
    # use — an earlier revision probed against a short placeholder and then
    # appended a longer real line afterward, which could push the final
    # result back over budget after the loop had already decided to stop
    # (caught by test_tier_c_orders_mandatory_first_never_cuts_a_line...).
    # `_omitted_line`'s length depends only on the omitted COUNT (a small
    # integer), never on which rules were kept, so probing with the count
    # this candidate WOULD leave omitted is exact, not a worst case.
    kept_lines: list[str] = []
    kept_count = 0
    for s in ordered:
        candidate_lines = kept_lines + [_conditional_line_trigger_only(s)]
        remaining_if_kept = len(ordered) - (kept_count + 1)
        probe_tail = [_omitted_line(remaining_if_kept)] if remaining_if_kept > 0 else []
        probe = _assemble(preamble, candidate_lines + probe_tail, "C")
        if len(probe) > budget_chars:
            break
        kept_lines = candidate_lines
        kept_count += 1

    omitted = len(ordered) - kept_count
    if omitted > 0:
        kept_lines.append(_omitted_line(omitted))
        log.warning(
            "agent_policy index: tier B (%d chars) still exceeded the %d-char "
            "budget; degraded to tier C, omitting %d of %d conditional "
            "rules (mandatory kept first).",
            len(tier_b), budget_chars, omitted, len(ordered),
        )
    tier_c = _assemble(preamble, kept_lines, "C")

    if len(tier_c) > budget_chars:
        # Only reachable if even the preamble + closing line alone overflow
        # the budget — nothing left to drop. This is the one case that still
        # must raise loudly rather than silently emit an over-budget string.
        raise PolicyIndexError(
            f"agent_policy index cannot fit the {budget_chars}-char budget "
            f"even with every conditional rule dropped (tier C is "
            f"{len(tier_c)} chars from the preamble + closing line alone). "
            "Shorten agent_policy.applies_when / the preamble rules "
            "themselves, or raise the budget."
        )
    return tier_c
