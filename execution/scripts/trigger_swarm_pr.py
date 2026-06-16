#!/usr/bin/env python3
"""
execution/scripts/trigger_swarm_pr.py — manually dispatch the swarm trigger
pipeline for a GitHub issue or PR, bypassing the webhook gateway.

Reconstructs the webhook payload from the GitHub REST API and hands the
normalized SwarmTrigger straight to SwarmDispatcher.handle_trigger — the
exact object github_gateway produces after HMAC verification. Useful for
backfilling events that fired before the webhook was configured, re-running
a review after a pipeline fix, and acceptance-testing changes to the
pipeline itself (first validated by self-dogfooding PR #87 on 2026-06-12).

Usage:
    python execution/scripts/trigger_swarm_pr.py <owner/repo> pr <number>
    python execution/scripts/trigger_swarm_pr.py <owner/repo> issue <number>

Env:
    GITHUB_TOKEN / ATELES_AGENT_PAT  GitHub identity (reads + fallback comments)
    NEOTOMA_BEARER_TOKEN             Neotoma writes (harness_event, checkpoint)
    APIS_DRY_RUN=1                   log + harness_event only, skip pipeline
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DAEMON_DIR = _REPO_ROOT / "execution" / "daemons" / "apis"
for p in (str(_REPO_ROOT), str(_DAEMON_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import httpx  # noqa: E402

from github_gateway import parse_github_event  # noqa: E402
from swarm_dispatch import SwarmDispatcher  # noqa: E402
from lib.notify import Notifier  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("trigger_swarm_pr")


async def main(repo: str, kind: str, number: int) -> None:
    token = os.environ.get("GITHUB_TOKEN", "") or os.environ.get(
        "ATELES_AGENT_PAT", ""
    )
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    api_path = "pulls" if kind == "pr" else "issues"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"https://api.github.com/repos/{repo}/{api_path}/{number}",
            headers=headers,
        )
        resp.raise_for_status()
        obj = resp.json()

    if kind == "pr":
        payload = {
            "action": "opened",
            "repository": {"full_name": repo},
            "pull_request": obj,
        }
        event_type = "pull_request"
    else:
        payload = {
            "action": "opened",
            "repository": {"full_name": repo},
            "issue": obj,
        }
        event_type = "issues"

    trigger = parse_github_event(
        event_type,
        payload,
        delivery_id=f"manual-{repo.replace('/', '-')}-{kind}-{number}",
    )
    if trigger is None:
        log.error("payload did not normalize to a SwarmTrigger")
        sys.exit(1)

    log.info(
        f"Trigger: kind={trigger.kind} {trigger.repository}#{trigger.number} "
        f"author={trigger.author}"
    )
    dispatcher = SwarmDispatcher(Notifier())
    await dispatcher.handle_trigger(trigger)
    log.info("handle_trigger returned")


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[2] not in ("pr", "issue"):
        print(__doc__)
        sys.exit(2)
    asyncio.run(main(sys.argv[1], sys.argv[2], int(sys.argv[3])))
