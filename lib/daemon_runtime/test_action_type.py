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


def test_declared_low_is_passed_through_unchanged_when_the_text_is_benign():
    r = _r("cicada", action_type=" Local_Edit ", title="Tidy the notes index")
    assert r == ResolvedActionType("local_edit", "declared")


def test_declared_without_a_verdict_is_never_touched_even_if_the_text_is_outward():
    r = resolve_gate_action_type(
        "cicada", {"action_type": "local_edit", "title": "Open a PR and pay the invoice"}
    )
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


# ── A low DECLARED type no longer shields an outward task ────────────────────
# The gate used to trust a declared action_type untouched, so once the policy
# listed most work types as LOW, a task declared low but titled "Send Alex the
# reply" ran unattended. The text is now read for declared types too.

# A policy in which "docs" is a LOW work type, as in the operator's live policy.
_DOCS_POLICY = _policy(high=set(gating._FALLBACK_HIGH_BLAST), low={"docs", "local_edit", "draft"})
_DOCS_VERDICT = _verdict_for(_DOCS_POLICY)


def _declared_docs(title, **more):
    return resolve_gate_action_type(
        None, {"action_type": "docs", "title": title, **more}, verdict=_DOCS_VERDICT
    )


@pytest.mark.parametrize(
    "title, expected_type, verb",
    [
        ("Send Alex the reply", "send_external_comms", "send"),
        ("Push and deploy the four corrections", "git_push", "push"),
        ("Finish and submit the tax questionnaire", "external_api_write", "submit"),
    ],
)
def test_the_three_queued_titles_are_escalated_despite_a_low_declared_type(title, expected_type, verb):
    r = _declared_docs(title)
    assert (r.value, r.source) == (expected_type, "declared_escalated")
    assert _DOCS_POLICY.blast_radius_for(r.value) is gating.BlastRadius.HIGH
    assert r.note == f"declared docs, but the task text names an outward action ({verb})"


@pytest.mark.parametrize(
    "title, verb",
    [
        ("Draft it, then send it to the client", "send"),
        ("Fix the importer and push", "push"),
        ("Please reply to the landlord", "reply"),
        ("Sending the report to Alex", "send"),
        ("Merge main into the release branch", "merge"),
        ("Pay the instructor", "pay the instructor"),
        ("Wire the money to Alex", "wire"),
        ("Tweet the launch note", "tweet"),
        ("Delete the stale user records", "delete"),
        ("File the quarterly tax return", "file"),
        ("DM Alex about Friday", "dm"),
        ("Email Alex the invoice", "email"),
    ],
)
def test_other_imperative_outward_phrasings_are_escalated(title, verb):
    r = _declared_docs(title)
    assert r.source == "declared_escalated", r
    assert r.note.endswith(f"({verb})"), r


def test_outward_verb_in_the_body_or_a_later_line_is_read_too():
    r = _declared_docs("Weekly cleanup", description="Tidy the folder.\nThen send it to the client.")
    assert r.source == "declared_escalated"
    r = _declared_docs("Weekly cleanup", body="- [ ] tidy\n- [ ] deploy to staging")
    assert (r.value, r.source) == ("release", "declared_escalated")


@pytest.mark.parametrize("declared", ["open_or_merge_pr", "payment", "send_external_comms"])
def test_a_declared_high_type_is_never_lowered_or_changed(declared):
    r = resolve_gate_action_type(
        None, {"action_type": declared, "title": "Tidy the notes index"}, verdict=_DOCS_VERDICT
    )
    assert r == ResolvedActionType(declared, "declared")
    # Even when the text names a different (also high) outward action.
    r = resolve_gate_action_type(
        None, {"action_type": declared, "title": "Push the branch"}, verdict=_DOCS_VERDICT
    )
    assert r == ResolvedActionType(declared, "declared")


@pytest.mark.parametrize("declared", ["operator_only", "open_pull_request", "weird"])
def test_a_declared_never_or_unrecognized_type_is_never_rewritten_by_text(declared):
    r = resolve_gate_action_type(
        None, {"action_type": declared, "title": "Send Alex the reply"}, verdict=_DOCS_VERDICT
    )
    assert r == ResolvedActionType(declared, "declared")


@pytest.mark.parametrize(
    "title",
    [
        "Refactor the send-gate tests",
        "Write deployment docs",
        "Review PR feedback",
        "Handle pushback from reviewers",
        "Review the post-merge cleanup",
        "Draft the reply for Alex",
        "Refactor the send gate",
        "Document how to deploy",
        "Update the release notes template",
        "Fix the email parser",
        "Message queue refactor",
        "Resolve the merge conflict in the lockfile",
        "Wire up the hook",
        "Delete unused imports",
        "Pay attention to the flaky test",
        "Do not push or deploy; just prepare the change",
        "Never send anything, only draft it",
        "Tidy the notes index. It was sent last week and deployed on Monday.",
    ],
)
def test_neutral_low_tasks_are_not_escalated(title):
    r = _declared_docs(title)
    assert r == ResolvedActionType("docs", "declared"), r


