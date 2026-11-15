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
treat the journal as empty.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

JOURNAL_NAME = ".monedula_payment_journal.json"

STATE_INTENT = "intent"
STATE_DONE = "done"
FAILURE_PRE_SUBMISSION = "pre_submission"
FAILURE_UNKNOWN = "unknown"


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


def _lock_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.lock")


@contextmanager
def _locked(path: Path, *, exclusive: bool) -> Iterator[None]:
    """Lock the journal across processes.

    The lock is a sibling file because the journal itself is atomically
    replaced. Locking the replaced inode would not serialize later openers.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(_lock_path(path), os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise JournalError(
            f"payment journal lock failed: {type(exc).__name__}"
        ) from exc
    try:
        with os.fdopen(fd, "a+") as lock_file:
            try:
                fcntl.flock(
                    lock_file.fileno(),
                    fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
                )
                yield
            except OSError as exc:
                raise JournalError(
                    f"payment journal lock failed: {type(exc).__name__}"
                ) from exc
            finally:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
    except JournalError:
        raise
    except OSError as exc:
        raise JournalError(
            f"payment journal lock failed: {type(exc).__name__}"
        ) from exc


def _load_unlocked(path: Path) -> dict[str, Any]:
    """Return the journal. A missing file is an empty journal; anything
    unreadable or structurally wrong raises :class:`JournalError`."""
    if not path.exists():
        return _empty()
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
    return data


def load(path: Path) -> dict[str, Any]:
    """Read one coherent journal snapshot under a shared lock."""
    with _locked(path, exclusive=False):
        return _load_unlocked(path)


def _write_unlocked(path: Path, data: dict[str, Any]) -> None:
    """Atomic, fsynced replace. Any failure raises :class:`JournalError`."""
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


def current_generation(path: Path) -> int:
    return int(load(path)["generation"])


def next_generation(path: Path) -> int:
    """Issue a never-before-used request generation, durably."""
    with _locked(path, exclusive=True):
        data = _load_unlocked(path)
        data["generation"] = int(data["generation"]) + 1
        _write_unlocked(path, data)
        if _load_unlocked(path)["generation"] != data["generation"]:
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
    with _locked(path, exclusive=True):
        data = _load_unlocked(path)
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
        _write_unlocked(path, data)
        back = _load_unlocked(path)
        if obligation_state(back, obligation) != STATE_INTENT or not token_consumed(
            back, token
        ):
            raise JournalError("payment intent did not persist")


def _outcome_fields(result: str | dict[str, Any]) -> tuple[str, str | None]:
    if isinstance(result, dict):
        status = str(result.get("status") or "unreported")[:64]
        claimed_class = str(result.get("failure_class") or "")
    else:
        status = str(result)[:64]
        claimed_class = ""
    if status.strip().lower() in {"sent", "succeeded", "success", "completed"}:
        return status, None
    failure_class = (
        FAILURE_PRE_SUBMISSION
        if claimed_class == FAILURE_PRE_SUBMISSION
        else FAILURE_UNKNOWN
    )
    return status, failure_class


def record_outcome(
    path: Path, obligation: str, result: str | dict[str, Any]
) -> None:
    """Record the transfer outcome and its conservative failure class."""
    with _locked(path, exclusive=True):
        data = _load_unlocked(path)
        entry = data["obligations"].get(obligation)
        if not isinstance(entry, dict):
            raise JournalError("outcome for an obligation with no recorded intent")
        status, failure_class = _outcome_fields(result)
        entry["state"] = STATE_DONE
        entry["outcome_status"] = status
        entry["outcome_at"] = time.time()
        if failure_class is not None:
            entry["failure_class"] = failure_class
        _write_unlocked(path, data)
        if obligation_state(_load_unlocked(path), obligation) != STATE_DONE:
            raise JournalError("payment outcome did not persist")


def retry_state(data: dict[str, Any], obligation: str) -> str | None:
    """State of the one retry nested under an original obligation."""
    original = data["obligations"].get(obligation)
    if not isinstance(original, dict):
        return None
    retry = original.get("retry")
    if not isinstance(retry, dict):
        return None
    return STATE_DONE if retry.get("state") == STATE_DONE else STATE_INTENT


def reserve_retry(
    path: Path,
    obligation: str,
    session: str,
    token: str,
    *,
    generation: int,
    expected_intent_at: float,
    expected_outcome_at: float,
    expected_failure_class: str,
    not_paid_confirmed: bool,
) -> None:
    """Atomically reserve the sole retry under the original obligation.

    The original failure and source session are revalidated while holding the
    same exclusive lock that writes the retry intent. Unknown or historical
    unclassified failures additionally require the operator's authenticated
    rail check.
    """
    with _locked(path, exclusive=True):
        data = _load_unlocked(path)
        original = data["obligations"].get(obligation)
        source = data["sessions"].get(session)
        if not isinstance(original, dict):
            raise JournalError("retry source obligation is missing")
        if not isinstance(source, dict):
            raise JournalError("retry source session is missing")
        if source.get("state") not in SESSION_TERMINAL:
            raise JournalError("retry source session is not terminal")
        if source.get("obligation") != obligation:
            raise JournalError("retry source session does not name this obligation")
        if original.get("state") != STATE_DONE:
            raise JournalError("retry source outcome is not terminal")
        if str(original.get("outcome_status") or "").strip().lower() != "failed":
            raise JournalError("retry source outcome is no longer failed")
        if original.get("intent_at") != expected_intent_at:
            raise JournalError("retry source intent changed before reservation")
        if original.get("outcome_at") != expected_outcome_at:
            raise JournalError("retry source outcome changed before reservation")
        actual_class = str(original.get("failure_class") or "unclassified")
        if actual_class != expected_failure_class:
            raise JournalError("retry source failure class changed before reservation")
        if actual_class != FAILURE_PRE_SUBMISSION and not not_paid_confirmed:
            raise JournalError(
                "retry source outcome is unconfirmed and lacks an authenticated "
                "'not paid' rail check"
            )
        if isinstance(original.get("retry"), dict):
            raise JournalError("retry already reserved for this obligation")
        if token in data["consumed_tokens"]:
            raise JournalError("retry approval token already consumed")
        original["retry"] = {
            "state": STATE_INTENT,
            "token": token,
            "generation": int(generation),
            "intent_at": time.time(),
        }
        data["consumed_tokens"][token] = obligation
        _write_unlocked(path, data)
        back = _load_unlocked(path)
        if retry_state(back, obligation) != STATE_INTENT:
            raise JournalError("retry intent did not persist")


def record_retry_outcome(
    path: Path, obligation: str, result: str | dict[str, Any]
) -> None:
    """Record the sole retry outcome without replacing the original attempt."""
    with _locked(path, exclusive=True):
        data = _load_unlocked(path)
        original = data["obligations"].get(obligation)
        retry = original.get("retry") if isinstance(original, dict) else None
        if not isinstance(retry, dict) or retry.get("state") != STATE_INTENT:
            raise JournalError("retry outcome has no reserved retry intent")
        status, failure_class = _outcome_fields(result)
        retry["state"] = STATE_DONE
        retry["outcome_status"] = status
        retry["outcome_at"] = time.time()
        if failure_class is not None:
            retry["failure_class"] = failure_class
        _write_unlocked(path, data)
        if retry_state(_load_unlocked(path), obligation) != STATE_DONE:
            raise JournalError("retry outcome did not persist")


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


def note_pending(
    path: Path,
    key: str,
    *,
    ended_at: float | None = None,
    started_on: str | None = None,
    obligation: str | None = None,
) -> None:
    """Record a session as raised-and-unresolved. Idempotent; never reopens a
    terminal session. Missing source metadata is filled from a later calendar
    read, but conflicting metadata fails closed."""
    with _locked(path, exclusive=True):
        data = _load_unlocked(path)
        existing = data["sessions"].get(key)
        if isinstance(existing, dict):
            changed = False
            for name, value in (
                ("started_on", started_on),
                ("obligation", obligation),
            ):
                if value is None:
                    continue
                if existing.get(name) not in (None, value):
                    if existing.get("state") in SESSION_TERMINAL:
                        # Terminal source metadata is immutable. A later
                        # profile/calendar edit cannot rewrite its history.
                        continue
                    if name == "obligation" and existing.get("state") == SESSION_PENDING:
                        # Terms can legitimately change before any payment is
                        # attempted; the next approval binds the new key.
                        existing[name] = value
                        changed = True
                        continue
                    raise JournalError(f"session {name} changed")
                if existing.get(name) is None:
                    existing[name] = value
                    changed = True
            if changed:
                _write_unlocked(path, data)
            return
        data["sessions"][key] = {
            "state": SESSION_PENDING,
            "first_seen": time.time(),
            "ended_at": ended_at,
            "started_on": started_on,
            "obligation": obligation,
        }
        _write_unlocked(path, data)
        if session_state(_load_unlocked(path), key) != SESSION_PENDING:
            raise JournalError("pending session did not persist")


def set_session_state(path: Path, key: str, state: str) -> None:
    """Move a session to a new state and read it back."""
    with _locked(path, exclusive=True):
        data = _load_unlocked(path)
        entry = data["sessions"].get(key)
        if not isinstance(entry, dict):
            entry = {"first_seen": time.time(), "ended_at": None}
        entry["state"] = state
        entry["resolved_at"] = time.time()
        data["sessions"][key] = entry
        _write_unlocked(path, data)
        if session_state(_load_unlocked(path), key) != state:
            raise JournalError("session state did not persist")
