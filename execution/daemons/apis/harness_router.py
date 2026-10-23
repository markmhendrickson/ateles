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
         "windows": [{"name": "weekly", "used_percent": 3, "resets_at": ISO}],
         "probe": {"status": "available|exhausted|unknown",
                   "observed_at": ISO, "source": str, "detail": str|null}}

    Live headroom is ``1 - max(used_percent)/100`` over windows that have not
    yet reset (a window past its ``resets_at`` counts as unused), and ``0.0``
    while ``exhausted_until`` is in the future.  Providers whose CLIs expose no
    numeric quota report use a bounded native probe: a current success supplies
    positive availability (headroom ``1.0``), an explicit limit refusal supplies
    exhaustion, and every other result is ``unknown`` rather than exhausted.
    An observation older than ``APIS_HARNESS_USAGE_MAX_AGE_SECONDS`` (default
    21600) whose windows have not reset carries no opinion.

    A provider that refused with a session/usage-limit message is also held out
    by a per-provider ``cooling`` object in the same file, written by
    ``record_cooling`` (``{"until": ISO, "reason": str, "observed_at": ISO}``).
    Unlike the process-local ``cool_down`` timer, it survives across the
    short-lived ``dispatch_role`` processes, and ends at the reset the refusal
    stated rather than after a flat hour.  It expires on its own; a fresh
    successful usage observation or provider probe clears it as stale evidence.

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
import plistlib
import sys
import tempfile
import time
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import model_tiering

FRONTIER_PROVIDERS = ("claude", "codex", "cursor")
LOCAL_PROVIDERS = ("claude-local",)
PROVIDERS = FRONTIER_PROVIDERS + LOCAL_PROVIDERS
DEFAULT_PROVIDER_ORDER = FRONTIER_PROVIDERS

_current_weights: dict[str, float] = {}
_cooldown_until: dict[str, float] = {}


@dataclass(frozen=True)
class ProviderPolicy:
    """The active provider-admission policy and where it was read from."""

    active_providers: tuple[str, ...]
    usage_gated_providers: tuple[str, ...]
    source: str
    error: str = ""
    daemon_environment: Mapping[str, object] | None = None


@dataclass(frozen=True)
class ProviderAdmission:
    """One provider's complete admission and model-resolution decision."""

    policy_source: str
    active_providers: tuple[str, ...]
    usage_gated_providers: tuple[str, ...]
    requested_provider: str
    admitted: bool
    reason_code: str
    reason: str
    action_class: str
    resolved_tier: str = ""
    tier_source: str = ""
    resolved_model: str = ""
    escalation_reasons: tuple[str, ...] = ()
    explicit_model_override: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "policy_source": self.policy_source,
            "active_providers": list(self.active_providers),
            "usage_gated_providers": list(self.usage_gated_providers),
            "requested_provider": self.requested_provider,
            "admitted": self.admitted,
            "reason_code": self.reason_code,
            "reason": self.reason,
            "action_class": self.action_class,
            "resolved_tier": self.resolved_tier,
            "tier_source": self.tier_source,
            "resolved_model": self.resolved_model,
            "escalation_reasons": list(self.escalation_reasons),
            "explicit_model_override": self.explicit_model_override,
        }


_PROVIDER_POLICY_PLIST_ENV = "APIS_PROVIDER_POLICY_PLIST"
_DEFAULT_PROVIDER_POLICY_PLIST = (
    Path.home() / "Library" / "LaunchAgents" / "com.ateles.apis.plist"
)
_daemon_runtime_context = False


def establish_daemon_runtime_context() -> None:
    """Mark this process as Apis itself; children never inherit this marker."""
    global _daemon_runtime_context
    _daemon_runtime_context = True


def _provider_csv(
    raw: object, *, field: str, allow_empty: bool = False
) -> tuple[tuple[str, ...], str]:
    if not isinstance(raw, str):
        return (), f"{field} is not a string"
    if allow_empty and not raw.strip():
        return (), ""
    parts = [item.strip().lower() for item in raw.split(",")]
    if any(not item for item in parts):
        return (), f"{field} contains an empty provider name"
    unknown = sorted({item for item in parts if item not in FRONTIER_PROVIDERS})
    if unknown:
        return (), f"{field} names unsupported providers: {', '.join(unknown)}"
    if len(set(parts)) != len(parts):
        return (), f"{field} contains duplicate provider names"
    return tuple(parts), ""


def _provider_csv_legacy(raw: object) -> tuple[str, ...]:
    """Historical generic-router parsing: ignore unknowns and duplicates."""
    if not isinstance(raw, str):
        return ()
    return tuple(
        dict.fromkeys(
            item.strip().lower()
            for item in raw.split(",")
            if item.strip().lower() in FRONTIER_PROVIDERS
        )
    )


