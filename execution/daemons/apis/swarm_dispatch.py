"""
execution/daemons/apis/swarm_dispatch.py — GitHub-event dispatch pipelines.

The orchestration layer behind the GitHub webhook gateway (ateles#80):

  issue.opened     → harness_event → Lanius (new-issue protocol: init gates,
                     assign Pavo, label) → review-expectation pre-registration
                     pass (ateles#81) → Pavo (Phase 1 scoping)
  pull_request.*   → harness_event → Lanius (PR gate inheritance; stop if a
                     pre-impl gate is open) → review panel (neotoma#1640) →
                     learning pass (ateles#82) → Vanellus aggregation →
                     blocking checkpoint_brief before merge (autonomy
                     guardrail)

Autonomy guardrail (ateles#80): read-only gates run unattended — they only
write Neotoma metadata and GitHub comments, both reversible. Side-effecting
steps stay operator-gated by default: Vanellus reviews and aggregates but
does NOT merge unless APIS_AUTONOMY_AUTO_MERGE=1; the merge boundary gets a
blocking checkpoint_brief plus an operator_decision notification instead.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from github_gateway import SwarmTrigger
from review_learning import propose_skill_updates
from review_panel import Lens, select_expectation_agents, select_panel
from skill_runner import SkillResult, run_skill

from lib.notify import Notifier, Priority

log = logging.getLogger("apis.swarm_dispatch")

DAEMON_NAME = "apis"

_PARENT_ISSUE = re.compile(r"\b(?:closes|fixes|resolves)\s+#(\d+)", re.I)
_GATE_VERDICT = re.compile(r"GATE_INHERITANCE:\s*(clear|blocked)", re.I)

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

# Pre-impl gates that must be signed off before the PR review panel runs.
# These are the gates Lanius checks for GATE_INHERITANCE.
PRE_IMPL_GATES = ("pm", "arch")

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


def parse_gate_verdict(stdout: str) -> str | None:
    """Extract Lanius's GATE_INHERITANCE verdict; None when absent."""
    m = _GATE_VERDICT.search(stdout or "")
    return m.group(1).lower() if m else None


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


