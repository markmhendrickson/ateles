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

    Entries are a bare number (legacy) or an object
    ``{"headroom": float, "cooldown_until": ISO-8601|null,
    "cooldown_reason": str|null}``.  An object whose ``cooldown_until`` has
    passed no longer applies, so a zero written at exhaustion lifts at the
    provider's reset without a hand edit; ``cooldown_reason: "manual"`` never
    expires.  A bare number (or an object with no ``cooldown_until``) is
    superseded by a live usage observation made after the file was last
    written, and never outvotes a live exhaustion report.

``APIS_HARNESS_USAGE_FILE``
    Live plan-usage snapshot, written by ``record_usage`` / ``record_exhausted``
    (see ``execution/scripts/harness_usage.py``).  Defaults to
    ``~/.config/ateles/harness-usage.json``.  Per provider::

        {"observed_at": ISO, "exhausted_until": ISO|null,
         "windows": [{"name": "weekly", "used_percent": 3, "resets_at": ISO}]}

    Live headroom is ``1 - max(used_percent)/100`` over windows that have not
    yet reset (a window past its ``resets_at`` counts as unused), and ``0.0``
    while ``exhausted_until`` is in the future.  An observation older than
    ``APIS_HARNESS_USAGE_MAX_AGE_SECONDS`` (default 21600) whose windows have
    not reset carries no opinion.

    A provider that refused with a session/usage-limit message is also held out
    by a per-provider ``cooling`` object in the same file, written by
    ``record_cooling`` (``{"until": ISO, "reason": str, "observed_at": ISO}``).
    Unlike the process-local ``cool_down`` timer, it survives across the
    short-lived ``dispatch_role`` processes, and ends at the reset the refusal
    stated rather than after a flat hour.  It expires on its own; a fresh usage
    observation or exhaustion report leaves it in force.

``APIS_HARNESS_MIN_HEADROOM``
    Providers at or below this value are held out.  Default: 0.05.

``APIS_HARNESS_COOLDOWN_SECONDS``
    How long a provider is held out after a quota/auth failure.  Default: 3600.

No metered/API-key fallback is represented here.  That hard boundary is
enforced by ``skill_runner`` when it constructs each child environment.

``claude-local`` (``local_provider.py``) is recognized but never enters the
weighted rotation above, even if named in ``APIS_HARNESS_PROVIDERS``: it is
reached only when pinned, or placed first for an eligible mechanical work
class via ``provider_candidates(local_first=True)``, with the frontier
providers behind it as the fallback.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import tempfile
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

FRONTIER_PROVIDERS = ("claude", "codex", "cursor")
LOCAL_PROVIDERS = ("claude-local",)
PROVIDERS = FRONTIER_PROVIDERS + LOCAL_PROVIDERS
DEFAULT_PROVIDER_ORDER = FRONTIER_PROVIDERS

_current_weights: dict[str, float] = {}
_cooldown_until: dict[str, float] = {}


def configured_providers() -> list[str]:
    """Return the de-duplicated, recognized frontier provider order."""
    raw = os.environ.get("APIS_HARNESS_PROVIDERS", ",".join(DEFAULT_PROVIDER_ORDER))
    ordered: list[str] = []
    for item in raw.split(","):
        provider = item.strip().lower()
        if provider in FRONTIER_PROVIDERS and provider not in ordered:
            ordered.append(provider)
    return ordered


def _headroom_path() -> Path:
    configured_path = os.environ.get("APIS_HARNESS_HEADROOM_FILE", "").strip()
    return (
        Path(configured_path).expanduser()
        if configured_path
        else Path.home() / ".config" / "ateles" / "harness-headroom.json"
    )


def _usage_path() -> Path:
    configured_path = os.environ.get("APIS_HARNESS_USAGE_FILE", "").strip()
    return (
        Path(configured_path).expanduser()
        if configured_path
        else Path.home() / ".config" / "ateles" / "harness-usage.json"
    )


def _wall_from_iso(raw: object) -> float | None:
    """Parse an ISO-8601 timestamp to epoch seconds; naive values are UTC."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _iso_from_wall(wall: float) -> str:
    return datetime.fromtimestamp(wall, tz=timezone.utc).isoformat()


def _clamp(value: object) -> float | None:
    try:
        return min(1.0, max(0.0, float(value)))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        raw = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
        parsed = json.loads(raw) if raw else {}
    except (OSError, TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _override_values() -> tuple[Mapping[str, object], float | None]:
    """Return the operator override layer and the file's write time."""
    overrides, written_at, _origin = _override_layer()
    return overrides, written_at


