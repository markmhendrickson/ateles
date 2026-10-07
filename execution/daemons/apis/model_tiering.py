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

Example config: ``docs/examples/model-tiering/`` holds a committed,
operator-neutral ``action-policy.json`` (kept equal to
``DEFAULT_ACTION_POLICY_HINT`` by a test) and ``vendor-binding.json``.
Validate any live file before installing it with
``python3 execution/daemons/apis/model_tiering.py --check <file> [<file>]``.
``execution/scripts/harness_usage.py tiers`` reports dispatches per tier and
``harness_usage.py spend`` reports what they spent.

Escalation signals (below) can only RAISE the resolved tier, never lower it —
they are cheap, deterministic, measured facts about the dispatch (diff size,
a security-sensitive path, a prior blocking finding, a repeated round, a
failed prior attempt), not a classifier's guess.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
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

# Operator ruling `small_rereview_rounds_run_mid` (2026-09-29): pm, qa and ux
# re-review rounds of a SMALL change run at their policy tier (mid) instead of
# escalating on the round alone. arch and security re-rounds stay top, as do
# large or security-touching ones; that is why only these three are listed.
ROUND_TOLERANT_ACTION_CLASSES: frozenset[str] = frozenset(
    {"lens_review:pm", "lens_review:qa", "lens_review:ux"}
)


# Classes whose defining condition IS "an earlier round blocked": a carry-forward
# check runs because a PR is being carried through another round, and a
# diagnosed repair runs because a review already diagnosed blocking findings.
# `prior_blocking_finding` and `review_round` are therefore true of every such
# dispatch by definition, so escalating on them pinned every one to top
# (tier ledger 2026-09-29: carry_forward_check and repair_diagnosed both 100%
# top on exactly those two signals). Every OTHER signal still raises them: a
# failed prior attempt, security paths, a large or unreadable diff.
# `security_fix` is deliberately absent: it stays top on its own policy tier.
ROUND_DEFINED_ACTION_CLASSES: frozenset[str] = frozenset(
    {"carry_forward_check", "repair_diagnosed"}
)


# A code default for a class the live action_policy does not map. Deliberately
# tiny and explicit: it is NOT a general "unmapped means cheap" rule (every other
# class still fails up to `top`), and the live policy always wins over it. It
# exists so the pre-gate producer score works on a deployment whose policy file
# predates the class, rather than being inert until someone edits config.
CLASS_DEFAULT_TIERS: dict[str, str] = {
    "confidence_scoring": "mid",
}


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
    # The caller tried to measure the diff (changed files / line count) and
    # could not. An unmeasured diff must not be treated as a small one: with no
    # measurement the security-path and size signals are both silently False,
    # which would tier a possibly-large or security-sensitive dispatch DOWN.
    diff_unreadable: bool = False
    # A blocking finding that no earlier round had raised (ruling
    # `small_rereview_rounds_run_mid`, 2026-09-29). Unlike `prior_blocking_finding`
    # (an earlier round blocked, which a re-review is by definition verifying),
    # this means the last round turned up something NEW — the change is not
    # converging, so it raises any class.
    new_blocking_finding: bool = False

    def touches_security_sensitive_path(self) -> bool:
        return any(
            fragment in changed_file
            for changed_file in self.changed_files
            for fragment in SECURITY_SENSITIVE_PATH_FRAGMENTS
        )

    def reasons(self, action_class: str = "") -> list[str]:
        """Every reason this dispatch's tier should be raised, for logging.

        ``action_class`` matters for exactly one narrowing: a re-review round
        (and the fact that an earlier round blocked) does not by itself raise
        a class in ``ROUND_TOLERANT_ACTION_CLASSES`` (pm/qa/ux re-reviews) or
        ``ROUND_DEFINED_ACTION_CLASSES`` (carry-forward checks and diagnosed
        repairs, which those two signals define). Size, security paths, a NEW
        blocking finding, a failed attempt and an unreadable diff still do.
        """
        tolerant = (
            action_class in ROUND_TOLERANT_ACTION_CLASSES
            or action_class in ROUND_DEFINED_ACTION_CLASSES
        )
        out: list[str] = []
        if self.diff_lines_changed > LARGE_DIFF_LINE_THRESHOLD:
            out.append(f"diff_lines_changed={self.diff_lines_changed}>{LARGE_DIFF_LINE_THRESHOLD}")
        if self.touches_security_sensitive_path():
            out.append("touches_security_sensitive_path")
        if self.prior_blocking_finding and not tolerant:
            out.append("prior_blocking_finding")
        if self.new_blocking_finding:
            out.append("new_blocking_finding")
        if self.review_round > 1 and not tolerant:
            out.append(f"review_round={self.review_round}")
        if self.prior_attempt_failed:
            out.append("prior_attempt_failed")
        if self.diff_unreadable:
            out.append("diff_unreadable")
        return out