class SwarmDispatcher:
    """Routes normalized GitHub triggers into agent pipelines."""

    def __init__(self, notifier: Notifier, config: DispatchConfig | None = None):
        self.notifier = notifier
        self.config = config or DispatchConfig()

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

    # ── issue.opened pipeline ────────────────────────────────────────────────

    async def _handle_issue_opened(self, trigger: SwarmTrigger) -> None:
        ref = f"{trigger.repository}#{trigger.number}"

        # 1. Lanius: new-issue protocol (init gate_status, assign Pavo, label).
        lanius = await run_skill(
            "lanius",
            self._lanius_issue_prompt(trigger),
            github_token=_token_for_agent_on_repo("lanius", trigger.repository),
            include_github_contract=True,
        )
        if not lanius.ok:
            self.notifier.send(
                f"Lanius failed on new issue {ref} — gate init incomplete",
                priority=Priority.BLOCKER,
                handler=DAEMON_NAME,
            )
            # Continue: expectations are still useful even if gate init failed.

        # 2. Shift-left review contract (ateles#81): relevant agents
        #    pre-register what they will check at PR time.
        lenses = select_expectation_agents(trigger.title, trigger.body, trigger.labels)
        for lens in lenses:
            await run_skill(
                lens.agent,
                self._expectation_prompt(trigger, lens),
                github_token=_token_for_agent_on_repo(lens.agent, trigger.repository),
                include_github_contract=True,
            )

        # 3. Pavo takes Phase 1 (pm scoping) — read-only gate, auto-runs.
        await run_skill(
            "pavo",
            self._pavo_prompt(trigger),
            github_token=_token_for_agent_on_repo("pavo", trigger.repository),
            include_github_contract=True,
        )

        self.notifier.send(
            f"Issue {ref} triaged autonomously: Lanius"
            f"{'✓' if lanius.ok else '✗'}, {len(lenses)} review expectation(s) "
            f"pre-registered, Pavo scoping started",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )

    # ── pull_request pipeline ────────────────────────────────────────────────

    async def _handle_pr(self, trigger: SwarmTrigger) -> None:
        ref = f"{trigger.repository}#{trigger.number}"
        parent = self._parent_issue_number(trigger.body)

        # 1. Lanius: enforce PR gate inheritance against the parent issue.
        _lanius_token = _token_for_agent_on_repo("lanius", trigger.repository)
        lanius = await run_skill(
            "lanius",
            self._lanius_pr_prompt(trigger, parent),
            github_token=_lanius_token,
            include_github_contract=True,
        )
        verdict = parse_gate_verdict(lanius.stdout)
        if verdict is None and lanius.ok:
            # PR-87 self-dogfood finding: Lanius sometimes replies without the
            # mandatory verdict line. One sharper retry before failing open.
            log.warning(
                f"[{DAEMON_NAME}] {ref}: Lanius omitted the verdict line — "
                "retrying once with an explicit reminder"
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
            )
            verdict = parse_gate_verdict(lanius.stdout)
        if verdict == "blocked":
            log.info(f"[{DAEMON_NAME}] {ref}: pre-impl gates open — panel skipped")
            self.notifier.send(
                f"PR {ref} blocked by Lanius — pre-impl gates not signed off",
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
        )

        reviews: list[tuple[str, str]] = []
        for lens in panel:
            result = await run_skill(
                lens.agent,
                self._panelist_prompt(
                    trigger, lens, expectations.get(lens.agent, ""), parent
                ),
                github_token=_token_for_agent_on_repo(lens.agent, trigger.repository),
                include_github_contract=True,
            )
            if result.ok:
                reviews.append((lens.lens, result.stdout))

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
        await run_skill(
            "vanellus",
            self._vanellus_prompt(trigger, parent, [p.lens for p in panel]),
            github_token=_token_for_agent_on_repo("vanellus", trigger.repository),
            include_github_contract=True,
        )

        if not self.config.auto_merge:
            await self._store_merge_checkpoint(trigger, parent, [p.lens for p in panel])
            self.notifier.send(
                f"PR {ref} reviewed by panel "
                f"({', '.join(p.lens for p in panel) or 'baseline only'}). "
                "Merge held for operator approval (checkpoint_brief filed).",
                priority=Priority.OPERATOR_DECISION,
                handler=DAEMON_NAME,
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

        # Delegate gate-waiving to Lanius (it owns gate_status mutations).
        # Lanius will correct each unsigned pre-impl gate to "waived", record
        # the waive in owner_history, and advance current_owner.
        await self._lanius_waive_gates(trigger)

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

    async def _handle_approve(self, trigger: SwarmTrigger) -> None:
        """Execute the /approve operator command (Phase H1 HITL checkpoint).

        Approves the pending pre-merge checkpoint on this PR/issue.

        Conservative merge choice: the dispatcher does NOT auto-merge the PR.
        Performing the merge programmatically (gh pr merge) is a side-effecting,
        hard-to-reverse action.  The checkpoint_brief was filed specifically
        because APIS_AUTONOMY_AUTO_MERGE=0 — the operator's intent is to control
        the merge gate themselves.  Auto-merging on /approve would bypass that
        intent.  Instead, the dispatcher:
          1. Resolves/closes the checkpoint_brief entity (approved).
          2. Posts a GitHub confirmation comment (without command tokens).
          3. Removes the operator's review request (un-assign on resolve rule).
          4. Re-assigns Vanellus as PR steward and hands back via notification.
        The operator then merges directly on GitHub or signals Vanellus.

        The release path for the merge checkpoint is this command.  Without
        /approve being wired, the checkpoint would be a deadlock.  See
        docs/swarm_hitl_checkpoints_design.md §"No-deadlock self-consistency".
        """
        ref = f"{trigger.repository}#{trigger.number}"
        log.info(
            f"[{DAEMON_NAME}] operator {_APPROVE_CMD} received on {ref} "
            f"(comment #{trigger.comment_id})"
        )

        # 1. Resolve the checkpoint_brief: file an approved resolution.
        #    We store a new checkpoint_brief entity with status=approved so the
        #    Neotoma trail is clear.  (The original open brief stays; resolution
        #    is an additive correction pattern.)
        await self._store_checkpoint_resolution(trigger, verdict="approved", reason="")

        # 2. Post a GitHub confirmation comment.  Body deliberately contains no
        #    command tokens so it cannot re-trigger the handler (neotoma#1686).
        await self._post_checkpoint_verdict_comment(
            trigger,
            body=(
                f"<!-- h1-checkpoint-approve -->\n"
                f"{attribution_header('apis', 'swarm dispatcher')}\n\n"
                f"Operator **approved** the pre-merge checkpoint on {ref}. "
                "Checkpoint resolved. The PR is ready to merge — proceed on "
                "GitHub or instruct Vanellus. Vanellus retains PR stewardship."
            ),
        )

        # 3. Remove operator from reviewer list (best-effort, non-fatal).
        await self._remove_operator_reviewer(trigger)

        # 4. Notify Vanellus handback.
        self.notifier.send(
            f"Operator approved merge checkpoint on {ref} — checkpoint resolved, "
            f"operator reviewer removed. PR ready to merge; Vanellus is PR steward.",
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

    async def _lanius_waive_gates(self, trigger: SwarmTrigger) -> None:
        """Ask Lanius to waive all unsigned pre-impl gates on the issue entity.

        Mirrors the gate-waive mechanics in the Lanius SKILL.md (lines 48-52):
        correct gate_status.<gate> → "waived", append to owner_history, advance
        current_owner to the next phase.  Best-effort: logs on failure but does
        not raise.
        """
        ref = f"{trigger.repository}#{trigger.number}"
        issue_number = trigger.number
        # comment_on_pr means trigger.number IS the PR number; the parent issue
        # number is in the PR body.  Extract it.
        if trigger.comment_on_pr:
            issue_number = self._parent_issue_number(trigger.body) or trigger.number

        prompt = (
            "Invoke the lanius agent per your appended system prompt.\n\n"
            f"The operator has issued `{_CONFIRM_GATES_CLEAR_CMD}` on "
            f"{trigger.repository}#{trigger.number} "
            f"({trigger.comment_html_url or trigger.html_url}).\n\n"
            f"Parent issue (where gates live): #{issue_number} in "
            f"{trigger.repository}.\n\n"
            "ACTION REQUIRED — operator override, execute immediately:\n"
            f"For each gate in {list(PRE_IMPL_GATES)} that is currently "
            "`pending` or `blocked` on the parent issue entity (not already "
            "`signed_off` or `waived`), do ALL of the following:\n"
            "  1. `correct()` the issue entity: set `gate_status.<gate>` → "
            "`\"waived\"`.\n"
            "  2. Append to `owner_history`: "
            '`{"gate": "<gate>", "action": "waived", '
            '"actor": "operator", "reason": "operator /confirm-gates-clear '
            'override", "timestamp": "<now>"}`.\n'
            "  3. After waiving all unsigned gates, set `current_owner` to "
            "the next phase (e.g. `pr_review` if pm and arch are now done).\n"
            "  4. Post ONE GitHub comment on the PR (or issue) confirming which "
            "gates were waived and that the review pipeline will now proceed.\n\n"
            "Do NOT waive gates that are already `signed_off` or `waived`.\n"
            "If ALL gates are already signed_off/waived, post a comment saying "
            "the pipeline is already clear.\n\n"
            f"{_agent_prompt_instruction('lanius', 'gate admin')}"
        )
        result = await run_skill(
            "lanius",
            prompt,
            github_token=_token_for_agent_on_repo("lanius", trigger.repository),
            include_github_contract=True,
        )
        if not result.ok:
            log.error(
                f"[{DAEMON_NAME}] Lanius gate-waive failed on {ref}: "
                f"{result.error or f'rc={result.returncode}'}"
            )
        else:
            log.info(f"[{DAEMON_NAME}] Lanius gate-waive completed for {ref}")

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
            "LEGACY-ISSUE RULE: distinguish 'gates never initialized' from "
            "'gates evaluated and still pending'. If the parent issue predates "
            "the gate pipeline — i.e. it has NO gate_status / current_owner "
            "metadata at all — do NOT hard-block. Initialize the gates "
            "retroactively, note the legacy status in your comment, and emit "
            "`GATE_INHERITANCE: clear` so review proceeds (merge stays "
            "operator-gated regardless, per the pipeline's fail-open-for-"
            "review guardrail). Only emit `blocked` when gates exist and a "
            "pre-impl gate is genuinely unsigned. To run the full issue "
            "pipeline on a legacy issue (gate init + expectations + Pavo), the "
            "operator can backfill via `trigger_swarm_pr.py issue <n>`.\n\n"
            "BLOCKED COMMENT REQUIREMENTS (ateles#112): when you post a "
            "blocking comment, it MUST include:\n"
            "  1. A list of WHICH pre-impl gates are unsigned and who owns "
            "each (e.g. `pm` owned by Pavo, `arch` owned by Waxwing).\n"
            "  2. The exact operator-override command: "
            f"`/confirm-gates-clear` — only @{operator_login} may issue this "
            "command; it waives all unsigned pre-impl gates and re-triggers "
            "the PR pipeline. No other commenter can clear gates.\n"
            "  3. The normal resolution path: the gate owner can sign off the "
            "gate via Neotoma (set gate_status.<gate> → signed_off) or the "
            "operator can waive it.\n\n"
            f"{_agent_prompt_instruction('lanius', 'PR gate inheritance')}\n\n"
            "End your reply with exactly one line: `GATE_INHERITANCE: clear` "
            "or `GATE_INHERITANCE: blocked` so the dispatcher can route."
        )

    @staticmethod
    def _panelist_prompt(
        t: SwarmTrigger, lens: Lens, expectation: str, parent: int | None = None
    ) -> str:
        expectation_block = (
            "Your pre-registered expectations on the parent issue were:\n"
            f"{expectation}\n\nReview against them first: did the change meet "
            "what you said you would check?"
            if expectation
            else "You did not pre-register expectations for this issue; review "
            "against your standing lens criteria."
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
            "applies — that marks the finding as systemic."
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
            "Do not run a generic full-file review — the Claude GHA already "
            "covers correctness/security as the baseline.\n\n"
            f"{expectation_block}\n\n"
            f"{comment_identity_block}\n\n"
            f"{blocking_rules}"
            f"{checkoff_block}"
        )

    @staticmethod
    def _vanellus_prompt(t: SwarmTrigger, parent: int | None, lenses: list[str]) -> str:
        return (
            "Invoke the vanellus agent per your appended system prompt.\n\n"
            f"Aggregate the review panel for PR {t.repository}#{t.number}: "
            f"{t.title}\n{t.html_url}\n"
            f"Parent issue: #{parent if parent else 'unknown'}. "
            f"Panel lenses that reviewed: {', '.join(lenses) or '(none — GHA baseline only)'}.\n\n"
            "Collect the `review:<lens>` comments on the PR plus the Claude "
            "GHA baseline review. Any [BLOCKING] finding ⇒ set the pr_review "
            "gate to changes_requested and route back to Gryllus with a "
            "summary comment. All clear ⇒ approve and advance pr_review to "
            "signed_off on the parent issue entity.\n\n"
            f"{_agent_prompt_instruction('vanellus', 'PR steward')}\n\n"
            "AUTONOMY GUARDRAIL — DO NOT MERGE. Merge is operator-gated: a "
            "blocking checkpoint_brief is filed at the merge boundary; the "
            "operator merges or instructs you to. This overrides any merge "
            "instruction in your standing protocol."
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
