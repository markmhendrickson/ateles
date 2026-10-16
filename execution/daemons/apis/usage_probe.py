"""Feed live harness capacity from each subscription-backed provider CLI.

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

Codex DOES expose numeric plan windows, but not through ``codex exec`` (verified
2026-10-06 against codex-cli 0.157.1): ``codex app-server`` speaks JSON-RPC over
stdio, and its ``account/rateLimits/read`` request returns the account's windows
with no model turn, for example::

    {"rateLimits": {"limitId": "codex", "planType": "pro",
        "primary": {"usedPercent": 78, "windowDurationMins": 10080,
                    "resetsAt": 1791580428},
        "secondary": null}, "ordinaryUsageAllowed": true}

``windowDurationMins`` names the window (10080 is the weekly one, 300 the
five-hour one).  Interactive Codex sessions record the same ``rate_limits`` on
every ``token_count`` event in their rollout files, which is the same data.
``codex exec --json`` emits no rate-limit event, so the app-server request is
the only turn-free source.  ``read_codex_rate_limits`` uses it; a reading that
is absent, malformed, or has no weekly window is INCONCLUSIVE, never a verdict,
and the adapter then falls through to the native request probe below.

Cursor does not expose numeric plan windows.  Its adapter (and Codex's
fallback) run the smallest provider-native request in an isolated temporary
directory: success is positive recovery evidence, an explicit limit refusal
with a reset is exhaustion evidence, and every other result is ``unknown``.
All adapters receive the subscription-only environment built by
``skill_runner``; this module never adds a metered credential or reads one
directly.
"""

from __future__ import annotations

import fcntl
import json
import logging
import math
import os
import re
import select
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import harness_router
from limit_reset import parse_refusal

log = logging.getLogger("apis.usage_probe")

# The CLI's window keys -> the names the snapshot (and gate) use.
WINDOW_NAMES = {"five_hour": "five_hour", "seven_day": "weekly_all"}

PROBE_PROMPT = "reply with the single word ok"


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str
    windows: tuple[dict[str, object], ...] = ()
    status: str = "unknown"
    source: str = "provider_native_probe"
    exhausted_until: float | None = None
    # Why no weekly reading was taken, when a capacity observation has none.
    reading_failure: str = ""


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


_TOKEN_LIKE = re.compile(r"[A-Za-z0-9_\-\.]{32,}")


def failure_excerpt(stdout: str, stderr: str, limit: int = 200) -> str:
    """The CLI's own reason for a failed probe, short and safe to log and show.

    Prefers the ``result`` event's text (for example "Not logged in"), then the
    last stderr line, then the last non-JSON stdout line.  Long token-like runs
    are masked so an unexpected message cannot carry a credential into the
    snapshot, a log line or a refusal.
    """
    candidates: list[str] = []
    for line in stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "result":
                text = event.get("result")
                if isinstance(text, str) and text.strip():
                    candidates.append(text)
            elif isinstance(event, dict) and event.get("type") == "assistant":
                content = (event.get("message") or {}).get("content")
                if isinstance(content, list):
                    for block in content:
                        text = block.get("text") if isinstance(block, dict) else None
                        if isinstance(text, str) and text.strip():
                            candidates.append(text)
    stderr_lines = [ln for ln in stderr.splitlines() if ln.strip()]
    plain = [ln for ln in stdout.splitlines() if ln.strip() and not ln.lstrip().startswith("{")]
    for pool in (candidates[-1:], stderr_lines[-1:], plain[-1:]):
        if pool:
            text = _TOKEN_LIKE.sub("<masked>", " ".join(pool[0].split()))
            return text[:limit]
    return "no output"


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


def provider_probe_command(provider: str, binary: str) -> tuple[list[str], str | None]:
    """Return one bounded provider-native capacity command and its stdin.

    The probes run in an empty temporary directory, persist no session, request
    the provider's read-only/ask sandbox, and make one model turn with a prompt
    that needs no tool.  Optional model overrides let deployment configuration
    choose the cheapest subscription model without making a model name part of
    the capacity contract.
    """
    if provider == "claude":
        return probe_command(binary), PROBE_PROMPT
    if provider == "codex":
        model = os.environ.get("APIS_USAGE_PROBE_CODEX_MODEL", "").strip()
        return (
            [
                binary,
                "exec",
                "--json",
                *(["--model", model] if model else []),
                "--sandbox",
                "read-only",
                "--ephemeral",
                "--skip-git-repo-check",
                "--color",
                "never",
                "-",
            ],
            PROBE_PROMPT,
        )
    if provider == "cursor":
        model = os.environ.get("APIS_USAGE_PROBE_CURSOR_MODEL", "").strip()
        return (
            [
                binary,
                "--print",
                "--trust",
                "--mode",
                "ask",
                "--sandbox",
                "enabled",
                "--output-format",
                "json",
                *(["--model", model] if model else []),
                PROBE_PROMPT,
            ],
            None,
        )
    raise ValueError(f"unsupported provider probe: {provider!r}")