def escalate(
    base_tier: str,
    signals: "EscalationSignals | None",
    action_class: str = "",
) -> tuple[str, list[str]]:
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
    reasons = signals.reasons(action_class)
    if not reasons:
        return base_tier, []
    return _higher_tier(base_tier, DEFAULT_TIER), reasons


@dataclass(frozen=True)
class ResolvedTier:
    """The tier a dispatch should run at, and how that was decided."""

    tier: str
    source: str  # "policy" | "class_default" | "unresolved_class" | "escalated"
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
        # A class the operator has not mapped. For the few classes that have a
        # deliberate code default (CLASS_DEFAULT_TIERS) that default applies;
        # for every other class this fails UP to the strongest tier.
        base_tier = CLASS_DEFAULT_TIERS.get(action_class)
        if base_tier is None:
            base_tier = DEFAULT_TIER
            source = "unresolved_class"
        else:
            source = "class_default"
    else:
        source = "policy"
    escalated_tier, reasons = escalate(base_tier, signals, action_class)
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
    "issue_triage": "mid",
    "carry_forward_check": "mid",
    "repair_diagnosed": "mid",
    "rebase": "mechanical",
    "regenerate_generated_files": "mechanical",
    "worktree_hygiene": "mechanical",
    "ci_log_triage": "mechanical",
    "confidence_scoring": "mechanical",
}


# ── Action-class vocabulary used by the Apis dispatch sites ──────────────────
#
# The single home for the class names ``swarm_dispatch`` passes to
# ``run_skill(action_class=...)``. Names come from the operator ruling and
# ``DEFAULT_ACTION_POLICY_HINT`` above. ``issue_triage`` was ruled mid on
# 2026-09-29 (``issue_triage_and_backfill_run_mid``). The two below the line
# are classes no ruling names: they are deliberately absent from the committed
# example policy, so they resolve ``unresolved_class -> top`` (fail-closed)
# until the operator rules on them, and every dispatch of one is visible as
# ``tiering=top(unresolved_class)``.

ACTION_BUILD = "build"
ACTION_SECURITY_FIX = "security_fix"
ACTION_REPAIR_DIAGNOSED = "repair_diagnosed"
ACTION_CARRY_FORWARD_CHECK = "carry_forward_check"
ACTION_CI_LOG_TRIAGE = "ci_log_triage"
ACTION_ISSUE_TRIAGE = "issue_triage"  # Lanius new-issue protocol and entity backfill
# The pre-gate producer score for an unscored low-blast task (ateles#1142,
# producer_confidence.py). A model call whose entire output is one number: it
# has a deliberate code default below, and the scorer additionally refuses to
# run at `top`, so a config that maps it up or leaves it unmapped can only
# ever switch it off, never make it expensive.
ACTION_CONFIDENCE_SCORING = "confidence_scoring"
# Not named by any ruling — unmapped on purpose, so they run at ``top``:
ACTION_PANEL_AGGREGATION = "panel_aggregation"  # Vanellus verdict aggregation
ACTION_TASK_DISPATCH_FALLBACK = "task_dispatch"  # queue task with no action_type

LENS_REVIEW_PREFIX = "lens_review:"

# Lenses ``review_panel.LENSES`` seats today, plus ``eng`` (an issue-spec
# section). ``legal``, ``content`` and ``eng`` have no entry in the ruling, so
# their ``lens_review:`` classes are left unmapped and run at ``top``.
KNOWN_LENSES: tuple[str, ...] = (
    "pm", "arch", "ux", "legal", "qa", "security", "content",
    # Issue-spec sections only: Cicada authors the Engineering section.
    "eng",
)

