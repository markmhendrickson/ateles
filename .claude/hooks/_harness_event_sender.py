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
then at `sender_lifetime_s(payload)` once the payload is known. It is a
backstop to the per-attempt deadline, so a stuck DNS lookup or a
slow-drip response can never keep this process alive past the cap.

Run with `python3 -I`, which drops the script directory from `sys.path`, so
the hooks directory is added back explicitly. Stdlib only. Always exits 0.
"""
import json
import os
import signal
import sys
from pathlib import Path

# Matches _session_integrity.HARNESS_EVENT_SENDER_MAX_LIFETIME_S; set before
# the import so even a hung import or stdin read is bounded.
_BOOTSTRAP_LIFETIME_S = 132


def _arm_lifetime(seconds: int) -> None:
    if hasattr(signal, "SIGALRM"):
        signal.signal(signal.SIGALRM, lambda *_: os._exit(0))
        signal.alarm(max(1, int(seconds)))


def main() -> int:
    try:
        _arm_lifetime(_BOOTSTRAP_LIFETIME_S)
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
        if not isinstance(payload, dict):
            return 0
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _session_integrity as si  # noqa: PLC0415 — after sys.path setup

        _arm_lifetime(si.sender_lifetime_s(payload))
        si.deliver_harness_event(payload)
    except Exception:  # noqa: BLE001 — fail open; nothing may surface from here
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException:  # noqa: BLE001
        sys.exit(0)
