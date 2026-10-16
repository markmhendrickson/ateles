"""Tests for lib.daemon_runtime.action_type (ateles#682).

The properties that matter, in order:

  1. Every value the module can emit is in the policy's vocabulary — never an
     alias, never `operator_only`, never a spelling the gate would classify as
     never-auto-executable (ateles#715).
  2. A declared value is passed through UNCHANGED. Nothing here discards,
     aliases or normalizes it.
  3. Text may only escalate an absent or LOW prior, or lower the generalist's
     HIGH prior on an explicit report-only statement. Nothing else moves.
  4. With no signal, resolution is exactly the per-agent prior.
"""

from __future__ import annotations

import pytest

from lib.daemon_runtime import action_type as at
from lib.daemon_runtime import gating
from lib.daemon_runtime.action_type import (
    AGENT_ACTION_TYPE_PRIOR,
    KNOWN_ACTION_TYPES,
    LOWERABLE_PRIOR_AGENTS,
    ResolvedActionType,
    infer_action_type_from_text,
    normalize_action_type,
    resolve_gate_action_type,
    tier_of,
)

# ── Vocabulary ───────────────────────────────────────────────────────────────


def test_vocabulary_mirrors_gating():
    """The stdlib-only mirror must not drift from the enforcing module."""
    assert at.LOW_BLAST_ACTION_TYPES == gating._FALLBACK_LOW_BLAST
    assert at.HIGH_BLAST_ACTION_TYPES | at.NEVER_ACTION_TYPES == gating._FALLBACK_HIGH_BLAST
    assert at.NEVER_ACTION_TYPES == gating.NEVER_AUTO_EXECUTE_ACTION_TYPES


def test_tier_of_agrees_with_the_enforcing_gate():
    policy = gating.ExecutionPolicy(entity_id="fallback", loaded=False)
    for value in sorted(
        gating._FALLBACK_LOW_BLAST
        | gating._FALLBACK_HIGH_BLAST
        | {"open_pull_request", "totally_unknown"}
    ):
        assert tier_of(value) == policy.blast_radius_for(value).value, value
    assert tier_of(None) is None
    assert tier_of("") is None


def test_every_inferable_value_is_classified_by_the_policy():
    """Walk every pattern's emitted value; none may fall through to NEVER."""
    policy = gating.ExecutionPolicy(entity_id="fallback", loaded=False)
    emitted = {action for _, action in at._HIGH_PATTERNS} | {at.REPORT_ONLY_ACTION_TYPE}
    assert emitted
    for value in emitted:
        assert policy.blast_radius_for(value) != gating.BlastRadius.NEVER, value
    # Canonical spellings only: no aliases, no operator_only.
    aliases = {"open_pr", "merge_pr", "transfer", "wage", "invoice_pay", "release"}
    assert not emitted & aliases
    assert "operator_only" not in emitted


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


# ── Text inference ───────────────────────────────────────────────────────────


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
    "title, body",
    [
        ("Add a read-only reconciler", "Write NOTHING - this job's only output is a divergence report."),
        ("Extend the drift check", "PURE REPORTING. No behaviour change, nothing gated on the result."),
        ("Audit the queue", "Reporting only."),
        ("Audit", "The only output is a written report."),
    ],
)
def test_report_only_statements(title, body):
    assert infer_action_type_from_text(title, body) == "compute_only_analysis"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "Review the roadmap",
        "Make the endpoint read-only",  # a constraint on a change, not a report
        "Do not modify the schema while you fix the importer",
        "Implement the exporter. Write nothing to production.",  # implementation verb
        "Fix the parser; the only output is a report of what broke",
        "Analyse the logs",  # absence of a verb is not a licence
    ],
)
def test_no_decisive_signal_is_none(text):
    assert infer_action_type_from_text(text) is None


def test_high_beats_report_only():
    text = "Write nothing locally; analyse the drift and open a PR with the report."
    assert infer_action_type_from_text(text) == "open_or_merge_pr"


def test_a_late_signal_in_a_long_body_is_not_missed():
    body = ("filler line\n" * 5000) + "and then merge the PR"
    assert infer_action_type_from_text("Quarterly review", body) == "open_or_merge_pr"


def test_non_string_inputs_do_not_raise():
    assert infer_action_type_from_text(None, None) is None
    assert infer_action_type_from_text(12, ["x"]) is None


# ── Resolution ───────────────────────────────────────────────────────────────


