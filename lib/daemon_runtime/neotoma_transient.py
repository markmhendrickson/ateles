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
"""

from __future__ import annotations

import logging
import time
from typing import Callable, TypeVar

import httpx

log = logging.getLogger("daemon_runtime.neotoma_transient")

RETRY_PAUSES_SECONDS: tuple[float, ...] = (2.0, 5.0)

T = TypeVar("T")


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def is_transient(exc: BaseException) -> bool:
    """Timeouts and connection-level errors only; never an HTTP status."""
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError))


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
