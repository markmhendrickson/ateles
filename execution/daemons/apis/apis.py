#!/usr/bin/env python3
"""
Apis — Ateles universal task dispatcher daemon.

Apis genus: honeybees. T3 daemon in the Ateles swarm.

Subscribes to Neotoma task events and dispatches to appropriate T4 agents
based on domain tags. Replaces Monedula's task-dispatch scope; Monedula
retains its payment-execution and calendar-detection logic.

Dispatch routing:
  task.created   → tag inference + subprocess dispatch to domain handler
  task.updated   → check for status transitions (e.g. approved → execute)
  task.due_today → remind operator; auto-execute if APIS_AUTO_EXECUTE=1

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
  APIS_CLAUDE_BIN             Path to the claude CLI (default: autodetect on PATH)
  APIS_DISPATCH_TIMEOUT       Per-dispatch timeout in seconds (default: 1800)
  ATELES_REPO_PATH            Local path to ateles clone (default: ~/repos/ateles)
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
from pathlib import Path

# ── Path bootstrap ────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Daemons run as standalone scripts (`python apis.py`), so there is no parent
# package for relative imports. Add this daemon's own directory to sys.path so
# sibling modules (routing, a2a_executor, a2a_gateway) import as top-level.
_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

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
from lib.daemon_runtime.gating import (  # noqa: E402
    checkpoint_already_dispatched,
    fetch_task_snapshot,
    mark_task_declined,
    read_checkpoint_resolution,
    stamp_checkpoint_dispatched,
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

SUBSCRIBE_ENTITY_TYPES = ["task", "checkpoint_brief"]

# Coarse action_type per resolved skill — feeds the execution gate's blast-radius
# classification. Conservative: anything that opens PRs, releases, pays, or posts
# publicly is high blast. An explicit task.action_type field overrides this.
# Values MUST match the execution_policy's high/low_blast_action_types vocabulary
# (default policy ent_dfce6edecefe3eb7fc9e0337) or the gate mis-classifies blast
# radius. PR open and merge both map to the policy's "open_or_merge_pr"; "release"
# is treated as high blast via blast_radius_default + the policy's publish set.
_AGENT_ACTION_TYPE: dict[str, str] = {
    "gryllus": "open_or_merge_pr",
    "vanellus": "open_or_merge_pr",
    "struthio": "publish",
    "monedula": "payment",
    "fringilla": "compute_only_analysis",
    "corvus": "send_external_comms",
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
    Read the agent-supplied confidence (0..1) from the task snapshot. Absent an
    explicit score, return 0.0 so the gate fails CLOSED (checkpoint) for any
    non-low-blast action — the operator is asked rather than the swarm guessing.
    """
    raw = snapshot.get("confidence", snapshot.get("confidence_score"))
    try:
        return max(0.0, min(1.0, float(raw)))
    except (TypeError, ValueError):
        return 0.0


def _successful_recurrences(snapshot: dict) -> int:
    try:
        return max(0, int(snapshot.get("successful_recurrences", 0)))
    except (TypeError, ValueError):
        return 0

ATELES_REPO = Path(
    os.environ.get("ATELES_REPO_PATH", str(Path.home() / "repos" / "ateles"))
)

DRY_RUN = os.environ.get("APIS_DRY_RUN", "0") == "1"
AUTO_EXECUTE = os.environ.get("APIS_AUTO_EXECUTE", "0") == "1"

# Path to the Claude CLI binary used to spawn T4 agents. Set by env var or
# auto-detected from PATH. If absent, dispatch falls back to log-only.
CLAUDE_BIN = os.environ.get("APIS_CLAUDE_BIN") or shutil.which("claude")

# Dispatch timeout per agent invocation (seconds).
DISPATCH_TIMEOUT_SECONDS = int(os.environ.get("APIS_DISPATCH_TIMEOUT", "1800"))


# ── Domain routing ─────────────────────────────────────────────────────────────
#
# Domain tags → T4 skill mappings live in routing.py, shared with the A2A
# gateway (a2a_executor.py) so inbound A2A tasks and SSE-sourced tasks route
# through one source of truth. Tags are inferred from the task title/body
# (neotoma-agent's due-date hygiene may set them first; Apis falls back to
# local inference). Each tag maps to a T4 skill dispatched via `claude --print`
# (see _spawn_claude_skill). Set APIS_DRY_RUN=1 to log intent without spawning.

from routing import (  # noqa: E402
    infer_tags_from_text as _infer_tags_from_text,
    resolve_skill as _resolve_skill,
)


# ── T4 dispatch ────────────────────────────────────────────────────────────────