def _policy_from_values(
    active_raw: object,
    gated_raw: object,
    source: str,
    *,
    strict: bool = True,
) -> ProviderPolicy:
    if not strict:
        return ProviderPolicy(
            _provider_csv_legacy(active_raw),
            _provider_csv_legacy(gated_raw),
            source,
        )
    active, active_error = _provider_csv(active_raw, field="APIS_HARNESS_PROVIDERS")
    gated, gated_error = _provider_csv(
        gated_raw, field="APIS_USAGE_GATED_PROVIDERS", allow_empty=True
    )
    error = active_error or gated_error
    if not error and not active:
        error = "APIS_HARNESS_PROVIDERS resolves to no active provider"
    return ProviderPolicy(active, gated, source, error)


def _provider_policy_plist_path() -> Path | None:
    explicit = os.environ.get(_PROVIDER_POLICY_PLIST_ENV)
    if explicit is not None:
        return Path(explicit).expanduser() if explicit.strip() else None
    # Unit tests must never inherit the developer host's live launchd policy.
    # Tests that exercise this source opt in through APIS_PROVIDER_POLICY_PLIST.
    if "pytest" in sys.modules or os.environ.get("PYTEST_CURRENT_TEST") is not None:
        return None
    return _DEFAULT_PROVIDER_POLICY_PLIST


def provider_policy(*, required: bool = False) -> ProviderPolicy:
    """Resolve provider activation and usage gating from one runtime policy.

    A daemon process receives both values in its environment. A standalone
    process does not inherit launchd's environment, so it reads the exact same
    two values from the active daemon plist. The optional plist override makes
    that source explicit on non-default installations and in tests.

    ``required=True`` is the standalone/guarded posture: an absent, partial,
    or malformed policy is returned as an error and callers refuse dispatch.
    Legacy generic routing keeps its historical defaults only when no source
    exists at all; an explicit malformed source never becomes a permissive
    default.
    """
    active_present = "APIS_HARNESS_PROVIDERS" in os.environ
    gated_present = "APIS_USAGE_GATED_PROVIDERS" in os.environ
    explicit_path = os.environ.get(_PROVIDER_POLICY_PLIST_ENV)
    if required and explicit_path is not None and (
        not explicit_path.strip() or explicit_path != explicit_path.strip()
    ):
        return ProviderPolicy(
            (), (), _PROVIDER_POLICY_PLIST_ENV,
            "declared daemon policy location is blank or has surrounding whitespace",
        )
    path = _provider_policy_plist_path()
    if required and (explicit_path is not None or not _daemon_runtime_context) and (
        path is None or not path.is_file()
    ):
        return ProviderPolicy(
            (), (), f"launchd plist {path}" if path else "runtime provider policy",
            "declared daemon policy file is unavailable",
        )
    # The installed daemon policy is authoritative when it is available.
    # A standalone shell may inherit a complete but older policy pair.
    plist_policy: ProviderPolicy | None = None
    if (path is not None and path.is_file()
            and (required or not (active_present or gated_present))):
        source = f"launchd plist {path}"
        try:
            with path.open("rb") as handle:
                parsed = plistlib.load(handle)
            if not isinstance(parsed, dict):
                plist_policy = ProviderPolicy(
                    (), (), source, "launchd plist root is not a map"
                )
            else:
                env = parsed.get("EnvironmentVariables")
                if not isinstance(env, dict):
                    plist_policy = ProviderPolicy(
                        (), (), source, "launchd plist has no EnvironmentVariables map"
                    )
                else:
                    missing = [
                        key for key in (
                            "APIS_HARNESS_PROVIDERS", "APIS_USAGE_GATED_PROVIDERS"
                        ) if key not in env
                    ]
                    plist_policy = (
                        ProviderPolicy(
                            (), (), source,
                            "runtime provider policy is partial: missing "
                            + ", ".join(missing),
                        ) if missing else _policy_from_values(
                            env["APIS_HARNESS_PROVIDERS"],
                            env["APIS_USAGE_GATED_PROVIDERS"],
                            source, strict=required,
                        )
                    )
                    if not missing and plist_policy is not None:
                        plist_policy = replace(
                            plist_policy,
                            daemon_environment={
                                key: env[key]
                                for key in (
                                    "APIS_ACTION_POLICY_FILE",
                                    "APIS_VENDOR_BINDING_FILE",
                                )
                                if key in env
                            },
                        )
        except (OSError, plistlib.InvalidFileException, TypeError, ValueError) as exc:
            plist_policy = ProviderPolicy(
                (), (), source,
                f"runtime provider policy is unreadable: {type(exc).__name__}",
            )
        if plist_policy.error:
            return plist_policy
    if active_present or gated_present:
        source = "process environment"
        if not (active_present and gated_present):
            missing = (
                "APIS_USAGE_GATED_PROVIDERS"
                if active_present
                else "APIS_HARNESS_PROVIDERS"
            )
            if required:
                return ProviderPolicy(
                    (),
                    (),
                    source,
                    f"runtime provider policy is partial: missing {missing}",
                )
            # Compatibility for generic router callers that historically set
            # only APIS_HARNESS_PROVIDERS. Guarded entrypoints use
            # required=True and reject this partial shape.
            source = "legacy partial process environment"
            active_raw = os.environ.get(
                "APIS_HARNESS_PROVIDERS", ",".join(DEFAULT_PROVIDER_ORDER)
            )
            return _policy_from_values(
                active_raw,
                os.environ.get("APIS_USAGE_GATED_PROVIDERS", active_raw),
                source,
                strict=False,
            )
        process_policy = _policy_from_values(
            os.environ["APIS_HARNESS_PROVIDERS"],
            os.environ["APIS_USAGE_GATED_PROVIDERS"],
            source,
            strict=required,
        )
        if (required and plist_policy is not None and not process_policy.error
                and (process_policy.active_providers, process_policy.usage_gated_providers)
                != (plist_policy.active_providers, plist_policy.usage_gated_providers)):
            return ProviderPolicy(
                (), (), plist_policy.source,
                "process environment provider policy conflicts with the installed "
                f"daemon policy in {plist_policy.source}",
            )
        if required and plist_policy is not None:
            return replace(
                process_policy, daemon_environment=plist_policy.daemon_environment
            )
        if required and _daemon_runtime_context:
            return process_policy
        return process_policy

    if plist_policy is not None:
        return plist_policy

    source = (
        f"launchd plist {path}"
        if path is not None
        else "runtime provider policy (no source configured)"
    )
    if required:
        return ProviderPolicy((), (), source, "runtime provider policy is missing")
    return ProviderPolicy(
        DEFAULT_PROVIDER_ORDER,
        DEFAULT_PROVIDER_ORDER,
        "legacy defaults (no runtime provider policy source)",
    )


