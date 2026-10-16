"""
lib/daemon_runtime/neotoma_timeout.py — one Neotoma HTTP timeout for the whole
daemon runtime.

Every Neotoma client in this package used to carry its own literal (10s, 15s,
20s). Those values were chosen when the datastore answered in well under a
second, and they were copied from file to file. When the instance degraded they
all became wrong at once, and the one that mattered most — ``agent_loader`` at
10s — turned every dispatch into a stub load (ateles#669).

Measured against production on 2026-09-01, the exact ``POST /entities/query``
body ``agent_loader`` sends returned in 32.3s, 19.0s, and 11.2s. A 10s budget
expires on all three; even the 20s siblings expire on the slowest.

So the default is 45s: comfortably above the observed p100 of ~32s with headroom
for a slower moment, and still bounded well below any daemon's own task timeout
(``skill_runner`` dispatches at 1800s), so a hung read cannot wedge a daemon.

This is a CEILING, not a target. A healthy instance answers in under a second
and the timeout is never reached; raising it costs nothing when the server is
fast and prevents a self-inflicted outage when it is slow.

Override with ``ATELES_NEOTOMA_TIMEOUT`` (seconds). An explicitly configured
invalid value raises at startup rather than silently changing the read budget.
"""

from __future__ import annotations

import math
import os

# Seconds. See the module docstring for how this number was chosen.
DEFAULT_NEOTOMA_TIMEOUT = 45.0

_ENV_VAR = "ATELES_NEOTOMA_TIMEOUT"


def neotoma_timeout(default: float = DEFAULT_NEOTOMA_TIMEOUT) -> float:
    """Return the Neotoma HTTP timeout in seconds.

    Reads ``ATELES_NEOTOMA_TIMEOUT`` at call time (not import time) so a daemon
    that loads its environment after import — and the tests — see the current
    value rather than whatever was set when the module first loaded.
    """
    configured = os.environ.get(_ENV_VAR)
    if configured is None:
        return default
    raw = configured.strip()
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise ValueError(f"{_ENV_VAR} must be a finite positive number") from None
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{_ENV_VAR} must be a finite positive number")
    return value
