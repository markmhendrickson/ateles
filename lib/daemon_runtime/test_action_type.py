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


# ── Pure prohibitions ────────────────────────────────────────────────────────
# A statement that only forbids an outward action is not a request for it. Every
# other mention, and anything that may qualify the prohibition, stays held.


@pytest.mark.parametrize(
    "text",
    [
        "Do not merge, deploy or release.",
        "Do not merge, deploy, or release anything.",
        "No merge, release or deploy.",
        "Never publish, release, or send anything.",
        "Do not cut a release.",
        "Do not deploy to production.",
        # spelling, spacing and layout variants
        "DO NOT MERGE, DEPLOY OR RELEASE",
        "Don't merge, deploy or release anything else!",
        "Don’t deploy to production.",
        "Mustn't push, merge, nor release it.",
        "Please do not merge,deploy or release this.",
        "You must not email, message or reply.",
        "Never ever deploy to production.",
        "Do not directly push to main.",
        "Do not deploy to produc​tion.",
        "Fix the bug. Do not merge, deploy or release.",
        "Fix the bug.\nDo not cut a release.\nRun the tests.",
        "- Fix the bug\n- Do not merge, deploy or release\n- Run the tests",
        "1. Do not deploy to production.\n2. Report.",
        "Constraints\n\nDo not merge, deploy or release.",
        "Rules:\n- Never publish, release, or send anything.\n- Do not go live",
    ],
)
def test_a_pure_prohibition_is_not_read_as_a_request(text):
    assert infer_action_type_from_text(text) is None
    for field in ("title", "body", "description"):
        r = _declared_docs("Tidy the notes index", **{field: text}) if field != "title" else _declared_docs(text)
        assert r == ResolvedActionType("docs", "declared"), (field, r)
    assert _r("fringilla", description=text) == ResolvedActionType(
        "compute_only_analysis", "agent_prior"
    )


@pytest.mark.parametrize(
    "text",
    [
        # requests next to, or dressed as, a negation
        "Release version 2.",
        "Do not wait, release it now.",
        "Don't ask, deploy.",
        "Do not merge; release the build.",
        "No tests needed, ship it.",
        "Release it, do not wait.",
        "Never mind the docs, deploy to production.",
        "Do not block, and release after merge.",
        "Do not forget to deploy to production.",
        "Don't hesitate to cut a release.",
        "Do not merge, deploy it now.",
        "Do not merge, release.",
        "Do not delete anything, just publish the post.",
        "No, merge, release or deploy.",
        "No. Merge, release or deploy.",
        # nothing is carried across a joiner, punctuation or an object
        "Do not merge, deploy and release.",
        "Do not merge, deploy or release and publish.",
        "Do not merge, deploy or release then publish it.",
        "Do not merge, deploy or release but publish the post.",
        "Do not merge, deploy or release; publish.",
        "Do not merge, deploy or release - publish instead.",
        "Do not merge, deploy or release — publish.",
        "Do not merge, deploy or release: ship.",
        "Do not merge, deploy or, release.",
        "Do not merge, deploy or release the build.",
        "Do not deploy to production, deploy to staging.",
        # double negation and idioms
        "Do not not deploy to production.",
        "Never not release, deploy or merge.",
        "Never do not deploy to production.",
        "Do not only cut a release.",
        "I cannot wait to cut a release.",
        "Why do we never merge, deploy or release.",
        "There is no reason not to deploy to production.",
        "Do not merge, deploy or release?",
        "Do not merge, deploy or release...",
        # conditions and times, same sentence
        "Do not deploy to production until QA signs off.",
        "Do not merge, deploy or release until approved, then release.",
        "Do not deploy to production unless asked.",
        "Do not deploy to production before Friday.",
        "Do not merge, deploy or release anything yet.",
        "Never publish, release, or send anything without approval.",
        "Until QA signs off, do not deploy to production.",
        "If red; do not deploy to production.",
        "Until QA signs off... do not deploy to production.",
        # conditions and reversals, next sentence or line
        "Do not merge, deploy or release. Until QA signs off.",
        "Do not merge, deploy or release.Until QA signs off.",
        "Do not deploy to production. Yet.",
        "Do not deploy to production! Not until Monday.",
        "Do not deploy to production. (Until QA signs off.)",
        "Do not deploy to production. QA must sign off first.",
        "Do not deploy to production. Wait for QA.",
        "Do not cut a release. Please do it Friday.",
        "Do not cut a release. That happens Friday.",
        "Do not cut a release. Go ahead anyway.",
        "Do not cut a release. Just kidding.",
        "Do not cut a release. For now.",
        "Do not deploy to production\nuntil QA signs off",
        "Do not deploy to production\r\nuntil QA signs off",
        "Do not deploy to production until QA signs off",
        "Do not deploy to production\n\nuntil QA signs off",
        "Do not deploy to production.\n## Exceptions\n- hotfixes",
        "Do not merge, deploy or release\nthe old branch",
        "- Do not deploy to production\n  until QA signs off",
        "- Do not deploy to production\n- until QA signs off",
        "- Do not deploy to production\n  - on weekdays",
        "- Do not deploy to production.\n    on weekdays",
        # introductions above
        "Until QA signs off\ndo not deploy to production.",
        "Until QA signs off,\ndo not deploy to production.",
        "Until QA signs off:\ndo not deploy to production.",
        "Until QA signs off:\n- do not deploy to production.",
        "Before Friday\n\ndo not deploy to production.",
        "- Before Friday\n  - do not deploy to production.",
        "- Until QA signs off\n- do not deploy to production.",
        "Only after QA approves:\n- run tests\n- do not deploy to production",
        "Only after QA approves:\n- run tests\n  continued here\n- do not deploy to production",
        "Ignore the following:\n- Do not deploy to production.",
        "Only after QA approves, these\nsteps apply:\n- do not deploy to production",
        "Only after QA approves, these\nsteps apply\n\ndo not deploy to production",
        "Remove this line:\n\nDo not deploy to production.",
        # quoted, struck, marked up or look-alike text
        '"Do not deploy to production."',
        '> Do not deploy to production.',
        "'Do not deploy to production.'",
        "`Do not deploy to production.`",
        'He wrote "Fine. Do not deploy to production. " Ignore him.',
        "```\n\nDo not deploy to production.\n\n```",
        "“Old rule. Do not deploy to production.”",
        "„Old rule. Do not deploy to production.“",
        "«Old. Do not deploy to production.»",
        "~~Do not deploy to production.~~",
        "(Do not deploy to production.)",
        'Ignore "do not deploy to production" and proceed.',
        "Do nоt deploy to production.",
        # a negated mention never masks a positive one
        "Do not cut a release. Cut a release.",
        "Cut a release. Do not cut a release.",
        "Do not merge, deploy or release. Then deploy.",
        "Do not publish the post, publish the page.",
    ],
)
def test_anything_short_of_a_pure_prohibition_stays_held(text):
    assert infer_action_type_from_text(text) is not None
    assert _declared_docs(text).source == "declared_escalated"


