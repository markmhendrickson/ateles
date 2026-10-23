"""Fail-closed monthly spend authorization and the local spend ledger.

The question this module answers is "may this call spend money?". Every
absent, unreadable, or malformed input resolves to a refusal, never to a
default (principle 5). ``unknown`` is not ``zero``: an unparseable cap is
``CAP_UNREADABLE``, not a cap of 0.

Cap sources
-----------
The cap comes from the ``vendor_binding.constraints`` JSON string:

``monthly_cap_usd``         this slot's own monthly cap (required)
``cap_group`` +             optional: slots sharing a ``cap_group`` also share a
``cap_group_total_usd``     combined monthly total. A call must fit BOTH the
                            slot cap and the group total.

Ledger
------
Append-only JSONL at ``<root>/<billing_slot>/<YYYY-MM>.jsonl`` (UTC month),
root ``$ATELES_GENERATION_SPEND_PATH`` or ``$XDG_STATE_HOME/ateles/generation_spend``
(default ``~/.local/state/...``: state, not a cache a cleaner may empty).
Directories are created 0700 and files 0600; a ledger location that is
group- or world-writable, or reached through a symlink, is treated as tampered
and refuses. The ledger makes no network call.

Each row carries a ``state``: ``pending`` (written under the lock BEFORE the
vendor request, at the estimate), ``completed`` (finalized with the final
cost), ``accepted_unfinished`` (the vendor may have billed but no usable
artifact came back; the estimate STAYS spent), or ``voided`` (the vendor
definitively did not bill; counts 0). Later rows for the same ``generation_id``
supersede earlier ones. Every state except ``voided`` counts toward the cap, so
a crash between the request and the finalize row still leaves the estimate
counted: the failure direction is "too much spent", never "nothing spent".

To keep two callers from both authorizing against the same remaining balance,
``SpendLedger.locked()`` takes an exclusive advisory lock that the caller holds
from authorization through the final row.

A small manifest (``manifest.json`` in the root) lists every ledger file that
has been written. A file the manifest lists that is now missing, or ledger
files with no manifest, refuse ``CAP_UNREADABLE``: a deleted ledger must not
read as zero spend. Deleting the whole root, manifest included, is not
detectable from local state (tracked in #1353).

Money is compared in integer micro-dollars so a cap boundary is not decided by
floating-point noise. ``spent + estimate == cap`` authorizes; only ``>``
refuses.
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from .errors import (
    CAP_EXHAUSTED,
    CapabilityError,
    CAP_UNREADABLE,
    CAP_UNSET,
    GenerationRefused,
)
from .slots import SLOTS
from .vendor_binding import VendorBinding

SPEND_PATH_ENV = "ATELES_GENERATION_SPEND_PATH"
LOCK_TIMEOUT_ENV = "ATELES_GENERATION_LOCK_TIMEOUT_S"
_MICRO = 1_000_000


def to_micro(usd: float) -> int:
    return int(round(usd * _MICRO))


def from_micro(micro: int) -> float:
    return micro / _MICRO


log = logging.getLogger(__name__)

STATE_PENDING = "pending"
STATE_COMPLETED = "completed"
STATE_ACCEPTED_UNFINISHED = "accepted_unfinished"
STATE_VOIDED = "voided"
STATES = (STATE_PENDING, STATE_COMPLETED, STATE_ACCEPTED_UNFINISHED, STATE_VOIDED)
MANIFEST_NAME = "manifest.json"


def default_root() -> Path:
    override = os.environ.get(SPEND_PATH_ENV)
    if override:
        return Path(override).expanduser()
    state_home = os.environ.get("XDG_STATE_HOME")
    base = Path(state_home).expanduser() if state_home else Path.home() / ".local" / "state"
    return base / "ateles" / "generation_spend"


def write_all(fd: int, data: bytes) -> None:
    """``os.write`` may write less than asked; loop until all bytes are out."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write")
        view = view[written:]


@dataclass(frozen=True)
class CapPolicy:
    slot: str
    cap_usd: float
    group: str | None = None
    group_total_usd: float | None = None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def parse_cap_policy(binding: VendorBinding) -> CapPolicy:
    """Read the cap from the binding or refuse. Never guesses a default."""
    slot = binding.capability
    if binding.constraints is None:
        raise GenerationRefused(
            CAP_UNREADABLE,
            slot,
            "the binding's constraints are not valid JSON, so the cap cannot be read",
            "Operator: fix vendor_binding.constraints to a JSON object containing "
            "monthly_cap_usd. The client refuses rather than guess.",
        )
    if binding.constraints_unset or "monthly_cap_usd" not in binding.constraints:
        raise GenerationRefused(
            CAP_UNSET,
            slot,
            "no monthly_cap_usd is configured for this slot",
            "Operator: set monthly_cap_usd in the binding's constraints. Refusing "
            "is correct until it is set.",
        )
    cap = _number(binding.constraints["monthly_cap_usd"])
    if cap is None:
        raise GenerationRefused(
            CAP_UNREADABLE,
            slot,
            "monthly_cap_usd is not a non-negative number",
            "Operator: set monthly_cap_usd to a plain number of US dollars.",
        )
    group = binding.constraints.get("cap_group")
    if group is None:
        return CapPolicy(slot, cap)
    if not isinstance(group, str) or not group.strip():
        raise GenerationRefused(
            CAP_UNREADABLE,
            slot,
            "cap_group is present but is not a non-empty string",
            "Operator: fix cap_group in the binding's constraints.",
        )
    if "cap_group_total_usd" not in binding.constraints:
        raise GenerationRefused(
            CAP_UNSET,
            slot,
            f"cap_group {group!r} is set but cap_group_total_usd is not",
            "Operator: set cap_group_total_usd (the combined monthly total) or "
            "remove cap_group.",
        )
    total = _number(binding.constraints["cap_group_total_usd"])
    if total is None:
        raise GenerationRefused(
            CAP_UNREADABLE,
            slot,
            "cap_group_total_usd is not a non-negative number",
            "Operator: set cap_group_total_usd to a plain number of US dollars.",
        )
    return CapPolicy(slot, cap, group.strip(), total)


