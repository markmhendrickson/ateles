#!/usr/bin/env python3
"""
Anthus — Swarm coordinator daemon.

Anthus genus: pipits (small ground-running songbirds). T3 daemon in the Ateles swarm.

Anthus maintains a global view of work-in-flight across all daemons and
surfaces conflicts, blockers, and anomalies to Ateles. It subscribes to
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
from lib.activity import ActivityLogger  # noqa: E402

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("anthus")

# ── Activity-log channel (CyphorhinusBot observation feed) ──────────────────
_activity = ActivityLogger(agent="anthus")

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
    events and surfaces escalations + critical daemon_reports to Ateles.
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

    State persistence (ateles#9): every dispatch and satisfaction is written
    to a Neotoma `participation_record` entity. On daemon restart, we load
    these records and rebuild in-memory state — no double-dispatch.
    """
    import participation
    from orchestrator import (  # local imports — avoid cost at startup
        GateState,
        compute_ready_gates,
        fetch_workflow_definitions,
        resolve_unmet_preconditions,
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

    # Hydrate in-memory state from persisted participation_records if we
    # don't have any in-process state yet for this work entity.
    if event.entity_id not in _gate_states:
        persisted = await participation.load_state_for(event.entity_id)
        if persisted:
            _gate_states[event.entity_id] = {
                name: GateState(
                    gate_name=name,
                    status=rec.get("status", "pending"),
                    dispatched_at=rec.get("dispatched_at"),
                    satisfied_at=rec.get("satisfied_at"),
                    artifact_refs=list(rec.get("artifact_refs", [])),
                )
                for name, rec in persisted.items()
            }
            log.info(
                f"[{DAEMON_NAME}] hydrated {len(persisted)} gate states for "
                f"{event.entity_id} from Neotoma"
            )

    comments = await _fetch_comments(snap)

    # Autonomous generalization (ateles agent-operator learning): scan this
    # work entity's comments for strategy_drift_signal lines, cluster them, and
    # let the generalizer auto-apply agent-local policies (or open operator-
    # gated proposals for cross-cutting themes). Fully reversible; best-effort.
    await _harvest_drift_signals(comments)

    existing = _gate_states.get(event.entity_id, {})

    unmet = await resolve_unmet_preconditions(wf, project)
    state, ready = compute_ready_gates(
        wf,
        snap,
        comments,
        existing_state=existing,
        unmet_preconditions=unmet,
    )
    _gate_states[event.entity_id] = state

    # Persist any newly-satisfied / newly-skipped gates discovered this tick.
    for gate_name, gs in state.items():
        prior = existing.get(gate_name)
        if gs.status == "satisfied" and (not prior or prior.status != "satisfied"):
            ref = gs.artifact_refs[-1] if gs.artifact_refs else ""
            await participation.record_satisfied(event.entity_id, gate_name, ref)
        elif gs.status == "skipped" and (not prior or prior.status != "skipped"):
            reason = "fast_path" if gate_name not in unmet else "precondition_unmet"
            await participation.record_skipped(event.entity_id, gate_name, reason)

    for gate in ready:
        log.info(
            f"[{DAEMON_NAME}] dispatch gate {gate.gate_name} → {gate.owner_agent} "
            f"on {event.entity_id}"
        )
        state[gate.gate_name].status = "dispatched"
        # Pin the exact agent_definition version at dispatch time (ateles#22).
        gate_agent_def = AgentLoader(gate.owner_agent).load()
        await participation.record_dispatched(
            work_entity_id=event.entity_id,
            workflow_definition_id=wf.entity_id,
            gate_name=gate.gate_name,
            agent=gate.owner_agent,
            agent_definition_ref=gate_agent_def.entity_id,
            agent_definition_observation_id=gate_agent_def.last_observation_id,
        )
        # Spawn the agent via claude CLI. The `--skill` flag does not exist;
        # the working invocation is `--append-system-prompt` with the SKILL.md
        # content. Code-writing agents need `--dangerously-skip-permissions`
        # to avoid interactive prompts under launchd (per Tier 1 smoke test
        # findings, 2026-05-25).
        job = _activity.started(
            f"dispatching gate {gate.gate_name} → {gate.owner_agent} on {event.entity_id}"
        )
        try:
            await _spawn_agent(
                owner_agent=gate.owner_agent,
                work_entity_id=event.entity_id,
                gate_name=gate.gate_name,
                snapshot=snap,
            )
            job.finished(
                f"spawned {gate.owner_agent} for {gate.gate_name} on {event.entity_id}"
            )
        except Exception as exc:
            job.failed(f"spawn failed: {exc}")
            raise
        _notifier.send(
            f"Gate dispatched: {gate.gate_name} ({gate.owner_agent}) on {event.entity_id}",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )


# Agents that need write access to the local filesystem or to call MCP servers
# that prompt for permission. Anthus runs under launchd with no TTY, so these
# must be invoked with --dangerously-skip-permissions.
_AGENTS_NEEDING_SKIP_PERMISSIONS = frozenset(
    {"cicada", "vanellus", "apus", "formica", "neotoma-agent"}
)


async def _spawn_agent(
    owner_agent: str,
    work_entity_id: str,
    gate_name: str,
    snapshot: dict,
) -> None:
    """
    Spawn an agent via `claude --print --append-system-prompt` with the agent's
    SKILL.md content as the system prompt. Runs in the background; output is
    logged but not awaited synchronously — the agent posts its artifact to the
    GitHub issue/PR, and Anthus picks it up on the next SSE event.

    The chosen invocation pattern matches the operator-driven Tier 1 workflow
    so that operator-run and Anthus-run dispatches produce identical artifacts.
    """
    import os
    import shutil
    from pathlib import Path

    claude_bin = shutil.which("claude")
    if not claude_bin:
        log.error(
            f"[{DAEMON_NAME}] `claude` binary not on PATH — cannot dispatch "
            f"{owner_agent}. Check launchagent PATH includes NVM."
        )
        return

    skill_path = (
        Path(os.environ.get("ATELES_REPO_ROOT", str(_REPO_ROOT)))
        / ".claude"
        / "skills"
        / owner_agent
        / "SKILL.md"
    )
    if not skill_path.exists():
        log.error(
            f"[{DAEMON_NAME}] SKILL.md not found for {owner_agent} at {skill_path}"
        )
        return

    try:
        skill_md = skill_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.error(f"[{DAEMON_NAME}] failed to read {skill_path}: {exc}")
        return

    # Inject this agent's learned, agent-local policies (active + provisional)
    # into the system prompt so generalized behaviour is actually applied at
    # dispatch — not merely available to consult.
    try:
        policy_block = AgentLoader(owner_agent).render_policy_prompt()
        if policy_block:
            skill_md = skill_md + policy_block
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[{DAEMON_NAME}] could not load policies for {owner_agent}: {exc}")

    title = snapshot.get("title", "")
    body = snapshot.get("body", "")
    repo = snapshot.get("repository") or snapshot.get("repo") or ""
    number = snapshot.get("number") or snapshot.get("issue_number") or ""
    # NOTE: do NOT prefix the prompt with `/<agent>`. In `claude --print` mode
    # a leading slash is interpreted as a slash-command and consumed silently,
    # producing zero-token output. The agent's identity comes from the
    # `--append-system-prompt` SKILL.md instead. (Tier 1 re-run finding,
    # 2026-05-25.)
    prompt = (
        f"Invoke the {owner_agent} agent per your appended system prompt.\n\n"
        f"GitHub issue {repo}#{number}: {title}\n\n{body}\n\n"
        f"Gate: {gate_name}. Work entity: {work_entity_id}. "
        f"End your response with the artifact header line as specified in your SKILL.md."
    )

    args = [
        claude_bin,
        "--print",
        "--append-system-prompt",
        skill_md,
    ]
    if owner_agent in _AGENTS_NEEDING_SKIP_PERMISSIONS:
        args.append("--dangerously-skip-permissions")
    args.append(prompt)

    log.info(
        f"[{DAEMON_NAME}] spawning {owner_agent} for {work_entity_id} "
        f"(skip_perms={owner_agent in _AGENTS_NEEDING_SKIP_PERMISSIONS})"
    )

    # Fire-and-forget: agent runs as background subprocess. Its artifact is
    # posted to the GitHub issue/PR; Anthus picks up satisfaction on the next
    # SSE comment event.
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        # Don't await proc.wait() — let it run independently so we don't block
        # the event loop. The SSE handler picks up the artifact when posted.
        log.info(f"[{DAEMON_NAME}] spawned {owner_agent} pid={proc.pid}")
    except OSError as exc:
        log.error(f"[{DAEMON_NAME}] failed to spawn {owner_agent}: {exc}")


async def _harvest_drift_signals(comments: list) -> None:
    """
    Parse strategy_drift_signal lines out of a work entity's comments and run
    the agent-local generalization loop over them. Best-effort: any failure is
    logged and swallowed so it never blocks workflow orchestration.
    """
    from lib.daemon_runtime import parse_comments
    from lib.daemon_runtime import generalizer

    try:
        signals = parse_comments(comments)
        if not signals:
            return
        decisions = await generalizer.harvest(signals)
        for d in decisions:
            if d.action.value != "noop":
                log.info(
                    f"[{DAEMON_NAME}] generalizer {d.action.value} for "
                    f"{d.cluster.agent}: {d.reason}"
                )
    except Exception as exc:  # noqa: BLE001 — never let learning break dispatch
        log.warning(f"[{DAEMON_NAME}] drift-signal harvest failed: {exc}")


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
    """Surface escalation entities to Ateles via Notifier."""
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
