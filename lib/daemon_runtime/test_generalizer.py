"""
Unit tests for the generalizer decision core and maturation lifecycle.

These cover the pure logic only — no Neotoma I/O. Run with:
    pytest lib/daemon_runtime/test_generalizer.py -v
"""

from __future__ import annotations

from drift import DriftCluster, DriftSignal, cluster_signals, parse_drift_signals
from generalizer import (
    DEFAULT_POLICY_CAP_PER_AGENT,
    Action,
    Maturation,
    PolicyState,
    affects_higher_layer,
    count_live_auto_policies,
    decide,
    maturation_decision,
)


def _cluster(agent: str, text: str, n: int) -> DriftCluster:
    sigs = []
    for i in range(n):
        sigs.extend(parse_drift_signals(f"[{agent}] strategy_drift_signal: {text}", f"ref{i}"))
    return cluster_signals(sigs)[0]


# ── decide() ────────────────────────────────────────────────────────────────


def test_below_threshold_is_noop():
    c = _cluster("pavo", "prefer terse landing copy", 2)
    d = decide(c, threshold=3, live_auto_policy_count=0)
    assert d.action == Action.NOOP


def test_threshold_met_auto_applies_agent_local():
    c = _cluster("pavo", "prefer terse landing copy", 3)
    d = decide(c, threshold=3, live_auto_policy_count=0)
    assert d.action == Action.AUTO_APPLY
    assert not d.affects_higher_layer


def test_higher_layer_routes_to_proposal():
    c = _cluster("pavo", "our pricing strategy and roadmap should shift", 5)
    d = decide(c, threshold=3, live_auto_policy_count=0)
    assert d.action == Action.PROPOSE
    assert d.affects_higher_layer


def test_cap_routes_to_proposal():
    c = _cluster("pavo", "prefer terse landing copy", 5)
    d = decide(c, threshold=3, live_auto_policy_count=DEFAULT_POLICY_CAP_PER_AGENT)
    assert d.action == Action.PROPOSE


def test_operator_conflict_routes_to_proposal():
    c = _cluster("pavo", "prefer terse landing copy", 5)
    d = decide(c, threshold=3, live_auto_policy_count=0, conflicts_with_operator_policy=True)
    assert d.action == Action.PROPOSE


def test_affects_higher_layer_keywords():
    assert affects_higher_layer(_cluster("pavo", "change the company north star metric", 1))
    assert not affects_higher_layer(_cluster("pavo", "prefer terse copy in headers", 1))


# ── maturation lifecycle ──────────────────────────────────────────────────────


def test_maturation_holds_below_threshold():
    s = PolicyState(auto_generated=True, application_count=4, maturation_threshold=9)
    assert maturation_decision(s, new_contradiction=False) == Maturation.HOLD


def test_maturation_promotes_by_exposure_not_time():
    s = PolicyState(auto_generated=True, application_count=9, maturation_threshold=9)
    assert maturation_decision(s, new_contradiction=False) == Maturation.PROMOTE


def test_contradiction_suspends_regardless_of_exposure():
    s = PolicyState(auto_generated=True, application_count=100, maturation_threshold=9)
    assert maturation_decision(s, new_contradiction=True) == Maturation.SUSPEND


def test_existing_contradiction_count_keeps_suspended():
    s = PolicyState(auto_generated=True, application_count=100, contradiction_count=1)
    assert maturation_decision(s, new_contradiction=False) == Maturation.SUSPEND


# ── PolicyState (de)serialization ──────────────────────────────────────────────


def test_policy_state_round_trip():
    s = PolicyState(
        auto_generated=True,
        application_count=3,
        contradiction_count=1,
        maturation_threshold=9,
        drift_signal_refs=["a", "b"],
        confirmed_at="2026-05-29T00:00:00+00:00",
    )
    back = PolicyState.from_notes(s.to_notes())
    assert back == s


def test_policy_state_handles_garbage_notes():
    assert PolicyState.from_notes("not json").application_count == 0
    assert PolicyState.from_notes("").auto_generated is False


def test_count_live_auto_policies_ignores_human_and_retired():
    # Maturation JSON lives in the agent_policy `body` field (no `notes` field).
    auto = PolicyState(auto_generated=True).to_notes()
    human = PolicyState(auto_generated=False).to_notes()
    policies = [
        {"status": "provisional", "body": auto},
        {"status": "active", "body": auto},
        {"status": "retired", "body": auto},  # retired -> not counted
        {"status": "active", "body": human},  # human-authored -> not counted
    ]
    assert count_live_auto_policies(policies) == 2
