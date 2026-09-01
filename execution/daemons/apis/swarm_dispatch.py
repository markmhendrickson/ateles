"""
execution/daemons/apis/swarm_dispatch.py — GitHub-event dispatch pipelines.

The orchestration layer behind the GitHub webhook gateway (ateles#80):

  issue.opened     → harness_event → Lanius (new-issue protocol: init gates,
                     assign Pavo, label) → ORDERED ADDITIVE SPEC sequence:
                     PM (Pavo) → Design/UX (Accipiter) → Eng (Cicada) →
                     QA (Phoenicurus) → Security (Waxwing) → Legal (Buteo).
                     PM/Eng/QA always run; Design/Security/Legal run only when
                     their lens is selected. Each agent reads the accumulated
                     spec-so-far and adds/replaces ONLY its own section; the
                     spec lives in a Neotoma `issue_spec` entity (one per issue,
                     keyed `<repo>#<n>`) and is mirrored into the issue BODY
                     between managed markers. Comments carry verdicts only.
                     When ATELES_SWARM_AUTO_BUILD is ON and gates are green, the
                     sequence chains into an implementation PR (Cicada build) so
                     the PR pipeline takes over; OFF (default) = notify-and-wait.
  pull_request.*   → harness_event → Lanius (PR gate inheritance; stop if a
                     pre-impl gate is open) → review panel (neotoma#1640) →
                     learning pass (ateles#82) → Vanellus aggregation →
                     blocking checkpoint_brief before merge (autonomy
                     guardrail)

Mechanical state transitions are DISPATCHER-SIDE WRITES, not agent-prompt work
(ateles#285). A deterministic mutation with no judgement in it — the
`/confirm-gates-clear` gate waive being the canonical case — is performed
directly by the dispatcher and then VERIFIED by re-reading the entity, because
an LLM turn asked to persist a mechanical mutation can silently no-op or
partially apply (and did, three times, on ateles#241). See `gate_waive.py`.

Autonomy guardrail (ateles#80): read-only gates run unattended — they only
write Neotoma metadata and GitHub comments, both reversible. Side-effecting
steps stay operator-gated by default: Vanellus reviews and aggregates but
does NOT merge unless APIS_AUTONOMY_AUTO_MERGE=1; the merge boundary gets a
blocking checkpoint_brief plus an operator_decision notification instead.
The auto-build handoff (issue → implementation PR) is separately gated behind
ATELES_SWARM_AUTO_BUILD (default OFF); it NEVER auto-merges — merge stays
behind APIS_AUTONOMY_AUTO_MERGE regardless.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx

from gate_waive import (
    CLEARED_GATE_STATES,
    IssueGateStore,
    WaiveOutcome,
    format_waive_comment,
)
from github_gateway import SwarmTrigger
from issue_spec import (
    SECTIONS,
    SPEC_MARKER_END,
    SPEC_MARKER_START,
    IssueSpecStore,
    SpecSection,
    SpecState,
    assemble_spec_markdown,
    splice_managed_block,
)
from review_learning import (
    DEFAULT_OWNER,
    OWNER_BY_LENS,
    ReviewFinding,
    parse_findings,
    propose_skill_updates,
)
from review_panel import (
    LENSES,
    Lens,
    lens_by_name,
    resolve_lens_provider,
    select_expectation_agents,
    select_panel,
)
from skill_runner import (
    SkillResult,
    run_skill,
    usable_providers,
    write_dispatch_failure_log,
)

from lib.daemon_runtime.checkpoint_posture import PostureOutcome, evaluate_with_posture
from lib.daemon_runtime.gating import load_policy
from lib.notify import Notifier, Priority

log = logging.getLogger("apis.swarm_dispatch")

DAEMON_NAME = "apis"

_PARENT_ISSUE = re.compile(r"\b(?:closes|fixes|resolves)\s+#(\d+)", re.I)
_GATE_VERDICT = re.compile(r"GATE_INHERITANCE:\s*(clear|blocked)", re.I)

# ateles#232 QA finding: `reopened` (github_gateway.py maps it to kind
# "pr_reopened", distinct from "pr_synchronize") re-enters the pipeline the
# same way a re-push does, so it must be eligible for the same auto-re-review
# treatment as a synchronize push against a gate-blocked PR.
_AUTO_REREVIEW_TRIGGER_KINDS = ("pr_synchronize", "pr_reopened")

# Product-code path classifier (ateles: harden pipeline-bypass detection).
# A PR that touches these paths is a product change that SHOULD originate from a
# gated issue routed through Cicada. When such a PR has NO parent issue
# (`Closes #N`), it bypassed the pm/arch pre-impl gates: Lanius still fails open
# for review (merge stays operator-gated), but the bypass must be LOUD, not
# silent. Matches source/API/schema/migration/CLI surfaces across repos; excludes
# pure docs, tests, and config-only diffs (those are lower-stakes and legitimately
# land without the full pipeline).
_PRODUCT_CODE_RE = re.compile(
    r"(?:^|/)(?:src|packages|app|server|lib|execution)/"
    r"|(?:^|/)(?:schema|schemas|migrations)/"
    r"|(?:^|/)openapi\.ya?ml$"
    r"|\.(?:ts|tsx|js|jsx|py|go|rs)$",
    re.I,
)
# Paths that, even if they match above, are not product code on their own.
_NON_PRODUCT_CODE_RE = re.compile(
    r"(?:^|/)(?:docs|tests?|__tests__|\.github|\.claude)/"
    r"|\.(?:test|spec)\.(?:ts|tsx|js|jsx|py)$"
    r"|(?:^|/)test_[^/]+\.py$",
    re.I,
)


def touches_product_code(changed_files: list[str]) -> bool:
    """True when the diff includes at least one product-code file.

    A file counts as product code when it matches `_PRODUCT_CODE_RE` and is not
    excluded by `_NON_PRODUCT_CODE_RE` (docs/tests/CI/config). Used to decide
    whether a parent-issue-less PR is a pipeline bypass worth surfacing loudly.
    An empty/unknown file list returns False (fail-open: don't cry bypass when we
    could not even fetch the diff).
    """
    for path in changed_files:
        if _NON_PRODUCT_CODE_RE.search(path):
            continue
        if _PRODUCT_CODE_RE.search(path):
            return True
    return False

# Operator command that clears (waives) all unsigned pre-impl gates on a PR's
# parent issue so the PR pipeline can proceed.  Only the operator login may
# issue this command (ateles#112 guardrail).
_CONFIRM_GATES_CLEAR_CMD = "/confirm-gates-clear"

# Operator command that re-runs the issue pipeline (Lanius triage + expectation
# pre-registration + Pavo scoping) on an EXISTING issue.  Useful for the
# operator's iteration-test rhythm: after each design increment, comment
# /swarm-run on the issue to re-drive the pipeline without having to close and
# re-open the issue.  Only _OPERATOR_LOGIN may invoke this command.
#
# Re-runs rely on Lanius's idempotent triage and Pavo's idempotent scoping:
# they edit-not-duplicate their own comments and correct-not-recreate the
# Neotoma gate entities.  Review expectations are re-posted as new comments
# (not idempotent) — acceptable for iteration testing.
_SWARM_RUN_CMD = "/swarm-run"

# Phase H1 — HITL checkpoint verdict commands (docs/swarm_hitl_checkpoints_design.md).
# These are the uniform operator confirm/reject/hold verbs for any blocking checkpoint.
# For the pre-merge checkpoint specifically:
#   /approve — the operator approves the pending merge checkpoint.  The dispatcher
#              resolves the checkpoint_brief, posts a confirmation, removes the
#              operator from the PR reviewer list, and hands back to Vanellus.
#              Note: the dispatcher does NOT auto-merge (conservative choice — see
#              _handle_approve docstring).  The operator may then merge on GitHub.
#   /reject  — reject the pending checkpoint; record the reason (everything after
#              "/reject "); resolve the checkpoint_brief as rejected; remove the
#              operator from reviewers.  Does NOT proceed with the held action.
#   /hold    — acknowledge (parked); leave the checkpoint blocking and the operator
#              still requested as reviewer.  No state change beyond the ack.
#
# Priority among all commands: /confirm-gates-clear wins if also present (it is
# the gate-inheritance unblock, more time-sensitive than a merge verdict).
# Among H1 commands alone: /approve > /reject > /hold (first match dispatched).
_APPROVE_CMD = "/approve"
_REJECT_CMD = "/reject"
_HOLD_CMD = "/hold"

# Correlation token embedded in the "READY TO MERGE" notification email so an
# operator email REPLY carrying "APPROVE" can be matched back to a specific PR
# (approval loop). Turdus parses this token out of the reply and POSTs it to the
# Apis gateway's /approve-email route. Format: `swarm-approve: <owner/repo>#<n>`.
# NB: deliberately NOT a slash-command — it must never match the issue-comment
# command guards, and it is inert if it ever appears in a GitHub comment.
_APPROVE_EMAIL_TOKEN_PREFIX = "swarm-approve:"


def format_approve_token(repository: str, pr_number: int) -> str:
    """The correlation line placed in a merge-ready notification email."""
    return f"{_APPROVE_EMAIL_TOKEN_PREFIX} {repository}#{pr_number}"


def parse_approve_token(text: str) -> tuple[str, int] | None:
    """Extract (repository, pr_number) from a `swarm-approve: owner/repo#N` line.

    Returns None when no well-formed token is present. Tolerant of surrounding
    quoting/whitespace an email client may add. Repo must be `owner/name` shape.
    """
    m = re.search(
        rf"{re.escape(_APPROVE_EMAIL_TOKEN_PREFIX)}\s*"
        r"([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#(\d+)",
        text,
    )
    if not m:
        return None
    return m.group(1), int(m.group(2))

# Pre-impl gates that must be signed off before the PR review panel runs.
# These are the gates Lanius checks for GATE_INHERITANCE.
#
# ateles#285: `ux` was MISSING from this tuple even though the Lanius skill,
# the workflow phase table (Phase 2 = Accipiter `ux` + Bombycilla `arch`), and
# _lanius_pr_prompt ("If pm/ux/arch are all satisfied there …") all treat it as
# a pre-impl gate. That omission is the mechanical cause of the 2026-07-23
# PARTIAL waive on ateles#241: the sweep waived `arch`, left `ux` pending, and
# PR gate inheritance kept blocking on a gate the operator believed cleared.
PRE_IMPL_GATES = ("pm", "ux", "arch")

# Operator GitHub login — only this login may waive gates via the comment
# command.  Defaults to the repo owner; override with APIS_OPERATOR_LOGIN.
_OPERATOR_LOGIN = os.environ.get("APIS_OPERATOR_LOGIN", "markmhendrickson")

# Bot/machine-account identities whose comments must NEVER trigger swarm
# commands, regardless of comment content.  This is the structural guard
# against self-trigger feedback loops (neotoma#1686): a swarm confirmation
# comment that contains a command token (e.g. "swarm-run") would re-fire the
# handler via its own webhook if only the operator-login positive check were
# present.  Known bot patterns:
#   • Exact machine-account names (lowercase comparison)
#   • The <operator>-ateles-<agent> naming convention for per-agent accounts
#   • GitHub Apps / Actions suffixes
_BOT_EXACT_LOGINS: frozenset[str] = frozenset({
    "ateles-agent",
    "neotoma-agent",
    "github-actions",
})
_BOT_SUFFIX = "[bot]"
_BOT_INFIX_RE = re.compile(r"-ateles-")


def _is_bot_author(login: str) -> bool:
    """Return True when *login* is a known swarm/machine identity.

    Used to short-circuit command dispatch before any positive-allowlist check:
    a bot-authored comment must never trigger swarm commands even when it
    happens to contain a command token (neotoma#1686 self-trigger defence).

    Pattern coverage:
      - Exact known machine accounts (ateles-agent, neotoma-agent, github-actions)
      - Per-agent accounts: any login containing "-ateles-" (operator-fork-safe)
      - GitHub App/Actions suffix: any login ending in "[bot]"
    """
    lower = login.lower()
    if lower in _BOT_EXACT_LOGINS:
        return True
    if lower.endswith(_BOT_SUFFIX):
        return True
    if _BOT_INFIX_RE.search(lower):
        return True
    return False


# Marker the pre-registration pass embeds in issue comments so the PR
# pipeline can recover which agents pre-registered (and what they promised
# to check) straight from GitHub — no Neotoma query in the hot path.
EXPECTATION_MARKER = "review_expectation"


def content_digest(entities: list[dict]) -> str:
    """Stable digest of an entity payload, so the store idempotency key
    changes whenever the content does. A bare delivery-id key 400s
    (ERR_IDEMPOTENCY_MISMATCH) when the same delivery is re-dispatched with a
    fresh occurred_at — observed on the PR-87 self-dogfood run."""
    blob = json.dumps(entities, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


# Phrases that mark a line as the agent narrating ABOUT its section rather than
# being the section content. Matched case-insensitively at line start.
_NARRATION_PREFIXES: tuple[str, ...] = (
    "perfect.",
    "perfect,",
    "great.",
    "done.",
    "i have successfully",
    "i've successfully",
    "i have written",
    "i've written",
    "i have completed",
    "i've completed",
    "i have created",
    "i have drafted",
    "i have now",
    "i have added",
    "here is my",
    "here's my",
    "here is the",
    "here's the",
    "below is my",
    "the following is",
    "the section is ready",
    "this section is ready",
    "the section is complete",
    "in summary,",
    "to summarize",
)
_NARRATION_SUMMARY_HEADINGS: tuple[str, ...] = ("## summary", "### summary")


def _is_narration_line(line: str) -> bool:
    """True when a line is the agent narrating about its work, not spec content."""
    low = line.strip().lower()
    if not low:
        return False
    if low in _NARRATION_SUMMARY_HEADINGS:
        return True
    return any(low.startswith(p) for p in _NARRATION_PREFIXES)


def _is_only_narration(text: str) -> bool:
    """True when, after sanitizing, nothing but narration/meta remains.

    Used by _extract_section_text to reject a section that is a DESCRIPTION of a
    spec ("I have successfully completed the Engineering section…") rather than
    an actual spec, so the section is flagged not-produced instead of storing
    garbage the implementer can't build from (root cause of #1882's no-op build).
    """
    stripped = (text or "").strip()
    if not stripped:
        return True
    content_lines = [
        ln.strip()
        for ln in stripped.splitlines()
        if ln.strip() and not _is_narration_line(ln.strip())
    ]
    if not content_lines:
        return True
    joined = " ".join(content_lines)
    low = joined.lower()
    # A bare "[agent] pull_request_link: BLOCKED …" status is not a usable spec.
    if "pull_request_link" in low and len(content_lines) <= 2:
        return True
    # Minimum-substance floor: a real spec section is more than a stray fragment
    # left over after narration is stripped (e.g. a single dangling list item
    # "1. Specifies files to touch"). Require at least 2 content lines OR ~60
    # chars of substance. This is what catches the #1882 Engineering remnant.
    if len(content_lines) < 2 and len(joined) < 60:
        return True
    return False


_PR_URL_RE = re.compile(
    r"https://github\.com/([\w.\-]+/[\w.\-]+)/pull/(\d+)", re.IGNORECASE
)


def _extract_pr_url(stdout: str, repository: str) -> str | None:
    """Return a github.com PR URL for ``repository`` found in ``stdout``, else None.

    Cicada reports the PR it opened as a github.com/<repo>/pull/<n> link; we
    only accept one whose <repo> matches the target so a stray link to another
    repo isn't mistaken for the build PR.
    """
    for m in _PR_URL_RE.finditer(stdout or ""):
        if m.group(1).lower() == repository.lower():
            return m.group(0)
    return None


def _sanitize_section_body(text: str, section: SpecSection) -> str:
    """Clean an agent's raw section text before it is stored + mirrored.

    Agents sometimes echo (a) the managed spec markers and (b) their own
    section heading. The assembler re-adds the ``### <heading>`` and the mirror
    re-wraps the markers, so echoing them produces duplicated headings and
    nested markers in the issue body (observed on #180's first live run). Strip
    both defensively so the stored section is *content only*:

      * remove any ``SPEC_MARKER_START`` / ``SPEC_MARKER_END`` lines the agent
        pasted, plus the "## Swarm specification" preamble the assembler owns;
      * drop a leading heading line that matches this section's own heading
        (in ``#``/``##``/``###`` form) so the assembler's single ``###`` stands
        alone.

    Idempotent and conservative: only the agent's OWN leading heading is
    removed; body sub-headings and other content are preserved verbatim.
    """
    if not text:
        return ""
    cleaned_lines: list[str] = []
    heading = (section.heading or "").strip().lower()
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        # Drop echoed managed markers and the assembler-owned preamble line.
        if SPEC_MARKER_START in line or SPEC_MARKER_END in line:
            continue
        if stripped == "## Swarm specification":
            continue
        cleaned_lines.append(line)
    # Trim leading blank lines, then drop a leading heading that duplicates
    # this section's own heading (the assembler will add exactly one).
    while cleaned_lines and not cleaned_lines[0].strip():
        cleaned_lines.pop(0)
    if cleaned_lines:
        first = cleaned_lines[0].strip()
        if first.startswith("#"):
            first_text = first.lstrip("#").strip().lower()
            if heading and first_text == heading:
                cleaned_lines.pop(0)
                while cleaned_lines and not cleaned_lines[0].strip():
                    cleaned_lines.pop(0)
    # Drop leading conversational narration lines (the agent talking ABOUT its
    # work rather than the work itself). Only strips from the top so real spec
    # content and body sub-headings are preserved.
    while cleaned_lines:
        first = cleaned_lines[0].strip()
        if first and _is_narration_line(first):
            cleaned_lines.pop(0)
            while cleaned_lines and not cleaned_lines[0].strip():
                cleaned_lines.pop(0)
            continue
        break
    return "\n".join(cleaned_lines).strip()


def parse_gate_verdict(stdout: str) -> str | None:
    """Extract Lanius's GATE_INHERITANCE verdict; None when absent."""
    m = _GATE_VERDICT.search(stdout or "")
    return m.group(1).lower() if m else None


# Lanius names the still-unsigned pre-impl gates on a `blocked` verdict, e.g.
# `GATE_PENDING: arch,ux`. The dispatcher feeds these to select_panel so the
# owning lenses are guaranteed a panel seat under the cap (ateles#230 gap: a
# carried-over gate could never clear because its lens was never re-invoked).
_GATE_PENDING = re.compile(r"GATE_PENDING:\s*([a-z0-9_,\s]+)", re.I)


def parse_pending_gates(stdout: str) -> set[str]:
    """Extract the gates Lanius reported as still-pending; empty when absent."""
    m = _GATE_PENDING.search(stdout or "")
    if not m:
        return set()
    return {g.strip().lower() for g in m.group(1).split(",") if g.strip()}


# Vanellus / panelist verdict token (SWARM_GITHUB_CONTRACT, skill_runner.py):
# a review comment carries exactly one of these bold verdict tokens.
_REVIEW_VERDICT = re.compile(
    r"\*\*(APPROVE|REQUEST_CHANGES|COMMENT|BLOCKED)\*\*", re.I
)


# Vanellus writes this verbatim when a declared lens produced no verdict for a
# round. Shared by the parsers and the recovery sweep so the two cannot drift.
NOT_RECEIVED_TOKEN = "NOT RECEIVED"

# The steward writes this verbatim when a merge it was authorized to perform was
# refused by a control outside the autonomy model — a local tool-permission
# classifier, a missing grant, a branch-protection denial. ateles#565: on PR
# #452 that refusal was recorded only in a review body, so it reached nobody and
# the PR sat APPROVED and CLEAN for eleven days. Shared by the prompt and the
# parser so the two cannot drift.
MERGE_REFUSED_MARKER = "MERGE REFUSED:"

# The durable outcomes of a merge attempt. `authorized_but_unable` is the state
# ateles#565 says is missing: the autonomy flag AUTHORIZED the merge and a
# separate control denied the mechanism. From the outside that was previously
# indistinguishable from the flag simply being off, which is why nobody noticed.
MERGE_STATE_AUTHORIZED_BUT_UNABLE = "authorized_but_unable"
MERGE_STATE_NOT_AUTHORIZED = "not_authorized"

# The lens labels a panel can actually produce. Bounding the prose parser to
# these means a malformed aggregation yields nothing rather than a fabricated
# lens name — absence of a verdict must never be inferred loosely.
_KNOWN_LENS_NAMES = frozenset(
    {"pm", "ux", "arch", "qa", "security", "legal", "content"}
)

def _stamp_review_model(result) -> str:
    """Prefix a review verdict with the provider/model that produced it.

    A verdict is evidence, and evidence needs a provenance line. When the panel
    falls back to a weaker model the resulting APPROVE or CHANGES_REQUESTED is
    not equivalent to one from the intended model — but downstream (the
    aggregation agent, the operator reading the PR, any future merge policy)
    sees only text. Without this stamp a degraded review is indistinguishable
    from a full-strength one, which is the "reports success while producing
    nothing" failure in a new costume.

    Returns the body unchanged when the provider is unknown, so this can never
    swallow a review.
    """
    body = result.stdout or ""
    provider = getattr(result, "provider", "") or ""
    if not provider:
        return body
    model = getattr(result, "model", "") or "(provider default)"
    return f"_Reviewed by {provider}/{model}._\n\n{body}"


_BLOCKING_COUNT_RE = re.compile(r"Blocking:\s*(\d+)", re.IGNORECASE)

# `pm: NOT RECEIVED`, `**security:** NOT RECEIVED`, `security = NOT RECEIVED`.
# Bounded to one line so a paragraph mentioning two lenses cannot pair the wrong
# name with the token.
_NOT_RECEIVED_RE = re.compile(
    r"\b(?P<lens>" + "|".join(sorted(_KNOWN_LENS_NAMES)) + r")\b"
    r"\s*\**\s*[:=]\s*\**\s*" + NOT_RECEIVED_TOKEN,
    re.IGNORECASE,
)

# Prose form Vanellus used on neotoma#2153: "the security lens verdict is
# missing for this round". Deliberately does NOT accept an arbitrary word before
# "lens": "every declared lens reported; no lens is missing" would otherwise
# yield a lens called `declared`, and a false positive here re-dispatches an
# agent that never needed running. Only names the panel actually has count, so
# the parser cannot invent a lens.
_MISSING_LENS_PROSE_RE = re.compile(
    r"\b(?P<lens>" + "|".join(sorted(_KNOWN_LENS_NAMES)) + r")\s+lens\b"
    r"[^.;\n]{0,60}?"
    r"\b(?:verdict\s+is\s+missing|is\s+missing|not\s+received)\b",
    re.IGNORECASE,
)


def parse_blocking_count(text: str) -> int | None:
    """The `Blocking: N` count from an aggregation, or None if absent."""
    m = _BLOCKING_COUNT_RE.search(text or "")
    return int(m.group(1)) if m else None


def parse_not_received_lenses(text: str) -> list[str]:
    """Lens names an aggregation marks as having produced no verdict.

    ateles#431. Order-preserving and deduplicated. Matches the structured
    `<lens>: NOT RECEIVED` forms and the prose form Vanellus used on
    neotoma#2153 ("the security lens verdict is missing for this round").
    """
    seen: list[str] = []
    body = text or ""
    for pattern in (_NOT_RECEIVED_RE, _MISSING_LENS_PROSE_RE):
        for m in pattern.finditer(body):
            lens = m.group("lens").strip().lower()
            # `the`/`this` etc. can precede "lens" in prose; ignore stop-words
            # rather than loosening the regex and inventing lens names.
            if lens in {"the", "a", "one", "this", "that", "any", "each", "no"}:
                continue
            if lens not in seen:
                seen.append(lens)
    return seen


def is_missing_lens_candidate(aggregation_body: str) -> bool:
    """True when an aggregation cleared on content but withheld for a lens.

    The exact neotoma#2153 shape: `Blocking: 0` from the lenses that DID
    report, merge withheld solely because a declared lens never answered.

    Both halves are required. A `Blocking: 0` with every lens present is simply
    clear; a missing lens alongside real blockers is the blockers' problem, and
    re-running the absent lens would not unblock it.
    """
    return (
        parse_blocking_count(aggregation_body) == 0
        and bool(parse_not_received_lenses(aggregation_body))
    )


def parse_review_verdict(stdout: str) -> str | None:
    """Extract the aggregated review verdict from Vanellus's output.

    Returns one of "approve" | "request_changes" | "comment" | "blocked", or
    None when no verdict token is present (treat None as not-clear — never
    silently proceed to a merge-ready signal on an unparseable verdict).

    Vanellus is instructed to repeat its full aggregated verdict inline in
    stdout (swarm_dispatch `_vanellus_prompt`), so the token is reliably here
    even when its `gh` comment post fails.
    """
    m = _REVIEW_VERDICT.search(stdout or "")
    return m.group(1).lower() if m else None


def review_verdict_is_clear(verdict: str | None) -> bool:
    """True only for an unambiguous non-blocking verdict (APPROVE or COMMENT).

    REQUEST_CHANGES / BLOCKED / None (unparseable) are all NOT clear — the PR is
    not merge-ready and blocking findings should route back for a fix.

    Reads the TOKEN ONLY. Callers deciding the merge path must use
    `review_blocks_merge`, which also consults the body — see its docstring.
    """
    return verdict in ("approve", "comment")


def review_blocks_merge(verdict: str | None, body: str | None = None) -> bool:
    """True when a review must route findings back instead of gating for merge.

    The single predicate the merge path branches on. Extracted because the two
    surfaces that act on the same review had drifted: `_emit_formal_review`
    adopted the ateles#595 body cross-check while the routing branch in
    `_handle_pr` still tested `review_verdict_is_clear(verdict)` alone. Since
    `review_verdict_is_clear("comment")` is True, a Vanellus `**COMMENT**` above
    a `[BLOCKING]` finding submitted REQUEST_CHANGES to GitHub — blocking the PR
    under branch protection — while the dispatcher filed the operator's
    merge-ready checkpoint and never routed the finding for a fix. Under
    APIS_AUTONOMY_AUTO_MERGE=1 it was worse than wrong, it was silent:
    `_gate_merge_readiness` early-returns, so the blocker produced no
    checkpoint, no fix round and no page.

    Blocks when EITHER input says so: the body carries a `[BLOCKING]` marker, or
    the token itself is not clear. The body half is the ateles#595 authority
    (a lens's own structured finding outranks its self-reported token); the token
    half preserves the pre-existing behaviour for REQUEST_CHANGES / BLOCKED /
    unparseable.

    Related to but deliberately NOT equal to
    `verdict_to_review_event(...) == REQUEST_CHANGES`. The ateles#241 asymmetry
    keeps `blocked` on a clean body mapped to the inert COMMENT — GitHub review
    state stays unescalated — yet such a review must still route findings back.
    So the invariant between the two surfaces is an IMPLICATION, not an
    equality: emitting REQUEST_CHANGES implies blocking the merge, never the
    converse. `test_request_changes_event_implies_blocking_the_merge_path`
    enforces it over every verdict x body-shape pair.
    """
    return body_has_blocking_findings(body) or not review_verdict_is_clear(verdict)


# GitHub's Reviews API accepts exactly these three events.
_REVIEW_EVENT_APPROVE = "APPROVE"
_REVIEW_EVENT_REQUEST_CHANGES = "REQUEST_CHANGES"
_REVIEW_EVENT_COMMENT = "COMMENT"


def parse_merge_refusal(text: str | None) -> str | None:
    """The stated reason a merge was refused, or None when none was reported.

    ateles#565. The steward is instructed (see `merge_authorization_clause`) to
    lead the line with `MERGE REFUSED:` when a control outside the autonomy
    model denies the merge, so a refusal that previously died in a review body
    becomes a parseable signal the dispatcher can escalate.

    Bounded to one line: a refusal reason is a sentence, and letting it run to
    the end of a long aggregation would page the operator with the whole review.
    """
    if not text:
        return None
    m = _MERGE_REFUSED_RE.search(text)
    if not m:
        return None
    reason = m.group("reason").strip().strip("*`").strip()
    return reason or "no reason given"


_MERGE_REFUSED_RE = re.compile(
    re.escape(MERGE_REFUSED_MARKER) + r"\s*(?P<reason>[^\n]{0,300})",
    re.IGNORECASE,
)


def body_has_blocking_findings(body: str | None) -> bool:
    """True when a review body carries at least one `[BLOCKING]` finding.

    ateles#595. `[BLOCKING] <category>: <summary>` is the structured field the
    review contract already defines (skill_runner.py) for exactly this purpose,
    so it — not the lens's self-reported verdict token — is the authority on
    whether a review blocks.

    Anchored to the marker rather than a substring search: `[NON-BLOCKING]`
    CONTAINS `BLOCKING`, so a naive `in` check would promote every advisory note
    into a merge-blocking REQUEST_CHANGES and jam the queue this fix exists to
    unjam. Reuses review_learning's marker shape so the two cannot drift.
    """
    if not body:
        return False
    return bool(_BLOCKING_MARKER_RE.search(body))


# `[BLOCKING] category: summary`, optionally wrapped in markdown emphasis as
# lenses actually write it (`**[BLOCKING] credential-scope:**` on ateles#558).
# The negative lookbehind on `NON-` is the whole trick — see the docstring.
_BLOCKING_MARKER_RE = re.compile(
    r"(?<!NON-)(?<!NON_)\[BLOCKING\]", re.IGNORECASE
)


def verdict_to_review_event(verdict: str | None, body: str | None = None) -> str:
    """Map an aggregated panel verdict onto a native GitHub review event.

    This is the load-bearing half of ateles#241: it turns a parsed-prose verdict
    into a real review state that GitHub itself enforces (visible in the PR's
    Reviews section and honoured by branch protection), instead of a verdict that
    only the dispatcher can see.

    Mapping — deliberately conservative in one direction only:
      approve         -> APPROVE
      request_changes -> REQUEST_CHANGES
      comment         -> COMMENT
      blocked         -> COMMENT   (blocking is expressed by routing findings
                                    back for a fix, not by hard-blocking merge)
      None            -> COMMENT   (unparseable: never escalate on a guess)

    REQUEST_CHANGES is returned for EXACTLY ONE input. That asymmetry is the
    point: escalating on a mis-parse would block a good PR under branch
    protection, and de-escalating a real REQUEST_CHANGES would defeat the
    feature. Everything ambiguous lands on the inert COMMENT.

    ateles#595 — body cross-check. When `body` is supplied and contains at least
    one `[BLOCKING]` marker, the body WINS and REQUEST_CHANGES is returned
    regardless of the token. On ateles#558 the legal lens posted `**COMMENT**`
    above a `[BLOCKING] credential-scope` finding; the token mapped to
    `--comment`, so a real blocker never reached `reviewDecision` and an
    aggregator trusting tokens over bodies counted that lens clear.

    The cross-check only ever ESCALATES, never de-escalates: a body with no
    blocking findings leaves every mapping above untouched, so the one-input
    asymmetry still holds everywhere the lens and its own findings agree.
    `body` is optional so ateles#241's existing callers are unaffected.
    """
    if body_has_blocking_findings(body):
        if verdict != "request_changes":
            log.warning(
                f"[{DAEMON_NAME}] lens verdict '{verdict or 'unparseable'}' "
                "contradicts its own [BLOCKING] findings — submitting "
                "REQUEST_CHANGES on the body (ateles#595)"
            )
        return _REVIEW_EVENT_REQUEST_CHANGES
    if verdict == "approve":
        return _REVIEW_EVENT_APPROVE
    if verdict == "request_changes":
        return _REVIEW_EVENT_REQUEST_CHANGES
    return _REVIEW_EVENT_COMMENT


def merge_authorization_clause(
    *, auto_merge: bool, pending_gates: set[str] | None = None
) -> str:
    """The merge-authorization paragraph handed to the PR steward.

    ateles#594 gap 3. Extracted from `_vanellus_prompt` so the autonomy clause
    is derived from BOTH inputs that govern it — the flag and the parent issue's
    gate state — in one testable place, rather than from the flag alone.

    The defect this closes: on ateles#592 the dispatch asserted
    "AUTONOMY — YOU MAY MERGE" while the parent issue #585 still had
    `pm: pending` and `impl: pending`. Lanius's fail-open-for-review path is
    correct and was not the bug; the bug is that a fail-open REVIEW state
    propagated into merge AUTHORIZATION language. Nothing stood between that
    clause and a bad merge except the steward re-deriving the gate itself.

    This is ateles#332 inverted: there the prompt forbade a merge the flag
    allowed, producing neither a merge nor an escalation. Authorization language
    must never contradict gate state in EITHER direction — so clear gates never
    upgrade a flag-off posture either.
    """
    pending = {g.strip().lower() for g in (pending_gates or set()) if g.strip()}

    if auto_merge and not pending:
        # Flag ON: the dispatcher deliberately files NO merge checkpoint (see
        # _gate_merge_readiness, which returns early on auto_merge), so if this
        # prompt still said "DO NOT MERGE" nothing would merge AND no checkpoint
        # would be raised — strictly worse than the flag being off. Authorize
        # the merge here, with the hard stops intact.
        return (
            "AUTONOMY — YOU MAY MERGE. Auto-merge is enabled "
            "(APIS_AUTONOMY_AUTO_MERGE=1). Merge via `gh pr merge --squash` "
            "ONLY when ALL hold: parent-issue gate inheritance passes (pm/ux/"
            "arch signed_off, waived, or not_required), every REQUIRED "
            "branch-protection check is green, the PR's base is the repository "
            "DEFAULT branch (never merge a stacked PR whose base is another "
            "open PR's head — state the required merge order instead), and a "
            "head-SHA-matched verdict is APPROVE with Blocking: 0. If any "
            "fails, do NOT merge — post the verdict naming the blocker and "
            "stop. Releases remain human-gated; merging to main is not a "
            "release.\n\n"
            "IF THE MERGE IS REFUSED by a tool-permission gate or any control "
            "outside this authorization, do NOT work around it and do NOT "
            "record the refusal only in your review body — say so explicitly "
            "and prominently in your reply, beginning the line with "
            f"`{MERGE_REFUSED_MARKER}`, so the dispatcher escalates it to the "
            "operator. A refusal nobody sees leaves the PR to rot into a merge "
            "conflict (ateles#565)."
        )

    if auto_merge and pending:
        # ateles#594 gap 3: authorization the gate forbids.
        return (
            "AUTONOMY — DO NOT MERGE. Auto-merge is enabled "
            "(APIS_AUTONOMY_AUTO_MERGE=1), but the parent issue still has "
            f"pre-impl gate(s) PENDING: {', '.join(sorted(pending))}. Gate "
            "inheritance outranks the autonomy flag, so authorization is "
            "withheld for this PR. Recommend and stop: post your aggregated "
            "verdict naming the pending gate(s) as the blocker."
        )

    return (
        "AUTONOMY GUARDRAIL — DO NOT MERGE. Merge is operator-gated: a "
        "blocking checkpoint_brief is filed at the merge boundary; the "
        "operator merges or instructs you to. This overrides any merge "
        "instruction in your standing protocol."
    )


# ── Per-agent GitHub account registry (#109) ─────────────────────────────────
# Canonical login for each of the 8 GitHub-facing agents.  Accounts are named
# `<APIS_OPERATOR_LOGIN>-ateles-<agent>` so that each fork of this public repo
# produces unique GitHub namespaces.  Ateles is forkable — hardcoding
# `ateles-<agent>` would collide for any other operator who forks the project.
# This map is only consulted when the agent's PAT exists (is_provisioned
# returns True); until then, the per-repo shared identity (#95) is used and
# native assignment is skipped.
GITHUB_FACING_AGENTS: frozenset[str] = frozenset({
    "lanius", "pavo", "vanellus", "waxwing",
    "accipiter", "buteo", "phoenicurus", "corvus",
})

# Built at module load from the operator handle so it stays consistent
# throughout a process lifetime.
AGENT_GITHUB_LOGIN: dict[str, str] = {
    a: f"{_OPERATOR_LOGIN}-ateles-{a}" for a in GITHUB_FACING_AGENTS
}


def dispatchable_agents() -> set[str]:
    """Every agent name the swarm can actually dispatch.

    Derived from the SAME structures dispatch uses — the review panel's lenses
    and the spec sections — plus the GitHub-facing roster and the release owner,
    rather than a fourth hand-maintained list. A guard that keeps its own copy of
    the roster drifts exactly like the thing it is guarding (ateles#441).
    """
    from issue_spec import SECTION_BY_AGENT
    from review_panel import LENSES

    agents = {lens.agent for lens in LENSES}
    agents |= set(SECTION_BY_AGENT)
    agents |= set(GITHUB_FACING_AGENTS)
    # Owners that appear in workflow definitions but run outside the panel and
    # spec paths: triage, PR stewardship, and release.
    agents |= {"lanius", "vanellus", "struthio", "cicada"}
    return agents


def workflow_owner_drift(
    workflows: list[dict], known_agents: set[str]
) -> list[tuple[str, str, str]]:
    """Gate owners a workflow names that no agent in the roster answers to.

    ateles#441. `workflow_definition` entities name the agent that owns each
    gate; dispatch picks agents from hardcoded rosters in `issue_spec.py` and
    `review_panel.py`. Nothing compared the two, so when Bombycilla was renamed
    Waxwing and Gryllus renamed Cicada on 2026-06-12, the workflows kept naming
    the old ones and no code noticed.

    The consequence is silent and slow: the renamed agent still runs, still
    returns ok, still writes its section — and the gate never signs, because the
    agent doing the work is not the owner the workflow expects. `pending` is a
    legitimate state, so nothing errors. ateles#416 sat that way for four days,
    and the auto-build handoff is gated on `_gates_green()`, so an unsignable
    gate stalls every downstream PR indefinitely.

    Returns ``(workflow_name, gate_name, owner_agent)`` per unknown owner. An
    empty list means every declared owner is someone the swarm can actually
    dispatch. Pure and side-effect free so the check is trivially testable.
    """
    drift: list[tuple[str, str, str]] = []
    for wf in workflows:
        snap = wf.get("snapshot") or wf
        name = snap.get("canonical_name") or wf.get("canonical_name") or (
            f"{snap.get('project', '?')}|{snap.get('workflow_type', '?')}"
        )
        for gate in snap.get("gates") or []:
            owner = (gate.get("owner_agent") or "").strip()
            if owner and owner not in known_agents:
                drift.append((str(name), str(gate.get("gate_name") or "?"), owner))
    return drift


def agent_github_login(agent: str) -> str:
    """Canonical GitHub login for *agent*, scoped to the current operator.

    Returns ``<APIS_OPERATOR_LOGIN>-ateles-<agent>`` so that each operator's
    fork of Ateles produces globally unique machine-account names (e.g.
    ``markmhendrickson-ateles-pavo``).  The operator handle is read once from
    ``APIS_OPERATOR_LOGIN`` at module load (``_OPERATOR_LOGIN``).

    Args:
        agent: lowercase agent genus name (e.g. "pavo", "lanius").
    """
    return f"{_OPERATOR_LOGIN}-ateles-{agent}"


def is_provisioned(agent: str) -> bool:
    """Return True when the agent's own GitHub PAT is set in the environment.

    Until `<AGENT>_AGENT_PAT` is provisioned this always returns False and the
    system behaves exactly as today (shared per-repo identity, attribution
    header in every comment).  This is the NO-OP guard for #109: with no
    per-agent PATs set, every call site behaves as before.

    Args:
        agent: lowercase agent genus name (e.g. "pavo", "lanius").
    """
    return bool(os.environ.get(f"{agent.upper()}_AGENT_PAT"))


def _token_for_agent_on_repo(agent: str, repo: str) -> str:
    """Three-tier GitHub token resolution for a specific agent + repo.

    Tier 1 — ``<AGENT>_AGENT_PAT`` (e.g. ``PAVO_AGENT_PAT``): the agent's own
              account.  Present only when the machine account has been created
              and the PAT stored.
    Tier 2 — per-repo identity: ``NEOTOMA_AGENT_PAT`` for ``*/neotoma`` repos,
              else ``ATELES_AGENT_PAT``.  This is the existing #95 behaviour,
              delegated to ``_token_for_repo``.
    Tier 3 — ``GITHUB_TOKEN`` (daemon-level fallback).

    The fallback when Tier 1 is absent is the per-repo shared account (Tier 2),
    NOT a hardcoded ateles-agent.  This preserves the #95 contract exactly.

    Args:
        agent: lowercase agent genus name (e.g. "pavo").
        repo:  full repo slug (e.g. "markmhendrickson/ateles").
    """
    agent_pat = os.environ.get(f"{agent.upper()}_AGENT_PAT", "")
    if agent_pat:
        return agent_pat
    # Tier 2 → 3 already implemented by _token_for_repo.
    return _token_for_repo(repo)


# ── QE3: eval-authoring affordance — PR-branch worktree for the qa lens ───────
#
# The qa lens (Phoenicurus) must AUTHOR an eval fixture, run it, commit, and push
# to the PR branch (the eval-backed qa gate, QE2). That needs a writable checkout
# of the PR branch — which a diff-only review child does not have. These helpers
# prepare a throwaway `git worktree` off the local neotoma clone, set its push
# URL to authenticate as the agent's own account (#109), and clean it up after.
#
# Scope: neotoma PRs only (the eval harness lives there). Best-effort: any failure
# returns None and the qa child falls back to diff-only + records the harness as
# unavailable — the gate never stalls on worktree prep.

# Local clone the worktree is based on. neotoma-rc-src is the prod-server's
# checkout; we add a SEPARATE worktree off it (never mutate it in place — the
# shared-checkout/daemon-fragility hazard).
NEOTOMA_LOCAL_CHECKOUT = os.path.expanduser(
    os.environ.get("NEOTOMA_LOCAL_CHECKOUT", "~/neotoma-rc-src")
)

# Auto-release (push_main trigger): the only repo with a publishable release
# pipeline. A merge to any other repo's main does not prepare a release.
PHOENICURUS_RELEASE_REPO = os.environ.get(
    "PHOENICURUS_RELEASE_REPO", "markmhendrickson/neotoma"
)
# The release repo's default branch. A check_suite:completed on this branch is
# the signal that a merge's CI has settled — the retry that lets a push_main
# deferred on in-progress/red CI finally prepare (prepare.py leaves the SHA lock
# unstamped on those transient deferrals precisely so this retry can fire).
PHOENICURUS_RELEASE_BRANCH = os.environ.get("PHOENICURUS_RELEASE_BRANCH", "main")
# Cap on the synchronous half of prepare.py (preflight + spawning the detached
# prepare agent). Generous: it shells out to git and `gh run list`.
PHOENICURUS_PREPARE_TIMEOUT_S = int(
    os.environ.get("PHOENICURUS_PREPARE_TIMEOUT_S", "300")
)
# Cap on publish.py invoked from an email approval. Publish is the whole
# irreversible sequence (merge RC, tag, await npm, GitHub Release, deploys), so
# this is generous — well above the npm-publish wait.
PHOENICURUS_PUBLISH_TIMEOUT_S = int(
    os.environ.get("PHOENICURUS_PUBLISH_TIMEOUT_S", "1800")
)


async def _git(args: list[str], cwd: str, timeout: int = 60) -> tuple[int, str, str]:
    """Run a git command, returning (rc, stdout, stderr). Never raises."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")
    except Exception as exc:  # noqa: BLE001 — best-effort
        return 1, "", str(exc)


async def prepare_pr_worktree(repo: str, pr_number: int, agent: str) -> str | None:
    """Prepare a writable worktree of a PR branch for the qa eval-authoring child.

    Returns the worktree path, or None on any failure (best-effort).

    Steps:
      1. Verify the local base clone exists (NEOTOMA_LOCAL_CHECKOUT).
      2. ``git fetch origin pull/<n>/head`` into a temp ref.
      3. ``git worktree add --detach <tmp> FETCH_HEAD`` — isolated from the base.
      4. Point the worktree's ``origin`` push URL at
         ``https://x-access-token:<token>@github.com/<repo>.git`` so the child's
         ``git push`` authenticates as the agent (#109), independent of the base
         clone's SSH remote.

    Only neotoma is supported (the eval harness lives there); other repos -> None.
    """
    if not repo.endswith("/neotoma"):
        return None
    base = NEOTOMA_LOCAL_CHECKOUT
    if not os.path.isdir(os.path.join(base, ".git")):
        log.warning(
            f"[{DAEMON_NAME}] QE3: local neotoma checkout absent at {base} — "
            "qa child falls back to diff-only (no eval authoring)"
        )
        return None

    # Fetch the PR head into FETCH_HEAD on the base clone (shared object store).
    rc, _, err = await _git(
        ["fetch", "origin", f"pull/{pr_number}/head"], cwd=base, timeout=120
    )
    if rc != 0:
        log.warning(
            f"[{DAEMON_NAME}] QE3: fetch pull/{pr_number}/head failed: {err.strip()}"
        )
        return None

    wt = tempfile.mkdtemp(prefix=f"qa_eval_pr{pr_number}_")
    rc, _, err = await _git(
        ["worktree", "add", "--detach", wt, "FETCH_HEAD"], cwd=base
    )
    if rc != 0:
        log.warning(f"[{DAEMON_NAME}] QE3: worktree add failed: {err.strip()}")
        shutil.rmtree(wt, ignore_errors=True)
        return None

    # Authenticate pushes as the agent's own account WITHOUT writing any token to
    # disk or into a remote URL.
    #
    # SECURITY (Loxia #134): embedding the token in remote.origin.url
    # (https://x-access-token:<token>@...) leaks it two ways — it persists in the
    # worktree's .git config on disk, AND git echoes the full remote URL (token
    # included) in error output when a push fails, which could surface in the
    # child's stderr → harness_event / GitHub comment / daemon log. Instead:
    #   1. set origin to a CLEAN HTTPS URL (no token) — push failures echo only
    #      https://github.com/<repo>.git, never a secret;
    #   2. configure `gh` as the credential helper for this worktree, which
    #      supplies the token from the child's GH_TOKEN/GITHUB_TOKEN env (injected
    #      by run_skill from the per-agent token) at push time. The token lives
    #      only in process env, never on disk.
    # extensions.worktreeConfig keeps both settings scoped to this throwaway
    # worktree, never the shared base-clone config (the prod-server's checkout).
    token = _token_for_agent_on_repo(agent, repo)
    if token:
        await _git(["config", "extensions.worktreeConfig", "true"], cwd=wt)
        await _git(
            ["config", "--worktree", "remote.origin.url",
             f"https://github.com/{repo}.git"],
            cwd=wt,
        )
        await _git(
            ["config", "--worktree",
             "credential.https://github.com.helper", "!gh auth git-credential"],
            cwd=wt,
        )
    else:
        log.info(
            f"[{DAEMON_NAME}] QE3: no token for {agent} on {repo} — qa child can "
            "write+run the eval locally for the QA report but cannot push "
            "(degraded-but-functional)"
        )

    log.info(f"[{DAEMON_NAME}] QE3: prepared PR worktree {wt} for {agent} on {repo}#{pr_number}")
    return wt


async def cleanup_pr_worktree(worktree: str | None) -> None:
    """Remove a worktree prepared by prepare_pr_worktree. Best-effort/idempotent."""
    if not worktree:
        return
    base = NEOTOMA_LOCAL_CHECKOUT
    if os.path.isdir(os.path.join(base, ".git")):
        await _git(["worktree", "remove", "--force", worktree], cwd=base)
    shutil.rmtree(worktree, ignore_errors=True)


def _agent_prompt_instruction(agent: str, role: str) -> str:
    """Return the prompt instruction block for GitHub comment identity.

    When the agent is provisioned (its own PAT exists) the agent posts AS
    ITSELF — no in-body attribution header is needed and the instruction tells
    it so.  When unprovisioned (current reality for all agents) it gets the
    existing "shared account — prepend the header" instruction.

    This helper centralises the conditional so every call site stays coherent
    in both states without scattered if/else branches.

    Args:
        agent: lowercase agent genus name.
        role:  human-readable role description for the header text.
    """
    if is_provisioned(agent):
        return (
            f"You are posting as your own GitHub account "
            f"(`{agent_github_login(agent)}`). "
            "Do NOT add an attribution header — your avatar and account name "
            "already identify you."
        )
    return (
        f"Begin any GitHub comment you post with "
        f"`{attribution_header(agent, role)}` — the GitHub "
        "account is shared, so the comment body is your identity."
    )


def attribution_header(agent: str, role: str) -> str:
    """First line of every swarm-authored GitHub comment posted under a SHARED account.

    PR-87 dogfood feedback: comments posted through a shared GitHub identity
    (or the operator's keyring) are indistinguishable from the operator
    without an in-body attribution line.  Until per-agent machine accounts
    are provisioned (#109), this line IS the agent identity on GitHub.

    When the agent has its own account (is_provisioned returns True) callers
    should use _agent_prompt_instruction instead, which omits the header so
    the agent does not redundantly label itself.  This function always returns
    the non-empty header string — gating on provisioning belongs at the call
    site via _agent_prompt_instruction.
    """
    return f"**\U0001f916 {agent.capitalize()} — Ateles swarm, {role}**"


def compose_fallback_comment(lens: str, agent: str, text: str) -> str:
    """Body for a dispatcher-posted review comment (panelist could not post)."""
    return (
        f"review:{lens}\n"
        f"{attribution_header(agent, f'{lens} lens panelist')}\n\n"
        f"{text}\n\n"
        f"_Posted by the Apis dispatcher on behalf of {agent} — the "
        "panelist could not post its comment directly._"
    )


# Stable marker embedded in every Vanellus aggregation comment so the
# dispatcher can detect whether the comment landed (dedup / missing-check).
_VANELLUS_COMMENT_MARKER = "<!-- vanellus-aggregation -->"

# Page cap for the comment scan (ateles#430). 50 pages = 5000 comments, far
# beyond any real PR; it exists so a pathological thread cannot spin the loop,
# and it logs when hit rather than truncating silently.
_MAX_COMMENT_PAGES = 50


def latest_aggregation_comment(comments: list[dict]) -> dict | None:
    """Return the NEWEST Vanellus aggregation comment, or None if there is none.

    ateles#430. GitHub's issue-comments endpoint SILENTLY IGNORES ``sort`` and
    ``direction`` — it always returns oldest-first. Two call sites here passed
    ``sort=created&direction=desc``, believed the response was newest-first, and
    ``return``ed on the first marker match. On any PR with more than one
    aggregation they therefore read the OLDEST verdict.

    On neotoma#2153 that republished a nine-day-old ``REQUEST_CHANGES`` over a
    current ``COMMENT / Blocking: 0``. The same bug in ``_pr_review_is_clear``
    is worse, because it gates merges and fails UNSAFE: an early ``APPROVE``
    superseded by a later ``REQUEST_CHANGES`` would read as clear and the PR
    would merge on a withdrawn approval.

    Selection is therefore client-side and never positional: collect every
    marker-bearing comment, then take max ``created_at``, tie-breaking on max
    ``id`` (monotonic per repo). Callers must page the full comment list before
    calling — with >100 comments the newest aggregation can sit on a later page,
    and sorting only what you fetched cannot find what was never returned.
    """
    candidates = [
        c for c in comments if _VANELLUS_COMMENT_MARKER in (c.get("body") or "")
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda c: (str(c.get("created_at") or ""), int(c.get("id") or 0)),
    )


def vanellus_comment_missing(comment_bodies: list[str]) -> bool:
    """Return True when no Vanellus aggregation comment has landed on the PR.

    Detects the stable HTML marker ``_VANELLUS_COMMENT_MARKER`` that the
    dispatcher prefixes to every fallback comment it posts on Vanellus's
    behalf, and that the _vanellus_prompt instructs Vanellus to include when
    it posts its own comment directly.  A missing comment means neither
    Vanellus nor a prior fallback post succeeded."""
    return not any(_VANELLUS_COMMENT_MARKER in body for body in comment_bodies)


def compose_vanellus_fallback_comment(text: str) -> str:
    """Body for a dispatcher-posted Vanellus aggregation comment.

    Prefixed with the stable marker so future dedup checks can find it, and
    with the Vanellus attribution header so readers know which agent authored
    the verdict."""
    return (
        f"{_VANELLUS_COMMENT_MARKER}\n"
        f"{attribution_header('vanellus', 'PR steward')}\n\n"
        f"{text}\n\n"
        "_Posted by the Apis dispatcher on behalf of Vanellus — "
        "Vanellus could not post its aggregation comment directly._"
    )


# Signatures of a credential/auth failure in a spawned `claude --print` panelist
# (expired Anthropic OAuth session or missing/invalid ANTHROPIC_API_KEY). When a
# panelist hits this, its "verdict" is just the auth error — posting it as a
# review is misleading, and the real fix is operator re-auth. See
# detect_auth_failure + the credential-expiry resilience pattern.
_AUTH_FAILURE_SIGNATURES = (
    "401 invalid authentication credentials",
    "invalid authentication credentials",
    "could not resolve authentication",
    "oauth token has expired",
    "authentication_error",
    "please run /login",
    "invalid api key",
    "x-api-key header is required",
    # Billing exhaustion is an infra/credential-class failure too: the model
    # call cannot run, so its "verdict" is not a review. Reframe + page, don't
    # treat as a completed fix. (Seen live on a metered-API GHA reviewer.)
    "credit balance is too low",
)


# Signatures of a SESSION / USAGE-LIMIT failure — distinct from an auth failure.
# A 401 needs the operator to re-auth (page + wait for a human). A usage limit
# clears ON ITS OWN at a known reset time, so the right response is not "page and
# stop" but "wait for the reset, then resume the same work automatically". These
# are kept separate from _AUTH_FAILURE_SIGNATURES precisely because the
# remediation differs: re-auth vs. auto-resume. ("credit balance is too low"
# stays with auth — a metered-billing exhaustion needs an operator top-up, not a
# timed retry.)
_SESSION_LIMIT_SIGNATURES = (
    "hit your session limit",
    "you've hit your session limit",
    "session limit reached",
    "usage limit reached",
    "you've reached your usage limit",
    "rate limit",
    "resets at",
    "resets ",
)


def detect_session_limit(*texts: str) -> bool:
    """True when a panelist/aggregation failed on a self-clearing usage limit.

    Distinguished from :func:`detect_auth_failure` so the dispatcher can SCHEDULE
    A RESUME after the reset window rather than page-and-stop: a session/usage
    limit is transient and clears on its own, unlike an expired token."""
    blob = " ".join(t for t in texts if t).lower()
    # Require an explicit "limit" or "reset" cue, not just any of the softer
    # substrings, to avoid misfiring on prose that merely mentions "rate".
    if not any(sig in blob for sig in _SESSION_LIMIT_SIGNATURES):
        return False
    return "limit" in blob or "reset" in blob


def parse_limit_reset_delay(*texts: str, now: "datetime | None" = None) -> int:
    """Best-effort seconds until a usage limit resets, from the failure message.

    Parses shapes like "resets 7:30pm", "resets at 19:30", "resets 7pm
    (Europe/Madrid)". Returns a conservative default when no time is found, and
    always at least a small floor so a mis-parse can't busy-loop. The clock is
    injected for testability; timezone in the message is ignored.

    TZ ASSUMPTION (Loxia #264 review): `now` defaults to naive `datetime.now()`,
    so the "resume late, not early" guarantee holds only when the daemon's local
    clock is at or behind the reset time's zone. The Apis daemon runs
    `Europe/Madrid` (the plist sets TZ), which is the zone the Anthropic reset
    messages quote, so this holds in practice. Even if it did not, the
    still-limited re-defer path re-arms with a fresh reset, so an early resume
    self-corrects rather than failing."""
    import re

    default = int(os.environ.get("APIS_LIMIT_RESET_DEFAULT_SECONDS", str(3 * 3600)))
    floor = 300
    blob = " ".join(t for t in texts if t).lower()
    m = re.search(r"resets?(?:\s+at)?\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", blob)
    if not m:
        return default
    now = now or datetime.now()
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3)
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return default
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    delay = int((target - now).total_seconds())
    # Pad past the reset boundary so we don't retry the instant it flips.
    return max(floor, delay + 120)


def detect_auth_failure(*texts: str) -> bool:
    """True when any text carries a Claude/Anthropic auth-failure signature.

    Checked against a panelist's stdout+stderr so the dispatcher can reframe an
    auth failure as an infra problem (and page the operator to re-auth) instead
    of posting the raw 401 as a review verdict."""
    blob = " ".join(t for t in texts if t).lower()
    return any(sig in blob for sig in _AUTH_FAILURE_SIGNATURES)


def compose_auth_failure_comment(agent: str) -> str:
    """Body for the comment posted when a panel agent's Claude call fails auth.

    Reframes the 401 as an operational issue with a clear remediation, rather
    than a bare 'could not post' that reads like the agent's verdict."""
    return (
        f"{_VANELLUS_COMMENT_MARKER}\n"
        # Attribute to the dispatcher, not Vanellus: this is NOT a Vanellus
        # verdict, and its own body + footer say so. A vanellus/PR-steward header
        # on a "not a verdict" notice reads, at a skim, exactly like the verdict
        # it is disclaiming — the same "looked like a verdict" bug this PR's
        # deferral marker was written to avoid, at the header level (#264 ux).
        f"{attribution_header('apis', 'swarm dispatcher')}\n\n"
        f"⚠️ **Panel review unavailable — agent credential failure.** The `{agent}` "
        "panel agent (spawned as `claude --print` by the Apis dispatcher) could "
        "not authenticate to the Anthropic API (401). This is an infrastructure "
        "issue, **not** a review verdict — the PR has not been assessed by the panel.\n\n"
        "**Fix (host-side):** set a long-lived `ANTHROPIC_API_KEY` in the Apis "
        "daemon environment (`com.ateles.apis` plist or `ateles-private/.env`) and "
        "reload, or re-authenticate the Claude CLI for the daemon user. Daemons "
        "should use an API key, not an interactive OAuth session that expires.\n\n"
        "_Posted by the Apis dispatcher; the operator has been paged to re-auth._"
    )


def lenses_missing_comments(
    comment_bodies: list[str], lenses: list[str]
) -> list[str]:
    """Lenses whose `review:<lens>` comment never landed on the PR.

    Headless panelists post their own comment when they can; this identifies
    the ones that could not so the dispatcher can post the captured review
    itself (PR-87 self-dogfood finding: 3 of 4 panel reviews existed only on
    stdout)."""
    posted = {
        lens
        for lens in lenses
        for body in comment_bodies
        if body.lstrip().startswith(f"review:{lens}")
    }
    return [lens for lens in lenses if lens not in posted]


def _token_for_repo(repo: str) -> str:
    """Return the GitHub token appropriate for the given repo slug.

    Per-repo identity (#95): comments and API calls on markmhendrickson/neotoma
    should use NEOTOMA_AGENT_PAT so they appear under the neotoma-agent machine
    account rather than the ateles-agent identity.  All other repos use
    ATELES_AGENT_PAT.  Both fall back to GITHUB_TOKEN when the per-repo PAT is
    unset, so nothing breaks if only the shared token is configured.
    """
    if repo.endswith("/neotoma"):
        return (
            os.environ.get("NEOTOMA_AGENT_PAT", "")
            or os.environ.get("GITHUB_TOKEN", "")
        )
    return (
        os.environ.get("ATELES_AGENT_PAT", "")
        or os.environ.get("GITHUB_TOKEN", "")
    )


@dataclass
class DispatchConfig:
    neotoma_base_url: str = os.environ.get(
        "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
    )
    neotoma_token: str = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    github_token: str = os.environ.get("GITHUB_TOKEN", "") or os.environ.get(
        "ATELES_AGENT_PAT", ""
    )
    panel_max: int = int(os.environ.get("APIS_PANEL_MAX", "4"))
    auto_merge: bool = os.environ.get("APIS_AUTONOMY_AUTO_MERGE", "0") == "1"
    dry_run: bool = os.environ.get("APIS_DRY_RUN", "0") == "1"
    # Auto-build handoff (rollout safety): when ON, a fully-assembled spec whose
    # gates are green chains into an implementation PR (Cicada build) so the PR
    # gate pipeline takes over.  Default OFF/unset = spec-only, notify-and-wait
    # for the operator's `build` approval.  Never auto-MERGES — that boundary
    # stays operator-gated via APIS_AUTONOMY_AUTO_MERGE regardless.
    auto_build: bool = os.environ.get("ATELES_SWARM_AUTO_BUILD", "0") == "1"
    # Operator-approval-triggers-merge (approval loop). When ON, an explicit
    # operator approval — via the /approve issue comment, a GitHub PR review
    # "approved" event, or an email reply carrying APPROVE + the correlation
    # token — performs the actual `gh pr merge --squash`. Default OFF preserves
    # the conservative Phase-H1 behaviour (resolve the checkpoint, operator
    # merges by hand). This is DISTINCT from APIS_AUTONOMY_AUTO_MERGE: that flag
    # merges WITHOUT the operator once the panel is green; this flag merges ONLY
    # on an explicit operator approval. The merge boundary still requires a human
    # act — this just lets that act be the merge itself instead of a second click.
    approval_triggers_merge: bool = (
        os.environ.get("APIS_APPROVAL_TRIGGERS_MERGE", "0") == "1"
    )
    # Merge method used when approval_triggers_merge fires. Squash is the repo
    # default for swarm build PRs (one clean commit per issue).
    approval_merge_method: str = os.environ.get(
        "APIS_APPROVAL_MERGE_METHOD", "squash"
    ).strip().lower()
    # Concurrency cap for the ordered issue spec pipeline: a burst of many
    # issue.opened events must not stampede the daemon (each pipeline spawns a
    # chain of `claude --print` children).  Capped at 3 concurrent pipelines.
    max_concurrent_issue_pipelines: int = int(
        os.environ.get("APIS_MAX_CONCURRENT_ISSUE_PIPELINES", "3")
    )
    # Max automatic review→fix→re-review rounds before escalating to the operator.
    # Each round: lens agents propose fixes for their blocking findings, Cicada
    # implements + pushes, the push (synchronize) re-runs the panel. Bounds the
    # Cicada↔panel loop so a finding the swarm can't resolve doesn't spin forever.
    max_fix_rounds: int = int(os.environ.get("APIS_MAX_FIX_ROUNDS", "2"))
    # Auto re-review on push (ateles#230). When a PR's parent issue still has an
    # open pre-impl gate, Lanius returns `blocked` and `_handle_pr` skips the
    # panel — correct on the FIRST look (nothing has been reviewed yet), but on a
    # re-push it means an author can push exactly the fix a lens asked for and
    # nothing re-evaluates it: the PR waits for a human to manually re-invoke the
    # reviewer (`/swarm-run`). When ON, a `synchronize` push to a gate-blocked PR
    # re-invokes the lens agents that own the open gates against the new head, so
    # they re-affirm the block, raise a new finding, or sign off on their own.
    #
    # This does NOT weaken the self-certification boundary: the gate still flips
    # only on a LENS agent's own verdict — a push never clears a gate, and the
    # dispatcher never infers sign-off from the fact that code changed. Merge
    # stays behind APIS_AUTONOMY_AUTO_MERGE regardless. Default OFF preserves
    # today's notify-and-wait exactly.
    auto_rereview_on_push: bool = (
        os.environ.get("ATELES_SWARM_AUTO_REREVIEW", "0") == "1"
    )
    # Repos the startup resume sweep scans for issue pipelines left in flight by
    # a daemon restart. The daemon is otherwise webhook-driven and keeps no repo
    # list, so this is explicit. Comma-separated; empty disables the sweep.
    resume_repositories: tuple[str, ...] = tuple(
        r.strip()
        for r in os.environ.get(
            "APIS_RESUME_REPOSITORIES",
            f"{_OPERATOR_LOGIN}/ateles,{_OPERATOR_LOGIN}/neotoma",
        ).split(",")
        if r.strip()
    )


class SwarmDispatcher:
    """Routes normalized GitHub triggers into agent pipelines."""

    def __init__(self, notifier: Notifier, config: DispatchConfig | None = None):
        self.notifier = notifier
        self.config = config or DispatchConfig()
        # Concurrency-safety for the additive issue spec pipeline: a bounded
        # semaphore so a burst of issue.opened events runs at most
        # ``max_concurrent_issue_pipelines`` spec sequences at once (the rest
        # queue).  Created lazily so tests that construct a dispatcher without a
        # running loop still work; see ``_issue_pipeline_semaphore``.
        self._issue_semaphore: asyncio.Semaphore | None = None
        # Re-dispatch bookkeeping for the stalled-review sweep, keyed by
        # "owner/repo#N". In-memory on purpose: a daemon restart re-runs the
        # startup resume sweep anyway, and losing the counter costs at most a
        # few extra re-dispatches — strictly better than persisting a counter
        # that could permanently suppress a PR the swarm should retry.
        self._stall_retries: dict[str, int] = {}
        self._stall_escalated: dict[str, bool] = {}
        # ateles#431. Per-lens, not per-PR: a PR can be held on one absent lens,
        # recover, and later be held on a different one — a shared counter would
        # carry the first lens's exhaustion onto the second and silently skip it.
        self._missing_lens_retries: dict[str, dict[str, int]] = {}
        self._missing_lens_escalated: dict[str, set[str]] = {}
        # ateles#511. Keyed by "owner/repo#N@<head sha>", not by ref: a new push
        # is a new attempt and must reset the budget, or three failures on one
        # revision would permanently suppress every later revision of that PR.
        self._revision_retries: dict[str, int] = {}
        self._revision_escalated: dict[str, bool] = {}
        # ateles#565. Keyed by "owner/repo#N:<mergeable_state>" so a CLEAN PR
        # that later rots to DIRTY pages again — that transition changes what
        # the operator can actually do about it.
        self._approved_escalated: dict[str, bool] = {}

    async def handle_trigger(self, trigger: SwarmTrigger) -> None:
        """Entry point handed to the webhook gateway. Never raises."""
        try:
            if self.config.dry_run:
                # Fully side-effect free: no harness_event store, no dispatch.
                log.info(
                    f"[{DAEMON_NAME}] DRY RUN — skipping pipeline for "
                    f"{trigger.kind} {trigger.repository}#{trigger.number}"
                )
                return
            await self._log_harness_event(trigger)
            if trigger.kind == "issue_opened":
                await self._handle_issue_opened(trigger)
            elif trigger.kind == "pr_review":
                await self._handle_pr_review(trigger)
            elif trigger.kind == "email_approve":
                await self._handle_email_approve(trigger)
            elif trigger.kind == "ci_status":
                await self._handle_ci_status(trigger)
            elif trigger.kind == "push_main":
                await self._handle_push_main(trigger)
            elif trigger.kind == "release_approve":
                await self._handle_release_approve(trigger)
            elif trigger.is_pr:
                await self._handle_pr(trigger)
            elif trigger.kind == "issue_comment":
                await self._handle_issue_comment(trigger)
        except Exception as exc:  # one bad delivery must not kill the daemon
            log.error(
                f"[{DAEMON_NAME}] pipeline error for {trigger.kind} "
                f"{trigger.repository}#{trigger.number}: {exc}",
                exc_info=True,
            )
            self.notifier.send(
                f"Swarm pipeline failed on {trigger.repository}#{trigger.number} "
                f"({trigger.kind}): {exc}",
                priority=Priority.BLOCKER,
                handler=DAEMON_NAME,
            )

    # ── issue.opened pipeline (ordered additive spec) ───────────────────────

    def _issue_pipeline_semaphore(self) -> asyncio.Semaphore:
        """Lazily construct the bounded semaphore for issue spec pipelines.

        Created on first use (inside the running loop) so a dispatcher can be
        constructed in a test without an active event loop.  A burst of many
        issue.opened events therefore runs at most
        ``config.max_concurrent_issue_pipelines`` spec sequences at once; the
        rest queue on ``acquire`` rather than stampeding the daemon.
        """
        if self._issue_semaphore is None:
            self._issue_semaphore = asyncio.Semaphore(
                max(1, self.config.max_concurrent_issue_pipelines)
            )
        return self._issue_semaphore

    def _selected_sections(self, trigger: SwarmTrigger) -> list[SpecSection]:
        """The ordered spec sections that run for this issue.

        PM, Eng, QA always run.  The conditional lenses (Design/UX, Security,
        Legal) run ONLY when ``select_expectation_agents`` — the SAME
        lens-selection logic used by the review panel — pulls their lens in for
        this issue's title/body/labels.  The result is always in canonical
        ``SECTIONS`` order.
        """
        selected_agents = {
            lens.agent
            for lens in select_expectation_agents(
                trigger.title, trigger.body, trigger.labels
            )
        }
        out: list[SpecSection] = []
        for section in SECTIONS:
            if section.always or section.agent in selected_agents:
                out.append(section)
        return out

    # ── in-flight pipeline marker (restart resume) ───────────────────────────
    #
    # The issue pipeline spawns `claude --print` children in-process and holds
    # its progress only in memory. A daemon restart (crash, KeepAlive bounce,
    # a deliberate `launchctl bootout` to pick up new code) therefore orphans
    # the children and silently voids the run: nothing retries it, and the
    # issue is left looking as though the pipeline simply never happened.
    # Observed 2026-07-17: two /swarm-run invocations were logged as started
    # and lost to a restart ~3 minutes later, with no trace and no retry.
    #
    # The `task` watchdog already implements resume-after-restart, but it
    # sweeps `task` entities and the issue pipeline creates none, so it cannot
    # see this work. Rather than force pipeline runs into the 82-field
    # operator-domain `task` schema (semantic abuse, and its re-dispatch goes
    # to the task executor, not here), mark the run on the ISSUE itself with a
    # hidden marker comment — the same durable primitive `_FIX_ROUND_MARKER`
    # already uses precisely because it "survives daemon restarts". It needs no
    # Neotoma round-trip, which matters: Neotoma was returning 530s during the
    # very outage window this fix addresses.
    # ateles#323: the marker now carries a `stage` so a restart between the
    # QUEUED log line and semaphore acquisition (previously unrecorded — see
    # _handle_issue_opened) is still durable and visible to the resume sweep.
    # `stage` defaults to "inflight" in the regex so markers written by an
    # older daemon build (no stage suffix) still round-trip as before.
    _PIPELINE_INFLIGHT_MARKER = (
        "<!-- apis-pipeline-inflight:{started_at}:{stage} -->"
    )
    # datetime.isoformat() emits "+00:00" (not "Z"), so the timestamp group must
    # accept "+"/":" — a regex that only allowed "Z" would write markers it could
    # never match back, silently breaking both the clear and the resume sweep.
    _PIPELINE_INFLIGHT_RE = re.compile(
        r"<!-- apis-pipeline-inflight:([0-9T:.+\-]+Z?)(?::(queued|inflight))? -->"
    )

    async def _handle_issue_opened(self, trigger: SwarmTrigger) -> None:
        """Ordered, additive spec pipeline for a newly-opened issue.

        Concurrency-safe: acquires the bounded issue-pipeline semaphore so a
        burst of issue.opened events cannot stampede the daemon.

        Restart-safe: records a "queued" marker BEFORE the semaphore is even
        attempted, transitions it to "inflight" once acquired, and clears it on
        completion — so a restart at ANY point (parked on the semaphore, or
        mid-run) leaves a durable trace the startup sweep can resume
        (ateles#230 follow-up; ateles#323 closed the gap where a restart while
        merely queued left no marker at all).

        Queue-visible: when the single-slot pipeline semaphore is already held,
        the acquire below can block for many minutes with no signal, so the
        issue looks acknowledged (its /swarm-run or open confirmation is posted)
        while it is actually parked (ateles#259). We emit a queued signal BEFORE
        blocking and a started signal once the slot is acquired, so the wait is
        visible in the log even absent any GitHub comment.
        """
        ref = f"{trigger.repository}#{trigger.number}"
        sem = self._issue_pipeline_semaphore()
        if sem.locked():
            log.info(
                f"[{DAEMON_NAME}] issue pipeline for {ref} QUEUED — waiting for "
                "the issue-pipeline slot (another pipeline is running)"
            )
            _queued_at = datetime.now(timezone.utc)
            # ateles#323: mark the pipeline BEFORE the semaphore is acquired,
            # in the SAME branch that already detects queuing. A restart while
            # parked here previously left NO durable trace at all, since the
            # only marker write happened after acquisition; the resume sweep
            # therefore never saw a queued-but-never-started pipeline. Gated
            # on the existing `sem.locked()` branch (not written
            # unconditionally) so the uncontended common case still pays for
            # exactly one marker write, same as before this fix.
            await self._mark_pipeline_inflight(trigger, stage="queued")
        else:
            _queued_at = None
        async with sem:
            if _queued_at is not None:
                waited_s = (datetime.now(timezone.utc) - _queued_at).total_seconds()
                log.info(
                    f"[{DAEMON_NAME}] issue pipeline for {ref} STARTED — "
                    f"acquired the issue-pipeline slot after {waited_s:.1f}s queued"
                )
            await self._mark_pipeline_inflight(trigger, stage="inflight")
            try:
                await self._run_issue_spec_pipeline(trigger)
            finally:
                # Clear on success AND on failure: a pipeline that ran to a
                # real error is not "interrupted" — it already reported itself
                # and must not be resurrected by the sweep on every boot.
                await self._clear_pipeline_inflight(trigger)

    async def _mark_pipeline_inflight(
        self, trigger: SwarmTrigger, *, stage: str = "inflight"
    ) -> None:
        """Post the hidden pipeline marker at the given stage ("queued" before
        the semaphore is acquired, "inflight" once inside it). Best-effort: a
        marker we cannot write only costs resumability, so it must never block
        the pipeline."""
        marker = self._PIPELINE_INFLIGHT_MARKER.format(
            started_at=datetime.now(timezone.utc).isoformat(), stage=stage
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"https://api.github.com/repos/{trigger.repository}/issues/"
                    f"{trigger.number}/comments",
                    json={"body": marker},
                    headers=self._github_headers(trigger.repository),
                )
                resp.raise_for_status()
        except Exception as exc:
            log.warning(
                f"[{DAEMON_NAME}] {trigger.repository}#{trigger.number}: could "
                f"not record in-flight pipeline marker ({exc}) — the run "
                "proceeds but will not be resumable if the daemon restarts"
            )

    async def _clear_pipeline_inflight(self, trigger: SwarmTrigger) -> None:
        """Delete any in-flight markers on this issue. Best-effort."""
        ref = f"{trigger.repository}#{trigger.number}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{trigger.repository}/issues/"
                    f"{trigger.number}/comments",
                    params={"per_page": 100},
                    headers=self._github_headers(trigger.repository),
                )
                resp.raise_for_status()
                for comment in resp.json():
                    if self._PIPELINE_INFLIGHT_RE.search(comment.get("body", "")):
                        await client.delete(
                            f"https://api.github.com/repos/{trigger.repository}"
                            f"/issues/comments/{comment.get('id')}",
                            headers=self._github_headers(trigger.repository),
                        )
        except Exception as exc:
            log.warning(
                f"[{DAEMON_NAME}] {ref}: could not clear in-flight pipeline "
                f"marker ({exc}) — a stale marker may cause one redundant "
                "resume on the next daemon start"
            )

    async def resume_interrupted_pipelines(self, repositories: list[str]) -> dict:
        """Resume issue pipelines left in flight by a daemon restart.

        Called once at startup. Searches each repo for open issues carrying the
        hidden pipeline marker — in either the "queued" stage (parked on the
        issue-pipeline semaphore when the daemon died, ateles#323) or the
        "inflight" stage (a lens agent was mid-run) — and re-runs each one
        through the same `issue_opened` entry point a fresh trigger would use,
        so a resumed "queued" issue goes through triage exactly as normal
        rather than a special-cased resume path.

        Bounded and fail-open by construction:
          * only OPEN issues with the marker are candidates (a closed issue's
            pipeline is moot);
          * `_handle_issue_opened` clears the marker in its own `finally`, so a
            resumed run cannot re-trigger itself into a loop;
          * the existing issue-pipeline semaphore still applies, so a resume
            burst cannot stampede the daemon;
          * every failure path logs and returns — a broken resume must never
            stop the daemon from booting.

        Stall guard (ateles#323): after a resumed pipeline runs, we verify it
        advanced past triage by checking whether the issue's `updated_at`
        moved past the moment the resume began — `_mirror_spec_to_issue`
        PATCHes the issue body on every section including the first, so any
        real progress bumps this. (Checking for mere PRESENCE of the
        spec-mirror marker would be wrong: it is spliced in and never
        removed, so it would already be there from any prior completed run on
        the same issue, regardless of whether THIS resume made any progress.)
        A resume that completes without the issue being touched again is NOT
        silently re-queued for next boot — it is logged at error level and
        escalated to the operator, since a repeat failure on every restart
        would otherwise be invisible.

        Returns a summary dict for logging/tests.
        """
        summary = {"scanned": 0, "resumed": 0, "failed": 0, "stalled": 0}
        for repository in repositories:
            try:
                issues = await self._issues_with_inflight_marker(repository)
            except Exception as exc:
                log.warning(
                    f"[{DAEMON_NAME}] resume sweep: could not scan {repository} "
                    f"({exc}) — skipping"
                )
                continue
            summary["scanned"] += len(issues)
            for issue in issues:
                ref = f"{repository}#{issue.get('number')}"
                stage = issue.get("_marker_stage", "inflight")
                log.info(
                    f"[{DAEMON_NAME}] resume sweep: {ref} has a '{stage}' "
                    "pipeline marker — the daemon restarted mid-run; re-running"
                )
                self.notifier.send(
                    f"Resuming issue pipeline on {ref} — it was interrupted by "
                    "a daemon restart",
                    priority=Priority.INFO,
                    handler=DAEMON_NAME,
                )
                resume_started_at = datetime.now(timezone.utc)
                try:
                    await self._handle_issue_opened(
                        SwarmTrigger(
                            kind="issue_opened",
                            repository=repository,
                            number=issue.get("number", 0),
                            title=issue.get("title", ""),
                            body=issue.get("body") or "",
                            author=(issue.get("user") or {}).get("login", ""),
                            html_url=issue.get("html_url", ""),
                            delivery_id=f"resume-{issue.get('number')}",
                            action="opened",
                            labels=[
                                lbl.get("name", "")
                                for lbl in (issue.get("labels") or [])
                                if isinstance(lbl, dict)
                            ],
                        )
                    )
                    summary["resumed"] += 1
                    if not await self._pipeline_advanced_past_triage(
                        repository, issue.get("number", 0), resume_started_at
                    ):
                        summary["stalled"] += 1
                        log.error(
                            f"[{DAEMON_NAME}] resume sweep: {ref} resumed but "
                            "never advanced past triage (the issue was not "
                            "touched again after the resume began) — this "
                            "will repeat on every restart; escalating instead "
                            "of re-queuing"
                        )
                        self.notifier.send(
                            f"Resumed issue pipeline on {ref} completed without "
                            "advancing past triage — needs manual investigation "
                            "(the pipeline may be stuck in a way that survives "
                            "restart).",
                            priority=Priority.OPERATOR_DECISION,
                            handler=DAEMON_NAME,
                        )
                except Exception as exc:
                    summary["failed"] += 1
                    log.error(
                        f"[{DAEMON_NAME}] resume sweep: {ref} failed to resume "
                        f"({exc})",
                        exc_info=True,
                    )
        if summary["scanned"]:
            log.info(f"[{DAEMON_NAME}] resume sweep: {summary}")
        return summary

    async def _clear_closed_issue_markers(self, repositories: list[str]) -> int:
        """Delete pipeline markers left behind on CLOSED issues.

        `_clear_pipeline_inflight` runs in a `finally`, but it is deliberately
        best-effort: any failure (a Neotoma/GitHub blip, a token expiry, or the
        daemon being killed between the last agent and the clear) only logs a
        warning. The marker then outlives its run.

        Nothing reclaimed those markers before this sweep. The resume sweep
        scans `state=open` only — correctly, since a closed issue's pipeline is
        moot — so a marker orphaned on an issue that later closed was never
        looked at again. It is invisible in the API but GitHub renders a
        marker-only comment as the "No description provided." placeholder, so
        each one shows up as a blank swarm comment on the thread forever.

        Bounded and fail-open, same discipline as the resume sweep: every
        failure logs and continues, and the count is returned for
        logging/tests. Open issues are untouched — a marker there may belong
        to a live run, and reaping it would strand that pipeline.

        The bound is best-effort, not exhaustive: this reads the first page of
        the 100 most-recently-updated closed issues, and the first page of each
        one's comments. A marker below either cut-off is not reaped on this
        pass. That is deliberate — the sweep runs on every boot, markers post
        early in a thread, and the cost is one lingering cosmetic comment, so
        an exhaustive crawl (N+1 requests per repo, unbounded) is not worth
        paying at startup. Revisit if closed-issue volume grows enough that
        orphans start surviving repeated boots.
        """
        cleared = 0
        for repository in repositories:
            try:
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.get(
                        f"https://api.github.com/repos/{repository}/issues",
                        params={
                            "state": "closed",
                            "per_page": 100,
                            "sort": "updated",
                            "direction": "desc",
                        },
                        headers=self._github_headers(repository),
                    )
                    resp.raise_for_status()
                    for issue in resp.json():
                        # /issues returns PRs too; a PR never carries this marker.
                        if issue.get("pull_request"):
                            continue
                        number = issue.get("number")
                        comments = await client.get(
                            f"https://api.github.com/repos/{repository}/issues/"
                            f"{number}/comments",
                            params={"per_page": 100},
                            headers=self._github_headers(repository),
                        )
                        comments.raise_for_status()
                        for c in comments.json():
                            body = c.get("body", "")
                            # Only reap comments that are ONLY a marker. A real
                            # agent comment that merely QUOTES a marker (Lanius
                            # and Vanellus both do, when reporting gate drift)
                            # carries operator-visible reasoning — deleting it
                            # would destroy the audit trail.
                            if not self._PIPELINE_INFLIGHT_RE.fullmatch(
                                body.strip()
                            ):
                                continue
                            await client.delete(
                                f"https://api.github.com/repos/{repository}"
                                f"/issues/comments/{c.get('id')}",
                                headers=self._github_headers(repository),
                            )
                            cleared += 1
                            log.info(
                                f"[{DAEMON_NAME}] cleared a stale pipeline "
                                f"marker on closed {repository}#{number}"
                            )
            except Exception as exc:
                log.warning(
                    f"[{DAEMON_NAME}] stale-marker sweep: could not scan "
                    f"{repository} ({exc}) — skipping"
                )
                continue
        return cleared

    async def _pipeline_advanced_past_triage(
        self, repository: str, number: int, resume_started_at: datetime
    ) -> bool:
        """True if the issue was updated AFTER `resume_started_at` — i.e. the
        resumed pipeline actually touched the issue again (the spec mirror
        PATCHes the issue body on every section, including the first, which
        bumps GitHub's `updated_at`).

        Checking for the mere PRESENCE of the spec-mirror marker
        (`SPEC_MARKER_START`) would be wrong: `_mirror_spec_to_issue` splices
        into a managed region it never deletes, so that marker is permanent
        on any issue that ever completed a single mirror in ANY prior run —
        it would read as "advanced" even when the CURRENT resumed run stalled
        immediately. Comparing `updated_at` against the moment this resume
        began is what actually detects "did this resume make an observable
        step" rather than "did some run, ever, make one."

        Fails OPEN (True) on a fetch/parse error: a stall guard that can't
        check must not itself manufacture a false escalation.
        """
        fields = await self._fetch_issue_fields(repository, number)
        if fields is None:
            log.warning(
                f"[{DAEMON_NAME}] resume sweep: could not verify triage "
                f"progress for {repository}#{number} (fetch failed) — "
                "assuming OK"
            )
            return True
        updated_at = fields.get("updated_at")
        if not updated_at:
            return True
        try:
            updated_dt = datetime.strptime(
                updated_at, "%Y-%m-%dT%H:%M:%SZ"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        return updated_dt > resume_started_at

    async def resume_deferred_reviews(self, repositories: list[str]) -> dict:
        """Re-run PR reviews that were deferred by a usage limit and have matured.

        A panel/aggregation throttled by a session limit posts a
        ``review-deferred-until:<ISO>`` marker instead of a verdict (see
        :meth:`_handle_panel_session_limit`). This sweep — run PERIODICALLY, since
        a reset is hours out and the daemon may not restart in that window — finds
        open PRs whose deferral time has passed and re-dispatches them through
        ``_handle_pr``, which re-runs the whole panel. If the limit has cleared,
        a real verdict lands; if not, a fresh (later) deferral marker replaces it.

        Bounded and fail-open, same discipline as resume_interrupted_pipelines:
          * only OPEN PRs with a MATURED marker are candidates;
          * the marker's timestamp gates re-dispatch, so an unmatured deferral is
            left alone (no busy-loop before the reset);
          * a re-run posts a new marker or a verdict, superseding the old one, so
            a still-limited PR can't thrash — it just re-defers to the next reset;
          * every failure path logs and continues.
        """
        summary = {"scanned": 0, "resumed": 0, "not_yet": 0, "failed": 0}
        now = datetime.now(timezone.utc)
        for repository in repositories:
            try:
                prs = await self._prs_with_matured_deferral(repository, now)
            except Exception as exc:
                log.warning(
                    f"[{DAEMON_NAME}] deferred-review sweep: could not scan "
                    f"{repository} ({exc}) — skipping"
                )
                continue
            summary["scanned"] += prs["scanned"]
            summary["not_yet"] += prs["not_yet"]
            for pr in prs["due"]:
                ref = f"{repository}#{pr.get('number')}"
                log.info(
                    f"[{DAEMON_NAME}] deferred-review sweep: {ref} deferral has "
                    "matured — re-running the panel"
                )
                try:
                    trigger = SwarmTrigger(
                        kind="pr_synchronize",
                        repository=repository,
                        number=int(pr.get("number")),
                        title=pr.get("title", ""),
                        body=pr.get("body") or "",
                        author=(pr.get("user") or {}).get("login", ""),
                        html_url=pr.get("html_url", ""),
                        delivery_id=f"deferred-resume-{pr.get('number')}",
                        action="synchronize",
                        head_ref=(pr.get("head") or {}).get("ref", ""),
                        base_ref=(pr.get("base") or {}).get("ref", ""),
                    )
                    await self._handle_pr(trigger)
                    summary["resumed"] += 1
                except Exception as exc:
                    summary["failed"] += 1
                    log.error(
                        f"[{DAEMON_NAME}] deferred-review sweep: {ref} failed to "
                        f"resume ({exc})",
                        exc_info=True,
                    )
        if summary["scanned"]:
            log.info(f"[{DAEMON_NAME}] deferred-review sweep: {summary}")
        return summary

    async def resume_stalled_reviews(self, repositories: list[str]) -> dict:
        """Re-dispatch open PRs whose review died without leaving any marker.

        The deferred sweep above only rescues PRs that got far enough to post a
        ``review-deferred-until:`` marker. A review that dies EARLIER leaves
        nothing behind — no marker, no formal review, no error anyone reads —
        and with auto-merge keyed on a formal approval the PR simply sits.

        Observed 2026-08-09 on ateles#408: Lanius omitted its verdict line, the
        retry ran while Neotoma writes were failing (neotoma#2141), and no
        verdict came back. Every check was green, the PR was mergeable, and it
        sat for two days with zero formal approvals until a human went looking.
        Nothing in the swarm was watching for "reviewed nothing, said nothing".

        Candidate = an open PR that is
          * older than ``stall_after_seconds`` (default 45m — comfortably past a
            normal panel run, so an in-flight review is never re-dispatched),
          * has NO formal review from anyone, and
          * has no unmatured deferral marker (that is the other sweep's job).

        Re-dispatch is idempotent by construction: ``_handle_pr`` re-runs the
        panel, and a panel that succeeds posts the formal review whose absence
        made the PR a candidate. So a recovered PR stops being a candidate; a PR
        that keeps failing is re-tried each pass and, after
        ``max_stall_retries``, escalated to the operator instead of looping
        forever — an agent that cannot review a given PR will not start being
        able to on the twentieth attempt, and silent infinite retry is how a
        real defect stays invisible.

        Fail-open and bounded, same discipline as the sibling sweeps: every
        failure path logs and continues; a broken sweep must never stop the loop.
        """
        summary = {"scanned": 0, "resumed": 0, "escalated": 0, "failed": 0}
        stall_after = int(os.environ.get("APIS_REVIEW_STALL_SECONDS", "2700"))
        max_retries = int(os.environ.get("APIS_REVIEW_STALL_MAX_RETRIES", "3"))
        now = datetime.now(timezone.utc)

        for repository in repositories:
            try:
                candidates = await self._prs_with_stalled_review(
                    repository, now, stall_after
                )
            except Exception as exc:
                log.warning(
                    f"[{DAEMON_NAME}] stalled-review sweep: could not scan "
                    f"{repository} ({exc}) — skipping"
                )
                continue

            summary["scanned"] += len(candidates)
            for pr in candidates:
                number = int(pr.get("number"))
                ref = f"{repository}#{number}"
                attempts = self._stall_retries.get(ref, 0)

                if attempts >= max_retries:
                    # Stop and tell the operator. Repeating a failing review
                    # forever burns agent budget and buries the cause.
                    if not self._stall_escalated.get(ref):
                        log.error(
                            f"[{DAEMON_NAME}] stalled-review sweep: {ref} still has "
                            f"no formal review after {attempts} re-dispatches — "
                            "not retrying. Operator attention needed."
                        )
                        self.notifier.send(
                            f"🔴 {ref} has no formal review after {attempts} "
                            "automatic re-dispatches. The review agent is failing "
                            "on this PR — check the apis log for why. Merge is "
                            "blocked until a review lands.",
                            priority=Priority.BLOCKER,
                        )
                        self._stall_escalated[ref] = True
                        summary["escalated"] += 1
                    continue

                log.warning(
                    f"[{DAEMON_NAME}] stalled-review sweep: {ref} has no formal "
                    f"review and no pending deferral — re-dispatching "
                    f"(attempt {attempts + 1}/{max_retries})"
                )
                self._stall_retries[ref] = attempts + 1
                try:
                    trigger = SwarmTrigger(
                        kind="pr_synchronize",
                        repository=repository,
                        number=number,
                        title=pr.get("title", ""),
                        body=pr.get("body") or "",
                        author=(pr.get("user") or {}).get("login", ""),
                        html_url=pr.get("html_url", ""),
                        delivery_id=f"stall-resume-{number}-{attempts + 1}",
                        action="synchronize",
                        head_ref=(pr.get("head") or {}).get("ref", ""),
                        base_ref=(pr.get("base") or {}).get("ref", ""),
                    )
                    await self._handle_pr(trigger)
                    summary["resumed"] += 1
                except Exception as exc:
                    summary["failed"] += 1
                    log.error(
                        f"[{DAEMON_NAME}] stalled-review sweep: {ref} re-dispatch "
                        f"failed ({exc})",
                        exc_info=True,
                    )
        if summary["scanned"]:
            log.info(f"[{DAEMON_NAME}] stalled-review sweep: {summary}")
        return summary

    async def resume_unactioned_revisions(self, repositories: list[str]) -> dict:
        """Re-arm a PR whose CHANGES_REQUESTED feedback was addressed by a push.

        ateles#511, task ``ent_4f11684cbf315fcd03900b10``. The fourth recovery
        sweep, and the first that carries a PR PAST a verdict rather than toward
        one. Measured 2026-08-31: 23 of 47 open ateles PRs sit at
        CHANGES_REQUESTED, median 33 days, oldest 67. Over half the queue was
        reviewed successfully, received actionable feedback, and then nothing
        consumed it.

        Why the existing sweeps cannot see this state:
          * ``resume_stalled_reviews`` requires ZERO reviews — these have one.
          * ``resume_deferred_reviews`` needs a marker that was never written.
          * ``resume_missing_lens_reviews`` needs a clear aggregation naming an
            absent lens — here the aggregation is present and blocking.

        Candidate = open PR, standing CHANGES_REQUESTED, head commit NEWER than
        that review, revision settled for ``APIS_REVISION_STALE_SECONDS``.
        Re-dispatch hands the outstanding review body to Cicada via
        ``run_skill("cicada", ...)`` — deliberately NOT ``_handle_pr``. #511
        rules the re-panel out: the PR already has a verdict carrying actionable
        findings, so what is missing is someone acting on them rather than
        another opinion, and re-paneling would spend a full panel run per stuck
        PR across a 23-PR backlog. Cicada's push then re-runs the panel through
        the normal ``pr_synchronize`` path, so the PR leaves this candidate set
        either way (a fresh review is newer than the push).

        Bounded and escalate-once, mirroring ``resume_stalled_reviews``: a PR
        the panel cannot get through will not start working on the twentieth
        attempt, and silent infinite retry is how a real defect stays invisible.
        Fail-open throughout — a broken sweep must never stop the loop.
        """
        summary = {"scanned": 0, "resumed": 0, "escalated": 0, "failed": 0}
        stale_after = int(os.environ.get("APIS_REVISION_STALE_SECONDS", "2700"))
        max_retries = int(os.environ.get("APIS_REVISION_MAX_RETRIES", "3"))
        now = datetime.now(timezone.utc)

        for repository in repositories:
            try:
                candidates = await self._prs_with_unactioned_revisions(
                    repository, now, stale_after
                )
            except Exception as exc:
                log.warning(
                    f"[{DAEMON_NAME}] revision sweep: could not scan "
                    f"{repository} ({exc}) — skipping"
                )
                continue

            summary["scanned"] += len(candidates)
            for pr in candidates:
                number = int(pr.get("number"))
                ref = f"{repository}#{number}"
                # Keyed by head SHA, not by ref: a NEW push is a new attempt and
                # must reset the budget. Keying by ref alone would let three
                # failures on one revision permanently suppress every later
                # revision of the same PR — the PR would be silently dead while
                # its author kept pushing fixes nobody ever reviewed.
                head_sha = ((pr.get("head") or {}).get("sha")) or ""
                key = f"{ref}@{head_sha}"
                attempts = self._revision_retries.get(key, 0)

                if attempts >= max_retries:
                    if not self._revision_escalated.get(key):
                        log.error(
                            f"[{DAEMON_NAME}] revision sweep: {ref} still blocked "
                            f"after {attempts} re-dispatches against {head_sha[:8]} "
                            "— not retrying. Operator attention needed."
                        )
                        self.notifier.send(
                            f"🔴 {ref}: the author pushed a revision answering the "
                            f"requested changes, but {attempts} automatic "
                            "re-reviews failed to produce a verdict. The PR is "
                            "stuck at CHANGES_REQUESTED with its feedback already "
                            "addressed — check the apis log for why the panel "
                            "cannot complete.",
                            priority=Priority.BLOCKER,
                            handler=DAEMON_NAME,
                        )
                        self._revision_escalated[key] = True
                        summary["escalated"] += 1
                    continue

                log.warning(
                    f"[{DAEMON_NAME}] revision sweep: {ref} has CHANGES_REQUESTED "
                    f"from {pr.get('_blocking_review_at')} and a revision pushed "
                    f"at {pr.get('_revision_pushed_at')} that nothing re-reviewed "
                    f"— re-arming (attempt {attempts + 1}/{max_retries})"
                )
                self._revision_retries[key] = attempts + 1
                try:
                    trigger = SwarmTrigger(
                        kind="pr_synchronize",
                        repository=repository,
                        number=number,
                        title=pr.get("title", ""),
                        body=pr.get("body") or "",
                        author=(pr.get("user") or {}).get("login", ""),
                        html_url=pr.get("html_url", ""),
                        delivery_id=(
                            f"revision-resume-{number}-{head_sha[:8]}-{attempts + 1}"
                        ),
                        action="synchronize",
                        head_ref=(pr.get("head") or {}).get("ref", ""),
                        base_ref=(pr.get("base") or {}).get("ref", ""),
                    )
                    # Re-dispatch CICADA against the outstanding feedback — NOT
                    # `_handle_pr`. #511 rules the re-panel out explicitly
                    # ("do not call _handle_pr or re-panel", and re-paneling CR
                    # PRs is listed as out of scope): the PR already HAS a
                    # verdict with actionable findings, so what is missing is
                    # someone acting on them, not another opinion. Re-paneling
                    # would also spend a full panel run per stuck PR across a
                    # 23-PR backlog.
                    result = await run_skill(
                        "cicada",
                        self._cicada_fix_prompt(
                            trigger,
                            self._parent_issue_number(pr.get("body") or ""),
                            attempts + 1,
                            pr.get("_blocking_review_body") or "",
                        ),
                        github_token=_token_for_agent_on_repo("cicada", repository),
                        include_github_contract=True,
                        notifier=self.notifier,
                    )
                    if not result.ok:
                        raise RuntimeError(
                            f"cicada re-dispatch did not complete: "
                            f"{result.error or 'no reason given'}"
                        )
                    summary["resumed"] += 1
                except Exception as exc:
                    summary["failed"] += 1
                    log.error(
                        f"[{DAEMON_NAME}] revision sweep: {ref} re-dispatch failed "
                        f"({exc})",
                        exc_info=True,
                    )
        if summary["scanned"]:
            log.info(f"[{DAEMON_NAME}] revision sweep: {summary}")
        return summary

    async def report_pr_review_queue(self, repositories: list[str]) -> dict:
        """Surface APPROVED-unmerged PRs before they rot into a conflict.

        ateles#565. Three of the four APPROVED PRs found on 2026-08-31 had gone
        CLEAN → DIRTY purely through elapsed time (33, 47 and 53 days). Nothing
        in the swarm was watching, because an APPROVED PR *presents as done* —
        unlike CHANGES_REQUESTED, which at least presents as needing work. The
        only reason those four were found at all was an ad-hoc query.

        This sweep does NOT merge. Merging stays an operator act (see
        ``_merge_pr``'s two flag-gated callers); an approved PR going stale is
        reported, never landed. What it does is make the state visible and
        distinguish, durably, WHY a given approved PR is still open:

          ``clean``    → mergeable now; merge is the only remaining step
          ``dirty``    → already rotted; needs a rebase before it can merge
          ``blocked``  → a required check or branch protection is holding it
          ``behind``   → base moved; needs an update but no conflict yet
          ``unstable`` → non-required check failing
          ``unknown``  → GitHub had not finished computing mergeability

        Escalation is tiered so the report stays worth reading. A PR that just
        got approved is INFO. One that has been approved past
        ``APIS_APPROVED_STALE_DAYS`` (default 3) while still CLEAN is the
        actionable case — mergeable, nobody merging it, and every day it waits
        is a day it can rot — so it pages at BLOCKER. One already DIRTY pages at
        BLOCKER too, but says rebase rather than merge, because merge is no
        longer available to the operator without one.

        Escalate-once per (PR, state): a PR that is re-reported every ten
        minutes trains the operator to ignore the channel, which is the failure
        this sweep exists to fix. The state is part of the key so a CLEAN PR
        that later rots to DIRTY does page again — that transition is exactly
        the moment the operator's available action changed.

        Fail-open: a broken report must never stop the loop.
        """
        stale_days = int(os.environ.get("APIS_APPROVED_STALE_DAYS", "3"))
        report: dict = {
            "scanned": 0,
            "approved_unmerged": 0,
            "stale": 0,
            "rotted": 0,
            # ateles#599/#607: PRs no panel ever reviewed. Counted separately
            # from approved_unmerged on purpose — conflating "nobody objected"
            # with "someone approved" is the whole hazard.
            "unreviewed": 0,
            "escalated": 0,
            "prs": [],
        }
        now = datetime.now(timezone.utc)

        for repository in repositories:
            try:
                candidates = await self._approved_unmerged_prs(repository, now)
            except Exception as exc:
                log.warning(
                    f"[{DAEMON_NAME}] approved-unmerged report: could not scan "
                    f"{repository} ({exc}) — skipping"
                )
                continue

            report["scanned"] += 1
            for pr in candidates:
                number = int(pr.get("number"))
                ref = f"{repository}#{number}"

                # Unreviewed is its own state, reported before the approved
                # branch so it can never be mistaken for one. Absence of a
                # blocking finding is NOT approval — nobody looked.
                if pr.get("_unreviewed"):
                    report["unreviewed"] += 1
                    report["prs"].append(
                        {
                            "ref": ref,
                            "number": number,
                            "repository": repository,
                            "title": pr.get("title", ""),
                            "html_url": pr.get("html_url", ""),
                            "approved_unmerged_age_days": 0,
                            "mergeable_state": "n/a",
                            "unreviewed": True,
                            "rotted": False,
                            "stale": False,
                            "blocker": "never_reviewed",
                        }
                    )
                    notify_key = f"{ref}:unreviewed"
                    if not self._approved_escalated.get(notify_key):
                        self._approved_escalated[notify_key] = True
                        report["escalated"] += 1
                        self.notifier.send(
                            f"🔴 {ref} has NO FORMAL REVIEW and an empty "
                            "reviewDecision. That is an absence of evidence, "
                            "not evidence of cleanliness: this PR presents as "
                            "the safest in the queue to anything that checks "
                            "for blocking findings, while being the least "
                            "verified. Two known causes, both observed on "
                            "2026-08-31 — (a) the body names no parent issue "
                            "with a closes/fixes/resolves keyword, so gate "
                            "inheritance blocks and no panel seats (#599, "
                            "#607); (b) a lens posted [BLOCKING] findings as a "
                            "plain PR comment, which never becomes a formal "
                            "review (#596, #602). Read the PR's actual "
                            f"comments before trusting it. {pr.get('html_url', '')}",
                            priority=Priority.BLOCKER,
                            handler=DAEMON_NAME,
                        )
                    continue

                age = int(pr.get("_approved_age_days") or 0)
                state = str(pr.get("_mergeable_state") or "unknown")
                rotted = state in ("dirty", "blocked")
                stale = age >= stale_days

                entry = {
                    "ref": ref,
                    "number": number,
                    "repository": repository,
                    "title": pr.get("title", ""),
                    "html_url": pr.get("html_url", ""),
                    "approved_at": pr.get("_approved_at"),
                    "approved_unmerged_age_days": age,
                    "mergeable_state": state,
                    "rotted": rotted,
                    "stale": stale,
                    # The operator-facing verdict: what THEY would have to do.
                    "blocker": (
                        "rebase_required"
                        if state == "dirty"
                        else "checks_or_protection"
                        if state == "blocked"
                        else "merge_pending_operator"
                        if state == "clean"
                        else "mergeability_unknown"
                    ),
                }
                report["prs"].append(entry)
                report["approved_unmerged"] += 1
                if stale:
                    report["stale"] += 1
                if rotted:
                    report["rotted"] += 1

                if not (stale or rotted):
                    continue
                notify_key = f"{ref}:{state}"
                if self._approved_escalated.get(notify_key):
                    continue
                self._approved_escalated[notify_key] = True
                report["escalated"] += 1
                if state == "dirty":
                    message = (
                        f"🔴 {ref} was APPROVED {age}d ago and has since rotted to "
                        "CONFLICTING — it can no longer be merged without a rebase. "
                        f"{pr.get('html_url', '')}"
                    )
                elif state == "blocked":
                    message = (
                        f"🔴 {ref} has been APPROVED {age}d and is blocked by a "
                        "required check or branch protection — approval alone will "
                        f"not land it. {pr.get('html_url', '')}"
                    )
                else:
                    message = (
                        f"🔴 {ref} has been APPROVED and mergeable ({state}) for "
                        f"{age}d with nothing merging it. Merge is the only "
                        "remaining step and it needs you — every further day is a "
                        f"day it can rot into a conflict. {pr.get('html_url', '')}"
                    )
                self.notifier.send(
                    message, priority=Priority.BLOCKER, handler=DAEMON_NAME
                )

        if report["approved_unmerged"]:
            log.info(
                f"[{DAEMON_NAME}] approved-unmerged report: "
                f"{report['approved_unmerged']} approved and unmerged "
                f"({report['stale']} stale, {report['rotted']} rotted)"
            )
        return report

    async def _has_live_deferral_marker(
        self, client: httpx.AsyncClient, repository: str, number: int
    ) -> bool:
        """True if `number`'s issue-comment thread ends on an unmatured
        `review-deferred-until:` marker (no verdict or newer marker since).

        Shares the walk-comments-in-order logic `_prs_with_matured_deferral`
        uses to find matured deferrals, but only cares whether a LIVE deferral
        marker exists at all — maturity is irrelevant here, since the deferred
        sweep (`_prs_with_matured_deferral` / `resume_deferred_reviews`) already
        owns re-dispatching once it matures. This just needs to say "hands off,
        someone else's sweep is watching this one."
        """
        c_url = f"https://api.github.com/repos/{repository}/issues/{number}/comments"
        try:
            cresp = await client.get(
                c_url,
                params={"per_page": 100},
                headers=self._github_headers(repository),
            )
            cresp.raise_for_status()
        except Exception:
            # Cannot tell → assume a marker might be live so we don't double-
            # dispatch. Same "false negative costs a delay" bias as the caller.
            return True
        latest_iso = None
        # ateles#430 audit: this scan is CORRECT and must stay last-write-wins.
        # GitHub returns issue comments oldest-first, so the final assignment is
        # the newest marker. Do NOT "optimise" this into an early return on the
        # first match — that is exactly the bug #430 fixed at the two sites that
        # did so. The ordering assumption is the same; the traversal is not.
        for c in cresp.json():
            body = c.get("body", "")
            m = self._REVIEW_DEFERRED_RE.search(body)
            if m:
                latest_iso = m.group(1)
            elif _VANELLUS_COMMENT_MARKER in body:
                latest_iso = None
        return latest_iso is not None

    async def _redispatch_missing_lens(
        self, repository: str, pr: dict, lens_name: str
    ) -> None:
        """Run ONE panel lens against the PR's current head and post its verdict.

        ateles#431. Deliberately not ``_handle_pr``: re-running the whole panel
        re-derives verdicts that already landed, costs four agent runs to obtain
        one, and (before #430) could republish a stale aggregation over a
        current one.

        Raises on failure so the caller counts it and the bounded retry applies.
        """
        lens = lens_by_name(lens_name)
        if lens is None:
            raise ValueError(
                f"no panel lens named '{lens_name}' — refusing to guess a "
                "dispatch target"
            )

        number = int(pr.get("number"))
        trigger = SwarmTrigger(
            kind="pr_synchronize",
            repository=repository,
            number=number,
            title=pr.get("title", ""),
            body=pr.get("body") or "",
            author=(pr.get("user") or {}).get("login", ""),
            html_url=pr.get("html_url", ""),
            delivery_id=f"missing-lens-{lens_name}-{number}",
            action="synchronize",
            head_ref=(pr.get("head") or {}).get("ref", ""),
            base_ref=(pr.get("base") or {}).get("ref", ""),
        )

        # The qa lens authors and runs an eval, so it needs a writable checkout;
        # every other lens is diff-only. Same treatment as the panel loop.
        worktree: str | None = None
        if lens.agent == "phoenicurus":
            worktree = await prepare_pr_worktree(repository, number, lens.agent)
        try:
            result = await run_skill(
                lens.agent,
                self._panelist_prompt(
                    trigger, lens, "", None, has_worktree=bool(worktree)
                ),
                github_token=_token_for_agent_on_repo(lens.agent, repository),
                include_github_contract=True,
                notifier=self.notifier,
                cwd=worktree,
                provider=resolve_lens_provider(
                    lens, available_providers=usable_providers()
                ),
                # Recovery re-runs the SAME lens, so it must be held to the same
                # capability floor. A re-dispatch that quietly downgrades would
                # defeat the floor precisely on the retry path.
                stage=lens.lens,
            )
        finally:
            await cleanup_pr_worktree(worktree)

        if not result.ok:
            raise RuntimeError(
                f"lens '{lens_name}' run failed: "
                f"{result.error or f'rc={result.returncode}'}"
            )

        # Post the verdict as a review:<lens> comment so Vanellus can aggregate
        # it. Without this the lens has run and the aggregation still cannot see
        # it — the same "produced a result nothing consumes" failure this sweep
        # exists to fix.
        await self._post_missing_panel_comments(
            trigger, [(lens.lens, result.stdout)], {lens.lens: lens.agent}
        )

    async def resume_missing_lens_reviews(self, repositories: list[str]) -> dict:
        """Re-dispatch a lens that never answered on an otherwise-clear panel.

        ateles#431. The third recovery sweep, targeting a state the other two
        cannot see: the panel RAN, Vanellus aggregated ``Blocking: 0``, and merge
        is withheld solely because a declared lens produced no verdict.

        Observed on neotoma#2153 (2026-08-19): pm APPROVE, arch SIGNED_OFF, ux
        COMMENT, security NOT RECEIVED. The hold is correct — a green
        ``security_gates`` CI check is an automated gate, not the security
        lens's judgement, and absence must never be read as clear. But nothing
        re-ran the absent lens, so a content-clear PR sat with no mechanism
        watching it:

          * ``lenses_missing_comments`` reposts a CAPTURED verdict whose comment
            failed to land; there is nothing to repost when none was produced.
          * ``resume_stalled_reviews`` requires ZERO reviews — #2153 had four.
          * ``resume_deferred_reviews`` needs a marker the missing lens never
            wrote.
          * an author push is no help: the code is already clear.

        Re-dispatches ONLY the missing lens, never ``_handle_pr``. Re-running the
        whole panel would re-derive verdicts that already landed and, before
        #430, could republish a stale aggregation.

        Bounded and escalate-once, mirroring ``resume_stalled_reviews``: a lens
        that cannot run will not start working on the twentieth attempt, and
        silent infinite retry is how a real defect stays invisible. Fail-open
        throughout — a broken sweep must never stop the loop.
        """
        summary = {"scanned": 0, "resumed": 0, "escalated": 0, "failed": 0}
        max_retries = int(os.environ.get("APIS_MISSING_LENS_MAX_RETRIES", "3"))

        for repository in repositories:
            try:
                candidates = await self._prs_with_missing_lens(repository)
            except Exception as exc:
                log.warning(
                    f"[{DAEMON_NAME}] missing-lens sweep: could not scan "
                    f"{repository} ({exc}) — skipping"
                )
                continue

            for pr, lenses in candidates:
                number = int(pr.get("number"))
                ref = f"{repository}#{number}"
                summary["scanned"] += len(lenses)
                escalated = self._missing_lens_escalated.setdefault(ref, set())
                per_lens = self._missing_lens_retries.setdefault(ref, {})

                for lens in lenses:
                    if lens in escalated:
                        continue
                    attempts = per_lens.get(lens, 0)

                    if attempts >= max_retries:
                        log.error(
                            f"[{DAEMON_NAME}] missing-lens sweep: {ref} lens "
                            f"'{lens}' still produced no verdict after "
                            f"{attempts} re-dispatches — not retrying."
                        )
                        self.notifier.send(
                            f"🔴 {ref}: the '{lens}' lens has produced no verdict "
                            f"after {attempts} automatic re-dispatches. The panel "
                            "is otherwise clear (Blocking: 0), so merge is held "
                            "only on this. A green CI gate is NOT a substitute — "
                            "the lens verdict is still required.",
                            priority=Priority.BLOCKER,
                        )
                        escalated.add(lens)
                        summary["escalated"] += 1
                        continue

                    log.warning(
                        f"[{DAEMON_NAME}] missing-lens sweep: {ref} is clear "
                        f"(Blocking: 0) but lens '{lens}' never reported — "
                        f"re-dispatching it (attempt {attempts + 1}/{max_retries})"
                    )
                    per_lens[lens] = attempts + 1
                    try:
                        await self._redispatch_missing_lens(repository, pr, lens)
                        summary["resumed"] += 1
                    except Exception as exc:
                        summary["failed"] += 1
                        log.error(
                            f"[{DAEMON_NAME}] missing-lens sweep: {ref} lens "
                            f"'{lens}' re-dispatch failed ({exc})",
                            exc_info=True,
                        )
        return summary

    async def _prs_with_missing_lens(
        self, repository: str
    ) -> list[tuple[dict, list[str]]]:
        """Open PRs whose LATEST aggregation is clear but names an absent lens.

        Reads the newest aggregation via ``latest_aggregation_comment`` over the
        fully-paged comment list (#430) — reading the oldest would resurrect a
        long-settled hold and re-dispatch a lens that has since reported.
        """
        list_url = f"https://api.github.com/repos/{repository}/pulls"
        out: list[tuple[dict, list[str]]] = []
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                list_url,
                params={"state": "open", "per_page": 50},
                headers=self._github_headers(repository),
            )
            resp.raise_for_status()
            for pr in resp.json():
                if pr.get("draft"):
                    continue
                number = int(pr.get("number"))
                try:
                    comments = await self._all_issue_comments(
                        repository, number, client
                    )
                except Exception as exc:
                    log.warning(
                        f"[{DAEMON_NAME}] missing-lens sweep: could not read "
                        f"comments on {repository}#{number} ({exc}) — skipping"
                    )
                    continue
                latest = latest_aggregation_comment(comments)
                if not latest:
                    continue
                body = latest.get("body") or ""
                if not is_missing_lens_candidate(body):
                    continue
                lenses = parse_not_received_lenses(body)
                if lenses:
                    out.append((pr, lenses))
        return out

    async def _prs_with_stalled_review(
        self, repository: str, now: datetime, stall_after_seconds: int
    ) -> list[dict]:
        """Open PRs older than the stall window with no formal review at all.

        Deliberately conservative — a PR is only a candidate when ALL hold:
          * updated_at is older than the stall window (an in-flight panel must
            never be re-dispatched on top of itself);
          * zero reviews of any state (a REQUEST_CHANGES is a working pipeline,
            not a stall — the ball is with the author);
          * no live `review-deferred-until:` marker (the deferred sweep owns
            those — re-dispatching here too would double-handle the same PR).
        """
        out: list[dict] = []
        list_url = f"https://api.github.com/repos/{repository}/pulls"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                list_url,
                params={"state": "open", "per_page": 100},
                headers=self._github_headers(repository),
            )
            resp.raise_for_status()
            for pr in resp.json() or []:
                try:
                    updated = datetime.fromisoformat(
                        str(pr.get("updated_at", "")).replace("Z", "+00:00")
                    )
                except ValueError:
                    continue
                if (now - updated).total_seconds() < stall_after_seconds:
                    continue
                if pr.get("draft"):
                    continue
                number = pr.get("number")
                try:
                    rv = await client.get(
                        f"{list_url}/{number}/reviews",
                        headers=self._github_headers(repository),
                    )
                    rv.raise_for_status()
                    if rv.json():
                        # Any formal review means the pipeline reached a verdict.
                        continue
                except Exception:
                    # Cannot tell → do not re-dispatch. A false negative costs a
                    # delay; a false positive re-runs a panel that may be fine.
                    continue
                if await self._has_live_deferral_marker(client, repository, number):
                    # The deferred sweep already owns this PR; re-dispatching
                    # here too would double-handle it (ateles#414).
                    continue
                out.append(pr)
        return out

    async def _prs_with_matured_deferral(self, repository: str, now: datetime) -> dict:
        """Open PRs in `repository` split by whether their deferral has matured.

        Returns {"due": [pr,...], "scanned": n, "not_yet": n}. A PR is 'due' when
        its latest deferral marker's ISO time is <= now. PRs with no deferral
        marker are ignored entirely (not scanned count). Best-effort per PR."""
        result: dict = {"due": [], "scanned": 0, "not_yet": 0}
        list_url = f"https://api.github.com/repos/{repository}/pulls"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                list_url,
                params={"state": "open", "per_page": 100},
                headers=self._github_headers(repository),
            )
            resp.raise_for_status()
            for pr in resp.json():
                num = pr.get("number")
                c_url = (
                    f"https://api.github.com/repos/{repository}/issues/{num}/comments"
                )
                try:
                    cresp = await client.get(
                        c_url,
                        params={"per_page": 100},
                        headers=self._github_headers(repository),
                    )
                    cresp.raise_for_status()
                except Exception:
                    continue
                # Walk comments in order. A deferral is LIVE only if it is the
                # latest terminal marker — i.e. no aggregation verdict was posted
                # AFTER it. Once a real verdict (or a newer deferral) lands, the
                # old deferral is superseded and the PR stops being "due", so a
                # resolved PR is not re-dispatched forever (Loxia #264 review).
                latest_iso = None  # ISO of the most-recent still-live deferral
                for c in cresp.json():
                    body = c.get("body", "")
                    m = self._REVIEW_DEFERRED_RE.search(body)
                    if m:
                        latest_iso = m.group(1)  # a newer deferral supersedes
                    elif _VANELLUS_COMMENT_MARKER in body:
                        latest_iso = None  # a verdict after a deferral clears it
                if latest_iso is None:
                    # No live deferral: either never deferred, or a verdict has
                    # since superseded it. Not a candidate.
                    continue
                result["scanned"] += 1
                try:
                    due_at = datetime.strptime(
                        latest_iso, "%Y-%m-%dT%H:%M:%SZ"
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    # Unparseable marker — treat as due so it can't get stuck.
                    result["due"].append(pr)
                    continue
                if due_at <= now:
                    result["due"].append(pr)
                else:
                    result["not_yet"] += 1
        return result

    # ── merge carrier: past-verdict sweeps (ateles#511, #565) ────────────────
    #
    # The three sweeps above (deferred / stalled / missing-lens) all carry a PR
    # TOWARD a verdict. None of them carries one PAST a verdict, and until this
    # block there was no mechanism that did. Measured 2026-08-31: 23 open PRs at
    # CHANGES_REQUESTED (median 33d, oldest 67d) and 4 APPROVED-and-unmerged for
    # 12/33/47/53 days, three of which rotted from CLEAN to DIRTY while waiting.
    # That is not a review-capacity problem — every one of those PRs was reviewed
    # successfully. It is the absence of a carrier.

    async def _prs_with_unactioned_revisions(
        self, repository: str, now: datetime, stale_after_seconds: int
    ) -> list[dict]:
        """Open PRs at CHANGES_REQUESTED whose head has moved SINCE that review.

        The sibling of ``_prs_with_stalled_review``, targeting the state that
        sweep excludes by design. ``resume_stalled_reviews`` requires ZERO
        reviews, because re-running a panel on an already-reviewed PR would just
        re-post the same verdict. Correct — but it leaves "reviewed, feedback
        addressed, nothing re-armed it" with no watcher at all.

        A PR is a candidate only when ALL hold:
          * the LATEST review from each reviewer nets out to CHANGES_REQUESTED
            (a later APPROVED from the same reviewer clears it — GitHub's own
            reviewDecision semantics; taking the newest review per reviewer is
            what keeps a re-approved PR from being re-armed forever);
          * a commit landed on the head AFTER that review — this is the whole
            signal. Without a newer push the ball is legitimately with the
            author, and re-dispatching would nag an agent to redo work nobody
            asked for. WITH a newer push, the author answered and the review
            never looked again;
          * that push is older than ``stale_after_seconds`` (default 45m), so a
            push whose re-review is still in flight is never double-handled.

        Deliberately NOT keyed on PR age. A 60-day-old PR whose author never
        pushed is not a carrier failure — it is abandoned work, and quietly
        re-dispatching it would bury that fact under a fresh review round. The
        push is what distinguishes "answered" from "abandoned".
        """
        out: list[dict] = []
        list_url = f"https://api.github.com/repos/{repository}/pulls"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                list_url,
                params={"state": "open", "per_page": 100},
                headers=self._github_headers(repository),
            )
            resp.raise_for_status()
            for pr in resp.json() or []:
                if pr.get("draft"):
                    continue
                number = pr.get("number")
                try:
                    rv = await client.get(
                        f"{list_url}/{number}/reviews",
                        params={"per_page": 100},
                        headers=self._github_headers(repository),
                    )
                    rv.raise_for_status()
                    reviews = rv.json() or []
                except Exception:
                    # Cannot tell → do not re-dispatch. Same bias as the sibling
                    # sweeps: a false negative costs a delay, a false positive
                    # burns a panel run on a PR that may be perfectly fine.
                    continue

                blocking_at = self._changes_requested_since(reviews)
                if blocking_at is None:
                    continue

                pushed_at = await self._head_commit_time(client, repository, pr)
                if pushed_at is None or pushed_at <= blocking_at:
                    # No revision since the block: the ball is with the author.
                    continue
                if (now - pushed_at).total_seconds() < stale_after_seconds:
                    # The revision is fresh — a re-review may still be running.
                    continue
                pr["_blocking_review_at"] = blocking_at.isoformat()
                pr["_revision_pushed_at"] = pushed_at.isoformat()
                # Carry the outstanding feedback itself: the re-dispatch hands
                # it to Cicada as guidance, so a candidate without it would send
                # the agent to the PR with nothing to act on.
                pr["_blocking_review_body"] = self._changes_requested_body(reviews)
                out.append(pr)
        return out

    @staticmethod
    def _changes_requested_body(reviews: list[dict]) -> str:
        """Body of the standing CHANGES_REQUESTED review, for the fix prompt.

        Latest-per-reviewer, matching ``_changes_requested_since``: the review
        that still blocks is the one whose findings the author must answer.
        """
        latest: dict[str, dict] = {}
        for review in reviews:
            login = ((review.get("user") or {}).get("login")) or ""
            state = (review.get("state") or "").upper()
            if not login or state == "COMMENTED":
                continue
            prev = latest.get(login)
            if prev is None or (review.get("submitted_at") or "") >= (
                prev.get("submitted_at") or ""
            ):
                latest[login] = review
        bodies = [
            (r.get("body") or "").strip()
            for r in latest.values()
            if (r.get("state") or "").upper() == "CHANGES_REQUESTED"
        ]
        return "\n\n---\n\n".join(b for b in bodies if b)

    @staticmethod
    def _changes_requested_since(reviews: list[dict]) -> datetime | None:
        """Timestamp of the standing CHANGES_REQUESTED, or None if not blocked.

        Mirrors GitHub's own ``reviewDecision``: only the LATEST review per
        reviewer counts, and a reviewer's APPROVED supersedes their own earlier
        CHANGES_REQUESTED. COMMENT reviews are ignored entirely — GitHub does
        not treat them as dismissing a block, and neither may we, or a drive-by
        comment would silently clear a real blocking finding.

        Returns the newest still-standing CHANGES_REQUESTED submission time.
        """
        latest_by_reviewer: dict[str, tuple[datetime, str]] = {}
        for review in reviews:
            state = str(review.get("state", "")).upper()
            if state not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                # COMMENT / PENDING never change the standing decision.
                continue
            login = ((review.get("user") or {}).get("login")) or ""
            raw = str(review.get("submitted_at") or "")
            try:
                at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            prev = latest_by_reviewer.get(login)
            if prev is None or at >= prev[0]:
                latest_by_reviewer[login] = (at, state)
        blocking = [
            at
            for at, state in latest_by_reviewer.values()
            if state == "CHANGES_REQUESTED"
        ]
        return max(blocking) if blocking else None

    async def _head_commit_time(
        self, client: httpx.AsyncClient, repository: str, pr: dict
    ) -> datetime | None:
        """Committer time of the PR's head commit, or None if unreadable.

        Uses the COMMITTER date, not the author date: a rebase or an amend
        rewrites the committer date while preserving the author date, and it is
        the push we care about — "did the branch move after the review" — not
        when the change was originally written.
        """
        sha = ((pr.get("head") or {}).get("sha")) or ""
        if not sha:
            return None
        try:
            resp = await client.get(
                f"https://api.github.com/repos/{repository}/commits/{sha}",
                headers=self._github_headers(repository),
            )
            resp.raise_for_status()
            raw = (
                ((resp.json() or {}).get("commit") or {}).get("committer") or {}
            ).get("date") or ""
            return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:
            return None

    async def _approved_unmerged_prs(
        self, repository: str, now: datetime
    ) -> list[dict]:
        """Open PRs whose standing decision is APPROVED and that are not merged.

        Each returned dict carries ``_approved_at``, ``_approved_age_days`` and
        ``_mergeable_state`` (GitHub's ``mergeable_state``: clean / dirty /
        blocked / behind / unstable / unknown).

        ``mergeable_state`` is only present on the SINGLE-PR endpoint, and
        GitHub computes it lazily — a first read frequently returns "unknown"
        while the background job runs. We report unknown honestly rather than
        retrying into a guess; a sweep that runs every 10 minutes will have it
        on the next pass, and a fabricated "clean" is far worse than a late one.
        """
        out: list[dict] = []
        unreviewed: list[dict] = []
        list_url = f"https://api.github.com/repos/{repository}/pulls"
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                list_url,
                params={"state": "open", "per_page": 100},
                headers=self._github_headers(repository),
            )
            resp.raise_for_status()
            for pr in resp.json() or []:
                if pr.get("draft"):
                    continue
                number = pr.get("number")
                try:
                    rv = await client.get(
                        f"{list_url}/{number}/reviews",
                        params={"per_page": 100},
                        headers=self._github_headers(repository),
                    )
                    rv.raise_for_status()
                    reviews = rv.json() or []
                except Exception:
                    continue
                approved_at = self._standing_approval_at(reviews)
                if approved_at is None:
                    # Not approved. Before moving on, catch the inverse hazard:
                    # a PR with NO reviews at all is not "clear", it is
                    # UNLOOKED-AT, and to any check of the form "are there
                    # unresolved blocking findings?" it presents as the safest
                    # PR in the queue. Measured 2026-08-31: 4 of 13 same-day
                    # PRs (ateles#599, #607, neotoma#2270, #2271) had no parent
                    # issue, so gate inheritance blocked and no panel ever
                    # seated. ateles#599 touches the payment path.
                    #
                    # Same tri-state discipline as ateles#560: unknown must
                    # never collapse into pass. Reported, never merged.
                    if not reviews:
                        pr["_unreviewed"] = True
                        unreviewed.append(pr)
                    continue
                detail: dict = {}
                try:
                    dresp = await client.get(
                        f"{list_url}/{number}",
                        headers=self._github_headers(repository),
                    )
                    dresp.raise_for_status()
                    detail = dresp.json() or {}
                except Exception:
                    detail = {}
                if detail.get("merged"):
                    continue
                pr["_approved_at"] = approved_at.isoformat()
                pr["_approved_age_days"] = max(
                    0, int((now - approved_at).total_seconds() // 86400)
                )
                pr["_mergeable_state"] = str(
                    detail.get("mergeable_state") or "unknown"
                ).lower()
                out.append(pr)
        # Unreviewed PRs ride back on the same return so the caller can report
        # them as a DISTINCT state. They are deliberately not merged into `out`:
        # they are not approved, and an "approved" list that quietly contained
        # never-reviewed PRs would be the exact confusion this guards against.
        return out + unreviewed

    @staticmethod
    def _standing_approval_at(reviews: list[dict]) -> datetime | None:
        """Submission time of the standing approval, or None if not approved.

        Same latest-review-per-reviewer rule as ``_changes_requested_since``.
        A PR counts as approved only when at least one reviewer's latest review
        is APPROVED and NO reviewer's latest is CHANGES_REQUESTED — one standing
        block outranks any number of approvals, exactly as GitHub decides it.
        """
        latest_by_reviewer: dict[str, tuple[datetime, str]] = {}
        for review in reviews:
            state = str(review.get("state", "")).upper()
            if state not in ("APPROVED", "CHANGES_REQUESTED", "DISMISSED"):
                continue
            login = ((review.get("user") or {}).get("login")) or ""
            raw = str(review.get("submitted_at") or "")
            try:
                at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                continue
            prev = latest_by_reviewer.get(login)
            if prev is None or at >= prev[0]:
                latest_by_reviewer[login] = (at, state)
        states = [state for _, state in latest_by_reviewer.values()]
        if "CHANGES_REQUESTED" in states:
            return None
        approvals = [
            at for at, state in latest_by_reviewer.values() if state == "APPROVED"
        ]
        return max(approvals) if approvals else None

    async def _issues_with_inflight_marker(self, repository: str) -> list[dict]:
        """Open issues in `repository` carrying a pipeline marker, in either
        the "queued" or "inflight" stage (ateles#323). Each returned issue dict
        carries an extra `_marker_stage` key set to the LATEST marker's stage
        (a restart between the queued write and the inflight transition can
        briefly leave both markers on the issue; the later comment wins)."""
        out: list[dict] = []
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{repository}/issues",
                params={"state": "open", "per_page": 100},
                headers=self._github_headers(repository),
            )
            resp.raise_for_status()
            for issue in resp.json():
                # /issues returns PRs too; a PR's pipeline is the PR handler's.
                if issue.get("pull_request"):
                    continue
                comments = await client.get(
                    f"https://api.github.com/repos/{repository}/issues/"
                    f"{issue.get('number')}/comments",
                    params={"per_page": 100},
                    headers=self._github_headers(repository),
                )
                comments.raise_for_status()
                stage = None
                for c in comments.json():
                    m = self._PIPELINE_INFLIGHT_RE.search(c.get("body", ""))
                    if m:
                        # Absent stage group == a marker from an older daemon
                        # build; treat it as "inflight" (today's behavior).
                        stage = m.group(2) or "inflight"
                if stage is not None:
                    issue["_marker_stage"] = stage
                    out.append(issue)
        return out

    async def _run_issue_spec_pipeline(self, trigger: SwarmTrigger) -> None:
        ref = f"{trigger.repository}#{trigger.number}"

        # 1. Lanius: new-issue protocol (init gate_status, assign Pavo, label).
        lanius = await run_skill(
            "lanius",
            self._lanius_issue_prompt(trigger),
            github_token=_token_for_agent_on_repo("lanius", trigger.repository),
            include_github_contract=True,
            notifier=self.notifier,
        )
        if not lanius.ok:
            self.notifier.send(
                f"Lanius failed on new issue {ref} — gate init incomplete",
                priority=Priority.BLOCKER,
                handler=DAEMON_NAME,
            )
            # Continue: the additive spec is still useful even if gate init failed.

        # 2. Ordered additive spec sequence. Each selected lens agent runs in
        #    CANONICAL ORDER and contributes exactly ONE section, reading the
        #    accumulated spec-so-far and adding/replacing only its own section.
        sections = self._selected_sections(trigger)
        spec_store = IssueSpecStore(
            self.config.neotoma_base_url, self.config.neotoma_token
        )
        # Idempotent: re-running on the same issue loads existing sections and
        # updates them in place (no duplicate entity, no duplicate section).
        state = await spec_store.load(
            trigger.repository, trigger.number, trigger.title
        )

        completed: list[str] = []
        for section in sections:
            spec_so_far = assemble_spec_markdown(state.sections or {})
            result = await run_skill(
                section.agent,
                self._spec_section_prompt(trigger, section, spec_so_far),
                github_token=_token_for_agent_on_repo(
                    section.agent, trigger.repository
                ),
                include_github_contract=True,
                notifier=self.notifier,
            )
            section_text = self._extract_section_text(result.stdout, section)
            # Persist ADDITIVELY: correct only this section's field. Even when
            # extraction yields nothing usable we still record the turn ran so
            # the sequence_state and mirror reflect progress.
            state = await spec_store.upsert_section(state, section, section_text)
            completed.append(section.key)
            # Mirror after each section so the issue body reflects the growing
            # spec incrementally (and re-running only replaces the marked block).
            await self._mirror_spec_to_issue(trigger, state)

        await spec_store.mark_mirrored(state)

        # 3. Auto-build handoff (rollout-flagged). When ON and gates are green,
        #    chain into an implementation PR so the PR gate pipeline takes over
        #    (and ultimately stops at the operator's merge approval). When OFF,
        #    STOP and notify that the spec is ready awaiting `build` approval.
        gates_green = await self._gates_green(
            lanius, trigger.repository, trigger.number
        )
        if self.config.auto_build and gates_green:
            pr_url = await self._open_implementation_pr(trigger, state)
            self.notifier.send(
                f"Issue {ref}: additive spec assembled ("
                f"{', '.join(completed) or 'none'}); "
                + (
                    f"auto-build ON — implementation PR opened ({pr_url}); the "
                    "PR gate pipeline now owns it (merge stays operator-gated)."
                    if pr_url
                    else "auto-build ON, but the Cicada build handoff opened NO "
                    "PR (spec may be incomplete or the build produced nothing). "
                    "Spec is ready; open the build manually or re-run once the "
                    "spec is implementable."
                ),
                # A successfully-opened PR is FYI — the PR gate pipeline owns it
                # and will stop at the operator's merge approval, so this needs
                # no immediate action and belongs in the digest. The NO-PR branch
                # is a real failure the operator must act on, so it stays an
                # immediate operator decision.
                priority=(
                    Priority.INFO if pr_url else Priority.OPERATOR_DECISION
                ),
                handler=DAEMON_NAME,
            )
        else:
            reason = (
                "auto-build OFF"
                if not self.config.auto_build
                else "gates not green"
            )
            self.notifier.send(
                f"Issue {ref}: additive spec assembled in order "
                f"({', '.join(completed) or 'none'}); Lanius"
                f"{'✓' if lanius.ok else '✗'}. "
                f"Spec is ready and awaiting `build` approval ({reason}). "
                "No PR opened; nothing auto-merged.",
                priority=Priority.OPERATOR_DECISION,
                handler=DAEMON_NAME,
            )

    async def _gates_green(
        self, lanius: SkillResult, repository: str, issue_number: int
    ) -> bool:
        """Whether every pre-impl gate is cleared in the SYSTEM OF RECORD.

        ateles#460. This used to read only Lanius's stdout: "green" meant
        Lanius did not print `GATE_INHERITANCE: blocked`. That is a report
        ABOUT gate state, not gate state, and the two diverge whenever a turn
        ends without an explicit block while a gate is still `pending`.

        Tolerable while auto-build was off, because the only consequence was a
        notification. With it on, this decides whether an agent starts writing
        code — and on the first two handoffs after enabling it, Cicada was
        dispatched onto issues whose `gate_status` still showed `arch: pending`
        (and once `ux: pending` too). Cicada refused both times and said so:

            Apis auto-build dispatch claimed green pre-impl gates while live
            gate_status still shows arch/ux pending

        The Lanius verdict is kept as a VETO — an explicit `blocked` still
        fails — but silence no longer passes. Fails CLOSED when the entity
        cannot be read: declining to build costs a delay, building on unsigned
        gates costs a PR nobody authorised.
        """
        if not lanius.ok:
            return False
        if parse_gate_verdict(lanius.stdout) == "blocked":
            return False

        ref = f"{repository}#{issue_number}"
        try:
            store = IssueGateStore(
                self.config.neotoma_base_url, self.config.neotoma_token
            )
            state = await store.load(repository, issue_number)
        except Exception as exc:  # noqa: BLE001 — fail closed, never crash
            log.warning(
                f"[{DAEMON_NAME}] {ref}: gate_status read failed ({exc}) — "
                "treating gates as NOT green (fail closed)"
            )
            return False

        if not state.found:
            log.warning(
                f"[{DAEMON_NAME}] {ref}: no issue entity, so no gate_status to "
                "check — treating gates as NOT green (fail closed)"
            )
            return False

        unsigned = [
            gate
            for gate in PRE_IMPL_GATES
            if (state.gate_status.get(gate) or "pending").strip().lower()
            not in CLEARED_GATE_STATES
        ]
        if unsigned:
            # Lanius said nothing and the record disagrees. That divergence is
            # the bug's signature, so name it rather than failing quietly.
            log.warning(
                f"[{DAEMON_NAME}] {ref}: Lanius reported no block, but "
                f"gate_status still has {', '.join(unsigned)} uncleared — "
                "not handing off to build (ateles#460)"
            )
            return False
        return True

    @staticmethod
    def _extract_section_text(stdout: str, section: SpecSection) -> str:
        """Pull the agent's section contribution out of its stdout.

        The section prompt asks the agent to wrap its section between the
        fences ``<<<SPEC_SECTION>>>`` … ``<<<END_SPEC_SECTION>>>``.  When those
        fences are present we take exactly what is inside (the additive
        contract).  When absent (a terse agent), we fall back to the trimmed
        stdout so the section is never silently empty.

        In BOTH paths the result is rejected (returns "") when it is only
        conversational narration ("I have successfully completed the
        Engineering section…") rather than an actual spec — the #1882 defect
        where the fence-fallback stored the agent's summary-of-its-work as the
        section, leaving the implementer nothing to build. Returning "" flags
        the section as not-produced so the pipeline can surface it, rather than
        mirroring garbage.
        """
        blob = stdout or ""
        start = blob.find("<<<SPEC_SECTION>>>")
        end = blob.find("<<<END_SPEC_SECTION>>>")
        if start != -1 and end != -1 and end > start:
            inner = blob[start + len("<<<SPEC_SECTION>>>"):end]
            cleaned = _sanitize_section_body(inner, section)
            return "" if _is_only_narration(cleaned) else cleaned
        # Fallback: a terse/non-conforming agent emitted no fences. Sanitize
        # hard — its raw stdout may echo the managed markers and/or its own
        # section heading, which (once the assembler re-adds the heading and
        # the mirror re-wraps the markers) produced the duplicated-header +
        # nested-marker corruption seen on the first live run (#180). And if the
        # unfenced stdout is only narration, reject it (root cause of #1882's
        # Engineering section being meta-commentary, not a spec).
        cleaned = _sanitize_section_body(blob, section)
        return "" if _is_only_narration(cleaned) else cleaned

    async def _mirror_spec_to_issue(
        self, trigger: SwarmTrigger, state: SpecState
    ) -> None:
        """Mirror the assembled spec into the GitHub issue BODY.

        Assembles ``state.sections`` in canonical order and splices the result
        into the issue description between the managed markers, preserving the
        human-written body above them (never clobbers the reporter's text).
        Re-running replaces only the marked block.  Best-effort, never raises.
        """
        if not _token_for_repo(trigger.repository) and not self.config.github_token:
            log.warning(
                f"[{DAEMON_NAME}] no GitHub token — spec mirror skipped for "
                f"{trigger.repository}#{trigger.number}"
            )
            return
        managed = assemble_spec_markdown(state.sections or {})
        issue_url = (
            f"https://api.github.com/repos/{trigger.repository}/issues/"
            f"{trigger.number}"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # Read the CURRENT body so we splice against live content (the
                # reporter or another agent may have edited it since open).
                resp = await client.get(
                    issue_url, headers=self._github_headers(trigger.repository)
                )
                resp.raise_for_status()
                current_body = resp.json().get("body") or ""
                new_body = splice_managed_block(current_body, managed)
                if new_body == current_body:
                    log.debug(
                        f"[{DAEMON_NAME}] spec mirror no-op for "
                        f"{trigger.repository}#{trigger.number} (unchanged)"
                    )
                    return
                patch = await client.patch(
                    issue_url,
                    json={"body": new_body},
                    headers=self._github_headers(trigger.repository),
                )
                patch.raise_for_status()
                log.info(
                    f"[{DAEMON_NAME}] mirrored spec into issue body of "
                    f"{trigger.repository}#{trigger.number}"
                )
        except Exception as exc:
            log.error(
                f"[{DAEMON_NAME}] spec mirror failed for "
                f"{trigger.repository}#{trigger.number}: {exc}"
            )

    async def _open_implementation_pr(
        self, trigger: SwarmTrigger, state: SpecState
    ) -> str | None:
        """Chain into implementation: dispatch Cicada to open a build PR.

        Only reached when ``ATELES_SWARM_AUTO_BUILD`` is ON and gates are green.
        Cicada is instructed to branch off FRESH origin/main in a worktree,
        implement the assembled spec, and open a PR whose body references the
        issue (`Closes #<n>`) so the existing ``_handle_pr`` gate pipeline takes
        over.  This method NEVER merges anything.

        Returns the opened PR URL when a PR was ACTUALLY opened, else None.
        A successful dispatch (exit 0) is NOT sufficient: on #1882 Cicada
        returned ok in ~30s with no PR (it had a narration-only Engineering
        section to build from). We therefore CONFIRM a real PR exists — first
        from a URL in Cicada's stdout, then by querying GitHub for an open PR
        referencing this issue — so the caller can notify honestly instead of a
        false "PR gate pipeline now owns it".
        """
        result = await run_skill(
            "cicada",
            self._cicada_build_prompt(trigger, state),
            github_token=_token_for_agent_on_repo("cicada", trigger.repository),
            include_github_contract=True,
            notifier=self.notifier,
        )
        if not result.ok:
            log.error(
                f"[{DAEMON_NAME}] Cicada build handoff failed for "
                f"{trigger.repository}#{trigger.number}: "
                f"{result.error or f'rc={result.returncode}'}"
            )
            return None

        # 1. A PR URL in Cicada's stdout (the happy path — it reports the link).
        url = _extract_pr_url(result.stdout or "", trigger.repository)
        if url:
            return url

        # 2. No URL in stdout — confirm against GitHub. Query open PRs that
        #    reference this issue (Closes #<n> in the body). If Cicada actually
        #    opened one, this finds it; if it produced nothing (the #1882 case),
        #    this returns None and the caller reports "handoff ran but no PR".
        found = await self._find_open_pr_for_issue(trigger)
        if not found:
            # Persist the COMPLETE child output. `run_skill` only writes a
            # diagnostics file when the dispatch FAILS (rc != 0); this branch is
            # the exit-0-but-did-nothing case, which used to leave no artifact at
            # all — the reason the #1882-class failure has been undiagnosable
            # across eight issues. The post-condition failing IS the failure, so
            # capture it here with the same writer and name the file in the log.
            log_path = await asyncio.to_thread(
                write_dispatch_failure_log,
                skill="cicada",
                role="cicada",
                returncode=result.returncode,
                stdout=result.stdout or "",
                stderr=result.stderr or "",
                task_entity_id=f"{trigger.repository}#{trigger.number}",
            )
            log.warning(
                f"[{DAEMON_NAME}] Cicada build handoff for "
                f"{trigger.repository}#{trigger.number} returned ok but NO PR "
                f"was opened ({len(result.stdout or '')}B stdout) — reporting "
                "no-PR so the operator can build manually. Full output: "
                f"{log_path or '(diagnostics file unavailable)'}"
            )
        return found

    async def _find_open_pr_for_issue(self, trigger: SwarmTrigger) -> str | None:
        """Return the URL of an open PR referencing this issue, or None.

        Matches a PR whose body references the issue number (``Closes #<n>`` /
        ``#<n>``). Best-effort GitHub REST; never raises.
        """
        n = trigger.number
        api = (
            f"https://api.github.com/repos/{trigger.repository}/pulls"
            "?state=open&per_page=30&sort=created&direction=desc"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    api, headers=self._github_headers(trigger.repository)
                )
                resp.raise_for_status()
                for pr in resp.json() or []:
                    body = pr.get("body") or ""
                    ref = re.search(rf"#\b{n}\b", body)
                    if ref:
                        return pr.get("html_url")
        except Exception as exc:  # noqa: BLE001 — best-effort
            log.warning(
                f"[{DAEMON_NAME}] PR-confirmation query failed for "
                f"{trigger.repository}#{n}: {exc}"
            )
        return None

    async def _resolve_pr_number(self, trigger: SwarmTrigger) -> int | None:
        """The PR number to act on for an approval trigger.

        A ``pr_review`` trigger, an ``email_approve`` trigger, and a comment ON
        a PR already carry the PR number in ``trigger.number`` (email_approve's
        is set + validated ``> 0`` by the gateway's /approve-email route). A
        ``/approve`` on the PARENT ISSUE carries the issue number, so we resolve
        the open PR that references it. Returns None when no PR can be
        identified (approval then merges nothing).
        """
        if (
            trigger.kind in ("pr_review", "email_approve")
            or getattr(trigger, "comment_on_pr", False)
        ):
            return trigger.number or None
        # Comment on an issue → find the linked open PR and parse its number.
        url = await self._find_open_pr_for_issue(trigger)
        if not url:
            return None
        m = re.search(r"/pull/(\d+)", url)
        return int(m.group(1)) if m else None

    async def _merge_pr(
        self, repo: str, pr_number: int, method: str = "squash"
    ) -> tuple[bool, str]:
        """Merge a PR via the GitHub REST API. Returns (merged, detail).

        Uses ``PUT /repos/{repo}/pulls/{n}/merge`` rather than shelling out to
        ``gh`` because the dispatcher runs with no git-repo cwd. Fail-safe: any
        error returns ``(False, <reason>)`` so the caller reports honestly and
        NEVER claims a merge that did not happen. GitHub returns 405 when the PR
        is not mergeable (conflicts, failing required checks, already merged),
        409 on a base-branch race — both surface as an honest "not merged".
        """
        method = method if method in ("squash", "merge", "rebase") else "squash"
        api = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/merge"
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.put(
                    api,
                    headers=self._github_headers(repo),
                    json={"merge_method": method},
                )
        except Exception as exc:  # noqa: BLE001 — never raise into the caller
            return False, f"merge request error: {exc}"
        if resp.status_code == 200:
            body = resp.json() if resp.content else {}
            if body.get("merged"):
                return True, body.get("sha", "")
            return False, f"GitHub reported not-merged: {body.get('message', '')}"
        # 405 not-mergeable, 409 race, 404 missing, 403 perms — all honest fails.
        detail = ""
        try:
            detail = (resp.json() or {}).get("message", "")
        except Exception:  # noqa: BLE001
            detail = (resp.text or "")[:200]
        return False, f"HTTP {resp.status_code}: {detail}"

    # ── pull_request pipeline ────────────────────────────────────────────────

    async def _handle_pr(self, trigger: SwarmTrigger) -> None:
        ref = f"{trigger.repository}#{trigger.number}"
        parent = self._parent_issue_number(trigger.body)

        # 0. Pipeline-bypass guard (ateles): a product-code PR with NO parent
        #    issue skipped the gated issue → pm/arch → Cicada path. Lanius still
        #    fails open for review below (merge stays operator-gated), but the
        #    bypass must be surfaced LOUDLY — a visible PR comment + an operator
        #    notification — instead of being silently retro-initialized as a
        #    "legacy independent fix". Best-effort; never blocks the pipeline.
        if parent is None:
            bypass_files = await self._changed_files(trigger)
            if touches_product_code(bypass_files):
                log.info(
                    f"[{DAEMON_NAME}] {ref}: product-code PR with no parent "
                    "issue — surfacing pipeline bypass (review still proceeds)"
                )
                newly_surfaced = await self._post_pipeline_bypass_comment(
                    trigger
                )
                # Only ping the operator the first time the bypass is surfaced.
                # Later PR events (synchronize/label/reopen) re-enter this path
                # for the same PR and would otherwise re-notify identically.
                if newly_surfaced:
                    self.notifier.send(
                        f"PR {ref} touched product code with no parent issue — it "
                        "bypassed the gated pm/arch → Cicada pipeline. Review "
                        "proceeds; merge stays operator-gated. File the issue and "
                        "add `Closes #N`, or accept the bypass.",
                        priority=Priority.OPERATOR_DECISION,
                        handler=DAEMON_NAME,
                    )

        # 1. Lanius: enforce PR gate inheritance against the parent issue.
        _lanius_token = _token_for_agent_on_repo("lanius", trigger.repository)
        lanius = await run_skill(
            "lanius",
            self._lanius_pr_prompt(trigger, parent),
            github_token=_lanius_token,
            include_github_contract=True,
            notifier=self.notifier,
        )
        verdict = parse_gate_verdict(lanius.stdout)
        if verdict is None:
            # PR-87 self-dogfood finding: Lanius sometimes replies without the
            # mandatory verdict line. One sharper retry before failing open.
            # Applies whether the skill run reported ok (verdict line simply
            # missing) or not (e.g. it crashed) — either way there has been no
            # verdict yet, and the retry is the one chance to get one before
            # the fail-open path below kicks in.
            log.warning(
                f"[{DAEMON_NAME}] {ref}: Lanius produced no verdict "
                f"(lanius.ok={lanius.ok}) — retrying once with an explicit "
                "reminder"
            )
            lanius = await run_skill(
                "lanius",
                self._lanius_pr_prompt(trigger, parent)
                + (
                    "\n\nREMINDER: your previous attempt omitted the mandatory "
                    "final line. End with exactly `GATE_INHERITANCE: clear` or "
                    "`GATE_INHERITANCE: blocked`. If you cannot verify the "
                    "gates from here, emit `GATE_INHERITANCE: clear` — review "
                    "proceeds and merge stays operator-gated regardless."
                ),
                github_token=_lanius_token,
                include_github_contract=True,
                notifier=self.notifier,
            )
            verdict = parse_gate_verdict(lanius.stdout)

        if verdict is None:
            # The retry above promised "one sharper retry before failing open"
            # but nothing actually failed open: a still-missing verdict fell
            # through to `if verdict == "blocked"`, which None fails, so the PR
            # proceeded without gate inheritance ever resolving — and, in the
            # observed case, without a formal review ever being posted. With
            # auto-merge keyed on that formal review, the PR then sits
            # indefinitely with nothing reporting a problem.
            #
            # Observed 2026-08-09 on ateles#408: Lanius omitted the verdict, the
            # retry ran while Neotoma writes were failing (neotoma#2141), and no
            # verdict came back. The PR stayed open for two days with 0 formal
            # approvals while every check was green. A human eventually noticed.
            #
            # Fail OPEN, deliberately. `clear` means "gate inheritance did not
            # block this" — it does not approve anything. The review panel still
            # runs, the lenses still reach their own verdicts, and merge still
            # requires a formal approval. The alternative (fail closed) would
            # wedge every PR whenever the gate agent is flaky or Neotoma is
            # briefly unwell, which is a worse failure than proceeding to a
            # review that can still say no. This mirrors the guidance already
            # given to Lanius in the retry prompt.
            reason = (
                "skill run failed" if not lanius.ok else "no verdict line after retry"
            )
            log.error(
                f"[{DAEMON_NAME}] {ref}: gate inheritance unresolved ({reason}) — "
                "failing OPEN to `clear` so the review panel still runs. Merge "
                "remains gated on a formal approval."
            )
            verdict = "clear"

        # Gates Lanius reports as still-pending (from its GATE_PENDING: line on a
        # blocked verdict). Fed to select_panel so the owning lens is guaranteed
        # a seat under the cap — otherwise a carried-over gate can never clear
        # because its owning lens is never re-invoked (ateles#230 panel gap).
        pending_gates = parse_pending_gates(lanius.stdout)
        if verdict == "blocked":
            # ateles#230: on the FIRST look a gate-blocked PR should skip the
            # panel — nothing has been reviewed, so there is no finding to
            # re-evaluate and running the panel would just front-run the gates.
            # But on a RE-PUSH the same skip is the autonomy hole: an author can
            # push exactly the fix a lens asked for and nothing re-evaluates it;
            # the PR sits until a human manually re-invokes the reviewer. The
            # `max_fix_rounds` contract above already assumes "the push
            # (synchronize) re-runs the panel" — this restores that for
            # externally-authored pushes too, not just Cicada's.
            #
            # SELF-CERTIFICATION BOUNDARY (ateles#230 arch §4): re-REVIEW is not
            # re-APPROVE. This re-runs the LENS AGENTS, which reach their own
            # verdicts and own their own gate_status mutations (Lanius/lens own
            # that — the dispatcher never writes gate state here, and never
            # infers sign-off from the fact that a push happened).
            if (
                self.config.auto_rereview_on_push
                and trigger.kind in _AUTO_REREVIEW_TRIGGER_KINDS
            ):
                log.info(
                    f"[{DAEMON_NAME}] {ref}: pre-impl gates open, but this is a "
                    f"{trigger.kind} — auto-re-reviewing against the new head "
                    "(ATELES_SWARM_AUTO_REREVIEW=1)"
                )
                # Fall through to the panel: the lenses re-evaluate the new head
                # and either re-affirm the block, raise a new finding, or sign
                # off. Merge stays behind APIS_AUTONOMY_AUTO_MERGE regardless.
            else:
                log.info(
                    f"[{DAEMON_NAME}] {ref}: pre-impl gates open — panel skipped"
                )
                self.notifier.send(
                    f"PR {ref} blocked by Lanius — pre-impl gates not signed off"
                    + (
                        ""
                        if trigger.kind not in _AUTO_REREVIEW_TRIGGER_KINDS
                        else " (re-push: set ATELES_SWARM_AUTO_REREVIEW=1 to "
                        "auto-re-review pushes against open gates)"
                    ),
                    priority=Priority.INFO,
                    handler=DAEMON_NAME,
                )
                return
        if not verdict:
            log.warning(
                f"[{DAEMON_NAME}] {ref}: Lanius emitted no GATE_INHERITANCE "
                "verdict — proceeding to panel (fail-open for review, "
                "merge stays gated)"
            )

        # 2. Assemble the review panel (neotoma#1640): pre-registered agents
        #    from the parent issue ∪ diff-surface matches ∪ downstream lenses.
        changed_files = await self._changed_files(trigger)
        expectations = (
            await self._preregistered_expectations(trigger.repository, parent)
            if parent
            else {}
        )
        panel = select_panel(
            gate_contributors=set(expectations),
            changed_files=changed_files,
            max_panel=self.config.panel_max,
            pending_gates=pending_gates,
        )

        reviews: list[tuple[str, str]] = []
        for lens in panel:
            # QE3: the qa lens (Phoenicurus) authors + runs an eval, so it needs a
            # writable PR-branch checkout as its cwd. Other lenses stay diff-only
            # (cwd=None). Best-effort: prep failure → diff-only fallback, no stall.
            qa_worktree: str | None = None
            if lens.agent == "phoenicurus":
                qa_worktree = await prepare_pr_worktree(
                    trigger.repository, trigger.number, lens.agent
                )
            try:
                result = await run_skill(
                    lens.agent,
                    self._panelist_prompt(
                        trigger,
                        lens,
                        expectations.get(lens.agent, ""),
                        parent,
                        has_worktree=bool(qa_worktree),
                        owns_pending_gate=lens.lens in pending_gates,
                    ),
                    github_token=_token_for_agent_on_repo(
                        lens.agent, trigger.repository
                    ),
                    include_github_contract=True,
                    notifier=self.notifier,
                    cwd=qa_worktree,
                    provider=resolve_lens_provider(
                        lens, available_providers=usable_providers()
                    ),
                    # The lens name IS the stage: `security` and `arch` decide
                    # whether code is safe to merge and carry a strong floor,
                    # while `content` does not. Without this the panel would
                    # happily review on whatever model had headroom.
                    stage=lens.lens,
                )
            finally:
                await cleanup_pr_worktree(qa_worktree)
            if result.ok:
                # Stamp the reviewing model onto the verdict. An approval from a
                # fallback model is weaker evidence than one from the intended
                # model, and the merge path cannot weigh what it cannot see.
                reviews.append((lens.lens, _stamp_review_model(result)))

        # 2b. Persist the captured reviews and backfill any review:<lens>
        #     comment the panelist could not post itself (PR-87 self-dogfood
        #     findings: stdout was the only copy, and Vanellus aggregates
        #     from the PR comments).
        if reviews:
            agents_by_lens = {p.lens: p.agent for p in panel}
            await self._persist_panel_reviews(trigger, reviews, agents_by_lens)
            await self._post_missing_panel_comments(
                trigger, reviews, agents_by_lens
            )

        # 3. Learning pass (ateles#82): systemic findings → operator-gated
        #    proposed_skill_update entities.
        proposals = propose_skill_updates(reviews, pr_ref=ref)
        if proposals:
            await self._store_entities(
                proposals,
                idempotency_key=(
                    f"learning-{ref}-{trigger.delivery_id}-"
                    f"{content_digest(proposals)}"
                ),
            )
            self.notifier.send(
                f"{len(proposals)} systemic review finding(s) on {ref} — "
                f"proposed skill update(s) await operator approval",
                priority=Priority.OPERATOR_DECISION,
                handler=DAEMON_NAME,
            )

        # 4. Vanellus aggregates panel verdicts. Merge is operator-gated
        #    unless APIS_AUTONOMY_AUTO_MERGE=1 (ateles#80 guardrail).
        vanellus_result = await run_skill(
            "vanellus",
            self._vanellus_prompt(
                trigger,
                parent,
                [p.lens for p in panel],
                reviews,
                auto_merge=self.config.auto_merge,
                # ateles#594 gap 3: the autonomy clause must be derived from
                # gate state too, not the flag alone. Lanius's fail-open-for-
                # review path (above) is correct, but that fail-open must not
                # propagate into merge AUTHORIZATION language.
                pending_gates=pending_gates,
            ),
            github_token=_token_for_agent_on_repo("vanellus", trigger.repository),
            include_github_contract=True,
            notifier=self.notifier,
        )

        # 4a-0. Session/usage-limit guard (checked BEFORE auth, since a limit
        #     message can co-occur with auth-ish prose): the aggregation's claude
        #     call was throttled, not un-authenticated. Its "verdict" is just the
        #     limit notice. Post a truthful REVIEW-INCOMPLETE marker (never a stub
        #     that reads like a verdict) and schedule a resume after the reset,
        #     rather than paging the operator to fix nothing.
        if detect_session_limit(vanellus_result.stdout, vanellus_result.stderr):
            await self._handle_panel_session_limit(
                trigger, parent, "vanellus",
                vanellus_result.stdout, vanellus_result.stderr,
            )
            return
        # 4a. Credential-expiry guard: if the aggregation's claude call failed
        #     auth (expired OAuth / missing ANTHROPIC_API_KEY), its "verdict" is
        #     just the 401. Reframe it as an infra failure and page the operator
        #     to re-auth, rather than posting the raw error as a review.
        if detect_auth_failure(vanellus_result.stdout, vanellus_result.stderr):
            await self._handle_panel_auth_failure(trigger, "vanellus")
            return
        # 4b. Dispatcher fallback: if Vanellus's own gh comment did not land
        #     on the PR, post the captured stdout ourselves (mirrors
        #     _post_missing_panel_comments for the aggregation step).
        await self._post_missing_vanellus_comment(trigger, vanellus_result)

        # 5. Act on the verdict — this is the loop closure. Previously the
        #    dispatcher filed a merge checkpoint here UNCONDITIONALLY, ignoring
        #    whether the panel actually blocked. Now:
        #      REQUEST_CHANGES/BLOCKED/unparseable → route findings back for a
        #        fix (lens agents propose, Cicada implements), bounded by
        #        max_fix_rounds; a Cicada push re-runs this whole handler.
        #      APPROVE/COMMENT → readiness gate: only signal merge-ready when
        #        required CI is also green.
        #    The verdict is read from stdout when present, else recovered from
        #    the durable aggregation comment on the PR (ateles#292) — Vanellus
        #    posts the verdict reliably but repeats it in stdout only ~25% of
        #    the time, and a missed token silently downgrades a REQUEST_CHANGES
        #    to an inert COMMENT on GitHub.
        verdict, used_comment_fallback = await self._resolve_review_verdict(
            trigger, vanellus_result.stdout
        )
        if used_comment_fallback:
            log.info(
                f"[{DAEMON_NAME}] {trigger.repository}#{trigger.number}: verdict "
                f"recovered via comment fallback (stdout omitted it) — {verdict!r}"
            )

        # 5a. Emit the verdict as a NATIVE GitHub Review (ateles#241) before
        #     branching, so it lands on BOTH paths — a REQUEST_CHANGES that
        #     routes findings back is exactly as worth recording in GitHub's
        #     review state as an APPROVE that proceeds. Best-effort: a failure
        #     here must not change the routing decision below, which remains
        #     driven by the parsed verdict.
        await self._emit_formal_review(trigger, verdict, vanellus_result.stdout)

        # 5b. ateles#565: a merge the steward was AUTHORIZED to perform and
        #     could not. On PR #452 that refusal (a local permission classifier
        #     denying the Bash tool) was written only into a review body, so it
        #     reached nobody and the PR sat APPROVED and CLEAN for eleven days.
        #     Escalate it as a blocker — the operator is the only party who can
        #     act on it. Best-effort, before the routing branch so it fires on
        #     BOTH paths.
        refusal = parse_merge_refusal(vanellus_result.stdout)
        if refusal:
            self.record_merge_refusal(
                repository=trigger.repository,
                number=trigger.number,
                reason=refusal,
                auto_merge=self.config.auto_merge,
            )

        # 5c. Route or gate. Reads the same body the formal review above read, via
        #     the same predicate, so the two surfaces cannot disagree about the
        #     same blocker: branching on the token alone let a `**COMMENT**` above
        #     a `[BLOCKING]` finding block the PR on GitHub while the dispatcher
        #     told the operator it was merge-ready.
        if review_blocks_merge(verdict, vanellus_result.stdout):
            await self._route_blocking_findings(trigger, parent, reviews, verdict)
            return

        await self._gate_merge_readiness(trigger, parent, panel)

    async def _emit_formal_review(
        self, t: SwarmTrigger, verdict: str | None, body: str
    ) -> str | None:
        """Post the aggregated panel verdict as a native GitHub Review.

        This is ateles#241: the swarm's verdicts previously existed only as prose
        in an issue comment, so GitHub's own review state stayed empty and branch
        protection had nothing to enforce. Emitting a real review event puts the
        verdict where GitHub (and a human scanning the PR) can see it.

        Deliberately dispatcher-side rather than agent-side: headless panelists
        routinely fail their own `gh` calls, and of the lens agents only Vanellus
        and Waxwing even hold `gh pr review` grants.

        Best-effort — returns the review id on success, else None. NEVER raises:
        the routing decision in `_handle_pr` is driven by the parsed verdict, and
        a GitHub hiccup must not change which branch runs.

        Two failures are expected and handled quietly rather than as errors:
        - 422 "Can not approve your own pull request" — the swarm authored the PR
          and is reviewing under the same identity. Falls back to COMMENT so the
          verdict still lands. Resolves once per-agent accounts (#109) ship.
        - 422 on a closed/merged PR — a race between the panel and a merge.
        """
        # ateles#595: the body, not the self-reported token, is the authority on
        # whether this review blocks. A `**COMMENT**` above a `[BLOCKING]`
        # finding must reach reviewDecision as REQUEST_CHANGES.
        event = verdict_to_review_event(verdict, body=body)
        ref = f"{t.repository}#{t.number}"

        if not _token_for_repo(t.repository) and not self.config.github_token:
            log.warning(
                f"[{DAEMON_NAME}] no GitHub token — formal review skipped for {ref}"
            )
            return None

        url = f"https://api.github.com/repos/{t.repository}/pulls/{t.number}/reviews"
        # GitHub rejects a bodyless REQUEST_CHANGES/COMMENT (only APPROVE may be
        # bodyless), so always carry text.
        text = (body or "").strip() or (
            f"Aggregated swarm panel verdict: {verdict or 'unparseable'}."
        )
        payload = {"event": event, "body": text[:65000]}

        async def _post(p: dict) -> httpx.Response:
            async with httpx.AsyncClient(timeout=30) as client:
                return await client.post(
                    url, json=p, headers=self._github_headers(t.repository)
                )

        try:
            resp = await _post(payload)
            downgraded = False
            if resp.status_code == 422 and event != _REVIEW_EVENT_COMMENT:
                # Self-review or otherwise-unacceptable event: degrade to COMMENT
                # so the verdict is still recorded, and say so plainly.
                detail = (resp.text or "")[:200]
                downgraded = True
                log.warning(
                    f"[{DAEMON_NAME}] formal review {event} rejected on {ref} "
                    f"(422) — retrying as COMMENT: {detail}"
                )
                resp = await _post({**payload, "event": _REVIEW_EVENT_COMMENT})
                # A COMMENT does NOT set reviewDecision, so the PR reads as
                # "never reviewed" forever and no gate can ever be satisfied.
                # GitHub refuses REQUEST_CHANGES/APPROVE on your own PR, so when
                # the authoring token and the reviewing token are the same
                # identity every verdict silently becomes non-binding. Measured
                # 2026-09-01: 11 of 15 unreviewed open ateles PRs are
                # ateles-agent reviewing ateles-agent, and 28 such downgrades
                # appear in apis.log. Only a distinct reviewer identity fixes
                # this, so page the operator instead of burying it in a WARNING.
                if await self._claim_escalation(t, "self-review-422"):
                    self.notifier.send(
                        f"PR {ref}: GitHub refused the `{event}` review "
                        f"({detail[:120]}), so it was recorded as a non-binding "
                        "COMMENT and the PR still reads as never-reviewed. The "
                        "review gate cannot be satisfied until the reviewing "
                        "token is a DIFFERENT GitHub identity from the PR "
                        "author. Merge held.",
                        priority=Priority.OPERATOR_DECISION,
                        handler=DAEMON_NAME,
                    )
            resp.raise_for_status()
            review_id = resp.json().get("id")
            # Report what actually landed, not what was attempted — the old log
            # said "posted formal GitHub review REQUEST_CHANGES" even when the
            # request had been downgraded to an inert COMMENT.
            landed = _REVIEW_EVENT_COMMENT if downgraded else event
            log.info(
                f"[{DAEMON_NAME}] posted formal GitHub review {landed} on {ref} "
                f"(id={review_id}, verdict={verdict}"
                f"{', DOWNGRADED from ' + event + ' — non-binding' if downgraded else ''})"
            )
            return str(review_id) if review_id is not None else None
        except Exception as exc:
            # Never fail the handler on a review-post problem.
            log.warning(
                f"[{DAEMON_NAME}] formal review post failed on {ref} "
                f"({event}): {exc}"
            )
            return None

    # ── loop closure: findings → fix → readiness (ateles#179) ────────────────

    # Stable marker for the per-PR fix-round counter comment. The count lives in
    # a hidden HTML comment on the PR so it survives daemon restarts and is
    # head-SHA-agnostic (a bounded loop must count rounds, not commits).
    _FIX_ROUND_MARKER = "<!-- apis-fix-round:{n} -->"
    _FIX_ROUND_RE = re.compile(r"<!-- apis-fix-round:(\d+) -->")

    def _lens_fix_agent(self, lens: str) -> str:
        """Agent that authors fix guidance for a finding raised under `lens`.

        The reviewing agent for the lens owns the domain response (model a:
        reviewer proposes, Cicada implements). Falls back to OWNER_BY_LENS /
        DEFAULT_OWNER for lenses with no dedicated reviewer.
        """
        for lp in LENSES:
            if lp.lens == lens and lp.agent:
                return lp.agent
        return OWNER_BY_LENS.get(lens, DEFAULT_OWNER)

    async def _fix_round_count(self, trigger: SwarmTrigger) -> int:
        """Read the current auto-fix round count from the PR's marker comment."""
        list_url = (
            f"https://api.github.com/repos/{trigger.repository}/issues/"
            f"{trigger.number}/comments"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    list_url,
                    params={"per_page": 100},
                    headers=self._github_headers(trigger.repository),
                )
                resp.raise_for_status()
                best = 0
                for comment in resp.json():
                    m = self._FIX_ROUND_RE.search(comment.get("body", ""))
                    if m:
                        best = max(best, int(m.group(1)))
                return best
        except Exception as exc:
            # Fail-safe: if we cannot read the count, assume we are AT the cap so
            # we escalate to the operator rather than risk an unbounded loop.
            log.warning(
                f"[{DAEMON_NAME}] {trigger.repository}#{trigger.number}: "
                f"fix-round count read failed ({exc}) — treating as at-cap"
            )
            return self.config.max_fix_rounds

    async def _pr_head_sha(self, trigger: SwarmTrigger) -> str | None:
        """Current head SHA of the PR, or None if it cannot be read."""
        url = (
            f"https://api.github.com/repos/{trigger.repository}/pulls/"
            f"{trigger.number}"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    url, headers=self._github_headers(trigger.repository)
                )
                resp.raise_for_status()
                return (resp.json().get("head") or {}).get("sha")
        except Exception as exc:  # noqa: BLE001 — advisory only
            log.warning(
                f"[{DAEMON_NAME}] {trigger.repository}#{trigger.number}: "
                f"could not read head SHA ({exc})"
            )
            return None

    async def _escalate_noop_fix_round(
        self, trigger: SwarmTrigger, kind: str, n: int, before: str | None
    ) -> bool:
        """Escalate when a 'successful' fix round pushed nothing.

        `run_skill(...).ok` only means Cicada's process exited 0 — it does NOT
        mean a commit landed. When Cicada exits clean without pushing, the
        handler logs "awaiting its push to re-review" and the PR enters a
        terminal wait: no push means no `pr_synchronize`, so this handler never
        re-runs, the round cap is never re-evaluated, and the auto-fix-exhausted
        escalation never fires. The PR simply stops, still CHANGES_REQUESTED,
        with nothing owning it.

        Measured 2026-08-31: ateles#570 round 2 dispatched at 17:31Z while its
        head commit stayed at 17:27Z, and no further trigger ever arrived;
        ateles#596 did the same. Comparing the head SHA across the call turns
        that silent stall into a real escalation.

        Returns True if the round was a no-op (caller should stop).
        """
        if before is None:
            return False  # couldn't measure — don't invent a failure
        after = await self._pr_head_sha(trigger)
        if after is None or after != before:
            return False
        ref = f"{trigger.repository}#{trigger.number}"
        log.warning(
            f"[{DAEMON_NAME}] {ref}: {kind} round {n} exited cleanly but pushed "
            f"no commit (head still {before[:7]}) — escalating instead of "
            "waiting for a push that will never arrive"
        )
        if await self._claim_escalation(trigger, f"{kind}-noop"):
            self.notifier.send(
                f"PR {ref}: {kind} round {n} completed without pushing any "
                "commit, so the review can never re-run. Needs your attention. "
                "Merge held.",
                priority=Priority.OPERATOR_DECISION,
                handler=DAEMON_NAME,
            )
        return True

    async def _record_fix_round(self, trigger: SwarmTrigger, n: int) -> None:
        """Post the fix-round marker comment (no command tokens; best-effort)."""
        body = (
            f"{self._FIX_ROUND_MARKER.format(n=n)}\n"
            f"{attribution_header('apis', 'swarm dispatcher')}\n\n"
            f"🔁 Auto-fix round {n} of {self.config.max_fix_rounds}: routing the "
            "panel's blocking findings back to the review agents for guidance, "
            "then to the implementer. A new push re-runs the panel."
        )
        list_url = (
            f"https://api.github.com/repos/{trigger.repository}/issues/"
            f"{trigger.number}/comments"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    list_url,
                    json={"body": body},
                    headers=self._github_headers(trigger.repository),
                )
                resp.raise_for_status()
        except Exception as exc:
            log.warning(
                f"[{DAEMON_NAME}] {trigger.repository}#{trigger.number}: "
                f"could not record fix round {n}: {exc}"
            )

    _ESCALATION_MARKER = "<!-- apis-escalated:{kind} -->"

    async def _claim_escalation(self, trigger: SwarmTrigger, kind: str) -> bool:
        """First-caller-wins guard for a once-per-PR operator escalation.

        A PR re-review fires on every push/label/reopen, so an operator
        escalation (`unparseable` verdict, auto-fix-exhausted) would re-notify
        identically each time — ateles#262 sent four copies of the same
        auto-fix-exhausted ping, neotoma#1988 three. This posts a hidden marker
        comment keyed by `kind`; if the marker already exists it returns False
        so the caller skips the notification. Same edit-not-duplicate primitive
        as the fix-round and bypass markers, and the body carries no command
        token. Fail-OPEN: if the marker can't be read or written we return True
        so a real escalation is never swallowed by a transient GitHub error.
        """
        marker = self._ESCALATION_MARKER.format(kind=kind)
        list_url = (
            f"https://api.github.com/repos/{trigger.repository}/issues/"
            f"{trigger.number}/comments"
        )
        headers = self._github_headers(trigger.repository)
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    list_url, params={"per_page": 100}, headers=headers
                )
                resp.raise_for_status()
                for comment in resp.json():
                    if marker in comment.get("body", ""):
                        log.info(
                            f"[{DAEMON_NAME}] {trigger.repository}"
                            f"#{trigger.number}: '{kind}' already escalated — "
                            "suppressing duplicate operator notification"
                        )
                        return False
                body = (
                    f"{marker}\n"
                    f"{attribution_header('apis', 'swarm dispatcher')}\n\n"
                    f"🔔 Escalated to the operator (`{kind}`). Further PR events "
                    "will not re-notify for this same condition."
                )
                resp = await client.post(
                    list_url, json={"body": body}, headers=headers
                )
                resp.raise_for_status()
                return True
        except Exception as exc:
            log.warning(
                f"[{DAEMON_NAME}] {trigger.repository}#{trigger.number}: "
                f"escalation-dedup check failed for '{kind}' ({exc}) — "
                "notifying anyway (fail-open)"
            )
            return True

    async def _route_blocking_findings(
        self,
        trigger: SwarmTrigger,
        parent: int | None,
        reviews: list[tuple[str, str]],
        verdict: str | None,
    ) -> None:
        """Route panel blocking findings back for an automatic fix (bounded).

        Model (a): each lens's blocking findings go to that lens's reviewing
        agent, which authors domain-specific fix guidance in its own voice. The
        consolidated guidance is handed to Cicada, which implements + pushes;
        the push (synchronize) re-runs the whole PR handler. Bounded by
        max_fix_rounds so an unresolvable finding escalates to the operator
        instead of looping forever. Merge stays operator-gated throughout.
        """
        ref = f"{trigger.repository}#{trigger.number}"

        # Group blocking findings by the lens that raised them.
        by_lens: dict[str, list[ReviewFinding]] = {}
        for lens, text in reviews:
            for f in parse_findings(text, lens=lens):
                if f.blocking:
                    by_lens.setdefault(lens, []).append(f)

        if not by_lens:
            # The review blocked the merge path but no parseable [BLOCKING] block
            # exists in the per-lens reviews. Three ways to get here: a BLOCKED
            # "cannot proceed", a malformed verdict, or — since the routing
            # branch reads the body too — a CLEAR token whose blocker was stated
            # only in Vanellus's aggregation. Don't guess a fix; escalate so a
            # human reads the review. Without this the body-derived blocker would
            # simply land in a different silence than the one it came from.
            # Only notify once per PR: a re-review must not re-ping the operator.
            if await self._claim_escalation(trigger, "unparseable-verdict"):
                self.notifier.send(
                    f"PR {ref}: review verdict `{verdict or 'unparseable'}` did "
                    "not clear the merge path but no blocking findings could be "
                    "parsed from the lens reviews — needs your read. Merge held.",
                    priority=Priority.OPERATOR_DECISION,
                    handler=DAEMON_NAME,
                )
            return

        prior_rounds = await self._fix_round_count(trigger)
        if prior_rounds >= self.config.max_fix_rounds:
            lenses = ", ".join(sorted(by_lens))
            # Once-per-PR: the exhausted-rounds condition is re-evaluated on
            # every subsequent push, so guard the operator ping behind the
            # escalation marker to avoid identical repeats (ateles#262 ×4).
            if await self._claim_escalation(trigger, "auto-fix-exhausted"):
                self.notifier.send(
                    f"PR {ref}: {self.config.max_fix_rounds} auto-fix rounds did "
                    f"not clear review (still blocking on: {lenses}). Escalating "
                    "— needs your attention. Merge held.",
                    priority=Priority.OPERATOR_DECISION,
                    handler=DAEMON_NAME,
                )
            return

        this_round = prior_rounds + 1
        await self._record_fix_round(trigger, this_round)

        # Each lens agent proposes fix guidance for its own findings.
        guidance_blocks: list[str] = []
        for lens in sorted(by_lens):
            agent = self._lens_fix_agent(lens)
            findings_text = "\n".join(
                f"- [{f.category}] {f.summary}\n  {f.detail}".rstrip()
                for f in by_lens[lens]
            )
            result = await run_skill(
                agent,
                self._fix_guidance_prompt(trigger, lens, agent, findings_text),
                github_token=_token_for_agent_on_repo(agent, trigger.repository),
                include_github_contract=True,
                notifier=self.notifier,
            )
            if result.ok and result.stdout.strip():
                guidance_blocks.append(
                    f"### {lens} lens ({agent})\n{result.stdout.strip()}"
                )
            else:
                # Fall back to the raw findings so Cicada still has the work.
                guidance_blocks.append(
                    f"### {lens} lens (guidance unavailable — raw findings)\n"
                    f"{findings_text}"
                )

        consolidated = "\n\n".join(guidance_blocks)

        # Snapshot the head so we can tell whether Cicada actually pushed
        # (a clean exit with no commit strands the PR — see
        # _escalate_noop_fix_round).
        head_before = await self._pr_head_sha(trigger)

        # Hand the consolidated guidance to Cicada to implement + push.
        cicada_result = await run_skill(
            "cicada",
            self._cicada_fix_prompt(trigger, parent, this_round, consolidated),
            github_token=_token_for_agent_on_repo("cicada", trigger.repository),
            include_github_contract=True,
            notifier=self.notifier,
        )
        # An auth-expired claude call can exit 0 with the 401 in stdout, so a
        # bare `ok` is not enough — reframe an auth failure as an infra page.
        if detect_auth_failure(cicada_result.stdout, cicada_result.stderr):
            await self._handle_panel_auth_failure(trigger, "cicada")
            return
        if not cicada_result.ok:
            self.notifier.send(
                f"PR {ref}: auto-fix round {this_round} — Cicada could not apply "
                "the review guidance. Needs your attention. Merge held.",
                priority=Priority.OPERATOR_DECISION,
                handler=DAEMON_NAME,
            )
            return
        if await self._escalate_noop_fix_round(
            trigger, "auto-fix", this_round, head_before
        ):
            return
        log.info(
            f"[{DAEMON_NAME}] {ref}: dispatched auto-fix round {this_round} "
            f"({len(by_lens)} lens(es) → Cicada); awaiting its push to re-review"
        )

    def record_merge_refusal(
        self,
        *,
        repository: str,
        number: int,
        reason: str,
        auto_merge: bool,
    ) -> str:
        """Escalate a refused merge and return its durable authorization state.

        ateles#565. PR #452 was APPROVED, CLEAN, mergeable, and unmerged: the
        steward attempted the merge, a local permission classifier denied the
        Bash tool that would perform it, and the refusal was written only into a
        review body. The classifier sits DOWNSTREAM of the agent's decision to
        merge and UPSTREAM of the `gh pr merge` call — entirely outside the
        swarm's autonomy model — so `APIS_AUTONOMY_AUTO_MERGE=1` authorized a
        merge whose mechanism was then denied, and the flag could not see it.

        The agent's judgement not to route around the denial was correct. The
        defect is that the refusal was terminal AND silent: an APPROVED-unmerged
        PR presents as done, so nothing surfaced it, and #210/#226/#335 rotted
        from MERGEABLE into CONFLICTING through nothing but elapsed time.

        Returns `authorized_but_unable` when the flag authorized the merge —
        distinct and queryable, per the issue's acceptance criteria. Under
        flag-off the refusal is the DESIGNED posture (a checkpoint_brief already
        carries it to the operator), so it records `not_authorized` and does not
        page; paging BLOCKER on every by-design hold would train the operator to
        ignore the channel that matters.

        Fail-open like the sweeps around it: a broken notifier must never turn a
        merge refusal into a crashed dispatch.
        """
        ref = f"{repository}#{number}"
        state = (
            MERGE_STATE_AUTHORIZED_BUT_UNABLE
            if auto_merge
            else MERGE_STATE_NOT_AUTHORIZED
        )

        if not auto_merge:
            log.info(
                f"[{DAEMON_NAME}] {ref}: merge not performed ({state}) — {reason}"
            )
            return state

        log.error(
            f"[{DAEMON_NAME}] {ref}: merge AUTHORIZED but refused ({state}) — "
            f"{reason}"
        )
        try:
            self.notifier.send(
                f"PR {ref}: merge was AUTHORIZED "
                "(APIS_AUTONOMY_AUTO_MERGE=1) but REFUSED by a control outside "
                f"the autonomy model — {reason}.\n\n"
                "The PR is approved and cannot land on its own. You are the "
                "only party who can act on this: merge it by hand, or grant "
                "the mechanism. Left alone an approved PR rots into a merge "
                "conflict and the merge becomes a rebase-and-re-review "
                "(ateles#565).",
                priority=Priority.BLOCKER,
                handler=DAEMON_NAME,
            )
        except Exception as exc:  # a refusal must never crash the dispatch
            log.error(
                f"[{DAEMON_NAME}] {ref}: merge-refusal escalation failed: {exc}",
                exc_info=True,
            )
        return state

    async def _gate_merge_readiness(
        self,
        trigger: SwarmTrigger,
        parent: int | None,
        panel: list[Lens],
        ci_state: str | None = None,
    ) -> None:
        """File the merge checkpoint + notify ONLY when review is clear AND CI green.

        Previously this fired unconditionally after review. Now the operator's
        merge-ready email is a truthful signal: it means the PR is actually
        ready. When CI is not green, hold quietly (digest note) rather than
        paging the operator prematurely. A completed check re-invokes this via
        the ci_status path; a re-push re-runs the whole panel.

        `ci_state` may be passed by a caller that already computed it (the
        ci_status path) to avoid a redundant _required_ci_state fetch (3 GitHub
        API round-trips); when None we compute it here as before.
        """
        ref = f"{trigger.repository}#{trigger.number}"
        if self.config.auto_merge:
            return  # auto-merge path: Vanellus handles merge, no checkpoint.

        ci = ci_state if ci_state is not None else await self._required_ci_state(trigger)
        if ci == "failing":
            # Route a red CI back for a fix, bounded like review findings.
            await self._route_ci_failure(trigger, parent)
            return
        if ci != "green":
            # pending / unknown — hold without paging; don't claim ready.
            log.info(
                f"[{DAEMON_NAME}] {ref}: review clear but CI {ci} — holding "
                "merge-ready signal until required checks are green"
            )
            self.notifier.send(
                f"PR {ref}: review clear, required CI {ci} — will hold the "
                "merge-ready request until checks are green.",
                priority=Priority.INFO,
                handler=DAEMON_NAME,
            )
            return

        await self._store_merge_checkpoint(trigger, parent, [p.lens for p in panel])
        # Approval channels (approval loop): the operator can (a) reply APPROVE to
        # this email, (b) click Approve on the PR in GitHub, or (c) comment
        # /approve — all converge on the same gated merge. The correlation token
        # lets a plain email reply be matched back to THIS PR.
        self.notifier.send(
            f"PR {ref} is READY TO MERGE — panel review clear "
            f"({', '.join(p.lens for p in panel) or 'baseline only'}) and "
            "required CI green. Reply APPROVE, approve on GitHub, or comment "
            "/approve to merge (checkpoint_brief filed).\n\n"
            f"{format_approve_token(trigger.repository, trigger.number)}",
            priority=Priority.OPERATOR_DECISION,
            handler=DAEMON_NAME,
        )

    async def _required_ci_state(self, trigger: SwarmTrigger) -> str:
        """Return "green" | "failing" | "pending" | "unknown" for the PR head.

        Gates on the combined commit status + check-runs for the PR's head SHA.

        ateles#350: the `pre_merge` boundary's on_check_failure posture governs
        what a check-fetch failure DOES beyond returning "unknown". "unknown"
        never fabricates a green either way — the caller already treats
        non-"green" as "hold, don't page ready-to-merge" — but under
        posture=closed (pre_merge's default) a check-fetch failure
        additionally files a blocking checkpoint_brief + notifies, instead of
        silently retrying on the next webhook with no operator-visible trail.
        The OPEN/CLOSED decision itself is delegated to
        `evaluate_with_posture` (lib/daemon_runtime/checkpoint_posture.py) —
        the single shared helper — rather than re-derived here.
        """

        async def _fetch_state() -> str:
            async with httpx.AsyncClient(timeout=30) as client:
                headers = self._github_headers(trigger.repository)
                pr = await client.get(
                    f"https://api.github.com/repos/{trigger.repository}/pulls/"
                    f"{trigger.number}",
                    headers=headers,
                )
                pr.raise_for_status()
                head_sha = pr.json().get("head", {}).get("sha", "")
                if not head_sha:
                    return "unknown"

                # Combined legacy commit status (Loxia, external CIs).
                status = await client.get(
                    f"https://api.github.com/repos/{trigger.repository}/commits/"
                    f"{head_sha}/status",
                    headers=headers,
                )
                status.raise_for_status()
                state = status.json().get("state", "")  # success|failure|pending|""

                # GitHub Actions check-runs (not covered by the status API).
                checks = await client.get(
                    f"https://api.github.com/repos/{trigger.repository}/commits/"
                    f"{head_sha}/check-runs",
                    headers={**headers, "Accept": "application/vnd.github+json"},
                )
                checks.raise_for_status()
                runs = checks.json().get("check_runs", [])
                run_conclusions = [r.get("conclusion") for r in runs]
                run_statuses = [r.get("status") for r in runs]

                failing_run = any(
                    c in ("failure", "timed_out", "cancelled", "action_required")
                    for c in run_conclusions
                )
                pending_run = any(s != "completed" for s in run_statuses)

                if state == "failure" or failing_run:
                    return "failing"
                if state == "pending" or pending_run:
                    return "pending"
                if state in ("success", "") and not runs and not failing_run:
                    # No checks configured at all — treat as green (nothing to fail).
                    return "green"
                return "green"

        ref = f"{trigger.repository}#{trigger.number}"
        # load_policy() does a blocking httpx.get(); run it off the event loop
        # so one slow/hung Neotoma fetch can't stall every other concurrent
        # webhook dispatch this daemon is handling.
        policy = await asyncio.to_thread(load_policy)
        result = await evaluate_with_posture(
            _fetch_state,
            boundary="pre_merge",
            policy=policy,
            title=f"CI status check failed: {ref}",
            handler=DAEMON_NAME,
            notify=lambda msg, **kw: self.notifier.send(
                msg, priority=Priority.OPERATOR_DECISION, **kw
            ),
            file_brief=lambda boundary, error, decision: self._file_ci_check_failure_brief(
                trigger, boundary=boundary, error=error
            ),
        )
        if isinstance(result, PostureOutcome):
            # Either posture returns "unknown" to the caller — OPEN logged
            # and proceeded, CLOSED blocked and escalated; neither fabricates
            # a "green" that would page the operator to merge unverified code.
            return "unknown"
        return result

    async def _file_ci_check_failure_brief(
        self, trigger: SwarmTrigger, *, boundary: str, error: str
    ) -> str | None:
        """Escalation hook for `evaluate_with_posture`'s CLOSED path at the
        `pre_merge` boundary (ateles#350).

        This daemon's checkpoints are keyed by `owner/repo#n` (`subject_ref`),
        not a Neotoma `task` entity, so this overrides the default
        `write_checkpoint_brief` escalation rather than forcing a fabricated
        task_entity_id. Returns the checkpoint_brief's entity_id (best-effort;
        None if Neotoma is unreachable — the caller does not proceed either
        way, since `evaluate_with_posture` already decided CLOSED).
        """
        ref = f"{trigger.repository}#{trigger.number}"
        entities = [
            {
                "entity_type": "checkpoint_brief",
                "title": f"CI status check failed: {ref}",
                "checkpoint_kind": "pre_merge_check_failure",
                "blocking": True,
                "subject_ref": ref,
                "body": (
                    f"Fetching required-CI status for {trigger.html_url} failed "
                    f"({error}). posture=closed for boundary={boundary}, so the "
                    "swarm is holding this PR at 'unknown' CI state rather than "
                    "silently retrying with no operator-visible trail. Review "
                    f"the failure and reply {_APPROVE_CMD} once CI state is "
                    f"confirmed manually, or {_REJECT_CMD} <reason> to stop."
                ),
                "status": "open",
            }
        ]
        await self._store_entities(
            entities,
            idempotency_key=(
                f"pre-merge-check-failure-{trigger.repository}-{trigger.number}-"
                f"{trigger.delivery_id}-{content_digest(entities)}"
            ),
        )
        return None

    async def _route_ci_failure(
        self, trigger: SwarmTrigger, parent: int | None
    ) -> None:
        """Route a red-CI PR back to Cicada for a fix, bounded like review findings."""
        ref = f"{trigger.repository}#{trigger.number}"
        prior_rounds = await self._fix_round_count(trigger)
        if prior_rounds >= self.config.max_fix_rounds:
            self.notifier.send(
                f"PR {ref}: required CI is failing after "
                f"{self.config.max_fix_rounds} auto-fix rounds. Escalating — "
                "needs your attention. Merge held.",
                priority=Priority.OPERATOR_DECISION,
                handler=DAEMON_NAME,
            )
            return
        this_round = prior_rounds + 1
        await self._record_fix_round(trigger, this_round)
        head_before = await self._pr_head_sha(trigger)
        cicada_result = await run_skill(
            "cicada",
            self._cicada_ci_fix_prompt(trigger, parent, this_round),
            github_token=_token_for_agent_on_repo("cicada", trigger.repository),
            include_github_contract=True,
            notifier=self.notifier,
        )
        if detect_auth_failure(cicada_result.stdout, cicada_result.stderr):
            await self._handle_panel_auth_failure(trigger, "cicada")
            return
        if not cicada_result.ok:
            self.notifier.send(
                f"PR {ref}: CI-fix round {this_round} — Cicada could not resolve "
                "the failing checks. Needs your attention. Merge held.",
                priority=Priority.OPERATOR_DECISION,
                handler=DAEMON_NAME,
            )
            return
        if await self._escalate_noop_fix_round(
            trigger, "CI-fix", this_round, head_before
        ):
            return
        log.info(
            f"[{DAEMON_NAME}] {ref}: dispatched CI-fix round {this_round} to "
            "Cicada; awaiting its push to re-review"
        )

    @staticmethod
    def _fix_guidance_prompt(
        trigger: SwarmTrigger, lens: str, agent: str, findings: str
    ) -> str:
        t = trigger
        return (
            f"Invoke the {agent} review agent per your appended system prompt.\n\n"
            f"The PR review panel returned BLOCKING findings under YOUR ({lens}) "
            f"lens on PR {t.repository}#{t.number}: {t.title}\n{t.html_url}\n\n"
            "Produce concrete, actionable fix GUIDANCE for the implementer "
            "(Cicada) — what to change and why, in your domain voice. Do NOT "
            "write the code yourself; you are the reviewer, Cicada implements. "
            "Be specific: name files, functions, and the exact change per "
            "finding. If a finding is actually a false positive, say so and why "
            "so Cicada can note it rather than force a change.\n\n"
            "----- BLOCKING FINDINGS (your lens) -----\n"
            f"{findings}\n"
            "----- END FINDINGS -----\n\n"
            f"{_agent_prompt_instruction(agent, lens + ' reviewer')}"
        )

    @staticmethod
    def _cicada_fix_prompt(
        trigger: SwarmTrigger, parent: int | None, round_n: int, guidance: str
    ) -> str:
        t = trigger
        return (
            "Invoke the cicada agent per your appended system prompt.\n\n"
            f"PR {t.repository}#{t.number} ({t.title}) got REQUEST_CHANGES from "
            f"the review panel (auto-fix round {round_n}). Address the review "
            "guidance below on the PR's existing branch, then push. Your push "
            "re-runs the panel automatically — do NOT open a new PR and do NOT "
            "merge.\n\n"
            f"Parent issue: #{parent if parent else 'unknown'}.\n"
            f"Check out and work on the PR branch for {t.repository}#{t.number} "
            f"({t.html_url}); commit with the ateles-agent identity and push to "
            "update this PR.\n\n"
            "Apply the per-lens guidance; where a reviewer flagged a false "
            "positive, note it in your commit/PR comment instead of forcing a "
            "change. Run the tests and self-review before pushing.\n\n"
            "----- CONSOLIDATED REVIEW GUIDANCE -----\n"
            f"{guidance}\n"
            "----- END GUIDANCE -----\n\n"
            f"{_agent_prompt_instruction('cicada', 'issue worker')}\n\n"
            "AUTONOMY GUARDRAIL — DO NOT MERGE. Merge stays operator-gated."
        )

    @staticmethod
    def _cicada_ci_fix_prompt(
        trigger: SwarmTrigger, parent: int | None, round_n: int
    ) -> str:
        t = trigger
        return (
            "Invoke the cicada agent per your appended system prompt.\n\n"
            f"PR {t.repository}#{t.number} ({t.title}) passed panel review but "
            f"required CI is FAILING (auto-fix round {round_n}). Check out the "
            "PR branch, read the failing checks "
            f"(`gh pr checks {t.number} --repo {t.repository}` and the run logs), "
            "fix the cause, run the tests locally, then push to update this PR. "
            "Your push re-runs the panel + CI automatically — do NOT open a new "
            "PR and do NOT merge.\n\n"
            f"Parent issue: #{parent if parent else 'unknown'}. {t.html_url}\n\n"
            f"{_agent_prompt_instruction('cicada', 'issue worker')}\n\n"
            "AUTONOMY GUARDRAIL — DO NOT MERGE. Merge stays operator-gated."
        )

    # ── issue_comment pipeline (ateles#112) ─────────────────────────────────

    async def _handle_issue_comment(self, trigger: SwarmTrigger) -> None:
        """Handle an issue_comment webhook event.

        Reacts to operator commands (only _OPERATOR_LOGIN may invoke any of them):
          /confirm-gates-clear  — waive unsigned pre-impl gates + re-trigger PR pipeline.
          /swarm-run            — re-run the full issue pipeline (Lanius triage +
                                  expectation pre-registration + Pavo scoping) on the
                                  existing issue.  Useful for iteration testing after a
                                  design increment without closing/re-opening the issue.
          /approve              — (Phase H1) approve the pending pre-merge checkpoint:
                                  resolve the checkpoint_brief, post a confirmation,
                                  remove the operator from PR reviewers, hand back to
                                  Vanellus.  Does NOT auto-merge (conservative; operator
                                  may then merge directly on GitHub).
          /reject <reason>      — (Phase H1) reject the pending checkpoint; record the
                                  reason (text after "/reject "); resolve as rejected;
                                  remove operator from reviewers.  Does NOT proceed.
          /hold                 — (Phase H1) park: ack only, leave checkpoint blocking
                                  and operator still requested as reviewer.

        Priority: /confirm-gates-clear wins if present alongside any other command
        (gate clearance is the most time-sensitive action).  Among H1 commands:
        /approve > /reject > /hold (first match dispatched).

        All other comments are silently ignored — this is the best-effort,
        never-crash path.

        Security guardrail: only _OPERATOR_LOGIN may invoke any command.  Any
        other commenter — including swarm agents — is silently ignored.
        """
        comment_author = trigger.comment_author
        comment_body = (trigger.comment_body or "").strip()
        ref = f"{trigger.repository}#{trigger.number}"

        # Guard 0: bot/machine-account self-trigger prevention (neotoma#1686).
        # A swarm confirmation comment containing the command token would re-fire
        # the handler via its own issue_comment webhook.  Return immediately for
        # ANY known bot identity — before even checking for command tokens — so
        # no swarm-authored comment can ever reach the dispatch path.  This is
        # stronger than the operator-login positive check (Guard 2) and must
        # come first so it cannot be bypassed by a comment_author that happens to
        # match the operator login (defence-in-depth).
        if _is_bot_author(comment_author):
            log.debug(
                f"[{DAEMON_NAME}] issue_comment on {ref} from bot/machine "
                f"account {comment_author!r} — ignored (self-trigger prevention)"
            )
            return

        has_gates_clear = _CONFIRM_GATES_CLEAR_CMD in comment_body
        has_swarm_run = _SWARM_RUN_CMD in comment_body
        # Phase H1 commands — check with word-boundary awareness: "/approve" must
        # not match "/approve-something-else".  Simple startswith/split check:
        # a command is present when the token appears as a standalone word (i.e.
        # followed by whitespace, end-of-string, or a space-delimited argument).
        has_approve = bool(
            re.search(r"(?:^|\s)/approve(?:\s|$)", comment_body)
        )
        has_reject = bool(
            re.search(r"(?:^|\s)/reject(?:\s|$)", comment_body)
        )
        has_hold = bool(
            re.search(r"(?:^|\s)/hold(?:\s|$)", comment_body)
        )

        # Guard 1: only react when a known command is present.
        if not any([has_gates_clear, has_swarm_run, has_approve, has_reject, has_hold]):
            log.debug(
                f"[{DAEMON_NAME}] issue_comment on {ref} has no recognised "
                f"command — ignored"
            )
            return

        # Guard 2: operator-only guardrail (applies to all commands).
        if comment_author.lower() != _OPERATOR_LOGIN.lower():
            # Pick whichever command was detected for the log message.
            cmd = (
                _CONFIRM_GATES_CLEAR_CMD if has_gates_clear
                else _SWARM_RUN_CMD if has_swarm_run
                else _APPROVE_CMD if has_approve
                else _REJECT_CMD if has_reject
                else _HOLD_CMD
            )
            log.warning(
                f"[{DAEMON_NAME}] {cmd} from non-operator {comment_author!r} "
                f"on {ref} — ignored (operator login: {_OPERATOR_LOGIN!r})"
            )
            return

        # Dispatch: /confirm-gates-clear wins over everything when present.
        # Among H1 commands: /approve > /reject > /hold.
        if has_gates_clear:
            await self._handle_confirm_gates_clear(trigger)
        elif has_swarm_run:
            await self._handle_swarm_run(trigger)
        elif has_approve:
            await self._handle_approve(trigger)
        elif has_reject:
            await self._handle_reject(trigger)
        else:
            await self._handle_hold(trigger)

    async def _handle_confirm_gates_clear(self, trigger: SwarmTrigger) -> None:
        """Execute the /confirm-gates-clear operator command.

        Waives all unsigned pre-impl gates on the issue and re-triggers the PR
        pipeline if the comment is on a PR.  Internal helper called from
        _handle_issue_comment after all guards pass.
        """
        ref = f"{trigger.repository}#{trigger.number}"

        log.info(
            f"[{DAEMON_NAME}] operator {_CONFIRM_GATES_CLEAR_CMD} received "
            f"on {ref} (comment #{trigger.comment_id})"
        )
        self.notifier.send(
            f"Operator cleared gates on {ref} via {_CONFIRM_GATES_CLEAR_CMD} — "
            "waiving unsigned pre-impl gates and re-triggering PR pipeline",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )

        # Perform the waive DISPATCHER-SIDE (ateles#285). This is a mechanical
        # state transition with no judgement in it, so it does not go through
        # an agent turn: the dispatcher corrects each unsigned pre-impl gate to
        # "waived", appends to owner_history, RE-READS to verify the write
        # landed, and posts an operator-visible comment either way.
        await self._waive_gates(trigger)

        # If the comment is on a PR, re-run the PR pipeline immediately.
        # If it's on the parent issue, the operator needs to re-push or
        # re-open the PR to re-trigger (we log a note).
        if trigger.comment_on_pr:
            # Build a minimal PR-shaped trigger from the issue_comment data
            # so _handle_pr can be called directly.
            pr_trigger = SwarmTrigger(
                kind="pr_opened",
                repository=trigger.repository,
                number=trigger.number,
                title=trigger.title,
                body=trigger.body,
                author=trigger.author,
                html_url=trigger.html_url,
                delivery_id=trigger.delivery_id,
                action="reopened",
                labels=trigger.labels,
                raw=trigger.raw,
            )
            log.info(
                f"[{DAEMON_NAME}] re-triggering PR pipeline for {ref} "
                "after gate waive"
            )
            await self._handle_pr(pr_trigger)
        else:
            log.info(
                f"[{DAEMON_NAME}] gates waived on issue {ref}; "
                "PR pipeline will re-run on next push or re-open"
            )
            self.notifier.send(
                f"Gates waived on issue {ref}. Re-push or re-open the PR to "
                "trigger the review panel.",
                priority=Priority.INFO,
                handler=DAEMON_NAME,
            )

    async def _handle_swarm_run(self, trigger: SwarmTrigger) -> None:
        """Execute the /swarm-run operator command.

        Re-runs the full issue pipeline (Lanius triage + expectation
        pre-registration + Pavo scoping) for the issue referenced by this
        issue_comment trigger.

        The issue_comment SwarmTrigger is already populated with the issue's
        title, body, and labels by github_gateway.parse_github_event (from the
        `issue` object in the comment event payload) — no additional gh fetch
        is needed in the normal case.  If any of those fields are empty (e.g.
        a legacy delivery with a sparse payload) we fetch them from the GitHub
        API before calling _handle_issue_opened.

        Best-effort: posts a confirming comment before starting, then calls
        _handle_issue_opened.  Never raises.
        """
        ref = f"{trigger.repository}#{trigger.number}"

        log.info(
            f"[{DAEMON_NAME}] operator {_SWARM_RUN_CMD} received on {ref} "
            f"(comment #{trigger.comment_id})"
        )
        self.notifier.send(
            f"Operator /swarm-run on {ref} — re-running the issue pipeline",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )

        # Post a confirming comment so the re-run is visible on GitHub.
        # Best-effort: failures here must not block the pipeline.
        try:
            await self._post_swarm_run_comment(trigger)
        except Exception as exc:
            log.warning(
                f"[{DAEMON_NAME}] failed to post /swarm-run confirmation "
                f"comment on {ref}: {exc}"
            )

        # Build an issue-shaped trigger from the comment event fields.
        # parse_github_event populates title/body/labels from the issue object
        # in the issue_comment payload, so these are available on trigger
        # without a separate API call.  If they're empty (sparse payload),
        # fetch them.
        issue_title = trigger.title
        issue_body = trigger.body
        issue_labels = trigger.labels

        if not issue_title:
            fetched = await self._fetch_issue_fields(
                trigger.repository, trigger.number
            )
            if fetched:
                issue_title = fetched.get("title", "")
                issue_body = fetched.get("body", "")
                issue_labels = [
                    lbl.get("name", "") for lbl in fetched.get("labels", [])
                ]

        issue_trigger = SwarmTrigger(
            kind="issue_opened",
            repository=trigger.repository,
            number=trigger.number,
            title=issue_title,
            body=issue_body,
            author=trigger.author,
            html_url=trigger.html_url,
            delivery_id=trigger.delivery_id,
            action="reopened",
            labels=issue_labels,
            raw=trigger.raw,
        )

        log.info(
            f"[{DAEMON_NAME}] re-running issue pipeline for {ref} "
            f"via {_SWARM_RUN_CMD}"
        )
        await self._handle_issue_opened(issue_trigger)

    # ── Phase H1: /approve /reject /hold — pre-merge checkpoint verdicts ──────

    async def _handle_pr_review(self, trigger: SwarmTrigger) -> None:
        """Handle a GitHub pull_request_review "submitted" event (approval loop).

        The operator clicking "Approve" in the PR review UI is a second approval
        entry point, equivalent to the /approve issue comment. Guards, in order:

          - Bot/machine reviewers are ignored (defence-in-depth; a swarm agent
            review must never drive a merge).
          - Only the operator login may approve-to-merge (same guard as the
            comment commands).
          - Only state == "approved" acts. "changes_requested" / "commented" /
            "dismissed" are acknowledged in the log and ignored — they are not
            an approval.

        A qualifying approval routes to the SHARED _approve_and_maybe_merge path,
        so the merge boundary (APIS_APPROVAL_TRIGGERS_MERGE) is honoured
        identically to the other entry points.
        """
        ref = f"{trigger.repository}#{trigger.number}"
        reviewer = trigger.review_author or ""
        state = trigger.review_state or ""

        if _is_bot_author(reviewer):
            log.debug(
                f"[{DAEMON_NAME}] pr_review on {ref} from bot/machine "
                f"{reviewer!r} — ignored (self-trigger prevention)"
            )
            return
        if reviewer.lower() != _OPERATOR_LOGIN.lower():
            log.debug(
                f"[{DAEMON_NAME}] pr_review on {ref} from non-operator "
                f"{reviewer!r} — ignored (operator login: {_OPERATOR_LOGIN!r})"
            )
            return
        if state != "approved":
            log.info(
                f"[{DAEMON_NAME}] pr_review on {ref} by operator is "
                f"{state!r}, not 'approved' — no merge action"
            )
            return

        log.info(
            f"[{DAEMON_NAME}] operator GitHub review APPROVED on {ref} — "
            "routing to shared approval path"
        )
        await self._approve_and_maybe_merge(trigger, source="GitHub review")

    async def _handle_email_approve(self, trigger: SwarmTrigger) -> None:
        """Handle an operator email-reply approval (approval loop).

        The Apis gateway's /approve-email route already authenticated the caller
        (shared secret) and Turdus already verified the reply came from the
        operator's own address and carried APPROVE + the correlation token. The
        trigger arrives pre-attributed to the operator, so we route straight to
        the shared approval path — the merge gate (APIS_APPROVAL_TRIGGERS_MERGE)
        applies identically to every channel.
        """
        ref = f"{trigger.repository}#{trigger.number}"
        log.info(
            f"[{DAEMON_NAME}] operator email APPROVE on {ref} — "
            "routing to shared approval path"
        )
        await self._approve_and_maybe_merge(trigger, source="email reply")

    async def _handle_ci_status(self, trigger: SwarmTrigger) -> None:
        """React to a completed CI rollup (check_suite:completed) for a PR head.

        Closes the last loop-closure gap (ateles#197): previously only a re-push
        re-ran the pipeline, so a PR that was review-clear but CI-pending held
        silently even after CI later went green, and a post-review CI failure was
        invisible until a human looked. Now:

          - required CI failed → route back to Cicada for a fix (bounded, shared
            with the push path via _route_ci_failure);
          - required CI green AND the panel verdict is already clear → run the
            readiness gate so the operator gets the (now-truthful) merge-ready
            signal.

        Guards: resolve the PR from the check_suite's associated PRs; SKIP if the
        suite's head_sha is not the PR's CURRENT head (a stale check for a
        superseded commit must not drive anything); do nothing unless review is
        already clear (CI-green on an unreviewed PR is not merge-ready — the
        normal panel path owns that). Fully best-effort; never raises.

        The stale-head skip is silent by design, not a hold: GitHub creates a
        distinct check_suite object per (head_sha, CI app), so a stale event
        for a superseded commit never suppresses or replaces the check_suite
        for the PR's current head — that head gets its own independent
        check_suite:completed event once its checks finish. A stale skip can
        therefore never be the terminal CI event for a PR; the current head's
        own completion re-drives this handler. Notifying on every stale skip
        would page the operator on ordinary out-of-order delivery, which is
        ateles#197 with `ci` swapped for `status`.
        """
        # Auto-release retry: a CI rollup completing on the release repo's
        # default branch is how a push_main that deferred on in-progress/red CI
        # gets a second chance. The merge webhook fires before that merge's CI
        # finishes, so _handle_push_main often hits `ci is None` and defers
        # WITHOUT stamping the SHA lock; when the same commit's CI later goes
        # green this event re-invokes prepare, which now sees green and prepares.
        # A default-branch suite carries no associated PR, so this must run
        # before the no-PR early return below.
        if (
            trigger.repository == PHOENICURUS_RELEASE_REPO
            and trigger.ci_head_branch == PHOENICURUS_RELEASE_BRANCH
        ):
            if trigger.ci_conclusion == "success":
                log.info(
                    f"[{DAEMON_NAME}] {PHOENICURUS_RELEASE_BRANCH} CI succeeded on "
                    f"{trigger.repository} ({trigger.ci_head_sha[:9]}) — retrying "
                    "release prep"
                )
                push_like = SwarmTrigger(
                    kind="push_main",
                    repository=trigger.repository,
                    number=0,
                    title="",
                    body="",
                    author="",
                    html_url="",
                    delivery_id=trigger.delivery_id,
                    action="push",
                    push_ref=f"refs/heads/{PHOENICURUS_RELEASE_BRANCH}",
                    push_after=trigger.ci_head_sha,
                )
                await self._handle_push_main(push_like)
            else:
                log.info(
                    f"[{DAEMON_NAME}] {PHOENICURUS_RELEASE_BRANCH} CI on "
                    f"{trigger.repository} concluded {trigger.ci_conclusion!r} — "
                    "no release retry"
                )
            return

        pr_number = next((n for n in trigger.ci_pr_numbers if n), 0) or trigger.number
        if not pr_number:
            log.debug(
                f"[{DAEMON_NAME}] ci_status on {trigger.repository} carried no "
                "associated PR — ignored"
            )
            return
        ref = f"{trigger.repository}#{pr_number}"

        pr = await self._fetch_pr(trigger.repository, pr_number)
        if not pr:
            # Fetch failed; fail-open (a later event or push re-drives) but the
            # operator must see the loop-closure event was dropped rather than
            # silently vanishing (ateles#197's core failure mode).
            self.notifier.send(
                f"CI-completion event for {ref}: could not fetch PR — the "
                "read failed. Loop-closure may be delayed; a later event or "
                "push will retry.",
                priority=Priority.INFO,
                handler=DAEMON_NAME,
            )
            return
        if pr.get("state") != "open" or pr.get("draft"):
            return
        current_head = (pr.get("head") or {}).get("sha", "")
        if trigger.ci_head_sha and current_head and trigger.ci_head_sha != current_head:
            log.info(
                f"[{DAEMON_NAME}] {ref}: check_suite head "
                f"{trigger.ci_head_sha[:9]} != PR head {current_head[:9]} — "
                "stale CI event, ignored"
            )
            return

        # Build a PR-shaped trigger the readiness/route helpers expect.
        pr_trigger = self._pr_trigger_from_api(trigger.repository, pr)
        parent = self._parent_issue_number(pr_trigger.body)

        # Terminal CI conclusion → failing? Route back (bounded), regardless of
        # review state: a broken build must be fixed to merge.
        ci = await self._required_ci_state(pr_trigger)
        if ci == "failing":
            log.info(f"[{DAEMON_NAME}] {ref}: CI completed failing — routing to fix")
            await self._route_ci_failure(pr_trigger, parent)
            return
        if ci != "green":
            return  # pending/unknown — a later check_suite:completed will re-fire

        # CI green: only advance the merge-ready signal if review is ALREADY
        # clear. An unreviewed PR going green is the panel path's job, not ours.
        if not await self._pr_review_is_clear(trigger.repository, pr_number):
            log.info(
                f"[{DAEMON_NAME}] {ref}: CI green but no clear panel verdict yet "
                "— leaving to the review path"
            )
            return
        log.info(f"[{DAEMON_NAME}] {ref}: CI green + review clear — gating readiness")
        # Pass the CI state we already computed so the gate does not re-fetch it.
        await self._gate_merge_readiness(pr_trigger, parent, panel=[], ci_state=ci)

    async def _resolve_review_verdict(
        self, t: SwarmTrigger, stdout: str
    ) -> tuple[str | None, bool]:
        """Resolve the panel verdict: stdout first, the PR comment as fallback.

        ateles#292: `parse_review_verdict` reads Vanellus's stdout, but Vanellus
        posts its aggregated verdict to GitHub without reliably repeating it
        inline (observed: 3 of 4 production emissions parsed as None). A missed
        token downgrades a real REQUEST_CHANGES to the inert COMMENT on the
        native review — precisely the state ateles#241 exists to record. So when
        stdout carries no token, re-read the durable artifact: the Vanellus
        aggregation comment already on the PR, marked with
        ``_VANELLUS_COMMENT_MARKER``.

        Returns ``(verdict, used_fallback)``. ``verdict`` is None when neither
        source carries a valid token — the caller still treats None as not-clear
        (``review_verdict_is_clear(None)`` is False), so behaviour on total
        failure is unchanged from before this fallback existed.

        Best-effort — NEVER raises. A GitHub hiccup falls through to
        ``(None, False)`` rather than breaking ``_handle_pr``.

        Ordering note: this runs after ``_post_missing_vanellus_comment``, which
        reposts Vanellus's stdout when no aggregation comment landed. So the
        comment section has already settled — this is not a race against
        comment-posting latency.
        """
        verdict = parse_review_verdict(stdout)
        if verdict is not None:
            return verdict, False

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # ateles#430: page the full list, then select the NEWEST
                # aggregation client-side. GitHub ignores sort/direction here,
                # so position in the response says nothing about recency.
                comments = await self._all_issue_comments(
                    t.repository, t.number, client
                )
                candidates = [
                    c
                    for c in comments
                    if _VANELLUS_COMMENT_MARKER in (c.get("body") or "")
                ]
                comment = latest_aggregation_comment(comments)
                if comment is not None:
                    body = comment.get("body") or ""
                    fallback_verdict = parse_review_verdict(body)
                    if fallback_verdict is None:
                        # Marker present but no token — a prose-only aggregation.
                        # Do not loosen the regex; report no verdict.
                        log.warning(
                            f"[{DAEMON_NAME}] {t.repository}#{t.number}: Vanellus "
                            "aggregation comment carries no verdict token either "
                            "— no verdict recovered"
                        )
                        return None, False
                    log.info(
                        f"[{DAEMON_NAME}] {t.repository}#{t.number}: stdout had no "
                        f"verdict token — recovered {fallback_verdict!r} from the "
                        "Vanellus aggregation comment (fallback fired) "
                        f"[comment id={comment.get('id')} "
                        f"created_at={comment.get('created_at')} "
                        f"of {len(candidates)} candidate(s)]"
                    )
                    return fallback_verdict, True
                log.warning(
                    f"[{DAEMON_NAME}] {t.repository}#{t.number}: no Vanellus "
                    "aggregation comment found — no verdict recovered"
                )
        except Exception as exc:
            log.warning(
                f"[{DAEMON_NAME}] {t.repository}#{t.number}: comment fallback read "
                f"failed ({exc}) — falling back to no-verdict"
            )
        return None, False

    async def _pr_review_is_clear(self, repository: str, pr_number: int) -> bool:
        """True when the latest Vanellus aggregation on the PR is a clear verdict.

        Pages the PR's full comment list and selects the newest aggregation
        CLIENT-SIDE by ``created_at`` (ateles#430). The previous implementation
        passed ``sort=created&direction=desc`` and returned the first marker
        match, believing that was newest-first. GitHub ignores both parameters
        on this endpoint, so it read the OLDEST aggregation.

        That failed UNSAFE on the one path where it matters most: an early
        ``APPROVE`` superseded by a later ``REQUEST_CHANGES`` read as clear, and
        the PR would merge on an approval the panel had already withdrawn.

        Still fail-closed (False) on any error, and now also False when the page
        cap is hit without finding a marker — never claim merge-ready off a read
        that may have missed the verdict.

        Reads the aggregation through `review_blocks_merge`, the same predicate
        the panel path uses, so this gate cannot drift from it. It previously
        tested the parsed TOKEN alone while holding the body that carried the
        findings: a `**COMMENT**` aggregation listing a `[BLOCKING]` item read as
        clear here, and since this is the CI-green path, a PR could reach
        merge-ready on an aggregation that named its own blocker.
        """
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                comments = await self._all_issue_comments(
                    repository, pr_number, client
                )
                comment = latest_aggregation_comment(comments)
                if comment is None:
                    return False
                body = comment.get("body") or ""
                return not review_blocks_merge(parse_review_verdict(body), body)
        except Exception as exc:
            log.warning(
                f"[{DAEMON_NAME}] {repository}#{pr_number}: review-verdict read "
                f"failed ({exc}) — treating as not-clear"
            )
            return False

    async def check_workflow_owner_drift(self) -> list[tuple[str, str, str]]:
        """Warn if any workflow names a gate owner the swarm cannot dispatch.

        ateles#441. Run at startup, because the drift it detects is otherwise
        invisible until a gate silently fails to sign — which took four days to
        notice on #416, and two months to notice at all after the 2026-06-12
        renames.

        Fail-open and best-effort: a check that cannot read Neotoma must never
        stop the daemon booting. Returns the drift rows (empty when clean) so
        callers and tests assert on the result rather than on log output.
        """
        try:
            store = IssueSpecStore(
                self.config.neotoma_base_url, self.config.neotoma_token
            )
            data = await store._post(
                "entities/query",
                {
                    "entity_type": "workflow_definition",
                    "limit": 200,
                    "include_snapshots": True,
                },
            )
            if not data:
                log.warning(
                    f"[{DAEMON_NAME}] workflow-owner drift check: could not read "
                    "workflow_definition entities — skipped"
                )
                return []
            workflows = data.get("entities", []) or []
            drift = workflow_owner_drift(workflows, dispatchable_agents())
            if drift:
                detail = ", ".join(
                    f"{wf} gate '{gate}' -> '{owner}'" for wf, gate, owner in drift
                )
                # ERROR, not WARNING: every issue routed through an affected
                # workflow stalls at that gate showing only `pending`, and the
                # auto-build handoff stays blocked for as long as it does.
                log.error(
                    f"[{DAEMON_NAME}] WORKFLOW OWNER DRIFT — {len(drift)} gate(s) "
                    f"name an agent that cannot be dispatched: {detail}. Issues "
                    "on these workflows will stall at those gates with `pending` "
                    "and no error."
                )
                self.notifier.send(
                    f"Workflow gate owner drift ({len(drift)}): {detail}. Gates "
                    "naming these agents can never sign — correct the "
                    "workflow_definition entities.",
                    priority=Priority.BLOCKER,
                    handler=DAEMON_NAME,
                )
            else:
                log.info(
                    f"[{DAEMON_NAME}] workflow-owner drift check: "
                    f"{len(workflows)} workflow(s) clean"
                )
            return drift
        except Exception as exc:
            log.warning(
                f"[{DAEMON_NAME}] workflow-owner drift check failed ({exc}) — "
                "continuing; this check must never block startup"
            )
            return []
    async def _all_issue_comments(
        self, repository: str, number: int, client: httpx.AsyncClient
    ) -> list[dict]:
        """Every comment on an issue/PR, following pagination to the last page.

        ateles#430. Paging is not optional here: the newest aggregation is what
        both callers need, and on a PR with >100 comments it sits on a later
        page. Selecting the max over a truncated first page cannot find a
        comment that was never returned — which is exactly the >100-comment case
        ``_pr_review_is_clear``'s old docstring claimed ``direction=desc``
        protected it from, using a parameter GitHub ignores.

        Raises on HTTP error so each caller keeps its own established failure
        posture (the fallback path degrades to "no verdict"; the merge gate
        fails closed).
        """
        url = (
            f"https://api.github.com/repos/{repository}/issues/{number}/comments"
        )
        out: list[dict] = []
        page = 1
        while page <= _MAX_COMMENT_PAGES:
            resp = await client.get(
                url,
                params={"per_page": 100, "page": page},
                headers=self._github_headers(repository),
            )
            resp.raise_for_status()
            batch = resp.json()
            if not batch:
                break
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        else:
            # Ran the page cap without a short page. Log rather than silently
            # truncating: a bounded scan that hides its own bound is how the
            # >100-comment gap stayed invisible in the first place.
            log.warning(
                f"[{DAEMON_NAME}] {repository}#{number}: comment scan hit the "
                f"{_MAX_COMMENT_PAGES}-page cap ({len(out)} comments) — the "
                "newest aggregation may lie beyond it"
            )
        return out

    async def _fetch_pr(self, repository: str, pr_number: int) -> dict | None:
        """Fetch a PR object from the GitHub API; None on error (fail-open)."""
        url = f"https://api.github.com/repos/{repository}/pulls/{pr_number}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    url, headers=self._github_headers(repository)
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            log.warning(
                f"[{DAEMON_NAME}] could not fetch PR {repository}#{pr_number}: "
                f"{exc}"
            )
            return None

    @staticmethod
    def _pr_trigger_from_api(repository: str, pr: dict) -> SwarmTrigger:
        """Build a pr-shaped SwarmTrigger from a GitHub PR API object.

        Used by the CI-status path so it can reuse the readiness/route helpers,
        which expect a trigger carrying number/title/body/html_url/head_ref.
        """
        return SwarmTrigger(
            kind="pr_synchronize",
            repository=repository,
            number=pr.get("number", 0),
            title=pr.get("title", ""),
            body=pr.get("body") or "",
            author=(pr.get("user") or {}).get("login", ""),
            html_url=pr.get("html_url", ""),
            delivery_id="",
            action="synchronize",
            head_ref=(pr.get("head") or {}).get("ref", ""),
            base_ref=(pr.get("base") or {}).get("ref", ""),
        )

    async def _handle_push_main(self, trigger: SwarmTrigger) -> None:
        """React to a merge landing on main by asking the release daemon to prepare.

        This is the auto-release trigger: instead of waiting for the Mon-Thu
        07:00 scheduled sweep, every merge to main immediately offers Phoenicurus
        a chance to cut a release candidate. The approval gate is UNCHANGED - this
        only removes the schedule lag before the operator is asked.

        We deliberately do NOT re-implement any release policy here. ``prepare.py
        --on-merge`` re-applies every gate it already owns (unreleased-commit
        count vs PHOENICURUS_MIN_COMMITS, main CI green, no release already in
        flight) and rate-limits per main commit, so a burst of merges cannot
        stack up release candidates. This handler's only job is to invoke it and
        stay quiet when there is nothing to do.

        Only the Neotoma repo has a publishable release pipeline; a push to any
        other repo is ignored. Fully best-effort; never raises.
        """
        if trigger.repository != PHOENICURUS_RELEASE_REPO:
            log.debug(
                f"[{DAEMON_NAME}] push to {trigger.repository} is not the release "
                "repo - ignoring"
            )
            return

        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "phoenicurus-release",
            "prepare.py",
        )
        if not os.path.exists(script):
            log.warning(f"[{DAEMON_NAME}] release prepare script missing: {script}")
            return

        sha = (trigger.push_after or "")[:9]
        log.info(
            f"[{DAEMON_NAME}] main advanced to {sha} on {trigger.repository} - "
            "invoking release prepare (--on-merge)"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                script,
                "--on-merge",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            # prepare.py's phase 1 is a fast preflight; phase 2 spawns a detached
            # headless agent and returns. A generous cap keeps a wedged git/gh
            # call from pinning the dispatcher forever.
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=PHOENICURUS_PREPARE_TIMEOUT_S
            )
            tail = (out or b"").decode(errors="replace").strip().splitlines()
            log.info(
                f"[{DAEMON_NAME}] release prepare exited rc={proc.returncode}"
                + (f": {tail[-1]}" if tail else "")
            )
        except asyncio.TimeoutError:
            log.error(
                f"[{DAEMON_NAME}] release prepare timed out after "
                f"{PHOENICURUS_PREPARE_TIMEOUT_S}s for {sha}"
            )
        except Exception as exc:  # noqa: BLE001 - best-effort
            log.error(f"[{DAEMON_NAME}] release prepare failed for {sha}: {exc}")

    async def _handle_release_approve(self, trigger: SwarmTrigger) -> None:
        """Publish a release the operator approved by email reply.

        Turdus verified the reply came from the operator's address and carried
        `approve <exact version>` + the release-approve token, then POSTed to the
        Apis /approve-release route (shared-secret gated). This runs the SAME
        irreversible publish path as a Telegram approve: `publish.py --version
        <v> --from-email-approval`, which itself flips the release_result
        pending_approval -> approved (refusing any other starting state, so a
        duplicate/stale reply can't re-publish) and then ships it.

        This is the ONE handler that drives an irreversible publish, so it is
        deliberately strict and loud: it never runs publish for a malformed
        version, it bounds the subprocess, and it Telegrams the outcome (success
        or failure) so the operator always learns what their reply did.
        """
        version = (trigger.release_version or "").strip()
        if not re.match(r"^v[0-9][0-9A-Za-z.\-+]*$", version):
            log.error(
                f"[{DAEMON_NAME}] release_approve with invalid version "
                f"{version!r} — refusing to publish"
            )
            return

        script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "phoenicurus-release",
            "publish.py",
        )
        if not os.path.exists(script):
            log.error(f"[{DAEMON_NAME}] publish script missing: {script}")
            self.notifier.send(
                f"🔴 Release {version} email-approved but publish.py is missing "
                f"at {script} — publish did NOT run.",
                priority=Priority.BLOCKER,
                handler=DAEMON_NAME,
            )
            return

        log.info(
            f"[{DAEMON_NAME}] operator email-approved release {version} — "
            "invoking publish.py --from-email-approval"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable,
                script,
                "--version",
                version,
                "--from-email-approval",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            out, _ = await asyncio.wait_for(
                proc.communicate(), timeout=PHOENICURUS_PUBLISH_TIMEOUT_S
            )
            tail = (out or b"").decode(errors="replace").strip().splitlines()
            last = tail[-1] if tail else ""
            if proc.returncode == 0:
                log.info(f"[{DAEMON_NAME}] publish {version} succeeded: {last}")
                # publish.py sends its own rich success Telegram; keep this short.
            else:
                log.error(
                    f"[{DAEMON_NAME}] publish {version} FAILED rc="
                    f"{proc.returncode}: {last}"
                )
                self.notifier.send(
                    f"🔴 Release {version} email-approved but publish FAILED "
                    f"(rc={proc.returncode}): {last[:300]}. See "
                    "phoenicurus-release.log.",
                    priority=Priority.BLOCKER,
                    handler=DAEMON_NAME,
                )
        except asyncio.TimeoutError:
            log.error(
                f"[{DAEMON_NAME}] publish {version} timed out after "
                f"{PHOENICURUS_PUBLISH_TIMEOUT_S}s"
            )
            self.notifier.send(
                f"🔴 Release {version} publish TIMED OUT after "
                f"{PHOENICURUS_PUBLISH_TIMEOUT_S}s — it may be partially done; "
                "check phoenicurus-release.log and the registry before retrying.",
                priority=Priority.BLOCKER,
                handler=DAEMON_NAME,
            )
        except Exception as exc:  # noqa: BLE001
            log.error(f"[{DAEMON_NAME}] publish {version} error: {exc}")
            self.notifier.send(
                f"🔴 Release {version} email-approved but publish errored: "
                f"{exc}. Publish may not have run.",
                priority=Priority.BLOCKER,
                handler=DAEMON_NAME,
            )

    async def _handle_approve(self, trigger: SwarmTrigger) -> None:
        """Execute the /approve operator command (Phase H1 HITL checkpoint).

        Thin entry point: the /approve issue-comment command shares one approval
        path with the GitHub PR-review "approved" event and the email-reply
        APPROVE path. All three converge on ``_approve_and_maybe_merge`` so the
        checkpoint resolution, confirmation, reviewer cleanup, and (optional)
        merge stay identical regardless of HOW the operator approved.
        """
        log.info(
            f"[{DAEMON_NAME}] operator {_APPROVE_CMD} received on "
            f"{trigger.repository}#{trigger.number} (comment #{trigger.comment_id})"
        )
        # NB: the source label is echoed into the GitHub confirmation comment, so
        # it must NOT contain a command token (e.g. "/approve") — a comment body
        # carrying the token would re-trigger this handler (neotoma#1686).
        await self._approve_and_maybe_merge(trigger, source="approve comment")

    async def _approve_and_maybe_merge(
        self, trigger: SwarmTrigger, source: str
    ) -> None:
        """Shared operator-approval handler for all three approval entry points.

        Always (regardless of the merge flag):
          1. Resolves the checkpoint_brief as approved (additive Neotoma trail).
          2. Posts a GitHub confirmation comment (no command tokens → no
             self-retrigger, neotoma#1686).
          3. Removes the operator's review request (un-assign on resolve).

        Then the merge boundary:
          - APIS_APPROVAL_TRIGGERS_MERGE=1 → perform the actual merge via
            ``_merge_pr`` and report the REAL outcome honestly (merged with SHA,
            or not-merged with the GitHub reason — never a merge claimed that did
            not happen).
          - flag OFF (default) → conservative Phase-H1 behaviour: PR is left
            ready for the operator to merge on GitHub. Vanellus retains
            stewardship.

        ``source`` is a short human string ("/approve comment", "GitHub review",
        "email reply") threaded into the notification + comment so the audit
        trail records which channel the approval arrived on.
        """
        ref = f"{trigger.repository}#{trigger.number}"

        # 1. Resolve the checkpoint_brief (approved).
        await self._store_checkpoint_resolution(trigger, verdict="approved", reason="")

        merge_line = (
            "The PR is ready to merge — proceed on GitHub or instruct Vanellus. "
            "Vanellus retains PR stewardship."
        )
        merged_ok = False
        merge_detail = ""

        # 2b. Optional merge (flag-gated). Resolve the PR number first; if none
        #     can be identified we cannot merge and say so plainly.
        if self.config.approval_triggers_merge:
            pr_number = await self._resolve_pr_number(trigger)
            if pr_number is None:
                merge_line = (
                    "APIS_APPROVAL_TRIGGERS_MERGE is ON, but no open PR could be "
                    "resolved from this approval — nothing merged. Merge manually."
                )
            else:
                merged_ok, merge_detail = await self._merge_pr(
                    trigger.repository,
                    pr_number,
                    self.config.approval_merge_method,
                )
                if merged_ok:
                    sha = (merge_detail or "")[:12]
                    merge_line = (
                        f"Merge approved via {source} — PR #{pr_number} MERGED "
                        f"({self.config.approval_merge_method}"
                        + (f", {sha}" if sha else "")
                        + ")."
                    )
                else:
                    merge_line = (
                        f"Merge approved via {source}, but the merge did NOT "
                        f"complete: {merge_detail}. The PR was left open — "
                        "resolve the blocker and merge manually."
                    )

        # 2. Post the GitHub confirmation comment (no command tokens in body).
        await self._post_checkpoint_verdict_comment(
            trigger,
            body=(
                f"<!-- h1-checkpoint-approve -->\n"
                f"{attribution_header('apis', 'swarm dispatcher')}\n\n"
                f"Operator **approved** the pre-merge checkpoint on {ref} "
                f"(via {source}). Checkpoint resolved. {merge_line}"
            ),
        )

        # 3. Remove operator from reviewer list (best-effort, non-fatal).
        await self._remove_operator_reviewer(trigger)

        # 4. Notify.
        self.notifier.send(
            f"Operator approved merge checkpoint on {ref} (via {source}) — "
            f"checkpoint resolved, operator reviewer removed. {merge_line}",
            priority=Priority.OPERATOR_DECISION,
            handler=DAEMON_NAME,
        )

    async def _handle_reject(self, trigger: SwarmTrigger) -> None:
        """Execute the /reject <reason> operator command (Phase H1 HITL checkpoint).

        Rejects the pending pre-merge checkpoint.  Records the reason (everything
        after "/reject " in the comment), resolves the checkpoint_brief as rejected,
        removes the operator from the PR reviewer list, and notifies.

        Does NOT proceed with the merge or any other held action.

        The rejection reason is a training signal for review_learning.  Full
        wiring of that signal is a future TODO (review_learning.propose_skill_updates
        currently takes a list of (lens, text) tuples from panel reviews; rejection
        reasons need a separate entry point).  For now the reason is stored in the
        checkpoint_brief resolution entity so it is available for offline analysis.
        """
        ref = f"{trigger.repository}#{trigger.number}"
        comment_body = (trigger.comment_body or "").strip()

        # Extract the reason: everything after "/reject " (case-sensitive token).
        reason = ""
        reject_match = re.search(r"(?:^|\s)/reject\s+(.*?)(?:\s*$)", comment_body, re.DOTALL)
        if reject_match:
            reason = reject_match.group(1).strip()

        log.info(
            f"[{DAEMON_NAME}] operator {_REJECT_CMD} received on {ref} "
            f"(comment #{trigger.comment_id}): reason={reason!r}"
        )

        # 1. Resolve the checkpoint_brief as rejected.
        await self._store_checkpoint_resolution(trigger, verdict="rejected", reason=reason)

        # 2. Post a GitHub confirmation comment (no command tokens in body).
        reason_line = f"\n\nRejection reason: {reason}" if reason else ""
        await self._post_checkpoint_verdict_comment(
            trigger,
            body=(
                f"<!-- h1-checkpoint-reject -->\n"
                f"{attribution_header('apis', 'swarm dispatcher')}\n\n"
                f"Operator **rejected** the pre-merge checkpoint on {ref}. "
                "The PR will NOT be merged. Checkpoint resolved as rejected."
                f"{reason_line}\n\n"
                "The rejection reason has been stored for review-learning. "
                "Route back to the implementer if changes are needed."
            ),
        )

        # 3. Remove operator from reviewer list (best-effort, non-fatal).
        await self._remove_operator_reviewer(trigger)

        # 4. Notify.
        self.notifier.send(
            f"Operator rejected merge checkpoint on {ref}"
            + (f": {reason}" if reason else "")
            + " — PR NOT merged; operator reviewer removed.",
            priority=Priority.OPERATOR_DECISION,
            handler=DAEMON_NAME,
        )

    async def _handle_hold(self, trigger: SwarmTrigger) -> None:
        """Execute the /hold operator command (Phase H1 HITL checkpoint).

        Parks the checkpoint: acknowledges the command, leaves the checkpoint
        blocking, and leaves the operator still requested as PR reviewer.
        No state change to the checkpoint_brief — it remains open.
        """
        ref = f"{trigger.repository}#{trigger.number}"
        log.info(
            f"[{DAEMON_NAME}] operator {_HOLD_CMD} received on {ref} "
            f"(comment #{trigger.comment_id}) — checkpoint parked"
        )

        # Post a GitHub ack comment (no command tokens in body).
        await self._post_checkpoint_verdict_comment(
            trigger,
            body=(
                f"<!-- h1-checkpoint-hold -->\n"
                f"{attribution_header('apis', 'swarm dispatcher')}\n\n"
                f"Checkpoint on {ref} **parked** by operator. "
                "No action taken — the merge checkpoint remains blocking and "
                "your review request is still open. "
                "Use the approve or reject commands when ready."
            ),
        )

        self.notifier.send(
            f"Operator parked merge checkpoint on {ref} — checkpoint still blocking.",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )

    # ── Phase H1 helpers ─────────────────────────────────────────────────────

    async def _store_checkpoint_resolution(
        self, trigger: SwarmTrigger, verdict: str, reason: str
    ) -> None:
        """Store a checkpoint_brief resolution entity for the given PR/issue.

        Files a new checkpoint_brief entity with status=<verdict> so the Neotoma
        trail records the resolution.  Best-effort: logs on failure, never raises.
        """
        ref = f"{trigger.repository}#{trigger.number}"
        entities = [
            {
                "entity_type": "checkpoint_brief",
                "title": f"Merge checkpoint resolved ({verdict}): {ref}",
                "checkpoint_kind": "pr_merge",
                "blocking": False,
                "subject_ref": ref,
                "body": (
                    f"Operator {verdict} the pre-merge checkpoint on {ref} "
                    f"via {_APPROVE_CMD if verdict == 'approved' else _REJECT_CMD} "
                    f"(comment #{trigger.comment_id})."
                    + (f"  Rejection reason: {reason}" if reason else "")
                ),
                "status": verdict,
            }
        ]
        ts = datetime.now(timezone.utc).isoformat()
        await self._store_entities(
            entities,
            idempotency_key=(
                f"merge-checkpoint-{verdict}-{trigger.repository}-"
                f"{trigger.number}-{trigger.comment_id}-{ts[:16]}"
            ),
        )

    async def _post_checkpoint_verdict_comment(
        self, trigger: SwarmTrigger, body: str
    ) -> None:
        """Post a verdict comment on the PR/issue.  Best-effort, never raises."""
        repo_token = _token_for_repo(trigger.repository)
        if not repo_token:
            log.warning(
                f"[{DAEMON_NAME}] no GitHub token — verdict comment skipped for "
                f"{trigger.repository}#{trigger.number}"
            )
            return
        url = (
            f"https://api.github.com/repos/{trigger.repository}/issues/"
            f"{trigger.number}/comments"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    url, json={"body": body},
                    headers=self._github_headers(trigger.repository)
                )
                resp.raise_for_status()
                log.info(
                    f"[{DAEMON_NAME}] posted checkpoint verdict comment on "
                    f"{trigger.repository}#{trigger.number}"
                )
        except Exception as exc:
            log.warning(
                f"[{DAEMON_NAME}] failed to post verdict comment on "
                f"{trigger.repository}#{trigger.number}: {exc}"
            )

    async def _request_operator_reviewer(self, trigger: SwarmTrigger) -> None:
        """Request the operator as a PR reviewer (best-effort, non-fatal).

        Called when the merge checkpoint blocks so the pending merge shows up
        in the operator's "Review requested" GitHub queue.  Assignment is
        SURFACING; the checkpoint_brief + block is ENFORCEMENT.

        If the request fails (permissions, not-a-collaborator, already-requested),
        the checkpoint still blocks and still notifies — this is best-effort.
        """
        repo_token = _token_for_repo(trigger.repository)
        if not repo_token:
            log.warning(
                f"[{DAEMON_NAME}] no GitHub token — operator reviewer request "
                f"skipped for {trigger.repository}#{trigger.number}"
            )
            return
        url = (
            f"https://api.github.com/repos/{trigger.repository}/pulls/"
            f"{trigger.number}/requested_reviewers"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    url,
                    json={"reviewers": [_OPERATOR_LOGIN]},
                    headers=self._github_headers(trigger.repository),
                )
                # 422 means the reviewer is already requested or is not a
                # collaborator — not an error for our purposes.
                if resp.status_code not in (200, 201, 422):
                    resp.raise_for_status()
                log.info(
                    f"[{DAEMON_NAME}] requested {_OPERATOR_LOGIN!r} as PR reviewer "
                    f"on {trigger.repository}#{trigger.number} "
                    f"(status={resp.status_code})"
                )
        except Exception as exc:
            log.warning(
                f"[{DAEMON_NAME}] failed to request operator as PR reviewer on "
                f"{trigger.repository}#{trigger.number}: {exc} — "
                "checkpoint still blocking (reviewer request is surfacing only)"
            )

    async def _remove_operator_reviewer(self, trigger: SwarmTrigger) -> None:
        """Remove the operator from the PR reviewer list (best-effort, non-fatal).

        Called on /approve and /reject so the operator's "Review requested" queue
        only shows live checkpoints.  /hold leaves the reviewer in place.

        Uses the DELETE /repos/{owner}/{repo}/pulls/{pull_number}/requested_reviewers
        endpoint.  Non-fatal: if it fails (already removed, permissions), the
        checkpoint resolution still stands.
        """
        repo_token = _token_for_repo(trigger.repository)
        if not repo_token:
            log.warning(
                f"[{DAEMON_NAME}] no GitHub token — operator reviewer removal "
                f"skipped for {trigger.repository}#{trigger.number}"
            )
            return
        url = (
            f"https://api.github.com/repos/{trigger.repository}/pulls/"
            f"{trigger.number}/requested_reviewers"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.request(
                    "DELETE",
                    url,
                    json={"reviewers": [_OPERATOR_LOGIN]},
                    headers=self._github_headers(trigger.repository),
                )
                # 200 = removed; 422 = was not requested — both are fine.
                if resp.status_code not in (200, 422):
                    resp.raise_for_status()
                log.info(
                    f"[{DAEMON_NAME}] removed {_OPERATOR_LOGIN!r} from PR reviewer "
                    f"list on {trigger.repository}#{trigger.number} "
                    f"(status={resp.status_code})"
                )
        except Exception as exc:
            log.warning(
                f"[{DAEMON_NAME}] failed to remove operator reviewer on "
                f"{trigger.repository}#{trigger.number}: {exc} — "
                "checkpoint resolution still stands"
            )

    async def _post_swarm_run_comment(self, trigger: SwarmTrigger) -> None:
        """Post (or edit) a confirming comment for the operator swarm-run command.

        Fix 1 — no command token in the body: the confirmation text deliberately
        does NOT contain the literal "/swarm-run" or "/confirm-gates-clear"
        strings.  This removes the self-trigger loop at the source: a comment
        that does not contain a command token cannot match the command detector
        in _handle_issue_comment, even if the webhook fires for the bot's own
        comment (neotoma#1686).

        Fix 3 — edit-not-duplicate (SWARM_GITHUB_CONTRACT): before posting a new
        comment, scan the issue's existing comments for one that contains the
        stable HTML marker ``<!-- swarm-run-confirmation -->``.  If found, PATCH
        it in place instead of creating a second confirmation.  This prevents
        stacked duplicates when the operator issues the command more than once in
        rapid succession.

        Uses the shared GitHub token for the repo.  Best-effort: caller catches
        exceptions and logs a warning rather than propagating.
        """
        # Stable marker that identifies THIS dispatcher's confirmation comment.
        # Must NOT contain a command token (that would re-trigger the handler).
        _CONFIRMATION_MARKER = "<!-- swarm-run-confirmation -->"

        repo_token = _token_for_repo(trigger.repository)
        if not repo_token:
            log.warning(
                f"[{DAEMON_NAME}] no GitHub token — swarm-run confirmation "
                f"comment skipped for {trigger.repository}#{trigger.number}"
            )
            return

        # Fix 1: body text uses "swarm-run" without the leading slash so it
        # does NOT match the _SWARM_RUN_CMD ("/swarm-run") token detector.
        body = (
            f"{_CONFIRMATION_MARKER}\n"
            f"{attribution_header('apis', 'swarm dispatcher')}\n\n"
            "\U0001f501 Operator **swarm-run** command received — re-running "
            "the issue pipeline (Lanius triage + review expectations + Pavo "
            "scoping). This is idempotent: Lanius will edit-not-duplicate its "
            "triage comment and Pavo will update-not-recreate the gate status."
        )

        list_url = (
            f"https://api.github.com/repos/{trigger.repository}/issues/"
            f"{trigger.number}/comments"
        )
        headers = self._github_headers(trigger.repository)

        async with httpx.AsyncClient(timeout=30) as client:
            # Fix 3: look for an existing confirmation comment to edit.
            existing_id: int | None = None
            try:
                resp = await client.get(
                    list_url, params={"per_page": 100}, headers=headers
                )
                resp.raise_for_status()
                for comment in resp.json():
                    if _CONFIRMATION_MARKER in comment.get("body", ""):
                        existing_id = comment["id"]
                        break
            except Exception as exc:
                log.warning(
                    f"[{DAEMON_NAME}] could not list comments for dedup check "
                    f"on {trigger.repository}#{trigger.number}: {exc} — "
                    "will post new"
                )

            if existing_id is not None:
                # PATCH the existing comment instead of creating a duplicate.
                patch_url = (
                    f"https://api.github.com/repos/{trigger.repository}/"
                    f"issues/comments/{existing_id}"
                )
                resp = await client.patch(
                    patch_url, json={"body": body}, headers=headers
                )
                resp.raise_for_status()
                log.info(
                    f"[{DAEMON_NAME}] edited existing swarm-run confirmation "
                    f"comment #{existing_id} on {trigger.repository}#{trigger.number}"
                )
            else:
                resp = await client.post(
                    list_url, json={"body": body}, headers=headers
                )
                resp.raise_for_status()
                log.info(
                    f"[{DAEMON_NAME}] posted new swarm-run confirmation comment "
                    f"on {trigger.repository}#{trigger.number}"
                )

    async def _post_pipeline_bypass_comment(self, trigger: SwarmTrigger) -> bool:
        """Post (or edit) a visible notice that a product-code PR has no parent issue.

        This is the loud half of the pipeline-bypass guard: a product PR that
        skipped the gated issue → pm/arch → Cicada path gets a plainly-worded
        comment on the PR so the bypass is visible in the GitHub timeline, not
        just in an operator ping. Review still proceeds and merge stays
        operator-gated — this comment does NOT block anything.

        Follows the SWARM_GITHUB_CONTRACT edit-not-duplicate rule via a stable
        marker, and — like _post_swarm_run_comment — the body carries NO command
        token (`/swarm-run`, `/confirm-gates-clear`, `Closes #`) so it cannot
        self-trigger the comment handler or spoof an issue link. Best-effort:
        exceptions are logged, never propagated.

        Returns True when a NEW bypass comment was posted (the bypass is being
        surfaced for the first time), False when an existing marker was merely
        re-edited or the post was skipped/failed. The caller uses this to fire
        the operator notification only on first surfacing — otherwise every
        subsequent PR event (synchronize, label, re-open) re-notifies for the
        same already-known bypass (ateles#242 got four identical pings).
        """
        _BYPASS_MARKER = "<!-- pipeline-bypass-notice -->"

        repo_token = _token_for_repo(trigger.repository)
        if not repo_token:
            log.warning(
                f"[{DAEMON_NAME}] no GitHub token — pipeline-bypass notice "
                f"skipped for {trigger.repository}#{trigger.number}"
            )
            return False

        # Body deliberately writes "Closes" with a backtick-escaped hash so the
        # instructional text does not itself parse as a parent-issue link.
        body = (
            f"{_BYPASS_MARKER}\n"
            f"{attribution_header('apis', 'swarm dispatcher')}\n\n"
            "⚠️ **Pipeline bypass** — this PR touches product code but "
            "has no parent issue, so it skipped the gated pipeline "
            "(issue triage → pm/arch sign-off → Cicada implementation).\n\n"
            "The review panel still runs and **merge stays operator-gated**, so "
            "nothing is blocked. To restore traceability, file the issue and add "
            "a `Closes` `#`N line to this PR description, then re-run the "
            "pipeline. Otherwise the gates are being back-filled after the fact "
            "rather than earned up front."
        )

        list_url = (
            f"https://api.github.com/repos/{trigger.repository}/issues/"
            f"{trigger.number}/comments"
        )
        headers = self._github_headers(trigger.repository)

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                existing_id: int | None = None
                try:
                    resp = await client.get(
                        list_url, params={"per_page": 100}, headers=headers
                    )
                    resp.raise_for_status()
                    for comment in resp.json():
                        if _BYPASS_MARKER in comment.get("body", ""):
                            existing_id = comment["id"]
                            break
                except Exception as exc:
                    log.warning(
                        f"[{DAEMON_NAME}] could not list comments for bypass "
                        f"dedup on {trigger.repository}#{trigger.number}: {exc} "
                        "— will post new"
                    )

                if existing_id is not None:
                    # A marker already exists → this bypass was surfaced before.
                    # Editing it in place is a no-op re-notify; even if the PATCH
                    # fails transiently the bypass is already known, so fail
                    # CLOSED (return False) — never re-ping for a known bypass.
                    patch_url = (
                        f"https://api.github.com/repos/{trigger.repository}/"
                        f"issues/comments/{existing_id}"
                    )
                    try:
                        resp = await client.patch(
                            patch_url, json={"body": body}, headers=headers
                        )
                        resp.raise_for_status()
                        log.info(
                            f"[{DAEMON_NAME}] edited existing pipeline-bypass "
                            f"notice #{existing_id} on "
                            f"{trigger.repository}#{trigger.number}"
                        )
                    except Exception as exc:
                        log.warning(
                            f"[{DAEMON_NAME}] could not edit existing bypass "
                            f"notice on {trigger.repository}#{trigger.number}: "
                            f"{exc}"
                        )
                    return False
                resp = await client.post(
                    list_url, json={"body": body}, headers=headers
                )
                resp.raise_for_status()
                log.info(
                    f"[{DAEMON_NAME}] posted pipeline-bypass notice on "
                    f"{trigger.repository}#{trigger.number}"
                )
                return True
        except Exception as exc:
            # We reached here without confirming an existing marker (the dedup
            # GET failed and/or the NEW-comment POST failed), so this may be a
            # first-time bypass we could not surface. Fail OPEN — return True so
            # the operator is still notified — mirroring _claim_escalation and
            # ensuring a genuine bypass is never silently swallowed by a
            # transient GitHub error. (A confirmed-duplicate PATCH failure
            # returns False above and never reaches here.)
            log.warning(
                f"[{DAEMON_NAME}] failed to post pipeline-bypass notice on "
                f"{trigger.repository}#{trigger.number}: {exc} — notifying "
                "anyway (fail-open)"
            )
            return True

    async def _fetch_issue_fields(
        self, repository: str, issue_number: int
    ) -> dict | None:
        """Fetch title, body, and labels for an issue via the GitHub API.

        Used as a fallback when the issue_comment trigger payload carries an
        empty title (sparse webhook delivery).  Returns None on any error.
        """
        url = f"https://api.github.com/repos/{repository}/issues/{issue_number}"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    url, headers=self._github_headers(repository)
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            log.warning(
                f"[{DAEMON_NAME}] failed to fetch issue fields for "
                f"{repository}#{issue_number}: {exc}"
            )
            return None

    async def _waive_gates(self, trigger: SwarmTrigger) -> WaiveOutcome:
        """Waive all unsigned pre-impl gates DISPATCHER-SIDE, then verify.

        ateles#285 — root cause and fix.  This used to prompt Lanius to perform
        the ``correct()`` calls.  That silently no-opped (2026-07-27T17:37: the
        agent spawned, produced nothing, wrote nothing, and posted nothing) and
        before that PARTIALLY applied (2026-07-23: waived ``arch``, left ``ux``
        pending).  Because the helper was documented "best-effort — logs on
        failure but does not raise", the operator saw the command acknowledged
        and had no reason to think it had not worked, while PR gate inheritance
        kept blocking correctly on the still-pending gate.

        A gate waive is a DETERMINISTIC STATE TRANSITION: ``gate_status.<gate>``
        → ``waived`` for each unsigned pre-impl gate, plus one ``owner_history``
        append.  There is no judgement in it, so it must not depend on an agent
        turn (same lesson as ateles#263 — do not rely on an LLM to persist a
        mechanical mutation).  The dispatcher now:

          1. reads the issue entity,
          2. sweeps ALL of :data:`PRE_IMPL_GATES` (a total function — the
             partial-apply mode is structurally impossible),
          3. writes ``gate_status`` + ``owner_history``,
          4. RE-READS and asserts every targeted gate is now cleared,
          5. reports the outcome to the operator on GitHub either way.

        Returns the :class:`WaiveOutcome` so the caller can decide whether to
        re-trigger the PR pipeline.  Never raises.
        """
        ref = f"{trigger.repository}#{trigger.number}"
        issue_number = trigger.number
        # comment_on_pr means trigger.number IS the PR number; the parent issue
        # number is in the PR body.  Extract it.
        if trigger.comment_on_pr:
            issue_number = self._parent_issue_number(trigger.body) or trigger.number

        store = IssueGateStore(
            self.config.neotoma_base_url, self.config.neotoma_token
        )
        try:
            outcome = await store.waive(
                trigger.repository, issue_number, PRE_IMPL_GATES
            )
        except Exception as exc:  # noqa: BLE001 — never crash the pipeline
            log.error(
                f"[{DAEMON_NAME}] gate waive raised on {ref}: {exc} — "
                "reporting failure to the operator"
            )
            outcome = WaiveOutcome(
                entity_found=True, targeted=list(PRE_IMPL_GATES),
                failed=list(PRE_IMPL_GATES),
            )

        # ALWAYS surface the result on GitHub — success, no-op, and failure.
        # The 2026-07-27 failure produced ZERO GitHub-visible output, which is
        # a core part of the bug (#285 point 3).
        try:
            await self._post_gate_waive_comment(trigger, outcome)
        except Exception as exc:  # noqa: BLE001 — comment is best-effort
            log.warning(
                f"[{DAEMON_NAME}] could not post gate-waive comment on {ref}: "
                f"{exc}"
            )

        if not outcome.entity_found:
            # ateles#416: this used to report and stop, naming a remedy nothing
            # was scheduled to perform. Back-fill the entity and retry the waive
            # once — a gate that cannot be signed OR waived is a dead end, and
            # the PR closing that issue is unmergeable by every available path.
            if await self.ensure_issue_entity(trigger.repository, issue_number):
                log.info(
                    f"[{DAEMON_NAME}] gate waive on {ref}: entity backfilled — "
                    "retrying the waive"
                )
                outcome = await store.waive(
                    trigger.repository, issue_number, PRE_IMPL_GATES
                )
                try:
                    await self._post_gate_waive_comment(trigger, outcome)
                except Exception as exc:  # noqa: BLE001 — comment best-effort
                    log.warning(
                        f"[{DAEMON_NAME}] could not post retried gate-waive "
                        f"comment on {ref}: {exc}"
                    )

        if not outcome.entity_found:
            log.error(
                f"[{DAEMON_NAME}] gate waive on {ref}: no Neotoma issue entity "
                f"for {trigger.repository}#{issue_number} and backfill did not "
                "produce one — nothing waived"
            )
            self.notifier.send(
                f"Gate waive on {ref} FAILED — no Neotoma issue entity for "
                f"{trigger.repository}#{issue_number}, and automatic backfill "
                "did not create one. Run `python3 execution/scripts/"
                f"trigger_swarm_pr.py issue {issue_number}` to triage it "
                "manually; the pipeline stays blocked until the entity exists.",
                priority=Priority.BLOCKER,
                handler=DAEMON_NAME,
            )
        elif outcome.failed:
            log.error(
                f"[{DAEMON_NAME}] gate waive on {ref} VERIFICATION FAILED — "
                f"still unsigned: {', '.join(outcome.failed)}"
            )
            self.notifier.send(
                f"Gate waive on {ref} FAILED verification — these gates are "
                f"still unsigned after the write: {', '.join(outcome.failed)}. "
                "The review pipeline will keep blocking.",
                priority=Priority.BLOCKER,
                handler=DAEMON_NAME,
            )
        elif outcome.waived:
            log.info(
                f"[{DAEMON_NAME}] gate waive on {ref}: waived and verified "
                f"{', '.join(outcome.waived)}"
            )
        else:
            log.info(
                f"[{DAEMON_NAME}] gate waive on {ref}: no gates needed waiving "
                "(all pre-impl gates already clear)"
            )
        return outcome

    async def ensure_issue_entity(
        self, repository: str, issue_number: int
    ) -> bool:
        """Ensure *issue_number* has a Neotoma issue entity with gates initialised.

        Creates the entity when missing, and dispatches triage when the entity
        exists but was never triaged (no ``gate_status``).

        ateles#416. Gate state lives on the issue entity, so when that entity
        does not exist a gate can be neither SIGNED nor WAIVED — both operate on
        an object that is not there. The waive path reported this and stopped:

            no Neotoma issue entity was found for this issue, so there is no
            `gate_status` to waive. The gate pipeline will keep blocking until
            the issue is triaged

        The remedy it names is real, but nothing was scheduled to perform it.
        Triage fires on `issue.opened` only — one-shot, no sweep, no retry — so
        an issue whose triage failed or predates the pipeline stays entity-less
        permanently, and every PR closing it is permanently unmergeable.

        Dispatches the Lanius new-issue protocol and RE-READS to confirm the
        entity now exists, per the #285 rule: an LLM turn asked to persist a
        mechanical mutation can silently no-op, so a dispatch returning ok is
        not evidence the entity was created.

        Returns True when the entity exists AND carries a ``gate_status``
        afterwards — existence alone is not success, because an un-triaged
        entity blocks the gates exactly as a missing one does. Bounded by
        construction:
        exactly one dispatch and one re-read, no internal loop — the caller owns
        retry policy.
        """
        ref = f"{repository}#{issue_number}"
        store = IssueGateStore(
            self.config.neotoma_base_url, self.config.neotoma_token
        )
        state = await store.load(repository, issue_number)
        if state.triaged:
            return True  # fast path: entity exists and its gates are initialised

        if state.found:
            # The entity exists but has no gate_status. Testing `found` here
            # short-circuited this case as success, which is why entities
            # created outside the `issue.opened` webhook (CLI / MCP / sync)
            # stayed permanently un-triaged: the object was there, so the
            # backfill declared victory without ever dispatching triage.
            log.warning(
                f"[{DAEMON_NAME}] {ref}: Neotoma issue entity exists but has "
                "no gate_status — dispatching Lanius triage to initialise it"
            )
        else:
            log.warning(
                f"[{DAEMON_NAME}] {ref}: no Neotoma issue entity — dispatching "
                "Lanius triage to backfill it (ateles#416)"
            )
        issue = await self._fetch_issue(repository, issue_number)
        if issue is None:
            log.error(
                f"[{DAEMON_NAME}] {ref}: cannot backfill — GitHub issue could "
                "not be read"
            )
            return False

        trigger = SwarmTrigger(
            kind="issue_opened",
            repository=repository,
            number=issue_number,
            title=issue.get("title", ""),
            body=issue.get("body") or "",
            author=(issue.get("user") or {}).get("login", ""),
            html_url=issue.get("html_url", ""),
            delivery_id=f"entity-backfill-{issue_number}",
            action="opened",
        )
        result = await run_skill(
            "lanius",
            self._lanius_issue_prompt(trigger),
            github_token=_token_for_agent_on_repo("lanius", repository),
            include_github_contract=True,
            notifier=self.notifier,
        )
        if not result.ok:
            log.error(
                f"[{DAEMON_NAME}] {ref}: backfill triage failed "
                f"({result.error or f'rc={result.returncode}'})"
            )
            return False

        # Read back. Triage reporting success is not evidence the entity landed.
        after = await store.load(repository, issue_number)
        if after.triaged:
            log.info(f"[{DAEMON_NAME}] {ref}: issue entity triaged")
        elif after.found:
            log.error(
                f"[{DAEMON_NAME}] {ref}: backfill triage returned ok but the "
                "entity still has no gate_status — gate ops remain blocked"
            )
        else:
            log.error(
                f"[{DAEMON_NAME}] {ref}: backfill triage returned ok but the "
                "entity still does not exist — gate ops remain blocked"
            )
        return after.triaged

    async def _fetch_issue(
        self, repository: str, issue_number: int
    ) -> dict | None:
        """Fetch a GitHub issue; None on error (caller decides what that means)."""
        url = (
            f"https://api.github.com/repos/{repository}/issues/{issue_number}"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    url, headers=self._github_headers(repository)
                )
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:  # noqa: BLE001 — fetch is best-effort
            log.warning(
                f"[{DAEMON_NAME}] could not fetch {repository}#{issue_number}: "
                f"{exc}"
            )
            return None

    async def _post_gate_waive_comment(
        self, trigger: SwarmTrigger, outcome: WaiveOutcome
    ) -> None:
        """Post (or edit) the operator-visible gate-waive result comment.

        Follows the SWARM_GITHUB_CONTRACT edit-not-duplicate rule via a stable
        marker, and — like _post_swarm_run_comment — the body carries NO command
        token so the dispatcher's own comment cannot re-trigger the command
        detector (neotoma#1686 self-trigger defence).
        """
        _WAIVE_MARKER = "<!-- swarm-gate-waive-result -->"

        if not _token_for_repo(trigger.repository):
            log.warning(
                f"[{DAEMON_NAME}] no GitHub token — gate-waive result comment "
                f"skipped for {trigger.repository}#{trigger.number}"
            )
            return

        body = format_waive_comment(
            marker=_WAIVE_MARKER,
            header=attribution_header("apis", "swarm dispatcher"),
            waived=outcome.waived,
            already_clear=outcome.already_clear,
            failed=outcome.failed,
            entity_found=outcome.entity_found,
        )

        list_url = (
            f"https://api.github.com/repos/{trigger.repository}/issues/"
            f"{trigger.number}/comments"
        )
        headers = self._github_headers(trigger.repository)

        async with httpx.AsyncClient(timeout=30) as client:
            existing_id: int | None = None
            try:
                resp = await client.get(
                    list_url, params={"per_page": 100}, headers=headers
                )
                resp.raise_for_status()
                for comment in resp.json():
                    if _WAIVE_MARKER in comment.get("body", ""):
                        existing_id = comment["id"]
                        break
            except Exception as exc:
                log.warning(
                    f"[{DAEMON_NAME}] could not list comments for gate-waive "
                    f"dedup on {trigger.repository}#{trigger.number}: {exc} — "
                    "will post new"
                )

            if existing_id is not None:
                patch_url = (
                    f"https://api.github.com/repos/{trigger.repository}/"
                    f"issues/comments/{existing_id}"
                )
                resp = await client.patch(
                    patch_url, json={"body": body}, headers=headers
                )
                resp.raise_for_status()
                log.info(
                    f"[{DAEMON_NAME}] edited gate-waive result comment "
                    f"#{existing_id} on {trigger.repository}#{trigger.number}"
                )
            else:
                resp = await client.post(
                    list_url, json={"body": body}, headers=headers
                )
                resp.raise_for_status()
                log.info(
                    f"[{DAEMON_NAME}] posted gate-waive result comment on "
                    f"{trigger.repository}#{trigger.number}"
                )

    # ── prompts ──────────────────────────────────────────────────────────────

    @staticmethod
    def _lanius_issue_prompt(t: SwarmTrigger) -> str:
        pavo_assign_block = (
            "Also assign the GitHub issue to Pavo's account when that account "
            "exists: `gh issue edit {n} --add-assignee {login} --repo {repo}` "
            "(best-effort — skip silently if the assignee is not a collaborator "
            "/ account does not exist; never fail triage).".format(
                n=t.number,
                repo=t.repository,
                login=agent_github_login("pavo"),
            )
            if is_provisioned("pavo")
            else ""
        )
        return (
            "Invoke the lanius agent per your appended system prompt.\n\n"
            f"A new GitHub issue was opened (webhook trigger, no operator in "
            f"the loop): {t.repository}#{t.number} — {t.title}\n{t.html_url}\n\n"
            f"{t.body}\n\n"
            "Run your new-issue protocol: load the workflow_definition, "
            "initialize gate_status/current_owner/owner_history on the issue "
            "entity, assign Pavo as Phase 1 owner, post the triage comment, "
            "and apply the lanius-triage label (best-effort — create the label "
            "first if missing, and never fail triage if labeling errors)."
            + (f"\n\n{pavo_assign_block}" if pavo_assign_block else "")
            + f"\n\n{_agent_prompt_instruction('lanius', 'issue triage')}"
        )

    @staticmethod
    def _expectation_prompt(t: SwarmTrigger, lens: Lens) -> str:
        return (
            f"Invoke the {lens.agent} agent per your appended system prompt.\n\n"
            f"GitHub issue {t.repository}#{t.number}: {t.title}\n{t.html_url}\n\n"
            f"{t.body}\n\n"
            f"Pre-register your review expectations for this issue (shift-left "
            f"review contract). Through your `{lens.lens}` lens — {lens.checks} — "
            "post ONE GitHub comment on the issue, formatted exactly as:\n\n"
            f"**{EXPECTATION_MARKER} ({lens.lens})** — what {lens.agent} will "
            "verify when a PR addresses this issue:\n"
            "- [ ] <one concrete, verifiable check>\n"
            "- [ ] <... 3 to 6 total, each a GitHub task-list item using "
            "`- [ ] ` exactly>\n\n"
            "Use GitHub task-list checkbox syntax (`- [ ]`) for every item — "
            "these are checked off at PR-review time. Do not use plain `-` "
            "bullets.\n\n"
            "Also store the same checklist in Neotoma as a plan_contribution "
            f"entity (contribution_type: {EXPECTATION_MARKER}, agent: "
            f"{lens.agent}) linked PART_OF the issue entity.\n"
            "Keep it a checklist, not an essay. The implementer treats these "
            "as binding definition-of-done."
        )

    @staticmethod
    def _pavo_prompt(t: SwarmTrigger) -> str:
        return (
            "Invoke the pavo agent per your appended system prompt.\n\n"
            f"You are Phase 1 (pm) owner for GitHub issue "
            f"{t.repository}#{t.number}: {t.title}\n{t.html_url}\n\n{t.body}\n\n"
            "Run your pm-gate scoping protocol: validate intent, acceptance "
            "criteria, and scope. Read any "
            f"`{EXPECTATION_MARKER}` comments on the issue first — they are "
            "the review contract for this issue.\n\n"
            "MANDATORY SIGN-OFF RULE (ateles#112): when scoping PASSES — "
            "intent is clear, acceptance criteria exist, and scope is "
            "adequately bounded — you MUST do ALL of the following:\n"
            "  1. `correct()` the issue entity: set `gate_status.pm` → "
            "`\"signed_off\"`.\n"
            "  2. Store a `plan_contribution` entity with "
            "`contribution_type: \"sign_off\"`, `gate: \"pm\"`, "
            "`agent: \"pavo\"`, and a brief `summary` of what you validated.\n"
            "  3. Append to `owner_history`: "
            '`{"gate": "pm", "action": "signed_off", "actor": "pavo", '
            '"timestamp": "<now>"}`.\n'
            "  4. Set `current_owner` to `\"arch\"` (advancing to the next "
            "phase).\n"
            "  5. Post a GitHub comment on the issue confirming the pm gate "
            "is signed off and what you validated.\n\n"
            "Only leave pm `pending` or set it to `blocked` when scoping "
            "GENUINELY FAILS — missing intent, no acceptance criteria, or "
            "scope is unclear. In that case post a comment explaining exactly "
            "what is missing so the author can address it. Do NOT leave pm "
            "`pending` after a successful evaluation — a pending pm gate is "
            "a deadlock for any PR that closes this issue.\n\n"
            f"{_agent_prompt_instruction('pavo', 'pm gate owner')}"
        )

    @staticmethod
    def _spec_section_prompt(
        t: SwarmTrigger, section: SpecSection, spec_so_far: str
    ) -> str:
        """Ordered additive-spec prompt: write/replace ONLY this agent's section.

        Each agent in the canonical sequence reads the accumulated spec-so-far
        and ADDS or REPLACES exactly its own section, building on prior sections
        (design assumes PM's scope, eng assumes design, qa assumes eng, …).

        The spec lives in the issue BODY, not in comments: the agent must NOT
        post its section as a GitHub comment.  It returns its section text
        wrapped between the fences below; the dispatcher stores it into the
        issue_spec entity's `<section>_section` field and mirrors the body.
        Verdicts (gate pass/fail, blockers) stay as comments.
        """
        # Section-specific method guidance keyed by lens.
        method_by_lens = {
            "pm": (
                "Validate intent, acceptance criteria, and scope. Your section "
                "is the SCOPE OF RECORD every later lens builds on: crisp problem "
                "statement, in-scope / out-of-scope, and a bulleted acceptance "
                "criteria checklist."
            ),
            "ux": (
                "Assume PM's scope above. Specify the agent/developer experience "
                "of the new surface: naming, error messages with actionable "
                "hints, and the docs/examples the change must ship."
            ),
            "eng": (
                "Assume the PM scope and Design section above. Specify the "
                "implementation approach: files/modules to touch, data/contract "
                "changes, layering, and the concrete build steps a PR will take. "
                "Do NOT write code here — write the plan the build PR will follow."
            ),
            "qa": (
                "Assume the Engineering plan above. Specify the test plan: the "
                "regression test for any fixed bug, edge cases for new branches, "
                "and contract tests for new endpoints — as a definition-of-done "
                "checklist."
            ),
            "arch": (
                "Assume the sections above. Specify the security/architecture "
                "requirements: contract-first ordering, tenant isolation on every "
                "entity lookup, idempotency_key on mutating ops, and any auth / "
                "credential-exposure constraints the build must honor."
            ),
            "legal": (
                "Assume the sections above. Specify the legal/compliance "
                "requirements: dependency licensing, PII/data-handling on "
                "public-effect surfaces, and guest-token / credential scope."
            ),
        }
        method = method_by_lens.get(
            section.lens, f"Contribute your `{section.lens}` lens to the spec."
        )

        pm_gate_block = ""
        if section.lens == "pm":
            # PM still owns the pm gate: fold the mandatory sign-off in so gate
            # mechanics keep advancing the pipeline (comments carry the verdict,
            # the body carries the spec).
            pm_gate_block = (
                "\n\nGATE (verdict via comment, not spec): when scoping PASSES you "
                "MUST also (a) `correct()` the issue entity `gate_status.pm` → "
                '`"signed_off"`, (b) store a `plan_contribution` '
                '(`contribution_type: "sign_off"`, `gate: "pm"`, `agent: "pavo"`), '
                '(c) append `owner_history` `{"gate":"pm","action":"signed_off",'
                '"actor":"pavo"}`, (d) set `current_owner` → `"arch"`, and (e) post '
                "ONE short verdict COMMENT confirming the pm gate is signed off. "
                "Only leave pm `pending`/`blocked` when scoping GENUINELY FAILS — a "
                "pending pm gate deadlocks any PR that closes this issue."
            )

        return (
            f"Invoke the {section.agent} agent per your appended system prompt.\n\n"
            f"You are the `{section.lens}` lens in the Ateles swarm's ORDERED "
            f"ADDITIVE SPEC pipeline for GitHub issue "
            f"{t.repository}#{t.number}: {t.title}\n{t.html_url}\n\n"
            "----- ISSUE DESCRIPTION -----\n"
            f"{t.body}\n"
            "----- END ISSUE DESCRIPTION -----\n\n"
            "The specification is assembled section-by-section, in this order: "
            "PM → Design/UX → Eng → QA → Security → Legal. The accumulated "
            "spec-so-far (prior sections) is below — READ IT and BUILD ON IT.\n\n"
            "----- SPEC SO FAR -----\n"
            f"{spec_so_far}\n"
            "----- END SPEC SO FAR -----\n\n"
            f"Your task: {method}\n\n"
            "ADDITIVE CONTRACT — read carefully:\n"
            f"- Write or REPLACE ONLY the `{section.heading}` section. Do NOT "
            "rewrite, summarize, or edit any other lens's section — they are "
            "owned by other agents (mirrors the plan-field merge discipline).\n"
            "- The spec lives in the issue BODY. Do NOT post your section as a "
            "GitHub comment — the dispatcher writes it into the issue "
            "description for you. Comments are for VERDICTS ONLY (gate pass/fail, "
            "blockers).\n"
            "- Return your section as markdown wrapped EXACTLY between these "
            "fences, and nothing else outside them that you want persisted:\n\n"
            "<<<SPEC_SECTION>>>\n"
            f"<your `{section.heading}` markdown here — concise, checklist-first, "
            "building on the sections above>\n"
            "<<<END_SPEC_SECTION>>>"
            f"{pm_gate_block}"
        )

    @staticmethod
    def _cicada_build_prompt(t: SwarmTrigger, state: SpecState) -> str:
        """Auto-build handoff prompt: Cicada opens an implementation PR.

        Only used when ``ATELES_SWARM_AUTO_BUILD`` is ON.  Cicada branches off
        FRESH origin/main in a worktree, implements the assembled spec, and
        opens a PR referencing the issue so the ``_handle_pr`` gate pipeline
        takes over.  NEVER merges — the merge boundary stays operator-gated.
        """
        spec_md = assemble_spec_markdown(state.sections or {})
        return (
            "Invoke the cicada agent per your appended system prompt.\n\n"
            f"The Ateles swarm has assembled a full additive specification for "
            f"GitHub issue {t.repository}#{t.number}: {t.title}\n{t.html_url}\n\n"
            "Auto-build is ON and the pre-impl gates are green. Implement the "
            "specification below and OPEN AN IMPLEMENTATION PULL REQUEST so the "
            "swarm's PR gate pipeline takes over.\n\n"
            "----- ASSEMBLED SPECIFICATION -----\n"
            f"{spec_md}\n"
            "----- END ASSEMBLED SPECIFICATION -----\n\n"
            "READING THE SPEC — severity markers (ateles#359). The spec above was "
            "written by the review lenses, so it may contain `[BLOCKING]` and "
            "`[NON-BLOCKING]` markers. Inside THIS spec those are "
            "DEFINITION-OF-DONE items for the PR you are about to open — "
            "requirements to satisfy in your implementation — NOT findings "
            "against existing work and NOT reasons to withhold a PR. They do not "
            "mean 'the author must address them before opening'; you ARE the "
            "author, and addressing them IS the build. Some are explicitly "
            "conditional ('this becomes [BLOCKING] if X'); treat those as "
            "forward-looking notes unless the condition holds today. The "
            "pre-impl gates are already green — a marker in the spec is not an "
            "open gate. Satisfy what you can within scope, note in the PR body "
            "anything you deliberately deferred and why, and OPEN THE PR. Raise "
            "a checkpoint instead of opening a PR only if you cannot produce a "
            "working implementation at all — never merely because the spec "
            "contains the word 'blocking'.\n\n"
            "BUILD PROTOCOL:\n"
            "  1. Branch off FRESH origin/main (fetch first) in an isolated git "
            "worktree — never off a feature branch.\n"
            "  2. Implement the spec, following the Engineering section's plan "
            "and honoring the QA/Security/Legal requirements as "
            "definition-of-done.\n"
            "  3. Add/extend tests per the QA section and run them.\n"
            f"  4. Open a PR whose body references the issue with "
            f"`Closes #{t.number}` so gate inheritance links back to it.\n\n"
            "HARD GUARDRAIL — DO NOT MERGE. Opening the PR is the end of your "
            "handoff: the PR gate pipeline reviews it and the merge stays "
            "operator-gated. Never merge, and never force-push over main.\n\n"
            f"{_agent_prompt_instruction('cicada', 'implementation build')}"
        )

    @staticmethod
    def _lanius_pr_prompt(t: SwarmTrigger, parent: int | None) -> str:
        parent_line = (
            f"The PR body references parent issue #{parent}."
            if parent
            else "No parent issue reference found in the PR body — find it "
            "yourself or treat gate inheritance as blocked."
        )
        operator_login = _OPERATOR_LOGIN
        return (
            "Invoke the lanius agent per your appended system prompt.\n\n"
            f"A pull request event ({t.action}) fired for "
            f"{t.repository}#{t.number}: {t.title}\n{t.html_url}\n"
            f"head={t.head_ref} base={t.base_ref}\n\n{t.body}\n\n"
            f"{parent_line}\n\n"
            "Run your PR-opened protocol: enforce PR gate inheritance from "
            "the parent issue (block with a comment listing pending gates if "
            "any pre-impl gate is not signed_off/waived); if clear, assign "
            "Vanellus as reviewer.\n\n"
            "GATE-STATUS SOURCE OF TRUTH: the parent issue's `gate_status` "
            "lives on its NEOTOMA issue entity, NOT in the GitHub issue body "
            "(the GitHub body never carries gate metadata). ALWAYS retrieve it "
            "first: `retrieve_entity_by_identifier(entity_type='issue', "
            "identifier=<number>, identifier_field='github_number')` (scoped to "
            "this repo), and read `gate_status` from that entity's snapshot. "
            "Treat that stored `gate_status` as AUTHORITATIVE. A pre-impl gate "
            "in `signed_off`, `waived`, or `not_required` counts as satisfied. "
            "If pm/ux/arch are all satisfied there, emit `GATE_INHERITANCE: "
            "clear` — do NOT re-derive gate state from anything else.\n\n"
            "LEGACY-ISSUE RULE: distinguish 'gates never initialized' from "
            "'gates evaluated and still pending'. ONLY when the retrieved issue "
            "entity has NO `gate_status` key at all (truly absent — not merely "
            "pending) do you treat it as legacy: do NOT hard-block, initialize "
            "the ABSENT gate keys retroactively, note the legacy status, and "
            "emit `GATE_INHERITANCE: clear`. Legacy init MUST MERGE, never "
            "overwrite: if the entity already has a `gate_status` with any gate "
            "at `signed_off`/`waived`, PRESERVE those exactly — never downgrade "
            "a `signed_off` gate to `pending`. Only emit `blocked` when "
            "`gate_status` exists AND a pre-impl gate is genuinely `pending` "
            "(unsigned). Merge stays operator-gated regardless, per the "
            "fail-open-for-review guardrail. To run the full issue pipeline on "
            "a legacy issue (gate init + expectations + Pavo), the operator can "
            "backfill via `trigger_swarm_pr.py issue <n>`.\n\n"
            "BLOCKED COMMENT REQUIREMENTS (ateles#112): when you post a "
            "blocking comment, it MUST include:\n"
            "  1. A list of WHICH pre-impl gates are unsigned and who owns "
            "each (e.g. `pm` owned by Pavo, `ux` owned by Accipiter, `arch` "
            "owned by Waxwing).\n"
            "  2. The exact operator-override command: "
            f"`/confirm-gates-clear` — only @{operator_login} may issue this "
            "command; it waives all unsigned pre-impl gates and re-triggers "
            "the PR pipeline. No other commenter can clear gates.\n"
            "  3. The normal resolution path: the gate owner can sign off the "
            "gate via Neotoma (set gate_status.<gate> → signed_off) or the "
            "operator can waive it.\n\n"
            f"{_agent_prompt_instruction('lanius', 'PR gate inheritance')}\n\n"
            "End your reply with exactly one line: `GATE_INHERITANCE: clear` "
            "or `GATE_INHERITANCE: blocked` so the dispatcher can route. When "
            "you emit `blocked`, add a SECOND line naming the unsigned pre-impl "
            "gates as a comma-separated list, e.g. `GATE_PENDING: arch,ux` — "
            "the dispatcher uses this to guarantee the lens agent that owns "
            "each pending gate a seat on the (capped) review panel, so a gate "
            "can never stay blocked because its owning lens was never "
            "re-invoked. Omit the line when clear."
        )

    @staticmethod
    def _panelist_prompt(
        t: SwarmTrigger,
        lens: Lens,
        expectation: str,
        parent: int | None = None,
        has_worktree: bool = False,
        owns_pending_gate: bool = False,
    ) -> str:
        """Build a lens panelist's prompt.

        `has_worktree` reflects whether this panelist actually got a writable PR
        checkout as its cwd. It gates the evidence bar: telling a lens to "run
        the code" when it is reviewing diff-only would be a lie that either
        wastes its turn or invites invented output (#254 / plan
        ent_ccd6660fc28800a2ae3a5623).
        """
        expectation_block = (
            "Your pre-registered expectations on the parent issue were:\n"
            f"{expectation}\n\nReview against them first: did the change meet "
            "what you said you would check?"
            if expectation
            else "You did not pre-register expectations for this issue; review "
            "against your standing lens criteria."
        )
        # The evidence bar for a [BLOCKING] verdict. Motivated by neotoma PR
        # #1946: 0 of 11 blocking findings across 3 rounds cited executing
        # anything, and both wrong findings came from a lens with no checkout.
        # Reading the code is a hypothesis; running it is evidence.
        evidence_bar = (
            "EVIDENCE BAR FOR BLOCKING. Your cwd is a writable checkout of the "
            "PR branch, and you have Bash. A finding may be `[BLOCKING]` ONLY "
            "if you RAN something that demonstrates it — a failing test, a "
            "command whose output contradicts what the code claims, a "
            "reproduction of the defect. Quote the command and its ACTUAL "
            "output in the finding's detail. If you cannot reproduce it, or you "
            "only reasoned from reading the diff, file it `[NON-BLOCKING]` and "
            "say what you could not verify. This is the standard "
            "`fixed_means_behavior_verified_not_contract_accepted` "
            "(ent_db0b7855d47012084477fb00) already imposes on the implementer; "
            "it binds you too. Do not block a merge on a hypothesis."
            if has_worktree
            else "EVIDENCE BAR FOR BLOCKING. You are reviewing DIFF-ONLY this "
            "run: no PR checkout could be prepared, so you cannot execute the "
            "code. Findings you cannot demonstrate by running something are "
            "hypotheses. Prefer `[NON-BLOCKING]`, and state plainly that the "
            "concern is unverified and what would confirm it. Reserve "
            "`[BLOCKING]` for defects evident from the diff itself (a missing "
            "declaration, a contradicted invariant, an absent required artifact) "
            "— never for a claim about runtime behaviour you could not observe."
        )
        blocking_rules = (
            "Your output is FORWARD-LOOKING and non-blocking: do not request "
            "changes; if the PR enables downstream work for you, create that "
            "task in your own queue and say so in the comment."
            if lens.forward_looking
            else "Findings that must block the merge: emit each as a line "
            "`[BLOCKING] <category>: <summary>` followed by detail and file "
            "references. Non-blocking suggestions: `[NON-BLOCKING] <category>: "
            "<summary>`. Cite the standing rule or guardrail doc when one "
            "applies — that marks the finding as systemic.\n\n" + evidence_bar
        )
        # Build the check-off instruction only when there is a parent issue AND
        # this panelist pre-registered expectations (so there is a comment to edit).
        checkoff_block = ""
        if parent and expectation:
            owner_repo = t.repository
            checkoff_block = (
                f"\n\nAfter posting your review, update your pre-registered "
                f"expectation checklist on the parent issue #{parent} to reflect "
                f"what this PR satisfied: find YOUR OWN `{EXPECTATION_MARKER} "
                f"({lens.lens})` comment on issue #{parent} (use "
                f"`gh api repos/{owner_repo}/issues/{parent}/comments` or "
                f"`gh issue view {parent} --repo {owner_repo} --comments` to "
                f"locate the comment authored for your lens), then edit it "
                f"(`gh api -X PATCH repos/{owner_repo}/issues/comments/<id> "
                f"-f body='...'`) so each item you verified in this PR is "
                f"`- [x]` and each not-yet-met item stays `- [ ]`. Only check "
                f"boxes that your review of THIS PR actually confirmed; leave "
                f"the rest unchecked. Do NOT create a new comment — edit the "
                f"existing one in place. Preserve the header line "
                f"(`**{EXPECTATION_MARKER} ({lens.lens})**`) and all item text "
                f"exactly; only toggle the checkboxes from `[ ]` to `[x]`."
            )
        # Gate-writeback (ateles: PR-panel sign-off never reached gate_status).
        # A lens seated because it OWNS a pending pre-impl gate (arch/ux/pm) must
        # transcribe its own non-blocking verdict into the parent issue's
        # `gate_status` — otherwise the panel re-runs the lens, the lens signs off
        # in a PR comment, but `gate_status.<gate>` stays `pending`, so Lanius
        # re-blocks next round and the PR loops forever (observed on #1944: arch
        # SIGNED_OFF in comments across multiple rounds while gate_status.arch
        # stayed pending). This is the missing writeback: the panelist owns its
        # own gate_status mutation (the dispatcher never writes gate state).
        gate_writeback_block = ""
        if parent and owns_pending_gate and not lens.forward_looking:
            gate_writeback_block = (
                f"\n\nGATE WRITEBACK — you own the pre-impl `{lens.lens}` gate on "
                f"parent issue #{parent}, which is currently `pending`. Your PR "
                "comment is NOT sufficient to clear it; the gate lives on the "
                "issue entity and Lanius reads it as authoritative. After you post "
                "your review, you MUST reconcile `gate_status` yourself:\n"
                f"  1. Retrieve the parent issue entity: "
                f"`retrieve_entity_by_identifier(entity_type='issue', "
                f"identifier='{parent}', by='github_number')` and read its "
                "`gate_status`.\n"
                f"  2. If — and ONLY if — your verdict on THIS PR is a clean "
                f"sign-off (no `[BLOCKING]` findings from your `{lens.lens}` "
                f"lens), `correct()` the issue entity so `gate_status.{lens.lens}` "
                "→ `\"signed_off\"`. MERGE the existing map: preserve every other "
                "gate's value exactly (never downgrade another gate), change only "
                f"your own `{lens.lens}` key. Use idempotency_key "
                f"`gate-signoff-{lens.lens}-{t.number}`.\n"
                "  3. If your verdict has any blocking finding, LEAVE the gate "
                "`pending` (do not sign off) — the block stands until the author "
                "resolves it and you re-review.\n"
                "Do this even though this is a PR-panel review, not the issue "
                "pipeline: the sign-off only counts once it is in `gate_status`."
            )
        _panelist_role = f"{lens.lens} lens panelist"
        if is_provisioned(lens.agent):
            comment_identity_block = (
                f"Post your review as a PR comment using the gh CLI. The comment "
                f"MUST begin with the line `review:{lens.lens}`. "
                + _agent_prompt_instruction(lens.agent, _panelist_role)
                + " Repeat the full review text in your reply here (the "
                "dispatcher parses it and posts the comment for you if your gh "
                "call fails)."
            )
        else:
            comment_identity_block = (
                f"Post your review as a PR comment using the gh CLI. The comment "
                f"MUST begin with the line `review:{lens.lens}` followed by "
                f"`{attribution_header(lens.agent, _panelist_role)}` "
                "so readers can tell which agent authored it (the GitHub account "
                "is shared). Repeat the full review text in your reply here (the "
                "dispatcher parses it and posts the comment for you if your gh "
                "call fails)."
            )
        return (
            f"Invoke the {lens.agent} agent per your appended system prompt.\n\n"
            f"You are a review panelist on PR {t.repository}#{t.number}: "
            f"{t.title}\n{t.html_url}\n\n"
            f"Review ONLY through your `{lens.lens}` lens: {lens.checks}\n"
            + (
                # The security lens is the one lens that MUST NOT defer to the
                # CI baseline. The generic line below tells every other lens
                # that correctness/security is already covered — pointing that
                # at the security lens would instruct it to skip the work it
                # exists to do (ateles#425).
                "The CI baseline is NOT a substitute for this lens: it reviews "
                "the diff, and your mandate is explicitly to look OUTSIDE the "
                "diff for the sinks it did not touch. Do the full adversarial "
                "pass.\n\n"
                if lens.lens == "security"
                else "Do not run a generic full-file review — the Claude GHA "
                "already covers correctness/security as the baseline.\n\n"
            )
            + f"{expectation_block}\n\n"
            f"{comment_identity_block}\n\n"
            f"{blocking_rules}"
            f"{checkoff_block}"
            f"{gate_writeback_block}"
        )

    @staticmethod
    def _vanellus_prompt(
        t: SwarmTrigger,
        parent: int | None,
        lenses: list[str],
        reviews: list[tuple[str, str]] | None = None,
        auto_merge: bool = False,
        pending_gates: set[str] | None = None,
    ) -> str:
        # The captured lens reviews are embedded INLINE below so the aggregator
        # never has to re-fetch them via `gh`. Vanellus runs diff-only
        # (cwd=None) with no git-repo context, so a `gh pr view --comments` read
        # fails with "requires being in a repository context" — which is exactly
        # how aggregations silently stalled (the dispatcher then posts Vanellus's
        # non-verdict "please give me the data" message via its fallback).
        if reviews:
            panel_block = "\n\n".join(
                f"### review:{lens}\n{(text or '').strip()}" for lens, text in reviews
            )
        else:
            panel_block = "(no panel lens reviews captured — GHA baseline only)"
        return (
            "Invoke the vanellus agent per your appended system prompt.\n\n"
            f"Aggregate the review panel for PR {t.repository}#{t.number}: "
            f"{t.title}\n{t.html_url}\n"
            f"Parent issue: #{parent if parent else 'unknown'}. "
            f"Panel lenses that reviewed: {', '.join(lenses) or '(none — GHA baseline only)'}.\n\n"
            "The panel lens reviews are provided INLINE below — aggregate them "
            "directly. Do NOT fetch them via gh; you run without a repo checkout "
            "and that read fails. (Optionally read the Claude GHA baseline with "
            f"`gh pr view {t.number} --repo {t.repository} --comments`, but the "
            "inline reviews are authoritative.) Any [BLOCKING] finding ⇒ set the "
            "pr_review gate to changes_requested and route back to Gryllus with "
            "a summary comment. All clear ⇒ approve and advance pr_review to "
            "signed_off on the parent issue entity.\n\n"
            "----- PANEL LENS REVIEWS (inline) -----\n"
            f"{panel_block}\n"
            "----- END PANEL LENS REVIEWS -----\n\n"
            f"{_agent_prompt_instruction('vanellus', 'PR steward')}\n\n"
            "POST YOUR AGGREGATED VERDICT AS A PR COMMENT using the gh CLI "
            f"(`gh pr comment {t.number} --repo {t.repository} --body ...`). "
            f"The comment MUST begin with the line `{_VANELLUS_COMMENT_MARKER}` "
            "so the dispatcher can detect whether it landed. "
            "Repeat the full aggregated verdict text in your reply here (the "
            "dispatcher parses it and posts the comment for you if your gh "
            "call fails).\n\n"
            + merge_authorization_clause(
                auto_merge=auto_merge, pending_gates=pending_gates
            )
        )

    # ── GitHub helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _parent_issue_number(pr_body: str) -> int | None:
        m = _PARENT_ISSUE.search(pr_body or "")
        return int(m.group(1)) if m else None

    def _github_headers(self, repo: str = "") -> dict[str, str]:
        """Return GitHub API request headers with the per-repo token (#95).

        When `repo` is supplied the per-repo PAT is preferred (see
        `_token_for_repo`); the instance-level `config.github_token` is used
        as a final fallback so behaviour is unchanged for callers that do not
        yet pass a repo.
        """
        token = _token_for_repo(repo) if repo else self.config.github_token
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _changed_files(self, t: SwarmTrigger) -> list[str]:
        url = (
            f"https://api.github.com/repos/{t.repository}/pulls/"
            f"{t.number}/files?per_page=100"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, headers=self._github_headers(t.repository))
                resp.raise_for_status()
                return [f["filename"] for f in resp.json()]
        except Exception as exc:
            log.warning(
                f"[{DAEMON_NAME}] changed-files fetch failed for "
                f"{t.repository}#{t.number}: {exc} — panel falls back to "
                "always-on lenses"
            )
            return []

    async def _preregistered_expectations(
        self, repository: str, issue_number: int
    ) -> dict[str, str]:
        """
        Recover which agents pre-registered review expectations on the parent
        issue, from the `review_expectation (<lens>)` marker comments the
        pre-registration pass posts. Returns {agent_name: expectation_text}.
        """
        url = (
            f"https://api.github.com/repos/{repository}/issues/"
            f"{issue_number}/comments?per_page=100"
        )
        marker = re.compile(
            rf"\*\*{EXPECTATION_MARKER} \((?P<lens>[\w-]+)\)\*\* — what "
            r"(?P<agent>\w+) will verify",
            re.I,
        )
        out: dict[str, str] = {}
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(url, headers=self._github_headers(repository))
                resp.raise_for_status()
                for comment in resp.json():
                    m = marker.search(comment.get("body", ""))
                    if m:
                        out[m.group("agent").lower()] = comment.get("body", "")
        except Exception as exc:
            log.warning(
                f"[{DAEMON_NAME}] expectation fetch failed for "
                f"{repository}#{issue_number}: {exc} — panelists review "
                "against standing lens criteria"
            )
        return out

    # ── Neotoma helpers ──────────────────────────────────────────────────────

    async def _store_entities(self, entities: list[dict], idempotency_key: str) -> None:
        if not self.config.neotoma_token:
            log.warning(f"[{DAEMON_NAME}] NEOTOMA_BEARER_TOKEN unset — store skipped")
            return
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{self.config.neotoma_base_url}/store",
                    json={"entities": entities, "idempotency_key": idempotency_key},
                    headers={
                        "Authorization": f"Bearer {self.config.neotoma_token}"
                    },
                )
                resp.raise_for_status()
        except Exception as exc:
            log.error(f"[{DAEMON_NAME}] Neotoma store failed ({idempotency_key}): {exc}")

    async def _log_harness_event(self, t: SwarmTrigger) -> None:
        entities = [
            {
                "entity_type": "harness_event",
                "event_type": f"github.{t.kind}",
                "handler": DAEMON_NAME,
                "subject_ref": f"{t.repository}#{t.number}",
                "summary": t.title[:200],
                "delivery_id": t.delivery_id,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
            }
        ]
        await self._store_entities(
            entities,
            idempotency_key=(
                f"harness-event-{t.kind}-{t.repository}-{t.number}-"
                f"{t.delivery_id}-{content_digest(entities)}"
            ),
        )

    async def _persist_panel_reviews(
        self,
        t: SwarmTrigger,
        reviews: list[tuple[str, str]],
        agents_by_lens: dict[str, str] | None = None,
    ) -> None:
        """Store each captured panel review as a harness_event so the review
        text survives even when the panelist could not post its PR comment."""
        agents_by_lens = agents_by_lens or {}
        entities = [
            {
                "entity_type": "harness_event",
                "event_type": "github.panel_review",
                "handler": DAEMON_NAME,
                "agent": agents_by_lens.get(lens, ""),
                "subject_ref": f"{t.repository}#{t.number}",
                "summary": f"panel review ({lens}) for {t.repository}#{t.number}",
                "delivery_id": t.delivery_id,
                "lens": lens,
                "content": text,
                "occurred_at": datetime.now(timezone.utc).isoformat(),
            }
            for lens, text in reviews
        ]
        await self._store_entities(
            entities,
            idempotency_key=(
                f"panel-reviews-{t.repository}-{t.number}-"
                f"{t.delivery_id}-{content_digest(entities)}"
            ),
        )

    async def _post_missing_panel_comments(
        self,
        t: SwarmTrigger,
        reviews: list[tuple[str, str]],
        agents_by_lens: dict[str, str] | None = None,
    ) -> None:
        """Backfill `review:<lens>` PR comments the panelists failed to post.

        Headless `claude --print` panelists cannot answer permission prompts,
        so their `gh` comment step often fails silently; Vanellus aggregates
        from the PR comments, so the dispatcher posts the captured review
        itself when the comment is missing. Never raises."""
        repo_token = _token_for_repo(t.repository)
        if not repo_token:
            log.warning(
                f"[{DAEMON_NAME}] no GitHub token — fallback review comments "
                f"skipped for {t.repository}#{t.number}"
            )
            return
        url = (
            f"https://api.github.com/repos/{t.repository}/issues/"
            f"{t.number}/comments"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    url, params={"per_page": 100}, headers=self._github_headers(t.repository)
                )
                resp.raise_for_status()
                bodies = [c.get("body", "") for c in resp.json()]
                captured = dict(reviews)
                agents = agents_by_lens or {}
                for lens in lenses_missing_comments(bodies, list(captured)):
                    body = compose_fallback_comment(
                        lens, agents.get(lens, "unknown panelist"), captured[lens]
                    )
                    post = await client.post(
                        url, json={"body": body}, headers=self._github_headers(t.repository)
                    )
                    post.raise_for_status()
                    log.info(
                        f"[{DAEMON_NAME}] posted fallback review:{lens} "
                        f"comment on {t.repository}#{t.number}"
                    )
        except Exception as exc:
            log.error(
                f"[{DAEMON_NAME}] fallback review comments failed for "
                f"{t.repository}#{t.number}: {exc}"
            )

    async def _handle_panel_auth_failure(self, t: SwarmTrigger, agent: str) -> None:
        """A panel agent's Claude call failed auth — surface it as infra, page operator.

        Posts a reframed PR comment (so the 401 isn't mistaken for a verdict) and
        sends a BLOCKER notification with the re-auth remediation. Idempotent on
        the comment via the Vanellus marker; best-effort, never raises.
        """
        log.error(
            f"[{DAEMON_NAME}] panel agent '{agent}' auth failure on "
            f"{t.repository}#{t.number} — Claude credential expired/invalid"
        )
        # Page the operator with the actionable fix.
        try:
            self.notifier.send(
                f"⚠️ Swarm review degraded: the {agent} panel agent can't "
                f"authenticate to Anthropic (401) on {t.repository}#{t.number}. "
                "PRs aren't being review-aggregated. Fix: set a long-lived "
                "ANTHROPIC_API_KEY in the Apis daemon env (com.ateles.apis plist "
                "or ateles-private/.env) and reload com.ateles.apis.",
                priority=Priority.BLOCKER,
                handler=DAEMON_NAME,
            )
        except Exception as exc:  # notifier must never crash the pipeline
            log.error(f"[{DAEMON_NAME}] re-auth notification failed: {exc}")
        # Post the reframed comment (dedup on the marker), best-effort.
        repo_token = _token_for_repo(t.repository)
        if not repo_token:
            return
        url = (
            f"https://api.github.com/repos/{t.repository}/issues/"
            f"{t.number}/comments"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    url, params={"per_page": 100},
                    headers=self._github_headers(t.repository),
                )
                resp.raise_for_status()
                if not vanellus_comment_missing([c.get("body", "") for c in resp.json()]):
                    return  # an aggregation comment already landed
                post = await client.post(
                    url, json={"body": compose_auth_failure_comment(agent)},
                    headers=self._github_headers(t.repository),
                )
                post.raise_for_status()
                log.info(
                    f"[{DAEMON_NAME}] posted auth-failure notice on "
                    f"{t.repository}#{t.number}"
                )
        except Exception as exc:
            log.error(
                f"[{DAEMON_NAME}] auth-failure comment failed for "
                f"{t.repository}#{t.number}: {exc}"
            )

    # Durable marker: a PR whose review was deferred by a usage limit. Carries
    # the ISO resume time so the sweep can re-dispatch it once the limit clears.
    # Head-SHA-agnostic and restart-surviving, same substrate as the fix-round
    # counter. Distinct from _VANELLUS_COMMENT_MARKER so a deferral is NEVER
    # mistaken for an aggregation that ran.
    _REVIEW_DEFERRED_MARKER = "<!-- review-deferred-until:{iso} -->"
    _REVIEW_DEFERRED_RE = re.compile(r"<!-- review-deferred-until:([^ ]+) -->")

    async def _handle_panel_session_limit(
        self,
        t: SwarmTrigger,
        parent: int | None,
        agent: str,
        stdout: str,
        stderr: str,
    ) -> None:
        """A panel/aggregation call was throttled by a self-clearing usage limit.

        Unlike an auth failure (page + wait for a human), a usage limit resets on
        its own — so this posts a TRUTHFUL "review incomplete, will resume" marker
        (never a stub that reads like a verdict) with the resume time, and lets
        the resume sweep re-dispatch after the reset. Best-effort; never raises.
        """
        delay = parse_limit_reset_delay(stdout, stderr)
        resume_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
        iso = resume_at.strftime("%Y-%m-%dT%H:%M:%SZ")
        log.warning(
            f"[{DAEMON_NAME}] {agent} panel on {t.repository}#{t.number} hit a "
            f"usage limit; deferring review resume until {iso} (~{delay}s)"
        )
        # Notify at INFO (always-digest), not BLOCKER: nothing for the operator
        # to fix — it self-resolves. (An operator who wants it sooner can top
        # up / wait.)
        try:
            self.notifier.send(
                f"⏳ Swarm review on {t.repository}#{t.number} paused on a usage "
                f"limit; auto-resumes at {iso}. No action needed.",
                priority=Priority.INFO,
                handler=DAEMON_NAME,
            )
        except Exception as exc:
            log.error(f"[{DAEMON_NAME}] session-limit notice failed: {exc}", exc_info=True)

        repo_token = _token_for_repo(t.repository)
        if not repo_token:
            return
        body = (
            f"{self._REVIEW_DEFERRED_MARKER.format(iso=iso)}\n"
            f"{attribution_header('apis', 'swarm dispatcher')}\n\n"
            "⏳ **Review incomplete — panel throttled by a usage limit.** The "
            f"`{agent}` aggregation could not run because the model call hit a "
            "session/usage limit. This is **not** a verdict — the PR has not been "
            f"assessed. The review will resume automatically after the limit "
            f"resets (scheduled ~`{iso}`); no operator action is required.\n\n"
            "_Posted by the Apis dispatcher — distinct from a `vanellus-"
            "aggregation` verdict on purpose, so a throttled review is never "
            "mistaken for a completed one._"
        )
        url = f"https://api.github.com/repos/{t.repository}/issues/{t.number}/comments"
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                # ADVANCE, don't skip: a still-limited re-run must move the reset
                # time forward, or the sweep would keep re-dispatching every pass
                # (Loxia #264 review). Delete any prior deferral markers, then
                # post the fresh one carrying the NEW reset ISO. Deleting first
                # also keeps the "latest live marker" invariant the sweep relies
                # on unambiguous.
                resp = await client.get(
                    url, params={"per_page": 100},
                    headers=self._github_headers(t.repository),
                )
                resp.raise_for_status()
                for c in resp.json():
                    if self._REVIEW_DEFERRED_RE.search(c.get("body", "")):
                        del_url = (
                            f"https://api.github.com/repos/{t.repository}/issues/"
                            f"comments/{c.get('id')}"
                        )
                        try:
                            d = await client.delete(
                                del_url, headers=self._github_headers(t.repository)
                            )
                            d.raise_for_status()
                        except Exception as exc:
                            log.warning(
                                f"[{DAEMON_NAME}] could not delete stale deferral "
                                f"marker {c.get('id')} on {t.repository}#{t.number} "
                                f"({exc}); posting fresh marker anyway"
                            )
                post = await client.post(
                    url, json={"body": body},
                    headers=self._github_headers(t.repository),
                )
                post.raise_for_status()
                log.info(
                    f"[{DAEMON_NAME}] posted review-deferred marker on "
                    f"{t.repository}#{t.number} (resume {iso})"
                )
        except Exception as exc:
            log.error(
                f"[{DAEMON_NAME}] deferral comment failed for "
                f"{t.repository}#{t.number}: {exc}"
            )

    async def _post_missing_vanellus_comment(
        self,
        t: SwarmTrigger,
        result: SkillResult,
    ) -> None:
        """Post Vanellus's aggregated verdict as a PR comment when its own gh
        call failed to land it (mirrors _post_missing_panel_comments for the
        aggregation step, ateles#127).

        Checks whether the PR already has a Vanellus aggregation comment (by
        looking for ``_VANELLUS_COMMENT_MARKER`` in existing comment bodies).
        If missing AND the skill run produced stdout, the dispatcher posts the
        captured text as a fallback comment.  Best-effort — never raises.

        Dedup: if Vanellus's own comment DID land (marker present), this
        method returns without posting, so no duplicate is ever created.
        """
        if not result.stdout:
            log.debug(
                f"[{DAEMON_NAME}] Vanellus produced no stdout for "
                f"{t.repository}#{t.number} — fallback skipped"
            )
            return

        repo_token = _token_for_repo(t.repository)
        if not repo_token:
            log.warning(
                f"[{DAEMON_NAME}] no GitHub token — Vanellus fallback comment "
                f"skipped for {t.repository}#{t.number}"
            )
            return

        url = (
            f"https://api.github.com/repos/{t.repository}/issues/"
            f"{t.number}/comments"
        )
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.get(
                    url,
                    params={"per_page": 100},
                    headers=self._github_headers(t.repository),
                )
                resp.raise_for_status()
                bodies = [c.get("body", "") for c in resp.json()]
                if not vanellus_comment_missing(bodies):
                    log.debug(
                        f"[{DAEMON_NAME}] Vanellus aggregation comment already "
                        f"present on {t.repository}#{t.number} — no fallback needed"
                    )
                    return
                body = compose_vanellus_fallback_comment(result.stdout)
                post = await client.post(
                    url,
                    json={"body": body},
                    headers=self._github_headers(t.repository),
                )
                post.raise_for_status()
                log.info(
                    f"[{DAEMON_NAME}] posted fallback Vanellus aggregation "
                    f"comment on {t.repository}#{t.number}"
                )
        except Exception as exc:
            log.error(
                f"[{DAEMON_NAME}] Vanellus fallback comment failed for "
                f"{t.repository}#{t.number}: {exc}"
            )

    async def _store_merge_checkpoint(
        self, t: SwarmTrigger, parent: int | None, lenses: list[str]
    ) -> None:
        """Blocking checkpoint at the pr_review→merge boundary (ateles#80).

        Phase H1 note: this checkpoint is the pre_merge boundary defined in
        docs/swarm_hitl_checkpoints_design.md.  The release path (no-deadlock
        requirement per the design) is the /approve operator command handled by
        _handle_approve — it resolves the checkpoint_brief and hands back to
        Vanellus.  /reject stops the held action; /hold parks without resolving.

        After filing the checkpoint_brief, the operator is also requested as a
        PR reviewer (best-effort, non-fatal) so the pending merge surfaces in
        their "Review requested" GitHub queue.  Assignment is SURFACING;
        checkpoint_brief + block is ENFORCEMENT.
        """
        entities = [
            {
                "entity_type": "checkpoint_brief",
                "title": f"Merge approval: {t.repository}#{t.number}",
                "checkpoint_kind": "pr_merge",
                "blocking": True,
                "subject_ref": f"{t.repository}#{t.number}",
                "parent_issue": parent,
                "body": (
                    f"PR {t.html_url} has completed panel review "
                    f"(lenses: {', '.join(lenses) or 'GHA baseline only'}). "
                    "Vanellus aggregated verdicts but merge is operator-"
                    "gated (APIS_AUTONOMY_AUTO_MERGE=0). "
                    f"Release this checkpoint with {_APPROVE_CMD} (resolves + "
                    f"hands back to Vanellus) or {_REJECT_CMD} <reason> (stops). "
                    f"{_HOLD_CMD} parks without resolving."
                ),
                "status": "open",
            }
        ]
        await self._store_entities(
            entities,
            idempotency_key=(
                f"merge-checkpoint-{t.repository}-{t.number}-{t.delivery_id}-"
                f"{content_digest(entities)}"
            ),
        )

        # Request the operator as PR reviewer so the pending merge surfaces in
        # their "Review requested" queue (Phase H1, design §"Native GitHub operator
        # assignment").  Best-effort: if this fails the checkpoint still blocks.
        await self._request_operator_reviewer(t)