# Every class an Apis call site passes, for ``--check`` to warn about classes a
# config leaves unmapped (which then resolve to ``top``).
DISPATCH_ACTION_CLASSES: tuple[str, ...] = (
    ACTION_BUILD,
    ACTION_SECURITY_FIX,
    ACTION_REPAIR_DIAGNOSED,
    ACTION_CARRY_FORWARD_CHECK,
    ACTION_CI_LOG_TRIAGE,
    ACTION_ISSUE_TRIAGE,
    ACTION_PANEL_AGGREGATION,
    *(f"{LENS_REVIEW_PREFIX}{lens}" for lens in KNOWN_LENSES),
)


def lens_review_class(lens: str) -> str:
    """Action class for one lens's review (or spec section, or fix guidance)."""
    return f"{LENS_REVIEW_PREFIX}{lens.strip().lower()}"


def repair_action_class(blocking_lenses: "set[str] | frozenset[str]") -> str:
    """Action class for a Cicada repair of already-diagnosed blocking findings.

    The ONE classifier both repair sites call (the fix-round path and the
    revision sweep), so they cannot drift: a security finding among them makes
    it a ``security_fix`` (stays top, per the ruling); anything else is a
    ``repair_diagnosed`` (mid). Security review of ateles#1358 found the sweep
    classifying every repair as the latter.
    """
    return (
        ACTION_SECURITY_FIX
        if "security" in {str(l).strip().lower() for l in blocking_lenses}
        else ACTION_REPAIR_DIAGNOSED
    )


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


# ── Observability: which tier did each dispatch actually run at ──────────────
#
# Every dispatch appends one ``event: "dispatch"`` line here when it starts and
# logs the same facts, and one ``event: "usage"`` line when it ends (tokens and
# cost as the provider reported them, ``null`` otherwise), tied by
# ``dispatch_id``. So "how much of the week ran on each tier" and "what did it
# spend" are answerable from one file instead of a Neotoma query per
# harness_event. Rows from before ``event`` existed are start rows. Best-effort by construction: a ledger write
# failure is logged and swallowed, never raised into a dispatch. The Neotoma
# ``harness_event`` (skill_runner) stays the durable audit row; this is the
# cheap local read for budget pacing.

_TIER_LEDGER_FILE_ENV = "APIS_TIER_LEDGER_FILE"
_DEFAULT_TIER_LEDGER_PATH = (
    Path.home() / "Library" / "Logs" / "ateles" / "tier-dispatch.jsonl"
)

# Ledger buckets that are not tiers. A dispatch that named no action class runs
# on the provider's ambient default (the most expensive model) — the very thing
# this whole change exists to remove — so it is counted under its own name
# rather than hidden inside a real tier.
UNTIERED = "untiered"
EXPLICIT_MODEL = "explicit_model"


def tier_ledger_path() -> Path:
    configured = os.environ.get(_TIER_LEDGER_FILE_ENV, "").strip()
    return Path(configured).expanduser() if configured else _DEFAULT_TIER_LEDGER_PATH


def describe_tiering(
    resolved: "ResolvedTier | None", model: str | None
) -> tuple[str, str]:
    """Return ``(tier, source)`` for a dispatch, including the un-tiered cases."""
    if resolved is not None:
        return resolved.tier, resolved.source
    if model:
        return EXPLICIT_MODEL, "explicit_model"
    return UNTIERED, "no_action_class"


USAGE_EVENT = "usage"
DISPATCH_EVENT = "dispatch"


def new_dispatch_id() -> str:
    """A key tying one dispatch's start row to its usage row in the ledger."""
    return uuid.uuid4().hex


def _append_ledger_row(row: dict) -> None:
    """Best-effort append; a write failure is logged, never raised into a dispatch."""
    try:
        path = tier_ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")
    except OSError as exc:
        log.warning(f"[apis] model_tiering: tier ledger write failed (non-fatal): {exc}")


def record_dispatch(
    *,
    skill: str,
    provider: str,
    resolved: "ResolvedTier | None",
    model: str | None,
    dispatch_id: str | None = None,
) -> str:
    """Log and ledger one dispatch's tiering; return the log marker.

    The marker (``tiering=<tier>(<source>) model=<model|default>``) is what the
    caller logs, so the log line and the ledger row cannot disagree.

    ``dispatch_id`` ties this start row to the usage row ``record_dispatch_usage``
    appends when the dispatch ends (tokens and cost are not known until then).
    """
    tier, source = describe_tiering(resolved, model)
    marker = f"tiering={tier}({source}) model={model or 'default'}"
    row = {
        "event": DISPATCH_EVENT,
        "dispatch_id": dispatch_id or "",
        "ts": datetime.now(timezone.utc).isoformat(),
        "skill": skill,
        "provider": provider,
        "action_class": resolved.action_class if resolved else "",
        "tier": tier,
        "source": source,
        "model": model or "",
        "escalation_reasons": list(resolved.escalation_reasons) if resolved else [],
    }
    _append_ledger_row(row)
    return marker


