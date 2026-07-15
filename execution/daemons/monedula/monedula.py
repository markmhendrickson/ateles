#!/usr/bin/env python3
"""
Monedula — Daily Payments Daemon
Named after Corvus monedula (jackdaw — moneta = money).

Runs once per day via launchd StartCalendarInterval.
Checks Google Calendar for yesterday's sessions that trigger payment obligations,
sends a Telegram preview, waits for operator approval, executes payments, and
sends a Telegram confirmation.

Usage:
  python3 monedula.py
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path

# ---------------------------------------------------------------------------
# Bootstrap: load env from ~/.config/neotoma/.env before anything else.
# (launchd does not source shell profiles)
# ---------------------------------------------------------------------------

_NEOTOMA_ENV_FILE = Path.home() / ".config" / "neotoma" / ".env"
if _NEOTOMA_ENV_FILE.exists():
    for _line in _NEOTOMA_ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

# ---------------------------------------------------------------------------
# lib/notify integration (path bootstrap required before import)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

try:
    from lib.notify import Notifier  # noqa: E402

    _notifier: Notifier | None = Notifier.from_neotoma()
except Exception:  # lib unavailable or Neotoma unreachable at import time
    _notifier = None


def _notify(message: str, priority: str = "info") -> None:
    """Send via lib/notify if available; silently skip if not."""
    if _notifier is None:
        return
    try:
        from lib.notify import Priority

        p = getattr(Priority, priority.upper(), Priority.INFO)
        _notifier.send(message, priority=p, handler="monedula")
    except Exception:
        pass


# Activity-log channel (CyphorhinusBot observation feed).
try:
    from lib.activity import ActivityLogger  # noqa: E402

    _activity: "ActivityLogger | None" = ActivityLogger(agent="monedula")
except Exception:
    _activity = None


# ---------------------------------------------------------------------------
# Constants / paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent  # ateles repo root
LOG_DIR = Path.home() / "Library" / "Logs" / "ateles"
LOG_FILE = LOG_DIR / "monedula.log"
STATE_FILE = Path(__file__).parent / ".monedula_last_run"

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_ALLOWED_USER_ID = os.environ.get("TELEGRAM_ALLOWED_USER_ID", "")
# TELEGRAM_TOPIC_MONEDULA is the thread ID for Monedula notifications.
# Legacy alias: TELEGRAM_TOPIC_PAYMENTS is also accepted for backwards compatibility.
TELEGRAM_TOPIC_MONEDULA = os.environ.get(
    "TELEGRAM_TOPIC_MONEDULA", ""
) or os.environ.get("TELEGRAM_TOPIC_PAYMENTS", "")
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
NEOTOMA_BASE_URL = os.environ.get("NEOTOMA_BASE_URL", "")

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_DIR.mkdir(parents=True, exist_ok=True)


class _FlushingFileHandler(logging.FileHandler):
    def emit(self, record: logging.LogRecord) -> None:
        super().emit(record)
        self.flush()


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [monedula] %(levelname)s %(message)s",
    handlers=[
        _FlushingFileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Idempotency guard
# ---------------------------------------------------------------------------


def _check_already_ran_today() -> bool:
    """Return True if this daemon already ran today (idempotency guard)."""
    if STATE_FILE.exists():
        contents = STATE_FILE.read_text().strip()
        if contents == date.today().isoformat():
            return True
    return False


def _mark_ran_today() -> None:
    STATE_FILE.write_text(date.today().isoformat())


def _clear_run_state() -> None:
    if STATE_FILE.exists():
        STATE_FILE.unlink()


# ---------------------------------------------------------------------------
# Calendar: fetch yesterday's events
# ---------------------------------------------------------------------------


def _yesterday() -> date:
    return date.today() - timedelta(days=1)


def fetch_yesterday_events() -> list[dict]:
    """
    Use gws CLI to fetch all calendar events for yesterday.
    Returns list of event dicts (each with at least 'summary').
    Returns empty list on any failure.
    """
    import shutil

    gws = shutil.which("gws")
    if not gws:
        log.error("gws CLI not found in PATH — cannot check calendar")
        return []

    yest = _yesterday()
    time_min = yest.strftime("%Y-%m-%dT00:00:00+02:00")
    time_max = yest.strftime("%Y-%m-%dT23:59:59+02:00")

    params = {
        "calendarId": "primary",
        "singleEvents": True,
        "orderBy": "startTime",
        "timeMin": time_min,
        "timeMax": time_max,
    }

    try:
        result = subprocess.run(
            [gws, "calendar", "events", "list", "--params", json.dumps(params)],
            capture_output=True,
            text=True,
            timeout=30,
            env=os.environ,
        )
        if result.returncode != 0:
            log.error(f"gws calendar events list failed: {result.stderr.strip()[:300]}")
            return []

        data = json.loads(result.stdout)
        items = data.get("items") or []
        log.info(f"Fetched {len(items)} calendar event(s) for {yest.isoformat()}")
        for item in items:
            log.debug(f"  Event: {item.get('summary', '(no title)')!r}")
        return items

    except json.JSONDecodeError as exc:
        log.error(f"Failed to parse gws output: {exc}")
        return []
    except Exception as exc:
        log.error(f"Calendar fetch error: {exc}")
        return []


# ---------------------------------------------------------------------------
# Neotoma: fetch due payment tasks
# ---------------------------------------------------------------------------


def _fetch_entity_by_id(entity_id: str) -> dict | None:
    """Fetch a single entity (with snapshot) by ID from Neotoma. None on error."""
    base_url = (NEOTOMA_BASE_URL or "http://localhost:3180").rstrip("/")
    is_loopback = "localhost" in base_url or "127.0.0.1" in base_url
    try:
        url = f"{base_url}/entities/{entity_id}"
        headers = {"Accept": "application/json"}
        if NEOTOMA_BEARER_TOKEN and not is_loopback:
            headers["Authorization"] = f"Bearer {NEOTOMA_BEARER_TOKEN}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        log.warning(f"Neotoma entity fetch failed for {entity_id}: {exc}")
        return None


def fetch_due_payment_tasks(handlers: list | None = None) -> list[dict]:
    """
    Return the payment tasks that are due today or overdue, scoped STRICTLY to
    the tasks explicitly linked to active payment profiles via
    `profile.neotoma_task_id`.

    This is deliberately NOT a keyword/domain scan of the whole task corpus —
    that produced false positives (any finance-domain or BTC-mentioning task).
    Only tasks a payment profile actually points at are payment tasks.

    Returns a list of task dicts (each the raw entity with a 'snapshot').
    Falls back to empty list on any error or if no handlers/links exist.
    """
    if not NEOTOMA_BASE_URL:
        log.warning("NEOTOMA_BASE_URL not set — skipping task scan")
        return []

    if not handlers:
        log.info("No payment handlers — skipping linked-task scan.")
        return []

    today = date.today().isoformat()

    # Collect the canonical task IDs declared by active payment profiles.
    task_ids: list[str] = []
    for h in handlers:
        tid = getattr(getattr(h, "profile", None), "neotoma_task_id", "") or ""
        tid = tid.strip()
        if tid and tid not in task_ids:
            task_ids.append(tid)

    if not task_ids:
        log.info("No payment profiles declare a neotoma_task_id — no linked tasks.")
        return []

    def _fields(task: dict) -> dict:
        return task.get("snapshot") or task.get("fields") or task

    due_tasks: list[dict] = []
    for tid in task_ids:
        entity = _fetch_entity_by_id(tid)
        if not entity:
            continue
        fields = _fields(entity)
        due = str(fields.get("due_date") or "")
        if due and due[:10] <= today:
            due_tasks.append(entity)

    log.info(
        f"Neotoma linked-task scan: {len(task_ids)} profile task(s) checked, "
        f"{len(due_tasks)} due today or overdue"
    )
    for t in due_tasks:
        fields = _fields(t)
        log.debug(
            f"  Task: {fields.get('title') or fields.get('name')!r} due={fields.get('due_date')!r}"
        )

    return due_tasks


def _task_to_preview_item(task: dict) -> dict:
    """
    Convert a Neotoma task entity into a generic preview item dict
    compatible with the preview builder.
    """
    fields = task.get("snapshot") or task.get("fields") or task
    name = str(fields.get("title") or fields.get("name") or "(unnamed task)")
    due = str(fields.get("due_date") or "")
    description = str(fields.get("description") or "")
    entity_id = task.get("entity_id") or task.get("id") or ""
    return {
        "source": "task",
        "name": name,
        "due_date": due,
        "description": description,
        "entity_id": entity_id,
    }


# ---------------------------------------------------------------------------
# Task-based auto-execute (approval-gated, idempotent, dry-run-safe)
# ---------------------------------------------------------------------------
#
# Approval model (operator chose "Neotoma approval flag", 2026-07-13): a due
# payment task executes only when its snapshot carries `payment_approved: true`.
# The operator sets that field (via Inspector / CLI / the Ateles agent) after
# reviewing the emailed preview. This replaces the interactive Telegram reply
# for the email-primary channel, and is fully auditable in Neotoma.
#
# Idempotency: a task whose status is already "done" (or already carries a
# `payment_event_id`) is never re-executed — no double-pay.
#
# Dry-run safety: MONEDULA_DRYRUN defaults to "1" (on). While on, handlers are
# invoked with a dry_run flag / no real broadcast, so wiring can be verified
# without moving money. The operator flips MONEDULA_DRYRUN=0 to arm real sends.


def _dryrun_enabled() -> bool:
    """True unless the operator has explicitly armed real sends (MONEDULA_DRYRUN=0)."""
    return os.environ.get("MONEDULA_DRYRUN", "1") != "0"


def _task_fields(task: dict) -> dict:
    return task.get("snapshot") or task.get("fields") or task


def _task_is_approved(task: dict) -> bool:
    """True iff the task snapshot explicitly flags payment_approved truthy."""
    val = _task_fields(task).get("payment_approved")
    return str(val).strip().lower() in ("true", "1", "yes")


def _task_already_paid(task: dict) -> bool:
    """True iff the task is already settled (status done or a payment_event recorded)."""
    fields = _task_fields(task)
    if str(fields.get("status") or "").strip().lower() == "done":
        return True
    return bool(str(fields.get("payment_event_id") or "").strip())


def _handler_for_task(task: dict, handlers: list) -> Any | None:
    """Return the handler whose profile.neotoma_task_id points at this task."""
    tid = str(task.get("entity_id") or task.get("id") or "").strip()
    if not tid:
        return None
    for h in handlers:
        prof_tid = str(getattr(getattr(h, "profile", None), "neotoma_task_id", "") or "").strip()
        if prof_tid and prof_tid == tid:
            return h
    return None


# ---------------------------------------------------------------------------
# Email reply-to-approve (operator approves a payment by replying to its email)
# ---------------------------------------------------------------------------
#
# Each due, unapproved payment gets one email whose subject carries a stable
# per-payment token: "[APPROVE-<token>]". The operator replies APPROVE (to pay)
# or SKIP (to decline); the body also asks them to confirm attendance for
# session-gated payments. On the next run, process_email_approvals() scans the
# inbox, and any message containing "APPROVE <token>" flips payment_approved=true
# on that task (SKIP sets it false). Matching is by the exact token, so a reply
# can never approve the wrong payment.


def _payment_token(task_id: str) -> str:
    """Stable short token for a task id (same across runs, so re-sends match)."""
    import hashlib

    h = hashlib.sha256(task_id.encode()).hexdigest()[:8].upper()
    return h


def _set_task_approved(task_id: str, approved: bool) -> bool:
    """Set payment_approved on a task via `neotoma corrections create`."""
    import shutil

    neotoma = shutil.which("neotoma")
    if not neotoma:
        log.warning("[approve] neotoma CLI not found — cannot set approval")
        return False
    try:
        r = subprocess.run(
            [neotoma, "--api-only", "corrections", "create", task_id,
             "--entity-type", "task", "--field-name", "payment_approved",
             "--corrected-value", "true" if approved else "false"],
            capture_output=True, text=True, timeout=30, env=os.environ,
        )
        if r.returncode != 0:
            log.warning(f"[approve] corrections create failed (rc={r.returncode}): "
                        f"{(r.stderr or '').strip()[:160]}")
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[approve] corrections create error: {exc}")
        return False


def _build_approval_email(task: dict, handler) -> tuple[str, str]:
    """Return (subject, body) for a per-payment approval request email."""
    fields = _task_fields(task)
    tid = str(task.get("entity_id") or task.get("id") or "")
    token = _payment_token(tid)
    title = str(fields.get("title") or fields.get("name") or "payment")
    profile = getattr(handler, "profile", None)
    amount = getattr(profile, "amount_eur", fields.get("amount") or fields.get("amount_eur") or "?")
    rail = getattr(profile, "payment_type", "?")
    due = str(fields.get("due_date") or "")
    ref = getattr(profile, "wise_reference", "") or ""

    # Recipient (rail-specific, no secrets beyond what the operator already has).
    if rail == "wise":
        payee = (getattr(profile, "wise_recipient_name", "")
                 or getattr(profile, "label", "") or "recipient")
        dest = getattr(profile, "wise_iban", "") or getattr(profile, "wise_recipient_id", "") or ""
    else:
        payee = getattr(profile, "label", "") or "recipient"
        dest = getattr(profile, "btc_address", "") or ""

    subject = f"[Ateles] Approve payment: {title} €{amount} [APPROVE-{token}]"

    # Session-gated payments (yoga/therapy) ask for attendance confirmation.
    session_note = ""
    low = (title + " " + str(fields.get("notes") or "")).lower()
    if any(k in low for k in ("yoga", "therapy", "terapia", "session", "sesión")):
        session_note = (
            "\nThis is a per-session payment. Only approve if you ATTENDED the "
            f"session on {due or 'the due date'}.\n"
        )

    dest_label = "IBAN/acct" if rail == "wise" else "Address"
    dest_line = f"  {dest_label}: {dest}\n" if dest else ""

    body = (
        f"Payment awaiting your approval:\n\n"
        f"  What:      {title}\n"
        f"  Amount:    €{amount} ({rail.upper()})\n"
        f"  To:        {payee}\n"
        + dest_line
        + f"  Due:       {due}\n"
        + (f"  Reference: {ref}\n" if ref else "")
        + session_note
        + "\nJust hit Reply and send:\n"
        "    APPROVE      — to pay it\n"
        "    SKIP         — to decline\n\n"
        f"Monedula executes approved payments on its next run "
        f"(currently {'DRY-RUN' if _dryrun_enabled() else 'LIVE'}).\n"
        f"(Your reply is matched to this payment automatically via the subject "
        f"line — you don't need to type any code.)\n"
    )
    return subject, body


APPROVAL_SENT_FILE = Path(__file__).parent / ".monedula_approvals_sent.json"


def _approval_sent_state() -> dict:
    try:
        return json.loads(APPROVAL_SENT_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _record_approval_sent(task_id: str) -> None:
    state = _approval_sent_state()
    state[task_id] = date.today().isoformat()
    try:
        APPROVAL_SENT_FILE.write_text(json.dumps(state))
    except OSError as exc:  # noqa: BLE001
        log.warning(f"[approve] could not record approval-sent state: {exc}")


def send_approval_requests(due_tasks: list[dict], handlers: list) -> int:
    """Email one approval request per due, unapproved, unpaid payment.

    Asks at most ONCE PER DAY per task: Monedula polls every ~15 min to read
    replies, and without this guard every poll would re-email the same request.
    Returns the number of requests sent.
    """
    sent = 0
    today = date.today().isoformat()
    state = _approval_sent_state()
    for task in due_tasks:
        if _task_already_paid(task) or _task_is_approved(task):
            continue
        tid = str(task.get("entity_id") or task.get("id") or "")
        if state.get(tid) == today:
            continue  # already asked today — waiting on the operator's reply
        handler = _handler_for_task(task, handlers)
        if handler is None:
            continue
        subject, body = _build_approval_email(task, handler)
        if _email_send_with_subject(subject, body):
            sent += 1
            _record_approval_sent(tid)
            fields = _task_fields(task)
            log.info(f"[approve] Sent approval request for "
                     f"{fields.get('title')!r} (token in subject).")
    return sent


def process_email_approvals(due_tasks: list[dict]) -> int:
    """Scan the inbox for APPROVE/SKIP <token> replies; apply to matching tasks.

    Returns the number of tasks whose approval state was changed. Only tasks in
    `due_tasks` are eligible, and matching is by exact per-payment token.
    """
    if os.environ.get("ATELES_NOTIFY_EMAIL", "0") != "1":
        return 0
    token_to_task = {}
    for task in due_tasks:
        tid = str(task.get("entity_id") or task.get("id") or "")
        if tid and not _task_already_paid(task):
            token_to_task[_payment_token(tid)] = tid
    if not token_to_task:
        return 0

    replies = _read_reply_texts(list(token_to_task.keys()))
    changed = 0
    for text in replies:
        up = text.upper()
        for token, tid in token_to_task.items():
            # The token rides along in the quoted subject of the reply
            # ("Re: … [APPROVE-<token>]"), so the operator never types it.
            if token not in up:
                continue
            verdict = _reply_verdict(up, token)
            if verdict is None:
                continue
            if _set_task_approved(tid, verdict):
                log.info(f"[approve] Email {'APPROVE' if verdict else 'SKIP'} "
                         f"for token {token} → task {tid}")
                changed += 1
    return changed


def _reply_verdict(up: str, token: str) -> bool | None:
    """Return True (approve), False (skip), or None (no verdict) for a reply.

    Accepts a bare "APPROVE"/"SKIP" (the token is matched separately from the
    subject line), and the explicit "APPROVE <token>" form. SKIP wins if both
    appear, so an ambiguous reply never pays by accident.
    """
    # Strip the quoted original: Gmail plain-text replies put the operator's own
    # words FIRST, then "On <date> ... wrote:" followed by the quoted request
    # (which itself contains the words APPROVE and SKIP). Only read the part the
    # operator actually typed, or a quoted instruction would count as a verdict.
    body = up
    for marker in ("\nON ", "\n-----ORIGINAL", "\n________"):
        idx = body.find(marker)
        if idx > 0:
            body = body[:idx]
            break

    verdict = None
    for line in body.splitlines():
        s = line.strip().rstrip(".!")
        if s.startswith(">") or "JUST HIT REPLY" in s or "TO DECLINE" in s:
            continue
        # Ignore the subject line itself (it always contains "APPROVE-<token>").
        if s.startswith("RE:") or s.startswith("[ATELES]"):
            continue
        if s in ("SKIP", f"SKIP {token}", f"SKIP-{token}"):
            return False  # SKIP is decisive — never pay on an ambiguous reply
        if s in ("APPROVE", "YES", f"APPROVE {token}", f"APPROVE-{token}"):
            verdict = True
    return verdict


def _gws_json(args: list[str], timeout: int = 45) -> Any:
    """Run a gws command and parse its JSON, tolerating banner/warning preamble."""
    import shutil

    gws = shutil.which("gws")
    if not gws:
        return None
    try:
        r = subprocess.run([gws, *args], capture_output=True, text=True,
                           timeout=timeout, env=os.environ)
        if r.returncode != 0:
            log.warning(f"[approve] gws {args[:2]} failed: {(r.stderr or '').strip()[:160]}")
            return None
        out = r.stdout or ""
        # gws prints a keyring/format banner before the JSON — start at the first
        # '{' or '[' so json.loads doesn't choke on it.
        start = min([i for i in (out.find("{"), out.find("[")) if i >= 0] or [-1])
        if start < 0:
            return None
        return json.loads(out[start:])
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[approve] gws json error: {exc}")
        return None


# Maps payment token -> the Gmail message id of the operator's APPROVE reply, so
# the payment confirmation can be sent as a reply IN THAT THREAD (rather than as
# a new standalone "results" email, which is what the operator saw before).
#
# Persisted: a payment can be approved on one poll and execute on a later one (or
# the daemon may restart in between). Keeping this only in memory silently lost
# the thread and downgraded the confirmation to a standalone digest.
REPLY_IDS_FILE = Path(__file__).parent / ".monedula_reply_ids.json"


def _load_reply_ids() -> dict[str, str]:
    try:
        data = json.loads(REPLY_IDS_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember_reply_id(token: str, message_id: str) -> None:
    ids = _load_reply_ids()
    if ids.get(token) == message_id:
        return
    ids[token] = message_id
    try:
        REPLY_IDS_FILE.write_text(json.dumps(ids))
    except OSError as exc:  # noqa: BLE001
        log.warning(f"[approve] could not persist reply id: {exc}")


_REPLY_MSG_IDS: dict[str, str] = _load_reply_ids()


def _read_reply_texts(tokens: list[str], max_msgs: int = 40) -> list[str]:
    """Return full text (subject + body) of recent replies carrying any token.

    `gws gmail +triage` returns only date/from/id/subject — NOT the body — so the
    operator's verdict ("approve") is invisible there. We therefore triage to find
    candidate message ids whose subject carries a token, then `+read --id` each to
    pull the actual body. Fail-open (returns []).
    """
    if not tokens:
        return []
    texts: list[str] = []
    seen_ids: set[str] = set()

    for token in tokens:
        data = _gws_json(["gmail", "+triage", "--format", "json", "--max",
                          str(max_msgs), "--query", f"newer_than:3d {token}"])
        msgs = []
        if isinstance(data, dict):
            msgs = data.get("messages") or data.get("results") or []
        elif isinstance(data, list):
            msgs = data
        for m in msgs:
            mid = str(m.get("id") or "")
            subject = str(m.get("subject") or "")
            # Only the operator's REPLY can carry a verdict; our own request can't.
            if not mid or mid in seen_ids or not subject.upper().startswith("RE:"):
                continue
            seen_ids.add(mid)
            body_data = _gws_json(["gmail", "+read", "--id", mid, "--headers",
                                   "--format", "json"], timeout=30)
            body = ""
            if isinstance(body_data, dict):
                # gws +read returns the plain-text body under `body_text`.
                body = str(body_data.get("body_text")
                           or body_data.get("body")
                           or body_data.get("text") or "")
            # Remember which message to reply to, so the payment confirmation
            # lands in this same thread — persisted, since the payment may
            # execute on a later poll or after a restart.
            _REPLY_MSG_IDS[token] = mid
            _remember_reply_id(token, mid)
            texts.append(f"{subject}\n{body}")
    return texts


def _email_reply_in_thread(message_id: str, body: str,
                           attachments: list[str] | None = None) -> bool:
    """Reply to `message_id` so the confirmation lands in the SAME thread as the
    operator's approval. Uses `gws gmail +reply`, which handles threading.

    Verifies the resolved recipient is the operator: `+reply` can misaddress when
    replying to a message the operator themselves sent (a known gws quirk), so we
    pass --to explicitly rather than trusting the inferred recipient.
    Fail-open: returns False so the caller can fall back to a standalone email.
    """
    import shutil

    if os.environ.get("ATELES_NOTIFY_EMAIL", "0") != "1":
        return False
    operator_email = os.environ.get("OPERATOR_EMAIL", "").strip()
    gws = shutil.which("gws")
    if not gws or not message_id or not operator_email:
        return False

    cmd = [gws, "gmail", "+reply", "--message-id", message_id,
           "--body", body, "--to", operator_email]
    swarm_email = os.environ.get("ATELES_SWARM_EMAIL", "").strip()
    if swarm_email:
        cmd += ["--from", swarm_email]

    staged: list[Path] = []
    stage_dir = PROJECT_ROOT / ".monedula_receipts_tmp"
    for src in (attachments or []):
        try:
            if not os.path.exists(src):
                continue
            stage_dir.mkdir(parents=True, exist_ok=True)
            dst = stage_dir / Path(src).name
            shutil.copyfile(src, dst)
            staged.append(dst)
            cmd += ["--attach", str(dst.relative_to(PROJECT_ROOT))]
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[notify] could not stage attachment {src}: {exc}")

    try:
        r = subprocess.run(cmd, timeout=60, capture_output=True, text=True,
                           cwd=str(PROJECT_ROOT), env=os.environ)
        if r.returncode != 0:
            log.warning(f"[notify] +reply failed (rc={r.returncode}): "
                        f"{(r.stderr or '').strip()[:160]}")
            return False
        log.info(f"[notify] Confirmation replied in thread ({message_id}).")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[notify] +reply error: {exc}")
        return False
    finally:
        for p in staged:
            try:
                p.unlink()
            except OSError:
                pass


def _email_send_with_subject(subject: str, body: str) -> bool:
    """Send an email with an explicit subject (approval requests need the token
    in the subject). Mirrors _email_send but does not derive subject from body."""
    import shutil

    if os.environ.get("ATELES_NOTIFY_EMAIL", "0") != "1":
        return False
    operator_email = os.environ.get("OPERATOR_EMAIL", "").strip()
    if not operator_email:
        return False
    gws = shutil.which("gws")
    if not gws:
        return False
    cmd = [gws, "gmail", "+send", "--to", operator_email,
           "--subject", subject, "--body", body]
    swarm_email = os.environ.get("ATELES_SWARM_EMAIL", "").strip()
    if swarm_email:
        cmd += ["--from", swarm_email]
    try:
        r = subprocess.run(cmd, timeout=60, capture_output=True, text=True,
                           cwd=str(PROJECT_ROOT), env=os.environ)
        if r.returncode != 0:
            log.warning(f"[approve] approval email send failed (rc={r.returncode}): "
                        f"{(r.stderr or '').strip()[:160]}")
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(f"[approve] approval email error: {exc}")
        return False


def execute_approved_tasks(due_tasks: list[dict], handlers: list) -> list[tuple]:
    """
    Execute the due payment tasks that are operator-approved and not already paid.

    Returns a list of (handler, result) tuples for tasks that were acted on.
    Respects MONEDULA_DRYRUN (default on): while dry-run, the handler is asked to
    build-but-not-broadcast where it supports a dry_run flag, and the result is
    tagged status="dry_run" so no confirmation implies a real send.
    """
    results: list[tuple] = []
    dry = _dryrun_enabled()

    for task in due_tasks:
        fields = _task_fields(task)
        title = str(fields.get("title") or fields.get("name") or "(unnamed task)")

        if _task_already_paid(task):
            log.info(f"[autoexec] Skipping already-paid task {title!r}.")
            continue
        if not _task_is_approved(task):
            log.info(f"[autoexec] Task {title!r} not approved (payment_approved) — skipping.")
            continue

        handler = _handler_for_task(task, handlers)
        if handler is None:
            log.warning(f"[autoexec] No handler resolves task {title!r} — skipping.")
            continue

        synthetic_match = {"summary": title, "source": "task",
                           "entity_id": task.get("entity_id") or task.get("id")}

        if dry:
            log.info(f"[autoexec] DRY-RUN — would execute {handler.name} for {title!r} "
                     f"(€{getattr(handler.profile, 'amount_eur', '?')}).")
            results.append((handler, {
                "status": "dry_run", "handler": handler.name,
                "amount_eur": getattr(handler.profile, "amount_eur", None),
                "task": title,
            }))
            continue

        log.info(f"[autoexec] Executing {handler.name} for approved task {title!r}...")
        try:
            result = handler.execute(synthetic_match)
        except Exception as exc:  # noqa: BLE001 — never crash the daemon
            log.error(f"[autoexec] {handler.name} execution error: {exc}")
            result = {"status": "failed", "handler": handler.name, "error": str(exc)}
        results.append((handler, result))

    return results


def task_for(task_id: str, due_tasks: list[dict]) -> dict:
    """Return the due-task dict with this entity id (empty dict if absent)."""
    for t in due_tasks:
        if str(t.get("entity_id") or t.get("id") or "") == task_id:
            return t
    return {}


def _is_recurring_obligation(task: dict, handler=None) -> bool:
    """True when a task is a standing per-session obligation that must NEVER be
    marked completed — only have its due_date rolled (operator standing rule for
    yoga / therapy and similar recurring wellness commitments).

    Signals, cheapest first: an explicit `recurrence` field on the task, or a
    session-payment keyword in the title/notes.
    """
    fields = task.get("snapshot") or task.get("fields") or task or {}
    if str(fields.get("recurrence") or "").strip():
        return True
    hay = " ".join([
        str(fields.get("title") or ""),
        str(fields.get("notes") or ""),
        str(getattr(getattr(handler, "profile", None), "label", "") or ""),
    ]).lower()
    return any(k in hay for k in
               ("yoga", "therapy", "terapia", "osteopat", "per-session",
                "pay only for attended"))


def _mark_tasks_paid(task_results: list[tuple], due_tasks: list[dict]) -> None:
    """
    After a REAL successful send, mark the corresponding task done in Neotoma
    with a payment note. No-op for dry-run results and for non-'sent' statuses,
    so a task is only ever marked paid when money actually moved.

    Follows a recurring-obligation exception: if the task/profile is flagged as a
    never-complete recurring obligation, callers should not route it here — this
    helper is for the one-off vendor/reimbursement tasks that DO complete.
    """
    import shutil

    if _dryrun_enabled():
        return  # never mutate task lifecycle during dry-run

    neotoma = shutil.which("neotoma")
    if not neotoma:
        log.warning("[autoexec] neotoma CLI not found — cannot mark tasks paid.")
        return

    # Map handler.name -> its task entity_id via due_tasks + profile link.
    by_handler_task = {}
    for t in due_tasks:
        tid = str(t.get("entity_id") or t.get("id") or "")
        by_handler_task[tid] = t

    for handler, result in task_results:
        if result.get("status") != "sent":
            continue
        tid = str(getattr(getattr(handler, "profile", None), "neotoma_task_id", "") or "")
        if not tid:
            continue
        ref = (result.get("transfer_id") or result.get("txid")
               or result.get("reference") or "")

        # Use `neotoma corrections create` (one high-priority correction per
        # field) — the CLI has no `entities update` verb. Set status=done and
        # record the payment reference in payment_event_id for idempotency.
        def _correct(field: str, value: str) -> bool:
            try:
                r = subprocess.run(
                    [neotoma, "--api-only", "corrections", "create", tid,
                     "--entity-type", "task",
                     "--field-name", field, "--corrected-value", value],
                    capture_output=True, text=True, timeout=30, env=os.environ,
                )
                if r.returncode != 0:
                    log.warning(f"[autoexec] corrections create {field} failed "
                                f"(rc={r.returncode}): {(r.stderr or '').strip()[:160]}")
                    return False
                return True
            except Exception as exc:  # noqa: BLE001
                log.warning(f"[autoexec] corrections create {field} error: {exc}")
                return False

        # Recurring-obligation rule: a per-session obligation (yoga, therapy, …)
        # is NEVER completed — the task is the standing obligation, not one
        # payment. Only record the payment reference; the due_date rolls to the
        # next session separately. One-off tasks (vendor invoices,
        # reimbursements) do complete.
        ok_ref = _correct("payment_event_id", str(ref)) if ref else True
        if _is_recurring_obligation(task_for(tid, due_tasks), handler):
            if ok_ref:
                log.info(f"[autoexec] Recorded payment on recurring task {tid} "
                         f"({handler.name}), ref={ref} — NOT marked done "
                         f"(recurring obligation).")
            else:
                log.warning(f"[autoexec] Recurring task {tid} payment ref not "
                            f"recorded — needs manual note.")
            continue

        ok_status = _correct("status", "done")
        if ok_status and ok_ref:
            log.info(f"[autoexec] Marked task {tid} done ({handler.name}), ref={ref}.")
        else:
            log.warning(f"[autoexec] Task {tid} NOT fully marked paid "
                        f"(status_ok={ok_status}, ref_ok={ok_ref}) — needs manual close.")


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------


def _email_send(text: str, attachments: list[str] | None = None) -> bool:
    """Send an operator notification by email via `gws gmail +send`. Fail-open.

    Subject = "[Monedula] " + the message's first line; body = full message.
    `attachments` are file paths (e.g. Wise receipt PDFs); gws requires them
    under the current directory, so they are copied into a repo-local temp dir
    for the send and removed after. Uses an argv list (no shell). Returns True
    on success. Gated by ATELES_NOTIFY_EMAIL / OPERATOR_EMAIL.
    """
    import shutil

    if os.environ.get("ATELES_NOTIFY_EMAIL", "0") != "1":
        return False
    operator_email = os.environ.get("OPERATOR_EMAIL", "").strip()
    if not operator_email:
        return False
    gws = shutil.which("gws")
    if not gws:
        log.warning("[notify] gws not found — cannot send email, falling back")
        return False

    first = (text.strip().splitlines() or ["notification"])[0]
    subject = f"[Monedula] {first[:80]}"
    cmd = [gws, "gmail", "+send", "--to", operator_email,
           "--subject", subject, "--body", text]
    swarm_email = os.environ.get("ATELES_SWARM_EMAIL", "").strip()
    if swarm_email:
        cmd += ["--from", swarm_email]

    # gws only attaches files under CWD — stage copies in a repo-local temp dir.
    staged: list[Path] = []
    stage_dir = PROJECT_ROOT / ".monedula_receipts_tmp"
    for src in (attachments or []):
        try:
            if not os.path.exists(src):
                continue
            stage_dir.mkdir(parents=True, exist_ok=True)
            dst = stage_dir / Path(src).name
            shutil.copyfile(src, dst)
            staged.append(dst)
            cmd += ["--attach", str(dst.relative_to(PROJECT_ROOT))]
        except Exception as exc:  # noqa: BLE001
            log.warning(f"[notify] could not stage attachment {src}: {exc}")

    try:
        r = subprocess.run(cmd, timeout=60, capture_output=True, text=True,
                           cwd=str(PROJECT_ROOT), env=os.environ)
        if r.returncode != 0:
            log.warning("[notify] gws +send failed (rc=%s): %s",
                        r.returncode, (r.stderr or "").strip()[:200])
            return False
        log.info("[notify] Operator notified by email (%s)%s.", operator_email,
                 f" with {len(staged)} receipt(s)" if staged else "")
        return True
    except Exception as exc:  # noqa: BLE001 — never crash the caller
        log.warning("[notify] email send error: %s", exc)
        return False
    finally:
        for p in staged:
            try:
                p.unlink()
            except OSError:
                pass
        try:
            if stage_dir.exists() and not any(stage_dir.iterdir()):
                stage_dir.rmdir()
        except OSError:
            pass


def _telegram_only(text: str) -> None:
    """Deliver via Telegram (send.mjs helper, falling back to telegram-send CLI)."""
    import shutil

    node = shutil.which("node")
    send_script = PROJECT_ROOT / "execution" / "lib" / "telegram" / "send.mjs"
    if node and send_script.exists():
        try:
            args = [node, str(send_script), "--text", text]
            if TELEGRAM_TOPIC_MONEDULA:
                args += ["--thread-id", TELEGRAM_TOPIC_MONEDULA]
            subprocess.run(args, timeout=15, capture_output=True, env=os.environ)
            return
        except Exception as exc:
            log.warning(f"send.mjs failed: {exc}, trying fallback")

    telegram_cmd = shutil.which("telegram-send")
    if telegram_cmd:
        try:
            subprocess.run(
                [telegram_cmd, text], timeout=15, capture_output=True, env=os.environ
            )
        except Exception as exc:
            log.warning(f"telegram-send fallback failed: {exc}")


def telegram_send(text: str, attachments: list[str] | None = None) -> None:
    """
    Deliver an operator notification.

    Operator prefers email (2026-07-13): when ATELES_NOTIFY_EMAIL=1, deliver via
    `gws gmail +send` and only fall through to Telegram (break-glass) if email
    delivery fails. When the flag is off, behaviour is unchanged (Telegram only).
    Name kept as `telegram_send` so existing call sites are untouched.

    `attachments` (e.g. Wise receipt PDFs) ride the email path; the Telegram
    fallback is text-only, so a note is appended listing what didn't attach.

    NOTE: the interactive approval poll (`telegram_long_poll_once`) still reads
    replies from Telegram, so calendar-triggered payments that wait for a reply
    require Telegram for the *reply* channel. Task-reminder previews (the common
    path here) are one-way and are satisfied by email alone.
    """
    if _email_send(text, attachments=attachments):
        return
    if attachments:
        names = ", ".join(Path(a).name for a in attachments)
        text = f"{text}\n\n(📎 receipts available but not attachable via Telegram: {names})"
    _telegram_only(text)


def telegram_long_poll_once(timeout_sec: int = 120) -> str | None:
    """
    Long-poll Telegram getUpdates for one incoming message from the allowed user
    in the correct chat.

    Returns the message text (stripped) if a matching message arrives within
    timeout_sec, or None on timeout.

    Uses a file-based offset tracker to avoid reprocessing old messages.
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set — cannot poll")
        return None

    offset_file = Path(__file__).parent / ".monedula_tg_offset"
    offset = 0
    if offset_file.exists():
        try:
            offset = int(offset_file.read_text().strip())
        except ValueError:
            offset = 0

    deadline = time.monotonic() + timeout_sec
    allowed_user_id = (
        int(TELEGRAM_ALLOWED_USER_ID) if TELEGRAM_ALLOWED_USER_ID else None
    )
    chat_id = int(TELEGRAM_CHAT_ID)

    log.info(f"Polling Telegram for reply (timeout={timeout_sec}s, offset={offset})...")

    while time.monotonic() < deadline:
        remaining = int(deadline - time.monotonic())
        if remaining <= 0:
            break

        poll_timeout = min(remaining, 30)  # max 30s per request
        url = (
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
            f"?offset={offset}&timeout={poll_timeout}&allowed_updates=message"
        )

        try:
            with urllib.request.urlopen(url, timeout=poll_timeout + 5) as resp:
                data = json.loads(resp.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            log.warning(f"Telegram getUpdates request failed: {exc} — retrying")
            time.sleep(2)
            continue
        except json.JSONDecodeError as exc:
            log.warning(f"Telegram getUpdates JSON parse error: {exc}")
            time.sleep(2)
            continue

        updates = data.get("result") or []
        for update in updates:
            update_id = update.get("update_id", 0)
            offset = max(offset, update_id + 1)
            offset_file.write_text(str(offset))

            msg = update.get("message") or {}
            from_user = msg.get("from") or {}
            msg_chat = msg.get("chat") or {}
            user_id = from_user.get("id")
            msg_chat_id = msg_chat.get("id")

            # Filter to correct chat and allowed user
            if msg_chat_id != chat_id:
                continue
            if allowed_user_id and user_id != allowed_user_id:
                continue

            text = (msg.get("text") or "").strip()
            if text:
                log.info(f"Received Telegram reply: {text!r}")
                return text

    log.info("Telegram poll timed out — no reply received")
    return None


# ---------------------------------------------------------------------------
# Payment dispatch logic
# ---------------------------------------------------------------------------


def _parse_reply(reply: str | None, handler_names: list[str]) -> set[str]:
    """
    Parse the operator's Telegram reply and return the set of handler names
    to execute.

    "yes all"      → all handlers
    "yes yoga"     → {"yoga"}
    "yes therapy"  → {"therapy"}
    "no"           → empty set (skip all)
    None/timeout   → empty set (skip all)
    """
    if not reply:
        return set()

    low = reply.lower().strip()

    if low in ("no", "no all", "skip", "skip all", "n"):
        return set()

    if low in ("yes", "yes all", "y", "y all"):
        return set(handler_names)

    # "yes yoga", "yes therapy", "y yoga", etc.
    for name in handler_names:
        if low in (f"yes {name}", f"y {name}", name):
            return {name}

    log.warning(f"Unrecognised reply: {reply!r} — treating as skip all")
    return set()


def _build_preview_message(
    triggered: list[tuple],
    yesterday_str: str,
    due_tasks: list[dict] | None = None,
) -> str:
    """Build the Telegram preview message for all triggered payments."""
    lines = [f"💸 Monedula — payment check for {yesterday_str}", ""]

    # Calendar-triggered payments
    if triggered:
        lines.append("📅 *Calendar-triggered payments*")
        lines.append("")
        for handler, matches in triggered:
            for match in matches:
                lines.append(handler.preview(match))
                lines.append("")

    # Neotoma task-based reminders
    if due_tasks:
        lines.append("📋 *Due payment tasks (Neotoma)*")
        lines.append("")
        for task in due_tasks:
            fields = task.get("snapshot") or task.get("fields") or task
            name = str(fields.get("title") or fields.get("name") or "(unnamed)")
            due = str(fields.get("due_date") or "")
            description = str(fields.get("description") or "")
            overdue = due and due < yesterday_str
            due_label = f"⚠️ overdue since {due}" if overdue else f"due {due}"
            lines.append(f"  • {name} ({due_label})")
            if description:
                # Show first 120 chars of description as context
                short_desc = description[:120].rstrip()
                if len(description) > 120:
                    short_desc += "…"
                lines.append(f"    {short_desc}")
        lines.append("")

    # Reply instructions
    handler_names = list(dict.fromkeys([h.name for h, _ in triggered]))
    lines += [
        "Reply:",
        "  yes all     — pay all calendar payments",
    ]
    for name in handler_names:
        lines.append(f"  yes {name:<10} — pay {name} only")
    lines.append("  no          — skip all")
    if due_tasks:
        lines.append("")
        lines.append(
            "  Task payments auto-execute once approved: set payment_approved=true"
        )
        lines.append(
            "  on the task in Neotoma (Inspector / CLI / ask Ateles). Monedula runs"
        )
        lines.append(
            "  them on its next poll (dry-run until MONEDULA_DRYRUN=0 is set)."
        )

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    log.info("Monedula starting.")

    # The once-daily guard applies to the CALENDAR-triggered path only: that path
    # pays for yesterday's sessions and must fire exactly once per day.
    #
    # The task/approval path must run on EVERY poll — it reads the operator's
    # emailed APPROVE/SKIP replies, which can arrive at any time. Gating the whole
    # run on "already ran today" made every scheduled poll a no-op after the first,
    # so replies were never processed. That path is independently idempotent
    # (payment_approved gate + _task_already_paid), so polling it is safe.
    calendar_leg_done_today = _check_already_ran_today()
    if calendar_leg_done_today:
        log.info("Calendar leg already ran today — task/approval leg still polling.")
    else:
        # Mark as started immediately to prevent concurrent launchd re-launches
        # from double-paying the calendar leg.
        _mark_ran_today()

    yesterday = _yesterday()
    yesterday_str = yesterday.isoformat()
    today_str = date.today().isoformat()
    log.info(f"Checking calendar for yesterday: {yesterday_str}")

    # Load handlers from env-var-defined payment profiles.
    # Set MONEDULA_PROFILES=THERAPY,YOGA (and corresponding profile env vars).
    from handlers import load_handlers

    all_handlers = load_handlers()

    # Calendar leg — skipped once it has already run today (it pays for
    # yesterday's sessions and must not fire twice).
    triggered: list[tuple] = []  # [(handler, [match, ...]), ...]
    if calendar_leg_done_today:
        log.info("Skipping calendar leg (already ran today).")
    else:
        events = fetch_yesterday_events()
        for handler in all_handlers:
            matches = handler.matches(events)
            if matches:
                triggered.append((handler, matches))

        if triggered:
            log.info(f"Triggered handlers: {[h.name for h, _ in triggered]}")

    # Fetch due payment tasks from Neotoma, scoped to profile-linked task IDs only.
    due_tasks = fetch_due_payment_tasks(all_handlers)

    # Abort early only if there's truly nothing to show
    if not triggered and not due_tasks:
        log.info(
            "No payment handlers triggered and no due payment tasks — nothing to do."
        )
        return

    if not triggered:
        log.info(
            "No calendar-triggered payments, but due payment tasks found — sending reminder only."
        )

    # Build and send the daily preview/digest — only on the calendar leg's
    # once-daily run. On the 15-min approval polls this would re-send every
    # cycle, so it is suppressed there.
    if not calendar_leg_done_today:
        preview_msg = _build_preview_message(triggered, yesterday_str,
                                             due_tasks=due_tasks)
        log.info("Sending payment preview...")
        telegram_send(preview_msg)

    # Email reply-to-approve: first apply any APPROVE/SKIP <token> replies the
    # operator sent since the last run (flips payment_approved on matching tasks).
    if due_tasks:
        applied = process_email_approvals(due_tasks)
        if applied:
            log.info(f"[approve] Applied {applied} email approval(s); re-fetching tasks.")
            due_tasks = fetch_due_payment_tasks(all_handlers)

    # Task-based auto-execute (approval-gated). Runs independently of the
    # calendar-triggered interactive path: any due task carrying
    # payment_approved=true is executed via its linked handler (dry-run unless
    # MONEDULA_DRYRUN=0). Then a per-payment confirmation is emailed.
    if due_tasks:
        task_results = execute_approved_tasks(due_tasks, all_handlers)

        # Only REAL outcomes are worth an email. A dry-run is a rehearsal, and
        # this leg polls every ~15 min — emailing dry-run results notified the
        # operator every cycle (and, worse, rendered them as "payment failed:
        # unknown error"). Report only actually-attempted payments.
        reportable = [(h, r) for h, r in task_results
                      if r.get("status") != "dry_run"]
        if task_results and not reportable:
            log.info(f"[autoexec] {len(task_results)} dry-run result(s) — "
                     f"not emailing (rehearsal only).")

        if reportable:
            # Confirm each payment as a REPLY in its own approval thread, so the
            # receipt lands in the conversation the operator approved from.
            # Anything without a known thread falls back to a standalone digest.
            unthreaded: list[tuple] = []
            for handler, result in reportable:
                text = (handler.format_confirmation(result)
                        if hasattr(handler, "format_confirmation")
                        else f"{handler.name}: {result.get('status')}")
                rp = result.get("receipt_path")
                receipts = [rp] if rp and os.path.exists(rp) else []

                tid = str(getattr(getattr(handler, "profile", None),
                                  "neotoma_task_id", "") or "")
                msg_id = _REPLY_MSG_IDS.get(_payment_token(tid)) if tid else None
                if msg_id and _email_reply_in_thread(msg_id, text,
                                                     attachments=receipts):
                    continue
                unthreaded.append((handler, result))

            if unthreaded:
                conf_lines = [f"📋 Monedula task-payment results for {today_str}:", ""]
                receipts = []
                for handler, result in unthreaded:
                    conf_lines.append(
                        handler.format_confirmation(result)
                        if hasattr(handler, "format_confirmation")
                        else f"{handler.name}: {result.get('status')}")
                    rp = result.get("receipt_path")
                    if rp and os.path.exists(rp):
                        receipts.append(rp)
                    conf_lines.append("")
                telegram_send("\n".join(conf_lines).rstrip(), attachments=receipts)

            _mark_tasks_paid(reportable, due_tasks)

        # For any tasks still awaiting approval, email a per-payment approval
        # request (with its token) so the operator can approve by replying.
        requested = send_approval_requests(due_tasks, all_handlers)
        if requested:
            log.info(f"[approve] Emailed {requested} approval request(s).")

    # If there are only task reminders (no actionable calendar payments), don't wait for approval.
    if not triggered:
        log.info("Task auto-execute complete — no calendar payments to approve. Done.")
        return

    # Wait for operator reply (2 minutes)
    reply = telegram_long_poll_once(timeout_sec=120)

    handler_names = list(dict.fromkeys([h.name for h, _ in triggered]))
    approved = _parse_reply(reply, handler_names)

    if not approved:
        log.info(f"No payments approved (reply={reply!r}) — skipping all.")
        telegram_send(f"⏭️ Monedula: skipped all payments for {yesterday_str}.")
        return

    log.info(f"Approved handlers: {approved}")

    # Execute approved payments
    all_results = []
    for handler, matches in triggered:
        if handler.name not in approved:
            log.info(f"Skipping {handler.name} (not approved).")
            continue
        for match in matches:
            log.info(f"Executing {handler.name} payment...")
            _job = _activity.started(f"executing {handler.name} payment") if _activity else None
            try:
                result = handler.execute(match)
                all_results.append((handler, result))
                log.info(f"{handler.name} result: {result}")
                if _job:
                    # Keep summary generic — no amounts, IBANs, or memos.
                    _job.finished(f"{handler.name} payment executed")
            except Exception as _exc:
                if _job:
                    _job.failed(f"{handler.name} payment error: {type(_exc).__name__}")
                raise

    # Send confirmation
    if not all_results:
        telegram_send(f"⚠️ Monedula: no payments executed for {yesterday_str}.")
        return

    confirmation_lines = [f"📋 Monedula results for {yesterday_str}:", ""]
    for handler, result in all_results:
        if hasattr(handler, "format_confirmation"):
            conf = handler.format_confirmation(result)
        else:
            conf = json.dumps(result, indent=2)
        confirmation_lines.append(conf)
        confirmation_lines.append("")

    confirmation_msg = "\n".join(confirmation_lines).rstrip()
    log.info("Sending confirmation to Telegram...")
    telegram_send(confirmation_msg)
    log.info("Monedula run complete.")


if __name__ == "__main__":
    _notify("monedula started", priority="info")
    try:
        main()
        _notify("monedula run complete", priority="info")
    except Exception as exc:
        log.exception(f"Monedula fatal error: {exc}")
        _notify(f"monedula fatal error: {exc}", priority="blocker")
        try:
            telegram_send(f"🔴 Monedula fatal error: {exc}")
        except Exception:
            pass
        sys.exit(1)
