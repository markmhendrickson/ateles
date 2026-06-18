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


def attribution_header(agent: str, role: str) -> str:
    """First line of every swarm-authored GitHub comment.

    PR-87 dogfood feedback: comments posted through a shared GitHub identity
    (or the operator's keyring) are indistinguishable from the operator
    without an in-body attribution line. Until per-repo machine identities
    are fully wired, this line IS the agent identity on GitHub."""
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
        lanius = await run_skill("lanius", self._lanius_issue_prompt(trigger))
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
            await run_skill(lens.agent, self._expectation_prompt(trigger, lens))

        # 3. Pavo takes Phase 1 (pm scoping) — read-only gate, auto-runs.
        await run_skill("pavo", self._pavo_prompt(trigger))

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
        lanius = await run_skill("lanius", self._lanius_pr_prompt(trigger, parent))
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
            "vanellus", self._vanellus_prompt(trigger, parent, [p.lens for p in panel])
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

    # ── prompts ──────────────────────────────────────────────────────────────

    @staticmethod
    def _lanius_issue_prompt(t: SwarmTrigger) -> str:
        return (
            "Invoke the lanius agent per your appended system prompt.\n\n"
            f"A new GitHub issue was opened (webhook trigger, no operator in "
            f"the loop): {t.repository}#{t.number} — {t.title}\n{t.html_url}\n\n"
            f"{t.body}\n\n"
            "Run your new-issue protocol: load the workflow_definition, "
            "initialize gate_status/current_owner/owner_history on the issue "
            "entity, assign Pavo as Phase 1 owner, post the triage comment, "
            "and apply the lanius-triage label.\n\n"
            f"Begin any GitHub comment you post with "
            f"`{attribution_header('lanius', 'issue triage')}` — the GitHub "
            "account is shared, so the comment body is your identity."
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
            "criteria, and scope; record your gate plan_contribution; advance "
            "or block the pm gate on the issue entity. Read any "
            f"`{EXPECTATION_MARKER}` comments on the issue first — they are "
            "the review contract for this issue.\n\n"
            f"Begin any GitHub comment you post with "
            f"`{attribution_header('pavo', 'pm gate owner')}` — the GitHub "
            "account is shared, so the comment body is your identity."
        )

    @staticmethod
    def _lanius_pr_prompt(t: SwarmTrigger, parent: int | None) -> str:
        parent_line = (
            f"The PR body references parent issue #{parent}."
            if parent
            else "No parent issue reference found in the PR body — find it "
            "yourself or treat gate inheritance as blocked."
        )
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
            f"Begin any GitHub comment you post with "
            f"`{attribution_header('lanius', 'PR gate inheritance')}` — the "
            "GitHub account is shared, so the comment body is your identity.\n\n"
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
        return (
            f"Invoke the {lens.agent} agent per your appended system prompt.\n\n"
            f"You are a review panelist on PR {t.repository}#{t.number}: "
            f"{t.title}\n{t.html_url}\n\n"
            f"Review ONLY through your `{lens.lens}` lens: {lens.checks}\n"
            "Do not run a generic full-file review — the Claude GHA already "
            "covers correctness/security as the baseline.\n\n"
            f"{expectation_block}\n\n"
            f"Post your review as a PR comment using the gh CLI. The comment "
            f"MUST begin with the line `review:{lens.lens}` followed by "
            f"`{attribution_header(lens.agent, f'{lens.lens} lens panelist')}` "
            "so readers can tell which agent authored it (the GitHub account "
            "is shared). Repeat the full review text in your reply here (the "
            "dispatcher parses it and posts the comment for you if your gh "
            "call fails).\n\n"
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
            f"Begin any GitHub comment you post with "
            f"`{attribution_header('vanellus', 'PR steward')}` — the GitHub "
            "account is shared, so the comment body is your identity.\n\n"
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
        """Blocking checkpoint at the pr_review→merge boundary (ateles#80)."""
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
                    "gated (APIS_AUTONOMY_AUTO_MERGE=0). Approve by "
                    "merging on GitHub or instructing Vanellus to merge; "
                    "reject by closing the PR or routing back to Gryllus."
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
