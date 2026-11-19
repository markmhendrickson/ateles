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
import logging
import math
import os
import re
import stat
import tempfile
import time
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

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


def validate_journal_data(data: object) -> list[str]:
    """Return value-free problem categories without changing the journal data."""
    if not isinstance(data, dict):
        return ["top-level shape"]
    problems = []
    if type(data.get("generation")) is not int or data["generation"] < 0:
        problems.append("generation")
    if not isinstance(data.get("obligations"), dict):
        problems.append("obligations map")
    if not isinstance(data.get("consumed_tokens"), dict):
        problems.append("consumed-token map")
    if "sessions" in data and not isinstance(data["sessions"], dict):
        problems.append("sessions map")
    if problems:
        return problems
    obligations = data["obligations"]
    tokens = data["consumed_tokens"]
    classification_data = data if "sessions" in data else {**data, "sessions": {}}
    for token, obligation in tokens.items():
        if (not _text(token) or not isinstance(obligation, str)
                or obligation not in obligations):
            problems.append("consumed-token reference")
            break
        entry = obligations[obligation]
        if (isinstance(entry, dict)
                and classify_journal_row(entry, classification_data)[0] != "invalid"):
            attempts = entry.get("attempts", [])
            bound_tokens = [entry.get("token")]
            if isinstance(attempts, list):
                bound_tokens.extend(prior.get("token") for prior in attempts
                                    if isinstance(prior, dict))
            if token not in bound_tokens:
                problems.append("consumed-token identity")
                break
    for obligation, entry in obligations.items():
        if not isinstance(obligation, str) or not isinstance(entry, dict):
            problems.append("obligation malformed")
            continue
        if entry.get("state") not in (STATE_INTENT, STATE_FUNDING_PENDING,
                                       STATE_DONE, STATE_FAILED):
            problems.append("obligation state malformed")
            continue
        kind, _ = classify_journal_row(entry, classification_data)
        if (kind.startswith("wise_modern")
                and tokens.get(entry["token"]) != obligation):
            problems.append("Wise consumed-token binding")
    return list(dict.fromkeys(problems))


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
    problems = validate_journal_data(data)
    if problems:
        raise JournalError(f"payment journal malformed: {', '.join(problems)}")
    # A valid legacy journal may have no sessions map.
    data.setdefault("sessions", {})
    for obligation, entry in data["obligations"].items():
        kind, reason = classify_journal_row(entry, data)
        if kind == "invalid":
            log.warning("payment journal attempt %s invalid (%s); held", obligation, reason)
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
    kind, _ = classify_journal_row(entry, data)
    if kind == "invalid" or state == STATE_DONE and entry.get("rail") == "wise" and kind != "wise_modern_resolved":
        return STATE_FAILED  # malformed or incomplete settlement never clears a hold
    return state


_LEGACY_SETTLED_WISE_FIELDS = frozenset({
    "state", "token", "generation", "intent_at", "outcome_status", "outcome_at",
})
_WISE_BASE_FIELDS = frozenset({
    "state", "token", "generation", "intent_at", "rail", "attempt",
    "customer_transaction_id", "handler_name", "session_key", "profile_entity_id",
    "task_id", "approved_terms_hash", "payment_date", "match_id",
})
_WISE_OPTIONAL_FIELDS = frozenset({
    "transfer_id", "create_entered", "funding_status", "funding_at",
    "last_status", "last_occurred_at", "last_checked_at", "outcome_status",
    "outcome_at", "failure_status", "discrepancy_status", "effects_applied",
    "next_check_at", "attempts",
})
_MODERN_SETTLED_WISE_FIELDS = _WISE_BASE_FIELDS | frozenset({
    "transfer_id", "last_status", "last_occurred_at", "last_checked_at",
    "outcome_status", "outcome_at",
})


def _epoch(value: Any) -> bool:
    if type(value) not in (int, float) or value <= 0:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _stamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return stamp if stamp.tzinfo is not None and stamp.utcoffset() is not None else None
    except ValueError:
        return None


def _text(value: Any, *, nullable: bool = False, empty: bool = False) -> bool:
    return (nullable and value is None or isinstance(value, str)
            and len(value) <= 256 and (empty or bool(value.strip())))


