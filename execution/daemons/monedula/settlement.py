"""
settlement.py — Resolve the final state of Monedula transfers left in flight.

A Wise transfer is funded in one call and delivered minutes to days later.
`_execute_wise_transfer()` therefore cannot know, at execute time, whether the
payment arrived; when it does not, it returns `awaiting_settlement` and parks
the profile (see ateles#575).

This module is the consumer of that state. Without it `awaiting_settlement`
would be a state nothing transitions out of — a task stuck forever, which is
worse than the bug it replaces. The sweep runs on every daemon tick, re-reads
each parked transfer from Wise, and drives one of three exits:

  settled  → note the settlement, close a one-off task / re-arm a recurring one
  failed   → note the failure, park the profile as payment_failed, leave the
             task OPEN, and surface it to the operator
  in flight→ write nothing; report it as suspect once it has waited too long

The exits are OBSERVED, not asserted: the sweep believes Wise's own transfer
record, never the daemon's memory of what it submitted. An observed exit fails
visibly — the transfer sits in the digest as suspect until someone looks — where
a self-asserted one fails invisibly, which is exactly how ateles#552 and #575
stayed hidden. (Same principle as ateles#581's `awaiting_merge`.)

The sweep is GET-only against Wise. It must never POST: a POST from here is a
second payment.
"""

from __future__ import annotations

import logging
import shutil
from datetime import date

from handlers.payment_profile import (
    PROFILE_STATUS_ACTIVE,
    PROFILE_STATUS_PAYMENT_FAILED,
    PaymentProfile,
    _valid_transfer_id,
    load_profiles_awaiting_settlement,
)
from handlers.wise_transfer import (
    classify_transfer_state,
    close_one_off,
    escalate,
    fetch_transfer,
    find_next_event_due_date,
    find_task_id,
    note_task,
    set_entity_field,
    settlement_alert_days,
)

log = logging.getLogger(__name__)


def _age_days(pending_at: str, today: date) -> int | None:
    """Days since the transfer was submitted, or None if that is unknowable.

    None is not zero. An unparseable or future submission date means the wait
    cannot be measured, and a wait that cannot be measured must surface rather
    than quietly read as "submitted just now" and never age into suspicion.
    """
    raw = (pending_at or "").strip()
    if not raw:
        return None
    try:
        submitted = date.fromisoformat(raw)
    except ValueError:
        return None
    age = (today - submitted).days
    return age if age >= 0 else None


def _resolve_one(
    profile: PaymentProfile,
    token: str,
    neotoma: str,
    today: date,
) -> dict:
    """Resolve one parked profile against Wise. Returns a digest record."""
    transfer_id = _valid_transfer_id(profile.pending_transfer_id)
    age = _age_days(profile.pending_transfer_at, today)
    record: dict = {
        "profile": profile.label,
        "entity_id": profile.entity_id,
        "transfer_id": profile.pending_transfer_id,
        "outcome": "in_flight",
        "wise_status": "",
        "age_days": age,
    }

    # load_profiles_awaiting_settlement() already drops these, so reaching here
    # means the guard was bypassed. Report, do not call Wise with a bad id.
    if transfer_id is None:
        record["outcome"] = "suspect"
        record["wise_status"] = "unresolvable transfer id"
        return record

    transfer = fetch_transfer(token, transfer_id)
    state = classify_transfer_state(transfer)
    record["wise_status"] = str((transfer or {}).get("status", "") or "")

    if state == "settled":
        record["outcome"] = "settled"
        _on_settled(profile, transfer_id, record["wise_status"], neotoma, today)
        return record

    if state == "failed":
        # Two consecutive observations of the SAME failed status before any
        # terminal outcome is written — the guard the module docstring and
        # WISE_TRANSFER_FAILED both promise, which was documented but never
        # implemented (ateles#604 review, found independently by two lenses).
        # The operator's ledger has bounced_back appearing ~20s after funding
        # and resolving back to processing on the next poll, so acting on one
        # read declares a healthy transfer dead, writes payment_failed, and
        # clears pending_transfer_id — after which nothing re-reads the
        # transfer and the error is permanent.
        seen = record["wise_status"]
        if (profile.pending_failed_status or "") != seen:
            set_entity_field(
                neotoma, profile.entity_id, "pending_failed_status", seen
            )
            record["outcome"] = "failed_pending_confirmation"
            log.warning(
                "[monedula] %s: Wise reports %s for transfer %s — holding for a "
                "second confirming read before recording a terminal failure",
                profile.label,
                seen,
                transfer_id,
            )
            return record

        record["outcome"] = "failed"
        _on_failed(profile, transfer_id, record["wise_status"], neotoma, today)
        return record

    # A transfer that came back from a failed status clears the latch: the
    # first observation is only evidence while it is still the current state.
    # Without this a bounce months ago would silently pre-confirm an unrelated
    # failure later, which is the same one-read verdict by another route.
    if profile.pending_failed_status:
        set_entity_field(neotoma, profile.entity_id, "pending_failed_status", "")

    # in_flight or unreadable — write nothing at all. The transfer is still on
    # its way (or Wise could not be read), and either way there is nothing yet
    # to record. Age it into suspicion so a wait that never ends is visible.
    threshold = settlement_alert_days()
    if age is None or age >= threshold:
        record["outcome"] = "suspect"
    return record


