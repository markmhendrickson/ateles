#!/usr/bin/env python3
"""Record live plan usage for the Apis harness router.

The router derives each provider's headroom from the usage snapshot this
script writes (``~/.config/ateles/harness-usage.json``, or
``APIS_HARNESS_USAGE_FILE``), so a provider held out at exhaustion comes back
at its reported reset without a hand edit of the headroom file.

    # Claude Code /usage reads weekly 3%, 5-hour 8%:
    harness_usage.py usage claude \\
        --window weekly=3@2026-10-05T20:00:00+02:00 \\
        --window five_hour=8@2026-09-29T02:00:00+02:00

    # Codex CLI said "usage limit ... try again at 2026-10-03 20:12":
    harness_usage.py exhausted codex --until 2026-10-03T20:12:00+02:00

    # What selection will use now, including any session-window cooling
    # (a provider that refused with "You've hit your session limit ... resets
    # 12:30pm" is held out until that reset, whatever its weekly headroom):
    harness_usage.py show
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_APIS_DIR = Path(__file__).resolve().parents[1] / "daemons" / "apis"
if str(_APIS_DIR) not in sys.path:
    sys.path.insert(0, str(_APIS_DIR))

import harness_router  # noqa: E402


def _parse_window(raw: str) -> dict[str, object]:
    """Parse ``name=percent[@resets_at]``."""
    name, sep, rest = raw.partition("=")
    if not sep or not name.strip():
        raise argparse.ArgumentTypeError(
            f"expected name=percent[@resets_at], got {raw!r}"
        )
    percent, _, resets_at = rest.partition("@")
    try:
        used = float(percent)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"non-numeric percent in {raw!r}") from exc
    window: dict[str, object] = {"name": name.strip(), "used_percent": used}
    if resets_at.strip():
        if harness_router._wall_from_iso(resets_at.strip()) is None:
            raise argparse.ArgumentTypeError(f"unparseable resets_at in {raw!r}")
        window["resets_at"] = resets_at.strip()
    return window


def _cooling_view(provider: str) -> dict[str, object] | None:
    """The provider's live cooling window, or None when it is not cooled.

    Weekly headroom can read healthy while the 5-hour session window is spent;
    this is what tells pacing the provider cannot take work until the reset.
    """
    cooling = harness_router.persisted_cooling(provider)
    if cooling is None:
        return None
    return {
        "until": cooling["until_iso"],
        "reason": cooling["reason"],
        "remaining_seconds": max(0, int(float(cooling["until"]) - time.time())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    usage = sub.add_parser("usage", help="record a plan-usage observation")
    usage.add_argument("provider", choices=harness_router.PROVIDERS)
    usage.add_argument(
        "--window",
        action="append",
        type=_parse_window,
        required=True,
        help="name=percent[@resets_at]; repeat per usage window",
    )

    exhausted = sub.add_parser("exhausted", help="record exhaustion until a reset")
    exhausted.add_argument("provider", choices=harness_router.PROVIDERS)
    exhausted.add_argument("--until", required=True, help="ISO-8601 reset time")

    sub.add_parser("show", help="print the headroom selection would use now")

    args = parser.parse_args(argv)
    if args.command == "usage":
        harness_router.record_usage(args.provider, args.window)
    elif args.command == "exhausted":
        until = harness_router._wall_from_iso(args.until)
        if until is None:
            parser.error(f"unparseable --until: {args.until!r}")
        harness_router.record_exhausted(args.provider, until)
    values = harness_router.configured_headroom()
    print(
        json.dumps(
            {
                provider: {
                    "headroom": values[provider],
                    "live": harness_router.live_headroom(provider),
                    "cooling": _cooling_view(provider),
                    "windows": harness_router.usage_windows(provider),
                }
                for provider in harness_router.configured_providers()
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
