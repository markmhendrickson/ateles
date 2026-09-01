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

``APIS_HARNESS_MODELS_<PROVIDER>``
    Comma-separated model preference order for one provider, strongest first.
    Empty string (the default for claude/codex) means "use the provider's
    ambient default and never pass an explicit model flag".

No metered/API-key fallback is represented here.  That hard boundary is
enforced by ``skill_runner`` when it constructs each child environment.

Why the fallback unit is a (provider, model) PAIR, not a provider
----------------------------------------------------------------
A subscription quota is scoped per-model-bucket, not per-provider.  On
2026-09-01 Cursor's Ultra plan reported the third-party bucket (Opus, Sonnet,
GPT, Gemini) at 100% while the Cursor-native bucket (Grok, Composer) sat at
24% — verified empirically: ``composer-2.5`` and ``cursor-grok-4.6-low``
answered normally in the same minute that ``claude-opus-5-thinking-high``
returned ``You've hit your usage limit for Opus``.  A router that treats "Opus
is out" as "Cursor is out" therefore discards ~76% of a paid plan's remaining
capacity and, with every other provider also degraded, fails the dispatch
outright.  That is exactly how 18 dispatches died while working capacity was
available.

Ordering policy is deliberately PROVIDER-LOYAL: every eligible model on a
provider is exhausted (strongest first) before crossing to the next provider,
accepting a weaker model as the cost of staying on the preferred plan.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from pathlib import Path

PROVIDERS = ("claude", "codex", "cursor")
DEFAULT_PROVIDER_ORDER = PROVIDERS

# ── Capability tiers ──────────────────────────────────────────────────────────
# A coarse, deliberately small ordinal scale. It exists so a STAGE can declare a
# floor ("review needs at least `strong`") without naming individual models,
# which churn constantly. Ranking is by what the model is marketed and priced as
# on its own plan, and is intentionally conservative: an unknown model is NOT
# assumed capable.
TIER_BASIC = 1  # fast/cheap models: routing, state checks
TIER_MID = 2  # mid-tier general coding models
TIER_STRONG = 3  # frontier reasoning models: judgment work

TIER_NAMES = {"basic": TIER_BASIC, "mid": TIER_MID, "strong": TIER_STRONG}

# Model preference order per provider, STRONGEST FIRST, with each model's tier.
# An empty tuple means "this provider takes its ambient default model and is
# never passed an explicit model flag" — the pre-existing behaviour, preserved
# for claude and codex, whose CLIs pick a sensible default from the logged-in
# plan.
#
# Cursor is enumerated because its plan splits quota into two buckets that fail
# independently (see module docstring), so naming models is the only way to
# reach the bucket that still has headroom. Identifiers verified present in
# `cursor-agent --list-models` on 2026-09-01.
DEFAULT_PROVIDER_MODELS: dict[str, tuple[tuple[str, int], ...]] = {
    "claude": (),
    "codex": (),
    "cursor": (
        ("claude-opus-5-thinking-high", TIER_STRONG),
        ("claude-sonnet-5-thinking-high", TIER_STRONG),
        ("gpt-5.3-codex-high", TIER_STRONG),
        ("cursor-grok-4.6-high", TIER_MID),
        ("composer-2.5", TIER_BASIC),
    ),
}

# Static capability floor per pipeline stage. Stage names are the ones the
# dispatcher actually uses — the review lenses of `_KNOWN_LENS_NAMES` plus the
# non-lens dispatch points — not an invented taxonomy.
#
# The split is by how much JUDGMENT the stage exercises:
#   * `strong` — the stage decides whether code is correct or safe. A wrong
#     verdict here is merged into main, and `security`/`arch` decisions are the
#     hardest to reverse afterwards.
#   * `mid`    — the stage produces real work or a substantive opinion, but a
#     mistake surfaces at the next gate rather than shipping.
#   * `basic`  — the stage is close to mechanical: match against a roster, read
#     state, apply a label.
DEFAULT_STAGE_FLOORS: dict[str, int] = {
    # Judgment stages — these protect main.
    "security": TIER_STRONG,
    "arch": TIER_STRONG,
    "qa": TIER_STRONG,
    "legal": TIER_STRONG,
    "review": TIER_STRONG,
    # Substantive but recoverable.
    "pm": TIER_MID,
    "ux": TIER_MID,
    "content": TIER_MID,
    "implementation": TIER_MID,
    "aggregation": TIER_MID,
    # Mechanical: pattern-match or state check.
    "routing": TIER_BASIC,
    "triage": TIER_BASIC,
    "merge_gating": TIER_BASIC,
}

