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
import re
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
    enforce_status_or_exit,
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

NEOTOMA_BASE_URL = os.environ.get("NEOTOMA_BASE_URL", "").rstrip("/")
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

POLL_INTERVAL = int(os.environ.get("TURDUS_POLL_INTERVAL", "300"))  # 5 minutes
DRY_RUN = os.environ.get("TURDUS_DRY_RUN", "0") == "1"
MAX_MESSAGES = int(os.environ.get("TURDUS_MAX_MESSAGES", "20"))

# ── Swarm PR-approval-by-email (approval loop) ────────────────────────────────
# When the operator replies APPROVE to a swarm "READY TO MERGE" notification,
# Turdus resolves the PR from the correlation token and POSTs to the Apis
# gateway's loopback /approve-email route, which routes to the same gated merge
# path as a GitHub review or /approve comment. Env-gated: without the operator
# address AND the shared secret, this path is inert.
OPERATOR_EMAIL = os.environ.get("OPERATOR_EMAIL", "").strip().lower()
APIS_APPROVE_URL = os.environ.get(
    "APIS_APPROVE_EMAIL_URL", "http://127.0.0.1:8742/approve-email"
)
APIS_APPROVE_EMAIL_SECRET = os.environ.get("APIS_APPROVE_EMAIL_SECRET", "")
# Marker identifying a swarm merge-ready notification (present in the subject
# the operator is replying to). Kept in sync with the Apis notification text.
_MERGE_READY_SUBJECT_MARKER = "READY TO MERGE"
_APPROVE_TOKEN_PREFIX = "swarm-approve:"

# ── Release email-approval (mirrors the PR swarm-approve flow) ────────────────
# A prepared release RC email (from phoenicurus prepare) carries a stable token
# `release-approve: <version>` and the subject marker below. When the operator
# replies `approve <version>`, Turdus POSTs the version to the Apis
# /approve-release loopback route, which drives publish.py — the same effect as
# a Telegram `approve <version>`. Exact-version-token matching (operator ruling
# 2026-07-27) prevents a stale reply from an old release thread ever triggering
# the wrong publish.
APIS_APPROVE_RELEASE_URL = os.environ.get(
    "APIS_APPROVE_RELEASE_URL", "http://127.0.0.1:8742/approve-release"
)
_RELEASE_READY_SUBJECT_MARKER = (
    "ready to approve"  # matches "🚀 Release <TAG> ready to approve"
)
_RELEASE_APPROVE_TOKEN_PREFIX = "release-approve:"

GWS_CREDENTIALS_PATH = Path(
    os.environ.get(
        "GWS_CREDENTIALS_PATH",
        str(Path.home() / ".config" / "gws" / "credentials.json"),
    )
)

# State file to track last processed message ID across restarts
_STATE_FILE = Path(__file__).parent / ".turdus_state.json"

# Gmail label applied to messages Turdus has fully handled. Used both to tag
# processed mail and to exclude it from the poll query, so an already-handled
# invoice never re-enters the unread set and re-notifies the operator.
PROCESSED_LABEL = "Turdus/processed"

# Cap on how many processed Gmail message IDs we retain in state for
# notification dedup. Bounds the state file; older IDs age out (they are also
# excluded by the poll label filter, so aging out is safe).
_MAX_PROCESSED_IDS = 500

# Gmail label NAME -> label ID, resolved lazily. messages.modify takes IDs.
_LABEL_ID_CACHE: dict[str, str] = {}

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
      {"messages": [{"id", "from", "subject", "date"}, ...], ...}

    Fields are normalised to the internal shape:
      id, sender (from "from"), subject, snippet (empty), date_iso (from "date").
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
                # Exclude messages Turdus has already handled. Without this the
                # default `is:unread` set keeps returning the same still-unread
                # invoice every poll, re-notifying the operator each cycle.
                "--query",
                f"is:unread in:inbox -label:{PROCESSED_LABEL}",
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
            messages.append(
                {
                    "id": msg.get("id", ""),
                    "sender": msg.get("from", ""),
                    "subject": msg.get("subject", "(no subject)"),
                    "snippet": "",  # +triage does not expose snippet
                    "date_iso": msg.get("date", ""),
                    "labels": msg.get("labels", []),
                }
            )
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