def configured_providers() -> list[str]:
    """Return the de-duplicated, recognized frontier provider order."""
    policy = provider_policy()
    return [] if policy.error else list(policy.active_providers)


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
    probe = entry.get("probe")
    probe_status = probe.get("status") if isinstance(probe, dict) else None
    probe_observed = (
        _wall_from_iso(probe.get("observed_at")) if isinstance(probe, dict) else None
    )
    try:
        max_age = float(os.environ.get("APIS_HARNESS_USAGE_MAX_AGE_SECONDS", "21600"))
    except ValueError:
        max_age = 21600.0
    probe_fresh = (
        probe_observed is not None and 0 <= moment - probe_observed <= max_age
    )
    if (
        not windows
        and probe_status == "available"
        and probe_fresh
        and provider not in WEEKLY_PACED_PROVIDERS
    ):
        return 1.0
    if exhausted_until is not None and not windows:
        # The reported reset has passed and nothing newer was observed: the
        # plan has reset, so the provider is back to full headroom.
        return 1.0
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


def _probe_evidence(
    provider: str, *, now_wall: float | None = None
) -> dict[str, object] | None:
    """Return a current provider-native probe observation, or ``None``."""
    entry = _read_json_object(_usage_path()).get(provider)
    probe = entry.get("probe") if isinstance(entry, dict) else None
    if not isinstance(probe, dict) or probe.get("status") not in {
        "available", "exhausted", "unknown",
    }:
        return None
    observed = _wall_from_iso(probe.get("observed_at"))
    if observed is None:
        return None
    moment = time.time() if now_wall is None else now_wall
    if observed > moment + 300.0 or moment - observed > usage_stale_seconds():
        return None
    return dict(probe)


# Where a provider's resolved headroom came from.  These name the four
# precedence tiers ``configured_headroom`` documents, so a caller that refuses
# on a ``0.0`` can point the operator at the source that actually won instead
# of guessing (the override file is not always the winner).
HEADROOM_SOURCE_MANUAL_OVERRIDE = "manual_override"
HEADROOM_SOURCE_DATED_OVERRIDE = "dated_cooldown_override"
HEADROOM_SOURCE_LIVE_USAGE = "live_usage_snapshot"
HEADROOM_SOURCE_LIVE_PROBE = "live_provider_probe"
HEADROOM_SOURCE_UNDATED_OVERRIDE = "undated_override"
HEADROOM_SOURCE_DEFAULT = "default"


