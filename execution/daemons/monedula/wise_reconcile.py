"""Read a Wise transfer's latest status without interpreting funding as payment.

The transport is injectable so tests never contact Wise. An incomplete or
unreadable response raises and leaves the journal's retry hold in place.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

TRANSFER_STATUSES = frozenset({
    "incoming_payment_waiting", "incoming_payment_initiated", "processing",
    "funds_converted", "outgoing_payment_sent", "bounced_back",
    "charged_back", "cancelled", "funds_refunded", "unknown",
})
RETURN_STATUSES = frozenset({"bounced_back", "charged_back", "cancelled", "funds_refunded"})
MONITOR_DAYS = 30
FIRST_BUSINESS_DAYS = 3


class TransferReadError(ValueError):
    """The transfer response cannot prove a current payment state."""


def _stamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TransferReadError("Wise transfer timestamp missing")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TransferReadError("Wise transfer timestamp invalid") from exc
    if parsed.tzinfo is None:
        raise TransferReadError("Wise transfer timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def compare_observation(
    status: str, occurred_at: str, last_status: str | None,
    last_occurred_at: str | None,
) -> str:
    """Order transfer reads; equal instants with different statuses are unknown."""
    current = _stamp(occurred_at)
    if last_occurred_at is None:
        return "later"
    previous = _stamp(last_occurred_at)
    if current < previous:
        return "earlier"
    if current > previous:
        return "later"
    return "same" if status == last_status else "conflict"


def latest_status(body: Any) -> tuple[str, str]:
    """Choose the latest event by occurred_at, including rollback transitions."""
    if not isinstance(body, dict):
        raise TransferReadError("Wise transfer body malformed")
    history = body.get("statusHistory")
    if history is not None:
        if body.get("hasMore") or body.get("nextPageToken") or body.get("next"):
            raise TransferReadError("Wise transfer status history incomplete")
        if not isinstance(history, list) or not history:
            raise TransferReadError("Wise transfer status history incomplete")
        events = []
        for row in history:
            if not isinstance(row, dict) or not isinstance(row.get("status"), str):
                raise TransferReadError("Wise transfer status history malformed")
            occurred = _stamp(row.get("occurred_at"))
            events.append((occurred, row["status"]))
        occurred = max(item[0] for item in events)
        current = {status for stamp, status in events if stamp == occurred}
        if len(current) != 1:
            raise TransferReadError("Wise transfer latest status ambiguous")
        status = current.pop()
    else:
        status = body.get("status")
        if not isinstance(status, str) or not status:
            raise TransferReadError("Wise transfer status missing")
        if "updated" not in body:
            raise TransferReadError("Wise transfer status cannot be ordered without timestamp")
        occurred = _stamp(body["updated"])
    # Unknown values are unresolved, never a paid result.
    return status, occurred.isoformat()


def read_transfer_status(
    token: str, transfer_id: int, transport: Callable[[str, str], Any],
) -> tuple[str, str]:
    if not isinstance(transfer_id, int) or transfer_id <= 0:
        raise TransferReadError("Wise transfer id invalid")
    return latest_status(transport(token, f"/v1/transfers/{transfer_id}"))


def next_check_at(confirmed_at: datetime, now: datetime) -> datetime | None:
    """Next day, daily for three business days, then weekly through day 30."""
    confirmed_at = confirmed_at.astimezone(timezone.utc)
    now = now.astimezone(timezone.utc)
    horizon = confirmed_at + timedelta(days=MONITOR_DAYS)
    if now >= horizon:
        return None
    candidates = [confirmed_at + timedelta(days=1)]
    day = confirmed_at.date()
    business_days = 0
    while business_days < FIRST_BUSINESS_DAYS:
        day += timedelta(days=1)
        if day.weekday() < 5:
            business_days += 1
            candidates.append(datetime.combine(day, confirmed_at.timetz()))
    for days in (7, 14, 21, 28, 30):
        candidates.append(confirmed_at + timedelta(days=days))
    return next(
        (candidate for candidate in sorted(set(candidates)) if now < candidate <= horizon),
        None,
    )


def discrepancy_report(rows: dict[str, dict], transfer_reads: dict[int, Any]) -> list[dict]:
    """Classify legacy done rows from supplied read-only transfer snapshots.

    The caller must supply a complete, documented statement window. This
    function does not read or write the journal or contact Wise.
    """
    report = []
    for obligation, entry in rows.items():
        if not isinstance(entry, dict) or entry.get("state") != "done":
            continue
        transfer_id = entry.get("transfer_id")
        category = "unmatched"
        status = None
        if isinstance(transfer_id, int) and transfer_id in transfer_reads:
            try:
                status, occurred_at = latest_status(transfer_reads[transfer_id])
            except TransferReadError:
                pass
            else:
                relation = compare_observation(
                    status, occurred_at, entry.get("last_status"),
                    entry.get("last_occurred_at"),
                )
                if (entry.get("timestamp_conflict_status") and relation != "later"
                        or relation == "conflict"):
                    category = "unresolved"
                elif relation == "earlier":
                    category = "unresolved"
                elif status == "outgoing_payment_sent":
                    category = "confirmed sent"
                elif status in RETURN_STATUSES:
                    category = "returned"
                else:
                    category = "still pending"
        report.append({"obligation": obligation, "transfer_id": transfer_id,
                       "category": category, "wise_status": status})
    return report


def _report_command() -> int:
    parser = argparse.ArgumentParser(description="Read-only Wise discrepancy report")
    parser.add_argument("command", choices=["report"])
    parser.add_argument("--journal", type=Path, required=True)
    parser.add_argument("--transfer-snapshots", type=Path, required=True)
    args = parser.parse_args()
    # Deliberately avoid payment_journal.load: it may create the first-use marker.
    journal = json.loads(args.journal.read_text())
    import payment_journal
    problems = payment_journal.validate_journal_data(journal)
    if problems:
        parser.error("journal is malformed: " + ", ".join(problems))
    source = json.loads(args.transfer_snapshots.read_text())
    if not isinstance(source, dict) or not isinstance(source.get("transfers"), dict):
        parser.error("transfer snapshots are malformed")
    reads = {int(key): value for key, value in source["transfers"].items()}
    report = discrepancy_report(journal["obligations"], reads)
    print(json.dumps({"coverage": source.get("coverage"), "rows": report}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_report_command())