def headroom_override_origin() -> str:
    """Which override source is in force: ``"file"``, ``"env"``, or ``"none"``."""
    return _override_layer()[2]


def _override_layer() -> tuple[Mapping[str, object], float | None, str]:
    """Return the operator override layer, the file's write time, and its origin.

    File first, then ``APIS_HARNESS_HEADROOM``: the first that parses wins, as
    before.  The write time is ``None`` for the env source, which no live
    observation supersedes.  The origin is ``"file"``, ``"env"``, or ``"none"``
    (no override layer parsed), so a refusal can name where a value came from.
    """
    headroom_path = _headroom_path()
    file_raw = ""
    file_mtime: float | None = None
    if headroom_path.is_file():
        try:
            file_raw = headroom_path.read_text(encoding="utf-8").strip()
            file_mtime = headroom_path.stat().st_mtime
        except OSError:
            pass
    env_raw = os.environ.get("APIS_HARNESS_HEADROOM", "").strip()
    for raw, written_at, origin in (
        (file_raw, file_mtime, "file"),
        (env_raw, None, "env"),
    ):
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed, written_at, origin
    return {}, None, "none"


def live_headroom(provider: str, *, now_wall: float | None = None) -> float | None:
    """Return headroom derived from the live usage snapshot, or ``None``.

    ``None`` means the snapshot has no current opinion on this provider: no
    entry, no usable windows, or an observation too old to trust whose windows
    have not yet reset.
    """
    moment = time.time() if now_wall is None else now_wall
    entry = _read_json_object(_usage_path()).get(provider)
    if not isinstance(entry, dict):
        return None
    exhausted_until = _wall_from_iso(entry.get("exhausted_until"))
    if exhausted_until is not None and exhausted_until > moment:
        return 0.0
    windows = entry.get("windows")
    if not isinstance(windows, list):
        return None
    if exhausted_until is not None and not windows:
        # The reported reset has passed and nothing newer was observed: the
        # plan has reset, so the provider is back to full headroom.
        return 1.0
    try:
        max_age = float(os.environ.get("APIS_HARNESS_USAGE_MAX_AGE_SECONDS", "21600"))
    except ValueError:
        max_age = 21600.0
    observed_at = _wall_from_iso(entry.get("observed_at"))
    fresh = observed_at is not None and moment - observed_at <= max_age
    if any(_window_problem(window) for window in windows):
        # A malformed reading is UNKNOWN, never exhausted: fall through to the
        # headroom file.  (The usage gate below refuses a gated provider on it.)
        return None
    used: list[float] = []
    all_reset = bool(windows)
    for window in windows:
        fraction = _clamp(_percent(window.get("used_percent")))
        if fraction is None:
            all_reset = False
            continue
        resets_at = _wall_from_iso(window.get("resets_at"))
        if resets_at is not None and resets_at <= moment:
            used.append(0.0)
        else:
            all_reset = False
            used.append(fraction)
    if not used or not (fresh or all_reset):
        return None
    return round(1.0 - max(used), 6)


def _percent(value: object) -> float | None:
    """A ``0..100`` percent as a ``0..1`` fraction, or ``None`` when unusable.

    Non-numeric, non-finite (``Infinity``/``NaN``) and out-of-range values are
    all ``None``: a reading that is not a percentage is not a reading, and the
    old clamp turned ``Infinity`` into "fully used", which held Claude out of
    every dispatch on a garbage write.
    """
    if isinstance(value, bool):
        return None
    try:
        percent = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not math.isfinite(percent) or percent < 0.0 or percent > 100.0:
        return None
    return percent / 100.0


def _window_problem(window: object) -> str | None:
    """Why one recorded usage window is not a usable reading, or ``None``."""
    if not isinstance(window, dict):
        return "window is not an object"
    name = window.get("name")
    if not isinstance(name, str) or not name.strip():
        return "window has no name"
    if _percent(window.get("used_percent")) is None:
        return f"window {name!r} has no valid used_percent (need a finite 0-100)"
    if window.get("resets_at") is not None and _wall_from_iso(window["resets_at"]) is None:
        return f"window {name!r} has an unparseable resets_at"
    return None


def _live_observed_at(provider: str) -> float | None:
    entry = _read_json_object(_usage_path()).get(provider)
    if not isinstance(entry, dict):
        return None
    return _wall_from_iso(entry.get("observed_at"))


