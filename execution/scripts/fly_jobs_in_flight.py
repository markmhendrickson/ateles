#!/usr/bin/env python3
"""Is a Fly job genuinely in flight? Used to defer the daemon redeploy.

A Fly job started by Apis is awaited by that Apis process, so restarting Apis
(``redeploy_daemons_from_main.sh`` runs ``launchctl kickstart -k``) orphans the
job: nothing is left to read its result and the task stays ``executing``.

Exit 0 and print the count when at least one ``in_flight`` reservation in the
Fly runner ledger has a ``deadline`` still in the future; exit 1 otherwise.

Prints only the count, never ledger contents: entries hold internal tokens.

Fails OPEN. A missing or unreadable ledger, or an entry with no readable
deadline, does not defer: this check protects a running job, and a broken
check must never stop fixes from deploying. An entry past its deadline does
not defer either, so a stuck entry cannot hold deploys forever.

Standard library only, so the redeploy script can run it with any python3;
the ledger path is resolved the same way as ``FlyConfig.from_env`` in
``execution/daemons/apis/fly_runner.py`` (a test holds the two equal).
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

LEDGER_ENV = "APIS_FLY_LEDGER_FILE"


def ledger_path(env: dict | None = None) -> Path:
    """The Fly runner ledger path, resolved exactly as ``FlyConfig.from_env``."""
    env = os.environ if env is None else env
    override = env.get(LEDGER_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "ateles" / "fly-runner-ledger.json"


def count_in_flight(path: Path, now: float) -> int:
    """Reservations whose deadline is still ahead; 0 on anything unreadable."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = data.get("in_flight")
    except Exception:  # noqa: BLE001 - fail open: a broken check never blocks deploys
        return 0
    if not isinstance(entries, dict):
        return 0
    count = 0
    for entry in entries.values():
        if not isinstance(entry, dict):
            continue
        deadline = entry.get("deadline")
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)):
            continue
        if math.isfinite(deadline) and deadline > now:
            count += 1
    return count


def main() -> int:
    count = count_in_flight(ledger_path(), time.time())
    if count > 0:
        print(count)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
