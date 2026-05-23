#!/usr/bin/env python3
"""
Anthus — Swarm coordinator daemon.

Anthus genus: pipits (small ground-running songbirds). T3 daemon in the Ateles swarm.

Anthus maintains a global view of work-in-flight across all daemons and
surfaces conflicts, blockers, and anomalies to Onychomys. It subscribes to
Neotoma SSE events for tasks, daemon_reports, escalations, and agent_grants,
and applies the priority_rubric before paging the operator.

Lives at: launchd on the operator's machine (no external endpoint required)

AAuth sub: anthus@ateles-swarm
Phase 2: skeleton with SSE subscription + Neotoma agent_definition load.
Full swarm-coordinator logic deferred to Phase 6.

Environment variables:
  NEOTOMA_BEARER_TOKEN      Neotoma API auth token
  NEOTOMA_BASE_URL          Neotoma API base URL (default: https://neotoma.markmhendrickson.com)
  TELEGRAM_BOT_TOKEN        Telegram bot token
  TELEGRAM_CHAT_ID          Telegram chat ID
  TELEGRAM_TOPIC_ANTHUS     Telegram topic ID for Anthus notifications (optional)
  ANTHUS_AGENT_DEFINITION_ID  Neotoma entity ID for Anthus's agent_definition (optional)
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# ── Path bootstrap ────────────────────────────────────────────────────────────
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
log = logging.getLogger("anthus")

# ── Config ────────────────────────────────────────────────────────────────────
DAEMON_NAME = "anthus"

# Entity types Anthus monitors
SUBSCRIBED_ENTITY_TYPES = [
    "task",
    "daemon_report",
    "escalation",
    "agent_grant",
]


# ── Event handler ─────────────────────────────────────────────────────────────


async def handle_event(event: NeotomaEvent) -> None:
    """
    Route Neotoma SSE events to the appropriate handler.

    Phase 6 will add full swarm-coordinator logic here. For now Anthus logs
    events and surfaces escalations + critical daemon_reports to Onychomys.
    """
    log.debug(
        f"[{DAEMON_NAME}] event: {event.entity_type}/{event.action} "
        f"entity={event.entity_id}"
    )

    if event.entity_type == "escalation" and event.action in ("created", "updated"):
        await _handle_escalation(event)
    elif event.entity_type == "daemon_report":
        await _handle_daemon_report(event)
    elif event.entity_type == "agent_grant" and event.action in (
        "updated",
        "deleted",
    ):
        await _handle_grant_change(event)
    elif event.entity_type == "task" and event.action == "created":
        # Phase 6: detect conflicting tasks, stale work-in-flight, etc.
        log.debug(f"[{DAEMON_NAME}] task created: {event.entity_id}")


async def _handle_escalation(event: NeotomaEvent) -> None:
    """Surface escalation entities to Onychomys via Notifier."""
    severity = event.snapshot.get("severity", "unknown")
    summary = event.snapshot.get("summary", event.entity_id)
    blocking = event.snapshot.get("blocking", False)

    priority = Priority.BLOCKER if blocking else Priority.OPERATOR_DECISION
    log.info(
        f"[{DAEMON_NAME}] escalation {event.entity_id}: severity={severity} "
        f"blocking={blocking} — notifying"
    )
    _notifier.send(
        f"Escalation [{severity}]: {summary}",
        priority=priority,
        handler=DAEMON_NAME,
    )


async def _handle_daemon_report(event: NeotomaEvent) -> None:
    """Notify on error-level daemon reports."""
    report_severity = event.snapshot.get("severity", "info")
    daemon = event.snapshot.get("daemon", "unknown")
    summary = event.snapshot.get("summary", "")

    if report_severity in ("error", "critical"):
        priority = (
            Priority.CRITICAL if report_severity == "critical" else Priority.BLOCKER
        )
        log.warning(
            f"[{DAEMON_NAME}] daemon_report {event.entity_id}: "
            f"daemon={daemon} severity={report_severity}"
        )
        _notifier.send(
            f"Daemon error [{daemon}]: {summary or event.entity_id}",
            priority=priority,
            handler=DAEMON_NAME,
        )


async def _handle_grant_change(event: NeotomaEvent) -> None:
    """Alert on unexpected agent_grant suspension or revocation."""
    status = event.snapshot.get("status", "")
    agent = event.snapshot.get("agent_sub", event.entity_id)

    if status in ("suspended", "revoked"):
        log.warning(
            f"[{DAEMON_NAME}] agent_grant {status}: {agent} — notifying operator"
        )
        _notifier.send(
            f"AAuth grant {status}: {agent}",
            priority=Priority.BLOCKER,
            handler=DAEMON_NAME,
        )


# ── Module-level notifier (populated in main) ─────────────────────────────────
_notifier: Notifier


# ── Main ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    global _notifier

    log.info(f"[{DAEMON_NAME}] Starting up...")

    # 1. Load agent_definition from Neotoma
    agent_def = AgentLoader(DAEMON_NAME).load()
    log.info(
        f"[{DAEMON_NAME}] agent_definition: status={agent_def.status} "
        f"grant={agent_def.agent_grant} sub={agent_def.aauth_sub}"
    )

    # 2. Load AAuth signer
    signer = AAuthSigner.from_key_file(DAEMON_NAME)
    if signer.is_stub:
        log.warning(
            f"[{DAEMON_NAME}] AAuth keypair not minted yet — "
            "observations attributed to operator token"
        )

    # 3. Load notification rubric
    _notifier = Notifier.from_neotoma()

    # 4. Notify startup
    _notifier.send(
        f"{DAEMON_NAME} started — monitoring {SUBSCRIBED_ENTITY_TYPES}",
        priority=Priority.INFO,
        handler=DAEMON_NAME,
    )

    # 5. Subscribe to Neotoma SSE stream
    sse = SSEClient(
        entity_types=SUBSCRIBED_ENTITY_TYPES,
        handler_name=DAEMON_NAME,
    )
    log.info(
        f"[{DAEMON_NAME}] Subscribing to SSE: entity_types={SUBSCRIBED_ENTITY_TYPES}"
    )

    try:
        await sse.stream(handle_event, reconnect=True)
    except asyncio.CancelledError:
        log.info(f"[{DAEMON_NAME}] SSE stream cancelled.")
    finally:
        log.info(f"[{DAEMON_NAME}] Shutting down.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info(f"[{DAEMON_NAME}] Stopped by operator.")
