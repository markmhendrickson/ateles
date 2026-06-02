#!/usr/bin/env python3
"""
Turdus — Ateles email triage daemon.

Turdus genus: thrushes. T3 daemon in the Ateles swarm.

Polls Gmail at a configurable interval, digests incoming email into Neotoma
entities, and creates tasks for actionable messages. Feeds the task stream
that Apis and neotoma-agent then process downstream.

Triage pipeline (Phase 4 skeleton):
  1. Poll Gmail for unread messages in INBOX since last_seen_id
  2. For each message: extract sender, subject, snippet, labels
  3. Classify: actionable (→ task.created) / informational (→ note) / noise (skip)
  4. Store email_message entity in Neotoma
  5. Create task entity (audience=agent) for actionable messages
  6. Archive or label the Gmail message as processed

Phase 4: skeleton with Gmail polling stub and entity creation.
Phase 7: full classification pipeline with LLM-based triage skill.

AAuth sub: turdus@ateles-swarm
Startup sequence (T3 daemon pattern):
  1. Load env from ~/.config/neotoma/.env
  2. Load agent_definition from Neotoma via lib/daemon_runtime
  3. Load AAuth signer
  4. Load priority_rubric from Neotoma via lib/notify
  5. Poll Gmail and triage on schedule

Environment variables:
  NEOTOMA_BEARER_TOKEN          Neotoma API auth token
  NEOTOMA_BASE_URL              Neotoma API base URL
  TELEGRAM_BOT_TOKEN            Telegram bot token
  TELEGRAM_CHAT_ID              Telegram chat ID
  TELEGRAM_TOPIC_TURDUS         Telegram topic ID for Turdus notifications (optional)
  TURDUS_AGENT_DEFINITION_ID    Neotoma entity ID for Turdus's agent_definition (optional)
  TURDUS_POLL_INTERVAL          Polling interval in seconds (default: 300 = 5 minutes)
  TURDUS_DRY_RUN                Set to "1" to log without writing to Neotoma or Gmail
  TURDUS_MAX_MESSAGES           Max messages to process per poll cycle (default: 20)
  GWS_CREDENTIALS_PATH          Path to gws credentials JSON (default: ~/.config/gws/credentials.json)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

# ── Path bootstrap ────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.daemon_runtime import (  # noqa: E402
    AAuthSigner,
    AgentLoader,
    SSEClient,  # noqa: F401 — imported for consistency; Turdus uses polling not SSE
)
from lib.notify import Notifier, Priority  # noqa: E402

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("turdus")

# ── Config ────────────────────────────────────────────────────────────────────
DAEMON_NAME = "turdus"

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
).rstrip("/")
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

POLL_INTERVAL = int(os.environ.get("TURDUS_POLL_INTERVAL", "300"))  # 5 minutes
DRY_RUN = os.environ.get("TURDUS_DRY_RUN", "0") == "1"
MAX_MESSAGES = int(os.environ.get("TURDUS_MAX_MESSAGES", "20"))

GWS_CREDENTIALS_PATH = Path(
    os.environ.get(
        "GWS_CREDENTIALS_PATH",
        str(Path.home() / ".config" / "gws" / "credentials.json"),
    )
)

# State file to track last processed message ID across restarts
_STATE_FILE = Path(__file__).parent / ".turdus_state.json"

# ── Classification rules (Phase 4: keyword-based; Phase 7: LLM) ───────────────

# Sender patterns that produce tasks
_ACTIONABLE_SENDER_KEYWORDS = [
    "invoice",
    "billing",
    "payment",
    "receipt",
    "bank",
    "noreply@github.com",
    "notifications@github.com",
]

# Subject patterns that produce tasks
_ACTIONABLE_SUBJECT_KEYWORDS = [
    "action required",
    "please review",
    "invoice",
    "receipt",
    "payment",
    "due",
    "urgent",
    "deadline",
    "reminder",
    "review requested",
    "pull request",
    "issue assigned",
]

# Patterns to skip entirely (noise)
_NOISE_PATTERNS = [
    "unsubscribe",
    "newsletter",
    "promotional",
    "no-reply@accounts.google",
    "noreply@medium.com",
]

# ── Invoice detection ─────────────────────────────────────────────────────────

# Subject keywords that indicate an invoice / payment request
_INVOICE_SUBJECT_KEYWORDS = [
    "factura",
    "invoice",
    "receipt",
    "billing",
    "payment due",
    "amount due",
    "cobro",
    "pagament",
    "your bill",
    "statement",
]

# Sender domain/address fragments that indicate an invoice
_INVOICE_SENDER_KEYWORDS = [
    "billing@",
    "invoices@",
    "accounts@",
    "facturacion@",
    "florslloveras.com",
    "supabase.com",
    "paypal",
    "stripe",
    "xero",
    "quickbooks",
]


def _is_invoice(sender: str, subject: str, snippet: str) -> bool:
    """Return True if this message is an invoice or payment request."""
    subj_lower = subject.lower()
    sender_lower = sender.lower()
    for kw in _INVOICE_SUBJECT_KEYWORDS:
        if kw in subj_lower:
            return True
    for kw in _INVOICE_SENDER_KEYWORDS:
        if kw in sender_lower:
            return True
    return False


def _classify_message(sender: str, subject: str, snippet: str) -> str:
    """
    Classify a Gmail message into one of: actionable | informational | noise

    Phase 4: simple keyword matching.
    Phase 7: LLM-based classification via `claude --print` invocation.
    """
    text_lower = f"{sender} {subject} {snippet}".lower()

    # Noise first
    for pattern in _NOISE_PATTERNS:
        if pattern in text_lower:
            return "noise"

    # Actionable check
    for keyword in _ACTIONABLE_SENDER_KEYWORDS:
        if keyword in sender.lower():
            return "actionable"

    for keyword in _ACTIONABLE_SUBJECT_KEYWORDS:
        if keyword in subject.lower():
            return "actionable"

    return "informational"


# ── State management ──────────────────────────────────────────────────────────


def _load_state() -> dict:
    """Load persisted state (last_message_id, etc.) from local state file."""
    if _STATE_FILE.exists():
        try:
            return json.loads(_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_message_id": None, "processed_count": 0}


def _save_state(state: dict) -> None:
    """Persist state to local state file."""
    if DRY_RUN:
        return
    try:
        _STATE_FILE.write_text(json.dumps(state, indent=2))
    except OSError as exc:
        log.warning(f"[{DAEMON_NAME}] Failed to save state: {exc}")


# ── Gmail poll via gws CLI ────────────────────────────────────────────────────


def _poll_gmail_messages(max_count: int) -> list[dict]:
    """
    Poll Gmail for unread messages in INBOX using the `gws gmail` CLI.

    Returns a list of dicts with: id, sender, subject, snippet, date_iso.

    Uses `gws gmail +triage --format json` which returns:
      {"messages": [{"id", "from", "subject", "date"}, ...], "query": ..., "resultSizeEstimate": ...}

    Fields are normalised to the internal shape expected by the rest of the daemon:
      id, sender (from "from"), subject, snippet (empty — not provided by +triage),
      date_iso (from "date").
    """
    try:
        result = subprocess.run(
            [
                "gws",
                "gmail",
                "+triage",
                "--max",
                str(max_count),
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.warning(
                f"[{DAEMON_NAME}] gws gmail +triage failed (rc={result.returncode}): "
                f"{result.stderr[:200]}"
            )
            return []

        data = json.loads(result.stdout)
        raw_messages = data.get("messages", []) if isinstance(data, dict) else data
        if not isinstance(raw_messages, list):
            log.warning(
                f"[{DAEMON_NAME}] Unexpected gws output format: {type(raw_messages)}"
            )
            return []

        # Normalise field names to internal shape
        messages = []
        for msg in raw_messages:
            messages.append({
                "id": msg.get("id", ""),
                "sender": msg.get("from", ""),
                "subject": msg.get("subject", "(no subject)"),
                "snippet": "",  # +triage does not expose snippet
                "date_iso": msg.get("date", ""),
                "labels": msg.get("labels", []),
            })
        return messages

    except FileNotFoundError:
        log.warning(
            f"[{DAEMON_NAME}] gws CLI not found — Gmail polling unavailable. "
            "Install gws and configure credentials to enable Turdus."
        )
        return []
    except subprocess.TimeoutExpired:
        log.warning(f"[{DAEMON_NAME}] gws gmail +triage timed out after 30s")
        return []
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(f"[{DAEMON_NAME}] Gmail poll error: {exc}")
        return []


def _label_gmail_message(message_id: str, label: str) -> bool:
    """
    Apply a Gmail label to a processed message via gws CLI.

    Phase 4: skeleton — label with 'Turdus/processed'.
    """
    if DRY_RUN:
        log.info(f"[{DAEMON_NAME}] DRY RUN — would label {message_id} with {label!r}")
        return True
    try:
        result = subprocess.run(
            ["gws", "gmail", "messages", "label", message_id, "--add", label],
            capture_output=True,
            text=True,
            timeout=15,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return False


# ── Neotoma writes ────────────────────────────────────────────────────────────


async def _store_email_entity(message: dict) -> str | None:
    """
    Create an email_message entity in Neotoma for a Gmail message.

    Returns the entity_id, or None on failure.
    """
    import httpx

    if not NEOTOMA_BEARER_TOKEN:
        log.debug(f"[{DAEMON_NAME}] No NEOTOMA_BEARER_TOKEN — skipping Neotoma write")
        return None

    if DRY_RUN:
        log.info(
            f"[{DAEMON_NAME}] DRY RUN — would store email entity for {message.get('id')}"
        )
        return None

    payload = {
        "entity_type": "email_message",
        "canonical_name": f"email_message:gmail:{message.get('id', 'unknown')}",
        "snapshot": {
            "message_id": message.get("id", ""),
            "sender": message.get("sender", ""),
            "subject": message.get("subject", ""),
            "snippet": message.get("snippet", ""),
            "date": message.get("date_iso", ""),
            "labels": message.get("labels", []),
            "classification": message.get("classification", "informational"),
            "source": "gmail",
        },
    }

    try:
        async with httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=15,
        ) as client:
            resp = await client.post(f"{NEOTOMA_BASE_URL}/observations", json=payload)
            resp.raise_for_status()
            data = resp.json()
            entity_id = data.get("entity_id") or (
                data.get("entities", [{}])[0].get("entity_id")
            )
            log.info(f"[{DAEMON_NAME}] Stored email_message entity {entity_id}")
            return entity_id
    except Exception as exc:
        log.error(f"[{DAEMON_NAME}] Failed to store email entity: {exc}")
        return None


async def _create_task_for_email(message: dict, email_entity_id: str | None) -> None:
    """
    Create a Neotoma task entity for an actionable email.

    Invoices/receipts/payment requests are routed directly to Monedula with
    priority=urgent, bypassing general Apis dispatch. All other actionable
    emails create a standard agent-audience task routed through Apis.
    """
    import httpx

    if not NEOTOMA_BEARER_TOKEN:
        return

    subject = message.get("subject", "(no subject)")
    sender = message.get("sender", "")
    snippet = message.get("snippet", "")
    is_invoice = _is_invoice(sender, subject, snippet)

    if is_invoice:
        task_title = f"Invoice/payment: {subject[:80]}"
        snapshot = {
            "title": task_title,
            "body": (
                f"Invoice or payment request detected by Turdus.\n"
                f"From: {sender}\n"
                f"Subject: {subject}\n"
                f"Snippet: {snippet[:200]}\n"
                f"Source: Gmail message ID {message.get('id', '')}"
            ),
            "audience": "agent",
            "assigned_to": "monedula",
            "priority": "urgent",
            "status": "open",
            "source": "turdus:gmail",
            "domain_tags": ["finance", "payment"],
        }
        log_label = "INVOICE→monedula"
    else:
        task_title = f"Email triage: {subject[:80]}"
        snapshot = {
            "title": task_title,
            "body": (
                f"Actionable email from {sender}.\n"
                f"Subject: {subject}\n"
                f"Snippet: {snippet[:200]}\n"
                f"Source: Gmail message ID {message.get('id', '')}"
            ),
            "audience": "agent",
            "status": "open",
            "source": "turdus:gmail",
        }
        log_label = "actionable"

    if DRY_RUN:
        log.info(
            f"[{DAEMON_NAME}] DRY RUN — would create {log_label} task for email "
            f"from {sender!r}: {subject[:60]!r}"
        )
        return

    payload = {
        "entity_type": "task",
        "canonical_name": f"task:turdus:email:{message.get('id', 'unknown')}",
        "snapshot": snapshot,
    }

    try:
        async with httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=15,
        ) as client:
            resp = await client.post(f"{NEOTOMA_BASE_URL}/observations", json=payload)
            resp.raise_for_status()
            data = resp.json()
            task_id = data.get("entity_id") or (
                data.get("entities", [{}])[0].get("entity_id")
            )
            log.info(
                f"[{DAEMON_NAME}] Created task {task_id} for email from {sender!r}"
            )

            # Link task REFERS_TO email entity
            if email_entity_id and task_id:
                rel_payload = {
                    "from_entity_id": task_id,
                    "to_entity_id": email_entity_id,
                    "relationship_type": "REFERS_TO",
                }
                await client.post(f"{NEOTOMA_BASE_URL}/relationships", json=rel_payload)

    except Exception as exc:
        log.error(f"[{DAEMON_NAME}] Failed to create task for email: {exc}")


# ── Poll cycle ────────────────────────────────────────────────────────────────


async def poll_once(notifier: Notifier, state: dict) -> dict:
    """
    Run a single Gmail poll-and-triage cycle.

    Returns updated state dict.
    """
    log.info(f"[{DAEMON_NAME}] Polling Gmail (max={MAX_MESSAGES} messages)...")

    messages = _poll_gmail_messages(MAX_MESSAGES)

    if not messages:
        log.info(f"[{DAEMON_NAME}] No new messages found")
        return state

    last_seen_id = state.get("last_message_id")
    new_messages: list[dict] = []

    for msg in messages:
        msg_id = msg.get("id", "")
        if msg_id == last_seen_id:
            break
        new_messages.append(msg)

    if not new_messages:
        log.info(f"[{DAEMON_NAME}] No messages newer than last_seen_id={last_seen_id}")
        return state

    log.info(f"[{DAEMON_NAME}] Processing {len(new_messages)} new message(s)")

    actionable_count = 0
    invoice_count = 0
    for msg in new_messages:
        sender = msg.get("sender", msg.get("from", ""))
        subject = msg.get("subject", "(no subject)")
        snippet = msg.get("snippet", "")

        classification = _classify_message(sender, subject, snippet)
        msg["classification"] = classification
        is_invoice = _is_invoice(sender, subject, snippet)

        label = "INVOICE→monedula" if is_invoice else classification.upper()
        log.info(
            f"[{DAEMON_NAME}] {label}: from={sender[:40]!r} "
            f"subject={subject[:60]!r}"
        )

        if classification == "noise":
            continue

        # Store email entity in Neotoma
        email_entity_id = await _store_email_entity(msg)

        if classification == "actionable" or is_invoice:
            if is_invoice:
                invoice_count += 1
            else:
                actionable_count += 1
            await _create_task_for_email(msg, email_entity_id)
            _label_gmail_message(msg.get("id", ""), "Turdus/processed")

    # Update state with newest processed message ID
    if new_messages:
        state["last_message_id"] = new_messages[0].get("id")
        state["processed_count"] = state.get("processed_count", 0) + len(new_messages)
        state["last_poll_at"] = datetime.now(UTC).isoformat()

    if invoice_count > 0:
        notifier.send(
            f"{DAEMON_NAME}: {invoice_count} invoice(s) → urgent task(s) created for monedula",
            priority=Priority.BLOCKER,
            handler=DAEMON_NAME,
        )
    if actionable_count > 0:
        notifier.send(
            f"{DAEMON_NAME}: {actionable_count} actionable email(s) → tasks created",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )

    _save_state(state)
    return state


# ── Main ──────────────────────────────────────────────────────────────────────


async def main() -> None:
    log.info(f"[{DAEMON_NAME}] Starting up (Phase 4 skeleton)...")
    log.info(
        f"[{DAEMON_NAME}] poll_interval={POLL_INTERVAL}s "
        f"max_messages={MAX_MESSAGES} "
        f"dry_run={DRY_RUN}"
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
        f"{DAEMON_NAME} started (Phase 4: email triage skeleton, dry_run={DRY_RUN})",
        priority=Priority.INFO,
        handler=DAEMON_NAME,
    )

    # 4. Load persisted state
    state = _load_state()
    log.info(
        f"[{DAEMON_NAME}] State loaded: last_message_id={state.get('last_message_id')} "
        f"processed_count={state.get('processed_count', 0)}"
    )

    # 5. Poll loop
    log.info(f"[{DAEMON_NAME}] Starting poll loop (interval={POLL_INTERVAL}s)...")
    while True:
        try:
            state = await poll_once(notifier, state)
        except Exception as exc:
            log.error(f"[{DAEMON_NAME}] Poll cycle error: {exc}", exc_info=True)
            notifier.send(
                f"{DAEMON_NAME} poll error: {exc}",
                priority=Priority.BLOCKER,
                handler=DAEMON_NAME,
            )

        await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info(f"[{DAEMON_NAME}] Stopped by operator.")