# Tier assumed for a provider that exposes no explicit model list. Both claude
# and codex authenticate against plans whose default model is a frontier
# reasoning model, so `strong` is the honest reading. If that stops being true
# the override below re-states it without a code change.
DEFAULT_AMBIENT_TIER = TIER_STRONG

_current_weights: dict[str, float] = {}
# Keyed by (provider, model) — model "" means the provider's ambient default.
# Per-pair so exhausting Opus on Cursor does not sideline Composer on Cursor.
_cooldown_until: dict[tuple[str, str], float] = {}


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


def stage_floor(stage: str) -> int:
    """Return the STATIC capability floor for a pipeline stage.

    ``APIS_STAGE_MIN_TIER`` is a JSON object of ``{"<stage>": "basic|mid|strong"}``
    read on every call, so the operator can retune a floor without a redeploy.
    Unknown stages default to ``mid``.

    Static by design. A floor the actor can lower in the moment is not a floor:
    under quota pressure the cheapest path is always to relax it, which would
    disable the constraint precisely when capacity is scarce and it matters
    most. Per-task movement is allowed only UPWARD, via ``effective_floor``.
    """
    raw = os.environ.get("APIS_STAGE_MIN_TIER", "").strip()
    floors: Mapping[str, object] = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                floors = parsed
        except (TypeError, ValueError):
            floors = {}

    configured = floors.get(stage, floors.get(stage.lower()))
    if isinstance(configured, str) and configured.strip().lower() in TIER_NAMES:
        return TIER_NAMES[configured.strip().lower()]
    return DEFAULT_STAGE_FLOORS.get(stage.lower(), TIER_MID)


def effective_floor(stage: str, requested: object = None) -> int:
    """Combine the static stage floor with a completing agent's request.

    RATCHET UPWARD ONLY: returns ``max(stage_floor, requested)``. The agent that
    finishes a stage often knows something config cannot — that a diff touched
    the payment path, say, and so deserves the strongest reviewer. That signal is
    worth propagating. But the same agent has an interest in NOT demanding an
    expensive reviewer for its own work, so a request that would LOWER the bar is
    ignored rather than honoured.

    Enforcement lives here, in the dispatcher's decision path, precisely because
    the request arrives as an agent-written field: anything an agent writes must
    be treated as a proposal, never as the decision.
    """
    floor = stage_floor(stage)
    if isinstance(requested, str):
        requested = TIER_NAMES.get(requested.strip().lower())
    if isinstance(requested, bool) or not isinstance(requested, int):
        return floor
    return max(floor, requested)


def configured_models(provider: str) -> tuple[tuple[str, int], ...]:
    """Return one provider's (model, tier) preference order, strongest first.

    ``APIS_HARNESS_MODELS_<PROVIDER>`` overrides the built-in list. Entries are
    ``name`` or ``name:tier`` where tier is ``basic``/``mid``/``strong``; a bare
    name is assumed ``mid`` — deliberately not ``strong``, so an operator adding
    an unrecognized model cannot accidentally satisfy a review-grade floor.
    """
    raw = os.environ.get(f"APIS_HARNESS_MODELS_{provider.upper()}")
    if raw is None:
        return DEFAULT_PROVIDER_MODELS.get(provider, ())
    if not raw.strip():
        return ()

    models: list[tuple[str, int]] = []
    seen: set[str] = set()
    for item in raw.split(","):
        name, _, tier_raw = item.strip().partition(":")
        name = name.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        models.append((name, TIER_NAMES.get(tier_raw.strip().lower(), TIER_MID)))
    return tuple(models)


