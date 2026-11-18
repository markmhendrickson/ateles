"""Bounded workers for synchronous calls whose transport timeout is not a wall clock."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


class Busy(Exception):
    """Every worker permit is held; no call was started."""


class BoundedCallPool:
    """Admit at most ``capacity`` calls; reclaim permits only when workers end."""

    def __init__(self, capacity: int, name: str) -> None:
        self.capacity = capacity
        self._permits = threading.BoundedSemaphore(capacity)
        self._name = name

    def call(
        self, fn: Callable[[], T], deadline: float,
        on_expire: Callable[[], None] | None = None,
    ) -> T:
        if not self._permits.acquire(blocking=False):
            raise Busy("all bounded call workers are in use")
        abandoned = threading.Event()
        outcome: dict[str, object] = {}
        completion_lock = threading.Lock()
        worker_done = False
        abort_done = True
        released = False

        def complete(*, worker: bool = False, abort: bool = False) -> None:
            nonlocal worker_done, abort_done, released
            with completion_lock:
                if worker:
                    worker_done = True
                if abort:
                    abort_done = True
                if worker_done and abort_done and not released:
                    released = True
                    self._permits.release()

        def run() -> None:
            try:
                if not abandoned.is_set() and time.monotonic() < deadline:
                    outcome["value"] = fn()
            except BaseException as exc:
                outcome["error"] = exc
            finally:
                complete(worker=True)

        try:
            worker = threading.Thread(target=run, name=f"{self._name}-call", daemon=True)
            worker.start()
        except BaseException:
            self._permits.release()
            raise
        worker.join(max(0.0, deadline - time.monotonic()))
        if worker.is_alive() or time.monotonic() > deadline:
            abandoned.set()
            if worker.is_alive() and on_expire is not None:
                with completion_lock:
                    needs_abort = not worker_done
                    if needs_abort:
                        abort_done = False

                if needs_abort:
                    def abort() -> None:
                        try:
                            on_expire()
                        finally:
                            complete(abort=True)

                    try:
                        threading.Thread(
                            target=abort, name=f"{self._name}-abort", daemon=True,
                        ).start()
                    except BaseException:
                        complete(abort=True)
                        raise
            raise TimeoutError("bounded call exceeded its wall-clock deadline") from None
        if "error" in outcome:
            raise outcome["error"]
        if "value" not in outcome:
            raise TimeoutError("bounded call deadline passed before work began")
        return outcome["value"]
