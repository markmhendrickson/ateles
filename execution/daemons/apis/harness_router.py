"""Quota-aware selection for subscription-backed agent harness CLIs.

The router deliberately owns policy, not subprocess details.  ``skill_runner``
supplies the binaries that are actually usable and reports capacity/auth
failures back through ``cool_down``.

Configuration is read for every selection so operators can change headroom
without restarting Apis:

``APIS_HARNESS_PROVIDERS``
    Comma-separated provider order.  Default: ``claude,codex,cursor``.

``APIS_HARNESS_HEADROOM``
    JSON object with estimated remaining bundled-plan capacity, from 0.0 to
    1.0.  Missing providers default to 1.0.  Example:
    ``{"claude": 0.1, "codex": 0.8, "cursor": 0.5}``.

``APIS_HARNESS_HEADROOM_FILE``
    Optional JSON file read on every selection, allowing a monitor or operator
    to refresh estimates without restarting Apis.  Defaults to
    ``~/.config/ateles/harness-headroom.json`` when that file exists.

    Each per-provider value in this file is a union type:

    * a bare number (legacy shape) — ``{"claude": 0.15}`` — just headroom, no
      cooldown metadata.
    * an object — ``{"headroom": 0.15, "cooldown_until": "<ISO 8601 UTC>",
      "cooldown_reason": "quota_error_parsed" | "quota_error_default" |
      "manual"}`` — headroom plus a durable cooldown record.  ``cool_down()``
      writes this shape when a provider fails with a quota/capacity error.
      Example, for a provider whose error text read
      "usage limit reached, resets in 3 weeks", parsed at 2026-09-25T12:00:00Z:

      .. code-block:: json

          {
            "claude": {
              "headroom": 1.0,
              "cooldown_until": "2026-10-16T12:00:00+00:00",
              "cooldown_reason": "quota_error_parsed"
            }
          }

      **Operator note:** ``cooldown_reason: "manual"`` means a human (not this
      module) set the cooldown, and it is NEVER auto-restored on read — clear
      it by hand (edit the file or delete the key) when the manual hold should
      end.  Every other ``cooldown_reason`` is auto-restored once wall-clock
      time passes ``cooldown_until`` (see ``configured_headroom()``).

``APIS_HARNESS_MIN_HEADROOM``
    Providers at or below this value are held out.  Default: 0.05.

``APIS_HARNESS_COOLDOWN_SECONDS``
    Fallback cooldown duration, used when a provider fails without a
    parseable reset time in its error text (or with no error text at all).
    Default: 3600.

No metered/API-key fallback is represented here.  That hard boundary is
enforced by ``skill_runner`` when it constructs each child environment.

Two clocks, deliberately not collapsed into one
------------------------------------------------

This module tracks cooldowns on TWO independent clocks and they must stay
separate:

* ``time.monotonic()`` — backs the process-local ``_cooldown_until`` dict
  that ``cooling_providers()`` and ``_provider_exclusion_reason()`` read on
  every selection.  Monotonic time has no fixed epoch, never jumps on a
  system clock change, and cannot be compared to a parsed calendar date or
  persisted across a process restart.
* ``time.time()`` (wall-clock UNIX epoch) — backs ``parse_reset_hint()`` and
  everything persisted to the headroom file (``cooldown_until`` is an ISO
  8601 UTC string).  A provider's quota reset time is a real calendar
  instant, so it can only be expressed on this clock, and only this clock
  survives a restart.

``cool_down()`` is the bridge: it parses a wall-clock deadline (when
possible) and converts it to an equivalent monotonic deadline for the
in-memory dict, while separately persisting the wall-clock deadline to disk.
Do not "simplify" this back to a single clock — the monotonic side exists
specifically so cooldowns are immune to clock changes on a long-running
daemon process, and the wall-clock side exists specifically so cooldowns
survive a restart and can be compared to a provider's own stated reset time.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger("apis.harness_router")

PROVIDERS = ("claude", "codex", "cursor")
DEFAULT_PROVIDER_ORDER = PROVIDERS

_current_weights: dict[str, float] = {}
_cooldown_until: dict[str, float] = {}

# Bound the amount of raw provider error text ever logged. A future provider
# error could interpolate a token/header/account identifier into its message,
# so log lines carry only the matched clause plus a small context window —
# never the unbounded raw stderr/stdout blob.
_LOG_SNIPPET_CONTEXT_CHARS = 40
_LOG_SNIPPET_MAX_CHARS = 160

_RESET_UNIT_SECONDS = {
    "hour": 3600,
    "day": 86400,
    "week": 604800,
}

# No real bundled-plan quota reset is ever more than a year out. This bounds
# `parse_reset_hint`'s relative-duration branch so an absurd or adversarial
# "resets in <huge N> weeks" value cannot overflow `datetime.fromtimestamp`
# downstream (in `_iso_from_wall`, called from `cool_down`) — that would
# crash the caller instead of degrading to the fixed-duration fallback.
_MAX_PARSEABLE_RESET_SECONDS = 365 * 86400

# "resets in N (hour|day|week)s?" — relative duration.
_RESET_IN_RE = re.compile(
    r"resets?\s+in\s+(\d+)\s*(hour|day|week)s?",
    re.IGNORECASE,
)

# "resets at <ISO8601>" — absolute date/time.
_RESET_AT_RE = re.compile(
    r"resets?\s+at\s+(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?"
    r"(?:Z|[+-]\d{2}:?\d{2})?)",
    re.IGNORECASE,
)

# "resets on <YYYY-MM-DD>" — absolute date only.
_RESET_ON_RE = re.compile(
    r"resets?\s+on\s+(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)


def _bounded_snippet(text: str, *, match_start: int, match_end: int) -> str:
    """Return a small, bounded excerpt around a regex match for safe logging.

    Never returns the unbounded raw text: callers pass full stderr/stdout,
    which may embed a token, header, or account identifier a future provider
    interpolates into its error message.
    """
    start = max(0, match_start - _LOG_SNIPPET_CONTEXT_CHARS)
    end = min(len(text), match_end + _LOG_SNIPPET_CONTEXT_CHARS)
    snippet = text[start:end].replace("\n", " ").strip()
    if len(snippet) > _LOG_SNIPPET_MAX_CHARS:
        snippet = snippet[:_LOG_SNIPPET_MAX_CHARS] + "…"
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


def parse_reset_hint(text: str, *, now_wall: float | None = None) -> float | None:
    """Parse an absolute reset time out of a provider quota error string.

    Recognizes exactly two forms — no general NLP date parser:

    * Absolute date: ``"resets at <ISO8601>"`` or ``"resets on <YYYY-MM-DD>"``.
    * Relative duration: ``"resets in N (hour|day|week)s?"``.

    Returns an absolute wall-clock UNIX timestamp (``time.time()`` epoch) if
    parseable, else ``None``. A parsed timestamp at or before ``now_wall`` is
    treated as unparseable (returns ``None``) — a guard against clock skew
    and stale-replay text producing a permanent or negative-duration
    cooldown. Malformed text that merely *matches* one of the clauses (e.g.
    an invalid calendar date) also returns ``None`` rather than raising.
    """
    if not text:
        return None
    now = time.time() if now_wall is None else now_wall

    match = _RESET_IN_RE.search(text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2).lower()
        parsed = now + amount * _RESET_UNIT_SECONDS[unit]
        # No real quota reset is ever a year+ out. Reject rather than let an
        # absurd or adversarial "resets in N weeks" value (N huge) overflow
        # datetime.fromtimestamp() downstream in _iso_from_wall() — that
        # would crash cool_down() instead of falling back to the default
        # cooldown, violating "never fail open, never fail permanently
        # closed".
        if parsed - now > _MAX_PARSEABLE_RESET_SECONDS:
            return None
        return parsed if parsed > now else None

    match = _RESET_AT_RE.search(text)
    if match:
        raw = match.group(1)
        parsed_dt = _parse_iso8601(raw)
        if parsed_dt is None:
            return None
        parsed = parsed_dt.timestamp()
        return parsed if parsed > now else None

    match = _RESET_ON_RE.search(text)
    if match:
        raw = match.group(1)
        try:
            parsed_dt = datetime.strptime(raw, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            return None
        parsed = parsed_dt.timestamp()
        return parsed if parsed > now else None

    return None


def _parse_iso8601(raw: str) -> datetime | None:
    """Best-effort ISO 8601 parse, tolerant of a trailing ``Z`` and no colon
    in the UTC offset. Returns ``None`` (never raises) on malformed input."""
    candidate = raw.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    else:
        # Normalize a bare "+0100"/"-0500" offset (no colon) to "+01:00".
        offset_match = re.search(r"([+-]\d{2})(\d{2})$", candidate)
        if offset_match:
            candidate = (
                candidate[: offset_match.start()]
                + offset_match.group(1)
                + ":"
                + offset_match.group(2)
            )
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def configured_providers() -> list[str]:
    """Return the de-duplicated, recognized provider order."""
    raw = os.environ.get("APIS_HARNESS_PROVIDERS", ",".join(DEFAULT_PROVIDER_ORDER))
    ordered: list[str] = []
    for item in raw.split(","):
        provider = item.strip().lower()
        if provider in PROVIDERS and provider not in ordered:
            ordered.append(provider)
    return ordered


def _headroom_path() -> Path:
    """Resolve the headroom file path — the single helper both read and write use."""
    configured_path = os.environ.get("APIS_HARNESS_HEADROOM_FILE", "").strip()
    return (
        Path(configured_path).expanduser()
        if configured_path
        else Path.home() / ".config" / "ateles" / "harness-headroom.json"
    )


def _iso_from_wall(wall_timestamp: float) -> str:
    return datetime.fromtimestamp(wall_timestamp, tz=timezone.utc).isoformat()


def _wall_from_iso(raw: str) -> float | None:
    parsed = _parse_iso8601(raw)
    return parsed.timestamp() if parsed is not None else None


def _write_headroom_entry(
    provider: str,
    *,
    headroom: float,
    cooldown_until: str | None,
    cooldown_reason: str | None,
) -> None:
    """Read-merge-write one provider's headroom-file entry.

    Preserves every other provider's entry verbatim — flat-float entries stay
    flat-float, object-shaped entries stay object-shaped. Only the touched
    provider is migrated to object shape. Writes atomically (tempfile +
    os.replace) since the router reads this file on every dispatch selection;
    a torn read would fail-open into equal weights. Any OSError is caught and
    logged — the in-memory cooldown still holds for this process even if the
    persisted record fails to write.
    """
    path = _headroom_path()
    existing: dict[str, object] = {}
    try:
        if path.is_file():
            raw = path.read_text(encoding="utf-8").strip()
            if raw:
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    existing = parsed
    except (OSError, TypeError, ValueError):
        existing = {}

    existing[provider] = {
        "headroom": headroom,
        "cooldown_until": cooldown_until,
        "cooldown_reason": cooldown_reason,
    }

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(existing, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        log.warning(
            f"[harness_router] failed to persist cooldown for {provider} to "
            f"{path}: {exc} — in-memory cooldown still applies this process"
        )


def _restore_expired_entry(path: Path, provider: str, entry: dict) -> None:
    """Clear an expired cooldown for ``provider`` in the on-disk file.

    Guards against double-logging: re-reads the file immediately before
    writing and only rewrites/logs if the on-disk value still shows the
    stale ``cooldown_until`` we are restoring — a second reader that raced us
    (or a repeated read in the same process) sees the already-cleared entry
    and stays silent.
    """
    try:
        raw = path.read_text(encoding="utf-8").strip()
        current = json.loads(raw) if raw else {}
    except (OSError, TypeError, ValueError):
        return
    if not isinstance(current, dict):
        return
    current_entry = current.get(provider)
    if not isinstance(current_entry, dict):
        return
    if current_entry.get("cooldown_until") != entry.get("cooldown_until"):
        # Already restored (by us or a racing reader) — stay silent.
        return

    current[provider] = {
        "headroom": 1.0,
        "cooldown_until": None,
        "cooldown_reason": None,
    }
    try:
        fd, tmp_name = tempfile.mkstemp(
            dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(current, handle, indent=2, sort_keys=True)
                handle.write("\n")
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
    except OSError as exc:
        log.warning(f"[harness_router] failed to rewrite restored headroom file: {exc}")
        return

    log.info(
        f"[harness_router] {provider} cooldown expired "
        f"(was until {entry.get('cooldown_until')}) — restored to headroom=1.0"
    )


_cooldown_metadata_cache: dict[str, dict] = {}


def configured_cooldown_metadata() -> dict[str, dict]:
    """Return the per-provider cooldown metadata parsed on the last
    ``configured_headroom()`` call (``cooldown_until``/``cooldown_reason``)."""
    return dict(_cooldown_metadata_cache)


def configured_headroom() -> dict[str, float]:
    """Return normalized per-provider bundled-plan headroom estimates.

    Accepts a bare number (legacy) OR an object
    ``{"headroom": float, "cooldown_until": str|None, "cooldown_reason": str|None}``
    per provider. A malformed object entry (missing/non-numeric ``headroom``)
    falls back to headroom ``1.0`` for that key only — it does NOT raise into
    the whole-file fail-open path, so one bad provider entry cannot silently
    reset every provider to equal weights.

    Auto-expiry on read: a provider whose ``cooldown_until`` has passed
    (wall-clock) is treated as not-cooling — the file is rewritten clearing
    that provider's cooldown fields and restoring headroom to 1.0.
    ``cooldown_reason: "manual"`` is never auto-restored.
    """
    env_raw = os.environ.get("APIS_HARNESS_HEADROOM", "").strip()
    headroom_path = _headroom_path()
    file_raw = ""
    if headroom_path.is_file():
        try:
            file_raw = headroom_path.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    values: Mapping[str, object] = {}
    source_is_file = False
    for raw, is_file in ((file_raw, True), (env_raw, False)):
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                values = parsed
                source_is_file = is_file
                break
        except (TypeError, ValueError):
            continue

    now_wall = time.time()
    metadata: dict[str, dict] = {}
    result: dict[str, float] = {}
    for provider in PROVIDERS:
        value = values.get(provider, 1.0)
        if isinstance(value, dict):
            headroom_value = value.get("headroom", 1.0)
            try:
                headroom_normalized = min(1.0, max(0.0, float(headroom_value)))
            except (TypeError, ValueError):
                headroom_normalized = 1.0

            cooldown_until = value.get("cooldown_until")
            cooldown_reason = value.get("cooldown_reason")

            if (
                source_is_file
                and cooldown_until
                and cooldown_reason != "manual"
            ):
                until_wall = _wall_from_iso(cooldown_until)
                if until_wall is not None and until_wall <= now_wall:
                    _restore_expired_entry(headroom_path, provider, value)
                    headroom_normalized = 1.0
                    cooldown_until = None
                    cooldown_reason = None

            result[provider] = headroom_normalized
            if cooldown_until or cooldown_reason:
                metadata[provider] = {
                    "cooldown_until": cooldown_until,
                    "cooldown_reason": cooldown_reason,
                }
        else:
            try:
                result[provider] = min(1.0, max(0.0, float(value)))
            except (TypeError, ValueError):
                result[provider] = 1.0

    _cooldown_metadata_cache.clear()
    _cooldown_metadata_cache.update(metadata)
    return result


def minimum_headroom() -> float:
    """Return the configured eligibility floor, normalized to ``0.0..1.0``."""
    try:
        return min(
            1.0,
            max(0.0, float(os.environ.get("APIS_HARNESS_MIN_HEADROOM", "0.05"))),
        )
    except ValueError:
        return 0.05


def _provider_exclusion_reason(
    provider: str,
    available: Mapping[str, str | None],
    *,
    headroom: Mapping[str, float],
    minimum: float,
    moment: float,
) -> str | None:
    """Return why one supported provider is ineligible, or ``None``."""
    if not available.get(provider):
        return "binary unavailable"
    if headroom[provider] <= minimum:
        return (
            f"headroom={headroom[provider]:.3f} is at or below "
            f"minimum={minimum:.3f}"
        )
    if _cooldown_until.get(provider, 0.0) > moment:
        metadata = _cooldown_metadata_cache.get(provider)
        if metadata and metadata.get("cooldown_until"):
            until_iso = metadata["cooldown_until"]
            reason = metadata.get("cooldown_reason") or "unknown"
            until_wall = _wall_from_iso(until_iso)
            remaining = (
                max(0.0, (until_wall - time.time()) / 3600.0)
                if until_wall is not None
                else None
            )
            remaining_str = f"{remaining:.1f}" if remaining is not None else "?"
            return (
                f"cooling down (cooldown_reason={reason}, "
                f"cooldown_until={until_iso}, ~{remaining_str}h remaining)"
            )
        return "cooling down"
    return None


def provider_exclusion_reason(
    provider: str,
    available: Mapping[str, str | None],
    *,
    now: float | None = None,
) -> str | None:
    """Explain why a hard-pinned provider cannot run right now."""
    normalized = provider.strip().lower()
    if normalized not in PROVIDERS:
        return "unsupported provider"
    return _provider_exclusion_reason(
        normalized,
        available,
        headroom=configured_headroom(),
        minimum=minimum_headroom(),
        moment=time.monotonic() if now is None else now,
    )


def usable_provider_names(
    available: Mapping[str, str | None], *, now: float | None = None
) -> set[str]:
    """Return configured providers passing the router's eligibility predicate."""
    moment = time.monotonic() if now is None else now
    headroom = configured_headroom()
    minimum = minimum_headroom()
    return {
        provider
        for provider in configured_providers()
        if _provider_exclusion_reason(
            provider,
            available,
            headroom=headroom,
            minimum=minimum,
            moment=moment,
        )
        is None
    }