async def _spawn_claude_skill(
    skill: str,
    entity_id: str,
    snapshot: dict,
    trigger: str,
    notifier: Notifier,
) -> None:
    """
    Spawn a T4 agent via `claude --print` with the SKILL.md appended to the
    system prompt and the task context piped on stdin.

    `claude --print` has no --skill flag; the working pattern (mirrors Formica
    and neotoma-agent) is --append-system-prompt with the SKILL.md content.

    Failures are reported via lib/notify and logged but never crash Apis —
    one bad task must not take down the dispatcher.
    """
    if CLAUDE_BIN is None:
        log.warning(
            f"[{DAEMON_NAME}] CLAUDE_BIN not configured and `claude` not on "
            f"PATH; skipping {skill} dispatch for {entity_id}."
        )
        notifier.send(
            f"{skill} dispatch skipped — claude binary unavailable",
            priority=Priority.WARN,
            handler=DAEMON_NAME,
        )
        return

    skill_path = ATELES_REPO / ".claude" / "skills" / skill / "SKILL.md"
    if not skill_path.exists():
        log.error(f"[{DAEMON_NAME}] SKILL.md not found for {skill} at {skill_path}")
        notifier.send(
            f"{skill} dispatch skipped — SKILL.md not found",
            priority=Priority.WARN,
            handler=DAEMON_NAME,
        )
        return

    try:
        skill_md = skill_path.read_text(encoding="utf-8")
    except OSError as exc:
        log.error(f"[{DAEMON_NAME}] failed to read {skill_path}: {exc}")
        return

    title = snapshot.get("title", "(untitled)")
    body = snapshot.get("body", "") or snapshot.get("description", "")
    prompt = (
        f"Invoke the {skill} agent per your appended system prompt.\n\n"
        f"Task {entity_id} (trigger={trigger}): {title}\n\n"
        f"{body}".strip()
    )

    cmd = [CLAUDE_BIN, "--print", "--append-system-prompt", skill_md]
    log.info(
        f"[{DAEMON_NAME}] Spawning: claude --print --append-system-prompt "
        f"<{skill}.SKILL.md> timeout={DISPATCH_TIMEOUT_SECONDS}s entity={entity_id}"
    )

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode()),
            timeout=DISPATCH_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        log.error(
            f"[{DAEMON_NAME}] {skill} dispatch timed out after "
            f"{DISPATCH_TIMEOUT_SECONDS}s for {entity_id}"
        )
        notifier.send(
            f"{skill} timed out on {entity_id}",
            priority=Priority.WARN,
            handler=DAEMON_NAME,
        )
        return

    if proc.returncode == 0:
        log.info(
            f"[{DAEMON_NAME}] {skill} dispatch ok for {entity_id} "
            f"({len(stdout)}B stdout)"
        )
    else:
        stderr_text = stderr.decode("utf-8", errors="replace")[:500]
        log.error(
            f"[{DAEMON_NAME}] {skill} dispatch failed (rc={proc.returncode}) "
            f"for {entity_id}: {stderr_text}"
        )
        notifier.send(
            f"{skill} failed on {entity_id} (rc={proc.returncode})",
            priority=Priority.WARN,
            handler=DAEMON_NAME,
        )


