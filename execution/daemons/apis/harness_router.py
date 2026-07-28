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

``APIS_HARNESS_MIN_HEADROOM``
    Providers at or below this value are held out.  Default: 0.05.

``APIS_HARNESS_COOLDOWN_SECONDS``
    How long a provider is held out after a quota/auth failure.  Default: 3600.

No metered/API-key fallback is represented here.  That hard boundary is
enforced by ``skill_runner`` when it constructs each child environment.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from pathlib import Path

PROVIDERS = ("claude", "codex", "cursor")
DEFAULT_PROVIDER_ORDER = PROVIDERS

_current_weights: dict[str, float] = {}
_cooldown_until: dict[str, float] = {}


def configured_providers() -> list[str]:
    """Return the de-duplicated, recognized provider order."""
    raw = os.environ.get("APIS_HARNESS_PROVIDERS", ",".join(DEFAULT_PROVIDER_ORDER))
    ordered: list[str] = []
    for item in raw.split(","):
        provider = item.strip().lower()
        if provider in PROVIDERS and provider not in ordered:
            ordered.append(provider)
    return ordered


def configured_headroom() -> dict[str, float]:
    """Return normalized per-provider bundled-plan headroom estimates."""
    env_raw = os.environ.get("APIS_HARNESS_HEADROOM", "").strip()
    configured_path = os.environ.get("APIS_HARNESS_HEADROOM_FILE", "").strip()
    headroom_path = (
        Path(configured_path).expanduser()
        if configured_path
        else Path.home() / ".config" / "ateles" / "harness-headroom.json"
    )
    file_raw = ""
    if headroom_path.is_file():
        try:
            file_raw = headroom_path.read_text(encoding="utf-8").strip()
        except OSError:
            pass

    values: Mapping[str, object] = {}
    for raw in (file_raw, env_raw):
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                values = parsed
                break
        except (TypeError, ValueError):
            continue

    result: dict[str, float] = {}
    for provider in PROVIDERS:
        value = values.get(provider, 1.0)
        try:
            result[provider] = min(1.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            result[provider] = 1.0
    return result


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
    try:
        minimum = min(
            1.0,
            max(0.0, float(os.environ.get("APIS_HARNESS_MIN_HEADROOM", "0.05"))),
        )
    except ValueError:
        minimum = 0.05

    eligible = [
        provider
        for provider in order
        if available.get(provider)
        and headroom[provider] > minimum
        and _cooldown_until.get(provider, 0.0) <= moment
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


def cooling_providers(*, now: float | None = None) -> set[str]:
    """Expose active cooldowns for diagnostics without leaking timestamps."""
    moment = time.monotonic() if now is None else now
    return {
        provider
        for provider, until in _cooldown_until.items()
        if until > moment
    }
