"""Tests for the merge path: ateles#565, #595, #594.

Three defects, one theme — an approved PR has nothing that carries it to merge,
and the controls that should have surfaced that failure are silent:

  #595  a lens verdict token overrides its own [BLOCKING] findings, so a
        blocker is submitted as `--comment` and never reaches reviewDecision.
  #565  under APIS_AUTONOMY_AUTO_MERGE=1 the dispatcher files NO merge
        checkpoint (it delegates to Vanellus), so when the merge is refused —
        by a local tool-permission classifier or anything else — the refusal
        reaches nobody and the PR rots toward CONFLICTING.
  #594  the autonomy clause is emitted without reading the parent issue's
        gate_status, so the dispatch can authorize a merge the gate forbids.

Each test asserts the OBSERVABLE EFFECT (escalation fired / review event
produced / clause downgraded), not that a contract string was accepted —
policy `fixed_means_behavior_verified_not_contract_accepted`
(ent_db0b7855d47012084477fb00).
"""

import swarm_dispatch
from lib.notify import Priority
from swarm_dispatch import (
    MERGE_REFUSED_MARKER,
    body_has_blocking_findings,
    merge_authorization_clause,
    parse_merge_refusal,
    verdict_to_review_event,
)


# ── #595: body-derived blockers dominate a self-reported token ───────────────


def test_blocking_marker_in_body_is_detected():
    """The [BLOCKING] marker is the structured field the review contract already
    defines. Detection must be anchored to the marker, not to loose prose."""
    assert body_has_blocking_findings(
        "**COMMENT**\n\n**[BLOCKING] credential-scope:** loads the entire .env"
    )
    assert body_has_blocking_findings("[BLOCKING] tenant-isolation: no scoping")
    assert not body_has_blocking_findings("**APPROVE**\n\nlgtm, no concerns")
    assert not body_has_blocking_findings("")
    assert not body_has_blocking_findings(None)


def test_non_blocking_marker_is_not_a_blocker():
    """`[NON-BLOCKING]` contains the substring `BLOCKING`. A naive `in` check
    would turn every advisory note into a merge-blocking REQUEST_CHANGES."""
    assert not body_has_blocking_findings(
        "**COMMENT**\n\n[NON-BLOCKING] naming: prefer `fetch_workflow`"
    )
    assert not body_has_blocking_findings(
        "[NON-BLOCKING] a: x\n[NON-BLOCKING] b: y"
    )


def test_comment_token_with_blocking_body_escalates_to_request_changes():
    """ateles#595, the exact ateles#558 shape: the legal lens posted **COMMENT**
    with a [BLOCKING] credential-scope finding. Submitted as --comment, it never
    reached reviewDecision and an aggregator reading tokens counted it clear."""
    body = (
        "**COMMENT**\n\n"
        "**[BLOCKING] credential-scope:** execution/scripts/live_transcript_tail.py"
        ":391-403 loads the *entire* ~/.config/neotoma/.env into the subprocess."
    )
    assert verdict_to_review_event("comment", body=body) == "REQUEST_CHANGES"


def test_approve_token_with_blocking_body_escalates_to_request_changes():
    """The inverse of the same disagreement, and the more dangerous one: an
    APPROVE token carrying a blocker would clear reviewDecision outright."""
    body = "**APPROVE**\n\n[BLOCKING] auth: the token check is bypassable"
    assert verdict_to_review_event("approve", body=body) == "REQUEST_CHANGES"


def test_blocked_token_with_blocking_body_escalates():
    """BLOCKED maps to COMMENT by design (ateles#241). With a real [BLOCKING]
    finding in the body, that de-escalation is exactly the bug."""
    body = "**BLOCKED**\n\n[BLOCKING] scope: out of bounds"
    assert verdict_to_review_event("blocked", body=body) == "REQUEST_CHANGES"


