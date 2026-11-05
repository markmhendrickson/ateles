#!/usr/bin/env python3
"""Daily read-only reading of the operator's checkpoint queue.

A reset gate tests that no decision truly needing the operator waits more than
72 hours. This job produces the evidence once a day: how many real decisions are
open, how old the oldest is, and how much automatic noise was added.

Each run reads every checkpoint brief and every task (unfiltered, by cursor,
page by page, one request at a time), classifies the open checkpoints with
``execution/scripts/decision_queue.py`` and publishes:

* ``decision-queue-reading.json`` (mode 0600, atomic; path override
  ``DECISION_QUEUE_READING_PATH``): the numbers, plus the real decisions as task
  id, class and age only. No titles. A stale ``generated_at`` means the job
  stopped.
* two ``metric_reading`` records (and their ``metric_definition`` records),
  stored with ``store_and_verify`` so each is read back.

Nothing else is ever written. It sends nothing to anyone, never approves,
resolves or supersedes a checkpoint, and never writes to a task: every request
goes through :func:`guarded_request`, which refuses any write that is not a
``/store`` of metric types.

Failure is never an all-clear. The instrument check compares the client-side
count of open checkpoints with the server's own filtered total; if they differ
by more than the drift margin, or a read fails or comes back short, the run is
``invalid``: no reading is stored and the exit code is non-zero.

Usage: ``decision_queue_reading.py --once [--dry-run]``
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

_REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (_REPO_ROOT, _REPO_ROOT / "execution" / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import decision_queue as dq  # noqa: E402
from execution.scripts.metrics import definitions, neotoma_io, schemas  # noqa: E402

Requester = Callable[..., "dict[str, Any]"]

COLLECTOR = "execution/daemons/decision-queue-reading/decision_queue_reading.py"
ARTIFACT_REF = "ateles-checkpoint-queue"
METRIC_OVER_LIMIT = "Q1_real_decisions_waiting_over_72h"
METRIC_AUTOMATIC = "Q2_automatic_holds_added_24h"
STORABLE_TYPES = frozenset({"metric_definition", "metric_reading"})
WAIT_LIMIT_HOURS = 72.0
NEW_HOLD_HOURS = 24.0
PAGE_SIZE = 200
PAGE_PAUSE = 0.3
MAX_PAGES = 400
#: The queue moves while a few minutes of reading go by.
DRIFT_ABSOLUTE = 15
DRIFT_FRACTION = 0.01
#: A paged read that returns fewer rows than the server's own total is short.
SHORT_READ_FRACTION = 0.98
SCHEMA_VERSION = 1
NOTICE = (
    "Daily reading of the open checkpoint queue. Values are counts, ids, classes "
    "and ages; no titles. status ok means the instrument check passed and every "
    "read-back matched. status invalid means the run could not be trusted and no "
    "numbers are given: it is never an all-clear. status partial means the "
    "numbers are sound but could not be recorded as readings. An entry in "
    "failed_checks names what did not run or degraded. If generated_at is more "
    "than 36 hours old, the job has stopped: tell the operator it needs looking at."
)

_NON_PROD_LABELS = frozenset({"dev", "staging", "stage", "test", "qa", "sandbox", "preview", "local"})


class ReadFailure(RuntimeError):
    """A read could not be completed; carries a short category, never raw text."""

    def __init__(self, check: str, reason: str) -> None:
        super().__init__(f"{check}: {reason}")
        self.check = check
        self.reason = reason


# ── write guard ──────────────────────────────────────────────────────────────

def guarded_request(inner: Requester) -> Requester:
    """Wrap a requester so the only write it can make is a metric ``/store``."""

    def call(method: str, path: str, body: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        verb = method.upper()
        if verb == "GET":
            return inner(method, path, body, **kw)
        if verb == "POST" and path == "/entities/query":
            return inner(method, path, body, **kw)
        if verb == "POST" and path == "/store":
            entities = (body or {}).get("entities") or []
            kinds = {e.get("entity_type") for e in entities if isinstance(e, dict)}
            if entities and kinds <= STORABLE_TYPES:
                return inner(method, path, body, **kw)
            raise PermissionError(f"refused /store of {sorted(map(str, kinds))}")
        raise PermissionError(f"refused {verb} {path}")

    return call


# ── reading ──────────────────────────────────────────────────────────────────

def read_all_by_cursor(
    entity_type: str,
    requester: Requester,
    *,
    page: int = PAGE_SIZE,
    pause: float = PAGE_PAUSE,
    max_pages: int = MAX_PAGES,
) -> list[dict[str, Any]]:
    """Every entity of a type, unfiltered, paged by ``next_cursor``.

    The server refuses ``cursor`` with ``snapshot_filters`` and refuses offsets
    above 2000, so filtering is the caller's job. Raises :class:`ReadFailure`
    on a failed request, a repeated cursor, the page cap, or a result shorter
    than the server's own reported total.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    cursor: str | None = None
    total: int | None = None
    for page_no in range(max_pages):
        body: dict[str, Any] = {"entity_type": entity_type, "include_snapshots": True, "limit": page}
        if cursor:
            body["cursor"] = cursor
        try:
            res = requester("POST", "/entities/query", body)
        except Exception as exc:  # noqa: BLE001 - category only, never raw text
            raise ReadFailure(f"read_{entity_type}", _category(exc)) from None
        rows.extend(res.get("entities") or [])
        if total is None and isinstance(res.get("total"), int):
            total = res["total"]
        cursor = res.get("next_cursor")
        if not cursor or not (res.get("entities") or []):
            break
        if cursor in seen:
            raise ReadFailure(f"read_{entity_type}", "cursor_repeated")
        seen.add(cursor)
        time.sleep(pause)
    else:
        raise ReadFailure(f"read_{entity_type}", "page_cap")
    if total is not None and len(rows) < total * SHORT_READ_FRACTION:
        raise ReadFailure(f"read_{entity_type}", "short_read")
    return rows