# Where a provider's resolved headroom came from.  These name the four
# precedence tiers ``configured_headroom`` documents, so a caller that refuses
# on a ``0.0`` can point the operator at the source that actually won instead
# of guessing (the override file is not always the winner).
HEADROOM_SOURCE_MANUAL_OVERRIDE = "manual_override"
HEADROOM_SOURCE_DATED_OVERRIDE = "dated_cooldown_override"
HEADROOM_SOURCE_LIVE_USAGE = "live_usage_snapshot"
HEADROOM_SOURCE_UNDATED_OVERRIDE = "undated_override"
HEADROOM_SOURCE_DEFAULT = "default"


def headroom_resolution(
    *, now_wall: float | None = None
) -> dict[str, tuple[float, str]]:
    """Return ``{provider: (headroom, source)}`` for every provider.

    This is the single implementation of the precedence; ``configured_headroom``
    is its value-only projection.  ``source`` is one of the
    ``HEADROOM_SOURCE_*`` constants and names the tier that produced the value.
    """
    moment = time.time() if now_wall is None else now_wall
    overrides, written_at = _override_values()

    result: dict[str, tuple[float, str]] = {}
    for provider in PROVIDERS:
        raw = overrides.get(provider)
        dated: float | None = None
        dated_source = HEADROOM_SOURCE_DATED_OVERRIDE
        undated: float | None = None
        if isinstance(raw, dict):
            value = _clamp(raw.get("headroom"))
            until = _wall_from_iso(raw.get("cooldown_until"))
            if value is not None:
                if raw.get("cooldown_reason") == "manual":
                    dated = value
                    dated_source = HEADROOM_SOURCE_MANUAL_OVERRIDE
                elif until is not None:
                    if until > moment:
                        dated = value
                else:
                    undated = value
        elif raw is not None:
            undated = _clamp(raw)
            if undated is None:
                undated = 1.0

        if dated is not None:
            result[provider] = (dated, dated_source)
            continue
        live = live_headroom(provider, now_wall=moment)
        if live is not None:
            observed = _live_observed_at(provider)
            # A reported exhaustion is never outvoted by a hand-set number;
            # otherwise the live reading wins over an undated override only
            # when it was observed after the file was last written.  The file
            # time is per file, not per entry, so editing another provider's
            # line keeps this provider's undated override in force.
            superseded = (
                undated is None
                or live == 0.0
                or (
                    written_at is not None
                    and observed is not None
                    and observed >= written_at
                )
            )
            if superseded:
                result[provider] = (live, HEADROOM_SOURCE_LIVE_USAGE)
                continue
        if undated is None:
            result[provider] = (1.0, HEADROOM_SOURCE_DEFAULT)
        else:
            result[provider] = (undated, HEADROOM_SOURCE_UNDATED_OVERRIDE)
    return result


def configured_headroom(*, now_wall: float | None = None) -> dict[str, float]:
    """Return normalized per-provider bundled-plan headroom estimates.

    Precedence per provider (see ``headroom_resolution`` for the source label):

    1. An override object with ``cooldown_reason: "manual"``, or with a
       ``cooldown_until`` still in the future.
    2. The live usage snapshot (``live_headroom``), when it has an opinion and
       any undated override was written before that observation.  A live
       ``0.0`` is never outvoted by a hand-set undated number.
    3. An undated override (bare number, or object without
       ``cooldown_until``) from the file or ``APIS_HARNESS_HEADROOM``.
    4. ``1.0``.

    An override whose ``cooldown_until`` has passed is ignored, so a zero set
    at exhaustion stops blocking at the provider's reset.
    """
    return {
        provider: value
        for provider, (value, _source) in headroom_resolution(
            now_wall=now_wall
        ).items()
    }


def _write_usage_entry(
    provider: str,
    entry: Mapping[str, object] | None = None,
    *,
    update: Mapping[str, object] | None = None,
) -> None:
    """Read-merge-write one provider's usage entry atomically.

    ``entry`` replaces the provider's observation (usage windows / exhaustion)
    but keeps its ``cooling`` window, which is a separate fact with its own
    expiry; ``update`` instead merges keys into the existing entry, leaving the
    rest as recorded.

    Serialized on a sibling lock file so two concurrent recorders cannot each
    read the old snapshot and drop the other's entry; the replace itself is
    atomic, so readers never see a torn file and never take the lock.
    """
    path = _usage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(f".{path.name}.lock"), "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        existing = _read_json_object(path)
        current = existing.get(provider)
        current = current if isinstance(current, dict) else {}
        if update is not None:
            merged = {**current, **dict(update)}
        else:
            merged = dict(entry or {})
            if "cooling" in current:
                merged.setdefault("cooling", current["cooling"])
        existing[provider] = merged
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


