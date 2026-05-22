#!/usr/bin/env python3
"""
Castor — Neotoma-repo automation daemon.

Castor genus: beavers. T3 daemon in the Ateles swarm.

Subscribes to Neotoma issue/PR events and processes them against the
neotoma repository. Mirrors the Formica pattern but scoped to the
neotoma repo rather than general GitHub automation.

AAuth sub: castor@ateles-swarm
Phase 1: skeleton only — event loop stubbed, full logic in Phase 3.

Startup sequence (T3 daemon pattern):
  1. Load env from ~/.config/neotoma/.env
  2. Load agent_definition from Neotoma via lib/daemon_runtime
  3. Load priority_rubric from Neotoma via lib/notify
  4. Subscribe to relevant Neotoma entity types via SSE
  5. Process events

Environment variables:
  NEOTOMA_BEARER_TOKEN    Neotoma API auth token
  NEOTOMA_BASE_URL        Neotoma API base URL (default: https://neotoma.markmhendrickson.com)
  TELEGRAM_BOT_TOKEN      Telegram bot token
  TELEGRAM_CHAT_ID        Telegram chat ID
  TELEGRAM_TOPIC_CASTOR   Telegram topic ID for Castor notifications (optional)
  CASTOR_AGENT_DEFINITION_ID  Neotoma entity ID for Castor's agent_definition (optional)
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

# ── Path bootstrap ────────────────────────────────────────────────────────────
# Allow running as `python castor.py` from the daemon directory
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime import (  # noqa: E402
    AAuthSigner,
    AgentLoader,
    NeotomaEvent,
    SSEClient,
)
from lib.notify import Notifier, Priority  # noqa: E402

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("castor")

# ── Config ────────────────────────────────────────────────────────────────────
DAEMON_NAME = "castor"
SUBSCRIBE_ENTITY_TYPES = ["issue", "pull_request"]

# Neotoma repo slug for GitHub automation
NEOTOMA_REPO = os.environ.get("CASTOR_NEOTOMA_REPO", "markmhendrickson/neotoma")


# ── Event handler ─────────────────────────────────────────────────────────────


async def handle_event(event: NeotomaEvent, notifier: Notifier) -> None:
    """
    Handle a Neotoma SSE event.

    Phase 1 skeleton: logs and notifies, no automation yet.
    Phase 3: spawn T4 invocable agent to process issue/PR.
    """
    entity_type = event.entity_type
    entity_id = event.entity_id
    action = event.action

    log.info(f"[castor] Event: {entity_type}/{entity_id} action={action}")

    # Phase 3: check if this issue/PR is targeted at the neotoma repo
    # and dispatch to process_issues / process_prs skill via claude --print
    # For now, just acknowledge receipt
    if entity_type == "issue" and action == "created":
        title = event.snapshot.get("title", "(untitled)")
        notifier.send(
            f"New issue: {title[:80]}\n  {entity_id}",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )

    elif entity_type == "pull_request" and action == "created":
        title = event.snapshot.get("title", "(untitled)")
        notifier.send(
            f"New PR: {title[:80]}\n  {entity_id}",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )


# ── Main ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    log.info(f"[{DAEMON_NAME}] Starting up...")

    # 1. Load agent_definition from Neotoma
    agent_def = AgentLoader(DAEMON_NAME).load()
    log.info(
        f"[{DAEMON_NAME}] agent_definition: status={agent_def.status} "
        f"grant={agent_def.agent_grant} sub={agent_def.aauth_sub}"
    )

    # 2. Load AAuth signer (stub until Phase 1 keypair minting)
    signer = AAuthSigner.from_key_file(DAEMON_NAME)
    if signer.is_stub:
        log.warning(
            f"[{DAEMON_NAME}] AAuth keypair not minted yet — "
            "observations attributed to operator token"
        )

    # 3. Load notification rubric
    notifier = Notifier.from_neotoma()
    notifier.send(
        f"{DAEMON_NAME} started (Phase 1 skeleton)",
        priority=Priority.INFO,
        handler=DAEMON_NAME,
    )

    # 4. Subscribe to SSE events
    sse = SSEClient(
        entity_types=SUBSCRIBE_ENTITY_TYPES,
        handler_name=DAEMON_NAME,
    )

    async def dispatch(event: NeotomaEvent) -> None:
        await handle_event(event, notifier)

    log.info(f"[{DAEMON_NAME}] Subscribing to SSE: {SUBSCRIBE_ENTITY_TYPES}")
    await sse.stream(dispatch)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info(f"[{DAEMON_NAME}] Stopped by operator.")
