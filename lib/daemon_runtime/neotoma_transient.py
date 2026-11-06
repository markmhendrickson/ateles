"""Bounded retry for TRANSIENT Neotoma transport failures.

Used by the fail-closed read-backs and provenance writes that gate a spawn.  A
single slow response (a read timeout or a dropped connection) must not kill an
approved delivery, but nothing else is softened:

* only timeouts and connection-level errors are retried -- an HTTP status
  response (any 4xx, and 5xx too: no evidence a 5xx is transient here) is
  raised on the first attempt, as is any non-transport error;
* at most two further attempts, after pauses of 2s then 5s, so the worst case
  added to one call is 7s of sleeping plus the extra attempts' own time limits;
* when every attempt fails the LAST exception is re-raised, so the caller's
  existing handler produces exactly today's failure result.

The callable is re-run unchanged, so a write carries the same body (and so the
same idempotency key) on every attempt.

``acall_with_transient_retry`` is the async form, for the large Apis
``pr_review`` store.  It uses the same pauses and the same classification,
plus two opt-ins that the gating paths above do not take: ``retry_status``
also retries HTTP 429 and 5xx (never any other 4xx), and ``total_seconds``
caps the whole call -- each attempt's time limit is shortened to fit what is
left, and no pause starts that would end past the cap.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, TypeVar

import httpx

log = logging.getLogger("daemon_runtime.neotoma_transient")

RETRY_PAUSES_SECONDS: tuple[float, ...] = (2.0, 5.0)

T = TypeVar("T")


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


async def _asleep(seconds: float) -> None:
    await asyncio.sleep(seconds)


def _monotonic() -> float:
    return time.monotonic()


def describe(exc: BaseException) -> str:
    """Type and text, so an exception with an empty message is not blank."""
    text = str(exc)
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def is_transient(exc: BaseException) -> bool:
    """Timeouts and connection-level errors only; never an HTTP status."""
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))


def is_transient_status(exc: BaseException) -> bool:
    """HTTP 429 or any 5xx; every other status (and every non-HTTP error) is not."""
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    code = exc.response.status_code
    return code == 429 or 500 <= code <= 599


def call_with_transient_retry(fn: Callable[[], T], *, label: str) -> T:
    attempts = len(RETRY_PAUSES_SECONDS) + 1
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 -- classified below, re-raised
            if not is_transient(exc) or attempt == attempts - 1:
                raise
            pause = RETRY_PAUSES_SECONDS[attempt]
            log.warning(
                "[neotoma] transient failure on %s (%s); retry %d/%d in %.0fs",
                label,
                exc or type(exc).__name__,
                attempt + 1,
                attempts - 1,
                pause,
            )
            _sleep(pause)
    raise AssertionError("unreachable")  # pragma: no cover


async def acall_with_transient_retry(
    fn: Callable[[float], Awaitable[T]],
    *,
    label: str,
    attempt_timeout: float,
    total_seconds: float,
    retry_status: bool = False,
) -> T:
    """Await ``fn(time_limit)`` with the bounded retry described above.

    ``fn`` receives the time limit for that attempt, at most
    ``attempt_timeout`` and never past ``total_seconds`` from the first call,
    and is cancelled if it runs past that limit (a retryable timeout).
    When the attempts or the total bound are spent, the last exception is
    re-raised unchanged.
    """
    deadline = _monotonic() + total_seconds
    attempts = len(RETRY_PAUSES_SECONDS) + 1
    for attempt in range(attempts):
        limit = min(attempt_timeout, deadline - _monotonic())
        try:
            # The client's own timeout applies per connect/read/write phase, so
            # a slow server could stretch one attempt past ``limit``; this cap
            # is what makes the per-attempt and total bounds hold.
            return await asyncio.wait_for(fn(limit), timeout=limit)
        except Exception as exc:  # noqa: BLE001 -- classified below, re-raised
            retryable = (
                is_transient(exc)
                or isinstance(exc, asyncio.TimeoutError)
                or (retry_status and is_transient_status(exc))
            )
            if not retryable or attempt == attempts - 1:
                raise
            pause = RETRY_PAUSES_SECONDS[attempt]
            if _monotonic() + pause >= deadline:
                log.warning(
                    "[neotoma] transient failure on %s (%s); total bound of "
                    "%.0fs spent, not retrying",
                    label,
                    describe(exc),
                    total_seconds,
                )
                raise
            log.warning(
                "[neotoma] transient failure on %s (%s); retry %d/%d in %.0fs",
                label,
                describe(exc),
                attempt + 1,
                attempts - 1,
                pause,
            )
            await _asleep(pause)
    raise AssertionError("unreachable")  # pragma: no cover
