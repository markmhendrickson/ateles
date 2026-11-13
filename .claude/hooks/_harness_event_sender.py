#!/usr/bin/env python3
"""Detached harness_event sender — started by `_session_integrity._launch_sender`.

NOT a hook and not wired in `.claude/settings.json`. A Stop hook starts this
as a child in its own session (`start_new_session=True`) with no inherited
stdio and an allowlisted environment, writes one JSON payload to its stdin,
and exits without waiting, so a Neotoma instance that is slow to accept
writes can no longer delay a Stop.

The payload — base URL, bearer, request body and diagnostics path — arrives
ONLY on stdin. Nothing secret is ever on this process's argv or in its
environment. The POST itself (60 s wall-clock deadline per attempt, one retry
with the same idempotency key on transient errors only, redirects refused by
`_NoRedirectHandler`) is `_session_integrity.deliver_harness_event`; this
file only reads stdin and calls it, so the transport has exactly one
implementation.

LIFETIME. An alarm caps the whole process: first at
`HARNESS_EVENT_SENDER_MAX_LIFETIME_S` (covering a stdin that never closes),
then re-armed to what is left of `sender_lifetime_s(payload)` (elapsed time
subtracted) once the payload is known. An exit forced by the alarm writes one
log line (key only) when the payload was already read. It is a
backstop to the per-attempt deadline, so a stuck DNS lookup or a
slow-drip response can never keep this process alive past the cap.

Run with `python3 -I`, which drops the script directory from `sys.path`, so
the hooks directory is added back explicitly. Stdlib only. Always exits 0.
"""
import json
import os
import signal
import sys
import time
from pathlib import Path

# Matches _session_integrity.HARNESS_EVENT_SENDER_MAX_LIFETIME_S; set before
# the import so even a hung import or stdin read is bounded.
_BOOTSTRAP_LIFETIME_S = 132


# Filled in once the payload and `_session_integrity` are known, so a
# lifetime exit can leave one log line (key only — never a token).
_ALARM_CTX: dict = {}


def _on_alarm(*_args) -> None:
    try:
        si = _ALARM_CTX.get("si")
        if si is not None and _ALARM_CTX.get("log_path"):
            si._append_sender_log(
                _ALARM_CTX["log_path"],
                f"[{_ALARM_CTX.get('log_tag', 'harness-event')}] sender lifetime "
                f"cap reached; exiting without a result; key={_ALARM_CTX.get('key', '?')}",
            )
    except BaseException:  # noqa: BLE001 — the exit below must happen regardless
        pass
    os._exit(0)


def _arm_lifetime(seconds: int) -> None:
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, _on_alarm)
        signal.alarm(max(1, int(seconds)))


def main() -> int:
    try:
        started = time.monotonic()
        _arm_lifetime(_BOOTSTRAP_LIFETIME_S)
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
        if not isinstance(payload, dict):
            return 0
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _session_integrity as si  # noqa: PLC0415 — after sys.path setup

        body = payload.get("body")
        _ALARM_CTX.update(
            si=si,
            log_path=str(payload.get("log_path") or ""),
            log_tag=str(payload.get("log_tag") or "harness-event"),
            key=(str(body.get("idempotency_key") or "?")[:200]
                 if isinstance(body, dict) else "?"),
        )
        # Re-arm for what is LEFT of the payload's cap, so the whole process
        # (stdin read + import + delivery) is bounded, not delivery alone.
        remaining = si.sender_lifetime_s(payload) - (time.monotonic() - started)
        _arm_lifetime(int(remaining) + 1 if remaining > 1 else 1)
        si.deliver_harness_event(payload)
    except Exception:  # noqa: BLE001 — fail open; nothing may surface from here
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException:  # noqa: BLE001
        sys.exit(0)