def cool_down(provider: str, *, reason_text: str = "", now: float | None = None) -> None:
    """Temporarily remove a provider after a cap/auth/launch failure.

    When ``reason_text`` carries a parseable reset hint (see
    ``parse_reset_hint``), the provider is cooled down until THAT time rather
    than the fixed default duration — durably, via the headroom file — and
    auto-restored once it passes. When ``reason_text`` is non-empty but
    unparseable, falls back to the fixed ``APIS_HARNESS_COOLDOWN_SECONDS``
    duration (never fails open with no cooldown). When ``reason_text`` is
    empty, behaves exactly as before (backward compatible): fixed duration,
    no parse attempt.
    """
    try:
        default_duration = max(
            0.0, float(os.environ.get("APIS_HARNESS_COOLDOWN_SECONDS", "3600"))
        )
    except ValueError:
        default_duration = 3600.0

    monotonic_now = time.monotonic() if now is None else now
    wall_now = time.time()

    parsed_wall: float | None = None
    if reason_text:
        parsed_wall = parse_reset_hint(reason_text, now_wall=wall_now)

    if parsed_wall is not None:
        monotonic_deadline = monotonic_now + (parsed_wall - wall_now)
        cooldown_until_iso = _iso_from_wall(parsed_wall)
        cooldown_reason = "quota_error_parsed"
        match = _RESET_IN_RE.search(reason_text) or _RESET_AT_RE.search(
            reason_text
        ) or _RESET_ON_RE.search(reason_text)
        snippet = (
            _bounded_snippet(
                reason_text, match_start=match.start(), match_end=match.end()
            )
            if match
            else ""
        )
        log.info(
            f"[harness_router] {provider}: parsed quota reset time from error "
            f"text — cooling down until {cooldown_until_iso} "
            f"(matched: \"{snippet}\")"
        )
    else:
        monotonic_deadline = monotonic_now + default_duration
        cooldown_until_iso = _iso_from_wall(wall_now + default_duration)
        cooldown_reason = "quota_error_default"
        if reason_text:
            bounded = reason_text[: _LOG_SNIPPET_MAX_CHARS]
            suffix = "…" if len(reason_text) > _LOG_SNIPPET_MAX_CHARS else ""
            log.info(
                f"[harness_router] {provider}: quota/capacity error had no "
                "parseable reset time — falling back to fixed cooldown of "
                f"{default_duration:.0f}s (error excerpt: \"{bounded}{suffix}\")"
            )

    _cooldown_until[provider] = monotonic_deadline

    headroom = configured_headroom()
    _write_headroom_entry(
        provider,
        headroom=headroom.get(provider, 1.0),
        cooldown_until=cooldown_until_iso,
        cooldown_reason=cooldown_reason,
    )
    # Refresh the metadata cache immediately so _provider_exclusion_reason
    # reflects the new cooldown without requiring a separate read.
    configured_headroom()


