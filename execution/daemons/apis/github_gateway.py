"""
execution/daemons/apis/github_gateway.py — GitHub webhook → Apis trigger layer.

Implements the missing trigger layer from ateles#80: GitHub fires
`issues.opened` / `pull_request.opened` webhooks at this receiver, which
verifies the HMAC signature, normalizes the payload into a SwarmTrigger, and
hands it to the Apis dispatch pipelines (swarm_dispatch.py) that spawn the
gate agents (Lanius → Pavo on issues; Lanius → review panel → Vanellus on PRs).

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
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from aiohttp import web

log = logging.getLogger("apis.github_gateway")

# GitHub actions that fire the PR pipeline. `synchronize` re-runs review on
# new pushes; `reopened` re-enters the pipeline after a close.
PR_ACTIONS = {"opened", "reopened", "synchronize"}
ISSUE_ACTIONS = {"opened"}


@dataclass
class SwarmTrigger:
    """Normalized GitHub event handed to the dispatch pipelines."""

    kind: str  # "issue_opened" | "pr_opened" | "pr_reopened" | "pr_synchronize"
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
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_pr(self) -> bool:
        return self.kind.startswith("pr_")


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

    return None


TriggerHandler = Callable[[SwarmTrigger], Awaitable[None]]


def make_app(secret: str, handler: TriggerHandler) -> web.Application:
    """
    Build the aiohttp application for the GitHub webhook receiver.

    Dispatch runs as a background task so the webhook responds within
    GitHub's 10s delivery timeout even though agent runs take minutes.
    """

    async def handle_webhook(request: web.Request) -> web.Response:
        delivery_id = request.headers.get("X-GitHub-Delivery", "unknown")
        event_type = request.headers.get("X-GitHub-Event", "")
        body = await request.read()

        if secret:
            sig = request.headers.get("X-Hub-Signature-256", "")
            if not verify_github_signature(secret, body, sig):
                log.warning(f"[apis] GitHub signature mismatch, delivery={delivery_id}")
                return web.Response(status=401, text="Signature mismatch")
        else:
            log.warning(
                "[apis] APIS_GITHUB_WEBHOOK_SECRET unset — accepting unsigned delivery"
            )

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

    app = web.Application()
    app["inflight"] = set()
    app.router.add_post("/github/webhook", handle_webhook)
    app.router.add_get("/health", handle_health)
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