@dataclass(frozen=True)
class Authorization:
    billing_slot: str
    month: str
    spent_usd: float
    cap_usd: float
    remaining_usd: float
    estimate_usd: float
    cap_group: str | None
    group_spent_usd: float | None = None
    group_total_usd: float | None = None


def _unreadable(slot: str, why: str) -> GenerationRefused:
    return GenerationRefused(
        CAP_UNREADABLE,
        slot,
        f"the spend ledger could not be read ({why})",
        "Operator: repair the ledger location (permissions, corruption, a "
        f"missing file the manifest lists, or {SPEND_PATH_ENV}) so spent totals "
        "can be verified. Nothing was spent by this call.",
    )


class LedgerWriteError(CapabilityError):
    """A ledger append failed. Not a refusal by itself: see ``record``."""


class SpendLedger:
    def __init__(
        self,
        root: Path | str | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
        lock_timeout_s: float | None = None,
    ) -> None:
        self.root = Path(root).expanduser() if root else default_root()
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        if lock_timeout_s is None:
            lock_timeout_s = float(os.environ.get(LOCK_TIMEOUT_ENV, "1200"))
        self.lock_timeout_s = lock_timeout_s

    def month(self) -> str:
        return self._clock().astimezone(timezone.utc).strftime("%Y-%m")

    def now_iso(self) -> str:
        return self._clock().astimezone(timezone.utc).isoformat()

    @contextmanager
    def locked(self, slot: str) -> Iterator["LockedLedger"]:
        try:
            self._ensure_dir(self.root)
            fd = os.open(
                self.root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
            )
        except OSError as exc:
            raise _unreadable(slot, type(exc).__name__) from None
        try:
            deadline = time.monotonic() + self.lock_timeout_s
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise _unreadable(
                            slot, "another generation call held the ledger lock too long"
                        ) from None
                    time.sleep(0.05)
            yield LockedLedger(self, slot)
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @staticmethod
    def _ensure_dir(path: Path) -> None:
        if path.is_symlink():
            raise OSError("ledger path is a symlink")
        os.makedirs(path, mode=0o700, exist_ok=True)
        if path.is_symlink():
            raise OSError("ledger path is a symlink")
        if stat.S_IMODE(path.stat().st_mode) & 0o022:
            raise OSError("ledger directory is group- or world-writable")


def _read_nofollow(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "r", encoding="utf-8") as fh:
        st = os.fstat(fh.fileno())
        if stat.S_IMODE(st.st_mode) & 0o022:
            raise OSError("ledger file is group- or world-writable")
        return fh.read()


