"""
lib/neotoma_concurrency.py — cap concurrent Neotoma readers across the swarm.

WHY
---
During the 2026-09-01 degradation the same `limit:1` query measured 16s, then
90s, then 100s, then timed out. The variable that tracked was **agent
concurrency**, not time of day and not data volume: roughly 13 agents were
querying the instance at once, alongside a dev server and the operator's own UI.

That load is the swarm's own doing, which makes shedding it the one remedy that
needs no infrastructure change, costs nothing, is instantly reversible, and
cannot lose data. Scaling the instance is the operator's call (it costs money);
restarting it is dangerous under saturation. Backing off is ours.

WHAT THIS IS NOT
----------------
This is a **process-local** semaphore, and it is important to be honest about
that: it caps concurrency within one Python process, not across the ~13
independent agent processes that actually caused the incident. A cross-process
cap needs a shared lease (a file lock or a Neotoma-side limit), and a
Neotoma-side limit is unavailable precisely when it is most needed.

So this bounds each daemon's own fan-out — the realistic near-term win, since a
single daemon issuing parallel queries is a common pattern here — and gives a
single place to add a file-lock backend later. The system-level cap belongs in
Neotoma itself (see neotoma#2217: a 2-worker reader pool with no statement
timeout means a couple of slow queries block everything).

RECOMMENDED CAP
---------------
Default 4 concurrent readers per process. Against 2 vCPU that keeps the
instance busy without queueing work behind a saturated reader pool; 13
concurrent readers against that instance is plainly too many, and was.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager

DEFAULT_MAX_CONCURRENT_READS = 4


def _cap() -> int:
    try:
        v = int(os.environ.get("NEOTOMA_MAX_CONCURRENT_READS", "") or DEFAULT_MAX_CONCURRENT_READS)
    except ValueError:
        return DEFAULT_MAX_CONCURRENT_READS
    return max(1, v)


_lock = threading.Lock()
_semaphore: threading.BoundedSemaphore | None = None
_semaphore_cap: int | None = None


def _get_semaphore() -> threading.BoundedSemaphore:
    global _semaphore, _semaphore_cap
    with _lock:
        cap = _cap()
        if _semaphore is None or _semaphore_cap != cap:
            _semaphore = threading.BoundedSemaphore(cap)
            _semaphore_cap = cap
        return _semaphore


@contextmanager
def neotoma_read_slot(timeout: float | None = None):
    """Hold a reader slot for the duration of a Neotoma read.

    Fails **open** on timeout rather than raising: a concurrency limiter that
    starts throwing during an incident converts a slow system into a broken one,
    and every caller here already handles slow reads. Yields True if a slot was
    acquired, False if it proceeded without one.
    """
    sem = _get_semaphore()
    acquired = sem.acquire(timeout=timeout) if timeout is not None else sem.acquire()
    try:
        yield bool(acquired)
    finally:
        if acquired:
            try:
                sem.release()
            except ValueError:
                # BoundedSemaphore raises if released more times than acquired;
                # a cap change mid-flight can cause that. Never propagate.
                pass
