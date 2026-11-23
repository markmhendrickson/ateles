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
import json
import os
import stat
import tempfile
import time
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
STATE_DONE = "done"


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
    """``None`` (never attempted), ``intent`` (unknown outcome) or ``done``."""
    entry = data["obligations"].get(obligation)
    if not isinstance(entry, dict):
        return None
    state = entry.get("state")
    # Anything other than a recorded outcome is treated as unknown — never
    # as "not attempted" — so a damaged entry cannot license a retry.
    return STATE_DONE if state == STATE_DONE else STATE_INTENT


def token_consumed(data: dict[str, Any], token: str) -> bool:
    return token in data["consumed_tokens"]


def record_intent(path: Path, obligation: str, token: str, *, generation: int) -> None:
    """Durably record intent-to-pay BEFORE the transfer, and read it back.

    Refuses (``JournalError``) if the obligation already has an entry or the
    token was already consumed — the caller must not execute in that case.
    """
    data = load(path)
    if obligation in data["obligations"]:
        raise JournalError("obligation already recorded — refusing a second attempt")
    if token in data["consumed_tokens"]:
        raise JournalError("approval token already consumed")
    data["obligations"][obligation] = {
        "state": STATE_INTENT,
        "token": token,
        "generation": int(generation),
        "intent_at": time.time(),
    }
    data["consumed_tokens"][token] = obligation
    _write(path, data)
    back = load(path)
    if obligation_state(back, obligation) != STATE_INTENT or not token_consumed(
        back, token
    ):
        raise JournalError("payment intent did not persist")


def record_outcome(
    path: Path, obligation: str, status: str, *, event_queued: bool | None = None,
    event_entry: dict[str, Any] | None = None,
) -> None:
    """Record the transfer's outcome after it returns, and read it back.

    ``event_queued`` says whether this route records a payment_event for the
    outcome: True when its entry is queued, False when queueing failed (then
    ``event_entry`` is kept with the outcome so recording can be retried), and
    None for a route that writes no payment_event (the Telegram consent route
    today), whose outcome carries no ``payment_event_state`` and so never holds
    payments for a record that route was never going to write.
    """
    if event_queued is not None and not isinstance(event_queued, bool):
        raise JournalError("event_queued must be True, False or None")
    if event_queued is False and not isinstance(event_entry, dict):
        raise JournalError("unrecorded payment_event requires its entry")
    data = load(path)
    entry = data["obligations"].get(obligation)
    if not isinstance(entry, dict):
        raise JournalError("outcome for an obligation with no recorded intent")
    entry["state"] = STATE_DONE
    entry["outcome_status"] = str(status)[:64]
    entry["outcome_at"] = time.time()
    if event_queued is not None:
        entry["payment_event_state"] = "queued" if event_queued else "unrecorded"
    if event_queued is False:
        entry["payment_event_entry"] = event_entry
    _write(path, data)
    back = load(path)
    if (
        obligation_state(back, obligation) != STATE_DONE
        or back["obligations"][obligation].get("payment_event_state")
        != entry.get("payment_event_state")
        or (
            event_queued is False
            and back["obligations"][obligation].get("payment_event_entry") != event_entry
        )
    ):
        raise JournalError("payment outcome did not persist")


def mark_payment_event_queued(path: Path, obligation: str) -> None:
    """Mark a retained event queued only after its pending entry was read back."""
    data = load(path)
    entry = data["obligations"].get(obligation)
    if (
        not isinstance(entry, dict)
        or entry.get("state") != STATE_DONE
        or entry.get("payment_event_state") != "unrecorded"
        or not isinstance(entry.get("payment_event_entry"), dict)
    ):
        raise JournalError("payment_event has no recoverable journaled outcome")
    entry["payment_event_state"] = "queued"
    _write(path, data)
    if load(path)["obligations"][obligation].get("payment_event_state") != "queued":
        raise JournalError("payment_event queued state did not persist")


def mark_payment_event_stored(path: Path, obligation: str) -> None:
    """Mark a journaled outcome stored only after the entity was read back."""
    data = load(path)
    entry = data["obligations"].get(obligation)
    if not isinstance(entry, dict) or entry.get("state") != STATE_DONE:
        raise JournalError("payment_event has no journaled outcome")
    entry["payment_event_state"] = "stored"
    _write(path, data)
    if load(path)["obligations"][obligation].get("payment_event_state") != "stored":
        raise JournalError("payment_event stored state did not persist")


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
