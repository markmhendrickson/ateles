"""Focused tests for action-class model tiering (operator ruling 2026-09-29)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import model_tiering  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch, tmp_path):
    monkeypatch.delenv("APIS_ACTION_POLICY", raising=False)
    monkeypatch.delenv("APIS_VENDOR_BINDING", raising=False)
    monkeypatch.setenv(
        "APIS_ACTION_POLICY_FILE", str(tmp_path / "missing-action-policy.json")
    )
    monkeypatch.setenv(
        "APIS_VENDOR_BINDING_FILE", str(tmp_path / "missing-vendor-binding.json")
    )
    yield


# ── configured_action_policy / configured_vendor_binding ────────────────────


def test_action_policy_defaults_empty_when_unconfigured() -> None:
    assert model_tiering.configured_action_policy() == {}


def test_vendor_binding_defaults_empty_when_unconfigured() -> None:
    assert model_tiering.configured_vendor_binding() == {}


def test_action_policy_file_beats_env(monkeypatch, tmp_path) -> None:
    """File-then-env precedence, matching harness_router.configured_headroom."""
    policy_path = tmp_path / "action-policy.json"
    policy_path.write_text(json.dumps({"build": "top"}))
    monkeypatch.setenv("APIS_ACTION_POLICY_FILE", str(policy_path))
    monkeypatch.setenv("APIS_ACTION_POLICY", json.dumps({"build": "mechanical"}))
    assert model_tiering.configured_action_policy() == {"build": "top"}


def test_vendor_binding_file_beats_env(monkeypatch, tmp_path) -> None:
    binding_path = tmp_path / "vendor-binding.json"
    binding_path.write_text(json.dumps({"cursor": {"top": "from-file"}}))
    monkeypatch.setenv("APIS_VENDOR_BINDING_FILE", str(binding_path))
    monkeypatch.setenv(
        "APIS_VENDOR_BINDING", json.dumps({"cursor": {"top": "from-env"}})
    )
    assert model_tiering.configured_vendor_binding() == {
        "cursor": {"top": "from-file"}
    }


def test_malformed_action_policy_json_degrades_to_empty(monkeypatch) -> None:
    monkeypatch.setenv("APIS_ACTION_POLICY", "{not json")
    assert model_tiering.configured_action_policy() == {}


def test_malformed_vendor_binding_json_degrades_to_empty(monkeypatch) -> None:
    monkeypatch.setenv("APIS_VENDOR_BINDING", "{not json")
    assert model_tiering.configured_vendor_binding() == {}


def test_action_policy_drops_unrecognized_tier_name(monkeypatch) -> None:
    monkeypatch.setenv(
        "APIS_ACTION_POLICY",
        json.dumps({"build": "top", "carry_forward": "super-duper"}),
    )
    assert model_tiering.configured_action_policy() == {"build": "top"}


def test_vendor_binding_drops_unrecognized_tier_name(monkeypatch) -> None:
    monkeypatch.setenv(
        "APIS_VENDOR_BINDING",
        json.dumps({"cursor": {"top": "opus-thing", "bogus": "x"}}),
    )
    assert model_tiering.configured_vendor_binding() == {
        "cursor": {"top": "opus-thing"}
    }


def test_vendor_binding_lowercases_provider_and_tier(monkeypatch) -> None:
    monkeypatch.setenv(
        "APIS_VENDOR_BINDING", json.dumps({"CURSOR": {"TOP": "opus-thing"}})
    )
    assert model_tiering.configured_vendor_binding() == {
        "cursor": {"top": "opus-thing"}
    }


def test_vendor_binding_drops_provider_whose_value_is_not_an_object(monkeypatch) -> None:
    monkeypatch.setenv("APIS_VENDOR_BINDING", json.dumps({"cursor": "not-a-map"}))
    assert model_tiering.configured_vendor_binding() == {}


# ── resolve_tier: policy-driven, fail-closed to the top tier ────────────────


def test_resolve_tier_uses_policy_value_when_present() -> None:
    resolved = model_tiering.resolve_tier(
        "carry_forward_check", policy={"carry_forward_check": "mechanical"}
    )
    assert resolved.tier == "mechanical"
    assert resolved.source == "policy"
    assert resolved.action_class == "carry_forward_check"
    assert resolved.escalation_reasons == ()


def test_resolve_tier_unresolved_class_fails_to_top_not_to_no_floor() -> None:
    resolved = model_tiering.resolve_tier("some_unclassified_thing", policy={})
    assert resolved.tier == "top"
    assert resolved.source == "unresolved_class"


def test_resolve_tier_reads_live_config_when_policy_not_passed(monkeypatch) -> None:
    monkeypatch.setenv("APIS_ACTION_POLICY", json.dumps({"build": "mid"}))
    resolved = model_tiering.resolve_tier("build")
    assert resolved.tier == "mid"
    assert resolved.source == "policy"


@pytest.mark.parametrize("action_class,tier", [
    ("lens_review:security", "top"),
    ("lens_review:arch", "top"),
    ("build", "top"),
    ("security_fix", "top"),
    ("lens_review:pm", "mid"),
    ("carry_forward_check", "mid"),
    ("repair_diagnosed", "mid"),
    ("rebase", "mechanical"),
    ("regenerate_generated_files", "mechanical"),
    ("worktree_hygiene", "mechanical"),
])
def test_every_checked_in_default_resolves(action_class, tier) -> None:
    """Guards a typo'd literal in DEFAULT_ACTION_POLICY_HINT from shipping."""
    resolved = model_tiering.resolve_tier(
        action_class, policy=model_tiering.DEFAULT_ACTION_POLICY_HINT
    )
    assert resolved.tier == tier


