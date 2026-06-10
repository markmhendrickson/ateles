#!/usr/bin/env python3
"""
Formica — Ateles issue-processing daemon.

Formica genus: ants. T3 daemon in the Ateles swarm.

Subscribes to Neotoma SSE events and processes issues/PRs by dispatching
to T4 invocable agents (Gryllus for issues, Vanellus for PRs).

This Python implementation uses lib/daemon_runtime for SSE and AAuth,
replacing the legacy Node.js daemon.mjs. The operator approval flow
(Telegram long-poll + /shipit command) and full PR automation pipeline
are preserved in the Node.js codebase for now and will migrate in Phase 5.

Migration status:
  Phase 3: Python entry point + SSE subscription + lib/notify + lib/daemon_runtime
  Phase 5: Full PR pipeline (worktree management, cursor_sdk_runner, operator_queue)

AAuth sub: formica@ateles-swarm
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
  TELEGRAM_TOPIC_FORMICA      Telegram topic ID for Formica notifications
  FORMICA_AGENT_DEFINITION_ID Neotoma entity ID for Formica's agent_definition
  FORMICA_DRY_RUN             Set to "1" to log events without dispatching agents
  FORMICA_TRIAGE_LABEL        Issue label that gates dispatch (default: "triage:formica")
  FORMICA_CLAUDE_BIN          Absolute path to `claude` binary (default: auto-detect on PATH)
  FORMICA_DISPATCH_TIMEOUT    Per-dispatch timeout in seconds (default: 1800)
  ATELES_REPO_PATH            Local path to ateles clone (default: ~/repos/ateles)
  NEOTOMA_REPO_PATH           Local path to neotoma clone (default: ~/repos/neotoma)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path

# ── Path bootstrap ────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime import (  # noqa: E402
    AAuthSigner,
    AgentLoader,
    GrantChecker,
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
log = logging.getLogger("formica")

# ── Activity-log channel (CyphorhinusBot observation feed) ──────────────────
_activity = ActivityLogger(agent="formica")

# ── Config ────────────────────────────────────────────────────────────────────
DAEMON_NAME = "formica"

SUBSCRIBE_ENTITY_TYPES = ["issue", "pull_request", "product_feedback"]

# Repo paths (used when spawning T4 agents)
ATELES_REPO = Path(
    os.environ.get("ATELES_REPO_PATH", str(Path.home() / "repos" / "ateles"))
)
NEOTOMA_REPO = Path(
    os.environ.get("NEOTOMA_REPO_PATH", str(Path.home() / "repos" / "neotoma"))
)

DRY_RUN = os.environ.get("FORMICA_DRY_RUN", "0") == "1"

# Skills used for T4 dispatch (via `claude --print`)
GRYLLUS_SKILL = "gryllus"  # issue worker
VANELLUS_SKILL = "vanellus"  # PR steward

# Path to the Claude CLI binary used to spawn T4 agents. Set by env var or
# auto-detected from PATH. If absent, dispatch falls back to log-only.
CLAUDE_BIN = os.environ.get("FORMICA_CLAUDE_BIN") or shutil.which("claude")

# Dispatch timeout per agent invocation (seconds).
DISPATCH_TIMEOUT_SECONDS = int(os.environ.get("FORMICA_DISPATCH_TIMEOUT", "1800"))

# Triage label filtering.
# When set, Formica only dispatches issues/PRs that carry this label.
# Format: "triage:formica" — matches the label convention triage:<agent>.
# If empty/unset, Formica dispatches all issues regardless of triage label.
FORMICA_TRIAGE_LABEL = os.environ.get("FORMICA_TRIAGE_LABEL", "triage:formica")


def _has_triage_label(snapshot: dict) -> bool:
    """
    Return True if the event's labels include FORMICA_TRIAGE_LABEL.

    If FORMICA_TRIAGE_LABEL is empty, always returns True (no filter).
    Labels may be a list or a comma-separated string in the snapshot.
    """
    if not FORMICA_TRIAGE_LABEL:
        return True

    raw_labels = snapshot.get("labels", [])
    if isinstance(raw_labels, str):
        import json as _json

        try:
            raw_labels = _json.loads(raw_labels)
        except (ValueError, TypeError):
            raw_labels = [lbl.strip() for lbl in raw_labels.split(",") if lbl.strip()]

    labels_lower = {str(lbl).lower() for lbl in raw_labels}
    return FORMICA_TRIAGE_LABEL.lower() in labels_lower


# ── T4 agent dispatch ──────────────────────────────────────────────────────────


async def _spawn_claude_skill(
    skill: str,
    entity_id: str,
    snapshot: dict,
    notifier: Notifier,
    participation_ref: str = "",
) -> None:
    """
    Spawn a T4 agent via `claude --print --skill <name>` with the entity
    context piped on stdin as JSON.

    Failures are reported via lib/notify/ and logged but do not crash
    Formica — one bad issue must not take down the daemon.
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

    # Load SKILL.md for the agent — `claude --print` does not accept a --skill
    # flag; the working pattern is --append-system-prompt with the SKILL.md
    # content (confirmed in Tier 1 smoke test, 2026-05-25).
    skill_path = (
        Path(os.environ.get("ATELES_REPO_PATH", str(Path.home() / "repos" / "ateles")))
        / ".claude"
        / "skills"
        / skill
        / "SKILL.md"
    )
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

    title = snapshot.get("title", "")
    repo = snapshot.get("repository") or snapshot.get("repo") or ""
    number = snapshot.get("number") or snapshot.get("issue_number") or ""
    prompt = (
        f"Invoke the {skill} agent per your appended system prompt.\n\n"
        f"GitHub issue {repo}#{number}: {title}\n\n"
        f"{snapshot.get('body', '')}\n\n"
        f"Work entity: {entity_id}."
    )

    cmd = [CLAUDE_BIN, "--print", "--append-system-prompt", skill_md]
    log.info(
        f"[{DAEMON_NAME}] Spawning: claude --print --append-system-prompt <{skill}.SKILL.md> "
        f"timeout={DISPATCH_TIMEOUT_SECONDS}s entity={entity_id}"
    )

    # Pass ATELES_PARTICIPATION_REF so the mcpsrv_neotoma MCP server can stamp
    # retrieval_event entities keyed to this dispatch (#23 — auto retrieval attribution).
    subprocess_env = {**os.environ}
    if participation_ref:
        subprocess_env["ATELES_PARTICIPATION_REF"] = participation_ref

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=subprocess_env,
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


