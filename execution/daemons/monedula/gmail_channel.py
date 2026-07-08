"""
gmail_channel.py — Email notify/approval channel for Monedula, via the `gws` CLI.

Replaces the old Telegram preview/approval/confirmation flow. All payment
previews, approvals, and confirmations go through Gmail; `gws` invocations
are isolated in this module behind small, documented helper functions so the
operator can verify/adjust the exact CLI flags in one place.

Verified `gws` subcommands (checked directly against the installed CLI via
`gws gmail <cmd> --help` during this build — see the daemon rewrite PR/task
for the transcript):
  gws gmail +send  --to <email> --subject <s> --body <html> --html --format json
      Sends a fresh message. Returns the standard Gmail API `messages.send`
      response as JSON: {"id": "<message id>", "threadId": "<thread id>", ...}
  gws gmail +read  --id <message-id> --format json [--html]
      Reads a single message body (used to inspect the operator's reply).
  gws gmail users messages list --params '<json>' --format json
      Raw `users.messages.list` passthrough — used to find reply messages
      within a thread (query: `threadId:<id>`).
  gws gmail users threads get --params '{"userId":"me","id":"<id>","format":"metadata"}' --format json
      Raw `users.threads.get` passthrough — used to enumerate every message
      in a thread (to detect an operator reply after our notify message).

ASSUMPTION FLAGGED FOR OPERATOR REVIEW: `+send` is documented to accept
`--html` for an HTML body but the helper always sends single-part HTML
(gws handles MIME wrapping internally — it is not a raw multipart/alternative
message we construct by hand). If the operator wants literal
multipart/alternative with a plain-text part, that requires either passing
non-HTML body text as well (which `+send`/`+reply` do not support in one
call) or dropping to `users.messages.send` with a hand-built raw MIME
message. This module currently relies on `--html` alone; flagged here as a
TODO rather than guessed at, per the task's isolate-and-document instruction.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from typing import Any, Literal, Optional

log = logging.getLogger(__name__)

GWS_TIMEOUT_SEC = 30


def _gws_path() -> str | None:
    return shutil.which("gws")


def send_email(
    to: str,
    subject: str,
    html_body: str,
) -> dict[str, Any] | None:
    """
    Send a fresh HTML email via `gws gmail +send`.

    Returns the parsed JSON response (expected keys: id, threadId) on
    success, or None on failure (never raises — callers must fail-safe).
    """
    gws = _gws_path()
    if not gws:
        log.error("gws CLI not found in PATH — cannot send email")
        return None

    try:
        result = subprocess.run(
            [
                gws,
                "gmail",
                "+send",
                "--to",
                to,
                "--subject",
                subject,
                "--body",
                html_body,
                "--html",
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=GWS_TIMEOUT_SEC,
            env=os.environ,
        )
        if result.returncode != 0:
            log.error(f"gws gmail +send failed: {result.stderr.strip()[:400]}")
            return None
        return _parse_json_output(result.stdout)
    except subprocess.TimeoutExpired:
        log.error("gws gmail +send timed out")
        return None
    except Exception as exc:
        log.error(f"gws gmail +send error: {exc}")
        return None


def _parse_json_output(stdout: str) -> dict[str, Any] | None:
    """
    `gws` prints a keyring-backend preamble line before JSON on some
    invocations (observed in local testing) — strip any leading non-JSON
    lines before parsing.
    """
    text = stdout.strip()
    if not text:
        return None
    # Find the first '{' or '[' and parse from there.
    for i, ch in enumerate(text):
        if ch in "{[":
            candidate = text[i:]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    log.error(f"Could not locate JSON in gws output: {text[:300]!r}")
    return None


def list_thread_messages(thread_id: str) -> list[dict[str, Any]]:
    """
    Return every message in a Gmail thread (metadata only) via the raw
    `users.threads.get` passthrough. Each item has at least 'id' and
    'internalDate'; failure returns an empty list (fail-safe — an empty
    thread read must never be treated as "operator replied").
    """
    gws = _gws_path()
    if not gws:
        log.error("gws CLI not found in PATH — cannot read thread")
        return []

    params = {"userId": "me", "id": thread_id, "format": "metadata"}
    try:
        result = subprocess.run(
            [
                gws,
                "gmail",
                "users",
                "threads",
                "get",
                "--params",
                json.dumps(params),
                "--format",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=GWS_TIMEOUT_SEC,
            env=os.environ,
        )
        if result.returncode != 0:
            log.warning(f"gws threads get failed: {result.stderr.strip()[:400]}")
            return []
        data = _parse_json_output(result.stdout)
        if not data:
            return []
        return data.get("messages") or []
    except subprocess.TimeoutExpired:
        log.warning("gws threads get timed out")
        return []
    except Exception as exc:
        log.warning(f"gws threads get error: {exc}")
        return []


def read_message_text(message_id: str) -> str | None:
    """
    Read a single message's plain-text body via `gws gmail +read`.
    Returns None on any failure (fail-safe).
    """
    gws = _gws_path()
    if not gws:
        log.error("gws CLI not found in PATH — cannot read message")
        return None

    try:
        result = subprocess.run(
            [gws, "gmail", "+read", "--id", message_id, "--format", "json"],
            capture_output=True,
            text=True,
            timeout=GWS_TIMEOUT_SEC,
            env=os.environ,
        )
        if result.returncode != 0:
            log.warning(f"gws +read failed for {message_id}: {result.stderr.strip()[:300]}")
            return None
        data = _parse_json_output(result.stdout)
        if not data:
            return None
        # +read --format json is expected to expose the plain-text body
        # under a 'body' key (mirrors the --format json example in
        # `gws gmail +read --help`).
        body = data.get("body")
        return str(body) if body is not None else None
    except subprocess.TimeoutExpired:
        log.warning(f"gws +read timed out for {message_id}")
        return None
    except Exception as exc:
        log.warning(f"gws +read error for {message_id}: {exc}")
        return None


def find_operator_reply(
    thread_id: str, notify_message_id: str
) -> str | None:
    """
    Look for an operator reply in the thread that arrived AFTER the
    notify message we sent. Returns the reply body text, or None if no
    reply has arrived yet (or on any error — fail-safe: "no reply" must
    never be misread as "approved").

    Strategy: list thread messages (metadata, ordered oldest-first by
    Gmail), find the notify message's position, and read the body of the
    first later message (if any).
    """
    messages = list_thread_messages(thread_id)
    if not messages:
        return None

    ids = [m.get("id") for m in messages]
    if notify_message_id not in ids:
        log.warning(
            f"notify message {notify_message_id!r} not found in thread {thread_id!r} "
            f"— cannot determine reply position, treating as no reply yet"
        )
        return None

    idx = ids.index(notify_message_id)
    later_ids = ids[idx + 1 :]
    if not later_ids:
        return None

    # Take the most recent later message as the operator's latest reply.
    reply_id = later_ids[-1]
    return read_message_text(reply_id)


def parse_approval_reply(text: str | None) -> Optional[Literal["approve", "skip"]]:
    """
    Parse an operator's email reply into an approval decision.

    Returns "approve", "skip", or None (no clear/recognised decision —
    treat as still-pending, never as approval).

    Accepted vocabulary (case/whitespace-insensitive, matched against the
    first non-empty line to ignore quoted history):
      approve: "yes", "y", "approve", "pay", "attended"
      skip:    "no", "n", "skip"
    """
    if not text:
        return None

    first_line = ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break

    if not first_line:
        return None

    low = first_line.lower().strip()
    # Strip common trailing punctuation.
    low = low.rstrip(".!")

    approve_words = {"yes", "y", "approve", "pay", "attended"}
    skip_words = {"no", "n", "skip"}

    first_token = low.split()[0] if low.split() else ""

    if low in approve_words or first_token in approve_words:
        return "approve"
    if low in skip_words or first_token in skip_words:
        return "skip"

    log.info(f"Unrecognised email reply first line: {first_line!r} — treating as pending")
    return None
