"""Feed the live plan-usage snapshot from the Claude CLI's own rate-limit report.

The one automatic, credential-free live source for Claude plan usage that was
verified (2026-09-29): ``claude --print --output-format stream-json --verbose``
emits a ``rate_limit_event`` line carrying every plan window the account has,
for example::

    {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed_warning", "rateLimitType": "seven_day",
        "unifiedWindows": {
            "five_hour": {"utilization": 0.2,  "resetsAt": 1790713800},
            "seven_day": {"utilization": 0.66, "resetsAt": 1791223200}}}}

``utilization`` is a 0-1 fraction and ``resetsAt`` epoch seconds.  It is the
CLI reporting on its own subscription session, so no token is read, copied or
sent anywhere by this module: the caller supplies the child environment (the
same subscription-only environment every dispatch already runs under).

Plain-text ``--print`` runs (what dispatches use) carry no usage, so the feeder
is a minimal probe run on demand rather than a parse of dispatch output.  It
runs only when the recorded reading is older than ``usage_refresh_seconds()``,
under a lock so concurrent dispatches share one probe, and it records nothing
when the output is not a well-formed report: a failed probe leaves the old
reading to age out, which the gate then refuses on (fail closed).

Other providers have no such report (codex runs ephemeral and keeps no rollout;
cursor prints text), so they are not fed and not gated.
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import harness_router

log = logging.getLogger("apis.usage_probe")

# The CLI's window keys -> the names the snapshot (and gate) use.
WINDOW_NAMES = {"five_hour": "five_hour", "seven_day": "weekly_all"}

PROBE_PROMPT = "reply with the single word ok"


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str
    windows: tuple[dict[str, object], ...] = ()


def parse_rate_limit_windows(stdout: str) -> list[dict[str, object]]:
    """Windows from the LAST well-formed ``rate_limit_event`` in stream-json.

    Returns ``[]`` when no event carries a usable window.  Every window is
    validated here (finite utilization, integer reset) so a malformed report
    can never reach the snapshot; a fraction above 1 is capped at 100%.
    """
    windows: list[dict[str, object]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("type") != "rate_limit_event":
            continue
        info = event.get("rate_limit_info")
        if not isinstance(info, dict):
            continue
        raw = info.get("unifiedWindows")
        if not isinstance(raw, dict):
            # Older shape: one window named by rateLimitType.
            key = info.get("rateLimitType")
            raw = {key: info} if isinstance(key, str) else {}
        parsed: list[dict[str, object]] = []
        for key, window in raw.items():
            if not isinstance(key, str) or not key.strip() or not isinstance(window, dict):
                continue
            utilization, resets = window.get("utilization"), window.get("resetsAt")
            if isinstance(utilization, bool) or isinstance(resets, bool):
                continue
            try:
                fraction = float(utilization)  # type: ignore[arg-type]
                reset_wall = float(resets)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if not math.isfinite(fraction) or fraction < 0 or not math.isfinite(reset_wall):
                continue
            parsed.append(
                {
                    "name": WINDOW_NAMES.get(key, key.strip()),
                    "used_percent": round(min(fraction, 1.0) * 100.0, 4),
                    "resets_at": harness_router._iso_from_wall(reset_wall),
                }
            )
        if parsed:
            windows = parsed  # later events supersede earlier ones
    return windows


def probe_command(binary: str) -> list[str]:
    """The cheapest invocation that still returns the account's windows."""
    return [
        binary,
        "--print",
        "--model", os.environ.get("APIS_USAGE_PROBE_MODEL", "haiku"),
        "--output-format", "stream-json",
        "--verbose",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--tools", "",
    ]  # the prompt goes on stdin: --tools is variadic and swallows a trailing argument