async def dispatch_gryllus(
    entity_id: str, snapshot: dict, notifier: Notifier, grants: GrantChecker
) -> None:
    """Dispatch Gryllus (issue worker) for a new issue."""
    if grants.is_suspended():
        log.warning(f"[{DAEMON_NAME}] Grant suspended — skipping Gryllus dispatch for {entity_id}")
        return

    title = snapshot.get("title", "(untitled)")
    repo = snapshot.get("repository", "unknown")
    log.info(
        f"[{DAEMON_NAME}] → Gryllus: issue={entity_id} repo={repo!r} "
        f"title={title[:60]!r}"
    )

    if DRY_RUN:
        log.info(f"[{DAEMON_NAME}] DRY RUN — skipping Gryllus dispatch for {entity_id}")
        return

    job = _activity.started(f"dispatching Gryllus for issue {entity_id} ({repo}): {title[:60]}")
    try:
        await _spawn_claude_skill(GRYLLUS_SKILL, entity_id, snapshot, notifier, entity_id)
        job.finished(f"Gryllus dispatched for issue {entity_id}")
    except Exception as exc:
        job.failed(f"Gryllus dispatch failed for {entity_id}: {type(exc).__name__}")
        raise


async def dispatch_vanellus(
    entity_id: str, snapshot: dict, notifier: Notifier, grants: GrantChecker
) -> None:
    """Dispatch Vanellus (PR steward) for a new PR."""
    if grants.is_suspended():
        log.warning(f"[{DAEMON_NAME}] Grant suspended — skipping Vanellus dispatch for {entity_id}")
        return

    title = snapshot.get("title", "(untitled)")
    log.info(f"[{DAEMON_NAME}] → Vanellus: pr={entity_id} title={title[:60]!r}")

    if DRY_RUN:
        log.info(
            f"[{DAEMON_NAME}] DRY RUN — skipping Vanellus dispatch for {entity_id}"
        )
        return

    job = _activity.started(f"dispatching Vanellus for PR {entity_id}: {title[:60]}")
    try:
        await _spawn_claude_skill(VANELLUS_SKILL, entity_id, snapshot, notifier, entity_id)
        job.finished(f"Vanellus dispatched for PR {entity_id}")
    except Exception as exc:
        job.failed(f"Vanellus dispatch failed for {entity_id}: {type(exc).__name__}")
        raise


