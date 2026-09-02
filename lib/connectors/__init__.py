"""
lib/connectors — observing external systems into Neotoma, with staleness.

An external system (Fly, GitHub, a client's API) is the source of truth. What
Neotoma holds is a *timestamped observation* of it, which can go stale. Every
piece of this package exists to keep that distinction visible, because the
alternative is the failure mode the package was built to end: a record that
confidently states the wrong thing.

Read ``docs/connectors.md`` for the design and the incidents behind it.

The public surface:

    Connector          the protocol a source implements (``observe()``)
    ConnectorResult    one run's outcome — returned, never raised
    Freshness          fresh | stale | unknown, with the age that decided it
    assess_freshness   the pure staleness verdict
    ConnectorStatus    the durable per-connector record the app reads
"""

from __future__ import annotations

from .base import (
    DEFAULT_MAX_WRITES,
    INGESTION_MODES,
    MIN_STALE_AFTER_SECONDS,
    STALE_INTERVAL_MULTIPLIER,
    Connector,
    ConnectorResult,
    ConnectorStatus,
    Freshness,
    WriteBudgetExceeded,
    assess_freshness,
    stale_after_for,
)

__all__ = [
    "Connector",
    "ConnectorResult",
    "ConnectorStatus",
    "Freshness",
    "WriteBudgetExceeded",
    "assess_freshness",
    "stale_after_for",
    "DEFAULT_MAX_WRITES",
    "INGESTION_MODES",
    "MIN_STALE_AFTER_SECONDS",
    "STALE_INTERVAL_MULTIPLIER",
]