def record_usage(
    provider: str,
    windows: Iterable[Mapping[str, object]],
    *,
    observed_at: float | None = None,
) -> None:
    """Record a live plan-usage observation for one provider.

    Each window carries ``name``, ``used_percent`` (0-100) and optionally
    ``resets_at`` (ISO-8601).  A fresh observation clears any recorded
    exhaustion that has not been re-reported.
    """
    normalized = provider.strip().lower()
    if normalized not in PROVIDERS:
        raise ValueError(f"unsupported provider: {provider!r}")
    rendered = []
    for window in windows:
        problem = _window_problem(window)
        if problem is not None:
            raise ValueError(f"refusing to record a malformed usage window: {problem}")
        item: dict[str, object] = {
            "name": str(window["name"]).strip(),
            "used_percent": float(window["used_percent"]),  # type: ignore[arg-type]
        }
        if window.get("resets_at") is not None:
            item["resets_at"] = window["resets_at"]
        rendered.append(item)
    if not rendered:
        raise ValueError("refusing to record a usage observation with no windows")
    _write_usage_entry(
        normalized,
        {
            "observed_at": _iso_from_wall(
                time.time() if observed_at is None else observed_at
            ),
            "windows": rendered,
            "exhausted_until": None,
        },
    )


def record_exhausted(
    provider: str, until: float, *, observed_at: float | None = None
) -> None:
    """Record that a provider reported exhaustion until wall time ``until``.

    Selection holds the provider out until then and restores it afterwards
    without any edit, which is the defect a hand-set zero could not avoid.
    """
    normalized = provider.strip().lower()
    if normalized not in PROVIDERS:
        raise ValueError(f"unsupported provider: {provider!r}")
    _write_usage_entry(
        normalized,
        {
            "observed_at": _iso_from_wall(
                time.time() if observed_at is None else observed_at
            ),
            "windows": [],
            "exhausted_until": _iso_from_wall(until),
        },
    )


def record_cooling(
    provider: str,
    until: float,
    *,
    reason: str,
    observed_at: float | None = None,
) -> None:
    """Persist a cooling window: hold ``provider`` out until wall time ``until``.

    Read by every later selection in any process, so a session-limit refusal in
    one ``dispatch_role`` run stops the next from spawning the same dead CLI.
    Other usage facts for the provider (windows, exhaustion) are untouched.
    """
    normalized = provider.strip().lower()
    if normalized not in PROVIDERS:
        raise ValueError(f"unsupported provider: {provider!r}")
    _write_usage_entry(
        normalized,
        update={
            "cooling": {
                "until": _iso_from_wall(until),
                "reason": reason,
                "observed_at": _iso_from_wall(
                    time.time() if observed_at is None else observed_at
                ),
            }
        },
    )


def record_probe_failure(
    provider: str, detail: str, *, observed_at: float | None = None
) -> None:
    """Remember why the last automatic usage refresh failed.

    Kept beside the reading (cleared by the next successful ``record_usage``) so
    a refusal and ``harness_usage.py show`` can say WHY the reading is not being
    refreshed (not logged in, binary missing, report shape changed) instead of
    only that it is stale.  Other usage facts are untouched.
    """
    normalized = provider.strip().lower()
    if normalized not in PROVIDERS:
        raise ValueError(f"unsupported provider: {provider!r}")
    _write_usage_entry(
        normalized,
        update={
            "last_probe_failure": {
                "at": _iso_from_wall(time.time() if observed_at is None else observed_at),
                "detail": " ".join(str(detail).split())[:300],
            }
        },
    )


def cooled_until_all(
    available: Mapping[str, str | None], *, now_wall: float | None = None
) -> dict[str, dict[str, object]] | None:
    """Return each provider's cooling window when cooling is why nothing can run.

    ``None`` unless at least one configured provider is otherwise eligible
    (binary present, headroom above the floor) and EVERY such provider is inside
    a persisted cooling window.  A provider with no binary or no headroom is not
    "eligible" and does not turn a cooled-everywhere state into a generic
    exhaustion; conversely one merely uncooled provider means selection has an
    answer and this is not the case.
    """
    moment = time.time() if now_wall is None else now_wall
    headroom = configured_headroom(now_wall=moment)
    minimum = minimum_headroom()
    windows: dict[str, dict[str, object]] = {}
    for provider in configured_providers():
        if not available.get(provider) or headroom[provider] <= minimum:
            continue
        cooling = persisted_cooling(provider, now_wall=moment)
        if cooling is None:
            return None
        windows[provider] = cooling
    return windows or None


