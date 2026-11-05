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

When the limit is reached the holder does not simply let go: a deploy that
outlived the lock could restart Apis over a job admitted after the release. It
first ends its parent (SIGTERM, then SIGKILL after ``PARENT_TERM_WAIT_SECONDS``),
waits ``KICKSTART_SETTLE_SECONDS`` for a ``launchctl kickstart`` the parent had
already started, and only then exits. It signals only the pid it was given, and
only while that process is still its parent.

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

# Test-only override of the limit. It can only SHORTEN the limit: a value that
# is not a number, not finite, not above zero, or not below the production
# limit is ignored, so it can never disable or lengthen the bound.
HOLD_LIMIT_ENV = "APIS_FLY_HOLD_LIMIT_SECONDS"

# At the limit: how long the parent gets to exit after SIGTERM before SIGKILL,
# and how long to wait afterwards so a kickstart it had begun can finish.
PARENT_TERM_WAIT_SECONDS = 30.0
KICKSTART_SETTLE_SECONDS = 5.0

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


def hold_limit(env: dict | None = None) -> float:
    """The lifetime limit: production value unless the test override shortens it."""
    env = os.environ if env is None else env
    try:
        value = float(env.get(HOLD_LIMIT_ENV, ""))
    except ValueError:
        return float(HOLD_LIMIT_SECONDS)
    if math.isfinite(value) and 0 < value < HOLD_LIMIT_SECONDS:
        return value
    return float(HOLD_LIMIT_SECONDS)


def _is_my_parent(parent: int) -> bool:
    return os.getppid() == parent and _parent_alive(parent)


def _end_parent(parent: int) -> None:
    """End the deploy script before the lock is released (limit reached)."""
    if not _is_my_parent(parent):
        return  # already gone, or the pid is no longer our parent: signal nothing
    try:
        os.kill(parent, signal.SIGTERM)
    except OSError:
        return
    deadline = time.monotonic() + PARENT_TERM_WAIT_SECONDS
    while _parent_alive(parent) and time.monotonic() < deadline:
        time.sleep(0.1)
    if _parent_alive(parent):
        try:
            os.kill(parent, signal.SIGKILL)
        except OSError:
            pass
        end = time.monotonic() + PARENT_TERM_WAIT_SECONDS
        while _parent_alive(parent) and time.monotonic() < end:
            time.sleep(0.1)
    time.sleep(KICKSTART_SETTLE_SECONDS)


def hold(parent: int, wait: float = LOCK_WAIT_SECONDS,
         limit: float | None = None) -> int:
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
    end = time.monotonic() + (hold_limit() if limit is None else limit)
    while time.monotonic() < end and _parent_alive(parent):
        time.sleep(0.2)
    if _parent_alive(parent):
        _end_parent(parent)  # limit reached with the deploy still running
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