def _gws_json(args: list[str], timeout: int = 15) -> dict | None:
    """
    Run a `gws` command expected to emit JSON on stdout and parse it.

    The CLI prefixes stdout with a keyring banner line, so drop any leading
    lines before the first '{'. Returns None on any failure (fail-open: the
    caller degrades rather than crashing the poll loop).
    """
    try:
        result = subprocess.run(
            ["gws", *args], capture_output=True, text=True, timeout=timeout
        )
        if result.returncode != 0:
            log.warning(
                f"[{DAEMON_NAME}] gws {' '.join(args[:3])} failed "
                f"(rc={result.returncode}): {(result.stderr or '').strip()[:200]}"
            )
            return None
        out = result.stdout
        brace = out.find("{")
        if brace == -1:
            return None
        return json.loads(out[brace:])
    except (
        FileNotFoundError,
        subprocess.TimeoutExpired,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        log.warning(f"[{DAEMON_NAME}] gws {' '.join(args[:3])} error: {exc}")
        return None


def _resolve_label_id(label_name: str) -> str | None:
    """
    Resolve a Gmail label NAME to its label ID, creating the label if absent.

    Gmail's messages.modify takes label IDs, not names. Cached per process.
    Returns None if the label can neither be found nor created.
    """
    cached = _LABEL_ID_CACHE.get(label_name)
    if cached:
        return cached

    listing = _gws_json(
        ["gmail", "users", "labels", "list", "--params", '{"userId":"me"}']
    )
    if listing:
        for lbl in listing.get("labels", []):
            if lbl.get("name") == label_name:
                label_id = lbl.get("id")
                if label_id:
                    _LABEL_ID_CACHE[label_name] = label_id
                    return label_id

    # Absent — create it. This is why the label never stuck before: nothing
    # ever created 'Turdus/processed', so no message could carry it.
    created = _gws_json(
        [
            "gmail",
            "users",
            "labels",
            "create",
            "--params",
            '{"userId":"me"}',
            "--json",
            json.dumps(
                {
                    "name": label_name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                }
            ),
        ]
    )
    label_id = (created or {}).get("id")
    if label_id:
        log.info(f"[{DAEMON_NAME}] created Gmail label {label_name!r} (id={label_id})")
        _LABEL_ID_CACHE[label_name] = label_id
        return label_id

    log.warning(f"[{DAEMON_NAME}] could not resolve or create label {label_name!r}")
    return None


def _label_gmail_message(message_id: str, label: str) -> bool:
    """
    Mark a processed message as handled: add the processed label AND remove the
    UNREAD label, via Gmail's messages.modify.

    Both are required to stop the re-notification loop. The label is what the
    poll query filters on (`-label:Turdus/processed`); removing UNREAD keeps the
    default `is:unread` basis truthful so a handled invoice cannot reappear even
    if label propagation lags.

    Failures are logged rather than swallowed: this call previously shelled out
    to `gws gmail messages label`, a subcommand that does not exist, so every
    label write failed silently and the processed label was never applied — the
    mechanical root cause of the re-notification loop.
    """
    if not message_id:
        return False
    if DRY_RUN:
        log.info(
            f"[{DAEMON_NAME}] DRY RUN — would label {message_id} with {label!r} "
            "and mark read"
        )
        return True

    label_id = _resolve_label_id(label)
    if not label_id:
        return False

    modified = _gws_json(
        [
            "gmail",
            "users",
            "messages",
            "modify",
            "--params",
            json.dumps({"userId": "me", "id": message_id}),
            "--json",
            json.dumps({"addLabelIds": [label_id], "removeLabelIds": ["UNREAD"]}),
        ]
    )
    if modified is None:
        log.warning(f"[{DAEMON_NAME}] failed to label/mark-read {message_id}")
        return False
    return True


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

    msg_id = message.get("id", "unknown")
    payload = {
        "entities": [
            {
                "entity_type": "email_message",
                "canonical_name": f"email_message:gmail:{msg_id}",
                "snapshot": {
                    "message_id": message.get("id", ""),
                    "sender": message.get("sender", ""),
                    "subject": message.get("subject", ""),
                    "snippet": message.get("snippet", ""),
                    "date": message.get("date_iso", ""),
                    "labels": message.get("labels", []),
                    "classification": message.get(
                        "classification", "informational"
                    ),
                    "source": "gmail",
                },
            }
        ],
        "idempotency_key": f"turdus-email-{msg_id}",
    }

    try:
        async with httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=15,
        ) as client:
            resp = await client.post(f"{NEOTOMA_BASE_URL}/store", json=payload)
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
            "description": (
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
            "domain": "finance",
            # ateles#682: declare what this task DOES and how sure we are, so
            # the execution gate classifies it on the work rather than on the
            # assignee. This one really does move money — HIGH by declaration,
            # and it SHOULD checkpoint. Confidence is deliberately low: Turdus
            # detected an invoice by keyword heuristic and has verified neither
            # the amount nor the payee, which is precisely the uncertainty the
            # gate exists to surface.
            "action_type": "payment",
            "confidence": 0.3,
            "confidence_drivers": (
                "Invoice detected by sender/subject keyword heuristic only. "
                "Amount, payee, and legitimacy unverified. Monedula must "
                "retrieve the payment_profile and confirm before acting."
            ),
        }
        log_label = "INVOICE→monedula"
    else:
        task_title = f"Email triage: {subject[:80]}"
        snapshot = {
            "title": task_title,
            "description": (
                f"Actionable email from {sender}.\n"
                f"Subject: {subject}\n"
                f"Snippet: {snippet[:200]}\n"
                f"Source: Gmail message ID {message.get('id', '')}"
            ),
            "audience": "agent",
            "status": "open",
            # ateles#682: Turdus's standing contract is that it STAGES Gmail
            # drafts and never sends — the send remains a human action. So the
            # honest action_type is "draft" (LOW blast), not
            # "send_external_comms". Confidence stays moderate: the email is
            # real and the triage is routine, but whether a reply is actually
            # warranted is a judgment the handling agent still makes.
            "action_type": "draft",
            "confidence": 0.6,
            "confidence_drivers": (
                "Email retrieved and classified actionable by Turdus's hourly "
                "sweep. Output is an unsent Gmail draft; delivery stays a "
                "human action. Whether a reply is warranted is unverified."
            ),
        }
        log_label = "actionable"

    if DRY_RUN:
        log.info(
            f"[{DAEMON_NAME}] DRY RUN — would create {log_label} task for email "
            f"from {sender!r}: {subject[:60]!r}"
        )
        return

    msg_id = message.get("id", "unknown")
    payload = {
        "entities": [
            {
                "entity_type": "task",
                "canonical_name": f"task:turdus:email:{msg_id}",
                "snapshot": snapshot,
            }
        ],
        "idempotency_key": f"turdus-task-{msg_id}",
    }

    try:
        async with httpx.AsyncClient(
            headers={
                "Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}",
                "Content-Type": "application/json",
            },
            timeout=15,
        ) as client:
            resp = await client.post(f"{NEOTOMA_BASE_URL}/store", json=payload)
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
                    "source_entity_id": task_id,
                    "target_entity_id": email_entity_id,
                    "relationship_type": "REFERS_TO",
                }
                await client.post(
                    f"{NEOTOMA_BASE_URL}/create_relationship", json=rel_payload
                )

    except Exception as exc:
        log.error(f"[{DAEMON_NAME}] Failed to create task for email: {exc}")