def probe_claude(
    binary: str,
    *,
    env: Mapping[str, str],
    timeout: float = 90.0,
    run=subprocess.run,
) -> ProbeResult:
    """Run one probe and return its validated windows (no snapshot write)."""
    try:
        with tempfile.TemporaryDirectory(prefix="usage-probe-") as cwd:
            proc = run(
                probe_command(binary),
                cwd=cwd,
                env=dict(env),
                capture_output=True,
                text=True,
                timeout=timeout,
                input=PROBE_PROMPT,
            )
    except (OSError, subprocess.SubprocessError) as exc:
        return ProbeResult(False, f"probe did not run: {type(exc).__name__}")
    windows = parse_rate_limit_windows(proc.stdout or "")
    if not windows:
        return ProbeResult(
            False, f"probe exit {proc.returncode} returned no rate_limit_event windows"
        )
    return ProbeResult(True, "ok", tuple(windows))


def _lock_path() -> Path:
    usage = harness_router._usage_path()
    return usage.with_name(f".{usage.name}.probe.lock")


def refresh_usage_if_stale(
    available: Mapping[str, str | None],
    *,
    env: Mapping[str, str],
    now_wall: float | None = None,
    force: bool = False,
    run=subprocess.run,
) -> dict[str, str]:
    """Refresh each gated provider's reading when it is older than the refresh age.

    Returns ``{provider: outcome}`` for the providers it looked at (``fresh``,
    ``refreshed``, or a failure detail).  Never raises: a failed refresh is
    logged and the gate refuses on the aging reading.  Disabled (returns ``{}``)
    with ``APIS_USAGE_PROBE=off`` so a test or an operator can opt out.
    """
    if os.environ.get("APIS_USAGE_PROBE", "on").strip().lower() in ("off", "0", "false", "no"):
        return {}
    if not harness_router.usage_gate_enabled():
        return {}
    outcomes: dict[str, str] = {}
    for provider in harness_router.usage_gated_providers():
        binary = available.get(provider)
        if provider != "claude" or not binary:
            continue
        try:
            outcomes[provider] = _refresh_claude(
                binary, env=env, now_wall=now_wall, force=force, run=run
            )
        except Exception as exc:  # noqa: BLE001 - feeding must never break dispatch
            log.error(f"[apis] usage refresh for {provider} failed: {exc}")
            outcomes[provider] = f"error: {type(exc).__name__}"
    return outcomes


def _observed_age(provider: str, moment: float) -> float | None:
    entry = harness_router._read_json_object(harness_router._usage_path()).get(provider)
    if not isinstance(entry, dict):
        return None
    observed = harness_router._wall_from_iso(entry.get("observed_at"))
    return None if observed is None else moment - observed


def _refresh_claude(
    binary: str, *, env: Mapping[str, str], now_wall: float | None, force: bool, run
) -> str:
    moment = time.time() if now_wall is None else now_wall
    threshold = harness_router.usage_refresh_seconds()

    def is_fresh() -> bool:
        age = _observed_age("claude", moment)
        gate = harness_router.usage_gate("claude", now_wall=moment)
        # A reading that is young but malformed still needs replacing.
        return (
            not force
            and age is not None
            and 0 <= age <= threshold
            and gate is not None
            and gate.code in (harness_router.GATE_OK, harness_router.GATE_PACED)
        )

    if is_fresh():
        return "fresh"
    if not force and harness_router.persisted_cooling("claude", now_wall=moment):
        # Held out until its reset anyway; the next dispatch after it probes.
        return "cooled (probe skipped)"
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        # Another dispatch may have refreshed while this one waited.
        moment = time.time() if now_wall is None else now_wall
        if is_fresh():
            return "fresh"
        result = probe_claude(binary, env=env, run=run)
        if not result.ok:
            log.warning(f"[apis] claude usage probe failed: {result.detail}")
            return f"probe failed: {result.detail}"
        harness_router.record_usage("claude", result.windows)
        log.info(
            "[apis] claude usage refreshed: "
            + ", ".join(f"{w['name']}={w['used_percent']}%" for w in result.windows)
        )
        return "refreshed"