def test_escalation_uses_the_strictest_candidate_under_the_policy():
    r = _declared_docs("Send Alex the reply and then pay the invoice for rent")
    assert r.source == "declared_escalated"
    assert _DOCS_POLICY.blast_radius_for(r.value) is gating.BlastRadius.HIGH


def test_undeclared_tasks_also_read_the_imperative_verbs_and_say_why():
    r = resolve_gate_action_type("fringilla", {"title": "Send Alex the reply"}, verdict=VERDICT)
    assert (r.value, r.source) == ("send_external_comms", "content")
    assert r.note == "the task text names an outward action (send)"


@pytest.mark.parametrize("title", ["Wire $500 to Alex", "Wire 200 euros to Alex", "Wire the money to Alex", "Wire 1,500.50 to Alex"])
def test_wiring_an_amount_escalates_to_payment(title):
    r = _declared_docs(title)
    assert (r.value, r.source) == ("payment", "declared_escalated"), r
    assert r.note.endswith("(wire)")


@pytest.mark.parametrize("title", ["Wire up the hook", "Wire the hook into the router", "Wire up 3 handlers"])
def test_wiring_code_is_not_a_payment(title):
    assert _declared_docs(title) == ResolvedActionType("docs", "declared")


# One phrasing per verb row, written so that no phrase pattern and no other row
# also covers it: the row is then the only thing that can escalate the title.
_ROW_PHRASINGS = {
    "send": ("Send Alex the reply", "send_external_comms"),
    "email": ("Email Alex the notes", "send_external_comms"),
    "message": ("Message Alex about Friday", "send_external_comms"),
    "dm": ("DM Alex about Friday", "send_external_comms"),
    "reply": ("Reply to Alex", "send_external_comms"),
    "respond": ("Respond to Alex", "send_external_comms"),
    "notify": ("Notify Alex", "send_external_comms"),
    "tweet": ("Tweet the launch note", "publish"),
    "post": ("Post the update", "publish"),
    "publish": ("Publish changelog", "publish"),
    "push": ("Push the branch", "git_push"),
    "deploy": ("Deploy the build", "release"),
    "release": ("Release the build", "release"),
    "ship": ("Ship the fix", "release"),
    "merge": ("Merge the branch", "open_or_merge_pr"),
    "submit": ("Submit the form", "external_api_write"),
    "pay": ("Pay Alex", "payment"),
    "transfer": ("Transfer the balance to Alex", "payment"),
    "invoice": ("Invoice Alex for March", "send_external_comms"),
    "wire": ("Wire $500 to Alex", "payment"),
    "file": ("File the quarterly tax return", "external_api_write"),
    "delete": ("Delete the stale user records", "delete_entity_or_data"),
    "payment": ("Approve the payment to Alex", "payment"),
    "external api write": ("Comment on the issue", "external_api_write"),
}


def test_every_verb_row_has_a_phrasing():
    assert set(_ROW_PHRASINGS) == {label for label, *_ in at._VERBS}


@pytest.mark.parametrize("label", sorted(_ROW_PHRASINGS))
def test_each_verb_row_escalates_its_phrasing(label):
    title, expected = _ROW_PHRASINGS[label]
    r = _declared_docs(title)
    assert (r.value, r.source) == (expected, "declared_escalated"), r
    assert r.note.endswith(f"({label})"), r


@pytest.mark.parametrize("label", sorted(_ROW_PHRASINGS))
def test_each_verb_row_is_load_bearing(monkeypatch, label):
    """Disable one row and its phrasing must stop escalating, so a row that was
    silently dropped or broken turns exactly this test red."""
    title, _ = _ROW_PHRASINGS[label]
    monkeypatch.setattr(
        at, "_VERB_PATTERNS", [row for row in at._VERB_PATTERNS if row[0] != label]
    )
    assert _declared_docs(title) == ResolvedActionType("docs", "declared")


def test_a_negation_covers_every_verb_joined_by_and():
    title = "Do not push and deploy the build"
    assert _declared_docs(title) == ResolvedActionType("docs", "declared")
    assert _declared_docs("Never send and email Alex") == ResolvedActionType("docs", "declared")


def test_the_negation_skip_is_what_holds_the_and_form_at_declared(monkeypatch):
    import re

    # Control: with the skip disabled the same title IS escalated, so the test
    # above can only pass because of the negation handling.
    monkeypatch.setattr(at, "_NEGATED", re.compile(r"(?!)"))
    r = _declared_docs("Do not push and deploy the build")
    assert r.source == "declared_escalated", r


def test_note_never_carries_raw_task_markup():
    r = _declared_docs("Open a PR ```<script>``` now")
    assert r.source == "declared_escalated"
    assert "<" not in r.note and "`" not in r.note
