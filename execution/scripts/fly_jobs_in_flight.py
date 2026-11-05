#!/usr/bin/env python3
"""Is a Fly job genuinely in flight? Used to defer the daemon redeploy.

A Fly job started by Apis is awaited by that Apis process, so restarting Apis
(``redeploy_daemons_from_main.sh`` runs ``launchctl kickstart -k``) orphans the
job: nothing is left to read its result and the task stays ``executing``.

Exit 0 and print the count when at least one ``in_flight`` reservation in the
Fly runner ledger is still live (its ``deadline`` plus ``SETTLE_GRACE_SECONDS``
is ahead); exit 1 otherwise.

``--hold`` is the form the redeploy script uses. It takes the Fly runner's own
admission lock (``<ledger>.admission.lock``, the exclusive ``flock`` that
``FlyExecutor._admission`` takes before any reservation), counts while holding
it, prints ONE line (``ok <count>`` or ``busy``), and keeps the lock until it
is sent SIGTERM, its parent dies, or ``HOLD_LIMIT_SECONDS`` passes. While it
holds the lock no launch can reserve, so the count stays true from the check
to the restart. The lock dies with the holder process.

Prints only the count, never ledger contents: entries hold internal tokens.

Fails OPEN. A missing or unreadable ledger, or an entry with no readable
deadline, does not defer: this check protects a running job, and a broken
check must never stop fixes from deploying. An entry past its deadline plus
the grace does not defer either, so a stuck entry cannot hold deploys forever.

Standard library only, so the redeploy script can run it with any python3;
the ledger path is resolved the same way as ``FlyConfig.from_env`` in
``execution/daemons/apis/fly_runner.py`` (a test holds the two equal).
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import signal
import sys
import time
from pathlib import Path

LEDGER_ENV = "APIS_FLY_LEDGER_FILE"

# A run whose deadline has passed may still be collecting its last answer and
# settling its result (confirming termination, charging runtime, releasing the
# reservation). Restarting Apis then would orphan that settlement, so a
# reservation keeps counting this long past its deadline. Still bounded, so a
# stuck entry cannot hold deploys forever.
SETTLE_GRACE_SECONDS = 300

# How long --hold waits for the admission lock before reporting ``busy``. The
# lock is held only briefly by admission or the reaper; failing to get it in
# this time defers the deploy to the next run rather than waiting forever.
LOCK_WAIT_SECONDS = 60.0

# The longest --hold keeps the lock, whatever happens to its parent: admission
# never stays blocked longer than this even if release is never sent.
HOLD_LIMIT_SECONDS = 15 * 60

_POLL_SECONDS = 0.05


def ledger_path(env: dict | None = None) -> Path:
    """The Fly runner ledger path, resolved exactly as ``FlyConfig.from_env``."""
    env = os.environ if env is None else env
    override = env.get(LEDGER_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "ateles" / "fly-runner-ledger.json"


def admission_lock_path(ledger: Path) -> Path:
    """The admission lock file, named exactly as ``FlyExecutor._admission``."""
    ledger = Path(ledger)
    return ledger.with_name(ledger.name + ".admission.lock")


def _live(entry: object, now: float) -> bool:
    """One reservation still counts. Any unusable deadline is skipped, never
    raised: one corrupt entry must not stop a live one from counting."""
    if not isinstance(entry, dict):
        return False
    deadline = entry.get("deadline")
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
        return False
    try:
        # float() of a huge JSON integer (e.g. 10**400) raises OverflowError.
        value = float(deadline)
        return math.isfinite(value) and value + SETTLE_GRACE_SECONDS > now
    except (OverflowError, ValueError):
        return False


def count_in_flight(path: Path, now: float) -> int:
    """Reservations still live (deadline + grace ahead), ``unconfirmed`` ones
    included on the same rule; 0 on anything unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = data.get("in_flight")
    except Exception:  # noqa: BLE001 - fail open: a broken check never blocks deploys
        return 0
    if not isinstance(entries, dict):
        return 0
    return sum(1 for entry in entries.values() if _live(entry, now))


def _parent_alive(parent: int) -> bool:
    try:
        os.kill(parent, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def hold(parent: int, wait: float = LOCK_WAIT_SECONDS,
         limit: float = HOLD_LIMIT_SECONDS) -> int:
    """Take the admission lock, report ``ok <count>`` or ``busy``, then keep
    the lock until SIGTERM, parent death or ``limit``."""
    def _stop(_sig, _frame):
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    ledger = ledger_path()
    lock = admission_lock_path(ledger)
    lock.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock, "a+")
    give_up = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= give_up or not _parent_alive(parent):
                print("busy", flush=True)
                return 0
            time.sleep(_POLL_SECONDS)
    count = count_in_flight(ledger, time.time())
    print(f"ok {count}", flush=True)
    sys.stdout.close()
    end = time.monotonic() + limit
    while time.monotonic() < end and _parent_alive(parent):
        time.sleep(0.2)
    return 0  # the lock is released when the process exits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hold", type=int, metavar="PARENT_PID",
                        help="take and keep the admission lock for PARENT_PID")
    parser.add_argument("--wait", type=float, default=LOCK_WAIT_SECONDS)
    args = parser.parse_args(argv)
    if args.hold is not None:
        return hold(args.hold, wait=args.wait)
    count = count_in_flight(ledger_path(), time.time())
    if count > 0:
        print(count)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