def _on_settled(
    profile: PaymentProfile,
    transfer_id: int,
    wise_status: str,
    neotoma: str,
    today: date,
) -> None:
    """Wise confirms delivery: this is the only path that may close a task."""
    task_id = find_task_id(profile)
    note_task(
        profile,
        task_id,
        neotoma,
        f"Payment settled {today.isoformat()}: Wise transfer_id={transfer_id} "
        f"wise_transfer_status={wise_status}",
    )

    if profile.one_off:
        # Closing here, not at execute time, is the whole point of ateles#575:
        # the task is marked done against an observed delivery.
        close_one_off(profile, task_id, neotoma)
        return

    # A recurring profile returns to service. Clearing pending_transfer_id
    # matters as much as restoring active — a stale id would make the next
    # preview warn about an in-flight transfer that has already landed.
    set_entity_field(neotoma, profile.entity_id, "status", PROFILE_STATUS_ACTIVE)
    set_entity_field(neotoma, profile.entity_id, "pending_transfer_id", "")
    set_entity_field(neotoma, profile.entity_id, "pending_transfer_at", "")

    # The due_date roll deferred at execute time. Do it here, once, now that
    # the payment it accounts for is known to have happened.
    next_due = find_next_event_due_date(profile)
    if next_due and task_id:
        _set_due_date(profile, task_id, neotoma, next_due)
    elif not next_due:
        log.warning(
            f"[{profile.name}] settled, but no next event found — due_date not rolled."
        )


def _on_failed(
    profile: PaymentProfile,
    transfer_id: int,
    wise_status: str,
    neotoma: str,
    today: date,
) -> None:
    """Wise reports the transfer dead: leave the task open, do not re-arm.

    The profile goes to payment_failed rather than back to active. Returning it
    to active would re-arm the payment automatically, and a failed transfer
    whose money may or may not have been returned is exactly the case where an
    automatic retry is a double payment. Re-payment stays an operator decision.
    """
    task_id = find_task_id(profile)
    note_task(
        profile,
        task_id,
        neotoma,
        f"Payment FAILED {today.isoformat()}: Wise transfer_id={transfer_id} "
        f"wise_transfer_status={wise_status} — task left open, payment not "
        f"re-armed; operator decision required",
    )
    set_entity_field(
        neotoma, profile.entity_id, "status", PROFILE_STATUS_PAYMENT_FAILED
    )
    set_entity_field(neotoma, profile.entity_id, "pending_transfer_id", "")
    log.error(
        f"[{profile.name}] Wise transfer {transfer_id} FAILED "
        f"({wise_status}) — task left open for the operator."
    )


def _set_due_date(
    profile: PaymentProfile, task_id: str, neotoma: str, next_due: str
) -> None:
    if not task_id:
        log.warning(f"[{profile.name}] cannot roll due_date: no task id")
        return
    if set_entity_field(neotoma, task_id, "due_date", next_due):
        log.info(f"[{profile.name}] Neotoma task due_date set to {next_due}.")
        return
    log.error(
        f"[{profile.name}] SETTLEMENT DUE_DATE FAILED: task {task_id} still shows "
        f"the pre-payment due_date after transfer settled — roll it by hand."
    )
    escalate(
        f"monedula: {profile.label} transfer settled but due_date could NOT be "
        f"rolled to {next_due} on task {task_id} — the record under-claims."
    )


def sweep_pending_settlements(token: str, *, today: date | None = None) -> list[dict]:
    """Resolve every profile parked awaiting settlement. Never raises.

    A failure on one profile must not abandon the others: each is resolved
    independently and a raise is logged and skipped, because the profiles left
    unswept would be transfers nobody is watching.
    """
    today = today or date.today()

    neotoma = shutil.which("neotoma")
    if not neotoma:
        log.warning("neotoma CLI not found — cannot resolve pending settlements")
        return []

    try:
        parked = load_profiles_awaiting_settlement()
    except Exception as exc:
        log.error(f"Could not load profiles awaiting settlement: {exc}")
        return []

    if not parked:
        return []

    log.info(f"Resolving {len(parked)} transfer(s) awaiting settlement…")
    records: list[dict] = []
    for profile in parked:
        try:
            records.append(_resolve_one(profile, token, neotoma, today))
        except Exception as exc:
            log.error(f"[{profile.name}] settlement resolution failed: {exc}")
            records.append(
                {
                    "profile": profile.label,
                    "entity_id": profile.entity_id,
                    "transfer_id": profile.pending_transfer_id,
                    "outcome": "suspect",
                    "wise_status": f"resolution error: {type(exc).__name__}",
                    "age_days": _age_days(profile.pending_transfer_at, today),
                }
            )
    return records


def format_settlement_lines(records: list[dict]) -> list[str]:
    """Telegram lines for the preview digest.

    A plain in-flight transfer under the alert threshold is omitted: it is
    working as intended, and reporting it every tick would train the operator
    to skim past the block that also carries the failures.
    """
    lines: list[str] = []
    for rec in records:
        outcome = rec.get("outcome")
        label = rec.get("profile", "payment")
        transfer_id = rec.get("transfer_id", "?")
        wise_status = rec.get("wise_status") or "unknown"
        age = rec.get("age_days")
        age_text = f"{age}d" if isinstance(age, int) else "age unknown"

        if outcome == "settled":
            lines.append(
                f"  ✅ {label}: transfer {transfer_id} settled ({wise_status})"
            )
        elif outcome == "failed":
            lines.append(
                f"  ❌ {label}: transfer {transfer_id} FAILED ({wise_status}) — "
                f"task left open, payment NOT re-armed"
            )
        elif outcome == "suspect":
            lines.append(
                f"  ⚠️ {label}: transfer {transfer_id} unsettled after {age_text} "
                f"(wise: {wise_status}) — check this one"
            )
    return lines