def minimum_headroom() -> float:
    """Return the configured eligibility floor, normalized to ``0.0..1.0``."""
    try:
        return min(
            1.0,
            max(0.0, float(os.environ.get("APIS_HARNESS_MIN_HEADROOM", "0.05"))),
        )
    except ValueError:
        return 0.05


COOLED_UNTIL = "cooled until"


def persisted_cooling(
    provider: str, *, now_wall: float | None = None
) -> dict[str, object] | None:
    """Return the provider's live cooling window from the usage file, or ``None``.

    A window is live while its ``until`` is in the future; an expired, absent or
    malformed one is no opinion, so a provider comes back at the reset without
    any edit.  Keys: ``until`` (epoch), ``until_iso``, ``reason``.
    """
    moment = time.time() if now_wall is None else now_wall
    entry = _read_json_object(_usage_path()).get(provider)
    cooling = entry.get("cooling") if isinstance(entry, dict) else None
    if not isinstance(cooling, dict):
        return None
    until = _wall_from_iso(cooling.get("until"))
    if until is None or until <= moment:
        return None
    return {
        "until": until,
        "until_iso": cooling["until"],
        "reason": str(cooling.get("reason") or "capacity"),
    }


def usage_windows(provider: str) -> list[dict[str, object]]:
    """Return the recorded usage windows (session, weekly, ...) for a provider."""
    entry = _read_json_object(_usage_path()).get(provider)
    windows = entry.get("windows") if isinstance(entry, dict) else None
    return [dict(w) for w in windows if isinstance(w, dict)] if isinstance(windows, list) else []


def render_wall(wall: float) -> str:
    """Local-zone ISO-8601 with offset, for messages an operator reads."""
    return datetime.fromtimestamp(wall).astimezone().isoformat(timespec="minutes")


# ---------------------------------------------------------------------------
# Usage gate: freshness and weekly pacing for frontier dispatch
#
# Incident 2026-09-29: over 60% of the weekly Claude allowance went in under a
# day while the snapshot still read 20%, observed seven hours earlier.  Headroom
# and the 60% ceiling both acted on a reading nobody had refreshed, so nothing
# throttled.  Among dispatches routed through this router the gate is the single
# place that decides whether a new frontier dispatch may start, from the snapshot
# alone: it fails CLOSED when the reading is missing, malformed or older than a
# bound, and it paces the week.  Daemons that launch the ``claude`` CLI directly
# never went through this router and are not covered.
#
# Scope: only providers with an automatic live feeder are gated
# (``APIS_USAGE_GATED_PROVIDERS``, default ``claude``, fed by ``usage_probe``).
# A provider with no live source (codex, cursor) cannot be refused for a stale
# reading nobody can refresh, so it keeps the exhaustion-record behaviour.
# Local providers are never gated: mechanical work keeps running.
# ---------------------------------------------------------------------------

WEEK_SECONDS = 7 * 24 * 3600.0
WEEKLY_WINDOW_NAMES = ("weekly_all", "weekly")

GATE_OK = "ok"
GATE_MISSING = "missing"
GATE_MALFORMED = "malformed"
GATE_STALE = "stale"
GATE_PACED = "paced"


@dataclass(frozen=True)
class UsageGate:
    """The gate's verdict for one provider, with the numbers behind it."""

    provider: str
    allowed: bool
    code: str
    message: str
    observed_at: float | None = None
    age_seconds: float | None = None
    max_age_seconds: float | None = None
    weekly_used_percent: float | None = None
    weekly_resets_at: float | None = None
    elapsed_fraction: float | None = None
    pace_percent: float | None = None
    ceiling_percent: float | None = None
    burst_percent: float | None = None
    # When the refusal clears if nothing more is used (pace), or when a retry
    # is worthwhile (stale/missing/malformed); ``None`` when allowed.
    returns_at: float | None = None
    # Why the last automatic refresh failed, when it did (see record_probe_failure).
    probe_failure: str | None = None


