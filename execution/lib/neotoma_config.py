"""
execution/lib/neotoma_config.py — shared NEOTOMA_BASE_URL resolution.

ateles#243: every daemon previously fell back to a hardcoded
http://localhost:3180 when NEOTOMA_BASE_URL was unset, which silently ran
guards and queries against a wrong (and now stale — canonical prod is :9180)
default instead of failing loudly. This helper is the single place that reads
NEOTOMA_BASE_URL; it never substitutes a default of any kind. The daemon's
plist/launchd config is the single source of truth for which host/port is
correct in which environment.
"""

from __future__ import annotations

import os


class NeotomaConfigError(RuntimeError):
    """Raised at startup when NEOTOMA_BASE_URL is unset or blank."""


def resolve_neotoma_base_url() -> str:
    """
    Read NEOTOMA_BASE_URL from the environment.

    Raises NeotomaConfigError with an actionable message if unset/blank.
    Never returns a default — the caller's daemon must fail loud at startup
    rather than silently run against a wrong host.
    """
    value = os.environ.get("NEOTOMA_BASE_URL", "").strip()
    if not value:
        raise NeotomaConfigError(
            "NEOTOMA_BASE_URL is not set. Configure it in the daemon's "
            "launchd plist (or ~/.config/neotoma/.env) — there is no "
            "default; running against an unconfigured Neotoma silently "
            "disables safety guards that depend on it (see ateles#243)."
        )
    return value
