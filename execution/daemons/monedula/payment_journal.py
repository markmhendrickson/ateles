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

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

JOURNAL_NAME = ".monedula_payment_journal.json"

STATE_INTENT = "intent"
STATE_DONE = "done"


class JournalError(Exception):
    """The journal could not be read or durably written. Hold all payments."""


def _empty() -> dict[str, Any]:
    return {"version": 1, "generation": 0, "obligations": {}, "consumed_tokens": {}}


def load(path: Path) -> dict[str, Any]:
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
    return data


def _write(path: Path, data: dict[str, Any]) -> None:
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


def record_outcome(path: Path, obligation: str, status: str) -> None:
    """Record the transfer's outcome after it returns, and read it back."""
    data = load(path)
    entry = data["obligations"].get(obligation)
    if not isinstance(entry, dict):
        raise JournalError("outcome for an obligation with no recorded intent")
    entry["state"] = STATE_DONE
    entry["outcome_status"] = str(status)[:64]
    entry["outcome_at"] = time.time()
    _write(path, data)
    if obligation_state(load(path), obligation) != STATE_DONE:
        raise JournalError("payment outcome did not persist")