class LockedLedger:
    """Ledger operations that are only reachable while the lock is held."""

    def __init__(self, ledger: SpendLedger, slot: str) -> None:
        self._l = ledger
        self._slot = slot

    def now_iso(self) -> str:
        return self._l.now_iso()

    def _file(self, billing_slot: str, month: str) -> Path:
        return self._l.root / billing_slot / f"{month}.jsonl"

    @staticmethod
    def _rel(billing_slot: str, month: str) -> str:
        return f"{billing_slot}/{month}.jsonl"

    def _manifest(self) -> dict[str, Any] | None:
        path = self._l.root / MANIFEST_NAME
        if not path.exists() and not path.is_symlink():
            return None
        data = json.loads(_read_nofollow(path))
        files = data["files"]
        if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
            raise ValueError("manifest files list is malformed")
        return {"files": files}

    def _write_manifest(self, files: list[str]) -> None:
        tmp = self._l.root / (MANIFEST_NAME + ".tmp")
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        try:
            write_all(fd, json.dumps({"files": sorted(set(files))}).encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, self._l.root / MANIFEST_NAME)

    def _check_manifest_for_missing(self, rel: str) -> None:
        manifest = self._manifest()
        if manifest is None:
            # No manifest. Fine on a fresh install; not fine if ledger data exists.
            existing = [
                p for p in self._l.root.glob("*/*.jsonl") if p.is_file() or p.is_symlink()
            ]
            if existing:
                raise ValueError("ledger files exist but the manifest is missing")
            return
        if rel in manifest["files"]:
            raise ValueError("a ledger file the manifest lists is missing")

    def _rows(self, billing_slot: str, month: str) -> list[dict[str, Any]]:
        path = self._file(billing_slot, month)
        rel = self._rel(billing_slot, month)
        try:
            if not path.exists() and not path.is_symlink():
                self._check_manifest_for_missing(rel)
                return []
            if self._manifest() is None:
                raise ValueError("ledger files exist but the manifest is missing")
            rows: dict[str, dict[str, Any]] = {}
            for line in _read_nofollow(path).splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                gid = row["generation_id"]
                if not isinstance(gid, str) or not gid:
                    raise ValueError("row without generation_id")
                if _number(row["cost_usd"]) is None:
                    raise ValueError("row without a valid cost_usd")
                if row["state"] not in STATES:
                    raise ValueError("row with an unknown state")
                rows[gid] = row  # a later row for the same id supersedes an earlier one
            return list(rows.values())
        except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            raise _unreadable(self._slot, type(exc).__name__) from None

    @staticmethod
    def _micro(row: dict[str, Any]) -> int:
        return 0 if row["state"] == STATE_VOIDED else to_micro(row["cost_usd"])

    def slot_spent_micro(self, billing_slot: str, month: str) -> int:
        return sum(self._micro(r) for r in self._rows(billing_slot, month))

    def group_spent_micro(self, group: str, month: str) -> int:
        total = 0
        for slot in SLOTS:
            for row in self._rows(slot, month):
                if row.get("cap_group") == group:
                    total += self._micro(row)
        return total

    def budget(self, policy: CapPolicy) -> tuple[float, float, float]:
        """(spent, cap, remaining) of the TIGHTER of the slot and group budgets."""
        spent, cap, remaining, _ = self._budget(policy)
        return from_micro(spent), from_micro(cap), from_micro(remaining)

    def _budget(self, policy: CapPolicy) -> tuple[int, int, int, bool]:
        month = self._l.month()
        slot_cap = to_micro(policy.cap_usd)
        slot_spent = self.slot_spent_micro(policy.slot, month)
        slot_remaining = slot_cap - slot_spent
        if policy.group is not None and policy.group_total_usd is not None:
            group_total = to_micro(policy.group_total_usd)
            group_spent = self.group_spent_micro(policy.group, month)
            group_remaining = group_total - group_spent
            if group_remaining < slot_remaining:
                return group_spent, group_total, group_remaining, True
        return slot_spent, slot_cap, slot_remaining, False

    def authorize(self, policy: CapPolicy, estimate_usd: float) -> Authorization:
        """Raise ``CAP_EXHAUSTED`` unless the call fits the slot AND group cap."""
        month = self._l.month()
        est = to_micro(estimate_usd)
        spent, cap, remaining, group_binds = self._budget(policy)
        if est > remaining:
            scope = "combined cap group" if group_binds else "slot"
            raise GenerationRefused(
                CAP_EXHAUSTED,
                policy.slot,
                f"estimated cost ${from_micro(est):.2f} exceeds the remaining "
                f"monthly budget for the {scope}",
                "Stop, wait for the next UTC month, or ask the operator to raise "
                "the cap in the binding's constraints.",
                spent_usd=from_micro(spent),
                cap_usd=from_micro(cap),
                remaining_usd=from_micro(remaining),
            )
        slot_spent = self.slot_spent_micro(policy.slot, month)
        return Authorization(
            billing_slot=policy.slot,
            month=month,
            spent_usd=from_micro(slot_spent),
            cap_usd=policy.cap_usd,
            remaining_usd=from_micro(to_micro(policy.cap_usd) - slot_spent),
            estimate_usd=estimate_usd,
            cap_group=policy.group,
            group_spent_usd=None,
            group_total_usd=policy.group_total_usd,
        )

    def record(self, row: dict[str, Any], billing_slot: str, month: str | None = None) -> None:
        """Append one row (pending before the vendor call, then its final state).

        ``month`` pins the file to the month the call was authorized in, so a
        call that straddles midnight on the last day of a month keeps all its
        rows in one file. Raises ``LedgerWriteError``; the caller decides what
        that means (before the vendor call: nothing spent; after: SPENT_UNRECORDED).
        """
        month = month or self._l.month()
        path = self._file(billing_slot, month)
        rel = self._rel(billing_slot, month)
        try:
            SpendLedger._ensure_dir(path.parent)
            manifest = self._manifest() or {"files": []}
            if rel not in manifest["files"]:
                self._write_manifest([*manifest["files"], rel])
            payload = (json.dumps(row, sort_keys=True) + "\n").encode()
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                write_all(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise LedgerWriteError(type(exc).__name__) from None

    def remaining_after(self, policy: CapPolicy) -> float:
        """Remaining budget (the tighter of slot and group) after recording."""
        month = self._l.month()
        remaining = to_micro(policy.cap_usd) - self.slot_spent_micro(policy.slot, month)
        if policy.group is not None and policy.group_total_usd is not None:
            remaining = min(
                remaining,
                to_micro(policy.group_total_usd)
                - self.group_spent_micro(policy.group, month),
            )
        return from_micro(remaining)
