"""Durable, obligation-keyed payment journal for Monedula email consent.

Implements the ordering in ``docs/foundation/payments.md#the-dedup-key-and-what-it-is-keyed-on``
and ``#the-unknown-case-a-transfer-submitted-whose-confirmation-never-returned``:

1. **Record intent** — an ``intent`` entry keyed on the obligation (handler,
   period/instance, payee, amount, currency) is written and fsynced BEFORE the
   transfer is attempted. The approval token that authorized it is recorded as
   consumed in the same write.
2. **Execute.**
3. **Record outcome** — the entry moves to ``done`` with the handler's status.

An obligation with ``intent`` and no outcome is the **unknown** case: the
transfer may or may not have landed. It is never retried automatically; the
daemon holds it and escalates to the operator.

The journal is also the source of the monotonically increasing consent
**request generation**: every new consent request takes a generation number
that was never issued before, so an approval given to an earlier request can
never match a later one, even if the consent-request mark is lost.

This file is never cleared by the consent flow (unlike the consent-request
mark). It stores only hashed obligation keys, short approval tokens,
timestamps and status strings — no payee names, account numbers or amounts.

Fail closed (``docs/foundation/principles.md#5``): an unreadable or malformed
journal raises :class:`JournalError`; callers hold every payment rather than
treat the journal as empty. The adjacent first-use marker is durably written
before the first journal write. If either file is absent on a fresh host,
payments remain held. After reconciling against the chain and wallet, the
operator explicitly runs ``python3 execution/daemons/monedula/payment_journal.py
init`` to create both files. A valid legacy journal without a marker remains
readable and gains the marker on read. Restoring an older backup requires
operator reconciliation, never automatic recovery.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import stat
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

JOURNAL_NAME = ".monedula_payment_journal.json"
MARKER_NAME = ".monedula_payment_journal.initialized"
MISSING_AFTER_FIRST_USE = (
    "payment journal missing after first use; reconcile before any payment"
)
NOT_INITIALIZED = "payment journal not initialized; reconcile and initialize before any payment"
INIT_REFUSED = "payment journal initialization refused; journal or marker already exists"

STATE_INTENT = "intent"
STATE_FUNDING_PENDING = "funding_pending"
STATE_DONE = "done"
STATE_FAILED = "failed"
REPLACEMENT_HOLD_MESSAGE = (
    "The original attempt remains held. A replacement route is not available yet; "
    "approval alone will not advance this payment. Do not pay again."
)


class JournalError(Exception):
    """The journal could not be read or durably written. Hold all payments."""


def _empty() -> dict[str, Any]:
    return {
        "version": 1,
        "generation": 0,
        "obligations": {},
        "consumed_tokens": {},
        "sessions": {},
    }


def load(path: Path) -> dict[str, Any]:
    """Return the journal; absence is never interpreted as a fresh start.

    A journal with no marker is a legacy deployment: create the marker when
    possible, but do not fail a valid read if that write fails. Every journal
    write must establish the marker durably first.
    """
    marker = path.with_name(MARKER_NAME)
    try:
        path.lstat()
    except FileNotFoundError:
        if _marker_present(marker):
            raise JournalError(MISSING_AFTER_FIRST_USE)
        raise JournalError(NOT_INITIALIZED)
    except OSError as exc:
        raise JournalError(f"payment journal unreadable: {type(exc).__name__}") from exc
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise JournalError(f"payment journal unreadable: {type(exc).__name__}") from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("generation"), int)
        or not isinstance(data.get("obligations"), dict)
        or not isinstance(data.get("consumed_tokens"), dict)
    ):
        raise JournalError("payment journal malformed")
    # ``sessions`` was added after the first journals were written: absent is
    # an empty map, present-but-wrong-shape is malformed (fail closed).
    if "sessions" not in data:
        data["sessions"] = {}
    elif not isinstance(data["sessions"], dict):
        raise JournalError("payment journal malformed")
    for obligation, entry in data["obligations"].items():
        if not isinstance(obligation, str) or not isinstance(entry, dict):
            raise JournalError("payment journal obligation malformed")
        if entry.get("state") not in (STATE_INTENT, STATE_FUNDING_PENDING, STATE_DONE, STATE_FAILED):
            raise JournalError("payment journal obligation state malformed")
        if entry.get("rail") == "wise":
            if entry.get("handler_name") is not None and not isinstance(
                entry["handler_name"], str
            ):
                raise JournalError("payment journal Wise handler name malformed")
            if any(field in entry and not isinstance(entry[field], str) for field in (
                "task_id", "approved_terms_hash", "payment_date", "match_id"
            )):
                raise JournalError("payment journal Wise approval binding malformed")
            legacy_wise = (
                "customer_transaction_id" not in entry
                and "attempt" not in entry
                and "last_status" not in entry
                and "last_occurred_at" not in entry
            )
            if not legacy_wise and (
                not isinstance(entry.get("customer_transaction_id"), str)
                or not isinstance(entry.get("attempt"), int)
                or entry["attempt"] < 1
                or not isinstance(entry.get("token"), str)
                or data["consumed_tokens"].get(entry["token"]) != obligation
            ):
                raise JournalError("payment journal Wise attempt malformed")
            attempts = entry.get("attempts", [])
            if not isinstance(attempts, list) or any(
                not isinstance(prior, dict)
                or prior.get("failure_status") not in ("funds_refunded", "cancelled", "not_submitted")
                or (prior.get("failure_status") != "not_submitted"
                    and not isinstance(prior.get("transfer_id"), int))
                or (prior.get("failure_status") == "not_submitted"
                    and prior.get("transfer_id") is not None)
                for prior in attempts
            ):
                raise JournalError("payment journal Wise attempt history malformed")
            transfer_id = entry.get("transfer_id")
            if not legacy_wise and entry["state"] in (STATE_FUNDING_PENDING, STATE_DONE, STATE_FAILED) and not (
                entry["state"] == STATE_FAILED and entry.get("failure_status") == "not_submitted"
            ) and (
                not isinstance(transfer_id, int) or isinstance(transfer_id, bool)
                or transfer_id <= 0
            ):
                raise JournalError("payment journal Wise transfer id missing")
    try:
        _ensure_marker(marker)
    except JournalError:
        # A valid legacy journal remains readable. Its next write must
        # establish the marker before changing any payment state.
        pass
    return data


def _marker_present(path: Path) -> bool:
    """Count even an unreadable file or broken link as an existing marker."""
    try:
        path.lstat()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise JournalError(f"payment journal marker unreadable: {type(exc).__name__}") from exc


def _ensure_marker(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        _atomic_write(path, {"version": 1, "created_at": time.time()})
        return
    except OSError as exc:
        raise JournalError(f"payment journal marker unreadable: {type(exc).__name__}") from exc
    if not stat.S_ISREG(mode):
        raise JournalError("payment journal marker is not a regular file")


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Atomic, fsynced replace for the journal and its first-use marker."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".journal-", dir=str(path.parent))
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(json.dumps(data, indent=2, sort_keys=True))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError as exc:
        raise JournalError(
            f"payment journal write failed: {type(exc).__name__}"
        ) from exc


def _atomic_create(path: Path, data: dict[str, Any]) -> None:
    """Create an fsynced file atomically, refusing an existing destination."""
    tmp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".journal-", dir=str(path.parent))
        with os.fdopen(fd, "w") as fh:
            fh.write(json.dumps(data, indent=2, sort_keys=True))
            fh.flush()
            os.fsync(fh.fileno())
        # A hard link publishes the complete file without replacing a file
        # another process may have created since the initial existence check.
        os.link(tmp, path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except FileExistsError as exc:
        raise JournalError(INIT_REFUSED) from exc
    except OSError as exc:
        raise JournalError(f"payment journal initialization failed: {type(exc).__name__}") from exc
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def init_journal(path: Path) -> None:
    """Explicit first-use bootstrap; partial failure leaves a restrictive hold."""
    marker = path.with_name(MARKER_NAME)
    if _marker_present(path) or _marker_present(marker):
        raise JournalError(INIT_REFUSED)
    _atomic_create(marker, {"version": 1, "created_at": time.time()})
    _atomic_create(path, _empty())
    if load(path) != _empty():
        raise JournalError("payment journal initialization did not persist")


def _main() -> int:
    parser = argparse.ArgumentParser(description="Bootstrap the Monedula payment journal")
    parser.add_argument("command", choices=["init"])
    parser.add_argument("--path", type=Path, default=Path(__file__).parent / JOURNAL_NAME)
    args = parser.parse_args()
    try:
        init_journal(args.path)
    except JournalError as exc:
        parser.exit(1, f"{exc}\n")
    print("payment journal initialized; verify the journal and marker before the next tick")
    return 0


def _write(path: Path, data: dict[str, Any]) -> None:
    """Establish durable first-use evidence before any journal write."""
    _ensure_marker(path.with_name(MARKER_NAME))
    _atomic_write(path, data)


@contextlib.contextmanager
def single_writer(path: Path):
    """Serialize daemon ticks on the existing stable first-use marker inode."""
    load(path)  # also establishes the marker for a valid legacy journal
    marker = path.with_name(MARKER_NAME)
    try:
        fd = os.open(marker, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if "fd" in locals():
            os.close(fd)
        raise JournalError(f"payment journal writer unavailable: {type(exc).__name__}") from exc
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def current_generation(path: Path) -> int:
    return int(load(path)["generation"])


def next_generation(path: Path) -> int:
    """Issue a never-before-used request generation, durably."""
    data = load(path)
    data["generation"] = int(data["generation"]) + 1
    _write(path, data)
    if current_generation(path) != data["generation"]:
        raise JournalError("payment journal generation did not persist")
    return int(data["generation"])


def obligation_state(data: dict[str, Any], obligation: str) -> str | None:
    """Return the durable state; an invalid entry always holds or raises."""
    entry = data["obligations"].get(obligation)
    if entry is None:
        return None
    if not isinstance(entry, dict):
        raise JournalError("payment journal obligation malformed")
    state = entry.get("state")
    if state not in (STATE_INTENT, STATE_FUNDING_PENDING, STATE_DONE, STATE_FAILED):
        raise JournalError("payment journal obligation state malformed")
    if state == STATE_DONE and entry.get("rail") == "wise" and not _wise_paid(entry, data):
        return STATE_FAILED  # historical settlement cannot clear a later return
    return state


def _wise_paid(entry: dict[str, Any], data: dict[str, Any] | None = None) -> bool:
    """Only a consistent, fully applied and session-settled attempt releases its profile."""
    if "last_status" not in entry and "last_occurred_at" not in entry:
        return entry.get("state") == STATE_DONE and entry.get("outcome_status") == "sent"
    return (
        entry.get("state") == STATE_DONE
        and entry.get("outcome_status") == "sent"
        and entry.get("last_status") == "outgoing_payment_sent"
        and isinstance(entry.get("last_occurred_at"), str)
        and bool(entry["last_occurred_at"])
        and entry.get("discrepancy_status") is None
        and entry.get("effects_applied") is True
        and (not entry.get("session_key") or
             data is not None and session_state(data, entry["session_key"]) == SESSION_SETTLED)
    )


def wise_replay_hold(
    data: dict[str, Any], profile_entity_id: str, *, exclude_obligation: str = ""
) -> tuple[str, dict[str, Any]] | None:
    """Hold a Wise offer on any unresolved attempt for its profile or an unclassified row.

    The key is returned for the operator message. No terms, calendar identity,
    handler name, or current obligation hash participates in this decision.
    """
    profile_hold = None
    for key, entry in data["obligations"].items():
        if key == exclude_obligation or entry.get("rail") == "btc":
            continue
        if _wise_paid(entry, data):
            continue
        if entry.get("rail") not in (None, "wise"):
            return key, entry
        row_profile = entry.get("profile_entity_id")
        if not isinstance(row_profile, str) or not row_profile.strip():
            return key, entry
        if profile_hold is None and (not profile_entity_id or row_profile == profile_entity_id):
            profile_hold = key, entry
    return profile_hold


def token_consumed(data: dict[str, Any], token: str) -> bool:
    return token in data["consumed_tokens"]


def record_intent(
    path: Path, obligation: str, token: str, *, generation: int, rail: str | None = None,
    handler_name: str | None = None, session_key: str | None = None,
    profile_entity_id: str = "", task_id: str = "", approved_terms_hash: str = "",
    payment_date: str = "", match_id: str = "",
) -> None:
    """Durably record intent-to-pay BEFORE the transfer, and read it back.

    Refuses (``JournalError``) if the obligation already has an entry or the
    token was already consumed — the caller must not execute in that case.
    """
    data = load(path)
    if obligation in data["obligations"]:
        raise JournalError("obligation already recorded — refusing a second attempt")
    if token in data["consumed_tokens"]:
        raise JournalError("approval token already consumed")
    if rail == "wise" and (not isinstance(profile_entity_id, str) or not profile_entity_id.strip()):
        raise JournalError("Wise profile ID required before intent")
    if rail == "wise":
        held = wise_replay_hold(data, profile_entity_id)
        if held is not None:
            raise JournalError(f"Wise attempt {held[0]} holds this profile; check Wise Activity")
    entry = {
        "state": STATE_INTENT,
        "token": token,
        "generation": int(generation),
        "intent_at": time.time(),
    }
    if rail == "wise":
        entry.update(
            rail="wise",
            attempt=1,
            customer_transaction_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"wise:{obligation}:1")),
            handler_name=handler_name,
            session_key=session_key,
            profile_entity_id=profile_entity_id,
            task_id=task_id,
            approved_terms_hash=approved_terms_hash,
            payment_date=payment_date,
            match_id=match_id,
        )
    data["obligations"][obligation] = entry
    data["consumed_tokens"][token] = obligation
    _write(path, data)
    back = load(path)
    if obligation_state(back, obligation) != STATE_INTENT or not token_consumed(
        back, token
    ):
        raise JournalError("payment intent did not persist")
    if rail == "wise" and any(
        back["obligations"][obligation].get(field) != entry[field]
        for field in ("token", "generation", "attempt", "customer_transaction_id",
                      "profile_entity_id", "task_id", "approved_terms_hash",
                      "payment_date", "match_id")
    ):
        raise JournalError("Wise attempt identity did not persist")


def record_transfer_created(path: Path, obligation: str, transfer_id: int) -> None:
    """Persist the Wise transfer id before the funding POST can begin."""
    data = load(path)
    entry = data["obligations"].get(obligation)
    if (
        not isinstance(entry, dict)
        or entry.get("rail") != "wise"
        or entry.get("state") != STATE_INTENT
        or entry.get("transfer_id") is not None
        or not isinstance(transfer_id, int)
        or isinstance(transfer_id, bool)
        or transfer_id <= 0
    ):
        raise JournalError("Wise transfer id cannot be recorded")
    entry["transfer_id"] = transfer_id
    _write(path, data)
    back = load(path)["obligations"][obligation]
    if any(back.get(field) != entry[field] for field in (
        "state", "token", "generation", "attempt", "customer_transaction_id", "transfer_id"
    )):
        raise JournalError("Wise transfer id did not persist")


def record_wise_not_submitted(path: Path, obligation: str) -> None:
    """Close an attempt only when transfer creation was never entered."""
    data = load(path)
    entry = data["obligations"].get(obligation)
    if (not isinstance(entry, dict) or entry.get("rail") != "wise"
            or entry.get("state") != STATE_INTENT or entry.get("transfer_id") is not None
            ):
        raise JournalError("Wise not-submitted transition refused")
    entry["state"] = STATE_FAILED
    entry["outcome_status"] = "failed"
    entry["failure_status"] = "not_submitted"
    _write(path, data)
    back = load(path)["obligations"][obligation]
    if (back.get("state"), back.get("failure_status")) != (STATE_FAILED, "not_submitted"):
        raise JournalError("Wise not-submitted transition did not persist")


def record_funding_pending(
    path: Path, obligation: str, transfer_id: int, funding_status: str
) -> None:
    """Record submission without claiming that the transfer was paid."""
    data = load(path)
    entry = data["obligations"].get(obligation)
    if (
        not isinstance(entry, dict)
        or entry.get("rail") != "wise"
        or entry.get("state") != STATE_INTENT
        or entry.get("transfer_id") != transfer_id
        or funding_status not in ("CREATED", "COMPLETED", "PROCESSING", "PENDING")
    ):
        raise JournalError("Wise funding pending transition refused")
    entry["state"] = STATE_FUNDING_PENDING
    entry["funding_status"] = funding_status
    entry["funding_at"] = time.time()
    _write(path, data)
    back = load(path)["obligations"][obligation]
    if any(back.get(field) != entry[field] for field in (
        "state", "token", "generation", "attempt", "customer_transaction_id",
        "transfer_id", "funding_status",
    )):
        raise JournalError("Wise funding pending did not persist")


def record_wise_status(
    path: Path, obligation: str, transfer_id: int, status: str, occurred_at: str
) -> bool:
    """Append a checked transfer observation; return True on first paid confirmation."""
    data = load(path)
    entry = data["obligations"].get(obligation)
    if (
        not isinstance(entry, dict)
        or entry.get("rail") != "wise"
        or entry.get("transfer_id") != transfer_id
        or entry.get("state") not in (STATE_INTENT, STATE_FUNDING_PENDING, STATE_DONE, STATE_FAILED)
        or not occurred_at
    ):
        raise JournalError("Wise status transition refused")
    from wise_reconcile import _stamp
    current_stamp = _stamp(occurred_at)
    previous = entry.get("last_occurred_at")
    if previous and current_stamp < _stamp(previous):
        return False  # an older observation must never roll current state back
    first_paid = status == "outgoing_payment_sent" and entry.get("outcome_status") != "sent"
    entry["last_status"] = status[:64]
    entry["last_occurred_at"] = occurred_at
    entry["last_checked_at"] = time.time()
    if first_paid:
        entry["state"] = STATE_DONE
        entry["outcome_status"] = "sent"
        entry["outcome_at"] = time.time()
        entry.pop("discrepancy_status", None)
    elif status in ("funds_refunded", "cancelled") and entry.get("outcome_status") != "sent":
        entry["state"] = STATE_FAILED
        entry["outcome_status"] = "failed"
        entry["failure_status"] = status
    elif entry.get("state") == STATE_DONE and status != "outgoing_payment_sent":
        entry["discrepancy_status"] = status[:64]
    elif entry.get("state") == STATE_DONE:
        entry.pop("discrepancy_status", None)
    elif entry.get("state") == STATE_INTENT:
        entry["state"] = STATE_FUNDING_PENDING
    _write(path, data)
    back = load(path)["obligations"][obligation]
    if (back.get("last_status"), back.get("last_occurred_at")) != (status[:64], occurred_at):
        raise JournalError("Wise status did not persist")
    if first_paid and back.get("outcome_status") != "sent":
        raise JournalError("Wise settlement did not persist")
    return first_paid


def mark_wise_effects_applied(path: Path, obligation: str, transfer_id: int) -> None:
    """Read back the marker after task and session effects were confirmed."""
    data = load(path)
    entry = data["obligations"].get(obligation)
    if (
        not isinstance(entry, dict)
        or entry.get("rail") != "wise"
        or entry.get("transfer_id") != transfer_id
        or entry.get("outcome_status") != "sent"
        or entry.get("last_status") != "outgoing_payment_sent"
    ):
        raise JournalError("Wise task effects cannot be marked before settlement")
    entry["effects_applied"] = True
    _write(path, data)
    if load(path)["obligations"][obligation].get("effects_applied") is not True:
        raise JournalError("Wise task effects marker did not persist")


def schedule_wise_check(path: Path, obligation: str, due_at: float | None) -> None:
    data = load(path)
    entry = data["obligations"].get(obligation)
    if not isinstance(entry, dict) or entry.get("rail") != "wise":
        raise JournalError("Wise check schedule has no attempt")
    entry["next_check_at"] = due_at
    _write(path, data)
    if load(path)["obligations"][obligation].get("next_check_at") != due_at:
        raise JournalError("Wise check schedule did not persist")


def record_approved_replacement_intent(
    path: Path, obligation: str, token: str, *, generation: int
) -> None:
    """Reserve a numbered replacement after the caller verifies fresh approval.

    This does not authorize or execute a transfer. A consent adapter must
    authenticate the new operator answer before calling it; no automatic
    path calls this function.
    """
    data = load(path)
    entry = data["obligations"].get(obligation)
    if (
        not isinstance(entry, dict)
        or entry.get("rail") != "wise"
        or entry.get("state") != STATE_FAILED
        or entry.get("failure_status") not in ("funds_refunded", "cancelled", "not_submitted")
        or token in data["consumed_tokens"]
        or token == entry.get("token")
        or generation <= entry.get("generation", 0)
    ):
        raise JournalError("Wise replacement requires terminal failure or no submission and fresh approval")
    attempt = entry.get("attempt")
    if not isinstance(attempt, int) or attempt < 1:
        raise JournalError("Wise attempt number malformed")
    previous = {key: value for key, value in entry.items() if key != "attempts"}
    attempts = entry.get("attempts", [])
    if not isinstance(attempts, list):
        raise JournalError("Wise attempt history malformed")
    attempts.append(previous)
    entry.clear()
    entry.update(
        state=STATE_INTENT, rail="wise", token=token,
        generation=generation, intent_at=time.time(), attempt=attempt + 1,
        customer_transaction_id=str(uuid.uuid5(
            uuid.NAMESPACE_URL, f"wise:{obligation}:{attempt + 1}"
        )),
        handler_name=previous.get("handler_name"),
        session_key=previous.get("session_key"),
        profile_entity_id=previous.get("profile_entity_id", ""),
        task_id=previous.get("task_id", ""),
        approved_terms_hash=previous.get("approved_terms_hash", ""),
        payment_date=previous.get("payment_date", ""),
        match_id=previous.get("match_id", ""),
        attempts=attempts,
    )
    data["consumed_tokens"][token] = obligation
    _write(path, data)
    back = load(path)["obligations"][obligation]
    if (
        back.get("state") != STATE_INTENT
        or back.get("attempt") != attempt + 1
        or back.get("token") != token
        or len(back.get("attempts", [])) != len(attempts)
        or not token_consumed(load(path), token)
    ):
        raise JournalError("Wise replacement intent did not persist")


def record_outcome(path: Path, obligation: str, status: str) -> None:
    """Record the transfer's outcome after it returns, and read it back."""
    data = load(path)
    entry = data["obligations"].get(obligation)
    if not isinstance(entry, dict):
        raise JournalError("outcome for an obligation with no recorded intent")
    if entry.get("rail") == "wise":
        raise JournalError("Wise outcome requires a confirmed transfer read")
    entry["state"] = STATE_DONE if status == "sent" else STATE_FAILED
    entry["outcome_status"] = str(status)[:64]
    entry["outcome_at"] = time.time()
    _write(path, data)
    if obligation_state(load(path), obligation) != entry["state"]:
        raise JournalError("payment outcome did not persist")


