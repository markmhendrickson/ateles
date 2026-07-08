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
  APIS_GITHUB_WEBHOOK_SECRET   HMAC-SHA256 secret configured on the GitHub webhook
  APIS_GITHUB_WEBHOOK_PORT     listen port (default: 8742; Apus owns 8741)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from aiohttp import web

log = logging.getLogger("apis.github_gateway")

# Operator GitHub login used to attribute an email-reply approval. Kept in sync
# with swarm_dispatch._OPERATOR_LOGIN via the same env var (read here to avoid a
# circular import — swarm_dispatch imports SwarmTrigger from this module).
_OPERATOR_LOGIN = os.environ.get("APIS_OPERATOR_LOGIN", "markmhendrickson")

# GitHub actions that fire the PR pipeline. `synchronize` re-runs review on
# new pushes; `reopened` re-enters the pipeline after a close.
PR_ACTIONS = {"opened", "reopened", "synchronize"}
ISSUE_ACTIONS = {"opened"}
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


@dataclass
class SwarmTrigger:
    """Normalized GitHub event handed to the dispatch pipelines."""

    kind: str  # "issue_opened" | "pr_opened" | "pr_reopened" | "pr_synchronize"
              # | "issue_comment"
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
            raw=payload,
        )

    return None


TriggerHandler = Callable[[SwarmTrigger], Awaitable[None]]


def make_app(
    secret: str,
    handler: TriggerHandler,
    approve_email_secret: str = "",
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
    """

    async def handle_webhook(request: web.Request) -> web.Response:
        delivery_id = request.headers.get("X-GitHub-Delivery", "unknown")
        event_type = request.headers.get("X-GitHub-Event", "")
        body = await request.read()

        # Fail closed (Loxia review on PR #87): the gateway sits behind a
        # public tunnel — an unset secret must reject deliveries, not accept
        # unsigned ones, or anyone who finds the endpoint can spawn pipelines.
        if not secret:
            log.error(
                "[apis] APIS_GITHUB_WEBHOOK_SECRET unset — rejecting delivery "
                f"{delivery_id} (fail closed)"
            )
            return web.Response(status=503, text="Webhook secret not configured")
        sig = request.headers.get("X-Hub-Signature-256", "")
        if not verify_github_signature(secret, body, sig):
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
        if not approve_email_secret:
            log.error("[apis] /approve-email hit but APPROVE secret unset — 503")
            return web.Response(status=503, text="approve-email not configured")
        if request.headers.get("X-Approve-Secret", "") != approve_email_secret:
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

    app = web.Application()
    app["inflight"] = set()
    app.router.add_post("/github/webhook", handle_webhook)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/approve-email", handle_approve_email)
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