REFRESH_HINT = (
    "To fix: run `python3 execution/scripts/harness_usage.py refresh` "
    "(see docs/runbooks/harness-headroom-restore.md, section 'Usage gate')"
)


def _probe_failure_text(entry: object, moment: float) -> str | None:
    failure = entry.get("last_probe_failure") if isinstance(entry, dict) else None
    if not isinstance(failure, dict) or not failure.get("detail"):
        return None
    at = _wall_from_iso(failure.get("at"))
    when = f" at {render_wall(at)}" if at is not None else ""
    text = f"last automatic refresh failed{when}: {failure['detail']}"
    if at is not None:
        retry = at + usage_probe_backoff_seconds()
        if retry > moment:
            text += f" (next automatic retry after {render_wall(retry)})"
    return text


def _env_float(name: str, default: float, *, low: float, high: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return min(high, max(low, value)) if math.isfinite(value) else default


def usage_gate_enabled() -> bool:
    return os.environ.get("APIS_USAGE_GATE", "on").strip().lower() not in (
        "off", "0", "false", "no",
    )


def usage_gated_providers() -> tuple[str, ...]:
    raw = os.environ.get("APIS_USAGE_GATED_PROVIDERS", "claude")
    return tuple(
        p for p in (item.strip().lower() for item in raw.split(","))
        if p in FRONTIER_PROVIDERS
    )


def usage_stale_seconds() -> float:
    """Longest a reading may age before frontier dispatch is refused (30 min)."""
    return _env_float("APIS_USAGE_STALE_SECONDS", 1800.0, low=60.0, high=6 * 3600.0)


def usage_refresh_seconds() -> float:
    """Age at which a dispatch refreshes the reading first; always < the bound."""
    return min(
        _env_float("APIS_USAGE_REFRESH_SECONDS", 600.0, low=30.0, high=6 * 3600.0),
        usage_stale_seconds() / 2.0,
    )


def usage_retry_seconds() -> float:
    return _env_float("APIS_USAGE_RETRY_SECONDS", 600.0, low=30.0, high=6 * 3600.0)


def usage_probe_backoff_seconds() -> float:
    """After a failed automatic refresh, wait this long before probing again (5 min).

    A logged-out or missing CLI leaves the reading stale, so without a backoff
    every dispatch (one per lens) would relaunch the same failing probe.
    """
    return _env_float("APIS_USAGE_PROBE_BACKOFF_SECONDS", 300.0, low=0.0, high=6 * 3600.0)


def weekly_ceiling_percent() -> float:
    """Share of the weekly allowance the swarm may use (operator ruling: 60)."""
    return _env_float("APIS_USAGE_WEEKLY_CEILING_PERCENT", 60.0, low=1.0, high=100.0)


def pace_burst_percent() -> float:
    """Points above the elapsed-fraction pace line the swarm may run ahead."""
    return _env_float("APIS_USAGE_PACE_BURST_PERCENT", 10.0, low=0.0, high=100.0)


def _refused(
    provider: str, code: str, message: str, moment: float, *,
    entry: object = None, **fields: object
) -> UsageGate:
    """A refusal for a reading that needs REFRESHING: says how, and why it failed."""
    failure = _probe_failure_text(entry, moment)
    text = f"{message}; {failure}" if failure else message
    return UsageGate(
        provider=provider,
        allowed=False,
        code=code,
        message=f"{text}. {REFRESH_HINT}",
        returns_at=moment + usage_retry_seconds(),
        probe_failure=failure,
        **fields,  # type: ignore[arg-type]
    )


def usage_gate(provider: str, *, now_wall: float | None = None) -> UsageGate | None:
    """Decide whether ``provider`` may start a new frontier dispatch.

    ``None`` when the provider is not gated (gate off, or no live feeder for it).
    Otherwise a ``UsageGate`` that is refused for a missing, malformed or stale
    reading, or when weekly use has reached the elapsed-fraction pace line
    (``ceiling x elapsed + burst``, capped at the ceiling).  Deliberately reads
    only the snapshot, so it is the same answer in every process.
    """
    normalized = provider.strip().lower()
    if not usage_gate_enabled() or normalized not in usage_gated_providers():
        return None
    moment = time.time() if now_wall is None else now_wall
    max_age = usage_stale_seconds()
    ceiling = weekly_ceiling_percent()
    burst = pace_burst_percent()
    common: dict[str, object] = {
        "max_age_seconds": max_age, "ceiling_percent": ceiling, "burst_percent": burst,
    }
    entry = _read_json_object(_usage_path()).get(normalized)
    if not isinstance(entry, dict) or (
        "observed_at" not in entry and "windows" not in entry
        and "exhausted_until" not in entry
    ):
        return _refused(
            normalized, GATE_MISSING,
            f"usage reading missing for {normalized} (never observed); "
            "refusing new frontier dispatch until one is recorded",
            moment, entry=entry, **common,
        )
    exhausted_until = _wall_from_iso(entry.get("exhausted_until"))
    if exhausted_until is not None and exhausted_until > moment:
        # A reported exhaustion is itself a current reading; headroom (0.0)
        # already holds the provider out until its stated reset.
        return UsageGate(normalized, True, GATE_OK, "exhaustion recorded", **common)  # type: ignore[arg-type]
    observed_at = _wall_from_iso(entry.get("observed_at"))
    windows = entry.get("windows")
    problem = None
    if observed_at is None:
        problem = "no valid observed_at"
    elif observed_at > moment + 300.0:
        problem = "observed_at is in the future"
    elif not isinstance(windows, list) or not windows:
        problem = "no usage windows"
    else:
        problem = next((p for p in map(_window_problem, windows) if p), None)
    if problem is not None:
        return _refused(
            normalized, GATE_MALFORMED,
            f"usage reading malformed for {normalized} ({problem}); "
            "refusing new frontier dispatch until a valid one is recorded",
            moment, entry=entry, observed_at=observed_at, **common,
        )
    assert observed_at is not None and isinstance(windows, list)
    age = max(0.0, moment - observed_at)
    common.update(observed_at=observed_at, age_seconds=age)
    if age > max_age:
        return _refused(
            normalized, GATE_STALE,
            f"usage reading stale since {render_wall(observed_at)} "
            f"({int(age // 60)} min old, bound {int(max_age // 60)} min); "
            "refusing new frontier dispatch until it is refreshed",
            moment, entry=entry, **common,
        )
    weekly = next(
        (w for w in windows if str(w.get("name")).strip() in WEEKLY_WINDOW_NAMES), None
    )
    resets_at = _wall_from_iso(weekly.get("resets_at")) if weekly else None
    if weekly is None or resets_at is None:
        return _refused(
            normalized, GATE_MALFORMED,
            f"usage reading for {normalized} has no weekly window with a reset "
            "time, so the weekly pace cannot be computed; refusing new frontier "
            "dispatch until one is recorded (an account with no weekly window "
            "stays refused; the only way past it is APIS_USAGE_GATE=off)",
            moment, entry=entry, **common,
        )
    if resets_at <= moment:
        return _refused(
            normalized, GATE_STALE,
            f"usage reading stale since {render_wall(observed_at)}: its weekly "
            f"window reset at {render_wall(resets_at)}; refusing new frontier "
            "dispatch until it is refreshed",
            moment, entry=entry, **common,
        )
    used = float(weekly["used_percent"])
    start = resets_at - WEEK_SECONDS
    elapsed = min(1.0, max(0.0, (moment - start) / WEEK_SECONDS))
    pace = min(ceiling, ceiling * elapsed + burst)
    common.update(
        weekly_used_percent=used, weekly_resets_at=resets_at,
        elapsed_fraction=elapsed, pace_percent=pace,
    )
    if used < pace:
        return UsageGate(normalized, True, GATE_OK, "within the weekly pace line", **common)  # type: ignore[arg-type]
    if used >= ceiling:
        returns_at = resets_at
    else:
        # Solve ceiling*e + burst = used for the moment the pace line catches up
        # (a minute of slack so it is strictly above), assuming no further use.
        returns_at = min(
            resets_at, start + ((used - burst) / ceiling) * WEEK_SECONDS + 60.0
        )
    return UsageGate(
        normalized, False, GATE_PACED,
        f"weekly usage {used:.0f}% is at or above the pace line {pace:.1f}% "
        f"({ceiling:.0f}% ceiling x {elapsed:.0%} of the week elapsed + "
        f"{burst:.0f} burst); frontier capacity for {normalized} returns at "
        f"{render_wall(returns_at)} if nothing more is used (the weekly percent "
        "is the whole account, so the operator's own sessions count toward it)",
        returns_at=returns_at, **common,  # type: ignore[arg-type]
    )


def usage_gate_refusals_all(
    available: Mapping[str, str | None], *, now_wall: float | None = None
) -> dict[str, UsageGate] | None:
    """Each gate refusal when the gate is why no frontier provider can run.

    ``None`` unless at least one configured provider is otherwise eligible
    (binary present, headroom above the floor, not cooled) and EVERY such
    provider is gate-refused.  Mirrors ``cooled_until_all`` so the dispatcher
    reports a stale or paced refusal as what it is, not as generic exhaustion.
    """
    moment = time.time() if now_wall is None else now_wall
    headroom = configured_headroom(now_wall=moment)
    minimum = minimum_headroom()
    refusals: dict[str, UsageGate] = {}
    for provider in configured_providers():
        if not available.get(provider) or headroom[provider] <= minimum:
            continue
        if persisted_cooling(provider, now_wall=moment) is not None:
            continue
        gate = usage_gate(provider, now_wall=moment)
        if gate is None or gate.allowed:
            return None
        refusals[provider] = gate
    return refusals or None


def _provider_exclusion_reason(
    provider: str,
    available: Mapping[str, str | None],
    *,
    headroom: Mapping[str, float],
    minimum: float,
    moment: float,
    moment_wall: float | None = None,
) -> str | None:
    """Return why one supported provider is ineligible, or ``None``."""
    if not available.get(provider):
        return "binary unavailable"
    if headroom[provider] <= minimum:
        return f"headroom={headroom[provider]:.3f} is at or below minimum={minimum:.3f}"
    cooling = persisted_cooling(provider, now_wall=moment_wall)
    if cooling is not None:
        return f"{COOLED_UNTIL} {render_wall(float(cooling['until']))} ({cooling['reason']})"
    gate = usage_gate(provider, now_wall=moment_wall)
    if gate is not None and not gate.allowed:
        return gate.message
    if _cooldown_until.get(provider, 0.0) > moment:
        return "cooling down"
    return None


def provider_exclusion_reason(
    provider: str,
    available: Mapping[str, str | None],
    *,
    now: float | None = None,
    now_wall: float | None = None,
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
        moment_wall=now_wall,
    )


def usable_provider_names(
    available: Mapping[str, str | None],
    *,
    now: float | None = None,
    now_wall: float | None = None,
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
            moment_wall=now_wall,
        )
        is None
    }