def _category(exc: BaseException) -> str:
    status = getattr(exc, "status", None)
    return f"{type(exc).__name__}:{status}" if isinstance(status, int) else type(exc).__name__


def server_open_total(requester: Requester) -> int:
    """The server's own count of open checkpoints (one-row filtered query)."""
    try:
        res = requester(
            "POST",
            "/entities/query",
            {
                "entity_type": "checkpoint_brief",
                "include_snapshots": False,
                "limit": 1,
                "snapshot_filters": {"status": {"op": "eq", "value": "awaiting_operator"}},
            },
        )
    except Exception as exc:  # noqa: BLE001
        raise ReadFailure("instrument_check", _category(exc)) from None
    total = res.get("total")
    if not isinstance(total, int):
        raise ReadFailure("instrument_check", "no_total")
    return total


def drift_margin(server_total: int) -> int:
    return max(DRIFT_ABSOLUTE, int(server_total * DRIFT_FRACTION))


def instrument_ok(client_count: int, server_total: int) -> bool:
    return abs(client_count - server_total) <= drift_margin(server_total)


def operator_names(requester: Requester) -> tuple[frozenset[str], str | None]:
    """Lowercased names from ``operator_profile``; (empty, reason) if unreadable."""
    try:
        res = requester(
            "POST",
            "/entities/query",
            {"entity_type": "operator_profile", "include_snapshots": True, "limit": 5},
        )
    except Exception as exc:  # noqa: BLE001
        return frozenset(), type(exc).__name__
    names: set[str] = set()
    for row in res.get("entities") or []:
        snap = neotoma_io.snapshot_fields(row)
        for field in ("first_name", "display_name", "handle"):
            value = str(snap.get(field) or "").strip().lower()
            if value:
                names.add(value)
    if not names:
        return frozenset(), "no_operator_profile"
    return frozenset(names), None


# ── numbers ──────────────────────────────────────────────────────────────────

def real_decision_list(placements: list[dq.Placement]) -> list[dict[str, Any]]:
    """One row per real decision (task), aged from its oldest open hold. No titles."""
    best: dict[str, dq.Placement] = {}
    for p in placements:
        if p.klass not in dq.REAL_DECISION_CLASSES:
            continue
        key = p.task_id or p.checkpoint_id
        held = best.get(key)
        if held is None or (p.age_hours or 0) > (held.age_hours or 0):
            best[key] = p
    rows = [
        {"task_id": key, "class": p.klass, "age_hours": p.age_hours}
        for key, p in best.items()
    ]
    rows.sort(key=lambda r: -(r["age_hours"] or 0))
    return rows


