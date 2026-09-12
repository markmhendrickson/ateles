#!/usr/bin/env python3
"""
Nyctea — Neotoma read-path probe and verdict logic.

The whole point of this module is the distinction the operator drew during the
2026-09-01 incident: "it looks to me like it's not down, but it's highly
degraded currently."

`/health` on the hosted instance returns `{"ok":true,"version":"0.22.1"}`
without touching the database. Measured live during that incident:

    GET  /health                    -> 200 in 0.89s
    POST /entities/query {limit:1}  -> timeout at 45s   (x3 consecutive)

So a liveness check answered 200 through a *total read outage*, three times in a
row, while the swarm's durable memory was completely unavailable. That is
ateles#577, and it is why this module refuses to treat `/health` as evidence of
anything except that a process is accepting connections.

VERDICT VOCABULARY
------------------
The verdicts are deliberately NOT collapsed into up/down, because the remedies
are mutually exclusive and picking the wrong one makes things worse:

    HEALTHY    read completes fast                  -> nothing
    DEGRADED   read completes, but slowly           -> shed load; do not restart
    SATURATED  read times out, process is alive     -> shed load; do NOT restart
    WEDGED     read times out AND liveness is gone  -> restart is defensible
    UNREACHABLE nothing answers at all              -> infrastructure/network

SATURATED vs WEDGED is the load-bearing distinction. A saturated instance is
doing exactly what it was asked to do, too much of it; restarting it drops
every in-flight write for no benefit and the load returns within seconds. A
wedged instance is not making progress at all. During the incident that motivated
this daemon, a restart was very nearly taken against what was actually
saturation. The probe reports; it does not restart. See RECOVERY in the module
docstring of nyctea.py.

LATENCY IS THE SIGNAL, NOT SUCCESS
----------------------------------
Degradation here is progressive, not binary: the same query measured 16s, then
90s, then 100s, then timeout as concurrency climbed. A boolean probe sees
"success, success, success, FAILURE" and reports a cliff. Recording the latency
of every probe turns that into a ramp the operator can act on before it hits
the wall.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum

# Cloudflare fronts the hosted instance and 403s urllib's default User-Agent
# with a 1010 "browser signature" error. Any explicit UA passes. Without this
# the probe reports a hard failure against a perfectly healthy instance.
USER_AGENT = "ateles-nyctea-watchdog/1.0"


class Verdict(str, Enum):
    """Ordered worst-last so `max()` over samples picks the worst verdict."""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    SATURATED = "saturated"
    WEDGED = "wedged"
    UNREACHABLE = "unreachable"


# Severity ordering for escalation decisions and for collapsing samples.
_SEVERITY = {
    Verdict.HEALTHY: 0,
    Verdict.DEGRADED: 1,
    Verdict.SATURATED: 2,
    Verdict.WEDGED: 3,
    Verdict.UNREACHABLE: 4,
}


def severity(v: Verdict) -> int:
    return _SEVERITY[v]


# ── Thresholds ───────────────────────────────────────────────────────────────
# Defaults are calibrated against the instance's own measured behaviour, not
# picked round-number-first:
#
#   healthy steady state    a limit:1 read is a single indexed row; sub-second
#   degraded floor 2.0s     the first latency at which an agent visibly stalls
#   read timeout 20s        past this a caller has already given up; the MCP
#                           client and most daemon HTTP calls time out earlier,
#                           so a probe that waits 90s is measuring nothing an
#                           agent would ever wait for
#
# Every value is env-overridable so the operator can retune without a deploy.


def _envf(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


DEGRADED_SECONDS = _envf("NYCTEA_DEGRADED_SECONDS", 2.0)
READ_TIMEOUT_SECONDS = _envf("NYCTEA_READ_TIMEOUT_SECONDS", 20.0)
LIVENESS_TIMEOUT_SECONDS = _envf("NYCTEA_LIVENESS_TIMEOUT_SECONDS", 10.0)


@dataclass
class ProbeResult:
    """One probe cycle: a real read, plus liveness only to disambiguate failure."""

    verdict: Verdict
    read_latency: float | None = None
    read_status: int | None = None
    liveness_latency: float | None = None
    liveness_ok: bool | None = None
    detail: str = ""
    checked_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict.value,
            "read_latency": self.read_latency,
            "read_status": self.read_status,
            "liveness_latency": self.liveness_latency,
            "liveness_ok": self.liveness_ok,
            "detail": self.detail,
            "checked_at": self.checked_at,
        }

    def summary(self) -> str:
        if self.read_latency is not None:
            read = f"read {self.read_latency:.1f}s"
        else:
            read = f"read TIMEOUT >{READ_TIMEOUT_SECONDS:.0f}s"
        if self.liveness_ok is None:
            live = "liveness not checked"
        elif self.liveness_ok:
            live = f"/health 200 in {self.liveness_latency:.2f}s"
        else:
            live = "/health FAILED"
        return f"{self.verdict.value.upper()}: {read}, {live}"


def _get(url: str, token: str, timeout: float) -> tuple[int, float]:
    req = urllib.request.Request(url, method="GET")
    req.add_header("User-Agent", USER_AGENT)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read(2048)
        return resp.status, time.monotonic() - t0


def _post(url: str, token: str, payload: dict, timeout: float) -> tuple[int, float]:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        resp.read(4096)
        return resp.status, time.monotonic() - t0


def check_liveness(base_url: str, timeout: float | None = None) -> tuple[bool, float | None]:
    """`/health` — deliberately NOT the verdict.

    Used only to tell SATURATED (process alive, reads dead) from WEDGED /
    UNREACHABLE. On its own this call is the exact false negative that let a
    read outage run unnoticed, so nothing in this module treats a 200 here as
    good news.
    """
    timeout = LIVENESS_TIMEOUT_SECONDS if timeout is None else timeout
    try:
        status, latency = _get(f"{base_url.rstrip('/')}/health", "", timeout)
        return 200 <= status < 300, latency
    except Exception:
        return False, None


def probe(
    base_url: str,
    token: str,
    read_timeout: float | None = None,
    degraded_seconds: float | None = None,
) -> ProbeResult:
    """Judge Neotoma by whether it can serve a real, authenticated read.

    The probe is `POST /entities/query {"limit": 1}` — the cheapest request that
    still traverses auth, the HTTP layer, the query planner and the database.
    A `limit:1` read returns a single row, so a slow result is a slow *system*,
    never a large payload.
    """
    read_timeout = READ_TIMEOUT_SECONDS if read_timeout is None else read_timeout
    degraded_seconds = DEGRADED_SECONDS if degraded_seconds is None else degraded_seconds
    url = f"{base_url.rstrip('/')}/entities/query"

    try:
        status, latency = _post(url, token, {"limit": 1}, read_timeout)
    except urllib.error.HTTPError as exc:
        # An HTTP error status is a *served* response: the app is answering, so
        # the read path is alive even though this request was rejected. 401/403
        # means the watchdog's own credentials are wrong — that is a watchdog
        # fault, and must not be reported as a Neotoma outage.
        live_ok, live_lat = check_liveness(base_url)
        return ProbeResult(
            verdict=Verdict.DEGRADED,
            read_status=exc.code,
            liveness_ok=live_ok,
            liveness_latency=live_lat,
            detail=f"read returned HTTP {exc.code} — check watchdog credentials if 401/403",
        )
    except Exception as exc:
        # Timeout, connection reset, DNS. Now liveness earns its keep: it is the
        # only thing that separates "too busy to answer" from "not there".
        live_ok, live_lat = check_liveness(base_url)
        if live_ok:
            verdict = Verdict.SATURATED
            detail = (
                "read timed out while /health still answers — the process is up and "
                "cannot serve a single row. Shed load; do NOT restart."
            )
        else:
            live2_ok, _ = check_liveness(base_url, timeout=LIVENESS_TIMEOUT_SECONDS)
            if live2_ok:
                verdict = Verdict.SATURATED
                detail = "read timed out; liveness flapping — treat as saturation"
            else:
                verdict = Verdict.WEDGED
                detail = "read timed out AND /health does not answer"
        return ProbeResult(
            verdict=verdict,
            read_status=None,
            liveness_ok=live_ok,
            liveness_latency=live_lat,
            detail=f"{detail} ({type(exc).__name__})",
        )

    if latency >= degraded_seconds:
        return ProbeResult(
            verdict=Verdict.DEGRADED,
            read_latency=latency,
            read_status=status,
            detail=(
                f"read served in {latency:.1f}s (>= {degraded_seconds:.1f}s). "
                "Degradation here is progressive — this is the ramp toward saturation."
            ),
        )

    return ProbeResult(
        verdict=Verdict.HEALTHY,
        read_latency=latency,
        read_status=status,
        detail=f"read served in {latency:.2f}s",
    )