# ── Swarm PR-approval-by-email (approval loop) ────────────────────────────────


def _extract_sender_address(sender: str) -> str:
    """Return the bare email address from a `Name <addr@x>` sender, lowered."""
    m = re.search(r"<([^>]+)>", sender or "")
    addr = (m.group(1) if m else (sender or "")).strip().lower()
    return addr


def _parse_approve_token(text: str) -> tuple[str, int] | None:
    """Extract (repository, pr_number) from a `swarm-approve: owner/repo#N`
    correlation line. Returns None if absent/malformed. Mirrors
    swarm_dispatch.parse_approve_token so both ends agree on the format."""
    m = re.search(
        rf"{re.escape(_APPROVE_TOKEN_PREFIX)}\s*"
        r"([A-Za-z0-9._-]+/[A-Za-z0-9._-]+)#(\d+)",
        text or "",
    )
    if not m:
        return None
    return m.group(1), int(m.group(2))


def _reply_says_approve(body: str) -> bool:
    """True when the operator's reply expresses approval.

    Conservative: the word APPROVE must appear as the operator's OWN text, i.e.
    on a line that is not a quoted line (Gmail prefixes quotes with '>'). This
    avoids matching the word "APPROVE" inside the quoted original notification
    ("Reply APPROVE ... to merge"). Case-insensitive on the word itself.
    """
    for line in (body or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(">"):
            continue  # skip blank + quoted-original lines
        if re.search(r"\bapprove\b", stripped, re.IGNORECASE):
            return True
    return False


def _read_message_body(message_id: str) -> str:
    """Fetch a message's plaintext body via `gws gmail +read`. "" on failure."""
    if not message_id:
        return ""
    try:
        result = subprocess.run(
            ["gws", "gmail", "+read", "--id", message_id, "--format", "json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.warning(
                f"[{DAEMON_NAME}] gws +read failed (rc={result.returncode}) for "
                f"{message_id}: {(result.stderr or '')[:160]}"
            )
            return ""
        data = json.loads(_strip_keyring_preamble(result.stdout))
        # gws returns the body under a few possible keys depending on version.
        # Current gws `+read` uses `body_text` (plaintext) / `body_html`. The
        # older names are kept as fallbacks. `body_text` MUST be checked — its
        # absence here silently returned "" and made the release/PR approval
        # handlers see no body, so an operator `approve <version>` reply matched
        # the subject but "carried no token" and was never routed (observed on
        # the first live release-approval, 2026-07-27). Prefer plaintext; fall
        # back to HTML only if that's all that's present.
        for key in ("body_text", "body", "text", "plain", "body_html", "snippet"):
            val = data.get(key)
            if isinstance(val, str) and val:
                return val
        return ""
    except Exception as exc:  # noqa: BLE001 — best-effort, never crash the poll
        log.warning(f"[{DAEMON_NAME}] +read error for {message_id}: {exc}")
        return ""


def _strip_keyring_preamble(stdout: str) -> str:
    """gws sometimes prints a keyring line before the JSON. Return from the
    first '{' so json.loads sees clean JSON."""
    idx = stdout.find("{")
    return stdout[idx:] if idx >= 0 else stdout


async def _post_email_approval(repository: str, pr_number: int, sender: str) -> bool:
    """POST an operator email approval to the Apis /approve-email route.

    Returns True on a 2xx. Fail-open (returns False, never raises) so a
    transient Apis outage never crashes the Turdus poll loop."""
    if not APIS_APPROVE_EMAIL_SECRET:
        log.warning(
            f"[{DAEMON_NAME}] email-approve for {repository}#{pr_number} skipped — "
            "APIS_APPROVE_EMAIL_SECRET unset (fail closed)"
        )
        return False
    if DRY_RUN:
        log.info(
            f"[{DAEMON_NAME}] DRY_RUN — would POST email-approve for "
            f"{repository}#{pr_number}"
        )
        return True
    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                APIS_APPROVE_URL,
                headers={"X-Approve-Secret": APIS_APPROVE_EMAIL_SECRET},
                json={
                    "repository": repository,
                    "pr_number": pr_number,
                    "sender": sender,
                },
            )
            if 200 <= resp.status_code < 300:
                return True
            log.warning(
                f"[{DAEMON_NAME}] email-approve POST returned {resp.status_code} "
                f"for {repository}#{pr_number}: {(resp.text or '')[:160]}"
            )
            return False
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[{DAEMON_NAME}] email-approve POST error: {exc}")
        return False


async def _maybe_handle_swarm_approval(msg: dict, notifier: Notifier) -> bool:
    """If ``msg`` is an operator APPROVE reply to a swarm merge-ready email,
    resolve the PR and POST the approval to Apis. Returns True when handled
    (so the caller can skip normal task creation for this message).

    Guards, in order — all must hold:
      1. OPERATOR_EMAIL is configured AND the sender matches it (only the
         operator may approve; a spoof-friendly display name is ignored — we
         match the bare address).
      2. The subject carries the merge-ready marker (cheap pre-filter before we
         spend a +read body fetch).
      3. The body (fetched via +read) contains the swarm-approve correlation
         token (→ the exact PR) AND an unquoted APPROVE.
    """
    if not OPERATOR_EMAIL:
        return False
    sender_addr = _extract_sender_address(msg.get("sender", msg.get("from", "")))
    if sender_addr != OPERATOR_EMAIL:
        return False
    subject = msg.get("subject", "") or ""
    if _MERGE_READY_SUBJECT_MARKER not in subject:
        return False

    body = _read_message_body(msg.get("id", ""))
    token = _parse_approve_token(body) or _parse_approve_token(subject)
    if not token:
        log.info(
            f"[{DAEMON_NAME}] operator reply matched merge-ready subject but "
            "carried no swarm-approve token — treating as a normal reply"
        )
        return False
    if not _reply_says_approve(body):
        log.info(
            f"[{DAEMON_NAME}] operator reply to {token[0]}#{token[1]} did not say "
            "APPROVE — not merging (may be a question or a hold)"
        )
        return False

    repository, pr_number = token
    ok = await _post_email_approval(repository, pr_number, sender_addr)
    if ok:
        _label_gmail_message(msg.get("id", ""), "Turdus/processed")
        notifier.send(
            f"{DAEMON_NAME}: operator emailed APPROVE for {repository}#{pr_number} "
            "→ routed to Apis merge gate.",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )
        log.info(f"[{DAEMON_NAME}] email-approve routed for {repository}#{pr_number}")
    return ok


# ── Release email-approval ───────────────────────────────────────────────────


def _parse_release_approve_token(text: str) -> str | None:
    """Extract the version from a `release-approve: v1.2.3` correlation line.

    Returns the version string (with its leading `v`) or None. The version shape
    is a normal semver-ish tag so it can be matched exactly against the operator
    reply — a stale `approve` from an old release thread must not approve a
    different version.
    """
    m = re.search(
        rf"{re.escape(_RELEASE_APPROVE_TOKEN_PREFIX)}\s*(v[0-9][0-9A-Za-z.\-+]*)",
        text or "",
    )
    return m.group(1) if m else None


def _reply_approves_version(body: str, version: str) -> bool:
    """True when the operator's own (unquoted) text says `approve <version>`.

    Exact-version match (operator ruling 2026-07-27): the reply must name THIS
    release's version, so a bare `approve` or an `approve` for a different
    version never triggers this publish. The `v` prefix is optional in the
    operator's reply (`approve 0.20.0` and `approve v0.20.0` both count).
    """
    want = version.lstrip("v")
    # word-boundaried version, optional leading v, must sit after an `approve`.
    pat = re.compile(rf"\bapprove\b[^\n]*\bv?{re.escape(want)}\b", re.IGNORECASE)
    for line in (body or "").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(">"):
            continue  # skip blank + quoted-original lines
        if pat.search(stripped):
            return True
    return False


async def _post_release_approval(version: str, sender: str) -> bool:
    """POST an operator release approval to the Apis /approve-release route.

    Fail-open (returns False, never raises) so a transient Apis outage never
    crashes the Turdus poll loop. Fails CLOSED when the shared secret is unset —
    an email must never drive an irreversible publish without the secret
    configured on both daemons.
    """
    if not APIS_APPROVE_EMAIL_SECRET:
        log.warning(
            f"[{DAEMON_NAME}] release-approve for {version} skipped — "
            "APIS_APPROVE_EMAIL_SECRET unset (fail closed)"
        )
        return False
    if DRY_RUN:
        log.info(f"[{DAEMON_NAME}] DRY_RUN — would POST release-approve for {version}")
        return True
    import httpx

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                APIS_APPROVE_RELEASE_URL,
                headers={"X-Approve-Secret": APIS_APPROVE_EMAIL_SECRET},
                json={"version": version, "sender": sender},
            )
            if 200 <= resp.status_code < 300:
                return True
            log.warning(
                f"[{DAEMON_NAME}] release-approve POST returned {resp.status_code} "
                f"for {version}: {(resp.text or '')[:160]}"
            )
            return False
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[{DAEMON_NAME}] release-approve POST error: {exc}")
        return False


