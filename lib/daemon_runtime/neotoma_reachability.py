"""Neotoma is a hard dependency: when the record is unreachable, the swarm halts.

The decision this implements
----------------------------

Operator, 2026-09-02 (task ent_670cacab2f46fd9547ced7ed, approved): *"Swarm
should not keep working during a Neotoma outage."* Not degraded operation, not
a hardcoded fallback sequence.

The reason is stronger than availability. A swarm that operates while its record
is unreachable produces **work with no record**. On 2026-09-01/02 agents
completed real work whose task entities never landed; it was salvaged only
because the operator relayed it into GitHub by hand. Across 18 unattended
daemons that is unaccountable work — worse than the work not happening, because
the swarm then acts on a history it cannot reconstruct.

Why the probe is a real read and never `/health`
------------------------------------------------

`/health` reads one small file synchronously and returns; it touches no
database. That is exactly what makes it useless as the gate here: a Neotoma
instance returns a green `/health` while every read hangs on a wedged DB. That
was observed directly, and `lib/neotoma_forensics.py` documents the same
asymmetry from the other side — it times `/health` *from inside the machine*
precisely because a fast `/health` next to hanging reads localises the fault to
a blocked event loop.

So the probe issues the cheapest query that actually traverses the read path
(`POST /entities/query`, `limit: 1`). A green verdict from this probe means the
record can be *read*, which is the only property dispatch depends on.

"Unreachable" is not "slow"
---------------------------

Neotoma answered in 20-30s with intermittent 502s under retry pressure — which
is to say: retrying harder is how *slow* becomes *unreachable*. Two mechanisms
keep the distinction:

  * A response that arrives — even a slow one, even a 502 — is evidence the
    server is alive. `SLOW` is a distinct verdict from `UNREACHABLE`, is
    reported, and does **not** halt.
  * A single failure never halts. `FAILURES_BEFORE_HALT` consecutive failures
    are required, and the probe is rate-limited by `PROBE_INTERVAL_SECONDS`
    (cached between calls) so a burst of dispatches issues one probe, not one
    per task. That cache is the backoff: the check cannot itself become the
    retry pressure that manufactures the outage it is testing for.

Recovery is deliberately asymmetric with entry: one good read clears the halt.
Staying halted after the record demonstrably answers would be its own outage.

Halt work, never stop observing
-------------------------------

This module gates *dispatch* — the point where the swarm decides to do work it
will need to record. It gates nothing else. Watchdogs keep sweeping, forensics
keeps capturing, health checks keep probing, and the notifier keeps delivering.
A hard dependency that stops the thing diagnosing the dependency makes recovery
impossible; a diagnostic capture asserts nothing about the record, so it does
not require the record. While halted, `TaskWatchdog.sweep` re-probes
(`probe(force=True)`) so recovery and drain do not depend on dispatch/SSE
traffic.

The halt announces itself off-Neotoma
-------------------------------------

A silently halted swarm is indistinguishable from an idle one — this codebase's
signature failure (#583/#636). `announce()` sends on ENTERING and on LEAVING the
halted state and **only** on those two transitions, so a halt that blocks two
hundred dispatches pages twice, not two hundred times. `lib/notify` has no rate
limiting of its own (#645), so the edge-trigger here *is* the rate limiting.
Delivery rides the existing `lib/notify` Telegram path rather than a new one.

Guarding the guard
------------------

`probe()` catches transport exceptions deliberately — an unreachable server
*is* an exception — and converts them into a verdict. It does not wrap the
caller's dispatch. `HaltedError` is raised by `raise_if_halted()` outside any
except-block in this module, so nothing here can catch the abort it exists to
raise.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

import httpx

log = logging.getLogger("neotoma.reachability")

NEOTOMA_BASE_URL = os.environ.get(
    "NEOTOMA_BASE_URL", "https://neotoma.markmhendrickson.com"
)
NEOTOMA_BEARER_TOKEN = os.environ.get("NEOTOMA_BEARER_TOKEN", "")

# How long one probe may take before the record counts as unreachable. Generous
# on purpose: Neotoma answering in 20-30s is degraded, not down, and a tight
# timeout would convert every slow period into a full swarm halt.
PROBE_TIMEOUT_SECONDS = float(os.environ.get("NEOTOMA_PROBE_TIMEOUT_SECONDS", "30"))
# A response slower than this is reported as SLOW — visible, but not a halt.
SLOW_THRESHOLD_SECONDS = float(os.environ.get("NEOTOMA_PROBE_SLOW_SECONDS", "10"))
# Minimum spacing between real network probes. A burst of dispatches shares one
# verdict; this is what stops the check becoming its own retry storm.
PROBE_INTERVAL_SECONDS = float(os.environ.get("NEOTOMA_PROBE_INTERVAL_SECONDS", "30"))
# Consecutive failures required before the swarm halts. One blip is not an
# outage; 502s were observed intermittently while the server was otherwise fine.
FAILURES_BEFORE_HALT = max(1, int(os.environ.get("NEOTOMA_PROBE_FAILURES_BEFORE_HALT", "3")))

# Escape hatch for a deliberate operator override (e.g. a drill, or recovering
# from a probe bug). Default off: the halt is the safe state.
HALT_DISABLED = os.environ.get("ATELES_DISABLE_NEOTOMA_HALT", "").strip() in {"1", "true", "yes"}


class Reachability(str, Enum):
    OK = "ok"                    # read path answered promptly
    SLOW = "slow"                # answered, but degraded — reported, does NOT halt
    UNREACHABLE = "unreachable"  # no answer, or an error where the read should be


class HaltedError(RuntimeError):
    """Raised at dispatch when Neotoma is unreachable.

    Carries the reason so the caller can report *why* it refused rather than
    reporting a generic failure — a halt indistinguishable from a crash is the
    silence failure again, one layer down.
    """

    def __init__(self, reason: str, consecutive_failures: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.consecutive_failures = consecutive_failures


@dataclass
class ProbeResult:
    verdict: Reachability
    elapsed_seconds: float
    detail: str = ""

    @property
    def reachable(self) -> bool:
        """SLOW counts as reachable. Degraded is not down."""
        return self.verdict in (Reachability.OK, Reachability.SLOW)


def _real_read(timeout: float) -> ProbeResult:
    """The probe itself: the cheapest query that actually traverses the read path.

    NOT `/health` — that returns green while every read hangs on a wedged DB.
    """
    started = time.monotonic()
    if not NEOTOMA_BEARER_TOKEN:
        # No token is a configuration fault, not an outage. Reporting it as
        # UNREACHABLE would halt the whole swarm on a misconfigured env var and
        # blame the server; say so plainly instead and let dispatch proceed.
        return ProbeResult(
            Reachability.OK, 0.0,
            "no bearer token — reachability unverified, not halting on config",
        )
    try:
        resp = httpx.post(
            f"{NEOTOMA_BASE_URL}/entities/query",
            headers={"Authorization": f"Bearer {NEOTOMA_BEARER_TOKEN}"},
            json={"entity_type": "task", "limit": 1},
            timeout=timeout,
        )
        elapsed = time.monotonic() - started
        # An arriving response — even a 5xx — proves the server is alive.
        # raise_for_status() would map that to UNREACHABLE and halt under
        # intermittent 502 pressure; classify by status before treating errors.
        if resp.status_code >= 500:
            return ProbeResult(
                Reachability.SLOW,
                elapsed,
                f"HTTP {resp.status_code} — server answered (degraded)",
            )
        if resp.status_code >= 400:
            # Auth/config/client fault: same spirit as a missing bearer —
            # do not halt the swarm and blame the server.
            return ProbeResult(
                Reachability.OK,
                elapsed,
                f"HTTP {resp.status_code} — config/client fault, not halting",
            )
        # A 2xx whose body is not a readable envelope means the read path
        # answered with something other than the record. Treat it as an outage:
        # the point of a real read is that it proves reads work.
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return ProbeResult(
                Reachability.UNREACHABLE, elapsed, "read returned a non-JSON body"
            )
        if not isinstance(data, dict):
            return ProbeResult(
                Reachability.UNREACHABLE, elapsed, "read returned an unexpected shape"
            )
        if elapsed >= SLOW_THRESHOLD_SECONDS:
            return ProbeResult(
                Reachability.SLOW, elapsed, f"read answered in {elapsed:.1f}s (degraded)"
            )
        return ProbeResult(Reachability.OK, elapsed, f"read answered in {elapsed:.1f}s")
    except Exception as exc:  # noqa: BLE001 — an unreachable server IS an exception
        elapsed = time.monotonic() - started
        return ProbeResult(
            Reachability.UNREACHABLE, elapsed, f"{type(exc).__name__}: {str(exc)[:160]}"
        )


@dataclass
class ReachabilityGate:
    """Cached, edge-triggered reachability state shared by every dispatch.

    One instance per daemon process (see `shared_gate()`). Thread-safe: Apis's
    watchdog, reconciler and SSE loop all consult it.
    """

    # Injectable so tests can simulate an unreachable Neotoma without a
    # network. Takes a timeout, returns a ProbeResult.
    probe_fn: Callable[[float], ProbeResult] = _real_read
    failures_before_halt: int = FAILURES_BEFORE_HALT
    probe_interval_seconds: float = PROBE_INTERVAL_SECONDS
    probe_timeout_seconds: float = PROBE_TIMEOUT_SECONDS

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _consecutive_failures: int = 0
    _halted: bool = False
    _last_probe_ts: float = 0.0
    _last_result: ProbeResult | None = None
    # Set by announce(); lets a caller see whether the operator has been told.
    _announced_halt: bool = False

    # ── state ────────────────────────────────────────────────────────────────

    @property
    def halted(self) -> bool:
        with self._lock:
            return self._halted

    @property
    def last_result(self) -> ProbeResult | None:
        with self._lock:
            return self._last_result

    # ── probing ──────────────────────────────────────────────────────────────

    def probe(self, now: float | None = None, force: bool = False) -> ProbeResult:
        """Return the current verdict, reusing a recent one unless `force`.

        The cache is load-bearing, not an optimisation: it is what keeps a burst
        of dispatches from becoming the retry pressure that turns slow into
        unreachable.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            cached = self._last_result
            fresh = (now - self._last_probe_ts) < self.probe_interval_seconds
            if cached is not None and fresh and not force:
                return cached

        result = self.probe_fn(self.probe_timeout_seconds)

        with self._lock:
            self._last_probe_ts = now
            self._last_result = result
            if result.reachable:
                self._consecutive_failures = 0
                self._halted = False
            else:
                self._consecutive_failures += 1
                if self._consecutive_failures >= self.failures_before_halt:
                    self._halted = True
        return result

    def raise_if_halted(self, now: float | None = None) -> None:
        """Probe, and abort the caller when the record is unreachable.

        Deliberately structured so the raise happens OUTSIDE any except-block in
        this module: a broad try/except around the check would swallow the very
        abort the check exists to raise.
        """
        result = self.probe(now=now)
        if HALT_DISABLED:
            if not result.reachable:
                log.error(
                    "[reachability] Neotoma UNREACHABLE (%s) but "
                    "ATELES_DISABLE_NEOTOMA_HALT is set — proceeding; work done "
                    "now may not be recorded",
                    result.detail,
                )
            return
        with self._lock:
            halted = self._halted
            failures = self._consecutive_failures
            detail = self._last_result.detail if self._last_result else ""
        if halted:
            raise HaltedError(
                f"Neotoma unreachable after {failures} consecutive probe failures "
                f"({detail}) — refusing to dispatch work that could not be recorded",
                failures,
            )

    # ── announcement (edge-triggered) ────────────────────────────────────────

    def announce(self, notifier) -> None:
        """Page the operator on ENTERING and LEAVING the halt, and only then.

        This edge-trigger is the rate limiting: `lib/notify` has none of its own
        (#645), so announcing per blocked dispatch would page once per task.
        Fail-open — a notifier error must never convert a halt into a crash.
        """
        with self._lock:
            halted = self._halted
            already = self._announced_halt
            detail = self._last_result.detail if self._last_result else ""
            failures = self._consecutive_failures
            if halted == already:
                return  # no edge — stay quiet
            self._announced_halt = halted

        try:
            from lib.notify import Priority

            if halted:
                # Structure fixed for Accipiter UX DoD; [COPY:…] slots are for
                # Paradisaea final wording — do not invent polished prose here.
                notifier.send(
                    "[COPY: SWARM HALTED — Neotoma unreachable / hard-stop title]\n"
                    f"  {failures} consecutive failed reads ({detail}).\n"
                    "  [COPY: what stopped]: dispatch refuses new work; "
                    "completions that cannot be recorded are not claimed DONE.\n"
                    "  [COPY: what still runs]: watchdogs, forensics, alerting.\n"
                    "\n"
                    "  Next:\n"
                    f"  1. [COPY: inspect failed read path] — "
                    f"POST {NEOTOMA_BASE_URL}/entities/query (never /health).\n"
                    "  2. [COPY: inspect Apis daemon logs on the live checkout] — "
                    "e.g. ~/ateles-rc-src / Apis process logs for "
                    "`[reachability]` / `HALTED — refusing to dispatch`.\n"
                    "  3. [COPY: inspect forensic capture dir] — "
                    "$NEOTOMA_FORENSICS_DIR or "
                    "~/.local/state/ateles/neotoma-forensics "
                    "(lib/neotoma_forensics.py).\n"
                    "  4. [COPY: distinguish outage vs config] — empty "
                    "NEOTOMA_BEARER_TOKEN does not halt; token/auth/config "
                    f"faults show differently from transport/timeout in ({detail}).\n"
                    "  5. [COPY: what deferred tasks do] — left in prior status; "
                    "drain after a successful re-probe clears the halt "
                    "(TaskWatchdog.sweep calls probe(force=True) while halted).\n"
                    "  6. [COPY: recovery condition] — one successful real read "
                    "clears the halt; resume pages on that edge.\n"
                    "  7. [COPY: override — last resort only] — "
                    "ATELES_DISABLE_NEOTOMA_HALT=1|true|yes forces proceed while "
                    "unreachable; work done then may not be recorded. Default off.",
                    priority=Priority.CRITICAL,
                    handler="neotoma-reachability",
                )
            else:
                notifier.send(
                    "[COPY: Swarm resumed — Neotoma reachable again.]\n"
                    f"  {detail}\n"
                    "  [COPY: deferred work drains after the gate clears / "
                    "via watchdog sweep].",
                    priority=Priority.BLOCKER,
                    handler="neotoma-reachability",
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("[reachability] halt announcement failed: %s", exc)

    # ── test seam ────────────────────────────────────────────────────────────

    def reset(self) -> None:
        with self._lock:
            self._consecutive_failures = 0
            self._halted = False
            self._last_probe_ts = 0.0
            self._last_result = None
            self._announced_halt = False


_shared: ReachabilityGate | None = None
_shared_lock = threading.Lock()


def shared_gate() -> ReachabilityGate:
    """Process-wide gate, so every dispatch path shares one verdict and one page."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = ReachabilityGate()
        return _shared