# Codex window lengths, in minutes, and the snapshot names they map to.
_CODEX_WEEK_MINUTES = 7 * 24 * 60
_CODEX_SESSION_MINUTES = 5 * 60
CODEX_RATE_LIMITS_SOURCE = "codex_app_server_rate_limits"


def _codex_window_name(minutes: int) -> str:
    """Name a codex window by its length, so the gate finds the weekly one.

    An unrecognised length keeps a descriptive name rather than being guessed
    into the weekly slot: the gate then reports "no weekly window", which is
    inconclusive, instead of pacing against the wrong window.
    """
    if abs(minutes - _CODEX_WEEK_MINUTES) <= 60:
        return "weekly_all"
    if abs(minutes - _CODEX_SESSION_MINUTES) <= 15:
        return "five_hour"
    return f"window_{minutes}m"


def parse_codex_rate_limits(result: object) -> tuple[list[dict[str, object]], str | None]:
    """Windows from an ``account/rateLimits/read`` result, and why not if none.

    Returns ``(windows, None)`` for a usable reading, else ``([], reason)``.
    Every window is validated (finite non-negative percent, whole-minute
    duration, finite reset) so a malformed report cannot reach the snapshot; a
    percent above 100 is capped.  A reading with no weekly-length window is
    refused here, because the gate cannot pace without one.  An account the
    server says may not use Codex (``ordinaryUsageAllowed`` false) while its
    weekly window shows room is contradictory, so it is inconclusive too.
    """
    if not isinstance(result, dict):
        return [], "result is not an object"
    snapshot = None
    by_id = result.get("rateLimitsByLimitId")
    if isinstance(by_id, dict) and isinstance(by_id.get("codex"), dict):
        snapshot = by_id["codex"]
    elif isinstance(result.get("rateLimits"), dict):
        snapshot = result["rateLimits"]
    if snapshot is None:
        return [], "no rateLimits snapshot"
    windows: list[dict[str, object]] = []
    for key in ("primary", "secondary"):
        raw = snapshot.get(key)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            return [], f"{key} window is not an object"
        used, minutes, resets = (
            raw.get("usedPercent"), raw.get("windowDurationMins"), raw.get("resetsAt"),
        )
        if any(isinstance(v, bool) for v in (used, minutes, resets)):
            return [], f"{key} window carries a boolean where a number belongs"
        try:
            percent = float(used)  # type: ignore[arg-type]
            length = int(minutes)  # type: ignore[arg-type]
            reset_wall = float(resets)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return [], f"{key} window is missing a number (usedPercent/windowDurationMins/resetsAt)"
        if not (math.isfinite(percent) and percent >= 0 and math.isfinite(reset_wall) and length > 0):
            return [], f"{key} window has an out-of-range value"
        windows.append(
            {
                "name": _codex_window_name(length),
                "used_percent": round(min(percent, 100.0), 4),
                "resets_at": harness_router._iso_from_wall(reset_wall),
            }
        )
    if not windows:
        return [], "no usage windows"
    weekly = next((w for w in windows if w["name"] == "weekly_all"), None)
    if weekly is None:
        return [], "no weekly-length window, so the weekly pace cannot be computed"
    if result.get("ordinaryUsageAllowed") is False and float(weekly["used_percent"]) < 100.0:  # type: ignore[arg-type]
        return [], "server reports ordinary usage not allowed while the weekly window shows room"
    return windows, None


