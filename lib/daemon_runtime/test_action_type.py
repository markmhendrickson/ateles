"""Tests for lib.daemon_runtime.action_type (ateles#682).

The properties that matter, in order:

  1. Every value the module can emit is in the policy's vocabulary — never an
     alias, never `operator_only`.
  2. A declared value is passed through UNCHANGED. Nothing here discards,
     aliases or normalizes it.
  3. Task text can only ESCALATE the prior, only where that is strictly
     stricter under the EFFECTIVE policy (the caller's `verdict`), and never from
     a never-auto-executable prior. Text never lowers anything.
  4. With no high-blast phrase, resolution is exactly the per-agent prior.
"""

from __future__ import annotations

import pytest

from lib.daemon_runtime import action_type as at
from lib.daemon_runtime import gating
from lib.daemon_runtime.action_type import (
    AGENT_ACTION_TYPE_PRIOR,
    KNOWN_ACTION_TYPES,
    ResolvedActionType,
    infer_action_type_from_text,
    normalize_action_type,
    resolve_gate_action_type,
)


def _verdict_for(policy):
    return lambda action: policy.blast_radius_for(action).value


FALLBACK = gating.ExecutionPolicy(entity_id="fallback", loaded=False)
VERDICT = _verdict_for(FALLBACK)


@pytest.mark.parametrize("text", ["Transfer €", "Transfer € now", "Transfer €50", "Transfer\u00a0€\u200b"])
def test_symbol_ending_transfer_phrase_escalates(text):
    result = resolve_gate_action_type("fringilla", {"title": text}, verdict=VERDICT)
    assert (result.value, result.source) == ("payment", "content")


@pytest.mark.parametrize("first,last", [("payment", "publish"), ("publish", "payment")])
@pytest.mark.parametrize("strictness", ["high", "never"])
def test_every_recognized_candidate_is_compared_under_effective_policy(first, last, strictness):
    policy = gating.ExecutionPolicy(
        entity_id="candidate-order", loaded=True,
        low_blast_action_types=frozenset({"compute_only_analysis", first}),
        high_blast_action_types=frozenset({last} if strictness == "high" else ()),
    )
    result = resolve_gate_action_type(
        "fringilla", {"title": "Transfer money then publish the page"},
        verdict=_verdict_for(policy),
    )
    assert (result.value, result.source) == (last, "content")
    assert policy.blast_radius_for(result.value).value == strictness


def test_equal_candidate_rank_keeps_existing_pattern_order():
    assert resolve_gate_action_type(
        "fringilla", {"title": "Publish the page then transfer money"}, verdict=VERDICT,
    ).value == "payment"


def _r(skill, **snap):
    return resolve_gate_action_type(skill, snap, verdict=VERDICT)


# ── Vocabulary ───────────────────────────────────────────────────────────────


def test_vocabulary_mirrors_gating():
    """The stdlib-only mirror must not drift from the enforcing module."""
    assert at.LOW_BLAST_ACTION_TYPES == gating._FALLBACK_LOW_BLAST
    assert at.HIGH_BLAST_ACTION_TYPES | at.NEVER_ACTION_TYPES == gating._FALLBACK_HIGH_BLAST
    assert at.NEVER_ACTION_TYPES == gating.NEVER_AUTO_EXECUTE_ACTION_TYPES


def test_every_inferable_value_is_classified_by_the_policy():
    emitted = {action for _, action in at._HIGH_PATTERNS}
    assert emitted
    for value in emitted:
        assert FALLBACK.blast_radius_for(value) == gating.BlastRadius.HIGH, value
    aliases = {"open_pr", "merge_pr", "transfer", "wage", "invoice_pay", "release"}
    assert not emitted & aliases
    assert "operator_only" not in emitted
    assert emitted <= KNOWN_ACTION_TYPES


def test_the_module_cannot_emit_a_low_value_or_lower_anything():
    """There is no LOW inference: nothing in the module names a report-only type."""
    for name in ("REPORT_ONLY_ACTION_TYPE", "LOWERABLE_PRIOR_AGENTS", "_REPORT_ONLY"):
        assert not hasattr(at, name), name


def test_prior_map_is_the_one_apis_uses():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "execution/daemons/apis"))
    import apis

    assert apis._AGENT_ACTION_TYPE is AGENT_ACTION_TYPE_PRIOR


