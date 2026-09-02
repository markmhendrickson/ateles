"""
lib/connectors/base.py — the connector contract and the staleness verdict.

## Why staleness is the whole design

For PRs and issues, Neotoma is canonical and GitHub is overlaid. Deployment
state is the opposite: the Fly API *is* the truth, and a Neotoma record of "the
running version" is a cache with no invalidation.

A stale record claiming ``0.22.1`` while the machine serves ``0.17.0`` is worse
than no record at all. It is the same failure as a health check returning 200
while nothing works — which is the defect this package exists to prevent. So:

  1. every observation carries ``observed_at``;
  2. consumers render AGE, never a bare value;
  3. a stale observation is visibly stale, never silently wrong.

## Three states, not two

``unknown`` is deliberately separate from ``stale``. ``checkout_drift`` already
draws this line — a failed ``git fetch`` reports UNKNOWN rather than drift,
because offline must not look identical to unpushed commits. Same reasoning
here: "we could not tell" and "we can tell, and it is bad" are different facts,
and collapsing them yields either false alarms or ignored ones.

Nothing in this module performs I/O. The verdict is a pure function of two
timestamps and an interval, so it is trivially testable and cannot itself be
the thing that breaks.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

# ── Staleness policy ────────────────────────────────────────────────────────
#
# stale_after = max(3 * verify_interval, 15 minutes)
#
# THREE INTERVALS, because one missed run is routine — a laptop sleeps, a fetch
# times out — and three consecutive misses is a broken connector. Alarming on a
# single miss is how the checkout-drift log became something nobody reads.
#
# A FIFTEEN-MINUTE FLOOR, so a fast connector does not declare itself stale
# during a brief network blip.
#
# ## Why this keys off the VERIFY interval, not a fetch interval
#
# A push-fed connector (webhook, SSE) still declares this, and it still means
# "how often we confirm the record matches reality" — not "how often we fetch".
#
# A webhook tells you what CHANGED; a poll tells you what IS. If a delivery is
# dropped — endpoint down, subscription expired, secret rotated — nothing ever
# tells you. The record diverges permanently while every timestamp still looks
# fresh, because nothing was expected and so nothing aged.
#
# That is not hypothetical. It is the SSE failure exactly: the subscription ID
# was absent for 88 days, ``sse_client`` logged a WARNING and returned, and the
# swarm processed 67,450 silent skips with no task event after 2026-06-04. The
# stream even receives server heartbeats — and discards them (`yield None`), so
# nothing records that events are still arriving and nothing can notice when
# they stop. The transport carries the liveness signal; the application throws
# it away.
#
# So a push source is never exempt from this field. Push supplies LATENCY;
# the periodic verify supplies LIVENESS, and only liveness can age. A connector
# that cannot say how it would notice its own silence has no business claiming
# freshness.

STALE_INTERVAL_MULTIPLIER = 3
MIN_STALE_AFTER_SECONDS = 900  # 15 minutes

#: Per-run write ceiling. The 2026 GitHub sync runaway wrote 520+ duplicate
#: issues before anyone noticed; a budget would have stopped it at 200 with a
#: loud error. Cheap insurance against a failure that has already happened.
DEFAULT_MAX_WRITES = int(os.environ.get("ATELES_CONNECTOR_MAX_WRITES", "200"))


def stale_after_for(poll_interval_seconds: int) -> int:
    """The age past which this connector's observations stop being trustworthy.

    The argument is the VERIFY interval — how often the connector confirms its
    record against the source — which for a push-fed connector is its poll
    backstop, not its event rate.
    """
    return max(
        STALE_INTERVAL_MULTIPLIER * int(poll_interval_seconds),
        MIN_STALE_AFTER_SECONDS,
    )


#: How a connector learns that something changed.
#:
#:   poll   — periodic fetch only. Cannot silently diverge: the observation
#:            either arrives or it ages into staleness.
#:   hybrid — push for latency, poll for liveness. The push path updates
#:            between verifies; the poll path is what can actually age.
#:
#: There is deliberately no ``push`` mode. A push-only connector has no way to
#: notice its own silence, which is the SSE failure this package is built
#: against — and a mode nobody can use safely is a mode worth not offering.
INGESTION_MODES = ("poll", "hybrid")


class WriteBudgetExceeded(RuntimeError):
    """A connector tried to write more records in one run than its budget allows.

    Raised by the runner, not by ``observe()``. Signals a probable loop — the
    2026 runaway's signature — and aborts the run rather than letting it
    continue writing.
    """

    def __init__(self, connector: str, attempted: int, budget: int) -> None:
        super().__init__(
            f"connector {connector!r} attempted {attempted} writes, "
            f"budget is {budget} — aborting as a probable runaway"
        )
        self.connector = connector
        self.attempted = attempted
        self.budget = budget


# ── The freshness verdict ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Freshness:
    """How much an observation can be trusted, and why.

    ``state`` is one of:

      fresh    — observed within ``stale_after``; safe to use and to alarm on
      stale    — older than that; show the age, and SUPPRESS alarms
      unknown  — never observed, or never successfully; infer nothing
    """

    state: str  # "fresh" | "stale" | "unknown"
    age_seconds: float | None = None
    stale_after_seconds: int | None = None
    detail: str = ""

    @property
    def is_fresh(self) -> bool:
        return self.state == "fresh"

    @property
    def alarms_allowed(self) -> bool:
        """Whether a watchdog may raise an alarm from this observation.

        Only ``fresh`` qualifies. An alarm derived from a stale reading — "5
        releases behind", computed from a day-old value — asserts a present it
        cannot see, which is the exact false-authority failure this package
        exists to prevent. When suppressed, alarm on the CONNECTOR's failure
        instead: a connector that stopped working is the more actionable fact.
        """
        return self.state == "fresh"

    def summary(self) -> str:
        if self.state == "unknown":
            return f"never observed ({self.detail})" if self.detail else "never observed"
        age = _humanize(self.age_seconds or 0.0)
        if self.state == "fresh":
            return f"observed {age} ago"
        return (
            f"STALE — observed {age} ago, "
            f"expected within {_humanize(float(self.stale_after_seconds or 0))}"
        )


def _humanize(seconds: float) -> str:
    """Compact age. Rendering age rather than a bare value is the whole point."""
    s = int(max(seconds, 0))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        h, m = divmod(s // 60, 60)
        return f"{h}h{m:02d}m" if m else f"{h}h"
    d, h = divmod(s // 3600, 24)
    return f"{d}d{h}h" if h else f"{d}d"


def _parse_ts(value: object) -> datetime | None:
    """Parse an ISO-8601 timestamp, tolerating ``Z`` and naive strings.

    Returns None for anything unparseable — an unreadable timestamp is
    ``unknown``, never silently treated as "now".
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def assess_freshness(
    observed_at: object,
    *,
    stale_after_seconds: int,
    now: datetime | None = None,
) -> Freshness:
    """Judge one observation's age. Pure; never raises.

    ``observed_at`` may be a datetime, an ISO-8601 string, or None/garbage —
    the last of which yields ``unknown`` rather than a guess.
    """
    ts = _parse_ts(observed_at)
    if ts is None:
        return Freshness(
            state="unknown",
            stale_after_seconds=stale_after_seconds,
            detail="no usable observed_at",
        )

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age = (current - ts).total_seconds()

    # A timestamp from the future means clock skew somewhere. Treat it as fresh
    # (age floors at 0) rather than inventing a verdict; the collector's own
    # status is where a broken clock should surface.
    if age < 0:
        age = 0.0

    return Freshness(
        state="fresh" if age <= stale_after_seconds else "stale",
        age_seconds=age,
        stale_after_seconds=stale_after_seconds,
    )


