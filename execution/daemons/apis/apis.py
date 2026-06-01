#!/usr/bin/env python3
"""
Apis — Ateles universal task dispatcher daemon.

Apis genus: honeybees. T3 daemon in the Ateles swarm.

Subscribes to Neotoma task events and dispatches to appropriate T4 agents
based on domain tags. Replaces Monedula's task-dispatch scope; Monedula
retains its payment-execution and calendar-detection logic.

Dispatch routing (Phase 4 skeleton — subprocess dispatch in Phase 5):
  task.created   → tag inference + dispatch to domain handler
  task.updated   → check for status transitions (e.g. approved → execute)
  task.due_today → remind operator; optionally auto-execute if configured

AAuth sub: apis@ateles-swarm
Startup sequence (T3 daemon pattern):
  1. Load env from ~/.config/neotoma/.env
  2. Load agent_definition from Neotoma via lib/daemon_runtime
  3. Load AAuth signer
  4. Load priority_rubric from Neotoma via lib/notify
  5. Subscribe to Neotoma SSE and dispatch events

Environment variables:
  NEOTOMA_BEARER_TOKEN        Neotoma API auth token
  NEOTOMA_BASE_URL            Neotoma API base URL
  TELEGRAM_BOT_TOKEN          Telegram bot token
  TELEGRAM_CHAT_ID            Telegram chat ID
  TELEGRAM_TOPIC_APIS         Telegram topic ID for Apis notifications (optional)
  APIS_AGENT_DEFINITION_ID    Neotoma entity ID for Apis's agent_definition (optional)
  APIS_DRY_RUN                Set to "1" to log events without dispatching agents
  APIS_AUTO_EXECUTE           Set to "1" to auto-execute due tasks (default: notify only)
  ATELES_REPO_PATH            Local path to ateles clone (default: ~/repos/ateles)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from pathlib import Path

# ── Path bootstrap ────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime import (  # noqa: E402
    AAuthSigner,
    AgentLoader,
    GateAction,
    NeotomaEvent,
    SSEClient,
    evaluate_gate,
    resolve_policy_for_agent,
    write_checkpoint_brief,
)
from lib.notify import Notifier, Priority  # noqa: E402

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("apis")

# ── Config ────────────────────────────────────────────────────────────────────
DAEMON_NAME = "apis"

SUBSCRIBE_ENTITY_TYPES = ["task"]

ATELES_REPO = Path(
    os.environ.get("ATELES_REPO_PATH", str(Path.home() / "repos" / "ateles"))
)

DRY_RUN = os.environ.get("APIS_DRY_RUN", "0") == "1"
AUTO_EXECUTE = os.environ.get("APIS_AUTO_EXECUTE", "0") == "1"


# ── Domain routing ─────────────────────────────────────────────────────────────
#
# Domain tags → T4 skill mappings.
# Tags are inferred from the task title/body by neotoma-agent's due-date
# hygiene step (which runs before Apis sees the task.updated event after
# the correction is applied). Apis uses the snapshot's tags field.
#
# Phase 4 skeleton: all routes log intent only.
# Phase 5: full subprocess dispatch via `claude --print --skill <skill>`.

_DOMAIN_ROUTES: dict[str, str] = {
    "finance": "monedula",  # payment execution — handed off to Monedula
    "ops": "gryllus",  # ops/deploy tasks → issue worker
    "engineering": "gryllus",  # engineering tasks → issue worker
    "agents": "gryllus",  # agent/swarm tasks → issue worker
    "neotoma": "gryllus",  # neotoma-repo tasks → issue worker
    "product": "gryllus",  # product/design tasks → issue worker
    "comms": "gryllus",  # comms tasks → issue worker
}

# Domain keyword patterns (mirrors neotoma-agent's patterns — kept in sync manually
# until a shared lib is extracted in Phase 5)
_DOMAIN_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(
            r"\b(payment|invoice|transfer|wage|salary|rent|yoga|therapy)\b", re.I
        ),
        "finance",
    ),
    (
        re.compile(r"\b(deploy|release|build|ci|pipeline|docker|kubernetes)\b", re.I),
        "ops",
    ),
    (
        re.compile(r"\b(bug|fix|error|crash|exception|regression|test)\b", re.I),
        "engineering",
    ),
    (
        re.compile(r"\b(design|ux|ui|figma|wireframe|mockup|copy|content)\b", re.I),
        "product",
    ),
    (
        re.compile(r"\b(neotoma|schema|entity|migration|api|endpoint)\b", re.I),
        "neotoma",
    ),
    (
        re.compile(r"\b(agent|daemon|skill|swarm|formica|apus|tyto|anthus)\b", re.I),
        "agents",
    ),
    (re.compile(r"\b(email|newsletter|telegram|social|post|draft)\b", re.I), "comms"),
]


def _infer_tags_from_text(title: str, body: str = "") -> list[str]:
    """Infer domain tags from task title + body (fallback when snapshot lacks tags)."""
    text = f"{title} {body}"
    tags: list[str] = []
    for pattern, tag in _DOMAIN_PATTERNS:
        if pattern.search(text) and tag not in tags:
            tags.append(tag)
    return tags


def _resolve_skill(tags: list[str]) -> str | None:
    """
    Pick the T4 skill for a task based on its domain tags.
    First match wins; returns None if no tag maps to a route.
    """
    for tag in tags:
        skill = _DOMAIN_ROUTES.get(tag)
        if skill:
            return skill
    return None


def _resolve_assignee(snapshot: dict, tags: list[str]) -> str | None:
    """
    Resolve the executing agent for a task.

    Routing is assign-at-creation: prefer the task's `assigned_to` field, set by
    whoever created the task via the agent-routing lookup. Apis is the FALLBACK
    router only — when `assigned_to` is unset or explicitly "apis", fall back to
    tag-based inference. (See .claude/rules/agent-routing.md.)
    """
    assigned = (snapshot.get("assigned_to") or "").strip().lower()
    if assigned and assigned != "apis":
        return assigned
    # Fallback: infer from domain tags (Apis acting as escalation/ambiguity router)
    return _resolve_skill(tags)


# Map a resolved skill/agent + task signals to a coarse action_type, used by the
# gate to classify blast radius. Conservative: anything that opens PRs, releases,
# pays, or sends comms is high blast.
_AGENT_ACTION_TYPE: dict[str, str] = {
    "gryllus": "open_pr",  # opens PRs against shared repos
    "vanellus": "merge_pr",  # merges to main
    "struthio": "release",  # publishes releases
    "monedula": "payment",  # moves money
    "corvus": "send_external_comms",  # posts publicly (when not draft-only)
}


def _infer_action_type(skill: str | None, snapshot: dict) -> str | None:
    """Best-effort action_type for the gate. Explicit task field wins."""
    explicit = (snapshot.get("action_type") or "").strip().lower()
    if explicit:
        return explicit
    if skill:
        return _AGENT_ACTION_TYPE.get(skill.lower())
    return None


def _read_confidence(snapshot: dict) -> float:
    """
    Read the agent-supplied confidence score (0..1) from the task snapshot.

    Until executing agents write their self-scored confidence back onto the task,
    Apis cannot know it at dispatch time. Absent an explicit score we return 0.0
    so the gate fails CLOSED (checkpoint) for any non-low-blast action — the
    operator is asked rather than the swarm guessing.
    """
    raw = snapshot.get("confidence", snapshot.get("confidence_score"))
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return 0.0


def _successful_recurrences(snapshot: dict) -> int:
    raw = snapshot.get("successful_recurrences", 0)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


# ── T4 dispatch ────────────────────────────────────────────────────────────────


async def dispatch_task(
    entity_id: str, snapshot: dict, trigger: str, notifier: Notifier | None = None
) -> None:
    """
    Route a task to its executing agent and apply the execution gate.

    Routing (assign-at-creation): prefer the task's `assigned_to` field; fall back
    to domain-tag inference only when unset/"apis" (Apis as escalation router).

    Gating (confidence × blast radius): before dispatching, resolve the governing
    execution_policy (Monedula gets the strict override), classify the action's
    blast radius, read the agent-supplied confidence, and evaluate the gate. If the
    gate says CHECKPOINT, write a blocking checkpoint_brief and notify the operator
    instead of executing.

    Phase 4: logs intent / writes checkpoints only.
    Phase 5: spawn `claude --print --skill <skill>` with task context on auto-execute.

    Args:
        entity_id: Neotoma entity ID of the task
        snapshot:  Current task snapshot
        trigger:   Event that triggered dispatch ("created", "updated", "due_today")
        notifier:  Optional notifier for operator checkpoint alerts
    """
    title = snapshot.get("title", "(untitled)")

    # Prefer tags already in snapshot (set by neotoma-agent hygiene)
    existing_tags: list[str] = snapshot.get("tags", []) or []
    if isinstance(existing_tags, str):
        import json as _json

        try:
            existing_tags = _json.loads(existing_tags)
        except (ValueError, TypeError):
            existing_tags = []

    # Fall back to local inference if tags not set yet
    if not existing_tags:
        body = snapshot.get("body", "") or snapshot.get("description", "")
        existing_tags = _infer_tags_from_text(title, body)

    # Assign-at-creation: assigned_to wins; tags are the fallback router.
    skill = _resolve_assignee(snapshot, existing_tags)

    if skill is None:
        log.info(
            f"[{DAEMON_NAME}] No route for task {entity_id!r} "
            f"(trigger={trigger}, assigned_to={snapshot.get('assigned_to')!r}, "
            f"tags={existing_tags}) — skipping dispatch"
        )
        return

    # ── Execution gate ──────────────────────────────────────────────────────
    policy = resolve_policy_for_agent(skill)
    action_type = _infer_action_type(skill, snapshot)
    confidence = _read_confidence(snapshot)
    decision = evaluate_gate(
        confidence=confidence,
        action_type=action_type,
        policy=policy,
        successful_recurrences=_successful_recurrences(snapshot),
    )

    log.info(
        f"[{DAEMON_NAME}] gate: task={entity_id} → {skill} "
        f"action={action_type} blast={decision.blast_radius.value} "
        f"conf={confidence:.2f}/{decision.threshold:.2f} "
        f"→ {decision.action.value} ({decision.reason}) policy={decision.policy_id}"
    )

    if decision.action != GateAction.AUTO_EXECUTE:
        brief_id = write_checkpoint_brief(
            task_entity_id=entity_id,
            decision=decision,
            title=title,
            plan_summary=(
                f"Assigned to {skill}. Action: {action_type or 'unknown'}. "
                f"Trigger: {trigger}. {decision.reason}."
            ),
            handler=DAEMON_NAME,
            alternatives=(
                ["Re-scope to a lower-blast action", "Provide missing inputs", "Decline"]
                if decision.action == GateAction.CHECKPOINT_WITH_ALTERNATIVES
                else None
            ),
        )
        if notifier is not None:
            notifier.send(
                f"PLAN checkpoint: {title[:70]}\n"
                f"  agent={skill} blast={decision.blast_radius.value} "
                f"conf={confidence:.2f} — {decision.reason}\n"
                f"  task={entity_id} brief={brief_id or '(unpersisted)'}",
                priority=Priority.BLOCKER,
                handler=DAEMON_NAME,
            )
        log.info(
            f"[{DAEMON_NAME}] HELD task {entity_id} for operator approval "
            f"(checkpoint_brief={brief_id})"
        )
        return

    log.info(
        f"[{DAEMON_NAME}] → {skill}: task={entity_id} trigger={trigger} "
        f"tags={existing_tags} title={title[:60]!r} (gate: auto-execute)"
    )

    if DRY_RUN:
        log.info(f"[{DAEMON_NAME}] DRY RUN — skipping {skill} dispatch for {entity_id}")
        return

    # Phase 5: full dispatch via claude --print
    # cmd = [
    #     "claude", "--print",
    #     "--skill", skill,
    #     "--input", json.dumps({
    #         "entity_id": entity_id,
    #         "snapshot": snapshot,
    #         "trigger": trigger,
    #     }),
    # ]
    # await asyncio.to_thread(subprocess.run, cmd, check=False, capture_output=True)


# ── Event handler ─────────────────────────────────────────────────────────────


async def handle_event(event: NeotomaEvent, notifier: Notifier) -> None:
    """
    Handle a Neotoma SSE task event.

    Phase 4:
      task.created   → dispatch to domain handler (dry-run)
      task.updated   → check status transitions; notify on due-date changes
      task.due_today → remind operator; auto-execute if APIS_AUTO_EXECUTE=1
    """
    entity_type = event.entity_type
    entity_id = event.entity_id
    action = event.action
    snapshot = event.snapshot or {}

    log.info(f"[{DAEMON_NAME}] Event: {entity_type}/{entity_id} action={action}")

    if entity_type != "task":
        # Defensive: SSE client filters by entity type, but guard here too
        return

    title = snapshot.get("title", "(untitled)")
    status = snapshot.get("status", "")

    if action == "created":
        notifier.send(
            f"Task created: {title[:80]}\n  {entity_id}",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )
        await dispatch_task(entity_id, snapshot, trigger="created", notifier=notifier)

    elif action == "updated":
        # Watch for approval transitions (Phase 5: will auto-execute)
        if status in ("approved", "ready"):
            log.info(
                f"[{DAEMON_NAME}] Task {entity_id} moved to status={status!r} — "
                "Phase 5 will trigger execution"
            )
        # Watch for due-date changes (raw payload may include a changed_fields list)
        changed = event.raw.get("changed_fields") or []
        if "due_date" in changed:
            new_due = snapshot.get("due_date", "")
            log.info(f"[{DAEMON_NAME}] Task {entity_id} due_date changed → {new_due}")

    elif action == "due_today":
        notifier.send(
            f"Task due today: {title[:80]}\n  {entity_id}",
            priority=Priority.BLOCKER,
            handler=DAEMON_NAME,
        )
        if AUTO_EXECUTE:
            log.info(
                f"[{DAEMON_NAME}] AUTO_EXECUTE=1 — dispatching due task {entity_id}"
            )
            await dispatch_task(
                entity_id, snapshot, trigger="due_today", notifier=notifier
            )
        else:
            log.info(
                f"[{DAEMON_NAME}] AUTO_EXECUTE off — operator notification sent for {entity_id}"
            )


# ── Main ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    log.info(f"[{DAEMON_NAME}] Starting up (Phase 4 skeleton)...")
    log.info(f"[{DAEMON_NAME}] ateles_repo={ATELES_REPO}")
    log.info(f"[{DAEMON_NAME}] dry_run={DRY_RUN} auto_execute={AUTO_EXECUTE}")

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
    notifier = Notifier.from_neotoma()
    notifier.send(
        f"{DAEMON_NAME} started (Phase 4: task dispatch skeleton, dry_run={DRY_RUN})",
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