def cool_down(
    provider: str, *, model: str = "", now: float | None = None
) -> None:
    """Temporarily remove ONE (provider, model) pair after a cap/auth failure.

    Scoped to the pair, not the provider: a quota is per model bucket, so
    exhausting Opus must not sideline the Cursor-native models that still have
    headroom on the very same plan.
    """
    try:
        duration = max(
            0.0, float(os.environ.get("APIS_HARNESS_COOLDOWN_SECONDS", "3600"))
        )
    except ValueError:
        duration = 3600.0
    moment = time.monotonic() if now is None else now
    _cooldown_until[(provider, model)] = moment + duration


def candidate_pairs(
    available: Mapping[str, str | None],
    *,
    preferred: str | None = None,
    min_tier: int = TIER_BASIC,
    now: float | None = None,
) -> list[tuple[str, str]]:
    """Return ``(provider, model)`` attempt order; ``model`` "" = ambient default.

    Ordering is PROVIDER-LOYAL by design: the provider chosen first is decided by
    the same smooth-weighted-round-robin as before, then ALL of that provider's
    eligible models are attempted strongest-first before any other provider is
    considered. Staying on the preferred plan — even on a weaker model — is the
    operator's stated preference, because a weaker model that runs beats a
    stronger one that is out of quota.

    ``min_tier`` is the calling STAGE's capability floor. Models below it are not
    merely deprioritized, they are EXCLUDED: a review that silently ran on a
    basic model and approved a PR is worse than a review that did not run, since
    the auto-merge path cannot tell the difference. An empty return therefore
    means "this stage cannot be served right now" and the caller must escalate
    rather than downgrade.
    """
    moment = time.monotonic() if now is None else now
    providers = _eligible_providers(available, preferred=preferred, now=moment)

    pairs: list[tuple[str, str]] = []
    for provider in providers:
        models = configured_models(provider)
        if not models:
            # Ambient-default provider: one candidate, tier assumed by policy.
            if DEFAULT_AMBIENT_TIER >= min_tier and _cooldown_until.get(
                (provider, ""), 0.0
            ) <= moment:
                pairs.append((provider, ""))
            continue
        for name, tier in models:
            if tier < min_tier:
                continue
            if _cooldown_until.get((provider, name), 0.0) > moment:
                continue
            pairs.append((provider, name))
    return pairs


def provider_candidates(
    available: Mapping[str, str | None],
    *,
    preferred: str | None = None,
    now: float | None = None,
) -> list[str]:
    """Return providers in attempt order (no model dimension).

    Retained as the provider-level view of the same selection ``candidate_pairs``
    builds on: balancing, headroom, and eligibility are still meaningful per
    provider. Callers that dispatch should prefer ``candidate_pairs``, which also
    chooses the model and honours a stage's capability floor.
    """
    return _eligible_providers(available, preferred=preferred, now=now)


def _eligible_providers(
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

    # A provider is eligible while ANY of its (provider, model) pairs is still
    # outside cooldown. Checking a bare provider key here would resurrect the
    # bug this change exists to fix: one exhausted model retiring a whole plan.
    eligible = [
        provider
        for provider in order
        if available.get(provider)
        and headroom[provider] > minimum
        and _provider_has_live_pair(provider, moment)
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


def _provider_has_live_pair(provider: str, moment: float) -> bool:
    """True while at least one of the provider's models is outside cooldown."""
    models = configured_models(provider)
    if not models:
        return _cooldown_until.get((provider, ""), 0.0) <= moment
    return any(
        _cooldown_until.get((provider, name), 0.0) <= moment for name, _ in models
    )


def reset_state() -> None:
    """Clear process-local balancing/cooldown state (tests and operator reloads)."""
    _current_weights.clear()
    _cooldown_until.clear()


def cooling_pairs(*, now: float | None = None) -> set[tuple[str, str]]:
    """Expose active per-pair cooldowns for diagnostics, without timestamps."""
    moment = time.monotonic() if now is None else now
    return {pair for pair, until in _cooldown_until.items() if until > moment}


def cooling_providers(*, now: float | None = None) -> set[str]:
    """Providers with NO live model left, for the existing diagnostics string.

    Deliberately not "any pair cooling": a provider with one exhausted model and
    four healthy ones is not cooling, and reporting it as such is what made the
    outage look like a total provider failure in the first place.
    """
    moment = time.monotonic() if now is None else now
    seen = {provider for provider, _ in _cooldown_until}
    return {
        provider
        for provider in seen
        if not _provider_has_live_pair(provider, moment)
    }