# Token fields a usage row carries, in the order they are reported. Each is
# written as an explicit ``null`` when the harness did not report it: a reader
# must be able to tell "not reported" from "measured zero", and the ledger must
# never hold an estimate.
USAGE_TOKEN_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
    "total_tokens",
)


def record_dispatch_usage(
    *,
    dispatch_id: str,
    skill: str,
    provider: str,
    resolved: "ResolvedTier | None",
    requested_model: str | None,
    usage: object,
    ok: bool | None = None,
) -> None:
    """Append the end-of-dispatch spend row for one dispatch.

    ``usage`` is a ``dispatch_usage.DispatchUsage``. Read by attribute so this
    module stays import-light. Everything the provider did not report is
    ``null``; nothing is derived or estimated. ``model`` is the model the
    harness reported when it named one, else the model that was requested, and
    ``model_source`` says which (``reported`` / ``requested`` / ``default``).
    ``ok`` is the dispatch's outcome, so a failed or timed-out dispatch's spend
    is attributable too.
    """
    tier, source = describe_tiering(resolved, requested_model)
    row: dict = {
        "event": USAGE_EVENT,
        "dispatch_id": dispatch_id or "",
        "ts": datetime.now(timezone.utc).isoformat(),
        "skill": skill,
        "provider": provider,
        "action_class": resolved.action_class if resolved else "",
        "tier": tier,
        "source": source,
        "model": getattr(usage, "model", None) or requested_model or "",
        "model_source": getattr(usage, "model_source", None),
        "ok": ok,
    }
    for name in USAGE_TOKEN_FIELDS:
        row[name] = getattr(usage, name, None)
    row["total_cost_usd"] = getattr(usage, "total_cost_usd", None)
    _append_ledger_row(row)


def reason_name(reason: str) -> str:
    """The signal a ledger escalation reason names, without its measurement.

    ``diff_lines_changed=1054>400`` -> ``diff_lines_changed``;
    ``review_round=2`` -> ``review_round``; a bare reason is its own name.
    """
    return str(reason).split("=", 1)[0].strip()


def tier_counts(
    *, since_hours: float | None = None, with_reasons: bool = False
) -> dict:
    """Dispatch counts per tier (and per action class) from the ledger.

    ``{"total": n, "by_tier": {...}, "by_class": {"class": {"tier": n}}}``.
    Malformed lines are skipped, not fatal: one torn write must not blind the
    report. A missing ledger is an empty report, never an error.

    ``with_reasons`` adds ``"by_reason": {reason: {"total": n, "by_class":
    {class: n}}}``: how many escalated dispatches each signal appears on, so a
    change to the signals can be measured against the ledger. A dispatch with
    several reasons counts once under each, so the totals can exceed the
    number of escalated dispatches. Rows written before reasons were recorded
    simply contribute nothing.
    """
    cutoff = None
    if since_hours is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    by_tier: dict[str, int] = {}
    by_class: dict[str, dict[str, int]] = {}
    by_reason: dict[str, dict] = {}
    total = 0
    path = tier_ledger_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
            tier = str(row["tier"])
            when = datetime.fromisoformat(str(row["ts"]))
        except (ValueError, KeyError, TypeError):
            continue
        if row.get("event") == USAGE_EVENT:
            continue  # a spend row for a dispatch already counted by its start row
        if cutoff is not None and when < cutoff:
            continue
        total += 1
        by_tier[tier] = by_tier.get(tier, 0) + 1
        klass = str(row.get("action_class") or "(none)")
        by_class.setdefault(klass, {})
        by_class[klass][tier] = by_class[klass].get(tier, 0) + 1
        if with_reasons:
            raw_reasons = row.get("escalation_reasons") or []
            if not isinstance(raw_reasons, list):
                raw_reasons = []
            for name in {reason_name(r) for r in raw_reasons if str(r).strip()}:
                entry = by_reason.setdefault(name, {"total": 0, "by_class": {}})
                entry["total"] += 1
                entry["by_class"][klass] = entry["by_class"].get(klass, 0) + 1
    report = {"total": total, "by_tier": by_tier, "by_class": by_class}
    if with_reasons:
        report["by_reason"] = by_reason
    return report


