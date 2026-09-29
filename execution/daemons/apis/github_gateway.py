"""
execution/daemons/apis/github_gateway.py — GitHub webhook → Apis trigger layer.

Implements the missing trigger layer from ateles#80: GitHub fires
`issues.opened` / `pull_request.opened` webhooks at this receiver, which
verifies the HMAC signature, normalizes the payload into a SwarmTrigger, and
hands it to the Apis dispatch pipelines (swarm_dispatch.py) that spawn the
gate agents (Lanius → Pavo on issues; Lanius → review panel → Vanellus on PRs).

Also handles `issue_comment` events (ateles#112): GitHub sends PR comments as
`issue_comment` with an `issue.pull_request` field present. The dispatcher
routes these to the operator-override command handler so `/confirm-gates-clear`
can unblock a deadlocked PR pipeline.

Also handles `pull_request_review` events (approval loop): the operator
clicking "Approve" in the PR review UI submits a review with state "approved".
The dispatcher routes an operator-authored approved review to the shared
approval path so it can (flag-gated) trigger the merge — the same effect as a
`/approve` comment, without leaving GitHub's review surface.

OPERATOR ACTION REQUIRED (ateles#112, approval loop): the live GitHub webhooks
on the markmhendrickson/ateles and markmhendrickson/neotoma repos must be
updated to include the `issue_comment` AND `pull_request_review` events — code
alone cannot do this. Use:
  gh api -X PATCH repos/markmhendrickson/ateles/hooks/<hook_id> \\
    -f "events[]=issues" -f "events[]=pull_request" \\
    -f "events[]=issue_comment" -f "events[]=pull_request_review"
or add them via the repo Settings → Webhooks UI.  Without `issue_comment`,
/confirm-gates-clear and /approve comments never arrive; without
`pull_request_review`, the GitHub "Approve" → merge path never fires.

Apus remains the Neotoma→git mirror webhook daemon only; this gateway is
mounted inside Apis because dispatching swarm work is Apis's job.

Endpoints:
  POST /github/webhook   GitHub webhook receiver (X-Hub-Signature-256 verified)
  GET  /health           liveness probe

Environment variables (read by apis.py and passed in):
  APIS_GITHUB_WEBHOOK_SECRET       HMAC-SHA256 secret configured on the GitHub webhook
  APIS_GITHUB_WEBHOOK_SECRET_NEXT  incoming value during a staged rotation
                                    (rotate_swarm_secret.py) — admitted alongside
                                    the current secret; empty outside a rotation
  APIS_GITHUB_WEBHOOK_PORT     listen port (default: 8742; Apus owns 8741)
  APIS_APPROVE_EMAIL_SECRET       shared secret for /approve-email + /approve-release
  APIS_APPROVE_EMAIL_SECRET_NEXT  incoming value during a staged rotation of the
                                   above; empty outside a rotation
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from aiohttp import web

from lib.daemon_runtime import label_gate as _label_gate

log = logging.getLogger("apis.github_gateway")

# Operator GitHub login used to attribute an email-reply approval. Kept in sync
# with swarm_dispatch._OPERATOR_LOGIN via the same env var (read here to avoid a
# circular import — swarm_dispatch imports SwarmTrigger from this module).
_OPERATOR_LOGIN = os.environ.get("APIS_OPERATOR_LOGIN", "markmhendrickson")

# GitHub actions that fire the PR pipeline. `synchronize` re-runs review on
# new pushes; `reopened` re-enters the pipeline after a close. `labeled` lets a
# label added AFTER the PR opened (e.g. the swarm-canary label, see
# ATELES_SWARM_REQUIRE_LABEL in swarm_dispatch.py) start the pipeline for it —
# without this, a PR opened before the label existed could never enter the
# gated lane.
#
# IMPORTANT: `labeled` is filtered at THIS layer (_labeled_event_admitted,
# below), not left to the dispatcher's label-gate check. Naively adding
# "labeled" to these sets is a known bug this file used to ship (Pavo/Waxwing/
# Loxia review on ateles#1269): parse_github_event would build a real
# pr_labeled/issue_opened trigger for EVERY label add on EVERY issue/PR, and
# handle_trigger's `elif trigger.is_pr:` branch accepts any "pr_*" kind — so
# with the gate UNSET (the documented default, "today's behaviour exactly"),
# adding any label to any PR silently re-ran the full review panel, and any
# label to any issue silently reran the issue pipeline. That is a NEW
# review-trigger, active in the unset/default configuration, that CLAUDE.md's
# documented contract (PR_ACTIONS = {opened, reopened, synchronize} as the
# only events that re-run PR review) never allowed. The label-gate check
# downstream only decided whether the panel *proceeded* once triggered — it
# never neutralized the trigger construction itself for the unset case.
#
# The fix: a `labeled` delivery is admitted into ISSUE_ACTIONS/PR_ACTIONS
# querying below, but parse_github_event calls _labeled_event_admitted BEFORE
# building any trigger for it, and returns None (dropped, DEBUG-logged) unless
# the gate is SET and the label just added is exactly the configured one. This
# keeps the "unset = unchanged" guarantee true at the layer that actually
# decides whether work starts, not just at the layer that decides whether it
# proceeds once started.
PR_ACTIONS = {"opened", "reopened", "synchronize", "labeled"}
# Same rationale for issues: a `labeled` action lets a label added after the
# issue opened start the issue pipeline for it — filtered the same way.
ISSUE_ACTIONS = {"opened", "labeled"}
# issue_comment events (ateles#112): any new comment on an issue or PR.
ISSUE_COMMENT_ACTIONS = {"created"}
# pull_request_review events (approval loop): the operator clicking "Approve"
# in the GitHub PR review UI. Only "submitted" carries a fresh verdict.
PR_REVIEW_ACTIONS = {"submitted"}
# check_suite events (ateles#197): a CI rollup completing. We only act on the
# terminal "completed" action — acting on every in-progress check_run would
# storm the pipeline. `status` events are intentionally NOT handled: they fire
# per-context and would multiply for the same head; check_suite:completed is the
# single terminal signal per commit.
CHECK_SUITE_ACTIONS = {"completed"}
# push events (auto-release): a merge to the default branch is what makes a new
# release preparable. Only pushes to this ref trigger; branch/tag pushes and
# branch deletions are ignored. `push` carries no `action` field.
RELEASE_PUSH_REF = os.environ.get("APIS_RELEASE_PUSH_REF", "refs/heads/main")

# Label gate (bootstrap mode / canary lane). Read here, independently of
# swarm_dispatch.DispatchConfig.require_label, for the same reason
# _OPERATOR_LOGIN is duplicated above: swarm_dispatch imports SwarmTrigger
# from this module, so importing swarm_dispatch back would be circular. Both
# reads go through lib/daemon_runtime/label_gate.required_label (the same
# helper Anthus uses), so an empty/whitespace value reads as "gate inactive"
# identically everywhere.
_REQUIRE_LABEL = _label_gate.required_label()


def _labeled_event_admitted(payload: dict[str, Any], ref: str) -> bool:
    """Decide whether a `labeled` action may build a trigger at all.

    A `labeled` delivery must NEVER reach `_handle_issue_opened` / `_handle_pr`
    unless the label gate is SET and the label just added
    (`payload["label"]["name"]`) is exactly the configured one. This is
    deliberately checked HERE, at the trigger layer, before any SwarmTrigger
    for a `labeled` action is constructed — not left to the dispatcher's
    `_label_gate_allows`, which only decides whether an already-built trigger
    proceeds. The bug this closes (ateles#1269 review round): with the gate
    UNSET, `_label_gate_allows` short-circuits to True, so if a `labeled`
    trigger were built unconditionally here, EVERY label add on EVERY PR
    would silently re-run the full review panel (same for issues and the
    issue pipeline) — a new review-trigger CLAUDE.md's documented contract
    (`PR_ACTIONS = {opened, reopened, synchronize}`) never allowed, active
    even in the default/unset configuration.

    Returns True only when: the gate is set (non-empty `_REQUIRE_LABEL`) AND
    the label GitHub reports as just added on this delivery equals it exactly
    (case-sensitive, matching GitHub's own label-name convention). Every other
    case — gate unset, a different label added, a malformed/missing `label`
    object — returns False and is dropped with a DEBUG line naming why,
    before `parse_github_event` builds anything for it.
    """
    if not _REQUIRE_LABEL:
        log.debug(
            f"[apis] labeled event on {ref} dropped — label gate is unset "
            "(labeled actions only admit a trigger when "
            "ATELES_SWARM_REQUIRE_LABEL is configured)"
        )
        return False
    # A `label` that is not a dict (a string, a list, null) is malformed and
    # denied here, rather than raising AttributeError into the aiohttp handler
    # and surfacing as a 500 (Falco + qa, #1269 round 3). Same for a name that
    # is not a string.
    raw_label = payload.get("label")
    added_label = raw_label.get("name", "") if isinstance(raw_label, dict) else ""
    if not isinstance(added_label, str) or not _label_gate.carries_label(
        _REQUIRE_LABEL, [added_label]
    ):
        log.debug(
            f"[apis] labeled event on {ref} dropped — label added "
            f"({added_label!r}) does not match the configured gate label "
            f"({_REQUIRE_LABEL!r})"
        )
        return False
    return True


@dataclass
class SwarmTrigger:
    """Normalized GitHub event handed to the dispatch pipelines."""

    kind: str  # "issue_opened" | "pr_opened" | "pr_reopened" | "pr_synchronize"
              # | "pr_labeled" | "issue_comment"
              # (issue_opened covers both the "opened" and "labeled" issue
              # actions — trigger.action carries which one fired)
    repository: str  # "owner/name"
    number: int
    title: str
    body: str
    author: str
    html_url: str
    delivery_id: str
    action: str
    labels: list[str] = field(default_factory=list)
    head_ref: str = ""
    base_ref: str = ""
    # Full commit reviewed by this trigger. An empty value means the source did
    # not provide one; callers must fetch it rather than guessing.
    head_sha: str = ""
    # issue_comment extras (ateles#112): populated when kind == "issue_comment"
    comment_id: int = 0
    comment_author: str = ""
    comment_body: str = ""
    comment_html_url: str = ""
    # True when the comment is on a PR (GitHub sends PR comments as issue_comment
    # with an `issue.pull_request` field; False for plain issue comments).
    comment_on_pr: bool = False
    # pull_request_review extras (approval loop): populated when kind ==
    # "pr_review". `review_state` is GitHub's verdict ("approved",
    # "changes_requested", "commented", "dismissed"); `review_author` is the
    # reviewer's login.
    review_state: str = ""
    review_author: str = ""
    # check_suite/status extras (ateles#197 — CI-driven loop closure): populated
    # when kind == "ci_status". `ci_head_sha` is the commit the checks ran
    # against; `ci_conclusion` is the terminal rollup ("success" | "failure" |
    # "cancelled" | "timed_out" | ...); `ci_pr_numbers` are the PRs GitHub
    # associates with that head (from check_suite.pull_requests), used to resolve
    # which PR to re-evaluate.
    ci_head_sha: str = ""
    ci_conclusion: str = ""
    ci_pr_numbers: list[int] = field(default_factory=list)
    # `ci_head_branch` is the branch the suite ran against (check_suite.
    # head_branch). Used to recognize a CI completion on the DEFAULT branch —
    # which carries no associated PR — so the release-prep retry can fire once a
    # merge's CI settles (the auto-release deferral path).
    ci_head_branch: str = ""
    # push extras (auto-release): populated when kind == "push". `push_ref` is
    # the fully-qualified ref ("refs/heads/main"); `push_after` is the new head
    # SHA. A push to the default branch is what makes a release preparable, so
    # this is the trigger the release daemon keys off.
    push_ref: str = ""
    push_after: str = ""
    # release_approve extras: the version the operator approved by email reply.
    release_version: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_pr(self) -> bool:
        # pr_review is NOT a pipeline-firing PR event — it must route to the
        # approval handler, not _handle_pr. Only the opened/reopened/synchronize
        # kinds (pr_opened, pr_reopened, pr_synchronize) drive the review panel.
        return self.kind.startswith("pr_") and self.kind != "pr_review"


def verify_github_signature(secret: str, body: bytes, signature_header: str) -> bool:
    """
    Verify GitHub's X-Hub-Signature-256 header (format: "sha256=<hexdigest>").

    Returns False on any malformed header. Constant-time comparison.
    """
    if not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header[len("sha256=") :])


def verify_github_signature_any(
    secrets: tuple[str, ...], body: bytes, signature_header: str
) -> bool:
    """Accept a signature valid under ANY of ``secrets`` (dual-admit overlap).

    Rotation is staged, never a flag day (authority_model.md#grants): during
    the overlap window the gateway must admit a delivery signed with either
    the retiring secret or the incoming one, so a rotation's live-verification
    probe (a real signed delivery) can succeed before the old secret retires,
    and any in-flight delivery signed under the old secret is not dropped
    mid-rotation. Empty/falsy entries are skipped so callers may pass a fixed
    two-slot tuple with the second slot blank outside a rotation. Every
    candidate is checked (no early return on the false branch) to avoid
    turning secret count into a timing signal.
    """
    verified = False
    for candidate in secrets:
        if not candidate:
            continue
        if verify_github_signature(candidate, body, signature_header):
            verified = True
    return verified


def _matches_any(presented: str, secrets: tuple[str, ...]) -> bool:
    """Constant-time check that ``presented`` equals ANY non-empty ``secrets``.

    Same dual-admit rationale as ``verify_github_signature_any``, for the
    shared-secret header comparison the /approve-email and /approve-release
    routes use instead of an HMAC signature. Every candidate is compared (no
    short-circuit on the first match) so the number of configured secrets is
    not observable via timing.
    """
    matched = False
    for candidate in secrets:
        if not candidate:
            continue
        if hmac.compare_digest(presented, candidate):
            matched = True
    return matched


def parse_github_event(
    event_type: str, payload: dict[str, Any], delivery_id: str = ""
) -> SwarmTrigger | None:
    """
    Normalize a GitHub webhook payload into a SwarmTrigger.

    Returns None for events/actions outside the trigger contract (the gateway
    ACKs them so GitHub does not retry, but nothing dispatches).
    """
    action = payload.get("action", "")
    repository = (payload.get("repository") or {}).get("full_name", "")

    if event_type == "issues" and action in ISSUE_ACTIONS:
        issue = payload.get("issue") or {}
        # PRs also surface via the issues API; only real issues trigger here.
        if "pull_request" in issue:
            return None
        if action == "labeled" and not _labeled_event_admitted(
            payload, f"{repository}#{issue.get('number', 0)}"
        ):
            return None
        return SwarmTrigger(
            kind="issue_opened",
            repository=repository,
            number=issue.get("number", 0),
            title=issue.get("title", ""),
            body=issue.get("body") or "",
            author=(issue.get("user") or {}).get("login", ""),
            html_url=issue.get("html_url", ""),
            delivery_id=delivery_id,
            action=action,
            labels=[lbl.get("name", "") for lbl in issue.get("labels", [])],
            raw=payload,
        )

    if event_type == "pull_request" and action in PR_ACTIONS:
        pr = payload.get("pull_request") or {}
        if action == "labeled" and not _labeled_event_admitted(
            payload, f"{repository}#{pr.get('number', 0)}"
        ):
            return None
        return SwarmTrigger(
            kind=f"pr_{action}",
            repository=repository,
            number=pr.get("number", 0),
            title=pr.get("title", ""),
            body=pr.get("body") or "",
            author=(pr.get("user") or {}).get("login", ""),
            html_url=pr.get("html_url", ""),
            delivery_id=delivery_id,
            action=action,
            labels=[lbl.get("name", "") for lbl in pr.get("labels", [])],
            head_ref=(pr.get("head") or {}).get("ref", ""),
            base_ref=(pr.get("base") or {}).get("ref", ""),
            head_sha=(pr.get("head") or {}).get("sha", ""),
            raw=payload,
        )

    # issue_comment events (ateles#112): operator /confirm-gates-clear override.
    # GitHub delivers PR comments as issue_comment with issue.pull_request present.
    if event_type == "issue_comment" and action in ISSUE_COMMENT_ACTIONS:
        issue = payload.get("issue") or {}
        comment = payload.get("comment") or {}
        is_pr_comment = "pull_request" in issue
        return SwarmTrigger(
            kind="issue_comment",
            repository=repository,
            number=issue.get("number", 0),
            title=issue.get("title", ""),
            body=issue.get("body") or "",
            author=(issue.get("user") or {}).get("login", ""),
            html_url=issue.get("html_url", ""),
            delivery_id=delivery_id,
            action=action,
            labels=[lbl.get("name", "") for lbl in issue.get("labels", [])],
            comment_id=comment.get("id", 0),
            comment_author=(comment.get("user") or {}).get("login", ""),
            comment_body=comment.get("body") or "",
            comment_html_url=comment.get("html_url", ""),
            comment_on_pr=is_pr_comment,
            raw=payload,
        )

    # pull_request_review events (approval loop): the operator submitting a PR
    # review with state "approved" in the GitHub UI. GitHub sends the review
    # object + the PR it targets. We normalize to a pr_review trigger carrying
    # the reviewer login and verdict; the dispatcher's operator-only guard +
    # the approved-state check decide whether it drives a merge.
    if event_type == "pull_request_review" and action in PR_REVIEW_ACTIONS:
        pr = payload.get("pull_request") or {}
        review = payload.get("review") or {}
        return SwarmTrigger(
            kind="pr_review",
            repository=repository,
            number=pr.get("number", 0),
            title=pr.get("title", ""),
            body=pr.get("body") or "",
            author=(pr.get("user") or {}).get("login", ""),
            html_url=pr.get("html_url", ""),
            delivery_id=delivery_id,
            action=action,
            head_ref=(pr.get("head") or {}).get("ref", ""),
            base_ref=(pr.get("base") or {}).get("ref", ""),
            head_sha=(pr.get("head") or {}).get("sha", ""),
            review_state=(review.get("state") or "").lower(),
            review_author=(review.get("user") or {}).get("login", ""),
            raw=payload,
        )

    # check_suite events (ateles#197): a CI rollup completed for a commit. We
    # normalize to a `ci_status` trigger carrying the head SHA, the terminal
    # conclusion, and the PRs GitHub links to that head. The dispatcher resolves
    # the affected PR and re-runs the readiness/routing logic — so a check going
    # green (while review is clear) can finally advance the merge signal, and a
    # check going red can route back to a fix, without waiting for a re-push.
    if event_type == "check_suite" and action in CHECK_SUITE_ACTIONS:
        suite = payload.get("check_suite") or {}
        prs = suite.get("pull_requests") or []
        first_pr = prs[0] if prs else {}
        return SwarmTrigger(
            kind="ci_status",
            repository=repository,
            # number is the first associated PR (0 if none — dispatcher guards).
            number=first_pr.get("number", 0),
            # check_suite carries no PR title/body/url; the dispatcher re-fetches
            # the PR to get them, so empty placeholders are fine here.
            title=first_pr.get("title", ""),
            body="",
            author="",
            html_url="",
            delivery_id=delivery_id,
            action=action,
            ci_head_sha=suite.get("head_sha", ""),
            ci_conclusion=(suite.get("conclusion") or "").lower(),
            ci_pr_numbers=[p.get("number", 0) for p in prs if p.get("number")],
            ci_head_branch=suite.get("head_branch", ""),
            raw=payload,
        )

    # push events (auto-release): a merge landed on the default branch, so there
    # may now be something to release. We normalize to a `push_main` trigger; the
    # dispatcher hands it to the release daemon, which re-applies its own gates
    # (unreleased-commit count, main CI green, no release already in flight)
    # before preparing anything. Deleted branches arrive with an all-zero `after`
    # SHA and must never trigger a release.
    if event_type == "push":
        ref = payload.get("ref", "")
        after = payload.get("after", "")
        if ref != RELEASE_PUSH_REF or payload.get("deleted") or set(after) <= {"0"}:
            return None
        head_commit = payload.get("head_commit") or {}
        return SwarmTrigger(
            kind="push_main",
            repository=repository,
            number=0,
            title=(head_commit.get("message") or "").split("\n")[0],
            body="",
            author=(head_commit.get("author") or {}).get("username", ""),
            html_url=head_commit.get("url", ""),
            delivery_id=delivery_id,
            action="push",
            push_ref=ref,
            push_after=after,
            raw=payload,
        )

    return None


TriggerHandler = Callable[[SwarmTrigger], Awaitable[None]]


def make_app(
    secret: str,
    handler: TriggerHandler,
    approve_email_secret: str = "",
    secret_next: str = "",
    approve_email_secret_next: str = "",
) -> web.Application:
    """
    Build the aiohttp application for the GitHub webhook receiver.

    Dispatch runs as a background task so the webhook responds within
    GitHub's 10s delivery timeout even though agent runs take minutes.

    ``approve_email_secret`` (approval loop): shared secret for the internal
    ``/approve-email`` route that Turdus POSTs to when the operator replies
    APPROVE to a merge-ready email. The route is loopback-only (the gateway
    binds 127.0.0.1) AND secret-gated; when the secret is unset the route
    fails closed (503) so an email reply can never drive a merge without the
    shared secret configured on both daemons.

    ``secret_next`` / ``approve_email_secret_next`` (rotation overlap window,
    `execution/scripts/rotate_swarm_secret.py`): when set, a delivery signed
    under the NEW value is admitted alongside the OLD one — dual-admit,
    per `authority_model.md#grants` ("rotation is staged, never a flag day").
    Both slots are checked on every request during the window; the rotator
    clears the ``_NEXT`` value once the old secret is retired, so outside a
    rotation these default to empty and behavior is unchanged.
    """

    async def handle_webhook(request: web.Request) -> web.Response:
        delivery_id = request.headers.get("X-GitHub-Delivery", "unknown")
        event_type = request.headers.get("X-GitHub-Event", "")
        body = await request.read()

        # Fail closed (Loxia review on PR #87): the gateway sits behind a
        # public tunnel — an unset secret must reject deliveries, not accept
        # unsigned ones, or anyone who finds the endpoint can spawn pipelines.
        if not secret and not secret_next:
            log.error(
                "[apis] APIS_GITHUB_WEBHOOK_SECRET unset — rejecting delivery "
                f"{delivery_id} (fail closed)"
            )
            return web.Response(status=503, text="Webhook secret not configured")
        sig = request.headers.get("X-Hub-Signature-256", "")
        if not verify_github_signature_any((secret, secret_next), body, sig):
            log.warning(f"[apis] GitHub signature mismatch, delivery={delivery_id}")
            return web.Response(status=401, text="Signature mismatch")

        if event_type == "ping":
            return web.json_response({"status": "pong"})

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return web.Response(status=400, text="Invalid JSON")

        trigger = parse_github_event(event_type, payload, delivery_id)
        if trigger is None:
            return web.json_response({"status": "ignored", "event": event_type})

        log.info(
            f"[apis] GitHub trigger {trigger.kind}: {trigger.repository}"
            f"#{trigger.number} delivery={delivery_id}"
        )
        task = asyncio.create_task(handler(trigger))
        request.app["inflight"].add(task)
        task.add_done_callback(request.app["inflight"].discard)
        return web.json_response({"status": "accepted", "kind": trigger.kind})

    async def handle_health(request: web.Request) -> web.Response:
        return web.json_response(
            {"status": "ok", "daemon": "apis", "inflight": len(request.app["inflight"])}
        )

    async def handle_approve_email(request: web.Request) -> web.Response:
        """Internal route: Turdus POSTs an operator email-reply approval here.

        Body JSON: {"repository": "owner/name", "pr_number": <int>,
                    "sender": "<verified operator email>"}
        Header: X-Approve-Secret: <shared secret>

        Fails closed: unset secret → 503; wrong secret → 401. Turdus is the
        authority that verified the reply came from the operator's address AND
        carried APPROVE + the correlation token; this route trusts that only
        because the shared secret authenticates the caller as our own Turdus.
        The actual merge stays behind APIS_APPROVAL_TRIGGERS_MERGE in the
        dispatcher, identical to the other approval channels.
        """
        if not approve_email_secret and not approve_email_secret_next:
            log.error("[apis] /approve-email hit but APPROVE secret unset — 503")
            return web.Response(status=503, text="approve-email not configured")
        if not _matches_any(
            request.headers.get("X-Approve-Secret", ""),
            (approve_email_secret, approve_email_secret_next),
        ):
            log.warning("[apis] /approve-email secret mismatch — 401")
            return web.Response(status=401, text="bad secret")
        try:
            payload = json.loads(await request.read())
        except json.JSONDecodeError:
            return web.Response(status=400, text="invalid JSON")
        repository = str(payload.get("repository", "")).strip()
        try:
            pr_number = int(payload.get("pr_number", 0))
        except (TypeError, ValueError):
            pr_number = 0
        if not repository or pr_number <= 0:
            return web.Response(status=400, text="repository + pr_number required")

        # Build a pr_review-shaped trigger attributed to the operator. Turdus
        # already verified the sender, so we stamp _OPERATOR_LOGIN + approved
        # and let the shared handler's merge gate apply. email_approve is a
        # distinct kind so the dispatcher can label the source honestly.
        trigger = SwarmTrigger(
            kind="email_approve",
            repository=repository,
            number=pr_number,
            title="",
            body="",
            author="",
            html_url=f"https://github.com/{repository}/pull/{pr_number}",
            delivery_id=f"email-approve-{repository}#{pr_number}",
            action="approved",
            review_state="approved",
            review_author=_OPERATOR_LOGIN,
        )
        log.info(
            f"[apis] email-approve accepted for {repository}#{pr_number} "
            f"(sender={payload.get('sender', '?')})"
        )
        task = asyncio.create_task(handler(trigger))
        request.app["inflight"].add(task)
        task.add_done_callback(request.app["inflight"].discard)
        return web.json_response({"status": "accepted", "kind": "email_approve"})

    async def handle_approve_release(request: web.Request) -> web.Response:
        """Internal route: Turdus POSTs an operator release-approval here.

        Body JSON: {"version": "vX.Y.Z", "sender": "<verified operator email>"}
        Header: X-Approve-Secret: <shared secret>

        Same trust model as /approve-email — Turdus already verified the reply
        came from the operator's address AND carried `approve <exact version>`
        plus the release-approve token; this route trusts that only because the
        shared secret authenticates the caller as our own Turdus. It emits a
        `release_approve` trigger; the dispatcher runs the SAME publish path as a
        Telegram approve (verify release_result is pending_approval → approved →
        publish.py), so this endpoint adds a channel, not a new bypass.

        Fails closed: unset secret → 503; wrong secret → 401; bad version → 400.
        """
        if not approve_email_secret and not approve_email_secret_next:
            log.error("[apis] /approve-release hit but APPROVE secret unset — 503")
            return web.Response(status=503, text="approve-release not configured")
        if not _matches_any(
            request.headers.get("X-Approve-Secret", ""),
            (approve_email_secret, approve_email_secret_next),
        ):
            log.warning("[apis] /approve-release secret mismatch — 401")
            return web.Response(status=401, text="bad secret")
        try:
            payload = json.loads(await request.read())
        except json.JSONDecodeError:
            return web.Response(status=400, text="invalid JSON")
        version = str(payload.get("version", "")).strip()
        # A release tag: leading v + digits. Reject anything else so a malformed
        # POST can never reach the publish path.
        if not re.match(r"^v[0-9][0-9A-Za-z.\-+]*$", version):
            return web.Response(status=400, text="valid version (vX.Y.Z) required")
        trigger = SwarmTrigger(
            kind="release_approve",
            repository="",
            number=0,
            title="",
            body="",
            author="",
            html_url="",
            delivery_id=f"release-approve-{version}",
            action="approved",
            release_version=version,
        )
        log.info(
            f"[apis] release-approve accepted for {version} "
            f"(sender={payload.get('sender', '?')})"
        )
        task = asyncio.create_task(handler(trigger))
        request.app["inflight"].add(task)
        task.add_done_callback(request.app["inflight"].discard)
        return web.json_response({"status": "accepted", "kind": "release_approve"})

    app = web.Application()
    app["inflight"] = set()
    app.router.add_post("/github/webhook", handle_webhook)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/approve-email", handle_approve_email)
    app.router.add_post("/approve-release", handle_approve_release)
    return app


async def serve(app: web.Application, port: int) -> None:
    """Run the webhook receiver forever (alongside the SSE loop)."""
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    log.info(f"[apis] GitHub webhook gateway listening on 127.0.0.1:{port}")
    while True:
        await asyncio.sleep(3600)
