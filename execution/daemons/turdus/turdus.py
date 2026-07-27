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
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import neotoma_mcp  # noqa: E402 — sibling module: HTTP-MCP client for Neotoma writes
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

# ── Self-notification guard ───────────────────────────────────────────────────
# Turdus sends its own digests via the notifier (gws +send --from ATELES_SWARM_EMAIL,
# a `+swarm` Gmail alias that lands in the SAME inbox). Those digests carry an
# "[Ateles] [turdus] … invoice(s) …" subject — which matches the invoice/actionable
# keyword filters, so on the next poll Turdus re-triages its OWN notification and
# fires another one: a runaway self-feeding loop. Skip any message Turdus itself
# authored — identified by the swarm From-address or the "[Ateles]" subject prefix.
SWARM_EMAIL = os.environ.get("ATELES_SWARM_EMAIL", "").strip().lower()
_SELF_SUBJECT_PREFIX = "[Ateles]"

APIS_APPROVE_URL = os.environ.get(
    "APIS_APPROVE_EMAIL_URL", "http://127.0.0.1:8742/approve-email"
)
APIS_APPROVE_EMAIL_SECRET = os.environ.get("APIS_APPROVE_EMAIL_SECRET", "")
# Marker identifying a swarm merge-ready notification (present in the subject
# the operator is replying to). Kept in sync with the Apis notification text.
_MERGE_READY_SUBJECT_MARKER = "READY TO MERGE"
_APPROVE_TOKEN_PREFIX = "swarm-approve:"

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

# Pure notification/automation senders that are NEVER invoices — a code-forge
# or provider system notice whose subject merely *mentions* an invoice keyword
# (e.g. a GitHub email about a PR titled "...break invoice loop"). These are
# hard denials: no real invoice is sent from these addresses.
_INVOICE_SENDER_DENYLIST = [
    "notifications@",  # code-forge / system notifications (github, gitlab, …)
    "noreply-accounts@",  # provider account notices (google, …)
]

# Generic "no-reply" prefixes are a WEAK deny: many legitimate billing systems
# send invoices from noreply@<billing-domain>. So a no-reply sender is only
# treated as non-invoice when it lacks a positive invoice sender-keyword — the
# known billing fragment (billing@, stripe, paypal, …) wins over the prefix.
_INVOICE_SOFT_DENY_PREFIXES = [
    "no-reply@",
    "noreply@",
]

# Subject fragments that signal a NON-payment message even when an invoice
# keyword is present — refunds, receipts-of-something-received, data-shares.
# A refund or a "receipt is ready" note is not a payment the operator owes.
_INVOICE_NEGATIVE_SUBJECT_KEYWORDS = [
    "refund",
    "on the way",
    "shared some",
    "account data",
    "has been paid",
    "payment received",
    "we've received your payment",
    "thanks for your payment",
]


def _has_invoice_sender_keyword(sender_lower: str) -> bool:
    return any(kw in sender_lower for kw in _INVOICE_SENDER_KEYWORDS)


# Money-signal gate (guard #3, ateles#205 signed-off scope): a currency
# symbol/code adjacent to an amount, or an amount adjacent to a currency. A bare
# invoice keyword with no money amount (e.g. "Please see attached invoice")
# should NOT, on its own, route a payment to monedula.
_CURRENCY_TOKENS = r"€|\$|£|usd|eur|gbp|chf|mxn"
_MONEY_SIGNAL_RE = re.compile(
    r"(?:(?:" + _CURRENCY_TOKENS + r")\s?\d)"  # €120 / $ 1,000 / USD 40
    r"|(?:\d[\d.,]*\s?(?:" + _CURRENCY_TOKENS + r"))",  # 120€ / 1.000 eur
    re.IGNORECASE,
)


def _has_money_signal(*texts: str) -> bool:
    """True if any text carries a currency+amount token (guard #3)."""
    return any(_MONEY_SIGNAL_RE.search(t or "") for t in texts)


