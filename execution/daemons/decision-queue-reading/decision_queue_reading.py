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

Holds are also counted by when they were raised, not only while they are open.
``last_observation_at`` moves when a hold is approved or retired, so the raised
time is the hold's earliest observation: read from
``/entities/{id}/observations`` for each hold touched in the last 24 hours,
sequentially, up to ``MAX_OBSERVATION_READS`` per run. Holds raised in the
window are bucketed as real, delivery lane (counted apart, never hidden) or
outside the lane. A raised time that cannot be established is counted as
unknown and keeps the finish test from counting the day as clean.
``finish_test`` carries the run of consecutive clean readings.

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
from lib.daemon_runtime.task_ownership import operator_names  # noqa: E402

Requester = Callable[..., "dict[str, Any]"]

COLLECTOR = "execution/daemons/decision-queue-reading/decision_queue_reading.py"
ARTIFACT_REF = "ateles-checkpoint-queue"
METRIC_OVER_LIMIT = "Q1_real_decisions_waiting_over_72h"
METRIC_AUTOMATIC = "Q2_automatic_holds_added_24h"
METRIC_OUTSIDE_FLY = "Q3_automatic_holds_outside_fly_raised_24h"
METRIC_FLY_LANE = "Q4_fly_lane_holds_raised_24h"
#: The live record type that holds the operator's open holds. Its name is retired in the
#: foundation vocabulary but the records are still written under it until migration.md runs.
CHECKPOINT_RECORD_TYPE = (
    "checkpoint_brief"  # vocab-ok: live record type, retired name pending migration
)
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
#: Observation reads per run, for the hold raised times. Past it, unknown.
MAX_OBSERVATION_READS = 1000
#: Consecutive clean daily readings that finish the milestone.
FINISH_NEEDED = 3
SCHEMA_VERSION = 1
NOTICE = (
    "Daily reading of the open checkpoint queue. Values are counts, ids, classes "
    "and ages; no titles. status ok means the instrument check passed and every "
    "read-back matched. status invalid means the run could not be trusted and no "
    "numbers are given: it is never an all-clear. status partial means the "
    "numbers are sound but could not be recorded as readings. An entry in "
    "failed_checks names what did not run or degraded. If generated_at is more "
    "than 36 hours old, the job has stopped: tell the operator it needs looking at. "
    "Holds raised in the last 24 hours are counted by their earliest observation, "
    "not their last: fly_lane_holds_raised_24h are automatic holds on delivery-lane "
    "tasks, counted apart and not a decision; outside_fly_holds_raised_24h and "
    "real_holds_raised_24h are the ones that matter; holds_raised_time_unknown_24h "
    "could not be placed and name a failed check. finish_test counts consecutive "
    "clean daily readings; it only reports. A day is clean when no real decision has "
    "waited over the limit, no automatic hold outside the lane was raised, none is "
    "unknown, and no check failed (a degraded run is never clean). A new real "
    "decision does not stop a day being clean, because a real decision is not "
    "clutter: it counts in real_holds_raised_24h, and only waiting over the limit "
    "stops the day. clean_today null means the run was invalid, so the day did not "
    "count. open_fly_holds lists each open hold on a delivery-lane task (checkpoint "
    "id, task id, age in hours, oldest first), shown apart because the lane's "
    "automatic holds are expected."
)

_NON_PROD_LABELS = frozenset(
    {"dev", "staging", "stage", "test", "qa", "sandbox", "preview", "local"}
)


class ReadFailure(RuntimeError):
    """A read could not be completed; carries a short category, never raw text."""

    def __init__(self, check: str, reason: str) -> None:
        super().__init__(f"{check}: {reason}")
        self.check = check
        self.reason = reason


# ── write guard ──────────────────────────────────────────────────────────────