def _wise_binding_problem(row: dict[str, Any]) -> str | None:
    """Validate the approval identity shared by writers and row readers."""
    for field in ("handler_name", "profile_entity_id", "task_id",
                  "approved_terms_hash", "match_id"):
        if not _text(row.get(field)):
            return f"{field} malformed"
    payment_date = row.get("payment_date")
    if (not isinstance(payment_date, str)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", payment_date)):
        return "payment_date malformed"
    try:
        date.fromisoformat(payment_date)
    except ValueError:
        return "payment_date malformed"
    return None


def classify_journal_row(
    row: object, data: dict[str, Any] | None = None
) -> tuple[str, str]:
    """Classify one complete writer shape; unknown values and keys always hold.

    Session state is needed to call a session-bound Wise payment resolved.
    The separate BTC branch recognizes only its historical six-field writer
    shape, with an optional explicit rail tag on migrated rows.
    """
    if not isinstance(row, dict):
        return "invalid", "row is not an object"
    from wise_reconcile import TRANSFER_STATUSES
    keys = row.keys()
    common = {"state", "token", "generation", "intent_at"}
    if not common <= keys or not _text(row["token"]) or type(row["generation"]) is not int or row["generation"] < 0 or not _epoch(row["intent_at"]):
        return "invalid", "common fields malformed"
    if "rail" not in row or row.get("rail") == "btc":
        base = common | ({"rail"} if row.get("rail") == "btc" else set())
        allowed = _LEGACY_SETTLED_WISE_FIELDS | ({"rail"} if row.get("rail") == "btc" else set())
        if "rail" not in row and keys == base and row.get("state") == STATE_INTENT:
            return "btc_valid_unresolved", "outcome unknown"
        if keys != allowed or row.get("state") not in (STATE_DONE, STATE_FAILED) or not _text(row.get("outcome_status")) or not _epoch(row.get("outcome_at")):
            return "invalid", "historical row shape or values malformed"
        if (row["state"] == STATE_DONE and row["outcome_status"] != "sent"
                or row["state"] == STATE_FAILED and row["outcome_status"] not in {"failed", "manual_required"}):
            return "invalid", "historical outcome malformed"
        if row.get("rail") == "btc":
            return ("btc_valid_settled" if row["state"] == STATE_DONE and row["outcome_status"] == "sent" else "btc_valid_unresolved"), ""
        if row["state"] == STATE_DONE and row["outcome_status"] == "sent":
            return "legacy_settled", ""
        # Untagged old BTC failures retain the historical all-profile hold.
        return "btc_valid_unresolved", "untagged historical non-settlement"
    if row.get("rail") != "wise" or not _WISE_BASE_FIELDS <= keys or not keys <= _WISE_BASE_FIELDS | _WISE_OPTIONAL_FIELDS:
        return "invalid", "Wise field set or rail malformed"
    if not _text(row["session_key"], nullable=True):
        return "invalid", "session_key malformed"
    binding_problem = _wise_binding_problem(row)
    if binding_problem:
        return "invalid", binding_problem
    if not isinstance(row["customer_transaction_id"], str):
        return "invalid", "customer_transaction_id malformed"
    try:
        uuid.UUID(row["customer_transaction_id"])
    except (ValueError, TypeError, AttributeError):
        return "invalid", "customer_transaction_id malformed"
    if type(row["attempt"]) is not int or row["attempt"] <= 0:
        return "invalid", "attempt malformed"
    if "transfer_id" in row and (type(row["transfer_id"]) is not int or row["transfer_id"] <= 0):
        return "invalid", "transfer_id malformed"
    if "create_entered" in row and row["create_entered"] is not True:
        return "invalid", "create_entered malformed"
    if "effects_applied" in row and type(row["effects_applied"]) is not bool:
        return "invalid", "effects_applied malformed"
    for field in ("intent_at", "outcome_at", "funding_at", "last_checked_at"):
        if field in row and not _epoch(row[field]):
            return "invalid", f"{field} malformed"
    if "next_check_at" in row and row["next_check_at"] is not None and not _epoch(row["next_check_at"]):
        return "invalid", "next_check_at malformed"
    status_fields = {"last_status", "last_occurred_at", "last_checked_at"}
    if keys & status_fields and not status_fields <= keys:
        return "invalid", "transfer status fields incomplete"
    if status_fields <= keys:
        if not isinstance(row.get("last_status"), str) or row["last_status"] not in TRANSFER_STATUSES or _stamp(row.get("last_occurred_at")) is None or not _epoch(row.get("last_checked_at")):
            return "invalid", "transfer status history malformed"
        if "transfer_id" not in row:
            return "invalid", "transfer status lacks transfer id"
    if "funding_status" in row and (not isinstance(row["funding_status"], str) or row["funding_status"] not in {"CREATED", "COMPLETED", "PROCESSING", "PENDING"} or not _epoch(row.get("funding_at"))):
        return "invalid", "funding status malformed"
    if "funding_at" in row and "funding_status" not in row:
        return "invalid", "funding timestamp without status"
    if "outcome_status" in row and (not isinstance(row["outcome_status"], str) or row["outcome_status"] not in {"sent", "failed"}):
        return "invalid", "outcome status malformed"
    if "outcome_at" in row and ("outcome_status" not in row or not _epoch(row["outcome_at"])) or row.get("outcome_status") == "sent" and not _epoch(row.get("outcome_at")):
        return "invalid", "outcome timestamp malformed"
    if "failure_status" in row and (not isinstance(row["failure_status"], str) or row["failure_status"] not in {"funds_refunded", "cancelled", "not_submitted"}):
        return "invalid", "failure status malformed"
    if "discrepancy_status" in row and (not isinstance(row["discrepancy_status"], str) or row["discrepancy_status"] not in TRANSFER_STATUSES or row["discrepancy_status"] == "outgoing_payment_sent"):
        return "invalid", "discrepancy status malformed"
    if "discrepancy_status" in row and (row.get("state") != STATE_DONE or row.get("outcome_status") != "sent" or row.get("last_status") != row["discrepancy_status"]):
        return "invalid", "discrepancy contradicts latest status"
    if "effects_applied" in row and row.get("outcome_status") != "sent":
        return "invalid", "effects without sent outcome"
    attempts = row.get("attempts", [])
    if not isinstance(attempts, list) or len(attempts) != row["attempt"] - 1:
        return "invalid", "attempt history malformed"
    for index, prior in enumerate(attempts):
        if (not isinstance(prior, dict)
                or not isinstance(prior.get("failure_status"), str)
                or prior["failure_status"] not in {"funds_refunded", "cancelled", "not_submitted"}):
            return "invalid", "prior attempt malformed"
        prior_kind, _ = classify_journal_row({**prior, "attempts": attempts[:index]})
        if prior_kind != "wise_modern_unresolved":
            return "invalid", "prior attempt malformed"
    if attempts and any(_stamp(prior.get("last_occurred_at")) and _stamp(row.get("last_occurred_at")) and _stamp(row["last_occurred_at"]) < _stamp(prior["last_occurred_at"]) for prior in attempts):
        return "invalid", "latest status predates attempt history"
    state, outcome, status = row["state"], row.get("outcome_status"), row.get("last_status")
    if state == STATE_DONE and outcome == "sent":
        if not all(_text(row.get(field)) for field in (
            "handler_name", "task_id", "approved_terms_hash", "payment_date"
        )):
            return "invalid", "sent attempt binding incomplete"
        if not _MODERN_SETTLED_WISE_FIELDS <= keys or "failure_status" in row or row.get("discrepancy_status") or status != "outgoing_payment_sent":
            return "wise_modern_unresolved", "sent outcome lacks current confirmation"
        if row.get("effects_applied") is not True:
            return "wise_modern_unresolved", "effects not applied"
        session = row.get("session_key")
        if session and (data is None or session_state(data, session) != SESSION_SETTLED):
            return "wise_modern_unresolved", "session not settled"
        return "wise_modern_resolved", ""
    if state == STATE_FAILED and outcome == "failed":
        if row.get("failure_status") == "not_submitted" and "transfer_id" not in row and "create_entered" not in row and status is None:
            return "wise_modern_unresolved", "not submitted"
        if row.get("failure_status") in {"funds_refunded", "cancelled"} and status == row["failure_status"] and "transfer_id" in row:
            return "wise_modern_unresolved", "terminal failure"
    if state in (STATE_INTENT, STATE_FUNDING_PENDING) and outcome is None and "failure_status" not in row and "discrepancy_status" not in row and row.get("effects_applied") is not True:
        if state == STATE_INTENT and status is None or state == STATE_FUNDING_PENDING and "transfer_id" in row:
            return "wise_modern_unresolved", "awaiting confirmation"
    return "invalid", "state and outcome contradict"


def is_legacy_settled_wise_row(entry: dict[str, Any]) -> bool:
    """Recognize only the settled row emitted by the pre-funding-states writer."""
    return classify_journal_row(entry)[0] == "legacy_settled"


def _wise_row_consistent(entry: dict[str, Any]) -> bool:
    """Classify contradictory Wise outcomes as unresolved without hiding the row."""
    return classify_journal_row(entry)[0] in {
        "legacy_settled", "wise_modern_resolved", "wise_modern_unresolved"
    }


def _wise_paid(entry: dict[str, Any], data: dict[str, Any] | None = None) -> bool:
    """Only a consistent, fully applied and session-settled attempt releases its profile."""
    return classify_journal_row(entry, data)[0] in {"legacy_settled", "wise_modern_resolved"}


def wise_replay_hold(
    data: dict[str, Any], profile_entity_id: str, *, exclude_obligation: str = ""
) -> tuple[str, dict[str, Any]] | None:
    """Hold a Wise offer on any unresolved attempt for its profile or an unclassified row.

    The key is returned for the operator message. No terms, calendar identity,
    handler name, or current obligation hash participates in this decision.
    """
    profile_hold = None
    for key, entry in data["obligations"].items():
        if key == exclude_obligation:
            continue
        kind, _ = classify_journal_row(entry, data)
        if entry.get("rail") == "btc" and kind in {"btc_valid_settled", "btc_valid_unresolved"}:
            continue
        if _wise_paid(entry, data):
            continue
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
    if rail == "wise":
        binding_problem = _wise_binding_problem({
            "handler_name": handler_name, "profile_entity_id": profile_entity_id,
            "task_id": task_id, "approved_terms_hash": approved_terms_hash,
            "payment_date": payment_date, "match_id": match_id,
        })
        if binding_problem:
            raise JournalError(f"Wise intent binding {binding_problem}")
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
        if classify_journal_row(entry, data)[0] != "wise_modern_unresolved":
            raise JournalError("Wise intent binding malformed")
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
        or classify_journal_row(entry, data)[0] != "wise_modern_unresolved"
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


def mark_wise_create_entered(path: Path, obligation: str) -> None:
    """Durably mark an entered transfer POST before it can be sent."""
    data = load(path)
    entry = data["obligations"].get(obligation)
    if (not isinstance(entry, dict)
            or classify_journal_row(entry, data)[0] != "wise_modern_unresolved"
            or entry.get("rail") != "wise"
            or entry.get("state") != STATE_INTENT or entry.get("transfer_id") is not None
            or entry.get("create_entered") is not None):
        raise JournalError("Wise create cannot be entered")
    entry["create_entered"] = True
    _write(path, data)
    if load(path)["obligations"][obligation].get("create_entered") is not True:
        raise JournalError("Wise create entry did not persist")


def record_wise_not_submitted(path: Path, obligation: str) -> None:
    """Close an attempt only when transfer creation was never entered."""
    data = load(path)
    entry = data["obligations"].get(obligation)
    if (not isinstance(entry, dict)
            or classify_journal_row(entry, data)[0] != "wise_modern_unresolved"
            or entry.get("rail") != "wise"
            or entry.get("state") != STATE_INTENT or entry.get("transfer_id") is not None
            or entry.get("create_entered") is not None):
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
        or classify_journal_row(entry, data)[0] != "wise_modern_unresolved"
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
    from wise_reconcile import TRANSFER_STATUSES
    if (
        not isinstance(entry, dict)
        or classify_journal_row(entry, data)[0] not in {"wise_modern_resolved", "wise_modern_unresolved"}
        or entry.get("rail") != "wise"
        or entry.get("transfer_id") != transfer_id
        or entry.get("state") not in (STATE_INTENT, STATE_FUNDING_PENDING, STATE_DONE, STATE_FAILED)
        or not isinstance(status, str) or status not in TRANSFER_STATUSES
        or _stamp(occurred_at) is None
    ):
        raise JournalError("Wise status transition refused")
    from wise_reconcile import _stamp as _wise_stamp
    current_stamp = _wise_stamp(occurred_at)
    previous = entry.get("last_occurred_at")
    if previous and current_stamp < _wise_stamp(previous):
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
        entry.pop("failure_status", None)
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
        or classify_journal_row(entry)[1] != "effects not applied"
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
    if (not isinstance(entry, dict) or entry.get("rail") != "wise"
            or classify_journal_row(entry, data)[0] not in {"wise_modern_resolved", "wise_modern_unresolved"}
            or due_at is not None and not _epoch(due_at)):
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
        or classify_journal_row(entry, data)[0] != "wise_modern_unresolved"
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
    if classify_journal_row(entry, data)[0] != "wise_modern_unresolved":
        raise JournalError("Wise replacement binding malformed")
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
