#!/usr/bin/env python3
"""
Nyctea — Neotoma degradation watchdog.

Nyctea scandiaca, the snowy owl: hunts by patience, and is diurnal — it watches
when the rest of the swarm is awake and making the load.

WHY THIS EXISTS
---------------
On 2026-09-01 the operator observed: "it looks to me like it's not down, but
it's highly degraded currently." Measured against the hosted instance minutes
later, three consecutive times:

    GET  /health                    -> 200 in 0.89s
    POST /entities/query {limit:1}  -> timeout at 45s

Nothing noticed. The swarm's entire durable memory was unreadable and every
liveness check in the system was green, because `/health` never touches the
database (ateles#577). The outage was found only because a human asked why the
UI felt slow.

Nyctea judges Neotoma by whether it can serve a real read. See probe.py for the
verdict vocabulary and thresholds.

RECOVERY — WHAT IS AUTOMATED AND WHAT IS DELIBERATELY NOT
---------------------------------------------------------
Automated: **nothing that mutates infrastructure.** This is a considered
position, not an unfinished one.

  * Restart is NOT automated. Under SATURATED the instance is alive and
    working; a restart drops every in-flight write — including partially
    applied stores — to fix a queue that refills in seconds. Restarting a
    *wedged* instance is defensible, but WEDGED and SATURATED are only
    distinguishable by a liveness signal that is itself flapping under load,
    which is exactly the condition in which an automated restarter would
    misfire. Fly's own restart policy is `always` and the machine has already
    self-restarted twice (exit_code=134 — SIGABRT, the V8 fatal-error path) on
    2026-08-31 without operator involvement; adding a second, dumber restarter
    on top of a working one buys nothing and races it.

  * Scaling is NOT automated. It costs the operator money, so it is a decision
    to present with a number, never one to take.

Load shedding helper: `lib/neotoma_concurrency.py` provides a process-local
reader semaphore for daemons to adopt (not wired by Nyctea itself). Nyctea
additionally publishes its verdict to a state file that a cooperating agent can
read to back off.

So Nyctea DETECTS and ESCALATES reliably. A watchdog that does that is worth
more than one that restarts unsafely.

QUIET HOURS — WHY THIS BYPASSES THEM
------------------------------------
It escalates at Priority.CRITICAL, which `lib/notify` delivers immediately even
inside the silence window.

That is a deliberate choice against a known failure: 34 escalations were queued
into a quiet-hours digest overnight and ZERO digest sends fired, so the operator
was never told about 12 stalled PRs (ateles#626/#627). A watchdog that queued
into that same channel would reproduce the exact failure it exists to prevent.

The justification for the bypass is specific rather than "this alert feels
important": while Neotoma is unreadable, **every other alerting path in the
swarm is also degraded**, because daemons resolve their config, their rubric and
their escalation targets from Neotoma. A Neotoma outage is the one condition
under which silence cannot be interpreted as "nothing is wrong" — it is
indistinguishable from "everything is wrong and nothing can say so". Deferring
that to 08:30 means the swarm runs blind all night, which is what happened.

To keep the bypass from becoming noise, Nyctea escalates on *transitions and
sustained conditions*, not on every cycle: see `_should_escalate`.

SELF-CHECK — NOT ANOTHER SILENT CORPSE
--------------------------------------
The defining defect class in this swarm is components that die quietly: Anthus
dead 2 months, Apis deaf 88 days, the live tailer dying inside a pause. A
watchdog that dies silently is worse than no watchdog, because its silence is
read as health.

So Nyctea writes a heartbeat file on every cycle, including cycles where the
verdict is HEALTHY and nothing is sent. `--self-check` reads that heartbeat and
exits non-zero if it is stale, which makes "is the watchdog alive?" a question
any other daemon, a cron line, or the operator can answer in one command without
trusting Nyctea's own reporting.

Environment:
  NEOTOMA_BASE_URL         default https://neotoma.markmhendrickson.com
  NEOTOMA_BEARER_TOKEN     required for the authed read probe
  NYCTEA_INTERVAL_SECONDS  probe cadence (default 60)
  NYCTEA_STATE_DIR         heartbeat + verdict dir (default ~/.local/state/ateles/nyctea)
  NYCTEA_DEGRADED_SECONDS / NYCTEA_READ_TIMEOUT_SECONDS  see probe.py
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from execution.daemons.nyctea.probe import (  # noqa: E402
    ProbeResult,
    Verdict,
    probe,
    severity,
)

log = logging.getLogger("nyctea")

DEFAULT_BASE_URL = "https://neotoma.markmhendrickson.com"
DEFAULT_INTERVAL = 60.0

# Escalate a *sustained* degradation rather than a single slow read, so one
# unlucky query does not page the operator. Two consecutive bad cycles at the
# default 60s cadence means the condition has held for at least a minute.
SUSTAIN_CYCLES = int(os.environ.get("NYCTEA_SUSTAIN_CYCLES", "2") or 2)

# Do not re-page for an unchanged condition more often than this. Escalating
# every cycle for an hour trains the operator to ignore the channel, which is
# how a bypass turns into noise and then into a filter.
REPAGE_SECONDS = float(os.environ.get("NYCTEA_REPAGE_SECONDS", "1800") or 1800)

# A heartbeat older than this means Nyctea is not running.
STALE_AFTER_SECONDS = float(os.environ.get("NYCTEA_STALE_AFTER_SECONDS", "300") or 300)


def state_dir() -> Path:
    d = os.environ.get("NYCTEA_STATE_DIR", "")
    if d:
        return Path(d)
    return Path.home() / ".local" / "state" / "ateles" / "nyctea"


def heartbeat_path() -> Path:
    return state_dir() / "heartbeat.json"


def _write_heartbeat(result: ProbeResult, escalated: bool) -> None:
    """Written on EVERY cycle, healthy or not — see SELF-CHECK above."""
    try:
        d = state_dir()
        d.mkdir(parents=True, exist_ok=True)
        payload = dict(result.as_dict())
        payload["escalated"] = escalated
        payload["pid"] = os.getpid()
        tmp = heartbeat_path().with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(heartbeat_path())
    except Exception as exc:  # noqa: BLE001 — never let bookkeeping kill the watchdog
        log.warning("[nyctea] heartbeat write failed: %s", exc)


def read_heartbeat() -> dict | None:
    try:
        return json.loads(heartbeat_path().read_text())
    except Exception:
        return None


def self_check(now: float | None = None) -> tuple[bool, str]:
    """Answer "is the watchdog itself alive?" without trusting the watchdog.

    Exit-code contract so anything can call it: 0 healthy, non-zero not.
    """
    now = time.time() if now is None else now
    hb = read_heartbeat()
    if hb is None:
        return False, f"no heartbeat at {heartbeat_path()} — Nyctea has never run or cannot write state"
    age = now - float(hb.get("checked_at") or 0)
    if age > STALE_AFTER_SECONDS:
        return False, (
            f"heartbeat is {age:.0f}s old (> {STALE_AFTER_SECONDS:.0f}s) — "
            f"Nyctea is not running; last verdict was {hb.get('verdict')!r}"
        )
    return True, f"alive; last verdict {hb.get('verdict')!r} {age:.0f}s ago"


class Watchdog:
    def __init__(self, base_url: str, token: str, notifier=None) -> None:
        self.base_url = base_url
        self.token = token
        self._notifier = notifier
        self._consecutive: dict[Verdict, int] = {}
        self._last_escalated_verdict: Verdict | None = None
        self._last_escalated_at: float = 0.0
        self._streak_verdict: Verdict | None = None
        self._streak = 0

    # ── escalation policy ────────────────────────────────────────────────────

    def _should_escalate(self, verdict: Verdict, now: float) -> bool:
        """Escalate sustained bad conditions, on transition or after a re-page gap.

        Recovery to HEALTHY is itself worth one message: an operator who was
        paged at 03:00 needs to know it cleared without having to go and look.
        """
        if verdict is Verdict.HEALTHY:
            recovered = self._last_escalated_verdict not in (None, Verdict.HEALTHY)
            if recovered:
                self._last_escalated_verdict = Verdict.HEALTHY
                self._last_escalated_at = now
                return True
            return False

        if self._streak < SUSTAIN_CYCLES:
            return False
        if verdict != self._last_escalated_verdict:
            return True
        return (now - self._last_escalated_at) >= REPAGE_SECONDS

    def _priority(self, verdict: Verdict):
        """CRITICAL for anything that stops reads; see QUIET HOURS above."""
        from lib.notify.notifier import Priority

        if verdict is Verdict.HEALTHY:
            return Priority.WARN  # recovery notice: informative, not urgent
        if severity(verdict) >= severity(Verdict.SATURATED):
            return Priority.CRITICAL
        return Priority.BLOCKER  # DEGRADED: send now, but not a silence-window bypass

    def _message(self, result: ProbeResult) -> str:
        v = result.verdict
        if v is Verdict.HEALTHY:
            return f"Neotoma read path recovered — {result.summary()}"
        remedy = {
            Verdict.DEGRADED: (
                "Reads are slowing. Shed concurrent agent load; do not restart. "
                "This is the ramp, not the wall."
            ),
            Verdict.SATURATED: (
                "Neotoma is UP and cannot serve a single row. The swarm has no "
                "durable memory right now. Shed agent load first; consider scaling "
                "vCPU/memory. Do NOT restart — in-flight writes would be lost and "
                "the load returns in seconds."
            ),
            Verdict.WEDGED: (
                "Neotoma is not answering reads OR liveness. A restart is "
                "defensible here, but confirm no write is mid-flight first."
            ),
            Verdict.UNREACHABLE: "Neotoma is unreachable — check network/Fly before anything else.",
        }.get(v, "")
        return (
            f"Neotoma {v.value.upper()} — {result.summary()}\n"
            f"{result.detail}\n{remedy}\n"
            f"(/health is not evidence of health: it never touches the DB — ateles#577)"
        )

    # ── main loop ────────────────────────────────────────────────────────────

    def run_once(self, now: float | None = None) -> ProbeResult:
        now = time.time() if now is None else now
        result = probe(self.base_url, self.token)

        if result.verdict == self._streak_verdict:
            self._streak += 1
        else:
            self._streak_verdict = result.verdict
            self._streak = 1

        escalated = False
        if self._should_escalate(result.verdict, now):
            escalated = self._escalate(result, now)

        _write_heartbeat(result, escalated)
        log.info("[nyctea] %s", result.summary())
        return result

    def _escalate(self, result: ProbeResult, now: float) -> bool:
        msg = self._message(result)
        try:
            notifier = self._notifier
            if notifier is None:
                from lib.notify.notifier import Notifier

                # from_neotoma() would read the rubric from the very instance
                # that is down. Construct with defaults instead: an outage must
                # not depend on the thing that is out.
                notifier = Notifier()
                self._notifier = notifier
            notifier.send(msg, priority=self._priority(result.verdict), handler="nyctea")
        except Exception as exc:  # noqa: BLE001
            # Last resort: if the notifier itself fails we still leave a trace on
            # stderr and in the heartbeat rather than failing silently.
            log.error("[nyctea] ESCALATION FAILED (%s). Condition: %s", exc, msg)
            return False
        self._last_escalated_verdict = result.verdict
        self._last_escalated_at = now
        return True

    def run_forever(self, interval: float) -> None:
        while True:
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001 — a probe bug must not kill the watchdog
                log.exception("[nyctea] probe cycle raised: %s", exc)
            time.sleep(interval)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Neotoma degradation watchdog")
    ap.add_argument("--once", action="store_true", help="probe once and exit")
    ap.add_argument(
        "--self-check",
        action="store_true",
        help="report whether Nyctea itself is alive (exit 0 if so)",
    )
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    if args.self_check:
        ok, detail = self_check()
        print(json.dumps({"ok": ok, "detail": detail}) if args.json else detail)
        return 0 if ok else 1

    base_url = os.environ.get("NEOTOMA_BASE_URL", DEFAULT_BASE_URL)
    token = os.environ.get("NEOTOMA_BEARER_TOKEN", "")
    if not token:
        # Without a token the probe measures auth, not the database — a 401 is
        # fast and would look healthy. Refuse rather than report a false green.
        print("NEOTOMA_BEARER_TOKEN is required for the read probe", file=sys.stderr)
        return 2

    wd = Watchdog(base_url, token)
    if args.once:
        result = wd.run_once()
        print(json.dumps(result.as_dict(), indent=2) if args.json else result.summary())
        return 0 if result.verdict is Verdict.HEALTHY else 1

    interval = float(os.environ.get("NYCTEA_INTERVAL_SECONDS", DEFAULT_INTERVAL))
    wd.run_forever(interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