def guarded_request(inner: Requester) -> Requester:
    """Wrap a requester so the only write it can make is a metric ``/store``."""

    def call(
        method: str, path: str, body: dict[str, Any] | None = None, **kw: Any
    ) -> dict[str, Any]:
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
        body: dict[str, Any] = {
            "entity_type": entity_type,
            "include_snapshots": True,
            "limit": page,
        }
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
    return (
        f"{type(exc).__name__}:{status}"
        if isinstance(status, int)
        else type(exc).__name__
    )


def server_open_total(requester: Requester) -> int:
    """The server's own count of open checkpoints (one-row filtered query)."""
    try:
        res = requester(
            "POST",
            "/entities/query",
            {
                "entity_type": CHECKPOINT_RECORD_TYPE,
                "include_snapshots": False,
                "limit": 1,
                "snapshot_filters": {
                    "status": {"op": "eq", "value": "awaiting_operator"}
                },
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


# ── numbers ──────────────────────────────────────────────────────────────────


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def real_decision_list(
    placements: list[dq.Placement],
    now: datetime,
    previous_first_seen: dict[str, datetime] | None = None,
) -> list[dict[str, Any]]:
    """One row per real decision (task). No titles.

    A task is aged from the earlier of its previous ``first_seen`` and the
    oldest open hold's own ``raised_at``, so a re-filed or touched hold does not
    restart its clock. ``previous_first_seen`` holds only tasks that were on the
    previous real list: a task that left it starts again from its hold's time.
    """
    previous = previous_first_seen or {}
    best: dict[str, dq.Placement] = {}
    for p in placements:
        if p.klass not in dq.REAL_DECISION_CLASSES:
            continue
        key = p.task_id or p.checkpoint_id
        held = best.get(key)
        if held is None or (p.age_hours or 0) > (held.age_hours or 0):
            best[key] = p
    rows = []
    for key, p in best.items():
        candidates = [t for t in (dq._parse_time(p.raised_at), previous.get(key)) if t]
        first_seen = min(candidates) if candidates else None
        age = (
            round((now - first_seen).total_seconds() / 3600, 1) if first_seen else None
        )
        rows.append(
            {
                "task_id": key,
                "class": p.klass,
                "age_hours": age,
                "first_seen": _iso(first_seen) if first_seen else None,
            }
        )
    rows.sort(key=lambda r: -(r["age_hours"] or 0))
    return rows


def _earliest_observation(
    requester: Requester, entity_id: str, count: Any
) -> tuple[datetime | None, str | None]:
    """The hold's earliest observation time, or (None, category) when unknown."""
    size = (count if isinstance(count, int) and count > 0 else 20) + 5
    path = (
        f"/entities/{urllib.parse.quote(entity_id, safe='')}/observations?limit={size}"
    )
    try:
        res = requester("GET", path, None)
    except Exception as exc:  # noqa: BLE001 - category only, never raw text
        return None, _category(exc)
    rows = res.get("observations") if isinstance(res, dict) else None
    total = res.get("total") if isinstance(res, dict) else None
    if not isinstance(rows, list) or not isinstance(total, int):
        return None, "bad_response"
    if len(rows) < total:
        return None, "short_read"
    times = [
        t
        for t in (
            dq._parse_time(r.get("observed_at")) for r in rows if isinstance(r, dict)
        )
        if t
    ]
    if not times:
        return None, "no_observed_at"
    return min(times), None


def measure_raised_holds(
    briefs: list[dict[str, Any]],
    tasks_by_id: dict[str, dict[str, Any]],
    placements: list[dq.Placement],
    *,
    now: datetime,
    requester: Requester,
    pause: float = PAGE_PAUSE,
    max_reads: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Bucket the automatic holds raised in the last 24 hours, open or closed.

    Returns the numbers and at most one failed-check entry. A hold touched in
    the window whose first observation is older does not count: approving or
    retiring an old hold moves ``last_observation_at`` but not its raised time.
    A hold with one observation is raised at its last observation, with no
    request; any other is read one request at a time, never in parallel.
    """
    max_reads = MAX_OBSERVATION_READS if max_reads is None else max_reads
    klass_of = {p.checkpoint_id: p.klass for p in placements}
    cutoff = timedelta(hours=NEW_HOLD_HOURS)
    counts = {dq.BUCKET_REAL: 0, dq.BUCKET_FLY_LANE: 0, dq.BUCKET_OUTSIDE_FLY: 0}
    outside_tasks: set[str] = set()
    unknown = 0
    reads = 0
    categories: set[str] = set()
    for record in briefs:
        brief = record.get("snapshot") or {}
        touched = dq._parse_time(record.get("last_observation_at"))
        if touched is None or now - touched > cutoff:
            continue
        reason = str(brief.get("reason") or "").strip()
        if not dq.is_gate_written(brief) or not dq.is_automatic_reason(reason):
            continue
        cid = str(record.get("entity_id") or "")
        raised: datetime | None = touched
        if record.get("observation_count") != 1:
            if reads >= max_reads or not cid:
                raised, why = None, "read_cap" if cid else "no_entity_id"
            else:
                if reads:
                    time.sleep(pause)
                reads += 1
                raised, why = _earliest_observation(
                    requester, cid, record.get("observation_count")
                )
            if raised is None:
                unknown += 1
                categories.add(why or "unknown")
                continue
        if now - raised > cutoff:
            continue
        task_id = brief.get("task_entity_id")
        task = ((tasks_by_id.get(task_id) if task_id else None) or {}).get("snapshot")
        bucket = dq.bucket_raised_hold(brief, task, klass_of.get(cid))
        if bucket is None:
            continue
        counts[bucket] += 1
        if bucket == dq.BUCKET_OUTSIDE_FLY and task_id:
            outside_tasks.add(str(task_id))
    failed: list[dict[str, str]] = []
    if unknown:
        failed.append(
            {
                "check": "hold_raised_time",
                "reason": ",".join(sorted(categories)),
            }
        )
    return {
        "fly_lane_holds_raised_24h": counts[dq.BUCKET_FLY_LANE],
        "outside_fly_holds_raised_24h": counts[dq.BUCKET_OUTSIDE_FLY],
        "real_holds_raised_24h": counts[dq.BUCKET_REAL],
        "holds_raised_time_unknown_24h": unknown,
        "outside_fly_hold_tasks": sorted(outside_tasks),
    }, failed


def open_fly_holds(placements: list[dq.Placement]) -> list[dict[str, Any]]:
    """One row per open lane hold, oldest first. No titles."""
    rows = [
        {
            "checkpoint_id": p.checkpoint_id,
            "task_id": p.task_id,
            "age_hours": p.age_hours,
        }
        for p in placements
        if p.klass == dq.FLY_LANE_HOLD
    ]
    rows.sort(key=lambda r: -(r["age_hours"] or 0))
    return rows


def is_clean(numbers: dict[str, Any], failed_checks: list[dict[str, str]]) -> bool:
    """A degraded run is never clean: any failed check means not clean."""
    return (
        not failed_checks
        and numbers["real_decisions_over_limit"] == 0
        and numbers["outside_fly_holds_raised_24h"] == 0
        and numbers["holds_raised_time_unknown_24h"] == 0
    )


def next_finish_test(
    previous: dict[str, Any] | None,
    *,
    numbers: dict[str, Any] | None,
    now: datetime,
    failed_checks: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """The finish test after this run. ``numbers`` None means an invalid run.

    An invalid run passes the previous value through, with ``clean_today``
    null: ``through`` then stops being yesterday, so a missed day resets the
    count at the next good run. A clean run on the day after ``through`` adds
    one, a rerun on ``through`` keeps the count (at least 1), any other clean
    run starts at 1, and a run that is not clean resets to 0.
    """
    today = now.astimezone(timezone.utc).date()
    prev_count = 0
    prev_through = None
    if isinstance(previous, dict):
        count = previous.get("consecutive_clean_readings")
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            prev_count = count
        try:
            prev_through = datetime.fromisoformat(str(previous.get("through"))).date()
        except ValueError:
            prev_through = None

    def doc(count: int, clean: bool | None, through: Any) -> dict[str, Any]:
        return {
            "needed": FINISH_NEEDED,
            "consecutive_clean_readings": count,
            "clean_today": clean,
            "through": through,
            "met": count >= FINISH_NEEDED,
        }

    if numbers is None:
        return doc(prev_count, None, prev_through.isoformat() if prev_through else None)
    if not is_clean(numbers, failed_checks or []):
        return doc(0, False, today.isoformat())
    if prev_through == today:
        count = max(prev_count, 1)
    elif prev_through == today - timedelta(days=1):
        count = prev_count + 1
    else:
        count = 1
    return doc(count, True, today.isoformat())


def load_previous_finish_test(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    value = doc.get("finish_test") if isinstance(doc, dict) else None
    return value if isinstance(value, dict) else None


def compute_reading(
    placements: list[dq.Placement],
    *,
    now: datetime,
    previous_first_seen: dict[str, datetime] | None = None,
    limit_hours: float = WAIT_LIMIT_HOURS,
    raised: dict[str, Any],
) -> dict[str, Any]:
    summary = dq.summarize(placements, wait_limit_hours=limit_hours)
    new = [
        p
        for p in placements
        if p.age_hours is not None and p.age_hours <= NEW_HOLD_HOURS
    ]
    new_real = [p for p in new if p.klass in dq.REAL_DECISION_CLASSES]
    listing = real_decision_list(placements, now, previous_first_seen)
    if len(listing) != summary["real_decisions"]:
        raise ValueError("real decision list disagrees with the summary")
    ages = sorted(r["age_hours"] for r in listing if r["age_hours"] is not None)
    return {
        "real_decisions": summary["real_decisions"],
        "real_decisions_over_limit": sum(1 for age in ages if age > limit_hours),
        "wait_limit_hours": summary["wait_limit_hours"],
        "oldest_hours": ages[-1] if ages else None,
        "median_hours": ages[len(ages) // 2] if ages else None,
        "age_basis": "first_seen",
        "total_open": summary["total_open"],
        "by_class": summary["by_class"],
        "new_holds_24h": len(new),
        "new_holds_24h_real": len(new_real),
        "new_holds_24h_not_real": len(new) - len(new_real),
        "real_decision_list": listing,
        **raised,
        "open_fly_holds": open_fly_holds(placements),
    }


def load_previous_first_seen(path: Path) -> tuple[dict[str, datetime], str | None]:
    """first_seen per task from the previous file; (empty, reason) if unusable.

    The clock lives in ``first_seen_carry``, which every run writes: a good run
    from its real list, an invalid run by passing the previous value through
    untouched, so any number of failed runs in between lose nothing. Files from
    before the carry key existed are read from ``real_decision_list`` once.
    """
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return {}, "missing"
    except (OSError, ValueError):
        return {}, "unreadable"
    if not isinstance(doc, dict):
        return {}, "unreadable"
    out: dict[str, datetime] = {}
    carry = doc.get("first_seen_carry")
    if isinstance(carry, dict):
        for task_id, value in carry.items():
            seen = dq._parse_time(value)
            if seen and task_id:
                out[str(task_id)] = seen
        return out, None
    rows = doc.get("real_decision_list")
    if not isinstance(rows, list):
        return {}, "no_carry"
    for row in rows:
        if isinstance(row, dict) and row.get("task_id"):
            seen = dq._parse_time(row.get("first_seen"))
            if seen:
                out[str(row["task_id"])] = seen
    return out, None


def build_metric_readings(
    numbers: dict[str, Any],
    now: datetime,
    *,
    note: str | None = None,
    coverage_ratio: float = 1.0,
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
        coverage_ratio=coverage_ratio,
        coverage_note=note,
        probe_ok=True,
        collector=COLLECTOR,
        status="ok",
    )
    return [
        schemas.build_record(
            "metric_reading",
            reading_key=schemas.reading_key(
                METRIC_OVER_LIMIT, ARTIFACT_REF, window_end
            ),
            metric_ref=METRIC_OVER_LIMIT,
            value=numbers["real_decisions_over_limit"],
            unit="decisions",
            detail={
                "real_decisions": numbers["real_decisions"],
                "oldest_hours": numbers["oldest_hours"],
                "median_hours": numbers["median_hours"],
                "by_class": numbers["by_class"],
                "age_basis": "first_seen: age runs from the earlier of the task's first appearance "
                "on the real list and its hold's own time; a re-filed hold does not restart it",
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
        schemas.build_record(
            "metric_reading",
            reading_key=schemas.reading_key(
                METRIC_OUTSIDE_FLY, ARTIFACT_REF, window_end
            ),
            metric_ref=METRIC_OUTSIDE_FLY,
            value=numbers["outside_fly_holds_raised_24h"],
            unit="holds",
            detail={
                "real": numbers["real_holds_raised_24h"],
                "unknown": numbers["holds_raised_time_unknown_24h"],
            },
            **common,
        ),
        schemas.build_record(
            "metric_reading",
            reading_key=schemas.reading_key(METRIC_FLY_LANE, ARTIFACT_REF, window_end),
            metric_ref=METRIC_FLY_LANE,
            value=numbers["fly_lane_holds_raised_24h"],
            unit="holds",
            detail={},
            **common,
        ),
    ]


def own_definitions() -> list[dict[str, Any]]:
    return [
        d for d in definitions.build_definitions() if d.get("collector") == COLLECTOR
    ]


# ── publishing ───────────────────────────────────────────────────────────────


def default_path() -> Path:
    override = os.environ.get("DECISION_QUEUE_READING_PATH", "").strip()
    return (
        Path(override)
        if override
        else Path.home() / ".local/state/ateles/decision-queue-reading.json"
    )


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
    carry: dict[str, datetime] | None = None,
    finish_test: dict[str, Any] | None = None,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "notice": NOTICE,
        "generated_at": now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        "failed_checks": failed_checks,
        "instrument": instrument,
        # The first-seen clock, carried through every run including invalid ones.
        "first_seen_carry": {k: _iso(v) for k, v in sorted((carry or {}).items())},
        # Consecutive clean readings, carried through every run like the clock.
        "finish_test": finish_test,
    }
    if numbers is not None and status in ("ok", "partial"):
        doc.update({k: v for k, v in numbers.items() if k != "real_decision_list"})
        doc["real_decision_list"] = numbers["real_decision_list"]
    return doc


def assert_prod_target() -> None:
    from lib.capabilities import neotoma_http

    host = (urllib.parse.urlparse(neotoma_http.base_url()).hostname or "").lower()
    if set(host.split(".")) & _NON_PROD_LABELS or host in (
        "localhost",
        "127.0.0.1",
        "::1",
    ):
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
    previous, why_prev = load_previous_first_seen(path or default_path())
    previous_finish = load_previous_finish_test(path or default_path())

    def finish(
        status: str, numbers: dict[str, Any] | None, code: int
    ) -> tuple[int, dict[str, Any]]:
        if numbers is not None and status in ("ok", "partial"):
            carry = {
                r["task_id"]: t
                for r in numbers["real_decision_list"]
                if (t := dq._parse_time(r.get("first_seen")))
            }
        else:
            carry = previous  # an invalid run must not lose the clock
        if why_prev:
            failed.append(
                {
                    "check": "previous_reading",
                    "reason": why_prev + "; aged from raised_at",
                }
            )
        doc = build_document(
            status=status,
            now=now,
            numbers=numbers,
            failed_checks=failed,
            instrument=instrument,
            carry=carry,
            finish_test=next_finish_test(
                previous_finish,
                numbers=numbers if status in ("ok", "partial") else None,
                now=now,
                failed_checks=failed,
            ),
        )
        if not dry_run:
            try:
                write_document(path or default_path(), doc)
            except (OSError, TypeError, ValueError) as exc:
                print(
                    f"COULD NOT WRITE READING FILE: {type(exc).__name__}",
                    file=sys.stderr,
                )
                return 1, doc
        return code, doc

    try:
        briefs = read_all_by_cursor(CHECKPOINT_RECORD_TYPE, requester, pause=pause)
        tasks = read_all_by_cursor("task", requester, pause=pause)
        client_open = sum(
            1
            for r in briefs
            if (r.get("snapshot") or {}).get("status") == "awaiting_operator"
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
        failed.append(
            {"check": "operator_profile", "reason": why + "; classifier default used"}
        )
    people = dq._person_owners() | names
    tasks_by_id = {r["entity_id"]: r for r in tasks if r.get("entity_id")}
    placements = dq.classify_queue(briefs, tasks_by_id, now=now, person_owners=people)
    raised, raised_failed = measure_raised_holds(
        briefs, tasks_by_id, placements, now=now, requester=requester, pause=pause
    )
    failed.extend(raised_failed)
    try:
        numbers = compute_reading(
            placements, now=now, previous_first_seen=previous, raised=raised
        )
    except ValueError:
        failed.append({"check": "classification", "reason": "list_summary_mismatch"})
        return finish("invalid", None, 1)

    if dry_run:
        return finish("ok", numbers, 0)

    notes = []
    if why:
        notes.append("operator names unavailable; classifier default used")
    coverage = 1.0
    if why_prev:
        notes.append(
            f"previous first-seen state unavailable ({why_prev}); ages run from hold time "
            "and may be understated"
        )
        if numbers["real_decisions"]:
            coverage = 0.5
    records = build_metric_readings(
        numbers, now, note="; ".join(notes) or None, coverage_ratio=coverage
    )
    problems: list[str] = []
    try:
        problems += neotoma_io.store_and_verify(
            own_definitions(),
            "decision-queue-definitions",
            "metric_id",
            requester=requester,
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
    parser.add_argument(
        "--once", action="store_true", help="run one reading (the default)"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="read and print; write nothing"
    )
    args = parser.parse_args(argv)

    if requester is None:
        # Importing lib.daemon_runtime loads the materialized dotenv into the
        # environment (the repo's existing credential path). Nothing is printed.
        import lib.daemon_runtime  # noqa: F401

        try:
            assert_prod_target()
        except Exception as exc:  # noqa: BLE001
            print(
                f"REFUSED: {getattr(exc, 'reason', type(exc).__name__)}",
                file=sys.stderr,
            )
            return 2
        requester = neotoma_io.request
    code, doc = run(requester, now or datetime.now(timezone.utc), dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(doc, indent=2, sort_keys=True))
    else:
        over = doc.get("real_decisions_over_limit", "n/a")
        auto = doc.get("new_holds_24h_not_real", "n/a")
        outside = doc.get("outside_fly_holds_raised_24h", "n/a")
        lane = doc.get("fly_lane_holds_raised_24h", "n/a")
        unknown = doc.get("holds_raised_time_unknown_24h", "n/a")
        finish = (doc.get("finish_test") or {}).get("consecutive_clean_readings", "n/a")
        print(
            f"date={doc['generated_at'][:10]} status={doc['status']} "
            f"real_decisions_over_72h={over} automatic_holds_added_24h={auto} "
            f"outside_fly_holds_raised_24h={outside} "
            f"fly_lane_holds_raised_24h={lane} "
            f"holds_raised_time_unknown_24h={unknown} "
            f"finish_test_clean_readings={finish} "
            f"failed_checks={len(doc['failed_checks'])}"
        )
    return code


if __name__ == "__main__":
    sys.exit(main())