def test_clean_body_preserves_the_original_mapping():
    """The cross-check only ever escalates. With no [BLOCKING] marker every
    ateles#241 mapping must survive byte-for-byte."""
    assert verdict_to_review_event("approve", body="**APPROVE** lgtm") == "APPROVE"
    assert verdict_to_review_event("comment", body="**COMMENT** nit") == "COMMENT"
    assert verdict_to_review_event("blocked", body="**BLOCKED** later") == "COMMENT"
    assert verdict_to_review_event(None, body="garbage") == "COMMENT"


def test_body_is_optional_so_existing_callers_are_unaffected():
    """ateles#241's callers pass only the verdict. The signature must stay
    backward-compatible or the cross-check becomes a breaking change."""
    assert verdict_to_review_event("approve") == "APPROVE"
    assert verdict_to_review_event("request_changes") == "REQUEST_CHANGES"
    assert verdict_to_review_event("comment") == "COMMENT"
    assert verdict_to_review_event("blocked") == "COMMENT"
    assert verdict_to_review_event(None) == "COMMENT"


def test_request_changes_remains_the_only_escalating_token_on_clean_bodies():
    """The ateles#241 asymmetry is a safety property: on a body with no blocking
    findings, exactly one token may produce the merge-blocking event."""
    escalating = [
        v
        for v in ("approve", "request_changes", "comment", "blocked", None, "", "wat")
        if verdict_to_review_event(v, body="**X** no findings") == "REQUEST_CHANGES"
    ]
    assert escalating == ["request_changes"], escalating


# ── #594: the autonomy clause must not contradict gate state ────────────────


def test_autonomy_clause_authorizes_when_flag_on_and_gates_clear():
    clause = merge_authorization_clause(auto_merge=True, pending_gates=set())
    assert "YOU MAY MERGE" in clause


def test_autonomy_clause_downgrades_when_a_pre_impl_gate_is_pending():
    """ateles#594 gap 3: the #592 dispatch asserted `YOU MAY MERGE` on a PR
    whose parent #585 had pm: pending and impl: pending. The guard held only
    because the steward re-derived the gate itself."""
    clause = merge_authorization_clause(
        auto_merge=True, pending_gates={"pm", "impl"}
    )
    assert "YOU MAY MERGE" not in clause
    assert "DO NOT MERGE" in clause
    # The steward must be told WHICH gate withheld authorization, or the
    # downgrade is just another silent refusal.
    assert "pm" in clause and "impl" in clause


def test_autonomy_clause_still_guards_when_flag_off():
    clause = merge_authorization_clause(auto_merge=False, pending_gates=set())
    assert "DO NOT MERGE" in clause


def test_pending_gates_cannot_authorize_a_merge_the_flag_forbids():
    """Clear gates never UPgrade a flag-off posture into authorization."""
    clause = merge_authorization_clause(auto_merge=False, pending_gates=set())
    assert "YOU MAY MERGE" not in clause


# ── #565: a refused merge escalates instead of vanishing ────────────────────


class _Recorder:
    def __init__(self):
        self.sent = []

    def send(self, message, priority=None, handler=None, **kw):
        self.sent.append((message, priority))


def _dispatcher_with_notifier(notifier):
    d = swarm_dispatch.SwarmDispatcher.__new__(swarm_dispatch.SwarmDispatcher)
    d.notifier = notifier
    return d


def test_merge_refusal_escalates_as_a_blocker():
    """ateles#565: PR #452 was APPROVED, CLEAN and mergeable. The merge was
    attempted, a local permission classifier refused it, and the refusal was
    recorded only in a review body — so it reached nobody. It must page."""
    notifier = _Recorder()
    d = _dispatcher_with_notifier(notifier)

    state = d.record_merge_refusal(
        repository="markmhendrickson/ateles",
        number=452,
        reason="blocked by the local permission classifier",
        auto_merge=True,
    )

    assert notifier.sent, "a refused merge must escalate, not vanish"
    message, priority = notifier.sent[0]
    assert priority is Priority.BLOCKER
    assert "452" in message
    assert "permission classifier" in message
    # The operator is the only party who can act on it — say so.
    assert "merge" in message.lower()
    assert state == "authorized_but_unable"


