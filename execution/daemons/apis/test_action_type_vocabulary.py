"""The action_type vocabularies must agree mechanically, not by comment.

`apis.py` carries this above `_AGENT_ACTION_TYPE`:

    # Values MUST match the execution_policy's high/low_blast_action_types
    # vocabulary (default policy ent_dfce6edecefe3eb7fc9e0337) or the gate
    # mis-classifies blast radius.

Nothing enforced it. A comment claiming two constants match is not a mechanism
that keeps them matching — the same shape as `lib/issue_labels.py` declaring
`PRE_IMPL_GATE_NAMES = ("pm", "arch")` directly under a comment saying it
mirrors `swarm_dispatch.PRE_IMPL_GATES`, which is `("pm", "ux", "arch")`.

These vocabularies AGREE TODAY. That is exactly why this test exists now: a
gate written while the invariant holds fails on the drift, whereas one written
after the drift tends to get weakened until it passes. Reverting the fix here
means deleting the test, which is itself the red.

Scope, stated precisely. ateles#715 already closed the dangerous half: an
action type in neither policy set resolves to `BlastRadius.NEVER` with a
warning naming the value, in both the enforcing path
(`gating.ExecutionPolicy.blast_radius_for`) and the advisory path
(`server._action_blast_radius`). An unclassified type is no longer silently
LOW. What remains is that a mismatch is only ever discovered at RUNTIME, one
task at a time, in a log line nobody reads — and its effect now is to hold work
rather than release it. So this is a drift detector, not a safety patch, and it
is written as a pytest rather than a `scripts/lint.sh` linter because a linter
registered only there may be invoked by no workflow (CLAUDE.md, "a mechanism
that does not bind is not a control").

Run: pytest execution/daemons/apis/test_action_type_vocabulary.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
from lib.daemon_runtime.gating import (  # noqa: E402
    NEVER_AUTO_EXECUTE_ACTION_TYPES,
    ExecutionPolicy,
)


def _classified_by(policy: ExecutionPolicy) -> frozenset[str]:
    """Every action type this policy can classify without falling through.

    Read off the policy object rather than restated here: a literal copy of the
    sets would be a THIRD vocabulary, free to drift from both of the two this
    test exists to hold together.
    """
    return frozenset(
        policy.low_blast_action_types
        | policy.high_blast_action_types
        | NEVER_AUTO_EXECUTE_ACTION_TYPES
    )


@pytest.fixture
def fallback_policy() -> ExecutionPolicy:
    """The offline policy, used deliberately instead of fetching the live one.

    A test that reached Neotoma would fail on a network blip and pass on a
    stale cache — it would report the transport, not the invariant. The
    fallback sets are the code's own copy of the policy entity, so holding the
    agent map against them is the comparison that can actually be made
    hermetically. Drift between the fallback and the live entity is a separate
    concern, owned by ateles#659.
    """
    return ExecutionPolicy(entity_id="fallback", loaded=False)


# ── The invariant the comment asserted ───────────────────────────────────────


def test_every_agent_action_type_is_classified(fallback_policy):
    """The mechanical form of the comment above `_AGENT_ACTION_TYPE`.

    RED when someone adds an agent mapping to a value no policy classifies:

        AssertionError: _AGENT_ACTION_TYPE values no policy classifies:
        ['deploy'] — the gate cannot classify these, so every task routed to
        that agent is held as never-auto-executable. Add each to the policy's
        low_blast_action_types or high_blast_action_types.
    """
    declared = {v.strip().lower() for v in apis._AGENT_ACTION_TYPE.values()}
    unclassified = sorted(declared - _classified_by(fallback_policy))
    assert not unclassified, (
        f"_AGENT_ACTION_TYPE values no policy classifies: {unclassified} — "
        "the gate cannot classify these, so every task routed to that agent is "
        "held as never-auto-executable. Add each to the policy's "
        "low_blast_action_types or high_blast_action_types."
    )


@pytest.mark.parametrize("agent,action_type", sorted(apis._AGENT_ACTION_TYPE.items()))
def test_each_agent_mapping_resolves_to_a_real_verdict(
    agent, action_type, fallback_policy
):
    """Per-agent, so a failure names WHICH agent is unroutable, not just that
    one is. A list of five values in one assertion sends you back to the source
    to find out which agent it belongs to."""
    verdict = fallback_policy.blast_radius_for(action_type)
    assert action_type.strip().lower() in _classified_by(fallback_policy), (
        f"{agent} -> {action_type!r} resolves to {verdict.value!r} by "
        "fallthrough, not by classification"
    )


# ── The guard must detect drift, not merely describe the present ─────────────


def test_the_check_fails_on_an_unclassified_value(fallback_policy):
    """Proves this file is a gate and not a tautology.

    Without this, every assertion above would pass just as happily against a
    check that could never fail — the "test that cannot fail on the thing it
    watches is decoration" case. Here the drift is simulated explicitly and the
    check MUST catch it.
    """
    drifted = {**apis._AGENT_ACTION_TYPE, "some_new_agent": "deploy_to_prod"}
    declared = {v.strip().lower() for v in drifted.values()}
    unclassified = declared - _classified_by(fallback_policy)
    assert unclassified == {"deploy_to_prod"}, (
        "the reconciliation check cannot detect an unclassified value — it "
        "would pass against any vocabulary at all"
    )


def test_an_unclassified_value_is_held_not_released(fallback_policy):
    """The drift's consequence, pinned (ateles#715).

    A value nobody classified must resolve NEVER — held for a human — rather
    than to the policy default. If this ever regresses to LOW, the drift stops
    being a nuisance and becomes the original fail-open.
    """
    from lib.daemon_runtime.gating import BlastRadius

    assert fallback_policy.blast_radius_for("deploy_to_prod") == BlastRadius.NEVER


def test_absent_action_type_still_takes_the_policy_default(fallback_policy):
    """"Nothing was declared" and "something was declared that nobody
    classified" are different states and must stay distinguishable. Collapsing
    them would make the gate hold every task that simply has no action_type."""
    assert (
        fallback_policy.blast_radius_for(None) == fallback_policy.blast_radius_default
    )
    assert fallback_policy.blast_radius_for("") == fallback_policy.blast_radius_default


# ── The sets themselves ──────────────────────────────────────────────────────


def test_high_and_low_blast_sets_are_disjoint(fallback_policy):
    """A value in both sets classifies by whichever branch runs first, which
    makes blast radius depend on statement order rather than on the value."""
    overlap = fallback_policy.low_blast_action_types & fallback_policy.high_blast_action_types
    assert not overlap, f"classified as both low and high blast: {sorted(overlap)}"


def test_never_tier_is_not_demotable_by_a_policy_set(fallback_policy):
    """`operator_only` must not be classifiable as low blast by any policy.

    It is not a tuning knob — it is the marking that says an agent structurally
    cannot do the work.
    """
    for value in NEVER_AUTO_EXECUTE_ACTION_TYPES:
        assert value not in fallback_policy.low_blast_action_types
