"""Tell GitHub's temporary request limits apart from real refusals, and wait them out.

A burst of reads (several Apis restarts within minutes, each running its
startup sweeps) can trip GitHub's *secondary* rate limit while the primary
quota still has thousands of requests left. GitHub answers that with a 403 (or
a 429) carrying ``Retry-After`` and/or a "secondary rate limit" message. Read as
a plain failure, it made the startup panel sweep give up on a pull request
until the next restart.

`send_with_limit_retry` sends one request and, only when the answer is a
temporary limit, waits as GitHub asks (``Retry-After``) or for a bounded
backoff, then resends, at most `MAX_ATTEMPTS` times in total. A 403 with none
of those signals is a real permission refusal and is returned at once, never
retried. A limit whose ``Retry-After`` is longer than `MAX_WAIT_SECONDS` is
returned unchanged rather than retried early: the caller's own failure posture
applies and its next periodic pass tries again.

Logging names the status, the wait and the URL path only, never a header or a
token.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
MAX_WAIT_SECONDS = 60.0
BACKOFF_BASE_SECONDS = 5.0
_SECONDARY_MARKERS = ("secondary rate limit", "abuse detection")

# Patched by tests so a retry does not really wait.
_sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep


def _header(resp: Any, name: str) -> str:
    headers = getattr(resp, "headers", None) or {}
    try:
        value = headers.get(name)
        if value is None:
            value = headers.get(name.lower())
    except Exception:  # noqa: BLE001 — an odd headers object is no signal
        return ""
    return str(value or "").strip()


def _body_text(resp: Any) -> str:
    try:
        return str(getattr(resp, "text", "") or "").casefold()
    except Exception:  # noqa: BLE001
        return ""


def temporary_limit_wait(resp: Any, attempt: int) -> float | None:
    """Seconds to wait before resending *resp*'s request, or None when it is
    not a temporary limit.

    Temporary: a 429; or a 403 carrying ``Retry-After``, a secondary-limit
    message, or ``x-ratelimit-remaining: 0``. *attempt* (0-based) scales the
    bounded backoff used when GitHub names no wait. The returned wait may
    exceed `MAX_WAIT_SECONDS`; the caller decides not to retry then.
    """
    status = getattr(resp, "status_code", None)
    if status not in (403, 429):
        return None
    retry_after = _header(resp, "Retry-After")
    remaining = _header(resp, "x-ratelimit-remaining")
    secondary = any(marker in _body_text(resp) for marker in _SECONDARY_MARKERS)
    if status == 403 and not (retry_after or secondary or remaining == "0"):
        return None  # a real permission refusal
    if retry_after:
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            pass
    if remaining == "0" and not secondary:
        reset = _header(resp, "x-ratelimit-reset")
        try:
            return max(0.0, float(reset) - time.time())
        except ValueError:
            pass
    return min(MAX_WAIT_SECONDS, BACKOFF_BASE_SECONDS * (2 ** attempt))


def _path(url: str) -> str:
    try:
        return urlsplit(str(url)).path or str(url)
    except Exception:  # noqa: BLE001
        return "<url>"


async def send_with_limit_retry(
    send: Callable[..., Awaitable[Any]], url: str, **kwargs: Any
) -> Any:
    """``await send(url, **kwargs)``, resent while GitHub answers a temporary limit.

    Returns the last response, whatever its status: the caller keeps its own
    ``raise_for_status`` / failure posture. Never sleeps longer than
    `MAX_WAIT_SECONDS` in one wait, and sends at most `MAX_ATTEMPTS` times.
    """
    resp = await send(url, **kwargs)
    for attempt in range(MAX_ATTEMPTS - 1):
        wait = temporary_limit_wait(resp, attempt)
        if wait is None:
            return resp
        status = getattr(resp, "status_code", None)
        if wait > MAX_WAIT_SECONDS:
            log.warning(
                f"[github_rate_limit] GET {_path(url)}: GitHub limit ({status}) asks "
                f"for {wait:.0f}s, over the {MAX_WAIT_SECONDS:.0f}s bound — not "
                "retrying now; the next periodic pass will"
            )
            return resp
        log.warning(
            f"[github_rate_limit] GET {_path(url)}: GitHub temporary limit "
            f"({status}) — waiting {wait:.1f}s, then retry "
            f"{attempt + 2}/{MAX_ATTEMPTS}"
        )
        await _sleep(wait)
        resp = await send(url, **kwargs)
    if temporary_limit_wait(resp, MAX_ATTEMPTS) is not None:
        log.warning(
            f"[github_rate_limit] GET {_path(url)}: still limited after "
            f"{MAX_ATTEMPTS} attempts ({getattr(resp, 'status_code', None)})"
        )
    return resp
