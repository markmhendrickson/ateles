"""
lib/notify/email_gate.py — the single global kill-switch for swarm outbound email.

Why this exists
---------------
On 2026-08-31/09-01 the swarm sent 500+ `[Ateles]` emails in 24 hours against 7
personal emails from the operator. That exhausted the Gmail free-tier sending
quota (~500 recipients/day) and the operator's OWN reply to a client thread
bounced with "You have reached a limit for sending mail." (ateles#645). The
swarm's chatter consumed a quota the operator depends on for real work.

The fix is deliberately NOT "stop the daemons". Dispatch keeps running; only the
email *send* path goes silent. `APIS_DRY_RUN=1` would have been the blunt
instrument — it halts task dispatch entirely, which is a far larger outage than
the problem it solves.

Contract
--------
`email_enabled()` is read AT SEND TIME (never cached at import), so flipping the
env var and restarting a daemon is enough — no code change, no redeploy.

Default is ENABLED. This is an explicit opt-OUT: a missing variable must never
silently disable operator alerting, because "no mail" and "nothing to say" would
become indistinguishable. Set `ATELES_NOTIFY_EMAIL_ENABLED=0` to suppress.

Suppression is NOT deletion
---------------------------
ateles#583 and #636 are the cautionary tale: 45 escalations queued, zero ever
sent, and nobody noticed because silence looked like calm. So a suppressed
message is still fully constructed and then RECORDED to a durable JSONL sink
before being dropped from the wire. Silence must remain auditable — the operator
can always answer "what would you have emailed me?".

Accepted values for "off": 0, false, no, off (case-insensitive). Anything else,
including an unset variable, means enabled.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger("notify.email_gate")

#: Env var controlling the global outbound-email kill-switch.
ENV_FLAG = "ATELES_NOTIFY_EMAIL_ENABLED"

#: Env var overriding where suppressed notifications are recorded.
ENV_SINK = "ATELES_SUPPRESSED_EMAIL_LOG"

_FALSEY = {"0", "false", "no", "off"}


def email_enabled() -> bool:
    """True unless the kill-switch is explicitly set to a falsey value.

    Read at call time, never memoized: a long-lived daemon must observe the
    operator's decision on its next send, not the value that happened to be set
    when the module was first imported.
    """
    raw = os.environ.get(ENV_FLAG)
    if raw is None:
        return True
    return raw.strip().lower() not in _FALSEY


def suppressed_log_path() -> Path:
    """Where suppressed notifications are recorded."""
    override = os.environ.get(ENV_SINK, "").strip()
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "ateles-suppressed-email.jsonl"


def record_suppressed(
    *,
    channel: str,
    subject: str,
    body: str,
    to: str = "",
    meta: dict | None = None,
) -> bool:
    """Durably record a notification that the kill-switch stopped from sending.

    Returns True if the record was written. Fail-open by design: a failure to
    write the audit line must never crash — or block — the daemon that was only
    trying to notify. It is logged at WARNING so a broken sink is still visible.
    """
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "channel": channel,
        "to": to,
        "subject": subject,
        "body": body,
        "suppressed_by": ENV_FLAG,
    }
    if meta:
        entry["meta"] = meta

    # Always leave a trace in the daemon log even if the file sink fails, so the
    # suppressed message is recoverable from two independent places.
    log.warning(
        "[email_gate] SUPPRESSED (%s not emailed; %s=0): %s",
        channel, ENV_FLAG, subject[:120],
    )

    path = suppressed_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:  # noqa: BLE001 — auditing must not break delivery logic
        log.warning("[email_gate] could not write suppressed-email sink %s: %s", path, exc)
        return False