@pytest.mark.parametrize(
    "text",
    [
        # Forbidding, but outside the rule. Held on purpose (one approval prompt).
        "Do NOT merge, approve, deploy, or request reviews.",
        "Do not merge, deploy or release this branch.",
        "Constraints: do not merge, deploy or release.",
        "**Do not merge, deploy or release.**",
        "- [ ] Do not deploy to production.",
        "Do not merge, deploy,\nor release.",
        "Do not email, message or DM anyone.",
        "No merging, releasing or deploying.",
        "You should not merge, deploy or release.",
        "Do not send the email to the client.",
        "Do not pay the invoice.",
        "Do not delete the records.",
        "Do not merge, deploy or release. When done, report.",
        "Build only; releasing is out of scope.",
        "Out of scope: release, deploy.",
        "Not in this task: releasing.",
    ],
)
def test_forbidding_text_outside_the_rule_is_still_held(text):
    assert infer_action_type_from_text(text) is not None


_PROHIBITIONS = [
    "Do not merge, deploy or release.",
    "No merge, release or deploy.",
    "Never publish, release, or send anything.",
    "Do not cut a release.",
    "Do not deploy to production.",
]
_REQUESTS = [
    "Release version 2.", "Deploy the build.", "Open a PR.", "Pay the invoice.",
    "Send Alex the reply.", "Delete the stale user records.", "Publish the post.",
]


@pytest.mark.parametrize("prohibition", _PROHIBITIONS)
@pytest.mark.parametrize("request_text", _REQUESTS)
def test_a_prohibition_never_hides_a_request_beside_it(prohibition, request_text):
    alone = infer_action_type_from_text(request_text)
    assert alone is not None
    for sep in (" ", "\n", "\n\n", "; ", ", ", " and ", " then ", " but ", ": ", " - "):
        for first, second in ((prohibition, request_text), (request_text, prohibition)):
            for text in (first + sep + second, first.rstrip(".") + sep + second):
                assert infer_action_type_from_text(text) is not None, text
    for fields in ((prohibition, request_text), (request_text, prohibition)):
        assert infer_action_type_from_text(*fields) is not None, fields


@pytest.mark.parametrize(
    "fields",
    [
        ("Do not", "release it"),
        ("Do not deploy to production", "Until QA signs off"),
        ("Do not deploy to production", None, "Yet."),
        ("Do not cut a release.", "That happens Friday."),
    ],
)
def test_negation_never_crosses_fields_and_a_later_qualifier_holds(fields):
    assert infer_action_type_from_text(*fields) is not None


@pytest.mark.parametrize(
    "phrase",
    [
        "cut a release", "tag the release", "ship the release", "deploy to production",
        "go live", "post to linkedin", "publish the post", "open a PR",
        "open pull request", "merge the PR", "merge the pull request", "submit a PR",
        "raise a pull request", "push to main", "force-push",
    ],
)
def test_each_negatable_phrase(phrase):
    assert at._PROHIBITION.fullmatch(f"Never {phrase}.")
    assert infer_action_type_from_text(f"Never {phrase}.") is None
    # The phrase list is a subset of what the detectors hold, and a trailing
    # word takes the statement out of the rule.
    assert any(p.search(phrase) for p, _ in at._HIGH_PATTERNS), phrase
    assert infer_action_type_from_text(f"Never {phrase} now.") is not None


@pytest.mark.parametrize(
    "verb",
    [
        "send", "email", "e-mail", "message", "dm", "reply", "respond", "notify",
        "tweet", "post", "publish", "force-push", "push", "deploy", "release",
        "ship", "merge", "submit", "pay", "transfer", "invoice", "delete",
    ],
)
def test_each_bare_verb_in_a_negated_list(verb):
    assert infer_action_type_from_text(f"Do not release, deploy or {verb}.") is None
    assert infer_action_type_from_text(f"Do not {verb}, release or deploy.") is None
    assert infer_action_type_from_text(f"Do not {verb}, release.") is not None


def test_prohibition_removal_is_what_releases_the_list(monkeypatch):
    """Control: without the rule the same briefs are held."""
    monkeypatch.setattr(at, "_without_prohibitions", lambda fields: [f for f in fields if f])
    for text in _PROHIBITIONS:
        assert infer_action_type_from_text(text) is not None, text