async def dispatch_task(
    entity_id: str,
    snapshot: dict,
    trigger: str,
    notifier: Notifier,
    gate_override: bool = False,
) -> None:
    """
    Route a task to the appropriate T4 skill and spawn it via `claude --print`.

    Applies the confidence × blast-radius execution gate before spawning: a
    non-auto-execute decision writes a blocking checkpoint_brief and notifies the
    operator instead of executing. `gate_override=True` skips the gate — used when
    re-dispatching a task whose checkpoint the operator has explicitly approved.

    Args:
        entity_id:     Neotoma entity ID of the task
        snapshot:      Current task snapshot
        trigger:       Event that triggered dispatch ("created", "due_today", "approved")
        notifier:      Notifier for dispatch-failure + checkpoint alerts
        gate_override: When True, bypass the gate (operator already approved)
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

    # An explicit assigned_to (set by Sylvia/Turdus) wins over tag inference.
    assigned_to = snapshot.get("assigned_to") or None
    skill = _resolve_skill(existing_tags, assigned_to=assigned_to)

    if skill is None:
        log.info(
            f"[{DAEMON_NAME}] No route for task {entity_id!r} "
            f"(trigger={trigger}, tags={existing_tags}, assigned_to={assigned_to}) "
            "— skipping dispatch"
        )
        return

    # ── Execution gate ──────────────────────────────────────────────────────
    # Skipped when re-dispatching an operator-approved checkpoint.
    if not gate_override:
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
            f"→ {decision.action.value} ({decision.reason})"
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
        f"tags={existing_tags} title={title[:60]!r}"
        + (" (gate: override)" if gate_override else " (gate: auto-execute)")
    )

    if DRY_RUN:
        log.info(f"[{DAEMON_NAME}] DRY RUN — skipping {skill} dispatch for {entity_id}")
        return

    await _spawn_claude_skill(skill, entity_id, snapshot, trigger, notifier)


# ── Checkpoint resolution ───────────────────────────────────────────────────


async def handle_checkpoint_brief(
    entity_id: str, snapshot: dict, notifier: Notifier
) -> None:
    """
    React to a checkpoint_brief the gate raised once the operator resolves it.

    approved → re-dispatch the referenced task with the gate bypassed (the
               operator IS the approval the gate was waiting for).
    rejected → mark the task declined; do not execute.
    pending/unknown → no-op (waiting on the operator).

    Idempotency: after acting, the brief is stamped resolved_dispatched=true; a
    replayed approved/rejected event whose brief carries that stamp is a no-op.
    Re-dispatch is also safe because the task skill owns its own idempotency, but
    the stamp avoids spawning the work twice on SSE redelivery.
    """
    resolution = read_checkpoint_resolution(snapshot)
    if resolution is None:
        log.info(
            f"[{DAEMON_NAME}] checkpoint_brief {entity_id} still pending "
            f"(status={snapshot.get('status')!r}) — no action"
        )
        return

    if checkpoint_already_dispatched(snapshot):
        log.info(
            f"[{DAEMON_NAME}] checkpoint_brief {entity_id} already dispatched "
            f"(resolution={resolution}) — no-op on replay"
        )
        return

    task_id = snapshot.get("task_entity_id")
    if not task_id:
        log.warning(
            f"[{DAEMON_NAME}] checkpoint_brief {entity_id} {resolution} but has no "
            "task_entity_id — cannot act"
        )
        return

    title = snapshot.get("title", "(untitled)")

    if resolution == "rejected":
        mark_task_declined(
            task_id, reason=f"operator rejected checkpoint {entity_id}", handler=DAEMON_NAME
        )
        stamp_checkpoint_dispatched(entity_id, handler=DAEMON_NAME)
        notifier.send(
            f"Checkpoint rejected: {title[:70]}\n  task={task_id} declined",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )
        return

    # approved → re-dispatch with the gate bypassed
    task_snapshot = fetch_task_snapshot(task_id)
    if task_snapshot is None:
        log.warning(
            f"[{DAEMON_NAME}] checkpoint {entity_id} approved but task {task_id} "
            "could not be fetched — not dispatching"
        )
        notifier.send(
            f"Checkpoint approved but task {task_id} unreachable — manual dispatch needed",
            priority=Priority.WARN,
            handler=DAEMON_NAME,
        )
        return

    log.info(
        f"[{DAEMON_NAME}] checkpoint {entity_id} APPROVED — re-dispatching task "
        f"{task_id} with gate override"
    )
    notifier.send(
        f"Checkpoint approved: {title[:70]}\n  re-dispatching task {task_id}",
        priority=Priority.INFO,
        handler=DAEMON_NAME,
    )
    # Stamp before dispatch so an SSE replay can't double-spawn the work; the task
    # skill's own idempotency covers the rare stamp-succeeded-then-dispatch-crashed case.
    stamp_checkpoint_dispatched(entity_id, handler=DAEMON_NAME)
    await dispatch_task(
        task_id, task_snapshot, trigger="approved", notifier=notifier, gate_override=True
    )


# ── Event handler ─────────────────────────────────────────────────────────────


async def handle_event(event: NeotomaEvent, notifier: Notifier) -> None:
    """
    Handle a Neotoma SSE task event.

      task.created   → dispatch to domain handler
      task.updated   → check status transitions; notify on due-date changes
      task.due_today → remind operator; auto-execute if APIS_AUTO_EXECUTE=1
    """
    entity_type = event.entity_type
    entity_id = event.entity_id
    action = event.action
    snapshot = event.snapshot or {}

    log.info(f"[{DAEMON_NAME}] Event: {entity_type}/{entity_id} action={action}")

    if entity_type == "checkpoint_brief":
        await handle_checkpoint_brief(entity_id, snapshot, notifier)
        return

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
        # Tasks dispatch on creation (and on due_today when AUTO_EXECUTE is set);
        # status transitions are logged for observability only to avoid
        # re-dispatching work already routed at creation.
        if status in ("approved", "ready"):
            log.info(
                f"[{DAEMON_NAME}] Task {entity_id} moved to status={status!r}"
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
    log.info(f"[{DAEMON_NAME}] Starting up...")
    log.info(f"[{DAEMON_NAME}] ateles_repo={ATELES_REPO}")
    log.info(
        f"[{DAEMON_NAME}] dry_run={DRY_RUN} auto_execute={AUTO_EXECUTE} "
        f"claude_bin={CLAUDE_BIN or '<none>'} dispatch_timeout={DISPATCH_TIMEOUT_SECONDS}s"
    )

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
        f"{DAEMON_NAME} started (task dispatch, dry_run={DRY_RUN})",
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
