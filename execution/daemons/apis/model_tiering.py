"""execution/daemons/apis/model_tiering.py — tier a dispatch's model by action class.

Operator ruling 2026-09-29 (agent_policy ``ent_0b35f456aa97635a4c870c13``):
every dispatch runs on the model tier its work needs, never the provider's
ambient default. The design lives in
``docs/foundation/gates_and_workflows.md#blast-radius-selects-the-gate-nothing-yet-selects-the-model-a-step-runs-at``:
a ``min_tier`` per action class on the ``action_policy``, with the tier the
runner actually holds resolved from a ``vendor_binding``. The action gate
(ateles#959) that will enforce this at claim/reclaim time is not built yet —
this module is the runner-side read of the SAME data, so #959 later enforces
what this already resolves rather than inventing a second copy.

Two layers, read fresh on every call (operators change either without a
daemon restart, matching ``harness_router.configured_headroom`` and
``local_provider.load_config``):

``action_policy``
    Maps an action CLASS (e.g. ``lens_review:security``, ``build``,
    ``carry_forward``) to a ``min_tier`` — one of ``TIERS`` below. A class
    absent from the map is UNRESOLVED, not "no floor": callers must fail to
    the highest tier rather than guess (see ``resolve_tier``). This mirrors
    ``action_policy.min_tier`` in the foundation design; until an
    ``action_policy`` entity type exists in Neotoma, the values are runtime
    JSON config, read the same file-then-env way as every other Apis policy
    surface.

``vendor_binding``
    Maps a tier to the model id each provider runs it on. A tier absent from a
    provider's map is also unresolved for that provider.

Fail-closed, deliberately asymmetric from ``harness_router``/``local_provider``
(which fail closed to "provider ineligible"): here, an unreadable policy or
binding fails UP to the strongest tier, never to the provider's ambient
default and never to silence. The operator's ruling is that dispatch must
never again run un-tiered by accident (that is the incident this policy
exists to close), so "config is missing" and "config says be careful" resolve
to the same runner behaviour.

Escalation signals (below) can only RAISE the resolved tier, never lower it —
they are cheap, deterministic, measured facts about the dispatch (diff size,
a security-sensitive path, a prior blocking finding, a repeated round, a
failed prior attempt), not a classifier's guess.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("apis.model_tiering")

# Ordered weakest -> strongest. Index comparison is how "raise the tier" and
# "the strongest tier" are computed; never compare tier names as strings.
TIERS: tuple[str, ...] = ("local", "mechanical", "mid", "top")

DEFAULT_TIER = "top"  # the fail-closed destination — see module docstring.

_ACTION_POLICY_ENV = "APIS_ACTION_POLICY"
_ACTION_POLICY_FILE_ENV = "APIS_ACTION_POLICY_FILE"
_DEFAULT_ACTION_POLICY_PATH = Path.home() / ".config" / "ateles" / "action-policy.json"

_VENDOR_BINDING_ENV = "APIS_VENDOR_BINDING"
_VENDOR_BINDING_FILE_ENV = "APIS_VENDOR_BINDING_FILE"
_DEFAULT_VENDOR_BINDING_PATH = Path.home() / ".config" / "ateles" / "vendor-binding.json"

# Path fragments that make a diff security-sensitive for escalation purposes.
# Deliberately narrower than review_panel's security lens `diff_patterns` (that
# set decides who REVIEWS; this one decides what tier the dispatch touching it
# runs at) but drawn from the same surfaces so the two never disagree about
# what counts as sensitive.
SECURITY_SENSITIVE_PATH_FRAGMENTS: tuple[str, ...] = (
    "/auth/",
    "/security/",
    ".env",
    "/hooks/",
    "/guards/",
    "/aauth",
    "grant_checker",
    "neotoma_signed",
    "signed_fetch",
    "/secrets_",
    "/monedula/",
    "/mcp/",
)

# Above this many changed lines, a diff escalates one tier regardless of class.
# Chosen to match the panel's existing "non-trivial PR" signal
# (review_panel.Lens.min_changed_files uses file count; this uses line count,
# the more direct proxy for how much judgement a diff needs).
LARGE_DIFF_LINE_THRESHOLD = 400


def _tier_index(tier: str) -> int:
    try:
        return TIERS.index(tier)
    except ValueError:
        return -1


def _higher_tier(a: str, b: str) -> str:
    """Return whichever of two tier names is stronger; an unknown name loses."""
    return a if _tier_index(a) >= _tier_index(b) else b


def _read_json_config(env_var: str, file_env_var: str, default_path: Path) -> dict:
    """File-then-env JSON read, matching ``harness_router.configured_headroom``
    and ``local_provider``'s config precedence exactly: an operator can change
    either without restarting Apis, and a stale file always wins over env so a
    forgotten export cannot silently override a maintained file.

    Malformed or unreadable input degrades to ``{}`` (unresolved), never
    raises — same posture as every other Apis policy reader. Unlike headroom,
    an empty result here does NOT mean "permissive default": callers resolve
    an unresolved class to ``DEFAULT_TIER``, not to "no floor".
    """
    configured_path = os.environ.get(file_env_var, "").strip()
    path = Path(configured_path).expanduser() if configured_path else default_path
    file_raw = ""
    if path.is_file():
        try:
            file_raw = path.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    env_raw = os.environ.get(env_var, "").strip()
    # Labelled explicitly (not inferred by comparing values) so identical
    # malformed content in the file and the env var still names the file
    # first, correctly, rather than misattributing based on a value match.
    for raw, label in ((file_raw, f"file {path}"), (env_raw, env_var)):
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            log.warning(
                f"[apis] model_tiering: malformed JSON in {label}; ignoring"
            )
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def configured_action_policy() -> dict[str, str]:
    """Return the action-class -> min_tier map, normalized to known tiers.

    An entry naming an unrecognized tier is dropped (logged), never coerced —
    a typo'd tier name must not silently become the weakest tier.
    """
    raw = _read_json_config(
        _ACTION_POLICY_ENV, _ACTION_POLICY_FILE_ENV, _DEFAULT_ACTION_POLICY_PATH
    )
    out: dict[str, str] = {}
    for action_class, tier in raw.items():
        tier_name = str(tier).strip().lower()
        if tier_name in TIERS:
            out[str(action_class)] = tier_name
        else:
            log.warning(
                f"[apis] model_tiering: action_policy class {action_class!r} "
                f"names unrecognized tier {tier!r}; ignoring this entry "
                f"(unresolved classes fail to {DEFAULT_TIER!r})"
            )
    return out


def configured_vendor_binding() -> dict[str, dict[str, str]]:
    """Return {provider: {tier: model_id}}, lower-cased on both axes.

    Shape matches ``local_provider``'s ``vendor_binding``-shaped config
    convention (a slot's fields under a recognizable key) but this module
    reads the raw {provider: {tier: model}} map directly — there is no nested
    ``config`` wrapper here because, unlike ``claude-local``, there is no
    single "enabled" gate: a provider simply has whatever tiers it has.
    """
    raw = _read_json_config(
        _VENDOR_BINDING_ENV, _VENDOR_BINDING_FILE_ENV, _DEFAULT_VENDOR_BINDING_PATH
    )
    out: dict[str, dict[str, str]] = {}
    for provider, tiers in raw.items():
        if not isinstance(tiers, dict):
            continue
        provider_name = str(provider).strip().lower()
        resolved: dict[str, str] = {}
        for tier, model in tiers.items():
            tier_name = str(tier).strip().lower()
            model_name = str(model).strip()
            if tier_name in TIERS and model_name:
                resolved[tier_name] = model_name
        if resolved:
            out[provider_name] = resolved
    return out


@dataclass(frozen=True)
class EscalationSignals:
    """Deterministic, measured facts about ONE dispatch that can raise its tier.

    Every field defaults to the "nothing to escalate on" value, so a caller
    that knows nothing about the dispatch (the common case today) passes no
    signals and gets back exactly the policy-resolved tier — these can only
    raise, never substitute for the base resolution.
    """

    changed_files: tuple[str, ...] = field(default_factory=tuple)
    diff_lines_changed: int = 0
    prior_blocking_finding: bool = False
    review_round: int = 1
    prior_attempt_failed: bool = False

    def touches_security_sensitive_path(self) -> bool:
        return any(
            fragment in changed_file
            for changed_file in self.changed_files
            for fragment in SECURITY_SENSITIVE_PATH_FRAGMENTS
        )

    def reasons(self) -> list[str]:
        """Every reason this dispatch's tier should be raised, for logging."""
        out: list[str] = []
        if self.diff_lines_changed > LARGE_DIFF_LINE_THRESHOLD:
            out.append(f"diff_lines_changed={self.diff_lines_changed}>{LARGE_DIFF_LINE_THRESHOLD}")
        if self.touches_security_sensitive_path():
            out.append("touches_security_sensitive_path")
        if self.prior_blocking_finding:
            out.append("prior_blocking_finding")
        if self.review_round > 1:
            out.append(f"review_round={self.review_round}")
        if self.prior_attempt_failed:
            out.append("prior_attempt_failed")
        return out


def escalate(base_tier: str, signals: "EscalationSignals | None") -> tuple[str, list[str]]:
    """Apply escalation signals to a resolved tier; return (tier, reasons).

    Any single signal raises straight to the top tier — these are correctness
    and cost-of-mistake signals (a big diff, a security path, a prior failure,
    a repeated round), not a graduated score, so there is no partial credit
    between "mechanical" and "mid": the moment one fires, the dispatch gets
    the strongest model available regardless of what its action class alone
    would have gotten.
    """
    if signals is None:
        return base_tier, []
    reasons = signals.reasons()
    if not reasons:
        return base_tier, []
    return _higher_tier(base_tier, DEFAULT_TIER), reasons


@dataclass(frozen=True)
class ResolvedTier:
    """The tier a dispatch should run at, and how that was decided."""

    tier: str
    source: str  # "policy" | "unresolved_class" | "escalated"
    action_class: str
    escalation_reasons: tuple[str, ...] = ()


def resolve_tier(
    action_class: str,
    *,
    signals: "EscalationSignals | None" = None,
    policy: dict[str, str] | None = None,
) -> ResolvedTier:
    """Resolve the tier ``action_class`` must run at, per the module contract.

    ``policy`` lets a caller pass an already-loaded map (e.g. one read once
    per panel run rather than once per lens) instead of re-reading config on
    every call; omitted, this reads ``configured_action_policy()`` fresh.

    An action class with no entry in policy is UNRESOLVED and fails to
    ``DEFAULT_TIER`` (fail-closed to the strongest tier — see module
    docstring), never to "no floor" and never to a provider's ambient
    default. Escalation is applied after policy resolution either way, so an
    unresolved-but-escalated class still reports its escalation reasons.
    """
    active_policy = configured_action_policy() if policy is None else policy
    base_tier = active_policy.get(action_class)
    if base_tier is None:
        base_tier = DEFAULT_TIER
        source = "unresolved_class"
    else:
        source = "policy"
    escalated_tier, reasons = escalate(base_tier, signals)
    if reasons:
        source = "escalated"
    return ResolvedTier(
        tier=escalated_tier,
        source=source,
        action_class=action_class,
        escalation_reasons=tuple(reasons),
    )


# NOT a committed default policy — an action class absent from the LIVE
# config still resolves to DEFAULT_TIER via resolve_tier's normal fail-closed
# path (module docstring), never to this map implicitly. This is a reference
# starting point matching the operator's 2026-09-29 ruling in words
# ("security and architecture review, security fixes and new builds stay on
# the top tier; pm/qa/ux review of small or delta changes, carry-forward
# checks and repairs of already-diagnosed findings run mid-tier; mechanical
# work runs local, with the smallest [frontier] tier as fallback"), kept here
# so a deployment can seed its own action-policy.json from a value this
# module's own tests pin, rather than re-deriving the mapping from prose. It
# is exported specifically so an operator or a future config-generation
# script can start from it; nothing in this module reads it implicitly.
DEFAULT_ACTION_POLICY_HINT: dict[str, str] = {
    "lens_review:security": "top",
    "lens_review:arch": "top",
    "build": "top",
    "security_fix": "top",
    "lens_review:pm": "mid",
    "lens_review:qa": "mid",
    "lens_review:ux": "mid",
    "carry_forward_check": "mid",
    "repair_diagnosed": "mid",
    "rebase": "mechanical",
    "regenerate_generated_files": "mechanical",
    "worktree_hygiene": "mechanical",
    "ci_log_triage": "mechanical",
}


class UnboundTierError(ValueError):
    """A resolved tier has no model bound for the target provider."""


def model_for_tier(
    provider: str,
    tier: str,
    *,
    binding: dict[str, dict[str, str]] | None = None,
) -> str | None:
    """Return the model id ``provider`` should use for ``tier``.

    Returns ``None`` ONLY when ``vendor_binding`` is entirely unconfigured
    (no deployment has set it up yet at all) — the provider's ambient default
    applies, matching every other unconfigured Apis surface, and matching
    ``action_class=None`` callers who never reach this function in the first
    place. Once ANY vendor_binding exists, a provider missing from it, or
    present but lacking this specific tier, both raise ``UnboundTierError``
    rather than silently falling back to the ambient default or a different
    tier's model. Treating "provider absent from the map" as permissive would
    let a real deployment — one that HAS configured tiering for some
    providers — silently run an unconfigured provider un-tiered while the
    dispatch's own ``harness_event`` still records ``tier_source="policy"``
    as though the floor were honored. That is exactly the incident this
    module exists to close (module docstring): unreadable/unresolved must
    fail to the strongest tier, never to silence.
    """
    active_binding = configured_vendor_binding() if binding is None else binding
    if not active_binding:
        return None
    normalized_provider = provider.strip().lower()
    provider_map = active_binding.get(normalized_provider)
    if provider_map is None:
        raise UnboundTierError(
            f"vendor_binding is configured (for {sorted(active_binding)}) but "
            f"has no entry at all for provider {provider!r} — refusing to run "
            f"tier {tier!r} on this provider's ambient default; configure "
            f"{_VENDOR_BINDING_ENV} or {_VENDOR_BINDING_FILE_ENV} rather than "
            "falling back silently"
        )
    model = provider_map.get(tier)
    if model is None:
        raise UnboundTierError(
            f"vendor_binding for provider {provider!r} has no model bound for "
            f"tier {tier!r} (bound tiers: {sorted(provider_map)}); configure "
            f"{_VENDOR_BINDING_ENV} or {_VENDOR_BINDING_FILE_ENV} rather than "
            "falling back silently"
        )
    return model