def compute_reading(
    placements: list[dq.Placement], *, limit_hours: float = WAIT_LIMIT_HOURS
) -> dict[str, Any]:
    summary = dq.summarize(placements, wait_limit_hours=limit_hours)
    new = [p for p in placements if p.age_hours is not None and p.age_hours <= NEW_HOLD_HOURS]
    new_real = [p for p in new if p.klass in dq.REAL_DECISION_CLASSES]
    listing = real_decision_list(placements)
    if len(listing) != summary["real_decisions"]:
        raise ValueError("real decision list disagrees with the summary")
    return {
        "real_decisions": summary["real_decisions"],
        "real_decisions_over_limit": summary["real_decisions_over_limit"],
        "wait_limit_hours": summary["wait_limit_hours"],
        "oldest_hours": summary["real_decisions_oldest_hours"],
        "median_hours": summary["real_decisions_median_hours"],
        "total_open": summary["total_open"],
        "by_class": summary["by_class"],
        "new_holds_24h": len(new),
        "new_holds_24h_real": len(new_real),
        "new_holds_24h_not_real": len(new) - len(new_real),
        "real_decision_list": listing,
    }


def build_metric_readings(
    numbers: dict[str, Any], now: datetime, *, note: str | None = None
) -> list[dict[str, Any]]:
    end = now.astimezone(timezone.utc).date()
    window_end = end.isoformat()
    window_start = (end - timedelta(days=1)).isoformat()
    sourced = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    common = dict(
        artifact_ref=ARTIFACT_REF,
        window_start=window_start,
        window_end=window_end,
        sourced_at=sourced,
        coverage_ratio=1.0,
        coverage_note=note,
        probe_ok=True,
        collector=COLLECTOR,
        status="ok",
    )
    return [
        schemas.build_record(
            "metric_reading",
            reading_key=schemas.reading_key(METRIC_OVER_LIMIT, ARTIFACT_REF, window_end),
            metric_ref=METRIC_OVER_LIMIT,
            value=numbers["real_decisions_over_limit"],
            unit="decisions",
            detail={
                "real_decisions": numbers["real_decisions"],
                "oldest_hours": numbers["oldest_hours"],
                "median_hours": numbers["median_hours"],
                "by_class": numbers["by_class"],
            },
            **common,
        ),
        schemas.build_record(
            "metric_reading",
            reading_key=schemas.reading_key(METRIC_AUTOMATIC, ARTIFACT_REF, window_end),
            metric_ref=METRIC_AUTOMATIC,
            value=numbers["new_holds_24h_not_real"],
            unit="holds",
            detail={
                "total": numbers["new_holds_24h"],
                "real": numbers["new_holds_24h_real"],
            },
            **common,
        ),
    ]


def own_definitions() -> list[dict[str, Any]]:
    return [d for d in definitions.build_definitions() if d.get("collector") == COLLECTOR]


# ── publishing ───────────────────────────────────────────────────────────────

def default_path() -> Path:
    override = os.environ.get("DECISION_QUEUE_READING_PATH", "").strip()
    return Path(override) if override else Path.home() / ".local/state/ateles/decision-queue-reading.json"


