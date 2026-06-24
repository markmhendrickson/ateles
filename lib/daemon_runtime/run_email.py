"""
lib/daemon_runtime/run_email.py — outbound email on a task execution-run thread.

E2 of the email-driven execution loop (docs/task_execution_loop.md). Email is the
swarm's preferred operator transport (decision:email_replaces_telegram_as_transport):
Apis sends kickoff / progress / outcome on ONE Gmail thread per execution run, and
the inbound daemon (E3) threads operator replies back to the same task.

Threading is STATELESS and deterministic — we never have to capture the real
Message-ID of the first send. Every run derives a stable root id from
(task_id, run_key); the kickoff email IS that root, and every later message sets
In-Reply-To/References to it. So a daemon restart mid-run still threads correctly,
and the inbound daemon can parse the task_id straight out of the References chain
(with the `[#ent_…]` subject token as a fallback).

Config (operator-agnostic — sourced from env, never hardcoded):
  ATELES_SWARM_EMAIL      dedicated swarm From address (e.g. swarm@<domain>).
  OPERATOR_EMAIL          recipient (the operator), per the repo-wide convention.
  ATELES_GMAIL_SEND_CMD   send template, shared with dispatch_report.deliver():
                          placeholders {eml} {to} {subject}, e.g.
                          'gws gmail send --raw {eml} --to {to}'.
  ATELES_EMAIL_DOMAIN     Message-ID domain when the swarm address has none
                          (default 'ateles.swarm').

Fail-open everywhere: a missing address / send command logs and returns False —
it never raises and never blocks dispatch. The swarm address must be provisioned
(mailbox created + gws authed for it) before sends land; until then the .eml is
still built and the intent logged.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from email.message import EmailMessage
from email.utils import formatdate

log = logging.getLogger("daemon_runtime.run_email")

# Subject token that carries the task id. This is the PRIMARY inbound matcher:
# Gmail rewrites the Message-ID on send (confirmed 2026-06-24 — a sent
# <run-…@gmail.com> came back as <CANB…@mail.gmail.com>), so a reply's References
# chain usually won't carry our synthetic root; the subject token (preserved as
# "Re: [#ent_…]") is what reliably survives. Shape: [#ent_abc123].
_TOKEN_RE = re.compile(r"\[#(ent_[0-9a-z]+)\]")


def _domain() -> str:
    addr = os.environ.get("ATELES_SWARM_EMAIL", "").strip()
    if "@" in addr:
        return addr.rsplit("@", 1)[1] or os.environ.get("ATELES_EMAIL_DOMAIN", "ateles.swarm")
    return os.environ.get("ATELES_EMAIL_DOMAIN", "ateles.swarm")


def run_subject(task_id: str, title: str) -> str:
    """Stable subject for every message in a run: '[#<task_id>] <title>'.

    The token is what makes the whole run share one Gmail thread (subject-grouping)
    AND lets the inbound daemon recover the task id from a reply's subject."""
    clean = " ".join((title or "").split())[:120] or "task"
    return f"[#{task_id}] {clean}"


def parse_task_id(text: str) -> str | None:
    """Recover the task id from a subject line or a References header value."""
    if not text:
        return None
    m = _TOKEN_RE.search(text)
    if m:
        return m.group(1)
    # References / Message-ID form: <run-ent_abc-created-0@domain> (best-effort;
    # often Gmail-rewritten — see _TOKEN_RE note).
    m = re.search(r"run-(ent_[0-9a-z]+)-", text)
    return m.group(1) if m else None


def thread_ids(
    task_id: str, run_key: str, stage: str, *, domain: str | None = None
) -> tuple[str, str | None, str | None]:
    """Return (message_id, in_reply_to, references) for one run email.

    The kickoff stage IS the thread root: its Message-ID is the deterministic
    root id and it has no In-Reply-To. Every later stage references that root, so
    threading needs no stored state. References/In-Reply-To are None for kickoff.
    """
    dom = domain or _domain()
    root = f"<run-{task_id}-{run_key}@{dom}>"
    if stage == "kickoff":
        return root, None, None
    return f"<run-{task_id}-{run_key}-{stage}@{dom}>", root, root