# ── escalation: can only raise, never substitute for or lower a tier ────────


def test_no_signals_leaves_tier_unchanged() -> None:
    tier, reasons = model_tiering.escalate("mechanical", None)
    assert tier == "mechanical"
    assert reasons == []


def test_empty_signals_leave_tier_unchanged() -> None:
    tier, reasons = model_tiering.escalate("mechanical", model_tiering.EscalationSignals())
    assert tier == "mechanical"
    assert reasons == []


def test_large_diff_escalates_to_top() -> None:
    signals = model_tiering.EscalationSignals(diff_lines_changed=401)
    tier, reasons = model_tiering.escalate("mechanical", signals)
    assert tier == "top"
    assert "diff_lines_changed=401>400" in reasons


def test_diff_at_threshold_does_not_escalate() -> None:
    signals = model_tiering.EscalationSignals(diff_lines_changed=400)
    tier, reasons = model_tiering.escalate("mechanical", signals)
    assert tier == "mechanical"
    assert reasons == []


def test_security_sensitive_path_escalates_to_top() -> None:
    signals = model_tiering.EscalationSignals(
        changed_files=("execution/daemons/apis/auth/token.py",)
    )
    tier, reasons = model_tiering.escalate("mechanical", signals)
    assert tier == "top"
    assert "touches_security_sensitive_path" in reasons


def test_non_sensitive_path_does_not_escalate() -> None:
    signals = model_tiering.EscalationSignals(
        changed_files=("docs/readme.md",)
    )
    tier, reasons = model_tiering.escalate("mechanical", signals)
    assert tier == "mechanical"
    assert reasons == []


def test_prior_blocking_finding_escalates_to_top() -> None:
    signals = model_tiering.EscalationSignals(prior_blocking_finding=True)
    tier, reasons = model_tiering.escalate("mechanical", signals)
    assert tier == "top"
    assert "prior_blocking_finding" in reasons


def test_repeated_review_round_escalates_to_top() -> None:
    signals = model_tiering.EscalationSignals(review_round=2)
    tier, reasons = model_tiering.escalate("mechanical", signals)
    assert tier == "top"
    assert "review_round=2" in reasons


def test_first_review_round_does_not_escalate() -> None:
    signals = model_tiering.EscalationSignals(review_round=1)
    tier, reasons = model_tiering.escalate("mechanical", signals)
    assert tier == "mechanical"
    assert reasons == []


def test_failed_prior_attempt_escalates_to_top() -> None:
    signals = model_tiering.EscalationSignals(prior_attempt_failed=True)
    tier, reasons = model_tiering.escalate("mechanical", signals)
    assert tier == "top"
    assert "prior_attempt_failed" in reasons


def test_escalation_never_lowers_an_already_top_tier() -> None:
    signals = model_tiering.EscalationSignals(prior_attempt_failed=True)
    tier, reasons = model_tiering.escalate("top", signals)
    assert tier == "top"


def test_resolve_tier_reports_escalated_source_when_signals_fire() -> None:
    resolved = model_tiering.resolve_tier(
        "rebase",
        policy={"rebase": "mechanical"},
        signals=model_tiering.EscalationSignals(prior_blocking_finding=True),
    )
    assert resolved.tier == "top"
    assert resolved.source == "escalated"
    assert resolved.escalation_reasons == ("prior_blocking_finding",)


def test_unresolved_class_that_also_escalates_still_reports_reasons() -> None:
    resolved = model_tiering.resolve_tier(
        "some_unclassified_thing",
        policy={},
        signals=model_tiering.EscalationSignals(prior_attempt_failed=True),
    )
    assert resolved.tier == "top"
    # Already top from being unresolved; escalation still names its reason.
    assert resolved.escalation_reasons == ("prior_attempt_failed",)


# ── model_for_tier: binding resolution, fail-closed on a partial binding ────


def test_model_for_tier_returns_none_when_vendor_binding_entirely_unconfigured() -> None:
    """No vendor_binding at all (a deployment that hasn't set up tiering) is
    the ONLY permissive case — the provider's ambient default applies."""
    assert model_tiering.model_for_tier("cursor", "top", binding={}) is None