def provider_candidates(
    available: Mapping[str, str | None],
    *,
    preferred: str | None = None,
    now: float | None = None,
) -> list[str]:
    """Return providers in attempt order, with a smooth weighted first choice.

    Headroom is used as the smooth-weighted-round-robin weight.  Equal
    headroom therefore alternates Claude → Codex → Cursor across dispatches,
    while unequal values naturally send more work to the roomier plan.
    Remaining eligible providers follow in descending headroom order so a
    capacity failure can fail over within the same dispatch.
    """
    moment = time.monotonic() if now is None else now
    order = configured_providers()
    if preferred is not None:
        normalized = preferred.strip().lower()
        order = [normalized] if normalized in PROVIDERS else []

    headroom = configured_headroom()
    minimum = minimum_headroom()

    eligible = [
        provider
        for provider in order
        if _provider_exclusion_reason(
            provider,
            available,
            headroom=headroom,
            minimum=minimum,
            moment=moment,
        )
        is None
    ]
    if not eligible:
        return []

    for provider in list(_current_weights):
        if provider not in eligible:
            _current_weights.pop(provider, None)
    total = sum(headroom[provider] for provider in eligible)
    for provider in eligible:
        _current_weights[provider] = (
            _current_weights.get(provider, 0.0) + headroom[provider]
        )

    order_index = {provider: index for index, provider in enumerate(order)}
    first = max(
        eligible,
        key=lambda provider: (
            _current_weights[provider],
            -order_index[provider],
        ),
    )
    _current_weights[first] -= total

    remaining = sorted(
        (provider for provider in eligible if provider != first),
        key=lambda provider: (-headroom[provider], order_index[provider]),
    )
    return [first, *remaining]


def reset_state() -> None:
    """Clear process-local balancing/cooldown state (tests and operator reloads)."""
    _current_weights.clear()
    _cooldown_until.clear()
    _cooldown_metadata_cache.clear()


def cooling_providers(*, now: float | None = None) -> set[str]:
    """Expose active cooldowns for diagnostics without leaking timestamps."""
    moment = time.monotonic() if now is None else now
    return {
        provider
        for provider, until in _cooldown_until.items()
        if until > moment
    }
