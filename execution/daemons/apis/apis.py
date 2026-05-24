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


# ── T4 dispatch ────────────────────────────────────────────────────────────────


async def dispatch_task(entity_id: str, snapshot: dict, trigger: str) -> None:
    """
    Route a task to the appropriate T4 skill.

    Phase 4: logs intent only.
    Phase 5: spawn `claude --print --skill <skill>` with task context.

    Args:
        entity_id: Neotoma entity ID of the task
        snapshot:  Current task snapshot
        trigger:   Event that triggered dispatch ("created", "updated", "due_today")
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

    skill = _resolve_skill(existing_tags)

    if skill is None:
        log.info(
            f"[{DAEMON_NAME}] No route for task {entity_id!r} "
            f"(trigger={trigger}, tags={existing_tags}) — skipping dispatch"
        )
        return

    log.info(
        f"[{DAEMON_NAME}] → {skill}: task={entity_id} trigger={trigger} "
        f"tags={existing_tags} title={title[:60]!r}"
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
        await dispatch_task(entity_id, snapshot, trigger="created")

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
            await dispatch_task(entity_id, snapshot, trigger="due_today")
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
