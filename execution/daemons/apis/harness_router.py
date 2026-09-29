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
import os
import tempfile
import time
from collections.abc import Iterable, Mapping
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
    """Return the operator override layer and the file's write time.

    File first, then ``APIS_HARNESS_HEADROOM``: the first that parses wins, as
    before.  The write time is ``None`` for the env source, which no live
    observation supersedes.
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
    for raw, written_at in ((file_raw, file_mtime), (env_raw, None)):
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed, written_at
    return {}, None


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
    used: list[float] = []
    all_reset = bool(windows)
    for window in windows:
        if not isinstance(window, dict):
            continue
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
    try:
        return float(value) / 100.0  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _live_observed_at(provider: str) -> float | None:
    entry = _read_json_object(_usage_path()).get(provider)
    if not isinstance(entry, dict):
        return None
    return _wall_from_iso(entry.get("observed_at"))


def configured_headroom(*, now_wall: float | None = None) -> dict[str, float]:
    """Return normalized per-provider bundled-plan headroom estimates.

    Precedence per provider:

    1. An override object with ``cooldown_reason: "manual"``, or with a
       ``cooldown_until`` still in the future.
    2. The live usage snapshot (``live_headroom``), when it has an opinion and
       any undated override was written before that observation.
    3. An undated override (bare number, or object without
       ``cooldown_until``) from the file or ``APIS_HARNESS_HEADROOM``.
    4. ``1.0``.

    An override whose ``cooldown_until`` has passed is ignored, so a zero set
    at exhaustion stops blocking at the provider's reset.
    """
    moment = time.time() if now_wall is None else now_wall
    overrides, written_at = _override_values()

    result: dict[str, float] = {}
    for provider in PROVIDERS:
        raw = overrides.get(provider)
        dated: float | None = None
        undated: float | None = None
        if isinstance(raw, dict):
            value = _clamp(raw.get("headroom"))
            until = _wall_from_iso(raw.get("cooldown_until"))
            if value is not None:
                if raw.get("cooldown_reason") == "manual":
                    dated = value
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
            result[provider] = dated
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
                result[provider] = live
                continue
        result[provider] = 1.0 if undated is None else undated
    return result


def _write_usage_entry(provider: str, entry: Mapping[str, object]) -> None:
    """Read-merge-write one provider's usage entry atomically.

    Serialized on a sibling lock file so two concurrent recorders cannot each
    read the old snapshot and drop the other's entry; the replace itself is
    atomic, so readers never see a torn file and never take the lock.
    """
    path = _usage_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(f".{path.name}.lock"), "a", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        existing = _read_json_object(path)
        existing[provider] = dict(entry)
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
        used = _percent(window.get("used_percent"))
        if used is None:
            raise ValueError(f"window without numeric used_percent: {window!r}")
        item: dict[str, object] = {
            "name": str(window.get("name", "")),
            "used_percent": float(window["used_percent"]),  # type: ignore[arg-type]
        }
        if window.get("resets_at") is not None:
            if _wall_from_iso(window.get("resets_at")) is None:
                raise ValueError(f"unparseable resets_at: {window!r}")
            item["resets_at"] = window["resets_at"]
        rendered.append(item)
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
        return f"headroom={headroom[provider]:.3f} is at or below minimum={minimum:.3f}"
    if _cooldown_until.get(provider, 0.0) > moment:
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


def cool_down(provider: str, *, now: float | None = None) -> None:
    """Temporarily remove a provider after a cap/auth/launch failure."""
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


def cooling_providers(*, now: float | None = None) -> set[str]:
    """Expose active cooldowns for diagnostics without leaking timestamps."""
    moment = time.monotonic() if now is None else now
    return {provider for provider, until in _cooldown_until.items() if until > moment}
