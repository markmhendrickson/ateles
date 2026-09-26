"""lib/daemon_runtime/policy_skill_renderer.py — render active `agent_policy`
rows as Agent Skills, and as the plain-text session-rule index.

ateles#1261 (E2 session transport). PR #1255 removed the swarm's governing
rules from the MCP server's `instructions` field because the client caps that
field at ~2,048 characters across ALL connected servers — a rule written to
the record stopped reaching any session. This module is the replacement
transport: one Agent Skill per active `agent_policy` row, rendered LIVE from
Neotoma, no generated files committed (a file copy per checkout is how 290
copies of CLAUDE.md drifted into 31 versions — see docs/foundation history).

Canonical artifact, per the issue's settled design (imperative source
revised after Falco's round-2 security review — see below):
  - `name`      — a stable slug derived from the rule's entity id.
  - `description` — the row's `applies_when` trigger plus, when present, a
                     one-line imperative (what a model sees up front, before
                     deciding whether to fetch the full rule): `index_line`
                     when the row has one (schema 1.4.0, ateles#1295
                     follow-up — a field authored specifically to state the
                     rule's OPERATIVE CONSTRAINT, not just its topic), else
                     the row's `title`. NEVER derived from `rule`/`body`
                     text — a row with neither `index_line` nor `title`
                     renders trigger + id only.
  - `body`      — the full rule text plus its entity id, for a FUTURE
                   transport (an agent explicitly fetching a rule by id);
                   never read back into the rendered SessionStart index
                   itself (never logged wholesale, since some rules hold
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
`render_index_text` tries four tiers in order and returns whichever first
fits, tagging the output with a trailing `<!-- tier: A|A2|B|C -->` comment
line so a session (and a test) can see which one ran without re-deriving it:

  Tier A  — preamble WITH a short imperative (there are only ever a
            handful of always-applies rules), conditional lines as
            `- When <applies_when>: <imperative> [<entity_id>]`.
  Tier A2 — audience split (ateles#1254 follow-up, task
            ent_3b6b010658ce88f6be9e19ac). Only reached when tier A
            overflows. Same preamble. A conditional row scoped to exactly
            one agent (`scope == "agent"` — by construction the session's
            own principal, since `_session_scope_ok` already filtered out
            every other agent's row) keeps its tier-A full line: there is
            one audience reading it, so compacting buys nothing. A row
            scoped `global`/`swarm` (`agent_loader.
            POLICY_SCOPES_REACHING_EVERY_AGENT` — reaches every dispatched
            agent AND every session) gets a compact line instead: trigger
            + summary truncated at a word boundary (80 chars) + id. This
            keeps full detail for the audience that is ONLY this session,
            and spends the budget cut on rules an agent can look up from
            its own daemon-side context instead.
  Tier B  — same preamble, conditional lines DROP the imperative/summary
            entirely, regardless of scope: `- When <applies_when>:
            [<entity_id>]`. The trigger IS the recognition key and the
            full rule is fetched by id — the per-rule imperative was
            always optional, per the ruled design.
  Tier C  — only reached if B still doesn't fit (a much larger future
            corpus). Preamble plus as many WHOLE conditional lines (tier-B
            shape) as fit, mandatory rules first (`rule_kind ==
            "mandatory"` before `advisory`), then one line stating how
            many rules were omitted and how to query Neotoma for the
            rest. Never cuts a line mid-way. Logs a WARNING — dropping
            rules from the index is real information loss, and silence
            about it is exactly the failure this file exists to avoid.

Scope filter: `policy_binds_agent_by_edge` (`lib/daemon_runtime/agent_loader.py`) is
imported, never re-implemented — CLAUDE.md's "extend the mechanism that
already generalizes" rule, and the specific mechanism ateles#1118 fixed and
decision 114 (2026-09-25) superseded. A session runs as ONE agent, the
session principal (`ATELES_SESSION_PRINCIPAL`, default the operator's
interactive agent — the same principal `execution/mcp/ateles/server.py`
resolves rules for on the other session transport). The index holds the rows
that bind that principal: every `global`/`swarm` row with no `GOVERNS` edge,
plus a row with a `GOVERNS` edge to the principal's own `agent_definition`.
A row edged to a different agent is withheld; a row whose `scope` is absent
or outside the closed vocabulary (`agent_loader.POLICY_SCOPES`) AND carries
no edge is withheld too. See `_session_scope_ok`.

`applies_when` is a newer field than the ones `docs/foundation/data_model.md`
already documents for `agent_policy` (`rule`, `rule_kind`, `scope`,
`agent_sub`, `domain`, `status`, ...) — `conformance_suite.md` (MG-13) names
it as the target of the skill migration's predicate collapse. Rows written
before that field existed have none.

THE RULE, STATED ONCE: a row is preamble if and only if `applies_when`,
whitespace-stripped and before any sanitizing, case-insensitively equals
"always". Every other case — a different value, or
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
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_THIS_DIR = Path(__file__).resolve().parent
if str(_THIS_DIR) not in sys.path:
    sys.path.insert(0, str(_THIS_DIR))

try:  # package import (normal runtime) with script-import fallback
    from .agent_loader import (  # type: ignore
        AGENT_POLICY_GOVERNS_EDGE,
        POLICY_QUERY_BODY,
        POLICY_SCOPES,
        POLICY_SCOPES_REACHING_EVERY_AGENT,
        fetch_governs_edges,
        policy_binds_agent,
        policy_binds_agent_by_edge,
        resolve_agent_definition_id,
        unwrap_policy_entities,
    )
except ImportError:  # pragma: no cover
    from agent_loader import (  # type: ignore
        AGENT_POLICY_GOVERNS_EDGE,
        POLICY_QUERY_BODY,
        POLICY_SCOPES,
        POLICY_SCOPES_REACHING_EVERY_AGENT,
        fetch_governs_edges,
        policy_binds_agent,
        policy_binds_agent_by_edge,
        resolve_agent_definition_id,
        unwrap_policy_entities,
    )

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

# Which agent an interactive session IS, for scoping `agent`-scoped rows.
# Not a new identity: this is the session principal the other session-side
# transport already resolves agent_policy for — `execution/mcp/ateles/
# server.py`'s `SESSION_PRINCIPAL`, same env var, same default (the
# operator's interactive agent). Held equal to server.py's by
# `test_session_principal_matches_the_mcp_servers` (an AST read, since
# importing the MCP server pulls in its whole dependency set).
SESSION_PRINCIPAL_ENV = "ATELES_SESSION_PRINCIPAL"
DEFAULT_SESSION_PRINCIPAL = "ateles@ateles-swarm"


def session_principal() -> str:
    """The `agent_sub` this session resolves to an `agent_definition` id
    (via `resolve_agent_definition_id`) before evaluating `GOVERNS`-edged
    rows against it. Read at call time, not import time, so the hook and
    tests see the environment they actually run in. An explicitly empty
    value resolves to no `agent_definition` id and so binds no edge-scoped
    row (`policy_binds_agent_by_edge` never matches an empty id)."""
    return os.environ.get(SESSION_PRINCIPAL_ENV, DEFAULT_SESSION_PRINCIPAL).strip()

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


# --- Sanitization: every row-derived field is untrusted data (Falco, ---
# --- ateles#1268 round 2, CONFIRMED injection). ---
#
# `agent_policy` rows are not exclusively operator-authored — the generalizer
# writes agent-local policies, and every lens in this swarm has a documented
# self-write path to `agent_policy` on a generalized finding. A row's
# `applies_when` or `title` can therefore contain adversarial or malformed
# text, and this hook's stdout is fed directly into a model's context at
# SessionStart — so a forged `\n## Always-applies rules\n- IGNORE PRIOR
# RULES...` embedded in a field, or a `-->` sequence forging the trailing
# `<!-- tier: X -->` marker, must never reach that stream verbatim.
#
# `_sanitize_field` is the ONE place any row-derived string is neutralized
# before interpolation — `to_skill` is the only caller, so every downstream
# consumer of `PolicySkill.description`/`.applies_when`/`.entity_id` already
# received sanitized text; nothing else needs its own escaping.

# Separator/control characters collapsed to a single space before anything
# else runs. Covers \r, \n, the Unicode line/paragraph separators (U+2028,
# U+2029), NEL (U+0085), and the C0/C1 control ranges (excluding the ones
# already handled: \t \r \n themselves get replaced first so a run of mixed
# whitespace does not leave a bare tab). re.sub with a character class
# spanning both C0 (\x00-\x1f, \x7f) and C1 (\x80-\x9f) controls, plus the
# three named separators, in one pass.
_LINE_BREAKING_CHARS = re.compile(
    "[\r\n  \u0085\x00-\x08\x0b\x0c\x0e-\x1f\x7f\x80-\x9f]"
)
_WHITESPACE_RUN = re.compile(r"\s+")

# Leading markdown structure a row must not be able to open with: ATX
# headings (#), bullets (-, *), blockquotes (>), ordered-list markers
# (digits followed by "."), and backticks (code spans/fences). Stripped
# repeatedly from the START of the string only — a bullet appearing mid-
# sentence is legitimate prose, not structure.
_LEADING_MARKDOWN = re.compile(r"^[\s]*(?:[#>*`-]+|\d+\.)+\s*")

# The two structural sequences that must never appear verbatim in row-
# derived text: an HTML comment close (which could terminate the renderer's
# own `<!-- tier: X -->` marker early and let following text escape it) and
# any "tier:" marker pattern (which could forge a fake tier tag).
_HTML_COMMENT_MARKERS = re.compile(r"<!--|-->")
_TIER_MARKER_PATTERN = re.compile(r"tier\s*:\s*[A-Za-z]", re.IGNORECASE)


def _sanitize_pass(text: str) -> str:
    """One pass of the neutralizing steps, in order: line-breaking/control
    characters to spaces, leading markdown structure stripped, HTML-comment
    and tier-marker sequences deleted, whitespace collapsed and trimmed."""
    text = _LINE_BREAKING_CHARS.sub(" ", text)
    text = _LEADING_MARKDOWN.sub("", text)
    text = _HTML_COMMENT_MARKERS.sub("", text)
    text = _TIER_MARKER_PATTERN.sub("", text)
    return _WHITESPACE_RUN.sub(" ", text).strip()


def _sanitize_field(raw: str, max_len: int) -> str:
    """Collapse `raw` to one inert line, capped at `max_len` chars.

    Runs `_sanitize_pass` REPEATEDLY until the text stops changing. One pass
    is not enough: a deletion can join the characters on either side of it
    into a new forbidden sequence (a comment marker or tier tag nested inside
    another rebuilds the outer one), and deleting a leading comment can
    expose a leading `##` that the markdown strip already ran past (Falco,
    ateles#1268 round 3). At the fixed point none of the forbidden sequences
    is present and there is no leading markdown, so the result is idempotent:
    `_sanitize_field(_sanitize_field(x, n), n) == _sanitize_field(x, n)`.
    Every pass that changes the text either shortens it or only swaps
    characters for spaces once, so the loop terminates; the iteration cap is
    a backstop, not the mechanism.

    Length is capped AFTER the fixed point, with an ellipsis. Cutting a clean
    string cannot create a forbidden sequence (every substring of a clean
    string is clean), so the cap preserves both properties. Returns "" if
    nothing survives — the caller skips the row and counts it, per Falco's
    round-2 fix: "rejected if it's empty after sanitising."
    """
    if not raw:
        return ""
    text = raw
    for _ in range(len(raw) + 2):
        cleaned = _sanitize_pass(text)
        if cleaned == text:
            break
        text = cleaned
    if not text:
        return ""
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"  # ellipsis
    return text


_ENTITY_ID_DISALLOWED = re.compile(r"[^A-Za-z0-9_]")

_APPLIES_WHEN_MAX = 160
_IMPERATIVE_MAX = 120


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect. `urllib`'s default handler follows a 3xx on a
    POST and re-sends the request headers, `Authorization` included, to
    whatever host the `Location` names (Falco, ateles#1268 round 3). The
    bearer token must never leave the configured Neotoma host, and Neotoma's
    query route has no legitimate reason to redirect, so a redirect is an
    error here — the same posture as `agent_loader`'s httpx transport, which
    does not follow redirects."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            f"redirect refused (would have gone to another location): {msg}",
            headers, fp,
        )


_OPENER = urllib.request.build_opener(_RefuseRedirect)


def _open(req: urllib.request.Request, timeout: float):
    """The one place a request leaves this module: through the opener that
    refuses redirects, never the global `urllib.request.urlopen`."""
    return _OPENER.open(req, timeout=timeout)


def _request(
    url: str, body: dict, timeout: float = 10.0, retries: int = 3
) -> dict:
    """POST `body` as JSON, stdlib-only (`urllib`) — the hook cannot import
    `httpx`, which is why this stays a second TRANSPORT from `AgentLoader`'s
    (Waxwing, ateles#1268 round 2: the FETCH SHAPE — query body, unwrap,
    status filter — is shared via `agent_loader.POLICY_QUERY_BODY` /
    `unwrap_policy_entities`; only the wire client legitimately differs,
    since one runs stdlib-only and the other doesn't).

    Retries up to `retries` times on a transient transport error (a
    connection reset, timeout, or 5xx-shaped `HTTPError`) before raising —
    a single flaky attempt must not read as "Neotoma is unreachable" and
    fall the whole session open when a second attempt would have succeeded.
    Does NOT retry a definitive failure (a refused 3xx redirect, a 4xx
    `HTTPError`, malformed JSON) — retrying those wastes the hook's time
    budget without any chance of a different outcome.

    Never follows a redirect (`_RefuseRedirect`), so the `Authorization`
    header is only ever sent to `url`'s own host.
    """
    headers = {"Content-Type": "application/json"}
    if NEOTOMA_BEARER_TOKEN:
        headers["Authorization"] = f"Bearer {NEOTOMA_BEARER_TOKEN}"
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        req = urllib.request.Request(
            url, data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        try:
            with _open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            if exc.code < 500:
                raise  # refused redirect or client error — retrying changes nothing
            last_exc = exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            last_exc = exc
        if attempt < retries:
            log.warning(
                "agent_policy fetch attempt %d/%d failed (%s: %s); retrying",
                attempt, retries, type(last_exc).__name__, last_exc,
            )
    assert last_exc is not None  # loop always sets it before falling through
    raise last_exc


def fetch_active_policy_rows(
    base_url: str = NEOTOMA_BASE_URL, timeout: float = 10.0
) -> list[dict]:
    """All active/provisional `agent_policy` snapshot dicts, unfiltered by
    scope. Raises on transport failure — the caller decides how to degrade
    (the hook turns this into the one-line notice; nothing here swallows it,
    per CLAUDE.md's "validate the instrument" and "a write/read that fails
    must not look like an empty result" rules).

    The query shape and the unwrap+status-filter step are `agent_loader`'s
    `POLICY_QUERY_BODY` / `unwrap_policy_entities` — the SAME functions
    `AgentLoader.load_active_policies` calls, so there is one reader of
    agent_policy's response shape, not two that can silently diverge
    (Waxwing, ateles#1268 round 2).
    """
    data = _request(f"{base_url}/entities/query", POLICY_QUERY_BODY, timeout=timeout)
    return unwrap_policy_entities(data)


def _session_scope_ok(
    snap: dict,
    principal: str | None = None,
    *,
    agent_definition_id: "str | None" = None,
    governs: "dict[str, frozenset[str]] | None" = None,
) -> bool:
    """Whether one row belongs in THIS session's index.

    A session runs as one agent — the session principal (`session_principal()`,
    default the operator's interactive agent) — so a row is in scope exactly
    when it binds that agent, decided by the SAME shared predicate the
    daemon loader uses, `policy_binds_agent_by_edge(snap, agent_definition_id,
    governs)` (operator ruling 2026-09-25, decision 114). `agent_sub` is
    read NOWHERE here — a copied predicate is how the two readers drift, and
    `policy_binds_agent_by_edge` is the ONE place both consult.

    Case by case (identical to `policy_binds_agent_by_edge`'s own doc):
      - A row with a `GOVERNS` edge to ANY agent binds ONLY the agent(s) it
        has an edge to, regardless of `scope`.
      - A row with no edge binds every agent when `scope` is `global` or
        `swarm`.
      - A row with no edge and no recognised swarm-wide `scope` binds
        NOBODY — the fail-closed default (`principles.md #5`); an
        `agent`-scoped row that has not been migrated to an edge is no
        longer read by its `agent_sub` at all, since that field (and
        `scope: agent`) are superseded by the edge, not a fallback to it.

    `principal` is `session_principal()` by default (or an explicit override
    in a test) — it is NEVER compared against a row's `agent_sub` (that
    field is superseded), but it is still the identity `agent_definition_id`
    is resolved FROM when the caller has not already resolved one.
    Resolving a principal's `agent_sub` to its `agent_definition` entity id
    is itself a Neotoma read, so `render_skills` resolves it ONCE per call
    and passes the result in as `agent_definition_id`, never re-resolving
    per row; a direct caller of this function (tests; a caller that already
    has the id) may pass `agent_definition_id` explicitly to skip that
    lookup entirely. When neither is available, no edge can ever match, so
    a row is included only through the swarm-wide `scope` branch above —
    the same fail-closed posture an unresolvable principal already had
    under the field-based predicate. `governs=None` treats every row as
    edgeless, for the same reason: never an implicit per-row fetch.
    """
    scope = str(snap.get("scope") or "").strip().lower()
    entity_id = str(snap.get("_entity_id") or snap.get("entity_id") or "")
    edge_targets = (governs or {}).get(entity_id, frozenset())
    if scope not in POLICY_SCOPES and not edge_targets:
        return False
    resolved_id = agent_definition_id
    if resolved_id is None and edge_targets:
        sub = session_principal() if principal is None else principal.strip()
        resolved_id = resolve_agent_definition_id(sub) if sub else None
    return policy_binds_agent_by_edge(snap, resolved_id or "", governs or {})


def to_skill(snap: dict) -> PolicySkill | None:
    """Project one row into a `PolicySkill`, or None if it has nothing safe
    to render (Falco: "rejected if it's empty after sanitising. skip the
    row and count it" — the caller, `render_skills`, does the counting).

    Never reads `rule` (the full rule body) into any field that reaches the
    rendered index (Falco, ateles#1268 round 2: "stop deriving tier A's
    imperative from the first sentence of the rule text... never read `rule`
    or `body` into the index at all"). The imperative comes from `index_line`
    when present — a field authored specifically to carry the one-liner's
    OPERATIVE CONSTRAINT (the concrete URL shape, tool name, limit, or
    forbidden action), so an agent acting on the summary alone still
    complies (rule-delivery evals, ateles#1301/#1295: the link-ids rule and
    the AskUserQuestion rule both failed when only a label-shaped `title`
    reached the index) — falling back to `title` when `index_line` is
    absent, and omitted entirely (renders tier-B style, trigger + id only)
    when neither is present. Never falls back to any rule-derived text.

    `applies_when` is sanitized before use, same as the imperative, and
    `entity_id` is cut to the id charset — every row-derived field is
    untrusted (Falco's finding applies to the whole row, not only the
    imperative). `body` still carries
    the raw `rule` text for the FUTURE MCP Skills-extension transport (an
    agent that explicitly fetches a rule by id gets the real text, which is
    fine — that path is not the SessionStart stdout stream this finding is
    about), but nothing in this module ever reads `.body` back out into the
    rendered index; `render_index_text`/`_assemble`/`_preamble_lines`/
    `_conditional_line_*` only ever touch `.description`/`.applies_when`/
    `.entity_id`.
    """
    raw_entity_id = str(snap.get("_entity_id") or snap.get("entity_id") or "")
    # The server assigns entity ids from a fixed charset; nothing outside it
    # (spaces, brackets, markup) can be legitimate, so it is dropped rather
    # than sanitized (Falco, ateles#1268 round 3).
    entity_id = _ENTITY_ID_DISALLOWED.sub("", raw_entity_id)[:64]
    if not entity_id:
        return None  # an id we cannot safely print is a row we cannot cite

    raw_applies_when = str(snap.get("applies_when") or "")
    applies_when = _sanitize_field(raw_applies_when, max_len=_APPLIES_WHEN_MAX)
    # Preamble is decided on the RAW value, whitespace-stripped — never on
    # the sanitized one. Sanitizing deletes markup, so "always" wrapped in a
    # comment or a heading marker would otherwise promote a row to
    # always-applies: the broader reading on the field that carries the
    # safety meaning (principles.md #5; Falco, ateles#1268 round 3).
    is_preamble = raw_applies_when.strip().lower() == _ALWAYS

    # `index_line` (schema 1.4.0) is a dedicated short-form field for the
    # one-liner's operative constraint; `title` is the row's display name
    # and is kept as the fallback so a row authored before `index_line`
    # existed still renders (ateles#1295 follow-up, ateles#1301 eval).
    raw_index_line = str(snap.get("index_line") or "")
    raw_title = str(snap.get("title") or "")
    imperative = _sanitize_field(raw_index_line, max_len=_IMPERATIVE_MAX) or (
        _sanitize_field(raw_title, max_len=_IMPERATIVE_MAX)
    )

    if not applies_when and not is_preamble:
        # No usable trigger and not the literal "always" — nothing safe to
        # render this row AS (it cannot be preamble, and a conditional row
        # with an empty trigger has no recognition key at all).
        if not imperative:
            return None
        applies_when = ""  # renders as "(trigger not recorded)" downstream

    trigger = applies_when if applies_when else "(trigger not recorded)"
    if is_preamble:
        description = imperative if imperative else "(no summary recorded)"
    elif imperative:
        description = f"When {trigger}: {imperative}"
    else:
        description = f"When {trigger}:"  # tier-B shape even at tier A

    domain = str(snap.get("domain") or "")
    rule = str(snap.get("rule") or "")
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


def render_skills(
    rows: list[dict],
    principal: str | None = None,
    *,
    agent_definition_id: "str | None" = None,
    governs: "dict[str, frozenset[str]] | None" = None,
) -> list[PolicySkill]:
    """Session-scoped rows, projected to PolicySkill, preamble first.

    Scoping is `_session_scope_ok(row, principal, agent_definition_id=,
    governs=)`; `principal=None` means the session principal from the
    environment (see `session_principal`).

    `agent_definition_id` and `governs` are resolved ONCE here (not once per
    row) when the caller has not already supplied them — a `GOVERNS` fetch
    or an `agent_definition` lookup per row would turn one session-start
    read into N. Both degrade to "resolve/bind nothing" on a Neotoma
    failure rather than raising: a caller that cannot reach the edge data
    still gets the swarm-wide (`global`/`swarm`) rows, which is the same
    fail-open-on-transport / fail-closed-on-data posture every other read
    in this module takes.

    Rows that sanitize to nothing renderable are SKIPPED, not raised —
    Falco's fix says "skip the row and count it," and one malformed or
    adversarial row must not take down the whole index. The count is
    logged at WARNING so a real data problem (not just an attack) is still
    visible.
    """
    resolved_governs = governs
    if resolved_governs is None:
        try:
            resolved_governs = fetch_governs_edges(NEOTOMA_BASE_URL, _request=_request)
        except Exception as exc:  # noqa: BLE001 — degrade to edgeless, don't raise
            log.warning(
                "could not fetch GOVERNS edges for the session index: %s: %s — "
                "treating every row as edgeless",
                type(exc).__name__, exc,
            )
            resolved_governs = {}

    resolved_definition_id = agent_definition_id
    if resolved_definition_id is None:
        sub = session_principal() if principal is None else principal.strip()
        if sub:
            try:
                resolved_definition_id = resolve_agent_definition_id(
                    sub, NEOTOMA_BASE_URL, _request=_request
                )
            except Exception as exc:  # noqa: BLE001 — unresolvable, don't raise
                log.warning(
                    "could not resolve agent_definition id for %r: %s: %s",
                    sub, type(exc).__name__, exc,
                )
                resolved_definition_id = None

    scoped = [
        r
        for r in rows
        if _session_scope_ok(
            r,
            principal,
            agent_definition_id=resolved_definition_id,
            governs=resolved_governs,
        )
    ]
    skills: list[PolicySkill] = []
    skipped = 0
    for r in scoped:
        skill = to_skill(r)
        if skill is None:
            skipped += 1
            continue
        skills.append(skill)
    if skipped:
        log.warning(
            "agent_policy index: skipped %d row(s) with nothing safe to "
            "render after sanitising (empty/unusable applies_when, title, "
            "or entity id).",
            skipped,
        )
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


# --- Audience split (ateles#1254 follow-up, task ent_3b6b010658ce88f6be9e19ac) ---
#
# "Session-only" vs. "reaches both sessions and agents" maps onto the SAME
# vocabulary `_session_scope_ok` already reads, rather than a new concept:
#   - `scope in POLICY_SCOPES_REACHING_EVERY_AGENT` ({"global", "swarm"}) —
#     the row reaches every dispatched agent AND every interactive session.
#     Both audiences. Compact line.
#   - `scope == "agent"` — the row binds exactly the one agent named by
#     `agent_sub` (which, by the time it is in `skills` at all, is this
#     session's own principal — `_session_scope_ok` already filtered out
#     every other agent's row). Session-only in the sense that matters here:
#     it does not fan out to the rest of the swarm. Full line.
# A row with neither shape never reaches `skills` (`_session_scope_ok` fails
# closed on an unrecognized/absent `scope`), so this split has no third case
# to default — it only ever sees the two scopes `POLICY_SCOPES` admits.
_COMPACT_SUMMARY_MAX = 80


def _truncate_at_word_boundary(text: str, max_len: int) -> str:
    """Cut `text` to at most `max_len` chars, never mid-word. If the whole
    string already fits, return it unchanged (no ellipsis). Otherwise cut at
    the last space at or before `max_len - 1` (room for the ellipsis) and
    append "…"; if there is no space in that span (one very long word), a
    hard character cut is the only option left and gets the ellipsis too —
    still capped at `max_len`, just not word-clean.
    """
    if len(text) <= max_len:
        return text
    truncated = text[: max_len - 1]
    boundary = truncated.rfind(" ")
    if boundary > 0:
        truncated = truncated[:boundary]
    return truncated.rstrip() + "…"


def _is_both_audience(s: PolicySkill) -> bool:
    """True when the row's scope reaches every agent AND every session —
    the swarm-wide scopes `agent_loader.POLICY_SCOPES_REACHING_EVERY_AGENT`
    already names, read here rather than re-derived (CLAUDE.md: "extend the
    mechanism that already generalizes; do not build a parallel one")."""
    return s.scope.strip().lower() in POLICY_SCOPES_REACHING_EVERY_AGENT


def _conditional_line_audience_aware(s: PolicySkill) -> str:
    """Tier A2: session-only rows (`scope == "agent"`) get the SAME full
    line as tier A (trigger + imperative + id) — there is exactly one
    session reading them, so compacting buys nothing. Both-audience rows
    (`scope` in {"global", "swarm"}) get a compact line: trigger + summary
    truncated at a word boundary + id, dropping the "When "/"" framing
    tier A's `description` carries so the truncation budget goes to content.
    """
    if not _is_both_audience(s):
        return _conditional_line_full(s)
    trigger = s.applies_when if s.applies_when else "(trigger not recorded)"
    summary = s.description
    # `description` already carries "When <trigger>: <imperative>" (or just
    # the imperative shape) — strip the redundant trigger restatement so the
    # compact form is `trigger | truncated-summary`, not a doubled trigger.
    prefix = f"When {trigger}: "
    if summary.startswith(prefix):
        summary = summary[len(prefix) :]
    summary = _truncate_at_word_boundary(summary, _COMPACT_SUMMARY_MAX)
    return f"- When {trigger}: {summary} [{s.entity_id}]"


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
    """The plain-text session index, degrading through four tiers rather
    than failing open on size (operator ruling, ateles#1261 follow-up: size
    must DEGRADE, never fail open — fail-open is reserved for Neotoma being
    unreachable or rendering itself raising, never merely for being over
    budget).

    Tier A:  full conditional lines (trigger + imperative + id) for EVERY
             row, regardless of scope.
    Tier A2: audience split (ateles#1254 follow-up). Same preamble; a
             session-only row (`scope == "agent"`, bound to exactly this
             session's principal) keeps the tier-A full line, since there is
             only one audience reading it — compacting it buys nothing. A
             both-audience row (`scope` in {"global", "swarm"} — reaches
             every dispatched agent AND every session,
             `agent_loader.POLICY_SCOPES_REACHING_EVERY_AGENT`) gets a
             compact line instead: trigger + summary truncated at a word
             boundary + id. Only reached when tier A overflows the budget;
             a corpus that already fits tier A never pays this cost.
    Tier B:  trigger + id only for every row, same preamble — the
             imperative/summary is dropped entirely regardless of scope.
    Tier C:  as many WHOLE tier-B lines as fit — mandatory rules first —
             plus one line naming how many were omitted. Never cuts a line
             mid-way; only DROPS whole lines, and only ever at tier C.

    The chosen tier is tagged in a trailing `<!-- tier: A|A2|B|C -->` comment
    so a session or a test can see which one ran. Always returns a string
    that fits `budget_chars` — the empty-corpus case (no preamble, no
    conditional) trivially fits at tier A and is not a special case here.
    """
    preamble = [s for s in skills if s.is_preamble]
    conditional = [s for s in skills if not s.is_preamble]

    # --- Tier A ---
    tier_a = _assemble(preamble, [_conditional_line_full(s) for s in conditional], "A")
    if len(tier_a) <= budget_chars:
        return tier_a

    # --- Tier A2: audience split — compact the both-audience rows only ---
    tier_a2 = _assemble(
        preamble, [_conditional_line_audience_aware(s) for s in conditional], "A2"
    )
    if len(tier_a2) <= budget_chars:
        both_audience_count = sum(1 for s in conditional if _is_both_audience(s))
        log.warning(
            "agent_policy index: tier A (%d chars) exceeded the %d-char "
            "budget; degraded to tier A2 (%d chars, %d of %d conditional "
            "rules compacted — both-audience scope, session-only rows kept "
            "full).",
            len(tier_a), budget_chars, len(tier_a2), both_audience_count,
            len(conditional),
        )
        return tier_a2

    # --- Tier B ---
    tier_b = _assemble(
        preamble, [_conditional_line_trigger_only(s) for s in conditional], "B"
    )
    if len(tier_b) <= budget_chars:
        log.warning(
            "agent_policy index: tier A (%d chars) and tier A2 (%d chars) "
            "both exceeded the %d-char budget; degraded to tier B (%d "
            "chars, %d conditional rules, imperative dropped, trigger + id "
            "kept).",
            len(tier_a), len(tier_a2), budget_chars, len(tier_b), len(conditional),
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