def cool_down(provider: str, *, now: float | None = None) -> None:
    """Temporarily remove a provider after a cap/auth/launch failure.

    Process-local: it lasts only as long as this interpreter.  A capacity
    refusal that states its reset should ALSO be recorded with
    ``record_cooling`` so the window outlives the process.
    """
    try:
        duration = max(
            0.0, float(os.environ.get("APIS_HARNESS_COOLDOWN_SECONDS", "3600"))
        )
    except ValueError:
        duration = 3600.0
    _cooldown_until[provider] = (time.monotonic() if now is None else now) + duration


def provider_candidates(
    available: Mapping[str, str | None],
    *,
    preferred: str | None = None,
    now: float | None = None,
    local_first: bool = False,
    now_wall: float | None = None,
) -> list[str]:
    """Return providers in attempt order, with a smooth weighted first choice.

    Headroom is used as the smooth-weighted-round-robin weight.  Equal
    headroom therefore alternates Claude → Codex → Cursor across dispatches,
    while unequal values naturally send more work to the roomier plan.
    Remaining eligible providers follow in descending headroom order so a
    capacity failure can fail over within the same dispatch.

    ``local_first`` (unpinned runs only) puts an eligible ``claude-local``
    ahead of that frontier list, which stays behind it as the fallback.
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
            moment_wall=now_wall,
        )
        is None
    ]
    local = [
        provider
        for provider in LOCAL_PROVIDERS
        if local_first
        and preferred is None
        and _provider_exclusion_reason(
            provider,
            available,
            headroom=headroom,
            minimum=minimum,
            moment=moment,
            moment_wall=now_wall,
        )
        is None
    ]
    if not eligible:
        return local

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
    return [*local, first, *remaining]


def reset_state() -> None:
    """Clear process-local balancing/cooldown state (tests and operator reloads)."""
    _current_weights.clear()
    _cooldown_until.clear()


def cooling_providers(
    *, now: float | None = None, now_wall: float | None = None
) -> set[str]:
    """Expose active cooldowns (process-local and persisted) for diagnostics."""
    moment = time.monotonic() if now is None else now
    cooling = {
        provider for provider, until in _cooldown_until.items() if until > moment
    }
    cooling.update(
        provider
        for provider in PROVIDERS
        if persisted_cooling(provider, now_wall=now_wall) is not None
    )
    return cooling
