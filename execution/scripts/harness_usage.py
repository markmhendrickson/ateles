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

    # Refresh Claude's reading now from the CLI's own rate-limit report (the
    # dispatcher does this automatically when the reading is older than
    # APIS_USAGE_REFRESH_SECONDS; run it from a timer to keep it warm):
    harness_usage.py refresh

    # What selection will use now, including any session-window cooling
    # (a provider that refused with "You've hit your session limit ... resets
    # 12:30pm" is held out until that reset, whatever its weekly headroom):
    harness_usage.py show

    # Dispatch attempts per model tier (from the tier ledger; a provider
    # failover counts once per provider tried):
    harness_usage.py tiers --since-hours 24

    # ...and why dispatches ran above their policy tier (escalation signals):
    harness_usage.py tiers --since-hours 24 --reasons

``show`` also reports, per gated provider, the reading's age, the weekly
ceiling, the pace line (ceiling x elapsed fraction of the week + burst), and
whether frontier dispatch is allowed right now.  A reading older than
APIS_USAGE_STALE_SECONDS (default 1800) or malformed refuses new frontier
dispatch until it is refreshed; APIS_USAGE_WEEKLY_CEILING_PERCENT (60) and
APIS_USAGE_PACE_BURST_PERCENT (10) set the pace; APIS_USAGE_GATE=off disables.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path

_APIS_DIR = Path(__file__).resolve().parents[1] / "daemons" / "apis"
if str(_APIS_DIR) not in sys.path:
    sys.path.insert(0, str(_APIS_DIR))

import harness_router  # noqa: E402
import model_tiering  # noqa: E402
import usage_probe  # noqa: E402


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
        "until_local": harness_router.render_wall(float(cooling["until"])),
        "reason": cooling["reason"],
        "remaining_seconds": max(0, int(float(cooling["until"]) - time.time())),
    }


def _gate_view(provider: str, headroom: float) -> dict[str, object]:
    """Snapshot age, ceiling, pace line and the dispatch verdict for one provider."""
    gate = harness_router.usage_gate(provider)
    if gate is None:
        # No gate for this provider: eligibility is headroom and cooling only.
        allowed = (
            headroom > harness_router.minimum_headroom()
            and harness_router.persisted_cooling(provider) is None
        )
        return {
            "gated": False,
            "dispatch_allowed": allowed,
            "summary": (
                f"{provider}: not usage-gated (gate off, or no automatic live "
                f"source); frontier dispatch {'ALLOWED' if allowed else 'REFUSED'} "
                f"on headroom {headroom:.2f} and cooling alone"
            ),
        }
    view: dict[str, object] = {
        "gated": True,
        "dispatch_allowed": gate.allowed,
        "code": gate.code,
        "reason": gate.message,
        "snapshot_age_seconds": None if gate.age_seconds is None else int(gate.age_seconds),
        "stale_after_seconds": None if gate.max_age_seconds is None else int(gate.max_age_seconds),
        "weekly_used_percent": gate.weekly_used_percent,
        "weekly_ceiling_percent": gate.ceiling_percent,
        "pace_line_percent": None if gate.pace_percent is None else round(gate.pace_percent, 2),
        "week_elapsed_fraction": (
            None if gate.elapsed_fraction is None else round(gate.elapsed_fraction, 4)
        ),
        "burst_percent": gate.burst_percent,
        "last_refresh_failure": gate.probe_failure,
        "retry_or_capacity_returns_at": (
            None if gate.returns_at is None else harness_router.render_wall(gate.returns_at)
        ),
    }
    verdict = "ALLOWED" if gate.allowed else "REFUSED"
    view["summary"] = f"{provider}: frontier dispatch {verdict} - {gate.message}"
    return view


def _refresh_claude() -> int:
    """Refresh Claude's reading from the CLI's own report and say what happened."""
    binary = shutil.which("claude")
    if not binary:
        print("refresh: no claude binary on PATH; nothing recorded", file=sys.stderr)
        return 1
    # A metered key would report the API account's limits, not the plan's.
    env = {k: v for k, v in os.environ.items()
           if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    outcome = usage_probe.refresh_usage_if_stale({"claude": binary}, env=env, force=True)
    print(json.dumps(outcome, indent=2))
    print(
        "note: this ran under YOUR login and environment; the daemon refreshes under "
        "its own, so it can still fail there. Check `show` (last_refresh_failure) "
        "after its next dispatch.",
        file=sys.stderr,
    )
    return 0 if outcome.get("claude") == "refreshed" else 1


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
    sub.add_parser(
        "refresh",
        help="refresh Claude's usage reading from the CLI's own rate-limit report",
    )

    tiers = sub.add_parser(
        "tiers",
        help=(
            "print dispatch attempts per model tier from the tier ledger "
            "(a provider failover counts once per provider tried)"
        ),
    )
    tiers.add_argument(
        "--since-hours", type=float, default=None,
        help="only count dispatches from the last N hours (default: all)",
    )
    tiers.add_argument(
        "--reasons", action="store_true",
        help=(
            "also report why dispatches were raised a tier: how many "
            "escalated dispatches each signal appears on, overall and per "
            "action class"
        ),
    )

    args = parser.parse_args(argv)
    if args.command == "tiers":
        print(json.dumps(
            model_tiering.tier_counts(
                since_hours=args.since_hours, with_reasons=args.reasons
            ),
            indent=2,
            sort_keys=True,
        ))
        return 0
    if args.command == "refresh":
        return _refresh_claude()
    if args.command == "usage":
        try:
            harness_router.record_usage(args.provider, args.window)
        except ValueError as exc:
            parser.error(str(exc))
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
                    "usage_gate": _gate_view(provider, values[provider]),
                }
                for provider in harness_router.configured_providers()
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