def write_document(path: Path, document: dict[str, Any]) -> None:
    """Atomic, owner-only write: a reader sees the old file or the new, never half."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(document, indent=2, sort_keys=True))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise


def build_document(
    *,
    status: str,
    now: datetime,
    numbers: dict[str, Any] | None,
    failed_checks: list[dict[str, str]],
    instrument: dict[str, Any] | None,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "notice": NOTICE,
        "generated_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        "failed_checks": failed_checks,
        "instrument": instrument,
    }
    if numbers is not None and status in ("ok", "partial"):
        doc.update({k: v for k, v in numbers.items() if k != "real_decision_list"})
        doc["real_decision_list"] = numbers["real_decision_list"]
    return doc


def assert_prod_target() -> None:
    from lib.capabilities import neotoma_http

    host = (urllib.parse.urlparse(neotoma_http.base_url()).hostname or "").lower()
    if set(host.split(".")) & _NON_PROD_LABELS or host in ("localhost", "127.0.0.1", "::1"):
        raise ReadFailure("target", "non_prod_target")
    neotoma_http.bearer_token()


# ── orchestration ────────────────────────────────────────────────────────────

def run(
    requester: Requester,
    now: datetime,
    *,
    dry_run: bool,
    pause: float = PAGE_PAUSE,
    path: Path | None = None,
) -> tuple[int, dict[str, Any]]:
    """Read, classify, publish. Returns (exit code, document)."""
    requester = guarded_request(requester)
    failed: list[dict[str, str]] = []
    instrument: dict[str, Any] | None = None

    def finish(status: str, numbers: dict[str, Any] | None, code: int) -> tuple[int, dict[str, Any]]:
        doc = build_document(
            status=status, now=now, numbers=numbers, failed_checks=failed, instrument=instrument
        )
        if not dry_run:
            try:
                write_document(path or default_path(), doc)
            except (OSError, TypeError, ValueError) as exc:
                print(f"COULD NOT WRITE READING FILE: {type(exc).__name__}", file=sys.stderr)
                return 1, doc
        return code, doc

    try:
        briefs = read_all_by_cursor("checkpoint_brief", requester, pause=pause)
        tasks = read_all_by_cursor("task", requester, pause=pause)
        client_open = sum(
            1 for r in briefs if (r.get("snapshot") or {}).get("status") == "awaiting_operator"
        )
        server_total = server_open_total(requester)
    except ReadFailure as exc:
        failed.append({"check": exc.check, "reason": exc.reason})
        return finish("invalid", None, 1)

    instrument = {
        "client_open": client_open,
        "server_open_total": server_total,
        "drift_margin": drift_margin(server_total),
        "checkpoints_read": len(briefs),
        "tasks_read": len(tasks),
        "passed": instrument_ok(client_open, server_total),
    }
    if not instrument["passed"]:
        failed.append({"check": "instrument_check", "reason": "count_mismatch"})
        return finish("invalid", None, 1)

    names, why = operator_names(requester)
    if why:
        failed.append({"check": "operator_profile", "reason": why + "; classifier default used"})
    people = dq._person_owners() | names
    tasks_by_id = {r["entity_id"]: r for r in tasks if r.get("entity_id")}
    placements = dq.classify_queue(briefs, tasks_by_id, now=now, person_owners=people)
    try:
        numbers = compute_reading(placements)
    except ValueError:
        failed.append({"check": "classification", "reason": "list_summary_mismatch"})
        return finish("invalid", None, 1)

    if dry_run:
        return finish("ok", numbers, 0)

    note = "operator names unavailable; classifier default used" if why else None
    records = build_metric_readings(numbers, now, note=note)
    problems: list[str] = []
    try:
        problems += neotoma_io.store_and_verify(
            own_definitions(), "decision-queue-definitions", "metric_id", requester=requester
        )
        problems += neotoma_io.store_and_verify(
            records, "decision-queue-readings", "reading_key", requester=requester
        )
    except Exception as exc:  # noqa: BLE001
        problems.append(type(exc).__name__)
    if problems:
        failed.append({"check": "store_and_verify", "reason": "read_back_failed"})
        return finish("partial", numbers, 1)
    return finish("ok", numbers, 0)


def main(
    argv: list[str] | None = None,
    *,
    requester: Requester | None = None,
    now: datetime | None = None,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--once", action="store_true", help="run one reading (the default)")
    parser.add_argument("--dry-run", action="store_true", help="read and print; write nothing")
    args = parser.parse_args(argv)

    if requester is None:
        # Importing lib.daemon_runtime loads the materialized dotenv into the
        # environment (the repo's existing credential path). Nothing is printed.
        import lib.daemon_runtime  # noqa: F401

        try:
            assert_prod_target()
        except Exception as exc:  # noqa: BLE001
            print(f"REFUSED: {getattr(exc, 'reason', type(exc).__name__)}", file=sys.stderr)
            return 2
        requester = neotoma_io.request
    code, doc = run(requester, now or datetime.now(timezone.utc), dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(doc, indent=2, sort_keys=True))
    else:
        print(f"status={doc['status']} failed_checks={len(doc['failed_checks'])}")
    return code


if __name__ == "__main__":
    sys.exit(main())
