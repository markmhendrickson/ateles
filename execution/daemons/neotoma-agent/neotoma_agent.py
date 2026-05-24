#!/usr/bin/env python3
"""
neotoma-agent — Neotoma-repo automation daemon.

Castor genus: beavers. T3 daemon in the Ateles swarm.

Subscribes to Neotoma issue/PR/task events and processes them:
  - task.created → due-date hygiene T4 skill (add due_date + domain tags)
  - issue.created → log and notify; Phase 5 will spawn Gryllus
  - pull_request.created → log and notify; Phase 5 will spawn Vanellus

AAuth sub: neotoma-agent@ateles-swarm
Phase 3: due-date hygiene for tasks added; issue/PR dispatch skeleton retained.

Startup sequence (T3 daemon pattern):
  1. Load env from ~/.config/neotoma/.env
  2. Load agent_definition from Neotoma via lib/daemon_runtime
  3. Load AAuth signer
  4. Load priority_rubric from Neotoma via lib/notify
  5. Subscribe to Neotoma SSE and process events

Environment variables:
  NEOTOMA_BEARER_TOKEN          Neotoma API auth token
  NEOTOMA_BASE_URL              Neotoma API base URL (default: https://neotoma.markmhendrickson.com)
  TELEGRAM_BOT_TOKEN            Telegram bot token
  TELEGRAM_CHAT_ID              Telegram chat ID
  TELEGRAM_TOPIC_NEOTOMA_AGENT  Telegram topic ID for neotoma-agent notifications (optional)
  NEOTOMA_AGENT_DEFINITION_ID   Neotoma entity ID for neotoma-agent's agent_definition (optional)
  NEOTOMA_AGENT_REPO            GitHub repo slug for automation (default: markmhendrickson/neotoma)
  DUE_DATE_HYGIENE_ENABLED      Set to "0" to disable due-date hygiene (default: enabled)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path

import httpx

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
log = logging.getLogger("neotoma-agent")

# ── Config ────────────────────────────────────────────────────────────────────
DAEMON_NAME = "neotoma-agent"
SUBSCRIBE_ENTITY_TYPES = ["issue", "pull_request", "task"]

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
).rstrip("/")
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

# Neotoma repo slug for GitHub automation (Phase 5)
NEOTOMA_REPO = os.environ.get("NEOTOMA_AGENT_REPO", "markmhendrickson/neotoma")

# Due-date hygiene feature flag
DUE_DATE_HYGIENE_ENABLED = os.environ.get("DUE_DATE_HYGIENE_ENABLED", "1") != "0"

# Triage label filtering for issue/PR dispatch.
# When set, neotoma-agent only acts on issues/PRs that carry this label.
# Format: "triage:neotoma-agent". Empty string = no filter (act on all).
NEOTOMA_AGENT_TRIAGE_LABEL = os.environ.get(
    "NEOTOMA_AGENT_TRIAGE_LABEL", "triage:neotoma-agent"
)


def _issue_has_triage_label(snapshot: dict) -> bool:
    """Return True if the snapshot's labels include NEOTOMA_AGENT_TRIAGE_LABEL."""
    if not NEOTOMA_AGENT_TRIAGE_LABEL:
        return True
    raw_labels = snapshot.get("labels", [])
    if isinstance(raw_labels, str):
        try:
            raw_labels = json.loads(raw_labels)
        except (ValueError, TypeError):
            raw_labels = [lbl.strip() for lbl in raw_labels.split(",") if lbl.strip()]
    labels_lower = {str(lbl).lower() for lbl in raw_labels}
    return NEOTOMA_AGENT_TRIAGE_LABEL.lower() in labels_lower


# ── Due-date hygiene ───────────────────────────────────────────────────────────


# Domain keyword → tag mapping for auto-tagging tasks
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


def _infer_domain_tags(title: str, body: str = "") -> list[str]:
    """Infer domain tags from task title and body text."""
    text = f"{title} {body}"
    tags: list[str] = []
    for pattern, tag in _DOMAIN_PATTERNS:
        if pattern.search(text) and tag not in tags:
            tags.append(tag)
    return tags


def _default_due_date(title: str, domain_tags: list[str]) -> str:
    """
    Compute a sensible default due_date for a new task.

    Rules (in priority order):
    1. finance/payment tasks → next business day
    2. ops/engineering tasks → 3 days out
    3. everything else → 7 days out
    """
    today = date.today()

    if "finance" in domain_tags:
        # Next business day (skip weekends)
        delta = timedelta(days=1)
        candidate = today + delta
        while candidate.weekday() >= 5:  # 5=Sat, 6=Sun
            candidate += timedelta(days=1)
        return candidate.isoformat()

    if "ops" in domain_tags or "engineering" in domain_tags:
        return (today + timedelta(days=3)).isoformat()

    return (today + timedelta(days=7)).isoformat()


