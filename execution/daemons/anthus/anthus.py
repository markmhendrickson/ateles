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
    "issue",
    "pull_request",
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
    elif event.entity_type in ("issue", "pull_request"):
        await _orchestrate_workflow_for(event)


# ── Orchestrator state (in-memory; persisted to Neotoma in Phase 6+) ──────────
# Maps work_entity_id → {gate_name → GateState}
_gate_states: dict[str, dict] = {}


async def _orchestrate_workflow_for(event) -> None:
    """
    On issue/pull_request event, select a workflow_definition that applies,
    compute ready gates, and dispatch each via `claude --print --skill <owner>`.

    Comments on the issue/PR are read via `gh` CLI (synchronous shell call)
    rather than the github_harness MCP to keep Anthus self-contained for now.
    Phase 6 will route this through the harness for proper attribution.
    """
    from orchestrator import (  # local import — avoid cost at startup
        compute_ready_gates,
        fetch_workflow_definitions,
        select_workflow,
    )

    snap = event.snapshot or {}
    project = _project_from_repo(snap.get("repository") or snap.get("repo") or "")
    if not project:
        log.debug(f"[{DAEMON_NAME}] no project derivable from event {event.entity_id}")
        return

    workflows = await fetch_workflow_definitions(project)
    if not workflows:
        return
    wf = select_workflow(snap, workflows)
    if wf is None:
        log.debug(
            f"[{DAEMON_NAME}] no workflow matches {event.entity_id} (project={project})"
        )
        return

    comments = await _fetch_comments(snap)
    existing = _gate_states.get(event.entity_id, {})
    state, ready = compute_ready_gates(wf, snap, comments, existing_state=existing)
    _gate_states[event.entity_id] = state

    for gate in ready:
        log.info(
            f"[{DAEMON_NAME}] dispatch gate {gate.gate_name} → {gate.owner_agent} "
            f"on {event.entity_id}"
        )
        state[gate.gate_name].status = "dispatched"
        # Real dispatch happens in Phase 6 once Anthus has its own
        # `claude --print --skill` spawning helper that signs with AAuth.
        # For now Anthus only logs and notifies — operator runs the agents
        # manually per docs/smoke_test_runbook.md.
        _notifier.send(
            f"Gate ready: {gate.gate_name} ({gate.owner_agent}) on {event.entity_id}",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )


def _project_from_repo(repo_slug: str) -> str:
    """Map a GitHub owner/repo string to the workflow_definition project name."""
    if not repo_slug:
        return ""
    parts = str(repo_slug).split("/")
    return parts[-1] if parts else ""


async def _fetch_comments(snap: dict) -> list:
    """
    Read comments on the issue/PR via `gh` CLI.
    Returns a list of {id, author, body, url} dicts that orchestrator can scan.

    Falls back to empty list on any error — orchestrator treats empty
    comments the same as "nothing satisfied yet".
    """
    import json as _json
    import subprocess as _sp

    number = snap.get("github_number") or snap.get("number")
    repo = snap.get("repository") or snap.get("repo")
    if not number or not repo:
        return []

    try:
        out = _sp.run(
            [
                "gh",
                "issue",
                "view",
                str(number),
                "--repo",
                str(repo),
                "--json",
                "comments",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        data = _json.loads(out.stdout)
        comments = data.get("comments", [])
        return [
            {
                "id": c.get("id"),
                "author": (c.get("author") or {}).get("login", ""),
                "body": c.get("body", ""),
                "url": c.get("url"),
            }
            for c in comments
        ]
    except Exception as exc:
        log.warning(
            f"[{DAEMON_NAME}] _fetch_comments failed for {repo}#{number}: {exc}"
        )
        return []


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