def test_prior_map_contents_are_unchanged_from_before_the_split():
    assert AGENT_ACTION_TYPE_PRIOR == {
        "cicada": "open_or_merge_pr",
        "vanellus": "open_or_merge_pr",
        "struthio": "publish",
        "monedula": "payment",
        "fringilla": "compute_only_analysis",
        "corvus": "send_external_comms",
    }


def test_normalize_preserves_every_declared_value_it_is_given():
    for value in ("operator_only", "open_pull_request", "release", "weird", "payment"):
        assert normalize_action_type(value) == value
        assert normalize_action_type(f"  {value.upper()} ") == value
    for blank in (None, "", "   ", False):
        assert normalize_action_type(blank) is None


def test_known_vocabulary_contains_the_never_set():
    assert gating.NEVER_AUTO_EXECUTE_ACTION_TYPES <= KNOWN_ACTION_TYPES


# ── Text inference: high-blast phrases only ──────────────────────────────────


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Open a PR that adds the check", "open_or_merge_pr"),
        ("Migrate config. PR #643 open.", "open_or_merge_pr"),
        ("Pay the invoice for the rent", "payment"),
        ("Send the email to the client about the delay", "send_external_comms"),
        ("Publish the post on the site", "publish"),
        ("Permanently delete the orphaned records", "delete_entity_or_data"),
        ("Close the GitHub issue once merged", "external_api_write"),
    ],
)
def test_high_blast_signals(text, expected):
    assert infer_action_type_from_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Review the roadmap",
        "Write NOTHING - the only output is a divergence report.",
        "PURE REPORTING. No behaviour change, nothing gated on the result.",
        "Make the endpoint read-only",
        "Analyse the logs",
    ],
)
def test_everything_else_is_none_report_only_language_included(text):
    assert infer_action_type_from_text(text) is None


def test_every_text_field_is_read_not_just_the_first_non_empty_one():
    assert infer_action_type_from_text("Quarterly review", "all fine", "then open a PR") == "open_or_merge_pr"
    assert infer_action_type_from_text("Quarterly review", "open a PR", "all fine") == "open_or_merge_pr"
    assert infer_action_type_from_text("open a PR", None, None) == "open_or_merge_pr"


@pytest.mark.parametrize(
    "text",
    [
        "open a PR",  # no-break spaces
        "open a PR",  # em / thin space
        "open​ a PR",  # zero-width space
        "open\n\ta   PR",
        "OPEN A PR",
        "open a pr #12",
    ],
)
def test_spacing_and_unicode_variants_of_a_high_blast_phrase_are_still_read(text):
    assert infer_action_type_from_text(text) == "open_or_merge_pr"


def test_a_late_signal_in_a_long_body_is_not_missed():
    body = ("filler line\n" * 5000) + "and then merge the PR"
    assert infer_action_type_from_text("Quarterly review", body) == "open_or_merge_pr"


def test_non_string_inputs_do_not_raise():
    assert infer_action_type_from_text(None, None) is None
    assert infer_action_type_from_text(12, ["x"]) is None


# ── Resolution ───────────────────────────────────────────────────────────────


def test_declared_is_passed_through_unchanged_whatever_the_text_says():
    r = _r("cicada", action_type=" Local_Edit ", title="Open a PR and pay the invoice")
    assert r == ResolvedActionType("local_edit", "declared")


@pytest.mark.parametrize("spelling", ["open_pull_request", "release", "open_pr", "operator_only", "weird"])
def test_declared_unrecognized_or_aliased_values_reach_the_gate_verbatim(spelling):
    r = _r("cicada", action_type=spelling, title="Write nothing, pure reporting")
    assert r.value == spelling
    assert r.source == "declared"


def test_blank_declared_value_is_not_a_declaration():
    assert _r("cicada", action_type="   ") == ResolvedActionType("open_or_merge_pr", "agent_prior")


def test_no_signal_resolves_exactly_as_the_prior_does_today():
    for skill, prior in AGENT_ACTION_TYPE_PRIOR.items():
        assert _r(skill, title="Plan the offsite") == ResolvedActionType(prior, "agent_prior")
    assert _r(None, title="Plan the offsite") == ResolvedActionType(None, "none")
    assert _r("apis", title="Plan the offsite") == ResolvedActionType(None, "none")