async def _maybe_handle_release_approval(msg: dict, notifier: Notifier) -> bool:
    """If ``msg`` is an operator `approve <version>` reply to a release RC email,
    POST the approval to Apis. Returns True when handled (caller skips normal
    task creation).

    Guards, all must hold:
      1. OPERATOR_EMAIL configured AND the sender matches it (bare address).
      2. Subject carries the release-ready marker (cheap pre-filter).
      3. Body carries the `release-approve: <version>` token AND the operator's
         own unquoted text says `approve <that exact version>`.
    """
    if not OPERATOR_EMAIL:
        return False
    sender_addr = _extract_sender_address(msg.get("sender", msg.get("from", "")))
    if sender_addr != OPERATOR_EMAIL:
        return False
    subject = msg.get("subject", "") or ""
    if _RELEASE_READY_SUBJECT_MARKER.lower() not in subject.lower():
        return False

    body = _read_message_body(msg.get("id", ""))
    version = _parse_release_approve_token(body) or _parse_release_approve_token(
        subject
    )
    if not version:
        log.info(
            f"[{DAEMON_NAME}] operator reply matched release subject but carried no "
            "release-approve token — treating as a normal reply"
        )
        return False
    if not _reply_approves_version(body, version):
        log.info(
            f"[{DAEMON_NAME}] operator reply to release {version} did not say "
            f"`approve {version}` — not publishing (a bare/other-version approve "
            "or a hold is ignored)"
        )
        return False

    ok = await _post_release_approval(version, sender_addr)
    if ok:
        _label_gmail_message(msg.get("id", ""), "Turdus/processed")
        notifier.send(
            f"{DAEMON_NAME}: operator emailed `approve {version}` → routed to the "
            "Apis release-publish gate.",
            priority=Priority.INFO,
            handler=DAEMON_NAME,
        )
        log.info(f"[{DAEMON_NAME}] release-approve routed for {version}")
    return ok


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

    # Per-message-ID dedup for the operator-facing notification. Even with the
    # poll label filter, a transient label-write failure could let a message
    # reappear; the notification must never fire twice for the same Gmail ID.
    # `processed_ids` is an ordered list (used as a bounded set) persisted in
    # state. Only genuinely-new IDs count toward the notification totals.
    processed_ids: list[str] = list(state.get("processed_ids", []))
    processed_id_set = set(processed_ids)

    def _mark_handled(mid: str) -> None:
        """Record a message ID as terminally handled for cross-cycle dedup.

        Covers EVERY disposition that `continue`s out of the loop — approval
        routing and noise included, not just the task path — so that if the
        positional `last_message_id` gate ever resets or messages reorder, a
        message can never be re-routed. This matters most for the approval
        branches: re-routing an operator `approve <version>` reply to the Apis
        publish gate a second time is more consequential than a duplicate
        invoice notification.
        """
        if mid and mid not in processed_id_set:
            processed_id_set.add(mid)
            processed_ids.append(mid)

    actionable_count = 0
    invoice_count = 0
    approval_count = 0
    for msg in new_messages:
        msg_id = msg.get("id", "")

        # Already handled in a prior cycle — skip entirely so we neither
        # re-create tasks nor re-notify the operator. Checked first so dedup
        # short-circuits even the approval paths below.
        if msg_id and msg_id in processed_id_set:
            continue

        # Release-approval reply takes precedence: an operator `approve <version>`
        # reply to a release RC email is routed to the Apis publish gate and the
        # message is then done (no task created). Checked before swarm-approval
        # because the two use distinct subject markers, so at most one matches.
        try:
            if await _maybe_handle_release_approval(msg, notifier):
                approval_count += 1
                _mark_handled(msg_id)
                continue
        except Exception as exc:  # never let approval detection break the poll
            log.error(f"[{DAEMON_NAME}] release-approval check failed: {exc}")

        # Swarm PR-approval reply takes precedence over normal classification:
        # an operator "APPROVE" reply to a merge-ready email is routed to Apis
        # and this message is then done (no task created for it).
        try:
            if await _maybe_handle_swarm_approval(msg, notifier):
                approval_count += 1
                _mark_handled(msg_id)
                continue
        except Exception as exc:  # never let approval detection break the poll
            log.error(f"[{DAEMON_NAME}] swarm-approval check failed: {exc}")

        sender = msg.get("sender", msg.get("from", ""))
        subject = msg.get("subject", "(no subject)")
        snippet = msg.get("snippet", "")

        classification = _classify_message(sender, subject, snippet)
        msg["classification"] = classification
        is_invoice = _is_invoice(sender, subject, snippet)

        label = "INVOICE→monedula" if is_invoice else classification.upper()
        log.info(
            f"[{DAEMON_NAME}] {label}: from={sender[:40]!r} subject={subject[:60]!r}"
        )

        if classification == "noise":
            _mark_handled(msg_id)
            continue

        # Store email entity in Neotoma
        email_entity_id = await _store_email_entity(msg)

        if classification == "actionable" or is_invoice:
            if is_invoice:
                invoice_count += 1
            else:
                actionable_count += 1
            await _create_task_for_email(msg, email_entity_id)
            _label_gmail_message(msg_id, PROCESSED_LABEL)
            # Record only after the message is fully handled, so a mid-message
            # crash lets it be retried next cycle rather than silently dropped.
            _mark_handled(msg_id)
        else:
            # informational (stored, no task) — still terminally handled.
            # Intentional asymmetry vs. the actionable/invoice branch: we do
            # NOT apply PROCESSED_LABEL or mark-read here, so informational mail
            # stays is:unread and re-enters the poll query until it ages out of
            # the bounded dedup set. Harmless — informational mail never
            # notifies — and it avoids Turdus silently marking the operator's
            # ordinary unread mail as read.
            _mark_handled(msg_id)

    # Update state with newest processed message ID and the bounded dedup set.
    if new_messages:
        state["last_message_id"] = new_messages[0].get("id")
        state["processed_count"] = state.get("processed_count", 0) + len(new_messages)
        state["last_poll_at"] = datetime.now(UTC).isoformat()
        state["processed_ids"] = processed_ids[-_MAX_PROCESSED_IDS:]

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

    # Enforce agent_definition.status (ateles#562). Previously this value was
    # logged and then ignored, so a "retired" agent ran normally.
    enforce_status_or_exit(agent_def, DAEMON_NAME)

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
