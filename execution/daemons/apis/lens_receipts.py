"""Lens-review entries in the Fly receipt ledger.

A lens review run on Fly is held in the same durable ledger as builder
deliveries (``FlyLedger.delivery_receipts``), under a ``lens:`` key that names
the repository, pull request, head commit and lens, so a lens dispatcher can see
across restarts that this lens already ran (or is running) on this head. The
operator clears a stuck entry with ``fly_delivery_receipt.py`` like any other.
Once a notification is claimed, later settlements retain its original
``notified_at``; no later claim may send another notice for that reservation.

Synchronous: async callers wrap each call in ``asyncio.to_thread``.
"""

from __future__ import annotations

import re
import secrets
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_delivery  # noqa: E402
import fly_runner  # noqa: E402

KIND = "lens"

LAUNCHING = "launching"
UNKNOWN = "unknown"
FAILED = "failed"
AWAITING = "awaiting_verdict"

_HEAD = re.compile(r"^[0-9a-f]{40}$")
_LENS = re.compile(r"^[a-z0-9_-]+$")
_LEDGER_ERRORS = (OSError, fly_runner.FlyLedgerError)


class LensReceiptUnreadable(Exception):
    """The ledger could not be read, so whether a lens entry exists is unknown."""


@dataclass(frozen=True)
class Reservation:
    key: str
    token: str
    meta: dict


def lens_key(repository: object, pr_number: object, head_sha: object, lens: object) -> str:
    """The ONE derivation of a lens receipt key. Raises ``ValueError`` on a
    head that is not a full lowercase commit id, a non-positive or non-int
    pull number, or a lens name outside ``[a-z0-9_-]``."""
    if not isinstance(head_sha, str) or not _HEAD.match(head_sha):
        raise ValueError("head_sha must be a 40-character lowercase hex commit id")
    if not isinstance(pr_number, int) or isinstance(pr_number, bool) or pr_number <= 0:
        raise ValueError("pr_number must be a positive int")
    if not isinstance(lens, str) or not _LENS.match(lens):
        raise ValueError("lens must match ^[a-z0-9_-]+$")
    repo = build_delivery.canonical_repository(repository)
    if not repo:
        raise ValueError("repository is empty")
    return f"lens:{repo}#{pr_number}@{head_sha}:{lens}"


def meta_for(repository: object, pr_number: int, head_sha: str, lens: str) -> dict:
    """The identity fields every entry for this lens run carries."""
    return {"kind": KIND, "repository": build_delivery.canonical_repository(repository),
            "pr": pr_number, "head": head_sha, "lens": lens}  # fmt: skip


def _ledger(ledger):
    return ledger if ledger is not None else build_delivery._delivery_ledger()


def reserve(key: str, meta: dict, *, ledger=None, clock=time.time):
    """``("reserved", Reservation)``, ``("held", existing_entry)`` when the key
    already has an entry, or ``("error", None)`` when the write failed or the
    existing entry could not be read back. Only ``reserved`` may launch."""
    led = _ledger(ledger)
    token = secrets.token_hex(16)
    entry = {**meta, "status": LAUNCHING, "token": token, "recorded_at": clock()}
    try:
        made = led.reserve_delivery(key, entry)
    except _LEDGER_ERRORS:
        return "error", None
    if made:
        return "reserved", Reservation(key=key, token=token, meta=dict(meta))
    try:
        existing = led.delivery_receipts().get(key)
    except _LEDGER_ERRORS:
        return "error", None
    if existing is None:
        return "error", None
    return "held", existing


def settle(res: Reservation, status: str, *, ledger=None, clock=time.time, **fields) -> bool:
    """Replace the entry with *status* (and *fields*) only while it is still
    this reservation's (compare-and-set on the token), retaining any prior
    notification claim. False on any failure."""
    entry = {**res.meta, **fields, "status": status, "token": res.token, "recorded_at": clock()}
    try:
        return bool(_ledger(ledger).settle_delivery(res.key, res.token, entry))
    except _LEDGER_ERRORS:
        return False


def release(res: Reservation, *, statuses: tuple[str, ...], ledger=None) -> bool:
    """Remove the entry only while it is this reservation's and its status is
    one of *statuses*. False on any ledger failure; a bare string for
    *statuses* is a programming error and raises ``TypeError``."""
    if isinstance(statuses, str):
        raise TypeError("statuses must be a tuple of status strings, not a string")
    try:
        return bool(_ledger(ledger).release_delivery(res.key, res.token, tuple(statuses)))
    except _LEDGER_ERRORS:
        return False


def held(key: str, *, ledger=None) -> dict | None:
    """The entry for *key*, or None. Raises ``LensReceiptUnreadable`` when the
    ledger cannot be read (unknown is never reported as absent)."""
    try:
        return _ledger(ledger).delivery_receipts().get(key)
    except _LEDGER_ERRORS as exc:
        raise LensReceiptUnreadable(str(exc)) from exc


def claim_notification(key: str, *, ledger=None, clock=time.time) -> dict | None:
    """Mark the entry notified and return it, only for the caller whose write
    wins; None when absent, tokenless, already notified, or the write lost or
    failed. Only a caller that gets an entry back may send the notice."""
    led = _ledger(ledger)
    try:
        entry = led.delivery_receipts().get(key)
    except _LEDGER_ERRORS:
        return None
    if not entry or not isinstance(entry.get("token"), str) or "notified_at" in entry:
        return None
    try:
        # The whole entry is compared, not just its token: two notifiers that
        # read the same entry share its token, and only one may win.
        won = led.replace_delivery(key, entry, {**entry, "notified_at": clock()})
    except _LEDGER_ERRORS:
        return None
    return entry if won else None