# ── Event handler ─────────────────────────────────────────────────────────────


async def handle_event(event: NeotomaEvent, notifier: Notifier, grants: GrantChecker) -> None:
    """
    Handle a Neotoma SSE event.

    Phase 3:
      - issue.created  → dispatch Gryllus (dry-run in Phase 3)
      - pull_request.created → dispatch Vanellus (dry-run in Phase 3)
      - product_feedback.created → log (Sturnus handles this in Phase 4)

    Phase 5: full subprocess dispatch with operator Telegram approval loop.
    """
    entity_type = event.entity_type
    entity_id = event.entity_id
    action = event.action
    snapshot = event.snapshot or {}

    log.info(f"[{DAEMON_NAME}] Event: {entity_type}/{entity_id} action={action}")

    if entity_type == "issue" and action == "created":
        title = snapshot.get("title", "(untitled)")
        audience = snapshot.get("audience", "human")
        priority_level = snapshot.get("priority", "medium")

        if not _has_triage_label(snapshot):
            log.debug(
                f"[{DAEMON_NAME}] Issue {entity_id} skipped — "
                f"missing label {FORMICA_TRIAGE_LABEL!r}"
            )
            return

        notifier.send(
            f"Issue [{audience}/{priority_level}]: {title[:80]}\n  {entity_id}",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )
        await dispatch_gryllus(entity_id, snapshot, notifier, grants)

    elif entity_type == "pull_request" and action == "created":
        title = snapshot.get("title", "(untitled)")

        if not _has_triage_label(snapshot):
            log.debug(
                f"[{DAEMON_NAME}] PR {entity_id} skipped — "
                f"missing label {FORMICA_TRIAGE_LABEL!r}"
            )
            return

        notifier.send(
            f"PR: {title[:80]}\n  {entity_id}",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )
        await dispatch_vanellus(entity_id, snapshot, notifier, grants)

    elif entity_type == "product_feedback" and action == "created":
        log.info(
            f"[{DAEMON_NAME}] product_feedback {entity_id} received — "
            "Sturnus will handle this in Phase 4"
        )

    elif entity_type == "issue" and action == "updated":
        # Log updates; Phase 5 will check if operator responded to open PRs
        log.debug(f"[{DAEMON_NAME}] Issue updated: {entity_id}")


# ── Main ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    log.info(f"[{DAEMON_NAME}] Starting up (Python migration — Phase 3)...")
    log.info(f"[{DAEMON_NAME}] ateles_repo={ATELES_REPO} neotoma_repo={NEOTOMA_REPO}")
    log.info(
        f"[{DAEMON_NAME}] dry_run={DRY_RUN} claude_bin={CLAUDE_BIN or '<not-found>'}"
    )
    log.info(f"[{DAEMON_NAME}] dispatch_timeout={DISPATCH_TIMEOUT_SECONDS}s")

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

    # 2b. Check agent_grant status — abort startup if suspended or revoked.
    grants = GrantChecker(agent_def.aauth_sub).load()
    if grants.is_revoked():
        log.error(
            f"[{DAEMON_NAME}] Agent grant is revoked — daemon cannot start. "
            "Re-consent required via: python execution/scripts/manage_grants.py restore <id>"
        )
        sys.exit(1)
    if grants.is_suspended():
        log.warning(
            f"[{DAEMON_NAME}] Agent grant is suspended — dispatch disabled. "
            "Restore via: python execution/scripts/manage_grants.py restore <id>"
        )
        # Continue running (SSE still streams) but dispatch will be a no-op.

    # 3. Load notification rubric
    notifier = Notifier.from_neotoma()
    notifier.send(
        f"{DAEMON_NAME} started (Python Phase 3, dry_run={DRY_RUN})",
        priority=Priority.INFO,
        handler=DAEMON_NAME,
    )

    # 4. Subscribe to SSE events
    sse = SSEClient(
        entity_types=SUBSCRIBE_ENTITY_TYPES,
        handler_name=DAEMON_NAME,
    )

    async def dispatch(event: NeotomaEvent) -> None:
        await handle_event(event, notifier, grants)

    log.info(f"[{DAEMON_NAME}] Subscribing to SSE: {SUBSCRIBE_ENTITY_TYPES}")
    await sse.stream(dispatch)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info(f"[{DAEMON_NAME}] Stopped by operator.")