def read_codex_rate_limits(
    binary: str,
    *,
    env: Mapping[str, str],
    timeout: float = 20.0,
    spawn=subprocess.Popen,
) -> ProbeResult:
    """Read Codex's plan windows from ``codex app-server`` (no model turn).

    Speaks the minimal JSON-RPC handshake (``initialize``, ``initialized``,
    ``account/rateLimits/read``) in an empty temporary directory under the
    caller's subscription-only environment, then closes the server.  The server
    exits on stdin EOF before replying, so stdin stays open until the answer
    arrives.  Never raises: any failure is an ``unknown`` result whose detail
    says why, and the caller falls back to the native request probe.
    """
    def unknown(detail: str) -> ProbeResult:
        return ProbeResult(
            False, detail, status="unknown", source=CODEX_RATE_LIMITS_SOURCE
        )

    requests = (
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "clientInfo": {"name": "ateles-usage-probe", "version": "1"},
            "capabilities": None}},
        {"jsonrpc": "2.0", "method": "initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": None},
    )
    proc = None
    try:
        with tempfile.TemporaryDirectory(prefix="usage-probe-codex-rl-") as cwd:
            proc = spawn(
                [binary, "app-server"],
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(
                ("\n".join(json.dumps(r) for r in requests) + "\n").encode()
            )
            proc.stdin.flush()
            deadline = time.monotonic() + timeout
            fd, buffer, reply = proc.stdout.fileno(), b"", None
            while reply is None:
                left = deadline - time.monotonic()
                if left <= 0:
                    return unknown(f"app-server did not answer within {timeout:.0f}s")
                ready, _, _ = select.select([fd], [], [], min(left, 1.0))
                if not ready:
                    continue
                chunk = os.read(fd, 65536)
                if not chunk:
                    return unknown("app-server closed without answering")
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                for line in lines:
                    try:
                        message = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(message, dict) and message.get("id") == 2:
                        reply = message
                        break
    except (OSError, subprocess.SubprocessError, AssertionError) as exc:
        return unknown(f"app-server did not run: {type(exc).__name__}: {str(exc)[:120]}")
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:  # noqa: BLE001 - cleanup must not mask the result
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass
    if "error" in reply:
        text = " ".join(str((reply.get("error") or {}).get("message", "error")).split())
        return unknown(f"app-server refused the read: {_TOKEN_LIKE.sub('<masked>', text)[:160]}")
    windows, problem = parse_codex_rate_limits(reply.get("result"))
    if problem is not None:
        return unknown(f"app-server reading unusable: {problem}")
    return ProbeResult(
        True,
        "codex plan windows read from app-server",
        tuple(windows),
        status="available",
        source=CODEX_RATE_LIMITS_SOURCE,
    )


def probe_claude(
    binary: str,
    *,
    env: Mapping[str, str],
    timeout: float = 60.0,
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
        return ProbeResult(
            False,
            f"probe did not run: {type(exc).__name__}: {str(exc)[:120]}",
            source="claude_rate_limit_event",
        )
    windows = parse_rate_limit_windows(proc.stdout or "")
    if not windows:
        return ProbeResult(
            False,
            f"probe exit {proc.returncode} returned no rate_limit_event windows; "
            f"CLI said: {failure_excerpt(proc.stdout or '', proc.stderr or '')}",
            source="claude_rate_limit_event",
        )
    return ProbeResult(
        True,
        "provider usage report succeeded",
        tuple(windows),
        status="available",
        source="claude_rate_limit_event",
    )


def _json_result_error(stdout: str) -> str | None:
    """Return a provider-reported result error even when the process exits 0."""
    objects: list[dict[str, object]] = []
    try:
        whole = json.loads(stdout)
    except (TypeError, ValueError):
        whole = None
    if isinstance(whole, dict):
        objects.append(whole)
    for line in stdout.splitlines():
        try:
            item = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(item, dict):
            objects.append(item)
    for item in reversed(objects):
        if item.get("is_error") is True or item.get("type") in {"error", "turn.failed"}:
            value = item.get("result") or item.get("message") or item.get("error")
            text = " ".join(str(value or "provider reported an error").split())
            return _TOKEN_LIKE.sub("<masked>", text)[:200]
    return None


def probe_provider(
    provider: str,
    binary: str,
    *,
    env: Mapping[str, str],
    now_wall: float | None = None,
    timeout: float = 60.0,
    run=subprocess.run,
    codex_rate_limits: Callable[..., ProbeResult] | None = None,
) -> ProbeResult:
    """Run one provider adapter and return capacity evidence without writing it.

    Codex first tries its numeric plan windows (``read_codex_rate_limits``); a
    reading that is inconclusive falls through to the native request probe, and
    if that is inconclusive too the result is ``unknown`` carrying both reasons.
    """
    if provider == "claude":
        return probe_claude(binary, env=env, timeout=timeout, run=run)
    numeric_unknown = ""
    if provider == "codex":
        reader = codex_rate_limits or read_codex_rate_limits
        numeric = reader(binary, env=env)
        if numeric.status == "available" and numeric.windows:
            return numeric
        numeric_unknown = numeric.detail
        log.info(
            f"[apis] codex plan windows unavailable ({numeric_unknown}); "
            "falling back to the native request probe"
        )
    result = _native_probe(provider, binary, env=env, now_wall=now_wall, timeout=timeout, run=run)
    if numeric_unknown and result.status == "unknown":
        return ProbeResult(
            False,
            f"{result.detail}; plan windows: {numeric_unknown}",
            status="unknown",
            source=result.source,
        )
    if numeric_unknown and result.status == "available":
        return ProbeResult(
            True, result.detail, status="available", source=result.source,
            reading_failure=f"plan windows: {numeric_unknown}",
        )
    return result


def _native_probe(
    provider: str,
    binary: str,
    *,
    env: Mapping[str, str],
    now_wall: float | None,
    timeout: float,
    run,
) -> ProbeResult:
    """The smallest provider-native request, read as available/exhausted/unknown."""
    source = {"codex": "codex_exec", "cursor": "cursor_agent_print"}.get(
        provider, f"{provider}_native_probe"
    )
    try:
        command, stdin = provider_probe_command(provider, binary)
        with tempfile.TemporaryDirectory(prefix=f"usage-probe-{provider}-") as cwd:
            proc = run(
                command,
                cwd=cwd,
                env=dict(env),
                capture_output=True,
                text=True,
                timeout=timeout,
                input=stdin,
            )
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        return ProbeResult(
            False,
            f"probe did not run: {type(exc).__name__}: {str(exc)[:120]}",
            source=source,
        )
    stdout, stderr = proc.stdout or "", proc.stderr or ""
    refusal = parse_refusal(
        stdout, stderr, provider=provider, now_wall=now_wall
    )
    if proc.returncode != 0 and refusal is not None and refusal.until_wall is not None:
        return ProbeResult(
            False,
            f"{refusal.kind} limit; reset from {refusal.matched!r}",
            status="exhausted",
            source=source,
            exhausted_until=refusal.until_wall,
        )
    reported_error = _json_result_error(stdout)
    if proc.returncode == 0 and reported_error is None:
        return ProbeResult(
            True,
            "provider-native probe succeeded",
            status="available",
            source=source,
        )
    detail = failure_excerpt(stdout, stderr)
    if refusal is not None and refusal.until_wall is None:
        detail = f"capacity refusal without a parseable reset: {refusal.matched}"
    elif reported_error:
        detail = reported_error
    return ProbeResult(
        False,
        f"probe exit {proc.returncode}: {detail}",
        source=source,
    )


PROBE_ADAPTERS = {
    "claude": probe_provider,
    "codex": probe_provider,
    "cursor": probe_provider,
}


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
        if provider not in available:
            continue
        binary = available.get(provider)
        if not binary:
            detail = f"{provider} binary unavailable"
            harness_router.record_probe_unknown(
                provider,
                source="provider_binary",
                detail=detail,
                observed_at=now_wall,
            )
            outcomes[provider] = f"probe failed: {detail}"
            continue
        try:
            outcomes[provider] = _refresh_provider(
                provider,
                binary,
                env=env,
                now_wall=now_wall,
                force=force,
                run=run,
            )
        except Exception as exc:  # noqa: BLE001 - feeding must never break dispatch
            log.error(f"[apis] usage refresh for {provider} failed: {exc}")
            outcomes[provider] = f"error: {type(exc).__name__}"
    return outcomes


def _observed_age(provider: str, moment: float) -> float | None:
    entry = harness_router._read_json_object(harness_router._usage_path()).get(provider)
    if not isinstance(entry, dict):
        return None
    probe = entry.get("probe")
    observed = harness_router._wall_from_iso(
        probe.get("observed_at") if isinstance(probe, dict) else entry.get("observed_at")
    )
    return None if observed is None else moment - observed


def _backoff_until(provider: str) -> float | None:
    """When the automatic probe may run again after a failure, or ``None``."""
    entry = harness_router._read_json_object(harness_router._usage_path()).get(provider)
    failure = entry.get("last_probe_failure") if isinstance(entry, dict) else None
    at = harness_router._wall_from_iso(failure.get("at")) if isinstance(failure, dict) else None
    if at is None:
        return None
    return at + harness_router.usage_probe_backoff_seconds()


def _refresh_provider(
    provider: str,
    binary: str,
    *,
    env: Mapping[str, str],
    now_wall: float | None,
    force: bool,
    run,
) -> str:
    moment = time.time() if now_wall is None else now_wall
    threshold = harness_router.usage_refresh_seconds()

    def is_fresh() -> bool:
        age = _observed_age(provider, moment)
        gate = harness_router.usage_gate(provider, now_wall=moment)
        # A reading that is young but malformed still needs replacing.
        return (
            not force
            and age is not None
            and 0 <= age <= threshold
            and gate is not None
            and gate.code in (harness_router.GATE_OK, harness_router.GATE_PACED)
        )

    def in_backoff() -> str | None:
        until = _backoff_until(provider)
        if not force and until is not None and moment < until:
            return (
                "backoff (last automatic refresh failed; next attempt after "
                f"{harness_router.render_wall(until)}; "
                "`harness_usage.py refresh` ignores the backoff)"
            )
        return None

    if is_fresh():
        return "fresh"
    if (waiting := in_backoff()) is not None:
        return waiting
    if not force and harness_router.persisted_cooling(provider, now_wall=moment):
        # The provider is held out until its stated reset.  Probe once the hold
        # expires instead of spending a request that selection cannot use.
        return "cooled (probe skipped)"
    lock = _lock_path()
    lock.parent.mkdir(parents=True, exist_ok=True)
    with open(lock, "a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        # Another dispatch may have refreshed while this one waited.
        moment = time.time() if now_wall is None else now_wall
        if is_fresh():
            return "fresh"
        # ...or failed while this one waited: one failed probe per backoff
        # window across every concurrent dispatch, not one per lens.
        if (waiting := in_backoff()) is not None:
            return waiting
        adapter = PROBE_ADAPTERS.get(provider)
        if adapter is None:
            detail = f"no capacity probe adapter registered for {provider}"
            harness_router.record_probe_unknown(
                provider, source="probe_registry", detail=detail, observed_at=moment
            )
            return f"probe failed: {detail}"
        result = adapter(
            provider,
            binary,
            env=env,
            now_wall=moment,
            run=run,
        )
        if result.status == "unknown":
            log.warning(f"[apis] {provider} usage probe failed: {result.detail}")
            try:
                harness_router.record_probe_unknown(
                    provider,
                    source=result.source,
                    detail=result.detail,
                    observed_at=moment,
                )
            except Exception as exc:  # noqa: BLE001 - diagnostics must not break dispatch
                log.error(f"[apis] could not record the usage probe failure: {exc}")
            return f"probe failed: {result.detail}"
        if result.status == "exhausted" and result.exhausted_until is not None:
            harness_router.record_exhausted(
                provider, result.exhausted_until, observed_at=moment
            )
            log.info(
                "[apis] %s exhaustion refreshed from %s until %s",
                provider,
                result.source,
                harness_router.render_wall(result.exhausted_until),
            )
            return "exhausted"
        if result.windows:
            harness_router.record_usage(
                provider,
                result.windows,
                observed_at=moment,
                provider_probe=True,
                probe_source=(
                    "provider_usage_report" if provider == "claude" else result.source
                ),
            )
            log.info(
                f"[apis] {provider} usage refreshed: "
                + ", ".join(
                    f"{w['name']}={w['used_percent']}%" for w in result.windows
                )
            )
        elif provider in harness_router.WEEKLY_PACED_PROVIDERS:
            # Budget evidence handling tightened per security review.
            harness_router.record_capacity_observation(
                provider,
                source=result.source,
                detail=result.detail,
                observed_at=moment,
                reading_failure=result.reading_failure
                or "no weekly reading in the provider's report",
            )
            log.warning(
                f"[apis] {provider} capacity observed from {result.source} but no "
                f"weekly reading was obtained ({result.reading_failure or 'none in report'})"
            )
            return (
                "probe failed: no weekly reading "
                f"({result.reading_failure or 'none in report'})"
            )
        else:
            harness_router.record_probe_available(
                provider,
                source=result.source,
                detail=result.detail,
                observed_at=moment,
            )
            log.info("[apis] %s capacity available from %s", provider, result.source)
        return "refreshed"


def _refresh_claude(
    binary: str, *, env: Mapping[str, str], now_wall: float | None, force: bool, run
) -> str:
    """Compatibility wrapper for tests and callers predating provider adapters."""
    return _refresh_provider(
        "claude", binary, env=env, now_wall=now_wall, force=force, run=run
    )