# ── What a connector returns ────────────────────────────────────────────────


@dataclass(frozen=True)
class ConnectorResult:
    """One connector run's outcome.

    Always RETURNED, never raised. The runner drives every connector in one
    loop, and one source's outage must not stop the others.
    """

    ok: bool
    records_written: int = 0
    error: str = ""  # one line, no secrets — this is rendered in the app
    detail: dict = field(default_factory=dict)

    @classmethod
    def failure(cls, error: str, **detail: object) -> "ConnectorResult":
        # One line, truncated: the app renders this, and a multi-KB traceback
        # in a status card is unreadable. Full detail belongs in the log.
        line = " ".join(str(error).split())[:300]
        return cls(ok=False, error=line, detail=dict(detail))

    @classmethod
    def success(cls, records_written: int = 0, **detail: object) -> "ConnectorResult":
        return cls(ok=True, records_written=records_written, detail=dict(detail))


# ── The durable status record ───────────────────────────────────────────────


@dataclass(frozen=True)
class ConnectorStatus:
    """The per-connector record the app reads, one per connector.

    ``last_attempt_at`` and ``last_success_at`` are SEPARATE FIELDS, and that
    separation is the point: a connector attempting and failing every minute is
    indistinguishable from a healthy one if only attempts are recorded. Silent
    failure of exactly that kind is what this package exists to end.
    """

    connector_name: str
    status: str = "never_run"  # "ok" | "failing" | "never_run"
    last_attempt_at: str | None = None
    last_success_at: str | None = None
    last_error: str = ""
    records_written: int = 0
    poll_interval_seconds: int = 0
    stale_after_seconds: int = 0
    consecutive_failures: int = 0
    ingestion_mode: str = "poll"
    #: When a push delivery (webhook/SSE) last updated this connector's records.
    #:
    #: Recorded but NEVER used to compute freshness — see ``freshness()``. It
    #: exists so a hybrid connector's push path is observable: "events arriving,
    #: verify overdue" and "no events for a week" are different situations, and
    #: a webhook that silently stopped is invisible without this field.
    last_push_at: str | None = None
    #: Set when this status was read back from Neotoma; None for a fresh
    #: in-memory record. Its presence is what tells the store to correct an
    #: existing entity rather than create a second one.
    entity_id: str | None = None

    def freshness(self, now: datetime | None = None) -> Freshness:
        """How stale this connector's DATA is — judged on last successful VERIFY.

        Deliberately not ``last_attempt_at``: a connector failing every minute
        has a recent attempt and worthless data. Attempts do not refresh facts.

        Deliberately not ``last_push_at`` either, and this is the load-bearing
        choice in the hybrid model. Letting a webhook delivery reset the clock
        would mean a source that goes quiet — because it genuinely had nothing
        to say, OR because deliveries silently stopped — is indistinguishable
        from a healthy one. Only a verify proves the record still matches
        reality, so only a verify may refresh it.
        """
        if not self.last_success_at:
            return Freshness(
                state="unknown",
                stale_after_seconds=self.stale_after_seconds,
                detail="no successful run yet",
            )
        return assess_freshness(
            self.last_success_at,
            stale_after_seconds=self.stale_after_seconds,
            now=now,
        )

    def to_entity_fields(self) -> dict:
        """The Neotoma ``connector_status`` snapshot for this connector."""
        return {
            "connector_name": self.connector_name,
            "status": self.status,
            "last_attempt_at": self.last_attempt_at,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "records_written": self.records_written,
            "poll_interval_seconds": self.poll_interval_seconds,
            "stale_after_seconds": self.stale_after_seconds,
            "consecutive_failures": self.consecutive_failures,
            "ingestion_mode": self.ingestion_mode,
            "last_push_at": self.last_push_at,
        }