def build_run_eml(
    *,
    from_addr: str,
    to_addr: str,
    subject: str,
    body: str,
    message_id: str,
    in_reply_to: str | None = None,
    references: str | None = None,
) -> bytes:
    """Build an RFC822 .eml with threading headers. Pure."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["To"] = to_addr
    if from_addr:
        msg["From"] = from_addr
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    msg.set_content(body)
    return bytes(msg)


def send_run_email(
    *,
    task_id: str,
    run_key: str,
    stage: str,
    title: str,
    body: str,
) -> bool:
    """Send one run-thread email. Fail-open: returns False (never raises) when the
    swarm address, recipient, or send command is unconfigured."""
    from_addr = os.environ.get("ATELES_SWARM_EMAIL", "").strip()
    to_addr = os.environ.get("OPERATOR_EMAIL", "").strip()
    send_tmpl = os.environ.get("ATELES_GMAIL_SEND_CMD", "").strip()
    if not from_addr or not to_addr:
        log.info(
            "[run_email] swarm/recipient address unset (ATELES_SWARM_EMAIL / "
            "OPERATOR_EMAIL) — skipping %s email for task %s", stage, task_id,
        )
        return False

    subject = run_subject(task_id, title)
    mid, irt, refs = thread_ids(task_id, run_key, stage)
    eml = build_run_eml(
        from_addr=from_addr, to_addr=to_addr, subject=subject, body=body,
        message_id=mid, in_reply_to=irt, references=refs,
    )

    if not send_tmpl:
        log.info(
            "[run_email] no $ATELES_GMAIL_SEND_CMD — %s email for task %s built "
            "but not sent (provision the swarm mailbox + send command)", stage, task_id,
        )
        return False

    # gws `--upload` is sandboxed to the current directory (a /tmp path is
    # rejected), so write the .eml under cwd. The daemon runs with the repo root
    # as its WorkingDirectory; override with ATELES_RUN_EMAIL_DIR if needed.
    out_dir = os.environ.get("ATELES_RUN_EMAIL_DIR") or os.path.join(
        os.getcwd(), ".run_email_outbox"
    )
    eml_path: str | None = None
    try:
        os.makedirs(out_dir, exist_ok=True)
        fd, eml_path = tempfile.mkstemp(
            suffix=".eml", prefix=f"run-{task_id}-{stage}-", dir=out_dir
        )
        with os.fdopen(fd, "wb") as fh:
            fh.write(eml)
        # Explicit .replace, NOT str.format — the send template carries literal JSON
        # braces (e.g. --params '{"userId":"me"}') that str.format would misparse.
        cmd = (
            send_tmpl.replace("{eml}", eml_path)
            .replace("{to}", to_addr)
            .replace("{subject}", subject)
        )
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0:
            log.warning(
                "[run_email] send failed (rc=%s) for task %s stage %s: %s",
                proc.returncode, task_id, stage, (proc.stderr or "").strip()[:200],
            )
            return False
        log.info("[run_email] sent %s email for task %s (thread root run-%s-%s)",
                 stage, task_id, task_id, run_key)
        return True
    except Exception as exc:  # noqa: BLE001 — never crash the caller
        log.warning("[run_email] send error for task %s stage %s: %s", task_id, stage, exc)
        return False
    finally:
        if eml_path:
            try:
                os.unlink(eml_path)
            except OSError:
                pass


# ── self-test (pure builders) ────────────────────────────────────────────────


def _selftest() -> int:
    checks: dict[str, bool] = {}

    subj = run_subject("ent_abc123", "Pay the yoga teacher  \n  for June")
    checks["subject_token"] = subj.startswith("[#ent_abc123] ")
    checks["subject_collapsed"] = "\n" not in subj and "  " not in subj.split("] ", 1)[1]
    checks["parse_from_subject"] = parse_task_id(subj) == "ent_abc123"

    root, irt, refs = thread_ids("ent_abc123", "created-0", "kickoff", domain="d.test")
    checks["kickoff_is_root"] = root == "<run-ent_abc123-created-0@d.test>"
    checks["kickoff_no_reply"] = irt is None and refs is None

    mid, irt2, refs2 = thread_ids("ent_abc123", "created-0", "done", domain="d.test")
    checks["stage_mid"] = mid == "<run-ent_abc123-created-0-done@d.test>"
    checks["stage_refs_root"] = irt2 == root and refs2 == root
    checks["parse_from_refs"] = parse_task_id(refs2) == "ent_abc123"

    eml = build_run_eml(
        from_addr="swarm@d.test", to_addr="op@d.test", subject=subj, body="hello",
        message_id=mid, in_reply_to=irt2, references=refs2,
    )
    checks["eml_has_threading"] = b"In-Reply-To" in eml and b"References" in eml
    checks["eml_has_token"] = b"[#ent_abc123]" in eml
    checks["eml_from"] = b"swarm@d.test" in eml

    # fail-open: no addresses configured
    for var in ("ATELES_SWARM_EMAIL", "OPERATOR_EMAIL", "ATELES_GMAIL_SEND_CMD"):
        os.environ.pop(var, None)
    checks["send_fail_open"] = send_run_email(
        task_id="ent_abc123", run_key="r", stage="kickoff", title="t", body="b"
    ) is False

    ok = all(checks.values())
    for k, v in checks.items():
        print(f"[{'PASS' if v else 'FAIL'}] {k}")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys

    sys.exit(_selftest())
