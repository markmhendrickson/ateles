#!/usr/bin/env python3
"""Detached harness_event sender — started by `_session_integrity._launch_sender`.

NOT a hook and not wired in `.claude/settings.json`. A Stop hook starts this
as a child in its own session (`start_new_session=True`) with no inherited
stdio, writes one JSON payload to its stdin, and exits without waiting, so a
Neotoma instance that is slow to accept writes can no longer delay a Stop.

The payload — base URL, bearer, request body and diagnostics path — arrives
ONLY on stdin. Nothing secret is ever on this process's argv or in its
environment. The POST itself (60 s timeout, one retry with the same
idempotency key, redirects refused by `_NoRedirectHandler`) is
`_session_integrity.deliver_harness_event`; this file only reads stdin and
calls it, so the transport has exactly one implementation.

Run with `python3 -I`, which drops the script directory from `sys.path`, so
the hooks directory is added back explicitly. Stdlib only. Always exits 0.
"""
import json
import sys
from pathlib import Path


def main() -> int:
    try:
        raw = sys.stdin.buffer.read()
        payload = json.loads(raw.decode("utf-8", errors="replace")) if raw else {}
        if not isinstance(payload, dict):
            return 0
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import _session_integrity as si  # noqa: PLC0415 — after sys.path setup

        si.deliver_harness_event(payload)
    except Exception:  # noqa: BLE001 — fail open; nothing may surface from here
        pass
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BaseException:  # noqa: BLE001
        sys.exit(0)
