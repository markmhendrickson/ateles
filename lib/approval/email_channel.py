"""Gmail send / inbox-scan / in-thread-reply plumbing for email approvals.

Thin wrappers over the `gws gmail` CLI. Agent-agnostic: no per-agent state files,
no domain types — the caller owns dedup state and decides what a token maps to.

Every function is FAIL-OPEN: a missing gws CLI, unset env, non-zero exit, or a
transport error logs a warning and returns a benign empty/false value. None of
them raise into the caller's daemon loop.

Env contract:
  ATELES_NOTIFY_EMAIL  "1" arms the channel; anything else → every call no-ops
  OPERATOR_EMAIL       request recipient AND the verified reply --to
  ATELES_SWARM_EMAIL   optional From: (the swarm's own address)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("ateles.approval.email")


def email_enabled() -> bool:
    """True only when the operator has armed the email channel."""
    return os.environ.get("ATELES_NOTIFY_EMAIL", "0") == "1"


def operator_email() -> str:
    """The operator's address, or '' if unset."""
    return os.environ.get("OPERATOR_EMAIL", "").strip()


def _swarm_from() -> str:
    return os.environ.get("ATELES_SWARM_EMAIL", "").strip()


def _gws() -> str | None:
    return shutil.which("gws")


def gws_json(args: list[str], timeout: int = 45) -> Any:
    """Run a `gws` command and parse its JSON, tolerating the banner preamble.

    gws prints a keyring/format banner before the JSON, so we start parsing at
    the first '{' or '['. Returns None on any failure (fail-open).
    """
    gws = _gws()
    if not gws:
        return None
    try:
        r = subprocess.run([gws, *args], capture_output=True, text=True,
                           timeout=timeout, env=os.environ)
        if r.returncode != 0:
            log.warning(f"gws {args[:2]} failed: {(r.stderr or '').strip()[:160]}")
            return None
        out = r.stdout or ""
        start = min([i for i in (out.find("{"), out.find("[")) if i >= 0] or [-1])
        if start < 0:
            return None
        return json.loads(out[start:])
    except Exception as exc:  # noqa: BLE001
        log.warning(f"gws json error: {exc}")
        return None


def send_request(subject: str, body: str, to: str | None = None) -> bool:
    """Send an approval-request email with an explicit subject (the token lives
    in the subject). Returns True on success, False on any failure (fail-open).

    `to` defaults to OPERATOR_EMAIL. Gated by ATELES_NOTIFY_EMAIL.
    """
    if not email_enabled():
        return False
    recipient = (to or operator_email()).strip()
    gws = _gws()
    if not gws or not recipient:
        return False
    cmd = [gws, "gmail", "+send", "--to", recipient,
           "--subject", subject, "--body", body]
    swarm = _swarm_from()
    if swarm:
        cmd += ["--from", swarm]
    try:
        r = subprocess.run(cmd, timeout=60, capture_output=True, text=True,
                           env=os.environ)
        if r.returncode != 0:
            log.warning(f"approval email send failed (rc={r.returncode}): "
                        f"{(r.stderr or '').strip()[:160]}")
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(f"approval email error: {exc}")
        return False


def read_replies(
    tokens: list[str],
    max_msgs: int = 40,
    on_reply_message: Callable[[str, str], None] | None = None,
) -> list[str]:
    """Return the full text (subject + body) of recent replies carrying any token.

    `gws gmail +triage` returns only headers (date/from/id/subject) — NOT the
    body — so a verdict is invisible there. We triage to find candidate message
    ids whose subject carries a token, then `+read --id` each to pull the body.

    Only messages whose subject starts with "RE:" are considered — the operator's
    reply can carry a verdict; our own outbound request cannot.

    `on_reply_message(token, message_id)` is invoked for each matched reply, so
    the caller can persist which message to reply to (for in-thread confirmation).
    Fail-open: returns [] on any failure.
    """
    if not tokens or not email_enabled():
        return []
    texts: list[str] = []
    seen_ids: set[str] = set()

    for token in tokens:
        data = gws_json(["gmail", "+triage", "--format", "json", "--max",
                         str(max_msgs), "--query", f"newer_than:3d {token}"])
        msgs: list[dict] = []
        if isinstance(data, dict):
            msgs = data.get("messages") or data.get("results") or []
        elif isinstance(data, list):
            msgs = data
        for m in msgs:
            mid = str(m.get("id") or "")
            subject = str(m.get("subject") or "")
            if not mid or mid in seen_ids or not subject.upper().startswith("RE:"):
                continue
            seen_ids.add(mid)
            body_data = gws_json(["gmail", "+read", "--id", mid, "--headers",
                                  "--format", "json"], timeout=30)
            body = ""
            if isinstance(body_data, dict):
                body = str(body_data.get("body_text")
                           or body_data.get("body")
                           or body_data.get("text") or "")
            if on_reply_message is not None:
                try:
                    on_reply_message(token, mid)
                except Exception as exc:  # noqa: BLE001
                    log.warning(f"on_reply_message callback failed: {exc}")
            texts.append(f"{subject}\n{body}")
    return texts


def reply_in_thread(message_id: str, body: str,
                    attachments: list[str] | None = None,
                    cwd: str | None = None) -> bool:
    """Reply to `message_id` so a confirmation lands in the SAME thread as the
    operator's approval. Uses `gws gmail +reply`, which handles threading.

    Passes --to OPERATOR_EMAIL EXPLICITLY: `+reply` can misaddress when replying
    to a message the operator themselves sent (a known gws quirk — it would send
    the confirmation back to the operator's own thread partner rather than the
    operator). Fail-open: returns False so the caller can fall back to a
    standalone email.

    `attachments` are staged into `cwd` (a temp dir under it) and passed as
    repo-relative --attach paths, then cleaned up.
    """
    if not email_enabled():
        return False
    recipient = operator_email()
    gws = _gws()
    if not gws or not message_id or not recipient:
        return False

    base = Path(cwd) if cwd else Path.cwd()
    cmd = [gws, "gmail", "+reply", "--message-id", message_id,
           "--body", body, "--to", recipient]
    swarm = _swarm_from()
    if swarm:
        cmd += ["--from", swarm]

    staged: list[Path] = []
    stage_dir = base / ".approval_attach_tmp"
    for src in (attachments or []):
        try:
            if not os.path.exists(src):
                continue
            stage_dir.mkdir(parents=True, exist_ok=True)
            dst = stage_dir / Path(src).name
            shutil.copyfile(src, dst)
            staged.append(dst)
            cmd += ["--attach", str(dst.relative_to(base))]
        except Exception as exc:  # noqa: BLE001
            log.warning(f"could not stage attachment {src}: {exc}")

    try:
        r = subprocess.run(cmd, timeout=60, capture_output=True, text=True,
                           cwd=str(base), env=os.environ)
        if r.returncode != 0:
            log.warning(f"+reply failed (rc={r.returncode}): "
                        f"{(r.stderr or '').strip()[:160]}")
            return False
        log.info(f"confirmation replied in thread ({message_id}).")
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning(f"+reply error: {exc}")
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