def test_authorized_but_unable_is_distinct_from_not_authorized():
    """ateles#565 AC: flag-on + tool-gate denial is a DIFFERENT state from the
    flag being off, and today they are indistinguishable from the outside."""
    notifier = _Recorder()
    d = _dispatcher_with_notifier(notifier)

    unable = d.record_merge_refusal(
        repository="r", number=1, reason="tool gate denied", auto_merge=True
    )
    not_authorized = d.record_merge_refusal(
        repository="r", number=2, reason="tool gate denied", auto_merge=False
    )

    assert unable == "authorized_but_unable"
    assert not_authorized == "not_authorized"
    assert unable != not_authorized


def test_not_authorized_refusal_does_not_page_as_a_blocker():
    """Flag-off is the designed posture, not a failure: the operator merges by
    hand and a checkpoint_brief already carries that. Paging BLOCKER on every
    one would train the operator to ignore the channel that matters."""
    notifier = _Recorder()
    d = _dispatcher_with_notifier(notifier)

    d.record_merge_refusal(
        repository="r", number=3, reason="operator-gated", auto_merge=False
    )

    assert not any(p is Priority.BLOCKER for _, p in notifier.sent)


def test_merge_refusal_is_parsed_from_the_steward_reply():
    """The refusal must survive the trip out of the agent as a parseable signal.
    On #452 it existed only as prose in a review body — the exact silence."""
    stdout = (
        "**APPROVE**\n\nAll lenses clear, Blocking: 0.\n"
        f"{MERGE_REFUSED_MARKER} blocked by the local permission classifier\n"
    )
    assert (
        parse_merge_refusal(stdout)
        == "blocked by the local permission classifier"
    )


def test_no_refusal_marker_parses_to_none():
    """A normal aggregation must not be read as a refusal — a false page here
    costs the operator's attention on the channel reserved for real blockers."""
    assert parse_merge_refusal("**APPROVE**\n\nMerged via gh pr merge.") is None
    assert parse_merge_refusal("") is None
    assert parse_merge_refusal(None) is None


def test_refusal_reason_is_bounded_to_one_line():
    """A reason is a sentence. Unbounded, the escalation would page the operator
    with the entire aggregation body."""
    stdout = f"{MERGE_REFUSED_MARKER} tool gate denied\nBlocking: 0\nmore prose"
    assert parse_merge_refusal(stdout) == "tool gate denied"


def test_refusal_marker_with_empty_reason_still_escalates():
    """A refusal with no stated reason is still a refusal. Returning None would
    reintroduce the silence for the sloppiest case."""
    assert parse_merge_refusal(f"{MERGE_REFUSED_MARKER}\n") == "no reason given"


def test_authorized_clause_tells_the_steward_to_report_a_refusal():
    """The escalation path only works if the steward is told to emit the marker
    instead of burying the refusal in its review body (ateles#565)."""
    clause = merge_authorization_clause(auto_merge=True, pending_gates=set())
    assert MERGE_REFUSED_MARKER in clause


def test_authorized_clause_carries_the_stacked_pr_precondition():
    """ateles#594 gap 1: a stacked PR reads CLEAN/MERGEABLE *because* its base is
    a feature branch, so the surface signals invert and it looks safer than a
    main-targeted PR. The clause must name the base-branch precondition."""
    clause = merge_authorization_clause(auto_merge=True, pending_gates=set())
    assert "DEFAULT branch" in clause
    assert "stacked" in clause.lower()


def test_merge_refusal_escalation_never_raises():
    """Fail-open, like every sweep around it: a broken notifier must not turn a
    merge refusal into a crashed dispatch."""
    class _Broken:
        def send(self, *a, **k):
            raise RuntimeError("notifier down")

    d = _dispatcher_with_notifier(_Broken())
    assert (
        d.record_merge_refusal(
            repository="r", number=4, reason="denied", auto_merge=True
        )
        == "authorized_but_unable"
    )