def test_model_for_tier_raises_when_binding_exists_but_provider_is_absent() -> None:
    """A vendor_binding that configures SOME providers but not this one must
    not silently let this provider run un-tiered on its ambient default —
    that would be a live deployment where the dispatch's own harness_event
    still claims tier_source="policy" as though the floor were honored, which
    is exactly the incident this module exists to prevent (regression guard
    for the fix that closed this hole)."""
    binding = {"claude": {"top": "claude-opus-5-thinking-high"}}
    with pytest.raises(model_tiering.UnboundTierError, match="cursor"):
        model_tiering.model_for_tier("cursor", "top", binding=binding)


def test_model_for_tier_returns_bound_model() -> None:
    binding = {"cursor": {"top": "claude-opus-5-thinking-high"}}
    assert (
        model_tiering.model_for_tier("cursor", "top", binding=binding)
        == "claude-opus-5-thinking-high"
    )


def test_model_for_tier_raises_on_partial_binding_missing_this_tier() -> None:
    """A provider WITH a binding but no entry for this tier must not silently
    fall back — that would be indistinguishable from a real "no floor" answer
    and hide a config typo behind normal-looking behaviour."""
    binding = {"cursor": {"mechanical": "some-cheap-model"}}
    with pytest.raises(model_tiering.UnboundTierError, match="cursor"):
        model_tiering.model_for_tier("cursor", "top", binding=binding)


def test_model_for_tier_is_case_insensitive_on_provider(monkeypatch) -> None:
    monkeypatch.setenv(
        "APIS_VENDOR_BINDING", json.dumps({"cursor": {"top": "opus-thing"}})
    )
    assert model_tiering.model_for_tier("CURSOR", "top") == "opus-thing"


# ── tier ordering sanity ─────────────────────────────────────────────────────


def test_tiers_are_ordered_weakest_to_strongest() -> None:
    assert model_tiering.TIERS == ("local", "mechanical", "mid", "top")


def test_default_tier_is_the_strongest() -> None:
    assert model_tiering.DEFAULT_TIER == model_tiering.TIERS[-1]


# ── ruling `small_rereview_rounds_run_mid` (2026-09-29) ─────────────────────


def _policy():
    return {"lens_review:pm": "mid", "lens_review:qa": "mid", "lens_review:ux": "mid",
            "lens_review:arch": "top", "lens_review:security": "top",
            "carry_forward_check": "mid"}


def _resolve(klass, **signal_kwargs):
    return model_tiering.resolve_tier(
        klass, signals=model_tiering.EscalationSignals(**signal_kwargs), policy=_policy()
    )


@pytest.mark.parametrize("klass", ["lens_review:pm", "lens_review:qa", "lens_review:ux"])
def test_small_rereview_of_a_round_tolerant_lens_stays_mid(klass) -> None:
    resolved = _resolve(klass, review_round=3, prior_blocking_finding=True,
                        diff_lines_changed=120, changed_files=("src/a.py",))
    assert (resolved.tier, resolved.source) == ("mid", "policy")


@pytest.mark.parametrize("klass", ["lens_review:arch", "lens_review:security"])
def test_rereview_of_arch_and_security_stays_top(klass) -> None:
    assert _resolve(klass, review_round=2).tier == "top"


def test_round_still_escalates_a_class_that_is_not_round_tolerant() -> None:
    resolved = _resolve("carry_forward_check", review_round=2)
    assert resolved.tier == "top"
    assert resolved.escalation_reasons == ("review_round=2",)


@pytest.mark.parametrize(
    "signals,reason",
    [
        ({"diff_lines_changed": 401}, "diff_lines_changed=401>400"),
        ({"changed_files": (".claude/hooks/x.py",)}, "touches_security_sensitive_path"),
        ({"new_blocking_finding": True}, "new_blocking_finding"),
        ({"prior_attempt_failed": True}, "prior_attempt_failed"),
        ({"diff_unreadable": True}, "diff_unreadable"),
    ],
)
def test_every_other_signal_still_raises_a_round_tolerant_lens(signals, reason) -> None:
    resolved = _resolve("lens_review:pm", review_round=2, **signals)
    assert resolved.tier == "top"
    assert reason in resolved.escalation_reasons


def test_a_diff_at_exactly_the_threshold_is_still_small() -> None:
    assert _resolve("lens_review:qa", review_round=2, diff_lines_changed=400).tier == "mid"


def test_new_blocking_finding_raises_any_class() -> None:
    assert _resolve("carry_forward_check", new_blocking_finding=True).tier == "top"


def test_round_tolerance_does_not_apply_to_an_unmapped_class() -> None:
    resolved = model_tiering.resolve_tier(
        "lens_review:legal",
        signals=model_tiering.EscalationSignals(review_round=2), policy=_policy(),
    )
    assert resolved.tier == "top"