def _is_invoice(sender: str, subject: str, snippet: str) -> bool:
    """Return True if this message is an invoice or payment request.

    Precision guards (ateles#205 signed-off scope) run before any bare-keyword
    match can route a payment to monedula:
      1. Pure automation/notification senders are never invoices.
      2. Refund / receipt-of-payment / data-share subjects are excluded even
         when they contain an invoice keyword.
      3. A generic no-reply@ sender is non-invoice unless it carries a positive
         invoice signal.
      4. Money-signal gate: a subject-keyword match must be corroborated by a
         currency/amount token (in subject or snippet) OR come from a trusted
         billing sender. A bare keyword with no amount and no billing sender
         does not route (closes the amount-less "invoice" false-positive).
    """
    subj_lower = subject.lower()
    sender_lower = sender.lower()

    # 1. Hard deny: pure notification/automation senders.
    for deny in _INVOICE_SENDER_DENYLIST:
        if deny in sender_lower:
            return False

    # 2. Negative subject signals veto a positive keyword match.
    for neg in _INVOICE_NEGATIVE_SUBJECT_KEYWORDS:
        if neg in subj_lower:
            return False

    subject_signal = any(kw in subj_lower for kw in _INVOICE_SUBJECT_KEYWORDS)
    sender_signal = _has_invoice_sender_keyword(sender_lower)

    # 3. Soft deny: a generic no-reply@ sender is non-invoice ONLY when there is
    #    no positive invoice signal at all. A real billing system that mails from
    #    noreply@<billing-domain> with an invoice subject still routes.
    if (
        any(p in sender_lower for p in _INVOICE_SOFT_DENY_PREFIXES)
        and not sender_signal
        and not subject_signal
    ):
        return False

    # 4. A trusted billing sender is sufficient on its own. Otherwise a subject
    #    keyword must be corroborated by a money signal (currency + amount), so a
    #    bare "please see attached invoice" with no amount does not route.
    if sender_signal:
        return True
    if subject_signal and _has_money_signal(subject, snippet):
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

    Writes via the Neotoma MCP transport (neotoma_mcp.store_entity); the former
    REST POST /observations was removed server-side and 404s. Local loopback
    accepts writes without a bearer token (TRUST_PROD_LOOPBACK), so this no
    longer early-returns on an empty token.
    """
    if DRY_RUN:
        log.info(
            f"[{DAEMON_NAME}] DRY RUN — would store email entity for {message.get('id')}"
        )
        return None

    entity = {
        "entity_type": "email_message",
        "canonical_name": f"email_message:gmail:{message.get('id', 'unknown')}",
        "message_id": message.get("id", ""),
        "sender": message.get("sender", ""),
        "subject": message.get("subject", ""),
        "snippet": message.get("snippet", ""),
        "date": message.get("date_iso", ""),
        "labels": message.get("labels", []),
        "classification": message.get("classification", "informational"),
        "source": "gmail",
    }

    entity_id = await neotoma_mcp.store_entity(
        NEOTOMA_BASE_URL,
        NEOTOMA_BEARER_TOKEN,
        entity,
        idempotency_key=f"turdus-email-{message.get('id', 'unknown')}",
    )
    if entity_id:
        log.info(f"[{DAEMON_NAME}] Stored email_message entity {entity_id}")
    else:
        log.error(f"[{DAEMON_NAME}] Failed to store email entity for {message.get('id')}")
    return entity_id


async def _create_task_for_email(
    message: dict, email_entity_id: str | None
) -> str | None:
    """
    Create a Neotoma task entity for an actionable email.

    Invoices/receipts/payment requests are routed directly to Monedula with
    priority=urgent, bypassing general Apis dispatch. All other actionable
    emails create a standard agent-audience task routed through Apis.

    Returns the created task's entity id (None on failure), so the caller can
    tell the operator WHICH task was created rather than just how many — and so
    the notifier only claims a task exists when the write actually landed.

    Writes via the Neotoma MCP transport (the REST POST /observations it used to
    call was removed server-side and 404s on every sweep).
    """
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
        }
        log_label = "actionable"

    if DRY_RUN:
        log.info(
            f"[{DAEMON_NAME}] DRY RUN — would create {log_label} task for email "
            f"from {sender!r}: {subject[:60]!r}"
        )
        return

    entity = {
        "entity_type": "task",
        "canonical_name": f"task:turdus:email:{message.get('id', 'unknown')}",
        **snapshot,
    }

    task_id = await neotoma_mcp.store_entity(
        NEOTOMA_BASE_URL,
        NEOTOMA_BEARER_TOKEN,
        entity,
        idempotency_key=f"turdus-task-{message.get('id', 'unknown')}",
    )
    if not task_id:
        log.error(f"[{DAEMON_NAME}] Failed to create task for email from {sender!r}")
        return None

    log.info(f"[{DAEMON_NAME}] Created task {task_id} for email from {sender!r}")

    # Link task REFERS_TO email entity (best-effort — a link failure must not
    # discard the task_id we just earned).
    if email_entity_id and task_id:
        await neotoma_mcp.create_relationship(
            NEOTOMA_BASE_URL,
            NEOTOMA_BEARER_TOKEN,
            source_entity_id=task_id,
            target_entity_id=email_entity_id,
            relationship_type="REFERS_TO",
            idempotency_key=f"turdus-rel-{message.get('id', 'unknown')}",
        )
    return task_id


def _format_sender(sender: str) -> str:
    """'Name <a@b.com>' -> 'Name (a@b.com)'; bare addresses pass through."""
    s = (sender or "").strip()
    if "<" in s and ">" in s:
        name = s.split("<", 1)[0].strip().strip('"')
        addr = s.split("<", 1)[1].split(">", 1)[0].strip()
        return f"{name} ({addr})" if name else addr
    return s or "(unknown sender)"


def _format_digest(headline: str, items: list[dict], max_items: int = 10) -> str:
    """Render a notification that says WHO and WHAT, not just a count.

    The previous form — "turdus: 1 invoice(s) → urgent task(s) created for
    monedula" — carried no sender, subject, or task id, so it was unactionable
    without opening Neotoma. Each line now names the sender and subject, with the
    created task id for follow-up.
    """
    lines = [f"{DAEMON_NAME}: {headline}", ""]
    for item in items[:max_items]:
        subject = (item.get("subject") or "(no subject)").strip()
        if len(subject) > 78:
            subject = subject[:77] + "…"
        lines.append(f"• {subject}")
        lines.append(f"    from: {_format_sender(item.get('sender', ''))}")
        if item.get("task_id"):
            lines.append(f"    task: {item['task_id']}")
        else:
            lines.append("    task: (creation failed — see logs)")
    if len(items) > max_items:
        lines.append(f"…and {len(items) - max_items} more.")
    return "\n".join(lines).rstrip()


# ── Swarm PR-approval-by-email (approval loop) ────────────────────────────────


def _extract_sender_address(sender: str) -> str:
    """Return the bare email address from a `Name <addr@x>` sender, lowered."""
    m = re.search(r"<([^>]+)>", sender or "")
    addr = (m.group(1) if m else (sender or "")).strip().lower()
    return addr


def _is_self_notification(sender: str, subject: str) -> bool:
    """True if this message is one Turdus (or the swarm) sent to the operator.

    Triage feeds on the operator's inbox, into which the notifier delivers swarm
    digests via a `+swarm` alias — same inbox, unread. Left unfiltered, Turdus's
    own "[Ateles] …" digest matches the invoice/actionable keywords and triggers
    an endless self-notification loop. Match on the swarm From-address (primary)
    or the "[Ateles]" subject prefix (fallback when the address isn't configured).
    """
    if SWARM_EMAIL and _extract_sender_address(sender) == SWARM_EMAIL:
        return True
    return (subject or "").lstrip().startswith(_SELF_SUBJECT_PREFIX)


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
        for key in ("body", "text", "plain", "snippet"):
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
        log.info(
            f"[{DAEMON_NAME}] email-approve routed for {repository}#{pr_number}"
        )
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

    actionable_count = 0
    invoice_count = 0
    invoice_items: list[dict] = []
    actionable_items: list[dict] = []
    approval_count = 0
    for msg in new_messages:
        # Swarm PR-approval reply takes precedence over normal classification:
        # an operator "APPROVE" reply to a merge-ready email is routed to Apis
        # and this message is then done (no task created for it).
        try:
            if await _maybe_handle_swarm_approval(msg, notifier):
                approval_count += 1
                continue
        except Exception as exc:  # never let approval detection break the poll
            log.error(f"[{DAEMON_NAME}] swarm-approval check failed: {exc}")

        sender = msg.get("sender", msg.get("from", ""))
        subject = msg.get("subject", "(no subject)")
        snippet = msg.get("snippet", "")

        # Skip Turdus's own swarm digests — they re-enter the inbox via the
        # +swarm alias and would otherwise self-trigger an invoice loop.
        if _is_self_notification(sender, subject):
            log.info(
                f"[{DAEMON_NAME}] SELF-SKIP: own swarm digest "
                f"subject={subject[:60]!r}"
            )
            continue

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
            task_id = await _create_task_for_email(msg, email_entity_id)
            # Keep WHO and WHAT, not just a tally — a bare count ("1 invoice(s)")
            # tells the operator nothing actionable.
            item = {
                "sender": sender,
                "subject": subject,
                "task_id": task_id,
                "snippet": snippet,
            }
            if is_invoice:
                invoice_count += 1
                invoice_items.append(item)
            else:
                actionable_count += 1
                actionable_items.append(item)
            _label_gmail_message(msg.get("id", ""), "Turdus/processed")

    # Update state with newest processed message ID
    if new_messages:
        state["last_message_id"] = new_messages[0].get("id")
        state["processed_count"] = state.get("processed_count", 0) + len(new_messages)
        state["last_poll_at"] = datetime.now(UTC).isoformat()

    if invoice_count > 0:
        notifier.send(
            _format_digest(
                f"{invoice_count} invoice(s) → urgent task(s) for monedula",
                invoice_items,
            ),
            priority=Priority.BLOCKER,
            handler=DAEMON_NAME,
        )
    if actionable_count > 0:
        notifier.send(
            _format_digest(
                f"{actionable_count} actionable email(s) → task(s) created",
                actionable_items,
            ),
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
