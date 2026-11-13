"""One paced background queue for Apis's ``harness_event`` audit stores.

An audit row records work; it must never delay that work. When the Neotoma
instance is slow to accept writes, a burst of audit stores issued inline
both holds up the dispatch or webhook handler that awaits them and lands
on the server all at once, where each one can time out. This queue fixes
both:

* ``emit`` never waits on the store. It puts the job on a bounded queue and
  returns at once.
* A single worker runs the jobs one at a time, starting each one at least
  ``MIN_GAP_SECONDS`` after the previous one finished.
* The queue holds at most ``MAX_PENDING`` jobs. A job that arrives when it
  is full is dropped with a warning naming its label, so a long outage
  cannot grow memory without bound.

Pacing is something the Apis daemon opts into: its ``main`` calls
``enable()``. Every other process that runs the same code (the
``dispatch_role`` CLI, other daemons, evals, tests) keeps today's
behaviour, where ``emit`` awaits the job inline. A short-lived process would
otherwise exit with rows still queued and lose them.

Shutdown: the worker is a plain task on the daemon's loop. When the loop is
cancelled (``asyncio.run`` at exit), the worker logs how many rows were
still waiting and stops. Those rows are not written, the same outcome as an
inline store cut off by the same shutdown. A SIGTERM that kills the process
loses them without the log line.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

log = logging.getLogger("apis.audit_queue")

# Seconds between the end of one audit store and the start of the next.
MIN_GAP_SECONDS = 3.0
# Jobs waiting beyond the one in flight. Past this, new rows are dropped.
MAX_PENDING = 100

Job = Callable[[], Awaitable[object]]


class PacedAuditQueue:
    """A bounded FIFO run by one worker, with a minimum gap between jobs."""

    def __init__(
        self,
        *,
        min_gap_seconds: float = MIN_GAP_SECONDS,
        max_pending: int = MAX_PENDING,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self.min_gap_seconds = min_gap_seconds
        self.max_pending = max_pending
        self._clock = clock
        self._sleep = sleep
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[tuple[Job, str]] | None = None
        self._worker: asyncio.Task | None = None
        self._last_finished: float | None = None
        self.dropped = 0

    def pending(self) -> int:
        """Jobs waiting to start (the one in flight is not counted)."""
        return self._queue.qsize() if self._queue is not None else 0

    def submit(self, job: Job, *, label: str) -> bool:
        """Queue ``job`` without waiting; False when it was dropped.

        Must be called on a running event loop. The queue binds to that loop
        on first use; a later loop (a new ``asyncio.run``) gets a fresh queue.
        """
        loop = asyncio.get_running_loop()
        if self._loop is not loop or self._queue is None:
            self._loop = loop
            self._queue = asyncio.Queue(maxsize=self.max_pending)
            self._worker = None
            self._last_finished = None
        if self._worker is None or self._worker.done():
            self._worker = loop.create_task(
                self._work(self._queue), name="apis-audit-queue"
            )
        try:
            self._queue.put_nowait((job, label))
        except asyncio.QueueFull:
            self.dropped += 1
            log.warning(
                "[apis] audit queue full (%d waiting); dropped %s",
                self.max_pending,
                label,
            )
            return False
        return True

    async def join(self) -> None:
        """Wait until every queued job has run. For tests and orderly exits."""
        if self._queue is not None:
            await self._queue.join()

    async def _work(self, queue: asyncio.Queue[tuple[Job, str]]) -> None:
        try:
            while True:
                job, label = await queue.get()
                try:
                    if self._last_finished is not None:
                        wait = self._last_finished + self.min_gap_seconds - self._clock()
                        if wait > 0:
                            await self._sleep(wait)
                    try:
                        await job()
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 — an audit row never kills the worker
                        log.warning(
                            "[apis] audit store %s failed: %s: %s",
                            label,
                            type(exc).__name__,
                            exc,
                        )
                    finally:
                        self._last_finished = self._clock()
                finally:
                    queue.task_done()
        except asyncio.CancelledError:
            waiting = queue.qsize()
            if waiting:
                log.warning(
                    "[apis] audit queue stopped with %d row(s) not written",
                    waiting,
                )
            raise


_active: PacedAuditQueue | None = None


def enable(queue: PacedAuditQueue | None = None) -> PacedAuditQueue:
    """Turn pacing on for this process (the Apis daemon's ``main``)."""
    global _active
    _active = queue or PacedAuditQueue()
    return _active


def disable() -> None:
    """Turn pacing off; ``emit`` awaits jobs inline again."""
    global _active
    _active = None


def active() -> PacedAuditQueue | None:
    return _active


async def emit(job: Job, *, label: str) -> None:
    """Run an audit store: queued when pacing is on, inline otherwise.

    Inline, ``job`` is awaited and any exception reaches the caller, exactly
    as the direct call it replaces. Queued, this returns at once and the
    worker logs any failure.
    """
    queue = _active
    if queue is None:
        await job()
        return
    queue.submit(job, label=label)