def headroom_resolution(
    *, now_wall: float | None = None, without_mac_readings: Collection[str] = ()
) -> dict[str, tuple[float, str]]:
    """Return ``{provider: (headroom, source)}`` for every provider.

    This is the single implementation of the precedence; ``configured_headroom``
    is its value-only projection.  ``source`` is one of the
    ``HEADROOM_SOURCE_*`` constants and names the tier that produced the value.

    ``without_mac_readings`` (operator ruling 2026-10-15): for these providers
    the Mac's own usage readings (the usage snapshot and the provider probe)
    are left OUT of the evidence before anything is resolved, so they can
    neither stand as the value nor outvote or expire an operator override.
    The operator's overrides then resolve exactly as they do for any
    dispatch.
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

        mac_readings = provider not in without_mac_readings
        probe = _probe_evidence(provider, now_wall=moment) if mac_readings else None
        probe_observed = (
            _wall_from_iso(probe.get("observed_at")) if probe is not None else None
        )
        dated_is_stale = (
            dated_source != HEADROOM_SOURCE_MANUAL_OVERRIDE
            and probe is not None
            and probe.get("status") == "available"
            and written_at is not None
            and probe_observed is not None
            and probe_observed >= written_at
        )
        if dated is not None and not dated_is_stale:
            result[provider] = (dated, dated_source)
            continue
        live = live_headroom(provider, now_wall=moment) if mac_readings else None
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
                source = (
                    HEADROOM_SOURCE_LIVE_PROBE
                    if probe is not None
                    and probe.get("status") == "available"
                    and not usage_windows(provider)
                    else HEADROOM_SOURCE_LIVE_USAGE
                )
                result[provider] = (live, source)
                continue
        if undated is None:
            result[provider] = (1.0, HEADROOM_SOURCE_DEFAULT)
        else:
            result[provider] = (undated, HEADROOM_SOURCE_UNDATED_OVERRIDE)
    return result


# Headroom sources that are a reading of the MAC's own provider logins. A
# dispatch that runs on Fly uses separately held logins (operator ruling
# 2026-10-15), so these say nothing about its capacity.
MAC_READING_SOURCES = frozenset({HEADROOM_SOURCE_LIVE_USAGE, HEADROOM_SOURCE_LIVE_PROBE})


def _headroom_for(fly_bound: Collection[str] = (), *,
                  now_wall: float | None = None) -> dict[str, float]:  # fmt: skip
    """``configured_headroom``, except that for a provider in *fly_bound* the
    Mac's own usage readings are removed from the evidence BEFORE resolution
    (they describe the Mac's login, not the Fly runner's). Every operator
    override, manual, dated or undated, zero or not, then wins exactly as it
    does for a local dispatch; with none, the default applies."""
    return {
        provider: value
        for provider, (value, _source) in headroom_resolution(
            now_wall=now_wall, without_mac_readings=fly_bound).items()
    }


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
    clear_keys: Iterable[str] = (),
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
        for key in clear_keys:
            merged.pop(key, None)
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
    provider_probe: bool = False,
    probe_source: str = "provider_usage_report",
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
    at = time.time() if observed_at is None else observed_at
    entry: dict[str, object] = {
        "observed_at": _iso_from_wall(at),
        "windows": rendered,
        "exhausted_until": None,
    }
    if provider_probe:
        entry["probe"] = {
            "status": "available",
            "observed_at": _iso_from_wall(at),
            "source": probe_source,
            "detail": None,
        }
    _write_usage_entry(
        normalized,
        entry,
        clear_keys=("cooling", "last_probe_failure") if provider_probe else (),
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
    at = time.time() if observed_at is None else observed_at
    _write_usage_entry(
        normalized,
        {
            "observed_at": _iso_from_wall(at),
            "windows": [],
            "exhausted_until": _iso_from_wall(until),
            "probe": {
                "status": "exhausted",
                "observed_at": _iso_from_wall(at),
                "source": "provider_refusal",
                "detail": f"capacity exhausted until {_iso_from_wall(until)}",
            },
        },
    )


def record_probe_available(
    provider: str,
    *,
    source: str,
    detail: str = "provider-native probe succeeded",
    observed_at: float | None = None,
) -> None:
    """Record positive capacity evidence from a subscription-backed CLI."""
    normalized = provider.strip().lower()
    if normalized not in FRONTIER_PROVIDERS:
        raise ValueError(f"unsupported provider: {provider!r}")
    at = time.time() if observed_at is None else observed_at
    _write_usage_entry(
        normalized,
        {
            "observed_at": _iso_from_wall(at),
            "windows": [],
            "exhausted_until": None,
            "probe": {
                "status": "available",
                "observed_at": _iso_from_wall(at),
                "source": source,
                "detail": detail,
            },
        },
        clear_keys=("cooling", "last_probe_failure"),
    )


def record_capacity_observation(
    provider: str,
    *,
    source: str,
    detail: str = "provider-native probe succeeded",
    observed_at: float | None = None,
    reading_failure: str | None = None,
) -> None:
    """Record capacity evidence for a provider that is paced on a weekly budget.

    Budget evidence handling tightened per security review: this keeps the
    recorded usage windows and their observation time as they are, until a new
    weekly reading replaces them. ``reading_failure`` is surfaced by the gate
    and ``show``.
    """
    normalized = provider.strip().lower()
    if normalized not in FRONTIER_PROVIDERS:
        raise ValueError(f"unsupported provider: {provider!r}")
    at = time.time() if observed_at is None else observed_at
    update: dict[str, object] = {
        "probe": {
            "status": "available",
            "observed_at": _iso_from_wall(at),
            "source": source,
            "detail": detail,
        },
    }
    if reading_failure:
        update["last_probe_failure"] = {
            "at": _iso_from_wall(at),
            "detail": " ".join(str(reading_failure).split())[:300],
        }
    _write_usage_entry(normalized, update=update, clear_keys=("cooling",))


def record_probe_unknown(
    provider: str,
    *,
    source: str,
    detail: str,
    observed_at: float | None = None,
) -> None:
    """Record that a live provider probe produced no capacity verdict."""
    normalized = provider.strip().lower()
    if normalized not in FRONTIER_PROVIDERS:
        raise ValueError(f"unsupported provider: {provider!r}")
    at = time.time() if observed_at is None else observed_at
    _write_usage_entry(
        normalized,
        update={
            "probe": {
                "status": "unknown",
                "observed_at": _iso_from_wall(at),
                "source": source,
                "detail": detail,
            },
            "last_probe_failure": {"at": _iso_from_wall(at), "detail": detail},
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


def reported_exhaustion(
    provider: str, *, now_wall: float | None = None
) -> float | None:
    """Return a live provider-reported exhaustion reset, independent of usage pacing."""
    moment = time.time() if now_wall is None else now_wall
    entry = _read_json_object(_usage_path()).get(provider)
    until = _wall_from_iso(entry.get("exhausted_until")) if isinstance(entry, dict) else None
    return until if until is not None and until > moment else None


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
# (``APIS_USAGE_GATED_PROVIDERS``, default all configured frontier providers,
# fed by ``usage_probe``).
# Numeric usage windows are preferred where available; providers without them
# use a bounded native request as explicit available/exhausted/unknown evidence.
# Local providers are never gated: mechanical work keeps running.
# ---------------------------------------------------------------------------

WEEK_SECONDS = 7 * 24 * 3600.0
WEEKLY_WINDOW_NAMES = ("weekly_all", "weekly")

# Providers whose dispatch is paced against a weekly budget. Their gate needs a
# valid, fresh weekly window; a bare capacity observation is not budget evidence.
# (Budget evidence handling tightened per security review.)
WEEKLY_PACED_PROVIDERS = ("claude", "codex")

GATE_OK = "ok"
GATE_MISSING = "missing"
GATE_MALFORMED = "malformed"
GATE_STALE = "stale"
GATE_PACED = "paced"
GATE_UNKNOWN = "unknown"


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
    policy = provider_policy()
    return () if policy.error else policy.usage_gated_providers


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

    ``None`` when the provider is not gated (gate off, or explicitly omitted).
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
        and "exhausted_until" not in entry and "probe" not in entry
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
    raw_probe = entry.get("probe")
    if isinstance(raw_probe, dict) and not windows:
        probe_status = raw_probe.get("status")
        probe_source = str(raw_probe.get("source") or "provider-native probe")
        probe_detail = str(raw_probe.get("detail") or "no detail")
        probe_observed = _wall_from_iso(raw_probe.get("observed_at"))
        if probe_observed is None or probe_observed > moment + 300.0:
            return _refused(
                normalized, GATE_MALFORMED,
                f"capacity evidence malformed for {normalized} from {probe_source} "
                "(no valid observed_at)",
                moment, entry=entry, **common,
            )
        age = max(0.0, moment - probe_observed)
        common.update(observed_at=probe_observed, age_seconds=age)
        if age > max_age:
            return _refused(
                normalized, GATE_STALE,
                f"capacity evidence stale for {normalized} from {probe_source} "
                f"since {render_wall(probe_observed)} ({int(age // 60)} min old, "
                f"bound {int(max_age // 60)} min)",
                moment, entry=entry, **common,
            )
        if probe_status == "available" and normalized in WEEKLY_PACED_PROVIDERS:
            return _refused(
                normalized, GATE_UNKNOWN,
                f"weekly budget reading unavailable for {normalized} (capacity "
                f"observed from {probe_source}, which does not authorize "
                "dispatch under the weekly ceiling); refusing new frontier "
                "dispatch until a weekly reading is recorded",
                moment, entry=entry, **common,
            )
        if probe_status == "available":
            return UsageGate(
                normalized, True, GATE_OK,
                f"{normalized} available from {probe_source}", **common,
            )  # type: ignore[arg-type]
        if probe_status == "unknown":
            return _refused(
                normalized, GATE_UNKNOWN,
                f"capacity unknown for {normalized} from {probe_source}: {probe_detail}",
                moment, entry=entry, **common,
            )
        if probe_status == "exhausted":
            return _refused(
                normalized, GATE_STALE,
                f"exhaustion evidence for {normalized} from {probe_source} no longer "
                "has a future reset; a recovery probe is required",
                moment, entry=entry, **common,
            )
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
    available: Mapping[str, str | None], *, now_wall: float | None = None,
    fly_bound: Collection[str] = (),
) -> dict[str, UsageGate] | None:
    """Each gate refusal when the gate is why no frontier provider can run.

    ``None`` unless at least one configured provider is otherwise eligible
    (binary present, headroom above the floor, not cooled) and EVERY such
    provider is gate-refused.  Mirrors ``cooled_until_all`` so the dispatcher
    reports a stale or paced refusal as what it is, not as generic exhaustion.
    """
    moment = time.time() if now_wall is None else now_wall
    headroom = _headroom_for(fly_bound, now_wall=moment)
    minimum = minimum_headroom()
    refusals: dict[str, UsageGate] = {}
    for provider in configured_providers():
        if not available.get(provider):
            continue
        if persisted_cooling(provider, now_wall=moment) is not None:
            continue
        # A dispatch bound for Fly is not gated by the Mac's usage reading.
        gate = None if provider in fly_bound else usage_gate(provider, now_wall=moment)
        if gate is not None and not gate.allowed:
            refusals[provider] = gate
            continue
        if headroom[provider] <= minimum:
            continue
        if gate is None or gate.allowed:
            return None
    return refusals or None


def _provider_exclusion_reason(
    provider: str,
    available: Mapping[str, str | None],
    *,
    headroom: Mapping[str, tuple[float, str]],
    minimum: float,
    moment: float,
    moment_wall: float | None = None,
    fly_bound: Collection[str] = (),
) -> str | None:
    """Return why one supported provider is ineligible, or ``None``.

    For a provider in *fly_bound* (the dispatch will run on Fly) the Mac's
    usage gate does not apply: not the weekly ceiling or pace line, and not
    a missing, stale or failed reading of the Mac's login. Cooling windows
    (a real rate-limit answer) still apply."""
    if not available.get(provider):
        return "binary unavailable"
    exhausted_until = reported_exhaustion(provider, now_wall=moment_wall)
    if exhausted_until is not None and provider not in fly_bound:
        return (
            f"headroom=0.000 from provider reported limit until "
            f"{render_wall(exhausted_until)}"
        )
    gate = None if provider in fly_bound else usage_gate(provider, now_wall=moment_wall)
    if gate is not None and not gate.allowed:
        return gate.message
    value, source = headroom[provider]
    if value <= minimum:
        # An ungated provider's usage reading is advisory. Operator overrides
        # and a provider-reported cooling window still refuse dispatch.
        if provider in usage_gated_providers() or source not in MAC_READING_SOURCES:
            return f"headroom={value:.3f} is at or below minimum={minimum:.3f}"
    cooling = persisted_cooling(provider, now_wall=moment_wall)
    if cooling is not None:
        return f"{COOLED_UNTIL} {render_wall(float(cooling['until']))} ({cooling['reason']})"
    if _cooldown_until.get(provider, 0.0) > moment:
        return "cooling down"
    return None


def provider_exclusion_reason(
    provider: str,
    available: Mapping[str, str | None],
    *,
    now: float | None = None,
    now_wall: float | None = None,
    fly_bound: Collection[str] = (),
) -> str | None:
    """Explain why a hard-pinned provider cannot run right now."""
    normalized = provider.strip().lower()
    if normalized not in PROVIDERS:
        return "unsupported provider"
    return _provider_exclusion_reason(
        normalized,
        available,
        headroom=headroom_resolution(
            now_wall=now_wall, without_mac_readings=fly_bound
        ),
        minimum=minimum_headroom(),
        moment=time.monotonic() if now is None else now,
        moment_wall=now_wall,
        fly_bound=fly_bound,
    )


def resolve_provider_admission(
    provider: str,
    *,
    action_class: str,
    signals: "model_tiering.EscalationSignals | None" = None,
    required_policy: bool = True,
    now: float | None = None,
    now_wall: float | None = None,
    fly_bound: Collection[str] = (),
    fly_extra: Collection[str] = (),
) -> ProviderAdmission:
    """Resolve strict provider admission and model selection in one result.

    The standalone lens runner passes this result through ``dispatch_role`` to
    ``skill_runner``. Daemon routing reads the same ``provider_policy`` through
    ``configured_providers`` and ``usage_gated_providers``. A configuration
    refusal is therefore distinct from a provider-reported cooldown/exhaustion,
    and a successful result carries the exact tier/model that the child must
    launch with.
    """
    import model_tiering  # Local import avoids widening harness_router's base deps.

    normalized = provider.strip().lower()
    policy = provider_policy(required=required_policy)
    action_source = model_tiering.action_policy_source()
    binding_source = model_tiering.vendor_binding_source()
    policy_source = (
        f"{policy.source}; action_policy={action_source}; "
        f"vendor_binding={binding_source}"
    )

    def decision(
        admitted: bool,
        code: str,
        reason: str,
        *,
        tier: str = "",
        tier_source: str = "",
        model: str = "",
        escalation_reasons: tuple[str, ...] = (),
    ) -> ProviderAdmission:
        return ProviderAdmission(
            policy_source=policy_source,
            active_providers=policy.active_providers,
            usage_gated_providers=policy.usage_gated_providers,
            requested_provider=normalized,
            admitted=admitted,
            reason_code=code,
            reason=reason,
            action_class=action_class,
            resolved_tier=tier,
            tier_source=tier_source,
            resolved_model=model,
            escalation_reasons=escalation_reasons,
        )

    if policy.error:
        return decision(
            False,
            "configuration_error",
            f"{policy.error}; provider={normalized}; policy_source={policy.source}",
        )
    if normalized not in FRONTIER_PROVIDERS:
        return decision(
            False,
            "configuration_error",
            f"unsupported provider {normalized!r}; policy_source={policy.source}",
        )
    if normalized not in policy.active_providers and normalized not in fly_extra:
        return decision(
            False,
            "provider_inactive",
            f"provider {normalized!r} is not active in {policy.source}",
        )

    try:
        if required_policy and policy.daemon_environment is not None:
            action_path, binding_path = model_tiering.daemon_model_policy_paths(
                policy.daemon_environment
            )
            action_source = f"file {action_path}"
            binding_source = f"file {binding_path}"
            policy_source = (
                f"{policy.source}; action_policy={action_source}; "
                f"vendor_binding={binding_source}"
            )
        else:
            action_path = binding_path = None
        action_policy = model_tiering.configured_action_policy(
            strict=True, authoritative_path=action_path
        )
    except model_tiering.ModelPolicyUnavailable as exc:
        return decision(False, "configuration_error", f"action policy: {exc}")
    if action_class not in action_policy:
        return decision(
            False,
            "configuration_error",
            f"required action class {action_class!r} is absent or malformed in "
            f"{action_source}; provider={normalized}",
        )
    resolved_tier = model_tiering.resolve_tier(
        action_class, policy=action_policy, signals=signals
    )
    try:
        binding = model_tiering.configured_vendor_binding(
            strict=True, authoritative_path=binding_path
        )
    except model_tiering.ModelPolicyUnavailable as exc:
        return decision(
            False, "configuration_error", f"required vendor binding: {exc}",
            tier=resolved_tier.tier, tier_source=resolved_tier.source,
            escalation_reasons=resolved_tier.escalation_reasons,
        )
    if not binding:
        return decision(
            False,
            "configuration_error",
            f"required vendor binding is absent or malformed in "
            f"{binding_source}; provider={normalized}; "
            f"tier={resolved_tier.tier}",
            tier=resolved_tier.tier,
            tier_source=resolved_tier.source,
            escalation_reasons=resolved_tier.escalation_reasons,
        )
    try:
        resolved_model = model_tiering.model_for_tier(
            normalized, resolved_tier.tier, binding=binding
        )
    except model_tiering.UnboundTierError as exc:
        return decision(
            False,
            "configuration_error",
            f"{exc}; policy_source={binding_source}; "
            f"provider={normalized}; tier={resolved_tier.tier}",
            tier=resolved_tier.tier,
            tier_source=resolved_tier.source,
            escalation_reasons=resolved_tier.escalation_reasons,
        )
    if not resolved_model:
        return decision(
            False,
            "configuration_error",
            f"vendor binding resolved no model; provider={normalized}; "
            f"tier={resolved_tier.tier}; "
            f"policy_source={binding_source}",
            tier=resolved_tier.tier,
            tier_source=resolved_tier.source,
            escalation_reasons=resolved_tier.escalation_reasons,
        )
    reserved = model_tiering.automatic_model_refusal(resolved_model)
    if reserved:
        return decision(
            False,
            "configuration_error",
            f"{reserved}; provider={normalized}; tier={resolved_tier.tier}; "
            f"policy_source={binding_source}",
            tier=resolved_tier.tier,
            tier_source=resolved_tier.source,
            model=resolved_model,
            escalation_reasons=resolved_tier.escalation_reasons,
        )

    moment_wall = time.time() if now_wall is None else now_wall
    exhausted_until = reported_exhaustion(normalized, now_wall=moment_wall)
    if exhausted_until is not None and normalized not in fly_bound:
        return decision(
            False,
            "provider_limit",
            f"provider {normalized!r} reported a limit until "
            f"{render_wall(exhausted_until)}",
            tier=resolved_tier.tier,
            tier_source=resolved_tier.source,
            model=resolved_model,
            escalation_reasons=resolved_tier.escalation_reasons,
        )
    if normalized in policy.usage_gated_providers and normalized not in fly_bound:
        gate = usage_gate(normalized, now_wall=moment_wall)
        if gate is not None and not gate.allowed:
            return decision(
                False,
                "usage_policy_refusal",
                f"{gate.message}; provider={normalized}; policy_source={policy.source}",
                tier=resolved_tier.tier,
                tier_source=resolved_tier.source,
                model=resolved_model,
                escalation_reasons=resolved_tier.escalation_reasons,
            )

    cooling = persisted_cooling(normalized, now_wall=moment_wall)
    if cooling is not None:
        return decision(
            False,
            "provider_limit",
            f"provider {normalized!r} reported a limit and is cooled until "
            f"{render_wall(float(cooling['until']))} ({cooling['reason']})",
            tier=resolved_tier.tier,
            tier_source=resolved_tier.source,
            model=resolved_model,
            escalation_reasons=resolved_tier.escalation_reasons,
        )
    moment = time.monotonic() if now is None else now
    if _cooldown_until.get(normalized, 0.0) > moment:
        return decision(
            False,
            "provider_limit",
            f"provider {normalized!r} is cooling down after a provider refusal",
            tier=resolved_tier.tier,
            tier_source=resolved_tier.source,
            model=resolved_model,
            escalation_reasons=resolved_tier.escalation_reasons,
        )

    headroom, headroom_source = headroom_resolution(
        now_wall=moment_wall, without_mac_readings=fly_bound
    )[normalized]
    # Usage-derived headroom is advisory for an active provider outside the
    # usage gate. Explicit operator overrides still apply to every provider.
    floor_applies = (
        normalized in policy.usage_gated_providers
        or headroom_source not in MAC_READING_SOURCES
    )
    if floor_applies and headroom <= minimum_headroom():
        code = (
            "provider_limit"
            if headroom_source == HEADROOM_SOURCE_LIVE_USAGE
            else "policy_refusal"
        )
        return decision(
            False,
            code,
            f"provider {normalized!r} has headroom={headroom:.3f} from "
            f"{headroom_source}, at or below minimum={minimum_headroom():.3f}",
            tier=resolved_tier.tier,
            tier_source=resolved_tier.source,
            model=resolved_model,
            escalation_reasons=resolved_tier.escalation_reasons,
        )

    return decision(
        True,
        "admitted",
        f"provider {normalized!r} admitted by {policy.source}",
        tier=resolved_tier.tier,
        tier_source=resolved_tier.source,
        model=resolved_model,
        escalation_reasons=resolved_tier.escalation_reasons,
    )


def usable_provider_names(
    available: Mapping[str, str | None],
    *,
    now: float | None = None,
    now_wall: float | None = None,
    fly_bound: Collection[str] = (),
    fly_extra: Collection[str] = (),
) -> set[str]:
    """Return configured providers passing the router's eligibility predicate
    (plus *fly_extra*, the providers selectable only on Fly)."""
    moment = time.monotonic() if now is None else now
    headroom = headroom_resolution(
        now_wall=now_wall, without_mac_readings=fly_bound
    )
    minimum = minimum_headroom()
    return {
        provider
        for provider in _candidate_order(fly_extra)
        if _provider_exclusion_reason(
            provider,
            available,
            headroom=headroom,
            minimum=minimum,
            moment=moment,
            moment_wall=now_wall,
            fly_bound=fly_bound,
        )
        is None
    }


def _candidate_order(fly_extra: Collection[str] = ()) -> list[str]:
    """The configured providers, then any provider selectable only on Fly
    (never added to the local list itself)."""
    order = configured_providers()
    return [*order, *(p for p in FRONTIER_PROVIDERS if p in fly_extra and p not in order)]


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
    fly_bound: Collection[str] = (),
    fly_extra: Collection[str] = (),
) -> list[str]:
    """Return providers in attempt order, with a smooth weighted first choice.

    Headroom is used as the smooth-weighted-round-robin weight.  Equal
    headroom therefore alternates Claude → Codex → Cursor across dispatches,
    while unequal values naturally send more work to the roomier plan.
    Remaining eligible providers follow in descending headroom order so a
    capacity failure can fail over within the same dispatch.

    ``local_first`` (unpinned runs only) puts an eligible ``claude-local``
    ahead of that frontier list, which stays behind it as the fallback.

    ``fly_bound`` / ``fly_extra`` (operator ruling 2026-10-15): the providers
    whose run will be on Fly are not gated by the Mac's usage reading, and
    ``fly_extra`` names providers selectable only there (a provider with a
    configured Fly login machine but disabled for local dispatch).
    """
    moment = time.monotonic() if now is None else now
    order = _candidate_order(fly_extra)
    if preferred is not None:
        normalized = preferred.strip().lower()
        order = [normalized] if normalized in PROVIDERS else []

    headroom = headroom_resolution(
        now_wall=now_wall, without_mac_readings=fly_bound
    )
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
            fly_bound=fly_bound,
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
    total = sum(headroom[provider][0] for provider in eligible)
    for provider in eligible:
        _current_weights[provider] = (
            _current_weights.get(provider, 0.0) + headroom[provider][0]
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
        key=lambda provider: (-headroom[provider][0], order_index[provider]),
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