# ---------------------------------------------------------------------------
# Raised sessions (one consent request per calendar session)
# ---------------------------------------------------------------------------
#
# A calendar session is raised for consent once, keyed on (handler, event id).
# The entry persists until the session is resolved, so a tick that cannot see
# the session (Neotoma timeout, event out of the lookback window, restart,
# midnight) neither loses it nor raises it a second time. Only the handler
# name, event id and the event's end time (epoch seconds, used to size the
# next calendar lookback) are stored: no titles, payees or amounts.

SESSION_PENDING = "pending"
# Terminal states: the session is never raised again.
SESSION_SETTLED = "settled"  # paid, or already recorded as paid
SESSION_SKIPPED = "skipped"  # explicit operator SKIP
SESSION_DROPPED = "dropped"  # event or profile no longer exists
SESSION_TERMINAL = frozenset({SESSION_SETTLED, SESSION_SKIPPED, SESSION_DROPPED})


def session_key(handler_name: str, event_id: str) -> str:
    return f"{handler_name}:{event_id}"


def session_state(data: dict[str, Any], key: str) -> str | None:
    entry = data["sessions"].get(key)
    if not isinstance(entry, dict):
        return None
    return str(entry.get("state") or SESSION_PENDING)


def pending_sessions(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        k: v
        for k, v in data["sessions"].items()
        if isinstance(v, dict) and (v.get("state") or SESSION_PENDING) == SESSION_PENDING
    }


def note_pending(path: Path, key: str, *, ended_at: float | None = None) -> None:
    """Record a session as raised-and-unresolved. Idempotent; never reopens a
    terminal session. Read back after writing."""
    data = load(path)
    if isinstance(data["sessions"].get(key), dict):
        return  # already pending or terminal: raise-once
    data["sessions"][key] = {
        "state": SESSION_PENDING,
        "first_seen": time.time(),
        "ended_at": ended_at,
    }
    _write(path, data)
    if session_state(load(path), key) != SESSION_PENDING:
        raise JournalError("pending session did not persist")


def set_session_state(path: Path, key: str, state: str) -> None:
    """Move a session to a new state and read it back."""
    data = load(path)
    entry = data["sessions"].get(key)
    if not isinstance(entry, dict):
        entry = {"first_seen": time.time(), "ended_at": None}
    entry["state"] = state
    entry["resolved_at"] = time.time()
    data["sessions"][key] = entry
    _write(path, data)
    if session_state(load(path), key) != state:
        raise JournalError("session state did not persist")


if __name__ == "__main__":
    raise SystemExit(_main())