class LedgerReadError(Exception):
    """The ledger exists but could not be read (not merely absent or empty)."""

    def __init__(self, path: Path, cause: BaseException) -> None:
        self.path = path
        self.cause = f"{type(cause).__name__}: {cause}"
        super().__init__(f"cannot read tier ledger {path}: {self.cause}")


USAGE_GROUPS: tuple[str, ...] = ("provider", "model", "tier", "action_class", "skill")


def usage_totals(
    *, group_by: str = "provider", since_hours: float | None = None
) -> dict:
    """Spend per ``group_by`` from the ledger's usage rows.

    ``{"group_by": g, "rows": n, "groups": {key: {"dispatches": n,
    "tokens": {field: {"sum": int | None, "reported_rows": n}},
    "cost": {"total_usd": float | None, "reported_rows": n}}}}``.

    Unknown is not zero. A field's ``sum`` is ``None`` when no row in the group
    reported it, and a measured zero stays ``0``. ``reported_rows`` is the
    number of dispatches that field's sum covers, so a total built from only
    some of a group's ``dispatches`` is visibly partial, field by field.
    Malformed lines and start rows are skipped; a missing ledger is an empty
    report. A ledger that exists but cannot be read raises ``LedgerReadError``
    rather than reporting as empty.
    """
    if group_by not in USAGE_GROUPS:
        raise ValueError(f"group_by must be one of {', '.join(USAGE_GROUPS)}")
    cutoff = None
    if since_hours is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=since_hours)
    groups: dict[str, dict] = {}
    rows = 0
    path = tier_ledger_path()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        lines = []  # no ledger yet is "no spend", not a failure
    except (OSError, UnicodeError) as exc:
        raise LedgerReadError(path, exc) from exc

    def _number(value: object) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    for line in lines:
        try:
            row = json.loads(line)
            when = datetime.fromisoformat(str(row["ts"]))
        except (ValueError, KeyError, TypeError):
            continue
        if not isinstance(row, dict) or row.get("event") != USAGE_EVENT:
            continue
        if cutoff is not None and when < cutoff:
            continue
        rows += 1
        key = str(row.get(group_by) or "(none)")
        entry = groups.setdefault(key, {
            "dispatches": 0,
            "tokens": {
                name: {"sum": None, "reported_rows": 0} for name in USAGE_TOKEN_FIELDS
            },
            "cost": {"total_usd": None, "reported_rows": 0},
        })
        entry["dispatches"] += 1
        for name in USAGE_TOKEN_FIELDS:
            value = row.get(name)
            if _number(value):
                field = entry["tokens"][name]
                field["sum"] = (field["sum"] or 0) + int(value)
                field["reported_rows"] += 1
        cost = row.get("total_cost_usd")
        if _number(cost):
            entry["cost"]["total_usd"] = (entry["cost"]["total_usd"] or 0.0) + float(cost)
            entry["cost"]["reported_rows"] += 1
    return {"group_by": group_by, "rows": rows, "groups": groups}


# ── Config validation: `model_tiering.py --check <file> [<file> ...]` ─────────


def _classify_config(data: object) -> str | None:
    """``action-policy`` (all string values), ``vendor-binding`` (all dict
    values), or None when the shape is neither or empty."""
    if not isinstance(data, dict) or not data:
        return None
    if all(isinstance(v, str) for v in data.values()):
        return "action-policy"
    if all(isinstance(v, dict) for v in data.values()):
        return "vendor-binding"
    return None