def _r(skill, **snap):
    return resolve_gate_action_type(skill, snap)


def test_declared_is_passed_through_unchanged_whatever_the_text_says():
    r = _r("cicada", action_type=" Local_Edit ", title="Open a PR and pay the invoice")
    assert r == ResolvedActionType("local_edit", "declared")


@pytest.mark.parametrize("spelling", ["open_pull_request", "release", "open_pr", "operator_only", "weird"])
def test_declared_unrecognized_or_aliased_values_reach_the_gate_verbatim(spelling):
    r = _r("cicada", action_type=spelling, title="Write nothing, pure reporting")
    assert r.value == spelling
    assert r.source == "declared"


def test_blank_declared_value_is_not_a_declaration():
    r = _r("cicada", action_type="   ")
    assert r == ResolvedActionType("open_or_merge_pr", "agent_prior")


def test_no_signal_resolves_exactly_as_the_prior_does_today():
    for skill, prior in AGENT_ACTION_TYPE_PRIOR.items():
        assert _r(skill, title="Plan the offsite") == ResolvedActionType(prior, "agent_prior")
    assert _r(None, title="Plan the offsite") == ResolvedActionType(None, "none")
    assert _r("apis", title="Plan the offsite") == ResolvedActionType(None, "none")


def test_cicada_report_only_is_lowered():
    r = _r("cicada", title="Add a read-only reconciler", description="Write NOTHING - only output is a divergence report.")
    assert r == ResolvedActionType("compute_only_analysis", "content")


def test_only_lowerable_agents_are_lowered():
    assert LOWERABLE_PRIOR_AGENTS == {"cicada"}
    for skill in ("vanellus", "monedula", "struthio", "corvus"):
        r = _r(skill, title="Write nothing, pure reporting")
        assert r.value == AGENT_ACTION_TYPE_PRIOR[skill], skill


def test_cicada_report_only_with_a_pr_reference_stays_high():
    r = _r("cicada", title="Pure reporting on PR #643")
    assert r.value == "open_or_merge_pr"


def test_report_only_text_never_supplies_a_value_without_a_prior():
    """Nothing to lower: a LOW reading would only move a task from the policy
    default to LOW, which is the licence-from-silence step this refuses."""
    assert _r(None, title="Write nothing, pure reporting") == ResolvedActionType(None, "none")
    assert _r("apis", title="Write nothing, pure reporting").value is None


def test_text_escalates_an_absent_or_low_prior():
    assert _r(None, title="Open a PR").value == "open_or_merge_pr"
    assert _r("fringilla", title="Send the email to the client").value == "send_external_comms"
    assert _r("fringilla", title="Send the email to the client").source == "content"


def test_text_never_changes_a_high_prior():
    assert _r("monedula", title="Publish the post on the site").value == "payment"
    assert _r("struthio", title="Pay the invoice for the rent").value == "publish"


def test_description_and_body_are_both_read():
    assert _r(None, title="x", body="open a PR").value == "open_or_merge_pr"
    assert _r(None, title="x", description="open a PR").value == "open_or_merge_pr"


def test_explicit_priors_argument_is_honoured():
    r = resolve_gate_action_type("cicada", {}, {"cicada": "payment"})
    assert r.value == "payment"


def test_prose_from_a_marked_source_never_lowers():
    for source in ("a2a", "gmail", "anything-unvetted"):
        r = _r("cicada", title="Write nothing, pure reporting", source=source)
        assert r == ResolvedActionType("open_or_merge_pr", "agent_prior"), source
    # ...but the same prose can still tighten.
    r = _r("fringilla", title="Send the email to the client", source="a2a")
    assert r.value == "send_external_comms"


def test_normalize_preserves_every_declared_value_it_is_given():
    """The opposite of a discard rule: ateles#715 needs the gate to receive what
    was declared, including what no vocabulary admits."""
    for value in ("operator_only", "open_pull_request", "release", "weird", "payment"):
        assert normalize_action_type(value) == value
        assert normalize_action_type(f"  {value.upper()} ") == value
    for blank in (None, "", "   ", False):
        assert normalize_action_type(blank) is None


def test_known_vocabulary_contains_the_never_set_and_every_inferable_value():
    assert gating.NEVER_AUTO_EXECUTE_ACTION_TYPES <= KNOWN_ACTION_TYPES
    assert {a for _, a in at._HIGH_PATTERNS} | {at.REPORT_ONLY_ACTION_TYPE} <= KNOWN_ACTION_TYPES