async def apply_due_date_hygiene(entity_id: str, snapshot: dict) -> None:
    """
    T4 skill: apply due-date and domain-tag hygiene to a newly created task.

    - If due_date is missing, set a sensible default.
    - Append any inferred domain tags not already present.
    - Uses Neotoma corrections API (field-level correction, audit-safe).
    """
    if not NEOTOMA_BEARER_TOKEN:
        log.debug(
            f"[{DAEMON_NAME}] NEOTOMA_BEARER_TOKEN not set — skipping due-date hygiene"
        )
        return

    title = snapshot.get("title", "")
    body = snapshot.get("body", "") or snapshot.get("description", "")
    existing_due = snapshot.get("due_date", "")
    existing_tags: list[str] = snapshot.get("tags", []) or []
    if isinstance(existing_tags, str):
        try:
            existing_tags = json.loads(existing_tags)
        except (ValueError, TypeError):
            existing_tags = []

    corrections: list[dict] = []

    # 1. Due date
    if not existing_due:
        domain_tags = _infer_domain_tags(title, body)
        due_date = _default_due_date(title, domain_tags)
        log.info(
            f"[{DAEMON_NAME}] Hygiene: setting due_date={due_date} for task {entity_id} "
            f"(domains={domain_tags})"
        )
        corrections.append(
            {
                "entity_id": entity_id,
                "field_name": "due_date",
                "corrected_value": due_date,
                "correction_note": f"neotoma-agent: auto due-date (domain={','.join(domain_tags) or 'general'})",
            }
        )

        # 2. Domain tags (only when we're also setting due_date to avoid double-tagging)
        new_tags = [t for t in domain_tags if t not in existing_tags]
        if new_tags:
            merged_tags = existing_tags + new_tags
            corrections.append(
                {
                    "entity_id": entity_id,
                    "field_name": "tags",
                    "corrected_value": merged_tags,
                    "correction_note": f"neotoma-agent: auto domain tags ({','.join(new_tags)})",
                }
            )

    if not corrections:
        log.debug(
            f"[{DAEMON_NAME}] Hygiene: task {entity_id} already has due_date — skipping"
        )
        return

    headers = {
        "Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(headers=headers, timeout=15) as client:
        for correction in corrections:
            try:
                resp = await client.post(
                    f"{NEOTOMA_BASE_URL}/corrections",
                    json=correction,
                )
                resp.raise_for_status()
                log.info(
                    f"[{DAEMON_NAME}] Correction applied: {correction['field_name']}={correction['corrected_value']!r} "
                    f"→ {entity_id}"
                )
            except httpx.HTTPStatusError as exc:
                log.error(
                    f"[{DAEMON_NAME}] Correction failed (HTTP {exc.response.status_code}) "
                    f"for {entity_id} field={correction['field_name']}: {exc.response.text[:200]}"
                )
            except Exception as exc:
                log.error(
                    f"[{DAEMON_NAME}] Correction error for {entity_id} field={correction['field_name']}: {exc}"
                )


# ── Event handler ─────────────────────────────────────────────────────────────


async def handle_event(event: NeotomaEvent, notifier: Notifier) -> None:
    """
    Handle a Neotoma SSE event.

    Phase 3:
      - task.created  → due-date hygiene T4 skill
      - issue.created → log + notify (Phase 5: spawn Gryllus)
      - pull_request.created → log + notify (Phase 5: spawn Vanellus)
    """
    entity_type = event.entity_type
    entity_id = event.entity_id
    action = event.action
    snapshot = event.snapshot or {}

    log.info(f"[{DAEMON_NAME}] Event: {entity_type}/{entity_id} action={action}")

    if entity_type == "task" and action == "created":
        title = snapshot.get("title", "(untitled)")
        log.info(f"[{DAEMON_NAME}] Task created: {title[:80]} ({entity_id})")
        if DUE_DATE_HYGIENE_ENABLED:
            await apply_due_date_hygiene(entity_id, snapshot)

    elif entity_type == "issue" and action == "created":
        title = snapshot.get("title", "(untitled)")
        audience = snapshot.get("audience", "?")
        if not _issue_has_triage_label(snapshot):
            log.debug(
                f"[{DAEMON_NAME}] Issue {entity_id} skipped — "
                f"missing label {NEOTOMA_AGENT_TRIAGE_LABEL!r}"
            )
            return
        notifier.send(
            f"New issue [{audience}]: {title[:80]}\n  {entity_id}",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )
        # Phase 5: check if targeted at neotoma repo → spawn Gryllus

    elif entity_type == "pull_request" and action == "created":
        title = snapshot.get("title", "(untitled)")
        if not _issue_has_triage_label(snapshot):
            log.debug(
                f"[{DAEMON_NAME}] PR {entity_id} skipped — "
                f"missing label {NEOTOMA_AGENT_TRIAGE_LABEL!r}"
            )
            return
        notifier.send(
            f"New PR: {title[:80]}\n  {entity_id}",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )
        # Phase 5: check if targeted at neotoma repo → spawn Vanellus


# ── Main ──────────────────────────────────────────────────────────────────────


async def main() -> None:
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
    notifier = Notifier.from_neotoma()

    log.info(
        f"[{DAEMON_NAME}] Due-date hygiene: {'enabled' if DUE_DATE_HYGIENE_ENABLED else 'disabled'}"
    )

    notifier.send(
        f"{DAEMON_NAME} started (Phase 3: due-date hygiene + issue/PR skeleton)",
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