@pytest.mark.parametrize("skill", sorted(AGENT_ACTION_TYPE_PRIOR) + [None, "apis"])
def test_report_only_text_never_changes_any_agents_prior(skill):
    """The lowering this change used to make is gone: ambiguity keeps the prior."""
    expected = AGENT_ACTION_TYPE_PRIOR.get(skill)
    for text in (
        "Write NOTHING - this job's only output is a divergence report.",
        "PURE REPORTING. No behaviour change, nothing gated on the result.",
        "Do not write nothing; implement the change",
        "write nothing",
    ):
        r = _r(skill, title="Add a read-only reconciler", description=text)
        assert r.value == expected, (skill, text)
        assert r.source != "content"


def test_text_escalates_an_absent_or_low_prior():
    assert _r(None, title="Open a PR").value == "open_or_merge_pr"
    r = _r("fringilla", title="Send the email to the client")
    assert (r.value, r.source) == ("send_external_comms", "content")


def test_text_never_changes_a_high_prior():
    assert _r("monedula", title="Publish the post on the site").value == "payment"
    assert _r("struthio", title="Pay the invoice for the rent").value == "publish"


def test_a_secondary_field_can_escalate_when_the_first_is_blank_or_benign():
    assert _r(None, title="x", body="benign", description="open a PR").value == "open_or_merge_pr"


def test_explicit_priors_argument_is_honoured():
    r = resolve_gate_action_type("cicada", {}, {"cicada": "payment"}, verdict=VERDICT)
    assert r.value == "payment"


# ── Policy-aware escalation ──────────────────────────────────────────────────


def _policy(high, low, default=gating.BlastRadius.LOW):
    return gating.ExecutionPolicy(
        entity_id="p",
        high_blast_action_types=frozenset(high),
        low_blast_action_types=frozenset(low),
        blast_radius_default=default,
        loaded=True,
    )


def test_without_a_verdict_there_is_no_escalation():
    r = resolve_gate_action_type(None, {"title": "Open a PR"})
    assert r == ResolvedActionType(None, "none")
    r = resolve_gate_action_type("fringilla", {"title": "Open a PR"})
    assert r.value == "compute_only_analysis"


def test_a_never_prior_is_never_replaced_by_a_high_candidate():
    """The policy does not classify open_or_merge_pr (so Cicada's prior is NEVER
    under it) but does classify payment HIGH. 'Escalating' NEVER -> HIGH would
    make the gate less strict."""
    p = _policy(high={"payment"}, low={"compute_only_analysis"})
    assert p.blast_radius_for("open_or_merge_pr") is gating.BlastRadius.NEVER
    r = resolve_gate_action_type(
        "cicada", {"title": "Pay the invoice for the rent"},
        verdict=_verdict_for(p),
    )
    assert (r.value, r.source) == ("open_or_merge_pr", "agent_prior")


def test_an_absent_prior_under_a_never_default_is_not_replaced_by_high():
    p = _policy(high={"open_or_merge_pr"}, low=set(), default=gating.BlastRadius.NEVER)
    r = resolve_gate_action_type(None, {"title": "Open a PR"}, verdict=_verdict_for(p))
    assert r.value is None


def test_an_absent_prior_under_a_high_default_does_not_change_label_for_an_equal_candidate():
    p = _policy(high={"open_or_merge_pr"}, low=set(), default=gating.BlastRadius.HIGH)
    r = resolve_gate_action_type(None, {"title": "Open a PR"}, verdict=_verdict_for(p))
    assert r.value is None  # same strictness: nothing to escalate


def test_escalation_follows_the_policys_sets_not_the_default_vocabulary():
    # Under this policy compute_only_analysis is HIGH and publish is LOW.
    p = _policy(high={"compute_only_analysis"}, low={"publish"})
    r = resolve_gate_action_type(
        "fringilla", {"title": "Publish the post on the site"}, verdict=_verdict_for(p)
    )
    # Fringilla's prior is HIGH here and the candidate LOW: no change.
    assert (r.value, r.source) == ("compute_only_analysis", "agent_prior")


def test_an_unclassified_candidate_is_stricter_and_may_replace_a_low_prior():
    p = _policy(high={"payment"}, low={"compute_only_analysis"})
    assert p.blast_radius_for("publish") is gating.BlastRadius.NEVER
    r = resolve_gate_action_type(
        "fringilla", {"title": "Publish the post on the site"}, verdict=_verdict_for(p)
    )
    assert (r.value, r.source) == ("publish", "content")
