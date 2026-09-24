"""Shared adapter runtime — admission obligations 1, 4, and 5 by construction.

Adapters call these APIs rather than reimplementing drop counting, coverage
stamping, or read-back-before-ack. See docs/foundation/adapters.md#the-admission-contract.
"""

from __future__ import annotations

import os
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any

import httpx

from .types import (
    AdapterBoundaryError,
    DeliveryIdError,
    Disposition,
    DropReason,
    HaltError,
    Obligation4Error,
    Observation,
    ProvenanceError,
    ReadbackError,
    UserIdWidenError,
)

# Fixed-interval wall-clock bucket for drop windows (obligation 1). Inline —
# do not import swarm_dispatch "window" helpers.
_WINDOW_SECONDS = 60

_BANNED_LOCAL_STATE = frozenset(
    {
        "cursor",
        "sync_log",
        "last_seen",
        "artifact_cache",
        "local_artifact_cache",
    }
)

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")


def current_window(*, now: float | None = None) -> str:
    """Minimal fixed-interval wall-clock bucket id (seconds since epoch // interval)."""
    ts = time.time() if now is None else now
    return str(int(ts // _WINDOW_SECONDS))


class DropCounter:
    """In-process drop counts keyed ``(tenant_or_user, adapter_id, window)``.

    Process-local for this issue (not Neotoma-hosted). Tenant is always part of
    the key — never ``(adapter_id, window)`` alone.
    """

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str, str], dict[str, int]] = defaultdict(
            lambda: defaultdict(int)
        )

    def record(
        self,
        reason: DropReason | str,
        *,
        tenant_or_user: str,
        adapter_id: str,
        window: str | None = None,
    ) -> str:
        """Increment the counter for ``reason`` in the current (or given) window.

        Returns the window id used.
        """
        win = window if window is not None else current_window()
        key = (tenant_or_user, adapter_id, win)
        reason_key = reason.value if isinstance(reason, DropReason) else str(reason)
        self._counts[key][reason_key] += 1
        return win

    def read(
        self,
        window: str,
        *,
        tenant_or_user: str,
        adapter_id: str,
        reason: DropReason | str | None = None,
    ) -> int:
        """Return drop count for the window (optionally filtered by reason)."""
        key = (tenant_or_user, adapter_id, window)
        bucket = self._counts.get(key)
        if not bucket:
            return 0
        if reason is None:
            return sum(bucket.values())
        reason_key = reason.value if isinstance(reason, DropReason) else str(reason)
        return int(bucket.get(reason_key, 0))


def admit(
    event: Any,
    mapping_fn: Callable[[Any], Disposition | None],
    *,
    counter: DropCounter,
    adapter_id: str,
    tenant_or_user: str,
    window: str | None = None,
) -> Disposition:
    """Map an inbound event to a disposition (obligation 1).

    On no match: record ``dropped``/``unmapped``, increment the counter for the
    current window, return ``Disposition.DROPPED`` without calling any write path.
    Mapped path returns the mapped non-dropped disposition and does not increment.
    """
    mapped = mapping_fn(event)
    if mapped is None or mapped is Disposition.DROPPED:
        counter.record(
            DropReason.UNMAPPED,
            tenant_or_user=tenant_or_user,
            adapter_id=adapter_id,
            window=window,
        )
        return Disposition.DROPPED
    return mapped


def _default_store_fn(body: dict) -> dict | None:
    """Auth-scoped POST /store — same env/httpx pattern as session_finalize._post_store.

    Never accepts a caller ``user_id`` widen: the bearer token alone scopes the write.
    """
    if not NEOTOMA_BEARER_TOKEN:
        raise AdapterBoundaryError(
            "missing_bearer",
            "NEOTOMA_BEARER_TOKEN not set — cannot POST /store without process auth.",
        )
    resp = httpx.post(
        f"{NEOTOMA_BASE_URL.rstrip('/')}/store",
        headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
        json=body,
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def write_observation(
    obs: Observation,
    *,
    store_fn: Callable[[dict], Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Persist an observation with obligatory provenance (obligation 4).

    Refuses missing ``source`` / ``sourced_time`` / ``coverage`` / ``delivery_id``.
    Rejects banned local-state kwargs (``cursor``, ``sync_log``, ``last_seen``,
    artifact cache). Never invents a sync log, cursor table, or local artifact
    cache. Never accepts caller ``user_id`` widen. Stamps
    ``idempotency_key=obs.delivery_id``.
    """
    for banned in _BANNED_LOCAL_STATE:
        if banned in kwargs:
            raise Obligation4Error(banned)
    if "user_id" in kwargs:
        raise UserIdWidenError()

    missing: list[str] = []
    if not getattr(obs, "source", None):
        missing.append("source")
    if not getattr(obs, "sourced_time", None):
        missing.append("sourced_time")
    if getattr(obs, "coverage", None) is None:
        missing.append("coverage")
    if missing:
        raise ProvenanceError(missing)
    if not getattr(obs, "delivery_id", None):
        raise DeliveryIdError()

    body: dict[str, Any] = {
        "entities": [
            {
                "entity_type": "observation",
                "source": obs.source,
                "sourced_time": obs.sourced_time,
                "coverage": {
                    "start": obs.coverage.start,
                    "end": obs.coverage.end,
                    "got": obs.coverage.got,
                },
                "payload": obs.payload,
                "delivery_id": obs.delivery_id,
            }
        ],
        "idempotency_key": obs.delivery_id,
    }
    fn = store_fn if store_fn is not None else _default_store_fn
    return fn(body)


def commit(
    *,
    write_fn: Callable[[], Any],
    readback_fn: Callable[[], bool],
    halt_check: Callable[[], bool] | None = None,
) -> None:
    """Write then read back; halt writes and acks nothing (obligation 5).

    If ``halt_check`` is true or raises: call neither ``write_fn`` nor
    ``readback_fn``, raise ``HaltError``. Else call ``write_fn``, then
    ``readback_fn``; if read-back does not confirm, raise ``ReadbackError``.
    Success returns only after confirmed read-back — caller must not ack earlier.
    """
    if halt_check is not None:
        try:
            halted = halt_check()
        except Exception as exc:  # noqa: BLE001 — any raise from halt_check is halt
            raise HaltError(hint=f"Halt check raised: {exc}") from exc
        if halted:
            raise HaltError()

    write_fn()
    confirmed = readback_fn()
    if not confirmed:
        raise ReadbackError()
