"""Approved policy caps must bind at resolution, including every signal mix."""

import itertools
import json

import pytest

import model_tiering as tiers


SIGNALS = list(itertools.product((False, True), repeat=7))


def signals(bits):
    large, sensitive, prior, repeated, failed, unreadable, new = bits
    return tiers.EscalationSignals(
        diff_lines_changed=401 if large else 0,
        changed_files=("lib/auth/check.py",) if sensitive else (),
        prior_blocking_finding=prior,
        review_round=3 if repeated else 1,
        prior_attempt_failed=failed,
        diff_unreadable=unreadable,
        new_blocking_finding=new,
    )


@pytest.mark.parametrize("action", ["lens_review:content", "panel_aggregation"])
@pytest.mark.parametrize("bits", SIGNALS)
def test_mid_cap_survives_every_automatic_signal_combination(action, bits):
    result = tiers.resolve_tier(
        action, signals=signals(bits),
        policy={action: {"min_tier": "mid", "max_tier": "mid"}},
    )
    assert result.tier == "mid"
    assert result.escalation_reasons == tuple(signals(bits).reasons(action))
    assert result.source == ("capped" if any(bits) else "policy")


@pytest.mark.parametrize("action", ["lens_review:eng", "lens_review:legal", "task_dispatch"])
def test_uncapped_mid_classes_still_escalate(action):
    assert tiers.resolve_tier(action, policy={action: "mid"}).tier == "mid"
    assert tiers.resolve_tier(
        action, signals=signals((True,) * 7), policy={action: "mid"},
    ).tier == "top"


@pytest.mark.parametrize("action", ["lens_review:security", "lens_review:arch"])
@pytest.mark.parametrize("bits", SIGNALS)
def test_protected_review_floors_remain_top(action, bits):
    assert tiers.resolve_tier(
        action, signals=signals(bits), policy=tiers.DEFAULT_ACTION_POLICY_HINT,
    ).tier == "top"


def test_blocking_pm_still_escalates():
    assert tiers.resolve_tier(
        "lens_review:pm", signals=tiers.EscalationSignals(new_blocking_finding=True),
        policy=tiers.DEFAULT_ACTION_POLICY_HINT,
    ).tier == "top"


@pytest.mark.parametrize("entry", [
    {"min_tier": "mid", "max_tier": "unknown"},
    {"min_tier": "top", "max_tier": "mid"},
    {"min_tier": "mid", "max_tier": None},
    {"max_tier": "mid"},
    {"min_tier": "mid", "max_tire": "mid"},
])
def test_malformed_cap_refuses_dispatch(entry):
    with pytest.raises(tiers.InvalidTierPolicyError):
        tiers.resolve_tier("panel_aggregation", policy={"panel_aggregation": entry})


def test_config_reader_and_validator_preserve_cap(monkeypatch, tmp_path):
    path = tmp_path / "policy.json"
    policy = {"panel_aggregation": {"min_tier": "mid", "max_tier": "mid"}, "build": "top"}
    path.write_text(json.dumps(policy))
    monkeypatch.setenv("APIS_ACTION_POLICY_FILE", str(path))
    assert tiers.configured_action_policy() == policy
    kind, _, errors, _ = tiers.check_config_file(path)
    assert kind == "action-policy" and not errors
    assert tiers.check_cross(policy, {"codex": {"top": "top-model"}})
    assert tiers.resolve_tier("panel_aggregation", signals=signals((True,) * 7)).tier == "mid"


def test_reference_policy_contains_approved_five_classes():
    for action in ["lens_review:content", "panel_aggregation", "lens_review:eng", "lens_review:legal", "task_dispatch"]:
        assert tiers.resolve_tier(action, policy=tiers.DEFAULT_ACTION_POLICY_HINT).tier == "mid"
