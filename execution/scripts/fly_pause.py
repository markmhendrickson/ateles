#!/usr/bin/env python3
"""Pause, resume or show the host-wide Fly switch.

While paused, no code on this host that goes through the Fly runner makes a
Fly Machines API call: Apis's periodic settlement and reaper skip, running
jobs are left alone, and new launches (Apis dispatch, ``dispatch_role.py
--runner fly``, ``fly_lens_review.py``) refuse with "Fly paused until <time>:
<reason>" and do not run locally instead. Use it to hold a production deploy
window without unloading Apis.

    python3 execution/scripts/fly_pause.py set --reason "neotoma deploy" [--minutes 60]
    python3 execution/scripts/fly_pause.py show
    python3 execution/scripts/fly_pause.py clear

The default duration is 60 minutes; over 12 hours is refused. The record lives
beside the Fly runner ledger (``APIS_FLY_LEDGER_FILE``, default
``~/.config/ateles/fly-runner-ledger.json``); run with the same value as Apis.
Exit codes: 0 done (``show``: not paused); 1 ``show``: paused; 2 refused.
Standard library only. See docs/cloud_hosting.md.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

APIS_DIR = Path(__file__).resolve().parent.parent / "daemons" / "apis"
if str(APIS_DIR) not in sys.path:
    sys.path.insert(0, str(APIS_DIR))

import fly_pause  # noqa: E402 - daemon module, stdlib only


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)
    set_p = sub.add_parser("set", help="pause Fly calls from this host")
    set_p.add_argument(
        "--reason", required=True, help="short reason, shown to every caller"
    )
    set_p.add_argument(
        "--minutes",
        type=float,
        default=fly_pause.DEFAULT_PAUSE_SECONDS / 60,
        help="duration in minutes (default 60, at most 720)",
    )
    sub.add_parser("clear", help="end the pause now")
    sub.add_parser("show", help="print the pause in force, if any")
    args = p.parse_args(argv)

    path = fly_pause.pause_path(fly_pause.ledger_path())
    if args.command == "set":
        try:
            state = fly_pause.set_pause(
                path, seconds=args.minutes * 60, reason=args.reason
            )
        except (ValueError, OSError) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2
        print(f"{state.message()} (record {path})")
        return 0
    if args.command == "clear":
        removed = fly_pause.clear_pause(path)
        print("Fly pause cleared" if removed else f"no Fly pause was set ({path})")
        return 0
    state = fly_pause.read_pause(path)
    if state.paused:
        print(state.message())
        return 1
    print(f"Fly is not paused ({path})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
