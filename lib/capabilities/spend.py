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
root ``$ATELES_GENERATION_SPEND_PATH`` or ``~/.cache/ateles/generation_spend``.
Directories are created 0700 and files 0600; a ledger location that is
group- or world-writable is treated as tampered and refuses. The ledger makes
no network call. Each row is keyed by ``generation_id``; rows are deduplicated
by that id before summing, so a replayed write cannot double-count.

Spend is recorded only after the artifact is received (never a reservation
that must be unwound). To keep two callers from both authorizing against the
same remaining balance, ``SpendLedger.locked()`` takes an exclusive advisory
lock that the caller holds from authorization through recording. That
serializes generation per ledger root, which is deliberate: a second call
sees the first call's recorded spend.

Money is compared in integer micro-dollars so a cap boundary is not decided by
floating-point noise. ``spent + estimate == cap`` authorizes; only ``>``
refuses.
"""

from __future__ import annotations

import fcntl
import json
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


def default_root() -> Path:
    override = os.environ.get(SPEND_PATH_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".cache" / "ateles" / "generation_spend"


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
        "Operator: repair the ledger location (permissions, corruption, or "
        f"{SPEND_PATH_ENV}) so spent totals can be verified. Nothing was spent.",
    )


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
            fd = os.open(self.root / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
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
        os.makedirs(path, mode=0o700, exist_ok=True)
        if stat.S_IMODE(path.stat().st_mode) & 0o022:
            raise OSError("ledger directory is group- or world-writable")


class LockedLedger:
    """Ledger operations that are only reachable while the lock is held."""

    def __init__(self, ledger: SpendLedger, slot: str) -> None:
        self._l = ledger
        self._slot = slot

    def now_iso(self) -> str:
        return self._l.now_iso()

    def _file(self, billing_slot: str, month: str) -> Path:
        return self._l.root / billing_slot / f"{month}.jsonl"

    def _rows(self, billing_slot: str, month: str) -> list[dict[str, Any]]:
        path = self._file(billing_slot, month)
        try:
            if not path.exists():
                return []
            if stat.S_IMODE(path.stat().st_mode) & 0o022:
                raise OSError("ledger file is group- or world-writable")
            rows: dict[str, dict[str, Any]] = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                gid = row["generation_id"]
                if not isinstance(gid, str) or not gid:
                    raise ValueError("row without generation_id")
                cost = _number(row["cost_usd"])
                if cost is None:
                    raise ValueError("row without a valid cost_usd")
                rows.setdefault(gid, row)  # dedupe by generation_id
            return list(rows.values())
        except (OSError, ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
            raise _unreadable(self._slot, type(exc).__name__) from None

    def slot_spent_micro(self, billing_slot: str, month: str) -> int:
        return sum(to_micro(r["cost_usd"]) for r in self._rows(billing_slot, month))

    def group_spent_micro(self, group: str, month: str) -> int:
        total = 0
        for slot in SLOTS:
            for row in self._rows(slot, month):
                if row.get("cap_group") == group:
                    total += to_micro(row["cost_usd"])
        return total

    def authorize(self, policy: CapPolicy, estimate_usd: float) -> Authorization:
        """Raise ``CAP_EXHAUSTED`` unless the call fits the slot AND group cap."""
        month = self._l.month()
        est = to_micro(estimate_usd)
        slot_cap = to_micro(policy.cap_usd)
        slot_spent = self.slot_spent_micro(policy.slot, month)
        slot_remaining = slot_cap - slot_spent
        group_spent = group_total = group_remaining = None
        if policy.group is not None and policy.group_total_usd is not None:
            group_total = to_micro(policy.group_total_usd)
            group_spent = self.group_spent_micro(policy.group, month)
            group_remaining = group_total - group_spent

        group_binds = group_remaining is not None and group_remaining < slot_remaining
        if group_binds:
            binding_cap, binding_spent, binding_remaining = (
                group_total, group_spent, group_remaining,
            )
        else:
            binding_cap, binding_spent, binding_remaining = (
                slot_cap, slot_spent, slot_remaining,
            )
        if est > slot_remaining or (group_remaining is not None and est > group_remaining):
            scope = "combined cap group" if group_binds else "slot"
            raise GenerationRefused(
                CAP_EXHAUSTED,
                policy.slot,
                f"estimated cost ${from_micro(est):.2f} exceeds the remaining "
                f"monthly budget for the {scope}",
                "Stop, wait for the next UTC month, or ask the operator to raise "
                "the cap in the binding's constraints.",
                spent_usd=from_micro(binding_spent),
                cap_usd=from_micro(binding_cap),
                remaining_usd=from_micro(binding_remaining),
            )
        return Authorization(
            billing_slot=policy.slot,
            month=month,
            spent_usd=from_micro(slot_spent),
            cap_usd=policy.cap_usd,
            remaining_usd=from_micro(slot_remaining),
            estimate_usd=estimate_usd,
            cap_group=policy.group,
            group_spent_usd=None if group_spent is None else from_micro(group_spent),
            group_total_usd=policy.group_total_usd,
        )

    def record(self, row: dict[str, Any], billing_slot: str) -> None:
        """Append one row, after the artifact has been received."""
        month = self._l.month()
        path = self._file(billing_slot, month)
        try:
            SpendLedger._ensure_dir(path.parent)
            payload = (json.dumps(row, sort_keys=True) + "\n").encode()
            fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, 0o600)
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            raise _unreadable(self._slot, f"write failed: {type(exc).__name__}") from None

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
