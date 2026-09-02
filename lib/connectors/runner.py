"""
lib/connectors/runner.py — drive registered connectors and record what happened.

## The failure this module is built against

This codebase's signature defect is not broken code; it is *correct code nobody
calls*. ``sync_issues`` exists as an MCP tool with no daemon and no scheduled
caller. The worker pool built 2026-07-29 was never selected.
``agent_auto_invocation.py`` is fully tested and wired into zero lines of
config. The digest queue has zero non-test callers.

So the runner's contract is narrow and its trigger is the deliverable. A
connector framework without a live trigger joins the list above, and the
acceptance criterion for this stage is a scheduled thing that runs — not the
code that would run if something called it.

## What the runner guarantees

  - One connector's failure never stops the others (each is isolated).
  - Every run records BOTH the attempt and, separately, the success.
  - A connector exceeding its write budget is aborted as a probable runaway.
  - Status writes use ``correct()`` with the payload shape the server actually
    accepts, and are verified by read-back rather than by a success code.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from .base import (
    DEFAULT_MAX_WRITES,
    Connector,
    ConnectorResult,
    ConnectorStatus,
    stale_after_for,
)
from .store import ConnectorStore

log = logging.getLogger("connectors.runner")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_connector(
    connector: Connector,
    store: ConnectorStore,
    *,
    max_writes: int = DEFAULT_MAX_WRITES,
) -> ConnectorResult:
    """Run one connector, record its status, and return the result.

    Never raises. A connector that throws despite the contract is caught here
    and recorded as a failure — the runner cannot trust implementations to be
    perfectly behaved, and an exception escaping into the daemon loop would
    take down every other connector with it.
    """
    name = getattr(connector, "name", connector.__class__.__name__)
    attempt_at = _now_iso()

    try:
        result = connector.observe()
        if not isinstance(result, ConnectorResult):  # defensive: contract violation
            result = ConnectorResult.failure(
                f"connector {name!r} returned {type(result).__name__}, "
                "expected ConnectorResult"
            )
    except Exception as exc:  # noqa: BLE001 — isolation is the point
        log.exception(f"[{name}] observe() raised; contract says it must not")
        result = ConnectorResult.failure(f"{type(exc).__name__}: {exc}")

    # A run that wrote more than its budget is reported as a failure even when
    # the connector called itself successful: the 2026 runaway reported success
    # while looping. Volume past the budget is evidence against the self-report.
    if result.ok and result.records_written > max_writes:
        log.error(
            f"[{name}] wrote {result.records_written} records, budget {max_writes} — "
            "treating as a runaway"
        )
        result = ConnectorResult.failure(
            f"write budget exceeded: {result.records_written} > {max_writes}",
            records_attempted=result.records_written,
        )

    _record_status(connector, store, result, attempt_at=attempt_at)

    if result.detail.get("skipped"):
        reason = result.detail.get("skip_reason") or "skipped"
        log.info(f"[{name}] skipped — {reason}")
    elif result.ok:
        log.info(f"[{name}] ok — {result.records_written} record(s)")
    else:
        log.error(f"[{name}] FAILED — {result.error}")
    return result


def _record_status(
    connector: Connector,
    store: ConnectorStore,
    result: ConnectorResult,
    *,
    attempt_at: str,
) -> None:
    """Persist this run's outcome, preserving prior success on failure.

    The read-modify-write matters: on failure we must keep the EXISTING
    ``last_success_at`` rather than clearing it, because "last worked 3 days
    ago" is the fact an operator needs. Overwriting it with null on every
    failure would destroy exactly the signal the field exists to carry.
    """
    name = getattr(connector, "name", connector.__class__.__name__)
    interval = int(getattr(connector, "poll_interval_seconds", 0) or 0)

    prior = store.read_status(name)
    prior_success = prior.last_success_at if prior else None
    prior_failures = prior.consecutive_failures if prior else 0
    prior_status = prior.status if prior else "never_run"
    prior_written = prior.records_written if prior else 0
    # The push clock belongs to the push path, which runs outside this loop.
    # A verify must never clear it: "verified, and events are also arriving" and
    # "verified, but nothing has pushed in a week" are different situations, and
    # the second is how a silently-dead webhook becomes visible.
    prior_push = prior.last_push_at if prior else None

    skipped = bool(result.detail.get("skipped"))
    if skipped:
        # Unbound / idle: attempt happened, but do not treat as verify success
        # (no last_success_at advance) and do not accumulate failures/alerts.
        status_label = prior_status if prior_status in ("ok", "failing") else "never_run"
        status = ConnectorStatus(
            connector_name=name,
            status=status_label,
            last_attempt_at=attempt_at,
            last_success_at=prior_success,
            last_error=str(result.detail.get("skip_reason") or ""),
            records_written=prior_written,
            poll_interval_seconds=interval,
            stale_after_seconds=stale_after_for(interval),
            consecutive_failures=prior_failures,
            ingestion_mode=str(getattr(connector, "ingestion_mode", "poll") or "poll"),
            last_push_at=prior_push,
        )
    else:
        status = ConnectorStatus(
            connector_name=name,
            status="ok" if result.ok else "failing",
            last_attempt_at=attempt_at,
            last_success_at=attempt_at if result.ok else prior_success,
            last_error="" if result.ok else result.error,
            records_written=result.records_written if result.ok else prior_written,
            poll_interval_seconds=interval,
            stale_after_seconds=stale_after_for(interval),
            consecutive_failures=0 if result.ok else prior_failures + 1,
            ingestion_mode=str(getattr(connector, "ingestion_mode", "poll") or "poll"),
            last_push_at=prior_push,
        )

    try:
        store.write_status(status)
    except Exception as exc:  # noqa: BLE001
        # Losing a status write must not fail the run that produced it; the
        # observations may well have landed. Loud in the log, non-fatal here.
        log.error(f"[{name}] failed to persist connector_status: {exc}")


def run_all(
    connectors: "list[Connector]",
    store: ConnectorStore,
    *,
    max_writes: int = DEFAULT_MAX_WRITES,
) -> "dict[str, ConnectorResult]":
    """Run every connector in isolation. Returns name -> result.

    Isolation is the whole reason this loop exists: Fly being unreachable must
    not stop GitHub from syncing, and vice versa.
    """
    results: dict[str, ConnectorResult] = {}
    for connector in connectors:
        name = getattr(connector, "name", connector.__class__.__name__)
        results[name] = run_connector(connector, store, max_writes=max_writes)
    return results


def enabled_connector_names() -> "set[str] | None":
    """Which connectors may run, from ``ATELES_CONNECTORS`` (comma-separated).

    Unset means "all". This is the staging control: the Fly connector ships
    live while the GitHub one stays dark until the datastore is healthy, with
    no code change to flip between them.
    """
    raw = os.environ.get("ATELES_CONNECTORS", "").strip()
    if not raw:
        return None
    return {part.strip() for part in raw.split(",") if part.strip()}