# ── The contract ────────────────────────────────────────────────────────────


@runtime_checkable
class Connector(Protocol):
    """One external system, observed into Neotoma.

    Implementations own only the source-specific part: read the system, write
    observations, report what happened. Scheduling, status, staleness, and the
    UI are shared and must not be reimplemented per source.

    The test of this abstraction is whether a third source — the Theodore
    project has wanted connectors and none were built — can be added by writing
    only ``observe()``. If a new source needs runner changes, the abstraction is
    two pipelines sharing a name and should be fixed rather than extended.
    """

    #: Stable identifier: "fly", "github", "theodore". Used in idempotency keys
    #: and as the ``connector_status`` canonical key, so it must not change.
    name: str

    #: How often this connector VERIFIES its record against the source.
    #:
    #: REQUIRED even for push-fed connectors, where it is the poll backstop
    #: rather than the primary path. A push source that never verifies cannot
    #: notice a dropped delivery, so its freshness is unfalsifiable — see the
    #: staleness note above and the 88-day SSE silence.
    #:
    #: Under ``hybrid`` this can be generous — hourly rather than every fifteen
    #: minutes — because its job is catching drift, not being current. Push
    #: already supplies currency. That makes the combination much cheaper than
    #: polling hard, which is the main argument against polling at all.
    poll_interval_seconds: int

    #: "poll" or "hybrid". Defaults to "poll" when a connector omits it, which
    #: is the safe direction: a connector is assumed unable to receive push
    #: until it says otherwise.
    ingestion_mode: str

    def observe(self) -> ConnectorResult:
        """Read the external system and write observations to Neotoma.

        MUST NOT raise — return ``ConnectorResult.failure(...)`` instead.

        MUST be idempotent: every write carries a deterministic
        ``idempotency_key`` built from stable identity
        (``connector-{name}-{external_id}-{content_hash}``), never a clock or a
        counter, so re-running over unchanged records is a no-op at the server
        rather than a source of duplicates.

        MUST verify writes by read-back, not by a success code. A ``body``
        field on a ``task`` was accepted with ``success: true`` and silently
        dropped on this instance; ``success: true`` means "the request parsed",
        not "the data persisted".
        """
        ...