def check_config_file(path: Path) -> tuple[str | None, dict, list[str], list[str]]:
    """Validate one config file strictly. Returns ``(kind, data, errors, warnings)``.

    Stricter than the runtime loaders on purpose: the loaders drop a bad entry
    and carry on (so a typo can never crash a daemon), which means a typo only
    shows up as a class quietly running at ``top``. This is where a typo is
    caught before the file is installed.
    """
    errors: list[str] = []
    warnings: list[str] = []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return None, {}, [f"cannot read {path}: {exc}"], warnings
    except ValueError as exc:
        return None, {}, [f"{path}: not valid JSON ({exc})"], warnings
    kind = _classify_config(data)
    if kind is None:
        return None, {}, [
            f"{path}: neither an action_policy ({{class: tier}}) nor a "
            "vendor_binding ({provider: {tier: model}}) object, or empty"
        ], warnings

    if kind == "action-policy":
        for klass, tier in data.items():
            if not str(klass).strip():
                errors.append("action_policy has an empty class name")
            if str(tier).strip().lower() not in TIERS:
                errors.append(
                    f"action_policy class {klass!r} names unknown tier {tier!r} "
                    f"(valid: {', '.join(TIERS)}); the runtime would drop this "
                    f"entry and run the class at {DEFAULT_TIER!r}"
                )
        unmapped = [c for c in DISPATCH_ACTION_CLASSES if c not in data]
        if unmapped:
            warnings.append(
                "classes with no entry resolve to "
                f"{DEFAULT_TIER!r} (fail-closed): {', '.join(unmapped)}"
            )
        stale = [
            c for c in data
            if c not in DISPATCH_ACTION_CLASSES and c not in DEFAULT_ACTION_POLICY_HINT
        ]
        if stale:
            warnings.append(
                "classes no Apis call site passes (harmless, but dead): "
                + ", ".join(stale)
            )
    else:
        from harness_router import FRONTIER_PROVIDERS as PROVIDERS  # lazy: import-light

        for provider, tiers in data.items():
            name = str(provider).strip().lower()
            if name not in PROVIDERS:
                errors.append(
                    f"vendor_binding provider {provider!r} is not a frontier "
                    f"harness provider ({', '.join(PROVIDERS)})"
                )
            for tier, model in tiers.items():
                if str(tier).strip().lower() not in TIERS:
                    errors.append(
                        f"vendor_binding {provider!r} names unknown tier {tier!r}"
                    )
                if not str(model).strip():
                    errors.append(
                        f"vendor_binding {provider!r} tier {tier!r} has an empty model"
                    )
            if DEFAULT_TIER not in {str(t).strip().lower() for t in tiers}:
                errors.append(
                    f"vendor_binding {provider!r} binds no {DEFAULT_TIER!r} "
                    "model, but every escalation and every unmapped class "
                    "resolves there — the provider could run nothing"
                )
    return kind, data, errors, warnings


def check_cross(policy: dict, binding: dict) -> list[str]:
    """Errors for tiers the policy can require that a provider does not bind.

    A provider missing a needed tier is dropped from selection for that tier
    (skill_runner's pre-selection filter), which is legal, so this reports it
    as an error only when NO provider binds the tier — that dispatch would fail.
    """
    needed = {str(t).strip().lower() for t in policy.values()} | {DEFAULT_TIER}
    needed &= set(TIERS)
    errors: list[str] = []
    for tier in sorted(needed, key=_tier_index):
        if tier == "local":
            continue  # served by claude-local, which is exempt from binding
        if not any(tier in {str(t).strip().lower() for t in tiers}
                   for tiers in binding.values()):
            errors.append(
                f"the policy resolves classes to tier {tier!r} but no provider "
                "in the vendor_binding binds it — those dispatches would be refused"
            )
    return errors


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="model_tiering",
        description=(
            "Validate model-tiering config files before installing them. Pass "
            "an action_policy file, a vendor_binding file, or both (the two "
            "are then cross-checked). Exit 0 valid, 1 invalid."
        ),
    )
    parser.add_argument(
        "--check", nargs="+", metavar="FILE", required=True,
        help="config file(s) to validate; the kind is detected from the shape",
    )
    args = parser.parse_args(argv)

    exit_code = 0
    loaded: dict[str, dict] = {}
    for raw in args.check:
        path = Path(raw).expanduser()
        kind, data, errors, warnings = check_config_file(path)
        for msg in errors:
            print(f"ERROR {path}: {msg}")
        for msg in warnings:
            print(f"warn  {path}: {msg}")
        if errors or kind is None:
            exit_code = 1
            print(f"FAIL  {path}")
            continue
        loaded[kind] = data
        print(f"ok    {path} ({kind}, {len(data)} entries)")
    if "action-policy" in loaded and "vendor-binding" in loaded:
        for msg in check_cross(loaded["action-policy"], loaded["vendor-binding"]):
            print(f"ERROR cross-check: {msg}")
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
