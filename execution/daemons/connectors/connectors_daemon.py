#!/usr/bin/env python3
"""
The connector daemon — the live trigger for ``lib/connectors``.

## Why this file is the deliverable

This codebase's signature failure is correct code nobody calls. ``sync_issues``
exists as an MCP tool with no daemon and no scheduled caller. The worker pool
built 2026-07-29 was never selected. ``agent_auto_invocation.py`` is fully
tested and wired into zero lines of config. The digest queue has zero non-test
callers.

The connector framework is only worth having if something runs it, so this
daemon plus its launchd plist — not the library — is what makes the stage real.

## What it does

Every ``CONNECTOR_POLL_SECONDS`` it runs each enabled connector, records
per-connector status in Neotoma, and emits a ``daemon_report`` when a connector
is failing. Anthus already subscribes to ``daemon_report`` over SSE and
surfaces ``error``/``critical`` to the operator, so a broken connector pages
someone without new plumbing.

## Posture

Advisory and read-only with respect to infrastructure. It observes; it never
deploys, resizes, restarts, or otherwise changes a live instance. A failure in
one connector is isolated by the runner and never stops the others.

Run it once by hand without touching the schedule:

    python3 execution/daemons/riparia-connectors/connectors_daemon.py --once
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# The daemon runs from a checkout, not an installed package.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# launchd does not source shell profiles, so credentials come from the same
# materialized dotenv every other daemon reads. setdefault, so an explicit
# environment always wins over the file.
_NEOTOMA_ENV_FILE = Path.home() / ".config" / "neotoma" / ".env"
if _NEOTOMA_ENV_FILE.exists():
    for _line in _NEOTOMA_ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _, _v = _line.partition("=")
        os.environ.setdefault(_k.strip(), _v.strip().strip("\"'"))

from lib.connectors.base import ConnectorResult  # noqa: E402
from lib.connectors.runner import enabled_connector_names, run_all  # noqa: E402
from lib.connectors.store import (  # noqa: E402
    NEOTOMA_USER_AGENT,
    ConnectorStore,
)

logging.basicConfig(
    level=os.environ.get("ATELES_LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("connectors")

DAEMON_NAME = "connectors"

#: How often the loop runs. Each connector declares its own poll interval and
#: staleness threshold; this is the floor at which they are offered a turn.
POLL_SECONDS = int(os.environ.get("CONNECTOR_POLL_SECONDS", "900"))  # 15 min

#: Alert only after this many consecutive failures. One failed run is routine —
#: a laptop sleeps, a fetch times out — and paging on it is how an alert
#: becomes something people mute.
ALERT_AFTER_FAILURES = int(os.environ.get("CONNECTOR_ALERT_AFTER_FAILURES", "3"))


def build_connectors() -> list:
    """Instantiate the enabled connectors.

    Registration is deliberately explicit rather than auto-discovered: a
    connector that starts running because a file appeared on disk is not a
    property anyone wants from the thing that writes to durable memory.

    Stage 2 adds the Fly connector here. Stage 5 adds GitHub, held until the
    Neotoma performance fix lands — building a sync against a 502-ing instance
    is how the last runaway happened.
    """
    connectors: list = []
    allowed = enabled_connector_names()

    # ── stage 2 ──
    # from lib.connectors.fly import FlyConnector
    # connectors.append(FlyConnector())

    if allowed is not None:
        connectors = [c for c in connectors if getattr(c, "name", "") in allowed]
    return connectors


def emit_daemon_report(severity: str, message: str, details: dict | None = None) -> None:
    """Write a ``daemon_report``. Anthus surfaces error/critical to the operator.

    Best-effort by design: an observability write must never take down the
    thing it observes.
    """
    base = os.environ.get("NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com")
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    if not token:
        log.debug("no bearer token — skipping daemon_report")
        return

    payload = {
        "entity_type": "daemon_report",
        "daemon_name": DAEMON_NAME,
        "aauth_sub": f"{DAEMON_NAME}@ateles-swarm",
        "severity": severity,
        "message": message,
        "report_at": datetime.now(timezone.utc).isoformat(),
    }
    if details:
        payload["details"] = json.dumps(details)

    body = json.dumps(
        {
            "entities": [payload],
            # Keyed on the day and the message so a connector failing every 15
            # minutes reports once per day rather than 96 times.
            "idempotency_key": (
                f"{DAEMON_NAME}-{severity}-"
                f"{datetime.now(timezone.utc).date().isoformat()}-"
                f"{abs(hash(message)) % 10**8}"
            ),
        }
    ).encode()

    try:
        req = urllib.request.Request(
            f"{base.rstrip('/')}/store",
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": NEOTOMA_USER_AGENT,
            },
        )
        with urllib.request.urlopen(req, timeout=30):
            pass
    except Exception as exc:  # noqa: BLE001
        log.debug(f"daemon_report write failed: {exc}")


def alert_on_failures(store: ConnectorStore, results: "dict[str, ConnectorResult]") -> None:
    """Report connectors that have failed repeatedly.

    Deliberately alarms on the CONNECTOR's health, not on what it observed. An
    alarm derived from a stale observation ("5 releases behind", computed from
    a day-old reading) asserts a present it cannot see. A connector that
    stopped working is both certain and more actionable.
    """
    for name, result in results.items():
        if result.ok:
            continue
        status = store.read_status(name)
        failures = status.consecutive_failures if status else 1
        if failures < ALERT_AFTER_FAILURES:
            log.info(f"[{name}] failure {failures}/{ALERT_AFTER_FAILURES}, not alerting yet")
            continue

        last_ok = (status.last_success_at if status else None) or "never"
        emit_daemon_report(
            "error",
            f"connector {name!r} has failed {failures} consecutive runs "
            f"(last success: {last_ok}) — its data is going stale",
            {"connector": name, "consecutive_failures": failures,
             "last_success_at": last_ok, "error": result.error},
        )


def run_once() -> "dict[str, ConnectorResult]":
    """One pass over every enabled connector."""
    connectors = build_connectors()
    if not connectors:
        log.info(
            "no connectors registered — the framework is live but has no sources yet "
            "(stage 2 adds Fly)"
        )
        return {}

    store = ConnectorStore()
    if not store.configured:
        # Never silently skip: "no token" and "nothing to report" must not look
        # the same, which is the whole failure class this package addresses.
        log.error("NEOTOMA_BEARER_TOKEN missing — cannot record connector state")
        return {}

    log.info(f"running {len(connectors)} connector(s): "
             f"{', '.join(getattr(c, 'name', '?') for c in connectors)}")
    results = run_all(connectors, store)
    alert_on_failures(store, results)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Ateles connectors.")
    parser.add_argument(
        "--once", action="store_true", help="run one pass and exit (default: loop)"
    )
    args = parser.parse_args()

    if args.once:
        results = run_once()
        return 0 if all(r.ok for r in results.values()) else 1

    log.info(f"connector daemon starting — poll every {POLL_SECONDS}s")
    while True:
        try:
            run_once()
        except Exception:  # noqa: BLE001
            # The loop must outlive any single pass; launchd restarting us on a
            # transient Neotoma 502 would be a restart storm, not a recovery.
            log.exception("connector pass failed; continuing")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())
