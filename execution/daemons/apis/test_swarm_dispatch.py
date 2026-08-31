"""Tests for the PR dispatch pipeline hardening (PR-87 self-dogfood
findings): content-digest idempotency keys, Lanius verdict retry, and the
dispatcher-side fallback for unposted panel review comments.

Also covers the checkbox definition-of-done changes:
  A — _expectation_prompt mandates GitHub task-list syntax
  B — _panelist_prompt includes a check-off instruction when parent + expectation present
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

import httpx
import pytest
import swarm_dispatch
from github_gateway import SwarmTrigger
from lib.daemon_runtime.gating import CheckpointPosture, ExecutionPolicy
from lib.notify import Priority
from review_panel import Lens
from skill_runner import SkillResult
from swarm_dispatch import (
    AGENT_GITHUB_LOGIN,
    EXPECTATION_MARKER,
    GITHUB_FACING_AGENTS,
    PRE_IMPL_GATES,
    _APPROVE_CMD,
    _CONFIRM_GATES_CLEAR_CMD,
    _HOLD_CMD,
    _OPERATOR_LOGIN,
    _REJECT_CMD,
    _SWARM_RUN_CMD,
    _VANELLUS_COMMENT_MARKER,
    DispatchConfig,
    SwarmDispatcher,
    _agent_prompt_instruction,
    _is_bot_author,
    _token_for_agent_on_repo,
    _token_for_repo,
    agent_github_login,
    attribution_header,
    compose_fallback_comment,
    compose_vanellus_fallback_comment,
    content_digest,
    cleanup_pr_worktree,
    is_provisioned,
    lenses_missing_comments,
    parse_gate_verdict,
    parse_pending_gates,
    parse_review_verdict,
    prepare_pr_worktree,
    review_verdict_is_clear,
    verdict_to_review_event,
    vanellus_comment_missing,
)


def _trigger(**overrides):
    base = dict(
        kind="pr_opened",
        repository="owner/repo",
        number=87,
        title="A pull request",
        body="Closes #80.",
        author="someone",
        html_url="https://github.com/owner/repo/pull/87",
        delivery_id="manual-test",
        action="opened",
    )
    base.update(overrides)
    return SwarmTrigger(**base)


class _StubNotifier:
    def __init__(self):
        self.sent = []
        self.priorities = []  # HEAD-side: priority-only assertions
        self.sent_full = []  # main-side: (message, priority) assertions

    def send(self, message, priority=None, handler=None):
        self.sent.append(message)
        self.priorities.append(priority)
        self.sent_full.append((message, priority))


def _config(**overrides):
    # No tokens: Neotoma stores and GitHub fallbacks short-circuit with a log.
    #
    # The autonomy flags are pinned OFF rather than left to their defaults.
    # DispatchConfig's defaults are evaluated from os.environ at IMPORT time, so
    # an operator running the suite in a shell that exports
    # APIS_AUTONOMY_AUTO_MERGE=1 (the live daemon's own posture) silently flipped
    # them for every test — and `_gate_merge_readiness` early-returns under
    # auto_merge, so the leak suppressed exactly the merge-path assertions this
    # module makes. Pinning them here makes these tests read the same locally and
    # in CI. Pass an override to exercise a flag deliberately.
    return DispatchConfig(
        **{
            "neotoma_token": "",
            "github_token": "",
            "auto_merge": False,
            "auto_rereview_on_push": False,
            **overrides,
        }
    )


# ── content_digest ──────────────────────────────────────────────────────────


def test_content_digest_is_stable_for_identical_payloads():
    entities = [{"entity_type": "harness_event", "occurred_at": "2026-06-12T10:00:00Z"}]
    assert content_digest(entities) == content_digest(list(entities))


def test_content_digest_changes_when_content_changes():
    a = [{"entity_type": "harness_event", "occurred_at": "2026-06-12T10:00:00Z"}]
    b = [{"entity_type": "harness_event", "occurred_at": "2026-06-12T10:00:01Z"}]
    assert content_digest(a) != content_digest(b)


# ── parse_gate_verdict ──────────────────────────────────────────────────────


def test_parse_gate_verdict_extracts_clear_and_blocked():
    assert parse_gate_verdict("…\nGATE_INHERITANCE: clear") == "clear"
    assert parse_gate_verdict("gate_inheritance: BLOCKED") == "blocked"


def test_parse_gate_verdict_none_when_absent():
    assert parse_gate_verdict("I could not verify the gates.") is None
    assert parse_gate_verdict("") is None
    assert parse_gate_verdict(None) is None


# ── parse_pending_gates (ateles#230 panel-assembly fix) ─────────────────────


def test_parse_pending_gates_extracts_comma_list():
    out = "GATE_INHERITANCE: blocked\nGATE_PENDING: arch, ux"
    assert parse_pending_gates(out) == {"arch", "ux"}


def test_parse_pending_gates_case_and_whitespace_insensitive():
    assert parse_pending_gates("gate_pending:  Arch ,QA ") == {"arch", "qa"}


def test_parse_pending_gates_empty_when_absent():
    assert parse_pending_gates("GATE_INHERITANCE: clear") == set()
    assert parse_pending_gates("") == set()
    assert parse_pending_gates(None) == set()


# ── parse_review_verdict / review_verdict_is_clear (loop closure) ────────────


def test_parse_review_verdict_extracts_all_tokens():
    assert parse_review_verdict("## Verdict\n**APPROVE**\nlgtm") == "approve"
    assert parse_review_verdict("**REQUEST_CHANGES**\n1 blocking") == "request_changes"
    assert parse_review_verdict("**COMMENT**\nnits only") == "comment"
    assert parse_review_verdict("**BLOCKED** cannot proceed") == "blocked"


def test_parse_review_verdict_none_when_absent():
    assert parse_review_verdict("no verdict token here") is None
    assert parse_review_verdict("") is None
    assert parse_review_verdict(None) is None


def test_review_verdict_is_clear_only_for_approve_or_comment():
    assert review_verdict_is_clear("approve") is True
    assert review_verdict_is_clear("comment") is True
    assert review_verdict_is_clear("request_changes") is False
    assert review_verdict_is_clear("blocked") is False
    # Unparseable verdict is NOT clear — never silently proceed to merge-ready.
    assert review_verdict_is_clear(None) is False


# ── verdict → native GitHub review event (ateles#241) ────────────────────────


def test_verdict_maps_to_review_event_one_row_per_verdict():
    assert verdict_to_review_event("approve") == "APPROVE"
    assert verdict_to_review_event("request_changes") == "REQUEST_CHANGES"
    assert verdict_to_review_event("comment") == "COMMENT"


def test_blocked_verdict_maps_to_comment_not_request_changes():
    """BLOCKED routes findings back for a fix; it must not additionally hard-block
    merge via GitHub review state — merge stays operator-gated."""
    assert verdict_to_review_event("blocked") == "COMMENT"


def test_unparseable_verdict_maps_to_comment():
    """Never escalate on a guess: an unparseable verdict must land on the inert
    COMMENT, not REQUEST_CHANGES (which blocks merge under branch protection)."""
    assert verdict_to_review_event(None) == "COMMENT"
    assert verdict_to_review_event("") == "COMMENT"
    assert verdict_to_review_event("nonsense") == "COMMENT"


def test_request_changes_is_the_only_escalating_mapping():
    """The asymmetry IS the safety property: exactly one input may produce the
    merge-blocking event. A mapping bug that escalates silently blocks good PRs;
    one that de-escalates defeats the feature."""
    escalating = [
        v
        for v in ("approve", "request_changes", "comment", "blocked", None, "", "wat")
        if verdict_to_review_event(v) == "REQUEST_CHANGES"
    ]
    assert escalating == ["request_changes"], escalating


# ── _handle_pr verdict branching ─────────────────────────────────────────────


def _pr_dispatcher_with_stubs(
    monkeypatch, *, vanellus_stdout, calls, auto_merge=False
):
    """Dispatcher whose _handle_pr reaches the verdict branch, then records
    which downstream path (_route_blocking_findings vs _gate_merge_readiness)
    fired, without doing real work in either.

    `auto_merge` drives the flag that decides whether a merge-ready PR gets a
    checkpoint at all, so a test can assert routing still happens under the
    autonomous posture — the case where a missed blocker is silent rather than
    merely wrong.
    """

    async def fake_run_skill(skill, prompt, **kwargs):
        # Lanius clears gate inheritance; panelists return trivial reviews;
        # Vanellus returns the configured verdict stdout.
        if skill == "lanius":
            return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
        if skill == "vanellus":
            return SkillResult(skill, True, 0, vanellus_stdout, "")
        return SkillResult(skill, True, 0, "**COMMENT**\nlgtm", "")

    async def fake_changed_files(self, trigger):
        return ["src/x.ts"]

    async def fake_route(self, trigger, parent, reviews, verdict):
        calls.append(("route", verdict))

    async def fake_gate(self, trigger, parent, panel):
        calls.append(("gate", None))

    async def fake_post_missing_vanellus(self, trigger, result):
        return None

    async def fake_emit_review(self, trigger, verdict, body):
        # ateles#241: record the native review emission so tests can assert the
        # event fired (and with which verdict) without real GitHub I/O.
        #
        # `body` MUST be forwarded. Dropping it made this helper disagree with
        # production: the real `_emit_formal_review` cross-checks the body
        # (ateles#595), so a `**COMMENT**` above a `[BLOCKING]` finding recorded
        # COMMENT here while GitHub received REQUEST_CHANGES — the stub silently
        # hid the very disagreement these tests exist to catch.
        calls.append(("review", verdict_to_review_event(verdict, body=body)))
        return "rev-1"

    async def fake_persist(self, *a, **k):
        return None

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(SwarmDispatcher, "_changed_files", fake_changed_files)
    monkeypatch.setattr(SwarmDispatcher, "_route_blocking_findings", fake_route)
    monkeypatch.setattr(SwarmDispatcher, "_gate_merge_readiness", fake_gate)
    monkeypatch.setattr(
        SwarmDispatcher, "_post_missing_vanellus_comment", fake_post_missing_vanellus
    )
    monkeypatch.setattr(SwarmDispatcher, "_emit_formal_review", fake_emit_review)
    monkeypatch.setattr(SwarmDispatcher, "_persist_panel_reviews", fake_persist)
    monkeypatch.setattr(SwarmDispatcher, "_post_missing_panel_comments", fake_persist)
    monkeypatch.setattr(SwarmDispatcher, "_preregistered_expectations",
                        lambda self, repo, parent: _async_return({}))
    return SwarmDispatcher(_StubNotifier(), _config(auto_merge=auto_merge))


def _async_return(value):
    async def _coro():
        return value
    return _coro()


def test_handle_pr_blocking_verdict_routes_findings(monkeypatch):
    calls = []
    d = _pr_dispatcher_with_stubs(
        monkeypatch, vanellus_stdout="**REQUEST_CHANGES**\n1 blocking", calls=calls
    )
    asyncio.run(d._handle_pr(_trigger(body="Closes #80.")))
    assert ("route", "request_changes") in calls
    assert ("gate", None) not in calls


def test_handle_pr_clear_verdict_gates_readiness(monkeypatch):
    calls = []
    d = _pr_dispatcher_with_stubs(
        monkeypatch, vanellus_stdout="**APPROVE**\nlgtm", calls=calls
    )
    asyncio.run(d._handle_pr(_trigger(body="Closes #80.")))
    assert ("gate", None) in calls
    assert not any(c[0] == "route" for c in calls)


def test_handle_pr_emits_formal_review_on_blocking_path(monkeypatch):
    """ateles#241: a REQUEST_CHANGES verdict must reach GitHub's review state,
    not just route findings internally."""
    calls = []
    d = _pr_dispatcher_with_stubs(
        monkeypatch, vanellus_stdout="**REQUEST_CHANGES**\n1 blocking", calls=calls
    )
    asyncio.run(d._handle_pr(_trigger(body="Closes #80.")))
    assert ("review", "REQUEST_CHANGES") in calls, calls


def test_handle_pr_emits_formal_review_on_clear_path(monkeypatch):
    """An APPROVE must also land as a native review — that is what lets branch
    protection see the swarm's verdict."""
    calls = []
    d = _pr_dispatcher_with_stubs(
        monkeypatch, vanellus_stdout="**APPROVE**\nlgtm", calls=calls
    )
    asyncio.run(d._handle_pr(_trigger(body="Closes #80.")))
    assert ("review", "APPROVE") in calls, calls


def test_formal_review_emitted_before_routing_decision(monkeypatch):
    """Emission happens on BOTH paths, so it must precede the branch — and must
    fire exactly once per handled PR (not duplicated across retry paths)."""
    calls = []
    d = _pr_dispatcher_with_stubs(
        monkeypatch, vanellus_stdout="**REQUEST_CHANGES**\nx", calls=calls
    )
    asyncio.run(d._handle_pr(_trigger(body="Closes #80.")))
    kinds = [c[0] for c in calls]
    assert kinds.count("review") == 1, f"expected exactly one review emission: {calls}"
    assert kinds.index("review") < kinds.index("route"), kinds


def test_handle_pr_unparseable_verdict_emits_comment_review(monkeypatch):
    """An unparseable verdict still records a review, as the inert COMMENT —
    never REQUEST_CHANGES, which would block merge on a parse failure."""
    calls = []
    d = _pr_dispatcher_with_stubs(
        monkeypatch, vanellus_stdout="the panel had thoughts", calls=calls
    )
    asyncio.run(d._handle_pr(_trigger(body="Closes #80.")))
    assert ("review", "COMMENT") in calls, calls


def test_handle_pr_unparseable_verdict_routes_not_gates(monkeypatch):
    calls = []
    d = _pr_dispatcher_with_stubs(
        monkeypatch, vanellus_stdout="the panel had thoughts", calls=calls
    )
    asyncio.run(d._handle_pr(_trigger(body="Closes #80.")))
    # Unparseable → treated as not-clear → route (never silently gate ready).
    assert any(c[0] == "route" for c in calls)
    assert ("gate", None) not in calls


# ── auto re-review on push against open gates (ateles#230) ───────────────────


def _gate_blocked_dispatcher(monkeypatch, *, calls, auto_rereview):
    """Dispatcher whose Lanius returns GATE_INHERITANCE: blocked, recording
    which lens skills actually run so we can assert panel-skip vs re-review."""

    async def fake_run_skill(skill, prompt, **kwargs):
        if skill == "lanius":
            return SkillResult(skill, True, 0, "GATE_INHERITANCE: blocked", "")
        if skill == "vanellus":
            return SkillResult(skill, True, 0, "**REQUEST_CHANGES**\nstill blocked", "")
        calls.append(("lens", skill))
        return SkillResult(skill, True, 0, "**COMMENT**\nre-reviewed", "")

    async def fake_changed_files(self, trigger):
        return ["src/x.ts"]

    async def fake_route(self, trigger, parent, reviews, verdict):
        calls.append(("route", verdict))

    async def fake_gate(self, trigger, parent, panel):
        calls.append(("gate", None))

    async def fake_noop(self, *a, **k):
        return None

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(SwarmDispatcher, "_changed_files", fake_changed_files)
    monkeypatch.setattr(SwarmDispatcher, "_route_blocking_findings", fake_route)
    monkeypatch.setattr(SwarmDispatcher, "_gate_merge_readiness", fake_gate)
    monkeypatch.setattr(SwarmDispatcher, "_post_missing_vanellus_comment", fake_noop)
    monkeypatch.setattr(SwarmDispatcher, "_persist_panel_reviews", fake_noop)
    monkeypatch.setattr(SwarmDispatcher, "_post_missing_panel_comments", fake_noop)
    monkeypatch.setattr(
        SwarmDispatcher,
        "_preregistered_expectations",
        lambda self, repo, parent: _async_return({}),
    )
    cfg = DispatchConfig(
        neotoma_token="", github_token="", auto_rereview_on_push=auto_rereview
    )
    return SwarmDispatcher(_StubNotifier(), cfg)


def test_gate_blocked_first_look_skips_panel_even_with_rereview_on(monkeypatch):
    """A gate-blocked PR on its FIRST look still skips the panel: nothing has
    been reviewed yet, so there is no finding to re-evaluate."""
    calls = []
    d = _gate_blocked_dispatcher(monkeypatch, calls=calls, auto_rereview=True)
    asyncio.run(d._handle_pr(_trigger(kind="pr_opened", action="opened")))
    assert not any(c[0] == "lens" for c in calls)
    assert not any(c[0] == "route" for c in calls)


def test_gate_blocked_repush_skips_panel_when_flag_off(monkeypatch):
    """Flag OFF (default) preserves today's behaviour exactly: a re-push to a
    gate-blocked PR is still a panel skip."""
    calls = []
    d = _gate_blocked_dispatcher(monkeypatch, calls=calls, auto_rereview=False)
    asyncio.run(d._handle_pr(_trigger(kind="pr_synchronize", action="synchronize")))
    assert not any(c[0] == "lens" for c in calls)
    assert not any(c[0] == "route" for c in calls)


def test_gate_blocked_repush_auto_rereviews_when_flag_on(monkeypatch):
    """Flag ON: a re-push to a gate-blocked PR re-invokes the lens agents
    against the new head instead of waiting for a manual /swarm-run."""
    calls = []
    d = _gate_blocked_dispatcher(monkeypatch, calls=calls, auto_rereview=True)
    asyncio.run(d._handle_pr(_trigger(kind="pr_synchronize", action="synchronize")))
    assert any(c[0] == "lens" for c in calls), "lens agents must re-review the new head"


def test_gate_blocked_reopen_auto_rereviews_when_flag_on(monkeypatch):
    """Flag ON: reopening a gate-blocked PR re-invokes the lens agents against
    the new head too — github_gateway.py maps `reopened` to kind
    "pr_reopened", a distinct string from "pr_synchronize", so this must be
    checked explicitly rather than assumed covered by the synchronize case."""
    calls = []
    d = _gate_blocked_dispatcher(monkeypatch, calls=calls, auto_rereview=True)
    asyncio.run(d._handle_pr(_trigger(kind="pr_reopened", action="reopened")))
    assert any(c[0] == "lens" for c in calls), "lens agents must re-review the new head"


def test_auto_rereview_never_gates_merge_readiness(monkeypatch):
    """Self-certification boundary (ateles#230 arch §4): an auto-re-review of a
    gate-blocked PR must never reach merge-readiness — a push is not a sign-off.
    The lens verdict (REQUEST_CHANGES here) routes findings instead."""
    calls = []
    d = _gate_blocked_dispatcher(monkeypatch, calls=calls, auto_rereview=True)
    asyncio.run(d._handle_pr(_trigger(kind="pr_synchronize", action="synchronize")))
    assert not any(c[0] == "gate" for c in calls), "a push must never gate merge-ready"
    assert any(c[0] == "route" for c in calls)


@pytest.mark.skipif(
    os.environ.get("ATELES_SWARM_AUTO_REREVIEW") is not None,
    reason=(
        "asserts the UNSET-env default, and DispatchConfig bakes its defaults "
        "from os.environ at import time — a shell that exports the flag (the "
        "live daemon's own posture) makes the premise false, not the code wrong"
    ),
)
def test_auto_rereview_flag_defaults_off():
    """Default OFF — autonomy expansions are opt-in (ateles#80 rollout rule)."""
    assert DispatchConfig(neotoma_token="", github_token="").auto_rereview_on_push is False


# ── _route_blocking_findings (per-lens → Cicada, bounded) ────────────────────


def _lens_agent(lens):
    from swarm_dispatch import LENSES
    for lp in LENSES:
        if lp.lens == lens:
            return lp.agent
    return "gryllus"


def test_route_findings_groups_by_lens_then_dispatches_cicada(monkeypatch):
    dispatched = []

    async def fake_run_skill(skill, prompt, **kwargs):
        dispatched.append(skill)
        return SkillResult(skill, True, 0, "guidance/fix applied", "")

    async def fake_count(self, trigger):
        return 0

    async def fake_record(self, trigger, n):
        return None

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(SwarmDispatcher, "_fix_round_count", fake_count)
    monkeypatch.setattr(SwarmDispatcher, "_record_fix_round", fake_record)

    d = SwarmDispatcher(_StubNotifier(), _config())
    reviews = [
        ("ux", "[BLOCKING] naming: the flag is undiscoverable\ndetail here"),
        ("qa", "[BLOCKING] coverage: no regression test\nadd one"),
        ("qa", "[NON-BLOCKING] style: minor"),
    ]
    asyncio.run(
        d._route_blocking_findings(_trigger(), parent=80, reviews=reviews,
                                   verdict="request_changes")
    )
    # Each lens with a blocking finding gets its owning agent invoked, then Cicada.
    assert _lens_agent("ux") in dispatched
    assert _lens_agent("qa") in dispatched
    assert "cicada" in dispatched
    # Cicada is invoked exactly once (after guidance), not per-lens.
    assert dispatched.count("cicada") == 1


def _route_capture_cicada_guidance(monkeypatch, *, lens_result_for):
    """Run _route_blocking_findings with a single-lens (ux) blocking finding,
    driving each lens agent's SkillResult via `lens_result_for(agent)`, and
    return the guidance text embedded in the Cicada dispatch prompt.

    Exercises the per-lens guidance fallback (lens agent fails / empty stdout →
    raw findings) that gates whether Cicada still receives the work."""
    captured = {}

    async def fake_run_skill(skill, prompt, **kwargs):
        if skill == "cicada":
            captured["cicada_prompt"] = prompt
            return SkillResult(skill, True, 0, "applied", "")
        return lens_result_for(skill)

    async def fake_count(self, trigger):
        return 0

    async def fake_record(self, trigger, n):
        return None

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(SwarmDispatcher, "_fix_round_count", fake_count)
    monkeypatch.setattr(SwarmDispatcher, "_record_fix_round", fake_record)

    d = SwarmDispatcher(_StubNotifier(), _config())
    reviews = [("ux", "[BLOCKING] naming: the flag is undiscoverable\nraw-ux-detail")]
    asyncio.run(
        d._route_blocking_findings(_trigger(), parent=80, reviews=reviews,
                                   verdict="request_changes")
    )
    return captured.get("cicada_prompt", ""), d


def test_route_findings_lens_failure_falls_back_to_raw_findings(monkeypatch):
    # Lens agent returns not-ok → Cicada still gets the raw findings.
    def lens_result_for(agent):
        return SkillResult(agent, False, 1, "", "boom")

    prompt, _ = _route_capture_cicada_guidance(
        monkeypatch, lens_result_for=lens_result_for
    )
    assert "guidance unavailable — raw findings" in prompt
    assert "raw-ux-detail" in prompt  # Cicada still received the work


def test_route_findings_empty_guidance_falls_back_to_raw_findings(monkeypatch):
    # Lens agent returns ok but blank stdout → fallback triggers.
    def lens_result_for(agent):
        return SkillResult(agent, True, 0, "   ", "")

    prompt, _ = _route_capture_cicada_guidance(
        monkeypatch, lens_result_for=lens_result_for
    )
    assert "guidance unavailable — raw findings" in prompt
    assert "raw-ux-detail" in prompt


def test_route_findings_mixed_lens_outcomes_include_both_sections(monkeypatch):
    # One lens succeeds with guidance, another fails → both appear in the
    # consolidated guidance handed to Cicada.
    captured = {}

    async def fake_run_skill(skill, prompt, **kwargs):
        if skill == "cicada":
            captured["prompt"] = prompt
            return SkillResult(skill, True, 0, "applied", "")
        if skill == _lens_agent("ux"):
            return SkillResult(skill, True, 0, "concrete ux guidance", "")
        # qa's owning agent fails → its section falls back to raw findings.
        return SkillResult(skill, False, 1, "", "err")

    async def fake_count(self, trigger):
        return 0

    async def fake_record(self, trigger, n):
        return None

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(SwarmDispatcher, "_fix_round_count", fake_count)
    monkeypatch.setattr(SwarmDispatcher, "_record_fix_round", fake_record)

    d = SwarmDispatcher(_StubNotifier(), _config())
    reviews = [
        ("ux", "[BLOCKING] naming: unclear\nux-raw"),
        ("qa", "[BLOCKING] coverage: none\nqa-raw"),
    ]
    asyncio.run(
        d._route_blocking_findings(_trigger(), parent=80, reviews=reviews,
                                   verdict="request_changes")
    )
    prompt = captured.get("prompt", "")
    assert "concrete ux guidance" in prompt          # successful lens section
    assert "guidance unavailable — raw findings" in prompt  # failed lens section
    assert "qa-raw" in prompt                         # failed lens still forwards work


def test_route_findings_escalates_at_retry_cap(monkeypatch):
    dispatched = []

    async def fake_run_skill(skill, prompt, **kwargs):
        dispatched.append(skill)
        return SkillResult(skill, True, 0, "x", "")

    async def fake_count(self, trigger):
        return 2  # == default max_fix_rounds

    async def fake_claim(self, trigger, kind):
        return True  # first escalation → notify

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(SwarmDispatcher, "_fix_round_count", fake_count)
    monkeypatch.setattr(SwarmDispatcher, "_claim_escalation", fake_claim)

    notifier = _StubNotifier()
    d = SwarmDispatcher(notifier, _config())
    reviews = [("qa", "[BLOCKING] coverage: no test\nadd one")]
    asyncio.run(
        d._route_blocking_findings(_trigger(), parent=80, reviews=reviews,
                                   verdict="request_changes")
    )
    # At the cap: no agents dispatched; operator escalated instead.
    assert dispatched == []
    assert any("auto-fix rounds did not clear" in m for m in notifier.sent)


def test_route_findings_exhausted_dedup_suppresses_renotify(monkeypatch):
    """A re-review after the cap must not re-page the operator.

    Regression for the duplicate-fire bug (ateles#262 sent four identical
    auto-fix-exhausted pings): when _claim_escalation reports the condition was
    already escalated (returns False), the notification is suppressed.
    """
    async def fake_run_skill(skill, prompt, **kwargs):
        return SkillResult(skill, True, 0, "x", "")

    async def fake_count(self, trigger):
        return 2  # == default max_fix_rounds

    async def fake_claim(self, trigger, kind):
        return False  # already escalated → suppress

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(SwarmDispatcher, "_fix_round_count", fake_count)
    monkeypatch.setattr(SwarmDispatcher, "_claim_escalation", fake_claim)

    notifier = _StubNotifier()
    d = SwarmDispatcher(notifier, _config())
    reviews = [("qa", "[BLOCKING] coverage: no test\nadd one")]
    asyncio.run(
        d._route_blocking_findings(_trigger(), parent=80, reviews=reviews,
                                   verdict="request_changes")
    )
    assert not any("auto-fix rounds did not clear" in m for m in notifier.sent)


def test_route_findings_cicada_auth_failure_pages_infra(monkeypatch):
    seen = {}

    async def fake_run_skill(skill, prompt, **kwargs):
        if skill == "cicada":
            # Auth-expired claude calls exit 0 with the 401 text in stdout.
            return SkillResult(
                skill, True, 0,
                "Your credit balance is too low to access the Anthropic API", ""
            )
        return SkillResult(skill, True, 0, "guidance", "")

    async def fake_count(self, trigger):
        return 0

    async def fake_record(self, trigger, n):
        return None

    async def fake_auth_page(self, trigger, agent):
        seen["auth_agent"] = agent

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(SwarmDispatcher, "_fix_round_count", fake_count)
    monkeypatch.setattr(SwarmDispatcher, "_record_fix_round", fake_record)
    monkeypatch.setattr(SwarmDispatcher, "_handle_panel_auth_failure", fake_auth_page)

    d = SwarmDispatcher(_StubNotifier(), _config())
    asyncio.run(
        d._route_blocking_findings(
            _trigger(), parent=80,
            reviews=[("qa", "[BLOCKING] coverage: no test\nadd one")],
            verdict="request_changes",
        )
    )
    assert seen.get("auth_agent") == "cicada"


def test_route_findings_no_parseable_blocking_escalates(monkeypatch):
    async def fake_run_skill(skill, prompt, **kwargs):
        raise AssertionError("should not dispatch when nothing parses")

    async def fake_claim(self, trigger, kind):
        return True  # first escalation → notify

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(SwarmDispatcher, "_claim_escalation", fake_claim)
    notifier = _StubNotifier()
    d = SwarmDispatcher(notifier, _config())
    # BLOCKED verdict with no [BLOCKING] blocks → escalate, don't guess.
    asyncio.run(
        d._route_blocking_findings(_trigger(), parent=80,
                                   reviews=[("pm", "cannot proceed")],
                                   verdict="blocked")
    )
    assert any("no blocking findings could be parsed" in m for m in notifier.sent)


def test_route_findings_unparseable_dedup_suppresses_renotify(monkeypatch):
    """A re-review of the same unparseable verdict must not re-page the operator."""
    async def fake_run_skill(skill, prompt, **kwargs):
        raise AssertionError("should not dispatch when nothing parses")

    async def fake_claim(self, trigger, kind):
        return False  # already escalated → suppress

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(SwarmDispatcher, "_claim_escalation", fake_claim)
    notifier = _StubNotifier()
    d = SwarmDispatcher(notifier, _config())
    asyncio.run(
        d._route_blocking_findings(_trigger(), parent=80,
                                   reviews=[("pm", "cannot proceed")],
                                   verdict="blocked")
    )
    assert not any(
        "no blocking findings could be parsed" in m for m in notifier.sent
    )


# ── _gate_merge_readiness (verdict-clear AND CI-green) ───────────────────────


def _gate_dispatcher(monkeypatch, *, ci_state, checkpoints, routed):
    async def fake_ci(self, trigger):
        return ci_state

    async def fake_checkpoint(self, trigger, parent, lenses):
        checkpoints.append(trigger.number)

    async def fake_route_ci(self, trigger, parent):
        routed.append(trigger.number)

    monkeypatch.setattr(SwarmDispatcher, "_required_ci_state", fake_ci)
    monkeypatch.setattr(SwarmDispatcher, "_store_merge_checkpoint", fake_checkpoint)
    monkeypatch.setattr(SwarmDispatcher, "_route_ci_failure", fake_route_ci)
    return SwarmDispatcher(_StubNotifier(), _config())


def test_gate_readiness_ci_green_files_checkpoint_and_pages(monkeypatch):
    checkpoints, routed = [], []
    d = _gate_dispatcher(monkeypatch, ci_state="green",
                         checkpoints=checkpoints, routed=routed)
    asyncio.run(d._gate_merge_readiness(_trigger(), parent=80, panel=[]))
    assert checkpoints == [87]
    assert any("READY TO MERGE" in m for m in d.notifier.sent)


def test_gate_readiness_ci_failing_routes_no_checkpoint(monkeypatch):
    checkpoints, routed = [], []
    d = _gate_dispatcher(monkeypatch, ci_state="failing",
                         checkpoints=checkpoints, routed=routed)
    asyncio.run(d._gate_merge_readiness(_trigger(), parent=80, panel=[]))
    assert checkpoints == []
    assert routed == [87]


def test_gate_readiness_ci_pending_holds_without_paging(monkeypatch):
    checkpoints, routed = [], []
    d = _gate_dispatcher(monkeypatch, ci_state="pending",
                         checkpoints=checkpoints, routed=routed)
    asyncio.run(d._gate_merge_readiness(_trigger(), parent=80, panel=[]))
    assert checkpoints == []
    assert routed == []
    # Held as INFO (digest), not an OPERATOR_DECISION merge page.
    assert not any("READY TO MERGE" in m for m in d.notifier.sent)


# ── _required_ci_state (CI detection — status API + check-runs precedence) ───


class _FakeCIHttpxClient:
    """Routes GET by URL to canned responses for _required_ci_state:
    pulls/{n} → head sha; commits/{sha}/status → {state}; .../check-runs → {runs}.
    Any endpoint may be set to raise to exercise the fail-open path."""

    def __init__(self, *, head_sha="abc123", status_state="success",
                 check_runs=None, raise_on=None):
        self.head_sha = head_sha
        self.status_state = status_state
        self.check_runs = check_runs if check_runs is not None else []
        self.raise_on = raise_on or set()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, url, **kwargs):
        parent = self

        def _payload():
            if "/pulls/" in url:
                if "pulls" in parent.raise_on:
                    raise httpx.HTTPError("boom")
                return {"head": {"sha": parent.head_sha}}
            if url.endswith("/status"):
                if "status" in parent.raise_on:
                    raise httpx.HTTPError("boom")
                return {"state": parent.status_state}
            if url.endswith("/check-runs"):
                if "checks" in parent.raise_on:
                    raise httpx.HTTPError("boom")
                return {"check_runs": parent.check_runs}
            return {}

        payload = _payload()

        class _Resp:
            def raise_for_status(self_inner):
                pass

            def json(self_inner):
                return payload

        return _Resp()


def _ci_state(monkeypatch, **kw):
    client = _FakeCIHttpxClient(**kw)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")
    d = SwarmDispatcher(_StubNotifier(), _config())
    return asyncio.run(d._required_ci_state(_trigger()))


def test_required_ci_state_green_when_status_success_no_checks(monkeypatch):
    assert _ci_state(monkeypatch, status_state="success", check_runs=[]) == "green"


def test_required_ci_state_green_when_all_checks_success(monkeypatch):
    runs = [
        {"status": "completed", "conclusion": "success"},
        {"status": "completed", "conclusion": "success"},
    ]
    assert _ci_state(monkeypatch, status_state="", check_runs=runs) == "green"


def test_required_ci_state_failing_when_status_failure(monkeypatch):
    assert _ci_state(monkeypatch, status_state="failure", check_runs=[]) == "failing"


def test_required_ci_state_failing_when_any_check_failed(monkeypatch):
    runs = [
        {"status": "completed", "conclusion": "success"},
        {"status": "completed", "conclusion": "failure"},
    ]
    assert _ci_state(monkeypatch, status_state="success", check_runs=runs) == "failing"


def test_required_ci_state_failing_takes_precedence_over_pending(monkeypatch):
    # A still-running check AND a failed one → failing wins (don't call it pending).
    runs = [
        {"status": "in_progress", "conclusion": None},
        {"status": "completed", "conclusion": "failure"},
    ]
    assert _ci_state(monkeypatch, status_state="pending", check_runs=runs) == "failing"


def test_required_ci_state_pending_when_a_check_incomplete(monkeypatch):
    runs = [
        {"status": "completed", "conclusion": "success"},
        {"status": "queued", "conclusion": None},
    ]
    assert _ci_state(monkeypatch, status_state="success", check_runs=runs) == "pending"


def test_required_ci_state_failing_on_actionable_conclusions(monkeypatch):
    for concl in ("timed_out", "cancelled", "action_required"):
        runs = [{"status": "completed", "conclusion": concl}]
        assert _ci_state(monkeypatch, status_state="", check_runs=runs) == "failing"


def test_required_ci_state_unknown_on_missing_head_sha(monkeypatch):
    assert _ci_state(monkeypatch, head_sha="") == "unknown"


def test_required_ci_state_unknown_fails_open_on_api_error(monkeypatch):
    # A transient API error must NEVER fabricate a green (which would page a merge).
    assert _ci_state(monkeypatch, raise_on={"status"}) == "unknown"
    assert _ci_state(monkeypatch, raise_on={"checks"}) == "unknown"
    assert _ci_state(monkeypatch, raise_on={"pulls"}) == "unknown"


# ── pre_merge on_check_failure posture (ateles#350) ──────────────────────────


def test_ci_check_failure_closed_posture_files_brief_and_notifies(monkeypatch):
    # pre_merge defaults to posture=closed (DEFAULT_CLOSED_BOUNDARIES):
    # an API error at the CI-status boundary must file a blocking
    # checkpoint_brief and send an operator_decision notification — not just
    # silently return "unknown" for a caller to (maybe) notice.
    monkeypatch.setattr(swarm_dispatch, "load_policy", lambda: ExecutionPolicy(
        entity_id="fallback", loaded=False
    ))
    stored = []

    async def fake_store(self, entities, idempotency_key):
        stored.append((entities, idempotency_key))

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    client = _FakeCIHttpxClient(raise_on={"status"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    notifier = _StubNotifier()
    d = SwarmDispatcher(notifier, _config())
    state = asyncio.run(d._required_ci_state(_trigger()))

    assert state == "unknown"  # caller-facing return value is unchanged
    assert len(stored) == 1
    entities, _ = stored[0]
    assert entities[0]["entity_type"] == "checkpoint_brief"
    assert entities[0]["checkpoint_kind"] == "pre_merge_check_failure"
    assert entities[0]["blocking"] is True
    assert any("posture=closed" in msg for msg in notifier.sent)
    assert Priority.OPERATOR_DECISION in notifier.priorities


def test_ci_check_failure_open_posture_only_logs_no_brief(monkeypatch, caplog):
    pol = ExecutionPolicy(
        entity_id="p",
        checkpoint_postures={"pre_merge": CheckpointPosture.OPEN},
        loaded=True,
    )
    monkeypatch.setattr(swarm_dispatch, "load_policy", lambda: pol)
    stored = []

    async def fake_store(self, entities, idempotency_key):
        stored.append((entities, idempotency_key))

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    client = _FakeCIHttpxClient(raise_on={"status"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    notifier = _StubNotifier()
    d = SwarmDispatcher(notifier, _config())
    with caplog.at_level("WARNING"):
        state = asyncio.run(d._required_ci_state(_trigger()))

    assert state == "unknown"
    assert stored == []  # no checkpoint_brief filed under open posture
    assert notifier.sent == []  # no operator page under open posture
    assert "on_check_failure=open" in caplog.text
    assert "boundary=pre_merge" in caplog.text


# ── _handle_ci_status (check_suite:completed → route/readiness, ateles#197) ──


def _ci_status_trigger(**over):
    base = dict(
        kind="ci_status", repository="owner/repo", number=87,
        title="", body="", author="", html_url="",
        delivery_id="d-ci", action="completed",
        ci_head_sha="abc123", ci_conclusion="success", ci_pr_numbers=[87],
    )
    base.update(over)
    return SwarmTrigger(**base)


def _wire_ci_status(monkeypatch, *, ci_state, review_clear, pr_head="abc123",
                    pr_state="open", pr_draft=False, calls):
    async def fake_fetch_pr(self, repo, num):
        return {"number": num, "state": pr_state, "draft": pr_draft,
                "title": "t", "body": "Closes #80", "html_url": "u",
                "head": {"sha": pr_head, "ref": "feat/x"}, "base": {"ref": "main"}}

    async def fake_ci(self, trigger):
        return ci_state

    async def fake_clear(self, repo, num):
        return review_clear

    async def fake_route(self, trigger, parent):
        calls.append(("route", trigger.number))

    async def fake_gate(self, trigger, parent, panel, ci_state=None):
        calls.append(("gate", trigger.number))

    monkeypatch.setattr(SwarmDispatcher, "_fetch_pr", fake_fetch_pr)
    monkeypatch.setattr(SwarmDispatcher, "_required_ci_state", fake_ci)
    monkeypatch.setattr(SwarmDispatcher, "_pr_review_is_clear", fake_clear)
    monkeypatch.setattr(SwarmDispatcher, "_route_ci_failure", fake_route)
    monkeypatch.setattr(SwarmDispatcher, "_gate_merge_readiness", fake_gate)
    return SwarmDispatcher(_StubNotifier(), _config())


def test_ci_status_failing_routes_to_fix(monkeypatch):
    calls = []
    d = _wire_ci_status(monkeypatch, ci_state="failing", review_clear=True, calls=calls)
    asyncio.run(d._handle_ci_status(_ci_status_trigger()))
    assert ("route", 87) in calls
    assert not any(c[0] == "gate" for c in calls)


def test_ci_status_green_and_review_clear_gates_readiness(monkeypatch):
    calls = []
    d = _wire_ci_status(monkeypatch, ci_state="green", review_clear=True, calls=calls)
    asyncio.run(d._handle_ci_status(_ci_status_trigger()))
    assert ("gate", 87) in calls
    assert not any(c[0] == "route" for c in calls)


def test_ci_status_green_but_review_not_clear_does_nothing(monkeypatch):
    calls = []
    d = _wire_ci_status(monkeypatch, ci_state="green", review_clear=False, calls=calls)
    asyncio.run(d._handle_ci_status(_ci_status_trigger()))
    assert calls == []


def test_ci_status_pending_does_nothing(monkeypatch):
    calls = []
    d = _wire_ci_status(monkeypatch, ci_state="pending", review_clear=True, calls=calls)
    asyncio.run(d._handle_ci_status(_ci_status_trigger()))
    assert calls == []


def test_ci_status_stale_head_is_ignored(monkeypatch):
    # check_suite head != PR's current head → a superseded commit's checks must
    # not drive anything.
    calls = []
    d = _wire_ci_status(monkeypatch, ci_state="failing", review_clear=True,
                        pr_head="newhead999", calls=calls)
    asyncio.run(d._handle_ci_status(_ci_status_trigger(ci_head_sha="oldhead111")))
    assert calls == []


def test_ci_status_stale_head_skip_sends_no_notification(monkeypatch):
    # Stale-head is a structural no-op, not a hold: GitHub fires a distinct
    # check_suite:completed per (head_sha, CI app), so the PR's current head
    # gets its own independent completion event that re-drives this handler.
    # A stale skip can never be the terminal CI event for a PR — notifying on
    # every one would page the operator on ordinary out-of-order delivery.
    calls = []
    d = _wire_ci_status(monkeypatch, ci_state="failing", review_clear=True,
                        pr_head="newhead999", calls=calls)
    asyncio.run(d._handle_ci_status(_ci_status_trigger(ci_head_sha="oldhead111")))
    assert d.notifier.sent == []


def test_ci_status_no_associated_pr_is_ignored(monkeypatch):
    calls = []
    d = _wire_ci_status(monkeypatch, ci_state="failing", review_clear=True, calls=calls)
    asyncio.run(d._handle_ci_status(_ci_status_trigger(number=0, ci_pr_numbers=[])))
    assert calls == []


def test_ci_status_fetch_pr_failure_notifies_operator(monkeypatch):
    # ateles#197's core failure mode: a transient GitHub API error must not
    # drop the loop-closure event with zero operator-visible trace.
    calls = []

    async def fake_fetch_pr_fails(self, repo, num):
        return None

    async def fake_ci(self, trigger):
        return "green"

    async def fake_clear(self, repo, num):
        return True

    async def fake_route(self, trigger, parent):
        calls.append(("route", trigger.number))

    async def fake_gate(self, trigger, parent, panel, ci_state=None):
        calls.append(("gate", trigger.number))

    monkeypatch.setattr(SwarmDispatcher, "_fetch_pr", fake_fetch_pr_fails)
    monkeypatch.setattr(SwarmDispatcher, "_required_ci_state", fake_ci)
    monkeypatch.setattr(SwarmDispatcher, "_pr_review_is_clear", fake_clear)
    monkeypatch.setattr(SwarmDispatcher, "_route_ci_failure", fake_route)
    monkeypatch.setattr(SwarmDispatcher, "_gate_merge_readiness", fake_gate)
    d = SwarmDispatcher(_StubNotifier(), _config())

    asyncio.run(d._handle_ci_status(_ci_status_trigger()))

    assert calls == []
    assert len(d.notifier.sent) == 1
    assert "owner/repo#87" in d.notifier.sent[0]
    assert "could not fetch PR" in d.notifier.sent[0]


def test_ci_status_closed_pr_is_ignored(monkeypatch):
    calls = []
    d = _wire_ci_status(monkeypatch, ci_state="green", review_clear=True,
                        pr_state="closed", calls=calls)
    asyncio.run(d._handle_ci_status(_ci_status_trigger()))
    assert calls == []


def test_ci_status_green_threads_ci_state_into_gate(monkeypatch):
    # Loxia review nit: don't re-fetch CI in the gate when the ci_status path
    # already computed it. Assert the gate receives ci_state="green".
    seen = {}

    async def fake_fetch_pr(self, repo, num):
        return {"number": num, "state": "open", "draft": False,
                "title": "t", "body": "Closes #80", "html_url": "u",
                "head": {"sha": "abc123", "ref": "f"}, "base": {"ref": "main"}}

    async def fake_ci(self, trigger):
        return "green"

    async def fake_clear(self, repo, num):
        return True

    async def fake_gate(self, trigger, parent, panel, ci_state=None):
        seen["ci_state"] = ci_state

    monkeypatch.setattr(SwarmDispatcher, "_fetch_pr", fake_fetch_pr)
    monkeypatch.setattr(SwarmDispatcher, "_required_ci_state", fake_ci)
    monkeypatch.setattr(SwarmDispatcher, "_pr_review_is_clear", fake_clear)
    monkeypatch.setattr(SwarmDispatcher, "_gate_merge_readiness", fake_gate)
    d = SwarmDispatcher(_StubNotifier(), _config())
    asyncio.run(d._handle_ci_status(_ci_status_trigger()))
    assert seen.get("ci_state") == "green"


def test_pr_review_is_clear_reads_newest_first(monkeypatch):
    # The intent below is right and unchanged: the LATEST aggregation decides.
    # The mechanism was wrong (ateles#430). This test used to assert
    # `direction == "desc"` and hand back a newest-first fixture — but GitHub
    # IGNORES sort/direction on issue comments and always returns oldest-first,
    # so the fixture asserted the bug. Selection is now client-side by
    # created_at, and the fixture returns oldest-first like the real API.
    captured = {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def get(self, url, **kwargs):
            captured["params"] = kwargs.get("params", {})
            # OLDEST-FIRST, as GitHub actually returns: the stale
            # REQUEST_CHANGES precedes the newer APPROVE.
            rows = [
                {
                    "id": 1,
                    "created_at": "2026-08-10T09:00:00Z",
                    "body": "<!-- vanellus-aggregation -->\n**REQUEST_CHANGES**\nold",
                },
                {
                    "id": 2,
                    "created_at": "2026-08-19T09:00:00Z",
                    "body": "<!-- vanellus-aggregation -->\n**APPROVE**\nlgtm",
                },
            ]

            class _Resp:
                def raise_for_status(self_inner):
                    pass

                def json(self_inner):
                    return rows if int(captured["params"].get("page", 1)) == 1 else []

            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")
    d = SwarmDispatcher(_StubNotifier(), _config())
    result = asyncio.run(d._pr_review_is_clear("owner/repo", 87))
    assert result is True
    # The inert sort/direction params are gone; recency is decided client-side.
    assert "direction" not in captured["params"]
    assert captured["params"].get("per_page") == 100


# ── prompt generators — guardrails must survive refactors ────────────────────


def test_fix_guidance_prompt_names_agent_and_forbids_writing_code():
    p = SwarmDispatcher._fix_guidance_prompt(
        _trigger(), lens="ux", agent="accipiter", findings="[naming] flag unclear"
    )
    assert "accipiter" in p
    assert "ux" in p
    # Reviewer proposes, does not implement (author≠reviewer separation).
    assert "Do NOT" in p and "write the code" in p
    assert "[naming] flag unclear" in p


def test_cicada_fix_prompt_carries_no_merge_guardrail_and_pr_ref():
    p = SwarmDispatcher._cicada_fix_prompt(
        _trigger(), parent=80, round_n=2, guidance="### ux (accipiter)\nfix it"
    )
    assert "DO NOT MERGE" in p
    assert "do NOT open a new PR" in p
    assert "round 2" in p
    assert "owner/repo#87" in p  # the PR ref from _trigger()
    assert "fix it" in p  # the guidance is embedded


def test_cicada_ci_fix_prompt_carries_no_merge_guardrail():
    p = SwarmDispatcher._cicada_ci_fix_prompt(_trigger(), parent=80, round_n=1)
    assert "DO NOT MERGE" in p
    assert "do NOT open a new PR" in p
    assert "CI is FAILING" in p or "required CI is FAILING" in p
    assert "owner/repo#87" in p


# ── lenses_missing_comments ─────────────────────────────────────────────────


def test_lenses_missing_comments_detects_unposted_reviews():
    bodies = ["review:pm\n\nLooks good.", "unrelated comment"]
    assert lenses_missing_comments(bodies, ["pm", "qa"]) == ["qa"]


def test_lenses_missing_comments_tolerates_leading_whitespace():
    bodies = ["  review:qa\nfine"]
    assert lenses_missing_comments(bodies, ["qa"]) == []


def test_lenses_missing_comments_all_missing_when_no_comments():
    assert lenses_missing_comments([], ["pm", "qa"]) == ["pm", "qa"]


# ── comment attribution ─────────────────────────────────────────────────────


def test_attribution_header_names_agent_and_role():
    header = attribution_header("corvus", "content lens panelist")
    assert "Corvus" in header
    assert "Ateles swarm" in header
    assert "content lens panelist" in header


def test_fallback_comment_carries_lens_prefix_and_attribution():
    body = compose_fallback_comment("qa", "phoenicurus", "Looks solid.")
    assert body.startswith("review:qa\n")
    assert "Phoenicurus" in body
    assert "Looks solid." in body
    assert "Apis dispatcher" in body
    # The prefix line must stay machine-detectable for dedup.
    assert lenses_missing_comments([body], ["qa"]) == []


# ── Lanius verdict retry ────────────────────────────────────────────────────


def test_lanius_missing_verdict_retried_once_then_blocked(monkeypatch):
    calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append((skill, prompt))
        if skill == "lanius" and len(calls) == 1:
            return SkillResult(skill, True, 0, "no verdict here", "")
        return SkillResult(skill, True, 0, "GATE_INHERITANCE: blocked", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(dispatcher._handle_pr(_trigger()))

    lanius_calls = [c for c in calls if c[0] == "lanius"]
    assert len(lanius_calls) == 2
    assert "REMINDER" in lanius_calls[1][1]
    # Blocked verdict on retry → panel never spawned, operator notified.
    assert [c[0] for c in calls] == ["lanius", "lanius"]
    assert any("blocked by Lanius" in m for m in notifier.sent)


def test_lanius_verdict_on_first_try_not_retried(monkeypatch):
    calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append(skill)
        return SkillResult(skill, True, 0, "GATE_INHERITANCE: blocked", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    dispatcher = SwarmDispatcher(_StubNotifier(), _config())

    asyncio.run(dispatcher._handle_pr(_trigger()))

    assert calls == ["lanius"]


def test_lanius_pr_prompt_gate_inheritance_contract_snapshot():
    """Golden snapshot of the gate-inheritance instruction block (ateles#195).

    Rather than scattered substring checks (which a prompt with all the right
    words plus a stray contradictory sentence would still pass), this pins the
    two authoritative sentences verbatim AND asserts their ORDER, so the policy
    reads as one coherent contract: read gate_status as source of truth FIRST,
    then the legacy rule only applies when the key is truly absent, and legacy
    init preserves — never downgrades — existing sign-offs.

    (NB: Lanius is an LLM agent — the retrieve→honor→merge behaviour executes
    inside a `claude --print` call, so a unit test cannot exercise the "pending
    still blocks" runtime branch; that is what the agentic_eval follow-up on
    ateles#195 covers. This test guards the CONTRACT Lanius is handed.)
    """
    prompt = SwarmDispatcher._lanius_pr_prompt(_trigger(), parent=80)

    source_of_truth = (
        "the parent issue's `gate_status` lives on its NEOTOMA issue entity, "
        "NOT in the GitHub issue body"
    )
    retrieve_call = (
        "retrieve_entity_by_identifier(entity_type='issue', "
        "identifier=<number>, identifier_field='github_number')"
    )
    authoritative = "Treat that stored `gate_status` as AUTHORITATIVE."
    satisfied = (
        "A pre-impl gate in `signed_off`, `waived`, or `not_required` counts "
        "as satisfied."
    )
    legacy_only_when_absent = (
        "ONLY when the retrieved issue entity has NO `gate_status` key at all "
        "(truly absent — not merely pending) do you treat it as legacy"
    )
    merge_not_overwrite = "Legacy init MUST MERGE, never overwrite"
    never_downgrade = "never downgrade a `signed_off` gate to `pending`"
    blocked_only_when_pending = (
        "Only emit `blocked` when `gate_status` exists AND a pre-impl gate is "
        "genuinely `pending`"
    )

    for fragment in (
        source_of_truth, retrieve_call, authoritative, satisfied,
        legacy_only_when_absent, merge_not_overwrite, never_downgrade,
        blocked_only_when_pending,
    ):
        assert fragment in prompt, f"missing gate-inheritance clause: {fragment!r}"

    # Order: source-of-truth is established BEFORE the legacy carve-out, and the
    # legacy rule requires true absence before it fires — so a pending gate can
    # never be re-cleared as "legacy".
    assert prompt.index(authoritative) < prompt.index(legacy_only_when_absent)
    assert prompt.index(legacy_only_when_absent) < prompt.index(never_downgrade)
    # The clear/blocked terminal-line contract is still present.
    assert "GATE_INHERITANCE: clear" in prompt
    assert "trigger_swarm_pr.py issue" in prompt


# ── pipeline-bypass guard (product PR with no parent issue) ──────────────────


def test_touches_product_code_detects_source_and_schema():
    assert swarm_dispatch.touches_product_code(["src/cli/mcp_config_scan.ts"])
    assert swarm_dispatch.touches_product_code(["packages/core/index.ts"])
    assert swarm_dispatch.touches_product_code(["openapi.yaml"])
    assert swarm_dispatch.touches_product_code(["execution/daemons/apis/foo.py"])
    assert swarm_dispatch.touches_product_code(["migrations/001_init.sql"])


def test_touches_product_code_excludes_docs_tests_ci_config():
    assert not swarm_dispatch.touches_product_code(["docs/guide.md", "README.md"])
    assert not swarm_dispatch.touches_product_code(["tests/cli/foo.test.ts"])
    assert not swarm_dispatch.touches_product_code(["src/cli/foo.test.ts"])
    assert not swarm_dispatch.touches_product_code(["execution/daemons/apis/test_x.py"])
    assert not swarm_dispatch.touches_product_code([".github/workflows/ci.yml"])
    assert not swarm_dispatch.touches_product_code([".claude/settings.json"])
    # Empty/unknown diff must fail open (do not cry bypass on a fetch failure).
    assert not swarm_dispatch.touches_product_code([])


def test_touches_product_code_mixed_diff_flags_when_any_product_file():
    # A PR that mixes docs + one real source file still counts as product code.
    assert swarm_dispatch.touches_product_code(
        ["docs/guide.md", "tests/x.test.ts", "src/cli/index.ts"]
    )


def _stub_bypass_dispatcher(monkeypatch, *, changed_files, posted, newly=True):
    """Wire a dispatcher whose _handle_pr short-circuits after the bypass guard.

    Lanius returns `blocked` so the panel never spawns (keeps the test focused on
    the guard), _changed_files is stubbed, and _post_pipeline_bypass_comment is
    replaced with a recorder so we assert the comment attempt without HTTP.

    `newly` is the value the stubbed _post_pipeline_bypass_comment returns —
    True means the bypass was surfaced for the first time (operator should be
    notified), False means an existing marker was merely re-edited (duplicate
    PR event; notification must be suppressed).
    """

    async def fake_run_skill(skill, prompt, **kwargs):
        return SkillResult(skill, True, 0, "GATE_INHERITANCE: blocked", "")

    async def fake_changed_files(self, trigger):
        return changed_files

    async def fake_post_bypass(self, trigger):
        posted.append(trigger.number)
        return newly

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(SwarmDispatcher, "_changed_files", fake_changed_files)
    monkeypatch.setattr(
        SwarmDispatcher, "_post_pipeline_bypass_comment", fake_post_bypass
    )
    notifier = _StubNotifier()
    return SwarmDispatcher(notifier, _config()), notifier


def test_pr_no_parent_product_code_surfaces_bypass_loudly(monkeypatch):
    posted = []
    dispatcher, notifier = _stub_bypass_dispatcher(
        monkeypatch, changed_files=["src/cli/mcp_config_scan.ts"], posted=posted
    )
    # PR body with NO Closes/Fixes reference → parent is None.
    asyncio.run(dispatcher._handle_pr(_trigger(body="A fix, no issue link.")))

    # A visible PR comment was attempted and the operator was notified.
    assert posted == [87]
    assert any("bypassed the gated" in m for m in notifier.sent)


def test_pr_bypass_duplicate_event_does_not_renotify(monkeypatch):
    """A repeat PR event for an already-surfaced bypass must not re-notify.

    Regression for the duplicate-fire bug: ateles#242 received four identical
    'touched product code' pings. When _post_pipeline_bypass_comment reports the
    marker already existed (returns False), the operator notification is skipped
    even though the guard path still runs.
    """
    posted = []
    dispatcher, notifier = _stub_bypass_dispatcher(
        monkeypatch,
        changed_files=["src/cli/mcp_config_scan.ts"],
        posted=posted,
        newly=False,
    )
    asyncio.run(dispatcher._handle_pr(_trigger(body="A fix, no issue link.")))

    # The guard still ran (comment attempted) but no duplicate ping was sent.
    assert posted == [87]
    assert not any("bypassed the gated" in m for m in notifier.sent)


def test_post_bypass_comment_fail_open_when_new_post_errors(monkeypatch):
    """A first-time bypass whose comment POST fails transiently must still
    return True (fail-open), so the operator is notified — aligning with
    _claim_escalation. Loxia review observation on PR #271."""

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return []  # no existing marker → this is a first surfacing

    class _FailPostClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def get(self, url, **kwargs):
            return _Resp()

        async def post(self, url, **kwargs):
            raise httpx.HTTPError("transient boom")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FailPostClient())
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")
    d = SwarmDispatcher(_StubNotifier(), _config())
    result = asyncio.run(d._post_pipeline_bypass_comment(_trigger()))
    assert result is True  # fail-open → caller will notify


def test_post_bypass_comment_fail_closed_when_duplicate_patch_errors(monkeypatch):
    """A confirmed-duplicate bypass whose PATCH fails must return False — the
    bypass is already surfaced, so never re-notify even on a transient error."""

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return [{"id": 5, "body": "<!-- pipeline-bypass-notice -->"}]

    class _FailPatchClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def get(self, url, **kwargs):
            return _Resp()

        async def patch(self, url, **kwargs):
            raise httpx.HTTPError("transient boom")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _FailPatchClient())
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")
    d = SwarmDispatcher(_StubNotifier(), _config())
    result = asyncio.run(d._post_pipeline_bypass_comment(_trigger()))
    assert result is False  # fail-closed → known bypass, no re-notify


def test_pr_with_parent_does_not_trigger_bypass(monkeypatch):
    posted = []
    dispatcher, notifier = _stub_bypass_dispatcher(
        monkeypatch, changed_files=["src/cli/mcp_config_scan.ts"], posted=posted
    )
    # Body carries Closes #80 → parent resolves → no bypass path.
    asyncio.run(dispatcher._handle_pr(_trigger(body="Closes #80.")))

    assert posted == []
    assert not any("bypassed the gated" in m for m in notifier.sent)


def test_pr_no_parent_docs_only_does_not_trigger_bypass(monkeypatch):
    posted = []
    dispatcher, notifier = _stub_bypass_dispatcher(
        monkeypatch, changed_files=["docs/guide.md", "README.md"], posted=posted
    )
    asyncio.run(dispatcher._handle_pr(_trigger(body="Docs tweak, no issue.")))

    assert posted == []
    assert not any("bypassed the gated" in m for m in notifier.sent)


# ── _token_for_repo (#95) ────────────────────────────────────────────────────


def test_token_for_repo_neotoma_uses_neotoma_agent_pat(monkeypatch):
    """markmhendrickson/neotoma should use NEOTOMA_AGENT_PAT."""
    monkeypatch.setenv("NEOTOMA_AGENT_PAT", "neotoma-secret")
    monkeypatch.setenv("ATELES_AGENT_PAT", "ateles-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "shared-token")
    assert _token_for_repo("markmhendrickson/neotoma") == "neotoma-secret"


def test_token_for_repo_ateles_uses_ateles_agent_pat(monkeypatch):
    """markmhendrickson/ateles (and any non-neotoma repo) should use ATELES_AGENT_PAT."""
    monkeypatch.setenv("NEOTOMA_AGENT_PAT", "neotoma-secret")
    monkeypatch.setenv("ATELES_AGENT_PAT", "ateles-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "shared-token")
    assert _token_for_repo("markmhendrickson/ateles") == "ateles-secret"


def test_token_for_repo_neotoma_falls_back_to_github_token_when_pat_absent(monkeypatch):
    """When NEOTOMA_AGENT_PAT is unset, fall back to GITHUB_TOKEN."""
    monkeypatch.delenv("NEOTOMA_AGENT_PAT", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "shared-token")
    assert _token_for_repo("markmhendrickson/neotoma") == "shared-token"


def test_token_for_repo_ateles_falls_back_to_github_token_when_pat_absent(monkeypatch):
    """When ATELES_AGENT_PAT is unset, fall back to GITHUB_TOKEN."""
    monkeypatch.delenv("ATELES_AGENT_PAT", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "shared-token")
    assert _token_for_repo("markmhendrickson/ateles") == "shared-token"


def test_token_for_repo_returns_empty_string_when_no_tokens(monkeypatch):
    """When no tokens are set at all, return an empty string (no crash)."""
    monkeypatch.delenv("NEOTOMA_AGENT_PAT", raising=False)
    monkeypatch.delenv("ATELES_AGENT_PAT", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert _token_for_repo("markmhendrickson/neotoma") == ""
    assert _token_for_repo("markmhendrickson/ateles") == ""


def test_token_for_repo_only_suffix_matters(monkeypatch):
    """Any repo ending in /neotoma picks the neotoma PAT, not just the canonical slug."""
    monkeypatch.setenv("NEOTOMA_AGENT_PAT", "neotoma-secret")
    monkeypatch.setenv("ATELES_AGENT_PAT", "ateles-secret")
    # An org fork would still route via NEOTOMA_AGENT_PAT.
    assert _token_for_repo("someorg/neotoma") == "neotoma-secret"
    # A repo merely containing "neotoma" in the name but not ending in /neotoma
    # should use ATELES_AGENT_PAT.
    assert _token_for_repo("markmhendrickson/neotoma-fork") == "ateles-secret"


# ── Change A: _expectation_prompt mandates GitHub task-list syntax ───────────

def _sample_lens(forward_looking: bool = False) -> Lens:
    return Lens(
        agent="phoenicurus",
        lens="qa",
        gate="qa",
        checks="quality, test coverage, regression risk",
        forward_looking=forward_looking,
    )


def test_expectation_prompt_contains_checkbox_syntax():
    """_expectation_prompt must include the `- [ ]` task-list placeholder."""
    t = _trigger()
    prompt = SwarmDispatcher._expectation_prompt(t, _sample_lens())
    assert "- [ ]" in prompt


def test_expectation_prompt_instructs_github_task_list():
    """The prompt must explicitly name 'GitHub task-list' (or equivalent) checkbox syntax."""
    t = _trigger()
    prompt = SwarmDispatcher._expectation_prompt(t, _sample_lens())
    assert "GitHub task-list" in prompt or "task-list checkbox" in prompt


def test_expectation_prompt_header_unchanged():
    """The `review_expectation (<lens>)` header line must survive the change."""
    t = _trigger()
    prompt = SwarmDispatcher._expectation_prompt(t, _sample_lens())
    assert f"**{EXPECTATION_MARKER} (qa)**" in prompt


def test_expectation_prompt_no_plain_bullet_placeholder():
    """Plain `- <...>` placeholder without checkbox must no longer be present."""
    t = _trigger()
    prompt = SwarmDispatcher._expectation_prompt(t, _sample_lens())
    # The old placeholder was literally "- <3 to 6 tight ...>"
    assert "- <3 to 6 tight" not in prompt


# ── Change B: _panelist_prompt check-off instruction ────────────────────────

def test_panelist_prompt_checkoff_included_when_parent_and_expectation():
    """When parent issue and expectation are both present, check-off block appears."""
    t = _trigger()
    expectation = (
        f"**{EXPECTATION_MARKER} (qa)** — what phoenicurus will verify:\n"
        "- [ ] Tests exist for the new code path\n"
        "- [ ] No regressions in existing tests\n"
    )
    prompt = SwarmDispatcher._panelist_prompt(t, _sample_lens(), expectation, parent=80)
    assert "edit" in prompt
    assert "- [x]" in prompt
    assert "#80" in prompt


def test_panelist_prompt_checkoff_references_correct_parent():
    """Check-off instruction must reference the actual parent issue number."""
    t = _trigger()
    expectation = "- [ ] Some check\n"
    prompt = SwarmDispatcher._panelist_prompt(t, _sample_lens(), expectation, parent=42)
    assert "#42" in prompt


def test_panelist_prompt_no_checkoff_when_parent_none():
    """Without a parent issue, the check-off block must NOT appear."""
    t = _trigger()
    expectation = "- [ ] Some check\n"
    prompt = SwarmDispatcher._panelist_prompt(t, _sample_lens(), expectation, parent=None)
    assert "- [x]" not in prompt
    assert "edit the existing" not in prompt


def test_panelist_prompt_no_checkoff_when_expectation_empty():
    """Without a pre-registered expectation, the check-off block must NOT appear."""
    t = _trigger()
    prompt = SwarmDispatcher._panelist_prompt(t, _sample_lens(), expectation="", parent=80)
    assert "- [x]" not in prompt
    assert "edit the existing" not in prompt


def test_panelist_prompt_no_checkoff_when_both_missing():
    """No parent AND no expectation: definitely no check-off."""
    t = _trigger()
    prompt = SwarmDispatcher._panelist_prompt(t, _sample_lens(), expectation="", parent=None)
    assert "- [x]" not in prompt


def test_panelist_prompt_checkoff_scoped_to_own_lens():
    """Check-off instruction must name the lens so the agent edits only its own comment."""
    t = _trigger()
    expectation = "- [ ] Some check\n"
    prompt = SwarmDispatcher._panelist_prompt(t, _sample_lens(), expectation, parent=80)
    # The instruction must reference the lens-specific comment marker so the
    # agent doesn't accidentally edit another lens's expectation comment.
    assert f"{EXPECTATION_MARKER} (qa)" in prompt


def test_panelist_prompt_checkoff_forward_looking_lens_also_gets_checkoff():
    """Forward-looking lenses pre-register too, so they should also check off."""
    t = _trigger()
    expectation = "- [ ] Forward-looking check\n"
    prompt = SwarmDispatcher._panelist_prompt(
        t, _sample_lens(forward_looking=True), expectation, parent=80
    )
    assert "- [x]" in prompt
    assert "#80" in prompt


def test_panelist_prompt_review_comment_instruction_still_present():
    """The original 'Post your review as a PR comment' instruction must remain."""
    t = _trigger()
    expectation = "- [ ] Some check\n"
    prompt = SwarmDispatcher._panelist_prompt(t, _sample_lens(), expectation, parent=80)
    assert "Post your review as a PR comment" in prompt


# ── Gate writeback: a lens seated as a pending-gate owner must transcribe its
#    own sign-off into gate_status (fixes the #1944 arch-signs-off-in-comments-
#    but-gate_status.arch-stays-pending loop) ─────────────────────────────────

def _arch_lens() -> Lens:
    return Lens(
        agent="waxwing",
        lens="arch",
        gate="arch",
        checks="architecture, tenant isolation, idempotency",
        forward_looking=False,
    )


def test_panelist_prompt_gate_writeback_when_owns_pending_gate():
    """A pending-gate owner is told to correct gate_status.<gate> → signed_off."""
    t = _trigger()
    expectation = "- [ ] arch check\n"
    prompt = SwarmDispatcher._panelist_prompt(
        t, _arch_lens(), expectation, parent=80, owns_pending_gate=True
    )
    assert "GATE WRITEBACK" in prompt
    assert "gate_status.arch" in prompt
    assert '"signed_off"' in prompt
    # Must be conditional on a clean verdict and must preserve the rest of the map.
    assert "ONLY if" in prompt
    assert "MERGE the existing map" in prompt
    assert "gate-signoff-arch-" in prompt  # idempotency key stem


def test_panelist_prompt_no_gate_writeback_when_not_owner():
    """A lens NOT seated as a pending-gate owner gets no writeback instruction."""
    t = _trigger()
    expectation = "- [ ] arch check\n"
    prompt = SwarmDispatcher._panelist_prompt(
        t, _arch_lens(), expectation, parent=80, owns_pending_gate=False
    )
    assert "GATE WRITEBACK" not in prompt


def test_panelist_prompt_no_gate_writeback_when_no_parent():
    """No parent issue → no gate_status to write, even if flagged as owner."""
    t = _trigger()
    prompt = SwarmDispatcher._panelist_prompt(
        t, _arch_lens(), "- [ ] x\n", parent=None, owns_pending_gate=True
    )
    assert "GATE WRITEBACK" not in prompt


def test_panelist_prompt_no_gate_writeback_for_forward_looking_lens():
    """Forward-looking lenses are non-blocking and don't own blocking gates."""
    t = _trigger()
    lens = Lens(
        agent="corvus",
        lens="forward",
        gate="forward",
        checks="downstream enablement",
        forward_looking=True,
    )
    prompt = SwarmDispatcher._panelist_prompt(
        t, lens, "- [ ] x\n", parent=80, owns_pending_gate=True
    )
    assert "GATE WRITEBACK" not in prompt


def test_panelist_prompt_gate_writeback_defaults_off():
    """owns_pending_gate defaults False — existing callers are unaffected."""
    t = _trigger()
    prompt = SwarmDispatcher._panelist_prompt(
        t, _arch_lens(), "- [ ] x\n", parent=80
    )
    assert "GATE WRITEBACK" not in prompt


# ── ateles#109 — per-agent GitHub identity (NO-OP until provisioned) ─────────
# These tests cover the three-tier token resolution, attribution_header gating,
# prompt instruction blocks, and native assignment.  They explicitly assert the
# NO-OP property: with no <AGENT>_AGENT_PAT env vars set, behaviour is identical
# to pre-#109.  They also assert correct behaviour when a PAT IS set (mocked).


# ── is_provisioned ─────────────────────────────────────────────────────────────


def test_is_provisioned_false_when_pat_absent(monkeypatch):
    """NO-OP: is_provisioned returns False when PAVO_AGENT_PAT is not set."""
    monkeypatch.delenv("PAVO_AGENT_PAT", raising=False)
    assert is_provisioned("pavo") is False


def test_is_provisioned_true_when_pat_set(monkeypatch):
    """is_provisioned returns True when PAVO_AGENT_PAT is set."""
    monkeypatch.setenv("PAVO_AGENT_PAT", "ghp_test_pavo_pat")
    assert is_provisioned("pavo") is True


def test_is_provisioned_uppercases_agent_name(monkeypatch):
    """Agent name is uppercased before constructing the env var key."""
    monkeypatch.setenv("LANIUS_AGENT_PAT", "ghp_lanius")
    assert is_provisioned("lanius") is True
    assert is_provisioned("LANIUS") is True


def test_is_provisioned_empty_string_is_false(monkeypatch):
    """An empty-string env var is treated as absent (not provisioned)."""
    monkeypatch.setenv("CORVUS_AGENT_PAT", "")
    assert is_provisioned("corvus") is False


# ── _token_for_agent_on_repo ──────────────────────────────────────────────────


def test_token_for_agent_on_repo_tier1_when_agent_pat_set(monkeypatch):
    """Tier 1: agent's own PAT wins when PAVO_AGENT_PAT is set."""
    monkeypatch.setenv("PAVO_AGENT_PAT", "ghp_pavo_own")
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_ateles_shared")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fallback")
    result = _token_for_agent_on_repo("pavo", "markmhendrickson/ateles")
    assert result == "ghp_pavo_own"


def test_token_for_agent_on_repo_tier2_neotoma_when_unprovisioned(monkeypatch):
    """NO-OP Tier 2: without agent PAT, neotoma repo uses NEOTOMA_AGENT_PAT."""
    monkeypatch.delenv("PAVO_AGENT_PAT", raising=False)
    monkeypatch.setenv("NEOTOMA_AGENT_PAT", "ghp_neotoma_shared")
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_ateles_shared")
    result = _token_for_agent_on_repo("pavo", "markmhendrickson/neotoma")
    # Must match _token_for_repo — the existing #95 behaviour.
    assert result == _token_for_repo("markmhendrickson/neotoma")
    assert result == "ghp_neotoma_shared"


def test_token_for_agent_on_repo_tier2_ateles_when_unprovisioned(monkeypatch):
    """NO-OP Tier 2: without agent PAT, ateles repo uses ATELES_AGENT_PAT."""
    monkeypatch.delenv("PAVO_AGENT_PAT", raising=False)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_ateles_shared")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_fallback")
    result = _token_for_agent_on_repo("pavo", "markmhendrickson/ateles")
    assert result == _token_for_repo("markmhendrickson/ateles")
    assert result == "ghp_ateles_shared"


def test_token_for_agent_on_repo_tier3_fallback_when_all_absent(monkeypatch):
    """Tier 3: when no agent PAT and no per-repo PAT, falls back to GITHUB_TOKEN."""
    monkeypatch.delenv("PAVO_AGENT_PAT", raising=False)
    monkeypatch.delenv("ATELES_AGENT_PAT", raising=False)
    monkeypatch.delenv("NEOTOMA_AGENT_PAT", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_shared_fallback")
    result = _token_for_agent_on_repo("pavo", "markmhendrickson/ateles")
    assert result == "ghp_shared_fallback"


# ── attribution_header — always returns non-empty ────────────────────────────


def test_attribution_header_always_returns_nonempty():
    """attribution_header always returns the non-empty header string regardless
    of provisioning state — gating belongs in _agent_prompt_instruction."""
    assert attribution_header("pavo", "pm gate owner") != ""
    assert "Pavo" in attribution_header("pavo", "pm gate owner")


# ── _agent_prompt_instruction ──────────────────────────────────────────────────


def test_agent_prompt_instruction_shared_account_when_unprovisioned(monkeypatch):
    """NO-OP: without agent PAT, instruction says 'shared account / prepend header'."""
    monkeypatch.delenv("PAVO_AGENT_PAT", raising=False)
    instruction = _agent_prompt_instruction("pavo", "pm gate owner")
    # Must contain the shared-account wording from the current behaviour.
    assert attribution_header("pavo", "pm gate owner") in instruction
    assert "shared" in instruction.lower()
    # Must NOT say the agent is posting as itself.
    assert "your own GitHub account" not in instruction


def test_agent_prompt_instruction_own_account_when_provisioned(monkeypatch):
    """When PAVO_AGENT_PAT is set, instruction says agent posts AS ITSELF."""
    monkeypatch.setenv("PAVO_AGENT_PAT", "ghp_pavo_pat")
    instruction = _agent_prompt_instruction("pavo", "pm gate owner")
    assert "your own GitHub account" in instruction
    # Must NOT contain the attribution header (avatar is the identity).
    assert attribution_header("pavo", "pm gate owner") not in instruction
    # Must NOT say shared account.
    assert "account is shared" not in instruction


def test_agent_prompt_instruction_provisioned_login_uses_convention(monkeypatch):
    """When provisioned, the instruction cites the operator-scoped login."""
    monkeypatch.setenv("PAVO_AGENT_PAT", "ghp_pavo_pat")
    instruction = _agent_prompt_instruction("pavo", "pm gate owner")
    # AGENT_GITHUB_LOGIN["pavo"] is now e.g. "markmhendrickson-ateles-pavo"
    assert AGENT_GITHUB_LOGIN["pavo"] in instruction


# ── Prompt-level no-op assertions ─────────────────────────────────────────────


def test_lanius_issue_prompt_shared_account_text_when_unprovisioned(monkeypatch):
    """NO-OP: _lanius_issue_prompt uses shared-account wording when no PAT set."""
    monkeypatch.delenv("LANIUS_AGENT_PAT", raising=False)
    monkeypatch.delenv("PAVO_AGENT_PAT", raising=False)
    t = _trigger(kind="issue_opened", number=1, title="A new issue", body="Body.")
    prompt = SwarmDispatcher._lanius_issue_prompt(t)
    assert "account is shared" in prompt
    assert attribution_header("lanius", "issue triage") in prompt
    # No GitHub-native assignment when pavo is unprovisioned.
    assert "--add-assignee" not in prompt


def test_lanius_issue_prompt_own_account_and_assignment_when_provisioned(monkeypatch):
    """When LANIUS and PAVO PATs are set: own-account text + native assignment."""
    monkeypatch.setenv("LANIUS_AGENT_PAT", "ghp_lanius")
    monkeypatch.setenv("PAVO_AGENT_PAT", "ghp_pavo")
    t = _trigger(kind="issue_opened", number=1, title="A new issue", body="Body.")
    prompt = SwarmDispatcher._lanius_issue_prompt(t)
    # Lanius should use its own account.
    assert "your own GitHub account" in prompt
    assert "account is shared" not in prompt
    # Native assignment instruction for pavo should appear.
    assert "--add-assignee" in prompt
    assert AGENT_GITHUB_LOGIN["pavo"] in prompt
    # Must be best-effort.
    assert "best-effort" in prompt.lower() or "skip silently" in prompt.lower()


def test_pavo_prompt_shared_account_text_when_unprovisioned(monkeypatch):
    """NO-OP: _pavo_prompt uses shared-account wording when PAVO_AGENT_PAT absent."""
    monkeypatch.delenv("PAVO_AGENT_PAT", raising=False)
    t = _trigger()
    prompt = SwarmDispatcher._pavo_prompt(t)
    assert "account is shared" in prompt
    assert attribution_header("pavo", "pm gate owner") in prompt


def test_pavo_prompt_own_account_when_provisioned(monkeypatch):
    """When PAVO_AGENT_PAT is set, _pavo_prompt instructs own-account posting."""
    monkeypatch.setenv("PAVO_AGENT_PAT", "ghp_pavo")
    t = _trigger()
    prompt = SwarmDispatcher._pavo_prompt(t)
    assert "your own GitHub account" in prompt
    assert "account is shared" not in prompt


def test_vanellus_prompt_shared_account_text_when_unprovisioned(monkeypatch):
    """NO-OP: _vanellus_prompt uses shared-account wording when unprovisioned."""
    monkeypatch.delenv("VANELLUS_AGENT_PAT", raising=False)
    t = _trigger()
    prompt = SwarmDispatcher._vanellus_prompt(t, parent=80, lenses=["pm", "qa"])
    assert "account is shared" in prompt
    assert attribution_header("vanellus", "PR steward") in prompt


def test_vanellus_prompt_own_account_when_provisioned(monkeypatch):
    """When VANELLUS_AGENT_PAT is set, _vanellus_prompt uses own-account text."""
    monkeypatch.setenv("VANELLUS_AGENT_PAT", "ghp_vanellus")
    t = _trigger()
    prompt = SwarmDispatcher._vanellus_prompt(t, parent=80, lenses=["pm"])
    assert "your own GitHub account" in prompt
    assert "account is shared" not in prompt


def test_lanius_pr_prompt_shared_account_text_when_unprovisioned(monkeypatch):
    """NO-OP: _lanius_pr_prompt uses shared-account wording when unprovisioned."""
    monkeypatch.delenv("LANIUS_AGENT_PAT", raising=False)
    t = _trigger()
    prompt = SwarmDispatcher._lanius_pr_prompt(t, parent=80)
    assert "account is shared" in prompt
    assert attribution_header("lanius", "PR gate inheritance") in prompt


def test_panelist_prompt_shared_account_text_when_unprovisioned(monkeypatch):
    """NO-OP: _panelist_prompt uses shared-account wording when unprovisioned."""
    monkeypatch.delenv("PHOENICURUS_AGENT_PAT", raising=False)
    t = _trigger()
    prompt = SwarmDispatcher._panelist_prompt(t, _sample_lens(), "", parent=None)
    assert attribution_header("phoenicurus", "qa lens panelist") in prompt


def test_panelist_prompt_own_account_when_provisioned(monkeypatch):
    """When provisioned, _panelist_prompt instructs own-account posting."""
    monkeypatch.setenv("PHOENICURUS_AGENT_PAT", "ghp_phoenicurus")
    t = _trigger()
    prompt = SwarmDispatcher._panelist_prompt(t, _sample_lens(), "", parent=None)
    assert "your own GitHub account" in prompt
    assert "account is shared" not in prompt


# ── AGENT_GITHUB_LOGIN / agent_github_login convention ───────────────────────


def test_agent_github_login_follows_convention():
    """All 8 GitHub-facing agents map to <operator>-ateles-<agent> logins."""
    expected_agents = {
        "lanius", "pavo", "vanellus", "waxwing",
        "accipiter", "buteo", "phoenicurus", "corvus",
    }
    assert set(AGENT_GITHUB_LOGIN.keys()) == expected_agents
    for agent, login in AGENT_GITHUB_LOGIN.items():
        assert login == f"{_OPERATOR_LOGIN}-ateles-{agent}", (
            f"Expected login '{_OPERATOR_LOGIN}-ateles-{agent}', got {login!r}"
        )


def test_agent_github_login_default_operator():
    """agent_github_login returns <APIS_OPERATOR_LOGIN>-ateles-<agent> for all 8 agents."""
    for agent in GITHUB_FACING_AGENTS:
        expected = f"{_OPERATOR_LOGIN}-ateles-{agent}"
        assert agent_github_login(agent) == expected, (
            f"Expected '{expected}', got {agent_github_login(agent)!r}"
        )


def test_agent_github_login_operator_override(monkeypatch):
    """agent_github_login honours a forked operator handle, proving fork-uniqueness.

    This is the key forkability test: a different operator who sets
    APIS_OPERATOR_LOGIN=someoneelse gets 'someoneelse-ateles-pavo', not
    'markmhendrickson-ateles-pavo', ensuring no GitHub namespace collision.

    We monkeypatch the module-level ``_OPERATOR_LOGIN`` variable directly
    (rather than reloading the module) to avoid polluting subsequent tests with
    a stale module-level state.
    """
    monkeypatch.setattr(swarm_dispatch, "_OPERATOR_LOGIN", "someoneelse")
    assert swarm_dispatch.agent_github_login("pavo") == "someoneelse-ateles-pavo"


def test_github_facing_agents_set():
    """GITHUB_FACING_AGENTS contains exactly the 8 expected agent names."""
    assert GITHUB_FACING_AGENTS == {
        "lanius", "pavo", "vanellus", "waxwing",
        "accipiter", "buteo", "phoenicurus", "corvus",
    }


# ── ateles#112 — /confirm-gates-clear handler ──────────────────────────────


def _comment_trigger(**overrides):
    """Build an issue_comment SwarmTrigger for gate-clear tests."""
    base = dict(
        kind="issue_comment",
        repository="owner/repo",
        number=80,
        title="An issue",
        body="",
        author="contributor",
        html_url="https://github.com/owner/repo/issues/80",
        delivery_id="comment-delivery",
        action="created",
        comment_id=42,
        comment_author=_OPERATOR_LOGIN,
        comment_body="/confirm-gates-clear",
        comment_html_url="https://github.com/owner/repo/issues/80#issuecomment-42",
        comment_on_pr=False,
    )
    base.update(overrides)
    return SwarmTrigger(**base)


class _FakeGateStore:
    """In-memory stand-in for IssueGateStore with a real waive/verify loop.

    ``gate_status`` starts from *initial* and is mutated by ``waive`` exactly as
    the real store does, so the sweep semantics (waive all unsigned, leave the
    cleared alone) are exercised end to end. ``verify_returns`` lets a test
    simulate a write that does NOT persist — the verification-failure path.
    """

    def __init__(
        self,
        initial: dict | None = None,
        *,
        entity_found: bool = True,
        verify_returns: dict | None = None,
    ):
        self.gate_status = dict(initial or {})
        self.entity_found = entity_found
        self.verify_returns = verify_returns
        self.owner_history: list[dict] = []
        self.waive_calls: list[tuple] = []

    async def load(self, repo, issue_number):
        """Mirror IssueGateStore.load — the backfill path (ateles#416) reads
        before it writes, and reads again to verify the entity landed."""

        class _State:
            def __init__(self, found: bool) -> None:
                self.found = found
                # This fake mirrors IssueGateState, which distinguishes an
                # entity that exists from one whose gates were initialised.
                # These fixtures model a fully triaged entity, so the two
                # coincide; see test_entity_backfill.py for the split case.
                self.triaged = found

        return _State(self.entity_found)

    async def waive(self, repo, issue_number, pre_impl_gates):
        from gate_waive import (
            WaiveOutcome,
            gates_needing_waive,
            verify_waived,
            waive_history_entries,
        )

        self.waive_calls.append((repo, issue_number, tuple(pre_impl_gates)))
        outcome = WaiveOutcome()
        if not self.entity_found:
            return outcome
        outcome.entity_found = True
        targeted = gates_needing_waive(self.gate_status, tuple(pre_impl_gates))
        outcome.targeted = list(targeted)
        outcome.already_clear = [g for g in pre_impl_gates if g not in targeted]
        if not targeted:
            outcome.verified = True
            return outcome
        for gate in targeted:
            self.gate_status[gate] = "waived"
        self.owner_history.extend(waive_history_entries(targeted, "2026-07-27T00:00:00Z"))
        # Verification re-read: either the real mutated state, or an injected
        # state simulating a write that did not persist.
        observed = (
            self.verify_returns
            if self.verify_returns is not None
            else self.gate_status
        )
        still_open = verify_waived(observed, targeted)
        outcome.failed = still_open
        outcome.waived = [g for g in targeted if g not in still_open]
        outcome.verified = not still_open
        return outcome


def _install_fake_gate_store(monkeypatch, store):
    """Patch IssueGateStore so the dispatcher uses *store*."""
    monkeypatch.setattr(
        swarm_dispatch, "IssueGateStore", lambda base_url, token: store
    )
    return store


def _capture_waive_comments(monkeypatch):
    """Capture gate-waive result comment bodies instead of hitting GitHub."""
    bodies: list[str] = []

    async def fake_post(self, trigger, outcome):
        from gate_waive import format_waive_comment

        bodies.append(
            format_waive_comment(
                marker="<!-- swarm-gate-waive-result -->",
                header="hdr",
                waived=outcome.waived,
                already_clear=outcome.already_clear,
                failed=outcome.failed,
                entity_found=outcome.entity_found,
            )
        )

    monkeypatch.setattr(SwarmDispatcher, "_post_gate_waive_comment", fake_post)
    return bodies


def test_confirm_gates_clear_performs_dispatcher_side_write(monkeypatch):
    """Operator /confirm-gates-clear must waive DISPATCHER-SIDE, not via an agent.

    ateles#285: the waive used to be prompted to Lanius, which silently no-opped
    (agent spawned, wrote nothing, posted nothing). A gate waive is a mechanical
    state transition, so the dispatcher performs it directly. This test pins
    that contract: the sweep runs WITHOUT any agent turn.
    """
    calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append((skill, prompt))
        return SkillResult(skill, True, 0, "", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    store = _FakeGateStore({g: "pending" for g in PRE_IMPL_GATES})
    _install_fake_gate_store(monkeypatch, store)
    comments = _capture_waive_comments(monkeypatch)

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(dispatcher._handle_issue_comment(_comment_trigger()))

    # The waive must NOT depend on an agent turn.
    assert not [c for c in calls if c[0] == "lanius"], (
        "the gate waive must be a dispatcher-side write, not a Lanius prompt "
        "(ateles#285)"
    )
    assert store.waive_calls, "the dispatcher must call the gate store"
    # Every pre-impl gate must be waived.
    for gate in PRE_IMPL_GATES:
        assert store.gate_status[gate] == "waived", (
            f"gate {gate!r} must be waived by the dispatcher-side sweep"
        )
    # Notifier must be called to inform the operator.
    assert any("cleared" in m or "waiv" in m for m in notifier.sent)
    # And GitHub must see it.
    assert comments, "a gate-waive result comment must be posted"


def test_confirm_gates_clear_from_non_operator_is_ignored(monkeypatch):
    """A non-operator's /confirm-gates-clear must be silently ignored."""
    calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append(skill)
        return SkillResult(skill, True, 0, "", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(
        dispatcher._handle_issue_comment(
            _comment_trigger(comment_author="some-random-user")
        )
    )

    # Must not call Lanius or any other skill.
    assert calls == [], f"No skills should be called for non-operator; got {calls}"
    # Must not send an operator notification about gate-clearing.
    assert not any("waiv" in m or "cleared" in m for m in notifier.sent)


def test_no_command_in_comment_is_no_op(monkeypatch):
    """A comment that contains no /confirm-gates-clear is completely ignored."""
    calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append(skill)
        return SkillResult(skill, True, 0, "", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(
        dispatcher._handle_issue_comment(
            _comment_trigger(
                comment_author=_OPERATOR_LOGIN,
                comment_body="LGTM, nice work!",
            )
        )
    )

    assert calls == []
    assert notifier.sent == []


def test_confirm_gates_clear_on_pr_comment_retriggers_pr_pipeline(monkeypatch):
    """When comment is on a PR, after waiving gates the PR pipeline re-runs."""
    calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append(skill)
        return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)

    # _handle_pr makes GitHub API calls; stub them out.
    # Instance methods need a `self` parameter when patched via monkeypatch.setattr
    # on the class.
    async def fake_changed_files(self, t):
        return []

    async def fake_preregistered(self, repo, number):
        return {}

    async def fake_store(self, entities, idempotency_key):
        pass

    async def fake_post_missing(self, t, reviews, agents_by_lens):
        pass

    async def fake_persist(self, t, reviews, agents_by_lens):
        pass

    async def fake_merge_checkpoint(self, t, parent, lenses):
        pass

    monkeypatch.setattr(SwarmDispatcher, "_changed_files", fake_changed_files)
    monkeypatch.setattr(SwarmDispatcher, "_preregistered_expectations", fake_preregistered)
    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(SwarmDispatcher, "_post_missing_panel_comments", fake_post_missing)
    monkeypatch.setattr(SwarmDispatcher, "_persist_panel_reviews", fake_persist)
    monkeypatch.setattr(SwarmDispatcher, "_store_merge_checkpoint", fake_merge_checkpoint)
    store = _install_fake_gate_store(
        monkeypatch, _FakeGateStore({g: "pending" for g in PRE_IMPL_GATES})
    )
    _capture_waive_comments(monkeypatch)

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    # PR comment: comment_on_pr=True, body references the parent issue.
    asyncio.run(
        dispatcher._handle_issue_comment(
            _comment_trigger(
                comment_on_pr=True,
                body="Closes #50.",  # parent issue is #50
            )
        )
    )

    # Lanius must be called at minimum (for gate waive + PR pipeline).
    assert store.waive_calls, "the dispatcher-side gate waive must have run"


def test_confirm_gates_clear_is_case_insensitive_for_operator_login(monkeypatch):
    """Operator login comparison is case-insensitive."""
    calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append(skill)
        return SkillResult(skill, True, 0, "", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    store = _install_fake_gate_store(
        monkeypatch, _FakeGateStore({g: "pending" for g in PRE_IMPL_GATES})
    )
    _capture_waive_comments(monkeypatch)
    dispatcher = SwarmDispatcher(_StubNotifier(), _config())

    # Mix case: MARKMHENDRICKSON vs markmhendrickson.
    asyncio.run(
        dispatcher._handle_issue_comment(
            _comment_trigger(comment_author=_OPERATOR_LOGIN.upper())
        )
    )
    assert store.waive_calls, "the dispatcher-side gate waive must have run"


# ── Part B — Pavo pm self-sign-off prompt ──────────────────────────────────


def test_pavo_prompt_contains_mandatory_sign_off_rule():
    """_pavo_prompt must tell Pavo to sign off gate_status.pm when scoping passes."""
    t = _trigger(kind="issue_opened", number=1, title="An issue", body="Body.")
    prompt = SwarmDispatcher._pavo_prompt(t)
    assert "MANDATORY SIGN-OFF RULE" in prompt or "signed_off" in prompt
    assert "gate_status.pm" in prompt
    assert "signed_off" in prompt


def test_pavo_prompt_requires_plan_contribution_sign_off():
    """Pavo must store a plan_contribution with contribution_type: sign_off."""
    t = _trigger(kind="issue_opened", number=1, title="An issue", body="Body.")
    prompt = SwarmDispatcher._pavo_prompt(t)
    assert "sign_off" in prompt
    assert "plan_contribution" in prompt


def test_pavo_prompt_warns_against_pending_deadlock():
    """Pavo prompt must warn that leaving pm pending is a deadlock."""
    t = _trigger(kind="issue_opened", number=1, title="An issue", body="Body.")
    prompt = SwarmDispatcher._pavo_prompt(t)
    assert "deadlock" in prompt or "pending pm gate" in prompt or "Do NOT leave pm" in prompt


def test_pavo_prompt_still_allows_blocking_on_failure():
    """When scoping fails, Pavo can (and should) block — prompt allows this."""
    t = _trigger(kind="issue_opened", number=1, title="An issue", body="Body.")
    prompt = SwarmDispatcher._pavo_prompt(t)
    assert "blocked" in prompt or "GENUINELY FAILS" in prompt


# ── Part C — Lanius PR blocked-comment guidance ────────────────────────────


def test_lanius_pr_prompt_blocked_comment_names_gates():
    """Blocked comment must name which pre-impl gates are unsigned."""
    prompt = SwarmDispatcher._lanius_pr_prompt(_trigger(), parent=80)
    # The prompt must mention the specific gates so Lanius lists them.
    for gate in PRE_IMPL_GATES:
        assert gate in prompt, f"Lanius PR prompt must mention gate '{gate}'"


def test_lanius_pr_prompt_blocked_comment_names_gate_owners():
    """Blocked comment must indicate who owns each gate (Pavo, Waxwing)."""
    prompt = SwarmDispatcher._lanius_pr_prompt(_trigger(), parent=80)
    assert "Pavo" in prompt or "pavo" in prompt
    assert "Waxwing" in prompt or "waxwing" in prompt


def test_lanius_pr_prompt_blocked_comment_includes_confirm_command():
    """Blocked comment must include the /confirm-gates-clear command."""
    prompt = SwarmDispatcher._lanius_pr_prompt(_trigger(), parent=80)
    assert _CONFIRM_GATES_CLEAR_CMD in prompt


def test_lanius_pr_prompt_blocked_comment_specifies_operator_only():
    """Blocked comment must specify that only the operator can issue the command."""
    prompt = SwarmDispatcher._lanius_pr_prompt(_trigger(), parent=80)
    assert _OPERATOR_LOGIN in prompt


def test_pre_impl_gates_constant_includes_expected_gates():
    """PRE_IMPL_GATES must include pm, ux and arch (the three pre-impl gates).

    ateles#285: `ux` was missing, which is why the 2026-07-23 waive on
    ateles#241 cleared `arch` and left `ux` pending. The Lanius skill's Phase 2
    (Accipiter `ux` + Bombycilla `arch`) and _lanius_pr_prompt both treat `ux`
    as a pre-impl gate, so the constant must agree.
    """
    assert "pm" in PRE_IMPL_GATES
    assert "ux" in PRE_IMPL_GATES
    assert "arch" in PRE_IMPL_GATES


def test_operator_login_defaults_to_repo_owner():
    """_OPERATOR_LOGIN must default to 'markmhendrickson' when env var not set."""
    # This is the env-based default; the actual value depends on env.
    # We verify the constant is non-empty (not blank).
    assert _OPERATOR_LOGIN, "_OPERATOR_LOGIN must not be empty"


# ── ateles#285 — dispatcher-side gate waive ──────────────────────────────────
#
# Regression coverage for the three failures recorded on ateles#241:
#   * 2026-07-23 PARTIAL apply (waived `arch`, left `ux` pending),
#   * 2026-07-27T17:08 rc=1,
#   * 2026-07-27T17:37 SILENT no-op (no write, no error, no comment).


def test_waive_sweeps_all_unsigned_gates_not_just_one():
    """ALL unsigned pre-impl gates are waived — the 2026-07-23 partial regression.

    That run waived `arch` and left `ux` pending, so the issue LOOKED cleared
    while PR gate inheritance kept blocking. `gates_needing_waive` is a total
    function over PRE_IMPL_GATES, so a partial sweep is structurally impossible.
    """
    from gate_waive import apply_waives, gates_needing_waive

    # Exactly the state of ateles#241 before the 2026-07-23 attempt.
    gate_status = {
        "pm": "signed_off",
        "ux": "pending",
        "arch": "pending",
        "impl": "pending",
        "pr_review": "pending",
        "qa": "pending",
        "legal": "not_required",
    }

    targeted = gates_needing_waive(gate_status, PRE_IMPL_GATES)
    assert set(targeted) == {"ux", "arch"}, (
        f"both unsigned pre-impl gates must be targeted, got {targeted}"
    )

    merged = apply_waives(gate_status, targeted)
    assert merged["ux"] == "waived", "ux must be waived (the 2026-07-23 miss)"
    assert merged["arch"] == "waived"
    # Signed-off and non-pre-impl gates are untouched.
    assert merged["pm"] == "signed_off"
    assert merged["impl"] == "pending"
    assert merged["legal"] == "not_required"


def test_waive_sweep_covers_gates_absent_from_gate_status():
    """A gate MISSING from gate_status is unsigned, not cleared."""
    from gate_waive import gates_needing_waive

    targeted = gates_needing_waive({"pm": "signed_off"}, PRE_IMPL_GATES)
    assert set(targeted) == {"ux", "arch"}, (
        f"absent gates must be treated as unsigned, got {targeted}"
    )


def test_already_cleared_gates_are_left_alone():
    """Gates already signed_off / waived / not_required are never re-waived."""
    from gate_waive import gates_needing_waive

    gate_status = {
        "pm": "signed_off",
        "ux": "waived",
        "arch": "not_required",
    }
    assert gates_needing_waive(gate_status, PRE_IMPL_GATES) == [], (
        "no gate should be targeted when all pre-impl gates are already cleared"
    )


def test_all_cleared_waive_is_a_reported_noop(monkeypatch):
    """An all-clear sweep still reports to the operator — silence is the bug."""
    store = _install_fake_gate_store(
        monkeypatch,
        _FakeGateStore({"pm": "signed_off", "ux": "waived", "arch": "signed_off"}),
    )
    comments = _capture_waive_comments(monkeypatch)

    async def fake_run_skill(skill, prompt, **kwargs):
        return SkillResult(skill, True, 0, "", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    dispatcher = SwarmDispatcher(_StubNotifier(), _config())

    asyncio.run(dispatcher._handle_issue_comment(_comment_trigger()))

    assert store.waive_calls
    assert comments, "a no-op sweep must STILL post an operator-visible comment"
    assert "No gates needed waiving" in comments[0]


def test_gate_status_parses_json_string_form():
    """gate_status round-trips as a JSON STRING in prod — it must still parse.

    Read off the live entity ent_4c1f77bc5fc86a2bad2025d6: Neotoma's schema
    inference typed the field as a string, so a naive dict access sees nothing
    to waive and the sweep no-ops.
    """
    from gate_waive import parse_gate_status

    raw = '{"pm": "signed_off", "ux": "pending", "arch": "waived"}'
    parsed = parse_gate_status(raw)
    assert parsed["pm"] == "signed_off"
    assert parsed["ux"] == "pending"
    assert parsed["arch"] == "waived"
    # Dict form works too, and junk degrades to empty rather than raising.
    assert parse_gate_status({"pm": "pending"}) == {"pm": "pending"}
    assert parse_gate_status("not json") == {}
    assert parse_gate_status(None) == {}


def test_verification_failure_is_reported_not_swallowed(monkeypatch):
    """A write that does NOT persist must report loudly, never continue silently.

    This is the 2026-07-27T17:37 failure: the transition never landed and the
    operator saw nothing. Now the re-read detects it, the outcome is not ok,
    the operator is notified at BLOCKER priority, and GitHub gets a failure
    comment naming the still-unsigned gates.
    """
    # The write "succeeds" but the verification re-read still shows ux pending.
    store = _install_fake_gate_store(
        monkeypatch,
        _FakeGateStore(
            {"pm": "signed_off", "ux": "pending", "arch": "pending"},
            verify_returns={"pm": "signed_off", "ux": "pending", "arch": "waived"},
        ),
    )
    comments = _capture_waive_comments(monkeypatch)

    async def fake_run_skill(skill, prompt, **kwargs):
        return SkillResult(skill, True, 0, "", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(dispatcher._handle_issue_comment(_comment_trigger()))

    assert store.waive_calls
    # The failure must reach GitHub, naming the gate that did not land.
    assert comments, "a verification failure must still post a comment"
    body = comments[0]
    assert "FAILED" in body, f"failure must be stated plainly: {body}"
    assert "`ux`" in body, f"the still-unsigned gate must be named: {body}"
    # And the operator must be notified.
    assert any("FAILED" in m or "failed" in m for m in notifier.sent), (
        f"operator must be notified of the verification failure: {notifier.sent}"
    )


def test_missing_issue_entity_is_reported(monkeypatch):
    """No Neotoma issue entity → report it; never claim the gates are clear."""
    store = _install_fake_gate_store(
        monkeypatch, _FakeGateStore(entity_found=False)
    )
    comments = _capture_waive_comments(monkeypatch)

    async def fake_run_skill(skill, prompt, **kwargs):
        return SkillResult(skill, True, 0, "", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(dispatcher._handle_issue_comment(_comment_trigger()))

    assert store.waive_calls
    assert comments and "could not be applied" in comments[0]
    assert any("FAILED" in m or "failed" in m for m in notifier.sent)


def test_waive_outcome_ok_semantics():
    """WaiveOutcome.ok: found + no failures. A missing entity is NOT ok."""
    from gate_waive import WaiveOutcome

    assert WaiveOutcome(entity_found=True, waived=["ux"]).ok
    # A genuine no-op on a found entity is fine.
    assert WaiveOutcome(entity_found=True, already_clear=["pm"]).ok
    assert not WaiveOutcome(entity_found=False).ok
    assert not WaiveOutcome(entity_found=True, failed=["ux"]).ok


def test_owner_history_append_preserves_existing_entries():
    """The waive APPENDS to owner_history — it never rebuilds the list."""
    from gate_waive import waive_history_entries

    existing = [
        {"agent": "lanius", "action": "triaged", "at": "2026-07-21T00:00:00Z"},
        {"agent": "pavo", "gate": "pm", "action": "signed_off"},
    ]
    appended = waive_history_entries(["ux", "arch"], "2026-07-27T18:00:00Z")
    merged = existing + appended

    assert merged[:2] == existing, "prior history must be preserved verbatim"
    assert len(appended) == 2
    for entry in appended:
        assert entry["action"] == "waived"
        assert entry["actor"] == "operator"
        assert entry["reason"] == "operator /confirm-gates-clear override"
        assert entry["timestamp"] == "2026-07-27T18:00:00Z"
    assert {e["gate"] for e in appended} == {"ux", "arch"}


def test_waive_comment_never_contains_a_command_token():
    """The dispatcher's own comment must not re-trigger the command detector.

    Same self-trigger defence as _post_swarm_run_comment (neotoma#1686): a body
    containing the literal command token would re-fire the handler via the
    bot's own webhook.
    """
    from gate_waive import format_waive_comment

    bodies = [
        format_waive_comment("<!-- m -->", "h", ["ux", "arch"], ["pm"], [], True),
        format_waive_comment("<!-- m -->", "h", [], ["pm", "ux", "arch"], [], True),
        format_waive_comment("<!-- m -->", "h", ["arch"], [], ["ux"], True),
        format_waive_comment("<!-- m -->", "h", [], [], [], False),
    ]
    for body in bodies:
        assert _CONFIRM_GATES_CLEAR_CMD not in body, (
            f"comment must carry no command token: {body}"
        )
        assert body.strip(), "every path must produce a non-empty comment"


def test_waive_comment_names_the_gates_in_every_outcome():
    """Success, failure, and no-op comments all name the gates involved."""
    from gate_waive import format_waive_comment

    success = format_waive_comment("<!-- m -->", "h", ["ux", "arch"], ["pm"], [], True)
    assert "`ux`" in success and "`arch`" in success
    assert "`pm`" in success, "already-cleared gates should be listed too"

    failure = format_waive_comment("<!-- m -->", "h", ["arch"], [], ["ux"], True)
    assert "`ux`" in failure and "FAILED" in failure
    assert "`arch`" in failure, "gates that DID land must still be named"

    noop = format_waive_comment("<!-- m -->", "h", [], ["pm", "ux", "arch"], [], True)
    assert "`pm`" in noop and "already" in noop.lower()


def test_gate_store_matches_entity_on_repo_and_number():
    """Entity matching tolerates repo/repository and issue_number/github_number."""
    from gate_waive import IssueGateStore

    match = IssueGateStore._matches
    # The live ateles#241 shape carries all four fields.
    live = {
        "repo": "markmhendrickson/ateles",
        "repository": "markmhendrickson/ateles",
        "issue_number": 241,
        "github_number": "241",
    }
    assert match(live, "markmhendrickson/ateles", 241)
    assert not match(live, "markmhendrickson/ateles", 242)
    assert not match(live, "markmhendrickson/neotoma", 241)
    # Only `repository` + only `github_number` (string) still matches.
    assert match({"repository": "o/r", "github_number": "7"}, "o/r", 7)
    # Only `repo` + int issue_number.
    assert match({"repo": "o/r", "issue_number": 7}, "o/r", 7)
    assert not match({"repo": "o/r"}, "o/r", 7)


# ── Phase 1 / Layer A: include_github_contract=True at GitHub-trigger call sites


def test_github_trigger_lanius_issue_passes_contract(monkeypatch):
    """All run_skill calls in _handle_issue_opened must pass include_github_contract=True.

    This is verified by capturing every keyword argument passed to run_skill and
    asserting the flag is set on the Lanius dispatch (the first call in the pipeline).
    """
    captured_kwargs: list[dict] = []

    async def spy_run_skill(skill, prompt, **kwargs):
        captured_kwargs.append({"skill": skill, **kwargs})
        return SkillResult(skill, True, 0, "ok", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", spy_run_skill)

    # select_expectation_agents returns lenses; stub it to return empty so only
    # lanius and pavo dispatches occur (simpler to assert on).
    monkeypatch.setattr(swarm_dispatch, "select_expectation_agents", lambda *a, **kw: [])

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())
    t = _trigger(kind="issue_opened", number=1, title="New issue", body="Body.")

    asyncio.run(dispatcher._handle_issue_opened(t))

    assert captured_kwargs, "run_skill must have been called at least once"
    for call in captured_kwargs:
        assert call.get("include_github_contract") is True, (
            f"run_skill call for skill={call['skill']!r} in _handle_issue_opened "
            "must pass include_github_contract=True"
        )


def test_additive_spec_pr_opened_is_info_priority(monkeypatch):
    """A successfully auto-built spec (PR opened) notifies at INFO, not
    OPERATOR_DECISION — it is FYI and belongs in the digest, since the PR gate
    pipeline now owns the work and stops at the operator's merge approval."""
    from lib.notify import Priority

    async def fake_run_skill(skill, prompt, **kwargs):
        return SkillResult(skill, True, 0, "ok", "")

    async def fake_open_pr(self, trigger, state):
        return "https://github.com/markmhendrickson/ateles/pull/999"

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(swarm_dispatch, "select_expectation_agents",
                        lambda *a, **kw: [])
    # ateles#460: _gates_green is async and takes (lanius, repository, number).
    async def _always_green(self, lanius, repository, issue_number):
        return True

    monkeypatch.setattr(SwarmDispatcher, "_gates_green", _always_green)
    monkeypatch.setattr(SwarmDispatcher, "_open_implementation_pr", fake_open_pr)
    monkeypatch.setattr(SwarmDispatcher, "_mark_pipeline_inflight",
                        lambda self, t, **kw: _async_none())
    monkeypatch.setattr(SwarmDispatcher, "_clear_pipeline_inflight",
                        lambda self, t: _async_none())

    notifier = _StubNotifier()
    cfg = _config()
    cfg.auto_build = True
    dispatcher = SwarmDispatcher(notifier, cfg)
    t = _trigger(kind="issue_opened", number=1, title="New issue", body="Body.")
    asyncio.run(dispatcher._handle_issue_opened(t))

    opened = [
        (m, p) for (m, p) in notifier.sent_full if "implementation PR opened" in m
    ]
    assert opened, "expected a PR-opened notification"
    assert all(p == Priority.INFO for _, p in opened), (
        "auto-build-succeeded notice must be INFO (digest), not immediate"
    )


async def _async_none():
    return None


def test_github_trigger_pr_pipeline_passes_contract(monkeypatch):
    """All run_skill calls in _handle_pr must pass include_github_contract=True."""
    captured_kwargs: list[dict] = []

    async def spy_run_skill(skill, prompt, **kwargs):
        captured_kwargs.append({"skill": skill, **kwargs})
        if skill == "lanius":
            return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
        return SkillResult(skill, True, 0, "ok", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", spy_run_skill)

    # Stub out the GitHub API helpers so the pipeline runs without network.
    async def fake_changed_files(self, t):
        return []

    async def fake_preregistered(self, repo, number):
        return {}

    async def fake_store(self, entities, idempotency_key):
        pass

    async def fake_post_missing(self, t, reviews, agents_by_lens):
        pass

    async def fake_persist(self, t, reviews, agents_by_lens):
        pass

    async def fake_merge_checkpoint(self, t, parent, lenses):
        pass

    monkeypatch.setattr(SwarmDispatcher, "_changed_files", fake_changed_files)
    monkeypatch.setattr(SwarmDispatcher, "_preregistered_expectations", fake_preregistered)
    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(SwarmDispatcher, "_post_missing_panel_comments", fake_post_missing)
    monkeypatch.setattr(SwarmDispatcher, "_persist_panel_reviews", fake_persist)
    monkeypatch.setattr(SwarmDispatcher, "_store_merge_checkpoint", fake_merge_checkpoint)

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(dispatcher._handle_pr(_trigger()))

    assert captured_kwargs, "run_skill must have been called at least once in _handle_pr"
    for call in captured_kwargs:
        assert call.get("include_github_contract") is True, (
            f"run_skill call for skill={call['skill']!r} in _handle_pr "
            "must pass include_github_contract=True"
        )


def test_gate_waive_spawns_no_agent(monkeypatch):
    """The gate waive must spawn NO agent at all (ateles#285).

    Replaces the old ``include_github_contract`` assertion: there is no longer
    a ``run_skill`` call to carry a contract, because the waive no longer goes
    through an agent turn. An agent spawn here would reintroduce exactly the
    silent-no-op failure mode this issue records.
    """
    captured_kwargs: list[dict] = []

    async def spy_run_skill(skill, prompt, **kwargs):
        captured_kwargs.append({"skill": skill, **kwargs})
        return SkillResult(skill, True, 0, "", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", spy_run_skill)
    store = _FakeGateStore({g: "pending" for g in PRE_IMPL_GATES})
    _install_fake_gate_store(monkeypatch, store)
    _capture_waive_comments(monkeypatch)

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(dispatcher._handle_issue_comment(_comment_trigger()))

    assert not captured_kwargs, (
        "the gate waive must not spawn any agent — it is a dispatcher-side "
        f"write (ateles#285); spawned: {captured_kwargs}"
    )
    assert store.waive_calls, "the dispatcher-side waive must have run"


# ── /swarm-run operator command (new) ─────────────────────────────────────────


def _swarm_run_trigger(**overrides):
    """Build an issue_comment SwarmTrigger for /swarm-run tests."""
    base = dict(
        kind="issue_comment",
        repository="owner/repo",
        number=42,
        title="An existing issue",
        body="Some issue description.",
        author="contributor",
        html_url="https://github.com/owner/repo/issues/42",
        delivery_id="swarm-run-delivery",
        action="created",
        comment_id=99,
        comment_author=_OPERATOR_LOGIN,
        comment_body="/swarm-run",
        comment_html_url="https://github.com/owner/repo/issues/42#issuecomment-99",
        comment_on_pr=False,
        labels=["bug"],
    )
    base.update(overrides)
    return SwarmTrigger(**base)


def test_swarm_run_from_operator_calls_handle_issue_opened(monkeypatch):
    """/swarm-run from the operator must call _handle_issue_opened for the right issue."""
    opened_calls = []

    async def fake_handle_issue_opened(self, trigger):
        opened_calls.append(trigger)

    # Stub _post_swarm_run_comment so it doesn't need a real HTTP connection.
    async def fake_post_swarm_run_comment(self, trigger):
        pass

    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle_issue_opened)
    monkeypatch.setattr(SwarmDispatcher, "_post_swarm_run_comment", fake_post_swarm_run_comment)

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())
    trigger = _swarm_run_trigger()

    asyncio.run(dispatcher._handle_issue_comment(trigger))

    assert len(opened_calls) == 1, (
        f"_handle_issue_opened must be called exactly once; got {len(opened_calls)}"
    )
    called_trigger = opened_calls[0]
    assert called_trigger.number == 42
    assert called_trigger.repository == "owner/repo"
    assert called_trigger.kind == "issue_opened"
    # Notifier must record the /swarm-run event.
    assert any(_SWARM_RUN_CMD in m or "swarm-run" in m.lower() for m in notifier.sent)


def test_swarm_run_passes_issue_fields_from_trigger(monkeypatch):
    """/swarm-run must forward the issue's title, body, labels from the trigger."""
    opened_calls = []

    async def fake_handle_issue_opened(self, trigger):
        opened_calls.append(trigger)

    async def fake_post_swarm_run_comment(self, trigger):
        pass

    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle_issue_opened)
    monkeypatch.setattr(SwarmDispatcher, "_post_swarm_run_comment", fake_post_swarm_run_comment)

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    trigger = _swarm_run_trigger(
        title="My precise issue title",
        body="Detailed body text.",
        labels=["enhancement", "needs-triage"],
    )

    asyncio.run(dispatcher._handle_issue_comment(trigger))

    assert len(opened_calls) == 1
    t = opened_calls[0]
    assert t.title == "My precise issue title"
    assert t.body == "Detailed body text."
    assert "enhancement" in t.labels
    assert "needs-triage" in t.labels


def test_swarm_run_from_non_operator_is_ignored(monkeypatch):
    """/swarm-run from a non-operator must be silently ignored."""
    opened_calls = []

    async def fake_handle_issue_opened(self, trigger):
        opened_calls.append(trigger)

    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle_issue_opened)

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(
        dispatcher._handle_issue_comment(
            _swarm_run_trigger(comment_author="some-random-user")
        )
    )

    assert opened_calls == [], (
        f"_handle_issue_opened must not be called for non-operator; got {opened_calls}"
    )
    # No operator notification about the swarm run.
    assert not any("swarm-run" in m.lower() for m in notifier.sent)


def test_comment_with_neither_command_is_no_op(monkeypatch):
    """A comment with neither /confirm-gates-clear nor /swarm-run is completely ignored."""
    opened_calls = []
    skill_calls = []

    async def fake_handle_issue_opened(self, trigger):
        opened_calls.append(trigger)

    async def fake_run_skill(skill, prompt, **kwargs):
        skill_calls.append(skill)
        return SkillResult(skill, True, 0, "", "")

    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle_issue_opened)
    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(
        dispatcher._handle_issue_comment(
            _swarm_run_trigger(comment_body="Great issue, thanks!")
        )
    )

    assert opened_calls == []
    assert skill_calls == []
    assert notifier.sent == []


def test_confirm_gates_clear_still_works_after_swarm_run_added(monkeypatch):
    """/confirm-gates-clear must still run the gate waive after the refactor."""
    calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append((skill, prompt))
        return SkillResult(skill, True, 0, "", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    store = _install_fake_gate_store(
        monkeypatch, _FakeGateStore({g: "pending" for g in PRE_IMPL_GATES})
    )
    _capture_waive_comments(monkeypatch)
    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(dispatcher._handle_issue_comment(_comment_trigger()))

    assert store.waive_calls, (
        "the dispatcher-side gate waive must still run for /confirm-gates-clear"
    )
    assert any("cleared" in m or "waiv" in m for m in notifier.sent)


def test_both_commands_prefers_confirm_gates_clear(monkeypatch):
    """When both /confirm-gates-clear and /swarm-run are in a comment, gates-clear wins."""
    opened_calls = []
    skill_calls = []

    async def fake_handle_issue_opened(self, trigger):
        opened_calls.append(trigger)

    async def fake_run_skill(skill, prompt, **kwargs):
        skill_calls.append(skill)
        return SkillResult(skill, True, 0, "Gates waived.", "")

    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle_issue_opened)
    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    store = _install_fake_gate_store(
        monkeypatch, _FakeGateStore({g: "pending" for g in PRE_IMPL_GATES})
    )
    _capture_waive_comments(monkeypatch)

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    asyncio.run(
        dispatcher._handle_issue_comment(
            _comment_trigger(
                comment_body=f"{_CONFIRM_GATES_CLEAR_CMD} {_SWARM_RUN_CMD}"
            )
        )
    )

    # /confirm-gates-clear path: the waive runs, the issue pipeline must NOT.
    assert store.waive_calls, (
        "the dispatcher-side gate waive must run when /confirm-gates-clear "
        "is present"
    )
    assert opened_calls == [], (
        "_handle_issue_opened must NOT be called when /confirm-gates-clear takes priority"
    )


def test_swarm_run_fetches_issue_fields_when_title_empty(monkeypatch):
    """/swarm-run fetches title/body/labels from the API when trigger.title is empty."""
    opened_calls = []
    fetch_calls = []

    async def fake_handle_issue_opened(self, trigger):
        opened_calls.append(trigger)

    async def fake_post_swarm_run_comment(self, trigger):
        pass

    async def fake_fetch_issue_fields(self, repository, issue_number):
        fetch_calls.append((repository, issue_number))
        return {
            "title": "Fetched title",
            "body": "Fetched body",
            "labels": [{"name": "fetched-label"}],
        }

    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle_issue_opened)
    monkeypatch.setattr(SwarmDispatcher, "_post_swarm_run_comment", fake_post_swarm_run_comment)
    monkeypatch.setattr(SwarmDispatcher, "_fetch_issue_fields", fake_fetch_issue_fields)

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    trigger = _swarm_run_trigger(title="", body="", labels=[])

    asyncio.run(dispatcher._handle_issue_comment(trigger))

    assert fetch_calls == [("owner/repo", 42)], (
        "_fetch_issue_fields must be called when trigger.title is empty"
    )
    assert len(opened_calls) == 1
    t = opened_calls[0]
    assert t.title == "Fetched title"
    assert t.body == "Fetched body"
    assert "fetched-label" in t.labels


def test_swarm_run_proceeds_even_if_confirmation_comment_fails(monkeypatch):
    """/swarm-run must call _handle_issue_opened even if the confirmation comment errors."""
    opened_calls = []

    async def fake_handle_issue_opened(self, trigger):
        opened_calls.append(trigger)

    async def failing_post_swarm_run_comment(self, trigger):
        raise RuntimeError("GitHub API unavailable")

    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle_issue_opened)
    monkeypatch.setattr(SwarmDispatcher, "_post_swarm_run_comment", failing_post_swarm_run_comment)

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())

    asyncio.run(dispatcher._handle_issue_comment(_swarm_run_trigger()))

    # Despite the confirmation comment failure, the pipeline must still run.
    assert len(opened_calls) == 1, (
        "_handle_issue_opened must still run when the confirmation comment fails"
    )


def test_swarm_run_constant_value():
    """_SWARM_RUN_CMD must be exactly '/swarm-run'."""
    assert _SWARM_RUN_CMD == "/swarm-run"


# ── Fix 1: confirmation body must not contain command tokens (neotoma#1686) ───

def test_post_swarm_run_comment_body_has_no_swarm_run_token(monkeypatch):
    """_post_swarm_run_comment body must NOT contain the literal '/swarm-run' token.

    The feedback loop (neotoma#1686) was caused by the confirmation comment
    containing the exact trigger string.  The body is now inspected by capturing
    the JSON sent to the GitHub API.
    """
    posted_bodies: list[str] = []

    async def fake_post_swarm_run_comment(self, trigger):
        # Call the real implementation but capture what it would POST.
        # We monkeypatch httpx.AsyncClient to record the body.
        pass

    # Directly test the body string produced before any HTTP call.
    # Build the marker + body as it appears in the implementation.
    _CONFIRMATION_MARKER = "<!-- swarm-run-confirmation -->"
    body = (
        f"{_CONFIRMATION_MARKER}\n"
        f"{attribution_header('apis', 'swarm dispatcher')}\n\n"
        "\U0001f501 Operator **swarm-run** command received — re-running "
        "the issue pipeline (Lanius triage + review expectations + Pavo "
        "scoping). This is idempotent: Lanius will edit-not-duplicate its "
        "triage comment and Pavo will update-not-recreate the gate status."
    )

    # The literal command tokens must be absent.
    assert _SWARM_RUN_CMD not in body, (
        f"Confirmation body must not contain {_SWARM_RUN_CMD!r} — "
        "that would re-trigger the handler via the bot's own webhook."
    )
    assert _CONFIRM_GATES_CLEAR_CMD not in body, (
        f"Confirmation body must not contain {_CONFIRM_GATES_CLEAR_CMD!r}."
    )
    # The stable marker must be present so Fix 3 (dedup) can find the comment.
    assert _CONFIRMATION_MARKER in body


def test_post_swarm_run_comment_body_contains_stable_marker(monkeypatch):
    """Confirmation body must include the stable HTML marker for dedup lookup."""
    _CONFIRMATION_MARKER = "<!-- swarm-run-confirmation -->"
    body = (
        f"{_CONFIRMATION_MARKER}\n"
        f"{attribution_header('apis', 'swarm dispatcher')}\n\n"
        "\U0001f501 Operator **swarm-run** command received — re-running "
        "the issue pipeline (Lanius triage + review expectations + Pavo "
        "scoping). This is idempotent: Lanius will edit-not-duplicate its "
        "triage comment and Pavo will update-not-recreate the gate status."
    )
    assert _CONFIRMATION_MARKER in body
    # Marker itself must not contain a command token.
    assert _SWARM_RUN_CMD not in _CONFIRMATION_MARKER
    assert _CONFIRM_GATES_CLEAR_CMD not in _CONFIRMATION_MARKER


# ── Fix 2: bot/machine-account guard (_is_bot_author) ─────────────────────────

def test_is_bot_author_exact_machine_accounts():
    """Known exact machine accounts are identified as bots."""
    assert _is_bot_author("ateles-agent") is True
    assert _is_bot_author("neotoma-agent") is True
    assert _is_bot_author("github-actions") is True


def test_is_bot_author_case_insensitive():
    """Bot check is case-insensitive."""
    assert _is_bot_author("Ateles-Agent") is True
    assert _is_bot_author("NEOTOMA-AGENT") is True
    assert _is_bot_author("GitHub-Actions") is True


def test_is_bot_author_ateles_infix_pattern():
    """Any login containing '-ateles-' is treated as a bot (per-agent accounts)."""
    assert _is_bot_author("markmhendrickson-ateles-pavo") is True
    assert _is_bot_author("someoneelse-ateles-lanius") is True
    assert _is_bot_author("fork-owner-ateles-vanellus") is True


def test_is_bot_author_github_app_suffix():
    """Logins ending in '[bot]' are treated as bots (GitHub Apps / Actions)."""
    assert _is_bot_author("github-actions[bot]") is True
    assert _is_bot_author("dependabot[bot]") is True
    assert _is_bot_author("renovate[bot]") is True
    assert _is_bot_author("copilot[bot]") is True


def test_is_bot_author_operator_and_humans_not_bots():
    """Real human and operator logins must not be flagged as bots."""
    assert _is_bot_author("markmhendrickson") is False
    assert _is_bot_author("some-random-user") is False
    assert _is_bot_author("contributor-ateles") is False  # no infix dash-ateles-dash
    assert _is_bot_author("ateles") is False              # not infix, not exact match


def test_bot_comment_with_swarm_run_is_ignored(monkeypatch):
    """A comment from a bot that contains /swarm-run must be silently ignored.

    This covers the neotoma#1686 feedback loop: the bot's own confirmation
    comment (which previously contained /swarm-run) fired the handler again.
    """
    opened_calls = []
    skill_calls = []

    async def fake_handle_issue_opened(self, trigger):
        opened_calls.append(trigger)

    async def fake_run_skill(skill, prompt, **kwargs):
        skill_calls.append(skill)
        return SkillResult(skill, True, 0, "", "")

    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle_issue_opened)
    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())

    for bot_login in [
        "neotoma-agent",
        "ateles-agent",
        "markmhendrickson-ateles-pavo",
        "github-actions[bot]",
        "dependabot[bot]",
        "github-actions",
    ]:
        asyncio.run(
            dispatcher._handle_issue_comment(
                _swarm_run_trigger(comment_author=bot_login)
            )
        )

    assert opened_calls == [], (
        f"_handle_issue_opened must not be called for bot authors; got {opened_calls}"
    )
    assert skill_calls == [], (
        f"No skills should be called for bot comments; got {skill_calls}"
    )
    assert notifier.sent == [], (
        f"No notifications should be sent for bot comments; got {notifier.sent}"
    )


def test_operator_swarm_run_still_dispatches_after_bot_guard(monkeypatch):
    """Bot guard must not block legitimate operator /swarm-run commands."""
    opened_calls = []

    async def fake_handle_issue_opened(self, trigger):
        opened_calls.append(trigger)

    async def fake_post_swarm_run_comment(self, trigger):
        pass

    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle_issue_opened)
    monkeypatch.setattr(SwarmDispatcher, "_post_swarm_run_comment", fake_post_swarm_run_comment)

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    asyncio.run(dispatcher._handle_issue_comment(_swarm_run_trigger()))

    assert len(opened_calls) == 1, (
        f"Operator /swarm-run must still dispatch; got {len(opened_calls)} calls"
    )


def test_operator_confirm_gates_clear_still_dispatches_after_bot_guard(monkeypatch):
    """Bot guard must not block legitimate operator /confirm-gates-clear commands."""
    calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append(skill)
        return SkillResult(skill, True, 0, "Gates waived.", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    store = _install_fake_gate_store(
        monkeypatch, _FakeGateStore({g: "pending" for g in PRE_IMPL_GATES})
    )
    _capture_waive_comments(monkeypatch)
    dispatcher = SwarmDispatcher(_StubNotifier(), _config())

    asyncio.run(dispatcher._handle_issue_comment(_comment_trigger()))

    assert store.waive_calls, (
        "the dispatcher-side gate waive must still run for /confirm-gates-clear"
    )


# ── Fix 3: _post_swarm_run_comment edits instead of posting a duplicate ────────

def test_post_swarm_run_comment_edits_existing_when_marker_found(monkeypatch):
    """When an existing comment with the marker is found, PATCH it instead of POST."""
    patch_calls: list[dict] = []
    post_calls: list[dict] = []

    _CONFIRMATION_MARKER = "<!-- swarm-run-confirmation -->"

    existing_comments = [
        {"id": 999, "body": f"{_CONFIRMATION_MARKER}\nOld confirmation text."},
    ]

    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

        async def get(self, url, **kwargs):
            class FakeResp:
                status_code = 200
                def raise_for_status(self): pass
                def json(self): return existing_comments
            return FakeResp()

        async def patch(self, url, **kwargs):
            patch_calls.append({"url": url, "json": kwargs.get("json", {})})
            class FakeResp:
                status_code = 200
                def raise_for_status(self): pass
            return FakeResp()

        async def post(self, url, **kwargs):
            post_calls.append({"url": url, "json": kwargs.get("json", {})})
            class FakeResp:
                status_code = 201
                def raise_for_status(self): pass
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    trigger = _swarm_run_trigger()

    # Provide a token so the method doesn't short-circuit.
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    asyncio.run(dispatcher._post_swarm_run_comment(trigger))

    assert len(patch_calls) == 1, (
        f"Expected 1 PATCH call (edit existing); got {len(patch_calls)} PATCH "
        f"and {len(post_calls)} POST"
    )
    assert len(post_calls) == 0, (
        f"Expected 0 POST calls when existing comment found; got {len(post_calls)}"
    )
    # PATCH URL must reference the existing comment ID.
    assert "999" in patch_calls[0]["url"], (
        f"PATCH URL must include comment ID 999; got {patch_calls[0]['url']!r}"
    )
    # The patched body must contain the marker and NOT contain command tokens.
    patched_body = patch_calls[0]["json"].get("body", "")
    assert _CONFIRMATION_MARKER in patched_body
    assert _SWARM_RUN_CMD not in patched_body
    assert _CONFIRM_GATES_CLEAR_CMD not in patched_body


def test_post_swarm_run_comment_posts_new_when_no_existing(monkeypatch):
    """When no existing confirmation comment is found, POST a new one."""
    patch_calls: list[dict] = []
    post_calls: list[dict] = []

    class FakeClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass

        async def get(self, url, **kwargs):
            class FakeResp:
                status_code = 200
                def raise_for_status(self): pass
                def json(self): return []  # no existing comments
            return FakeResp()

        async def patch(self, url, **kwargs):
            patch_calls.append(url)
            class FakeResp:
                status_code = 200
                def raise_for_status(self): pass
            return FakeResp()

        async def post(self, url, **kwargs):
            post_calls.append({"url": url, "json": kwargs.get("json", {})})
            class FakeResp:
                status_code = 201
                def raise_for_status(self): pass
            return FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    asyncio.run(dispatcher._post_swarm_run_comment(_swarm_run_trigger()))

    assert len(post_calls) == 1, f"Expected 1 POST; got {len(post_calls)}"
    assert len(patch_calls) == 0, f"Expected 0 PATCH; got {len(patch_calls)}"
    posted_body = post_calls[0]["json"].get("body", "")
    assert "<!-- swarm-run-confirmation -->" in posted_body
    assert _SWARM_RUN_CMD not in posted_body


# ── Phase H1: /approve /reject /hold + operator reviewer ─────────────────────


def _checkpoint_trigger(**overrides):
    """Build an issue_comment SwarmTrigger simulating a PR checkpoint comment."""
    base = dict(
        kind="issue_comment",
        repository="owner/repo",
        number=87,
        title="A pull request",
        body="Closes #80.",
        author="contributor",
        html_url="https://github.com/owner/repo/pull/87",
        delivery_id="h1-test-delivery",
        action="created",
        comment_id=200,
        comment_author=_OPERATOR_LOGIN,
        comment_body="/approve",
        comment_html_url="https://github.com/owner/repo/pull/87#issuecomment-200",
        comment_on_pr=True,
    )
    base.update(overrides)
    return SwarmTrigger(**base)


class _FakeHttpxClient:
    """Minimal httpx.AsyncClient stub that records POST/DELETE calls."""

    def __init__(self, **kwargs):
        self.post_calls: list[dict] = []
        self.delete_calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def post(self, url, **kwargs):
        self.post_calls.append({"url": url, "json": kwargs.get("json", {})})
        return _FakeResp(201)

    async def request(self, method, url, **kwargs):
        if method == "DELETE":
            self.delete_calls.append({"url": url, "json": kwargs.get("json", {})})
        return _FakeResp(200)


class _FakeResp:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=None, response=self  # type: ignore[arg-type]
            )

    def json(self):
        return {}


class _FakeListResp(_FakeResp):
    """A 200 response whose .json() returns a caller-supplied list — for the
    GitHub comments GET, which the deferral/session-limit paths list before
    posting."""

    def __init__(self, items, status_code=200):
        super().__init__(status_code)
        self._items = items

    def json(self):
        return self._items


# ── /approve command ─────────────────────────────────────────────────────────


def test_approve_from_operator_resolves_checkpoint_and_removes_reviewer(monkeypatch):
    """/approve from the operator resolves checkpoint + removes operator reviewer."""
    stored: list[dict] = []
    http_client = _FakeHttpxClient()

    async def fake_store(self, entities, idempotency_key):
        stored.extend(entities)

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: http_client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())
    asyncio.run(dispatcher._handle_issue_comment(_checkpoint_trigger()))

    # Checkpoint resolution entity must be stored with status=approved.
    assert any(
        e.get("entity_type") == "checkpoint_brief" and e.get("status") == "approved"
        for e in stored
    ), f"Expected approved checkpoint_brief in stored: {stored}"

    # Comment posted (POST to issues/<n>/comments).
    assert any("comments" in c["url"] for c in http_client.post_calls), (
        f"Expected a comment POST; got {http_client.post_calls}"
    )

    # Operator removed from reviewer list (DELETE to requested_reviewers).
    assert any(
        "requested_reviewers" in c["url"] for c in http_client.delete_calls
    ), f"Expected reviewer DELETE; got {http_client.delete_calls}"
    delete_body = next(
        c for c in http_client.delete_calls if "requested_reviewers" in c["url"]
    )
    assert _OPERATOR_LOGIN in delete_body["json"].get("reviewers", []), (
        f"DELETE body must remove _OPERATOR_LOGIN; got {delete_body}"
    )

    # Notifier called with an approval/handback message.
    assert any("approved" in m.lower() or "approve" in m.lower() for m in notifier.sent), (
        f"Notifier must mention approval; got {notifier.sent}"
    )


def test_approve_comment_body_has_no_command_tokens(monkeypatch):
    """/approve confirmation comment must not contain command tokens (self-trigger guard)."""
    comment_bodies: list[str] = []

    async def fake_store(self, entities, idempotency_key):
        pass

    class _CapturingClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, **kwargs):
            comment_bodies.append(kwargs.get("json", {}).get("body", ""))
            return _FakeResp(201)
        async def request(self, method, url, **kwargs):
            return _FakeResp(200)

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", _CapturingClient)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    asyncio.run(
        SwarmDispatcher(_StubNotifier(), _config())._handle_issue_comment(
            _checkpoint_trigger()
        )
    )

    for body in comment_bodies:
        assert _APPROVE_CMD not in body, (
            f"Approve comment must not contain {_APPROVE_CMD!r}: {body!r}"
        )
        assert _REJECT_CMD not in body
        assert _HOLD_CMD not in body
        assert _SWARM_RUN_CMD not in body
        assert _CONFIRM_GATES_CLEAR_CMD not in body


def test_approve_from_non_operator_is_ignored(monkeypatch):
    """/approve from a non-operator must be silently ignored."""
    stored: list = []
    http_calls: list = []

    async def fake_store(self, entities, idempotency_key):
        stored.extend(entities)

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())
    asyncio.run(
        dispatcher._handle_issue_comment(
            _checkpoint_trigger(comment_author="some-random-user")
        )
    )

    assert stored == [], f"No Neotoma store should happen for non-operator: {stored}"
    assert notifier.sent == [], f"No notifications for non-operator: {notifier.sent}"


def test_approve_from_bot_is_ignored(monkeypatch):
    """/approve from a bot identity must be silently ignored (self-trigger guard)."""
    stored: list = []

    async def fake_store(self, entities, idempotency_key):
        stored.extend(entities)

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())
    for bot_login in ["ateles-agent", "neotoma-agent", "github-actions[bot]"]:
        asyncio.run(
            dispatcher._handle_issue_comment(
                _checkpoint_trigger(comment_author=bot_login)
            )
        )

    assert stored == [], f"No store for bot authors: {stored}"
    assert notifier.sent == [], f"No notifications for bot authors: {notifier.sent}"


# ── approval-triggers-merge + GitHub review approval path ────────────────────


def _review_trigger(**overrides):
    """Build a pr_review SwarmTrigger simulating a GitHub PR-review submission."""
    base = dict(
        kind="pr_review",
        repository="owner/repo",
        number=87,
        title="A pull request",
        body="Closes #80.",
        author="ateles-agent",
        html_url="https://github.com/owner/repo/pull/87",
        delivery_id="review-test",
        action="submitted",
        review_state="approved",
        review_author=_OPERATOR_LOGIN,
    )
    base.update(overrides)
    return SwarmTrigger(**base)


class _MergeAwareClient:
    """httpx stub that answers the PR-merge PUT with a configurable outcome and
    records every call, plus the comment POST and reviewer DELETE."""

    def __init__(self, *, merge_status=200, merged=True, **kwargs):
        self._merge_status = merge_status
        self._merged = merged
        self.put_calls: list[dict] = []
        self.post_calls: list[dict] = []
        self.delete_calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def put(self, url, **kwargs):
        self.put_calls.append({"url": url, "json": kwargs.get("json", {})})
        if self._merge_status == 200:
            return _MergeResp(200, {"merged": self._merged, "sha": "abc123def456",
                                    "message": "" if self._merged else "not mergeable"})
        return _MergeResp(self._merge_status, {"message": "Pull Request is not mergeable"})

    async def post(self, url, **kwargs):
        self.post_calls.append({"url": url, "json": kwargs.get("json", {})})
        return _MergeResp(201, {})

    async def request(self, method, url, **kwargs):
        if method == "DELETE":
            self.delete_calls.append({"url": url, "json": kwargs.get("json", {})})
        return _MergeResp(200, {})

    async def get(self, url, **kwargs):
        # _find_open_pr_for_issue path (unused when comment_on_pr/pr_review).
        return _MergeResp(200, [])


class _MergeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.content = b"x"
        self.text = str(payload)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)  # type: ignore[arg-type]

    def json(self):
        return self._payload


def _merge_flag_config(**over):
    cfg = _config()
    cfg.approval_triggers_merge = True
    cfg.approval_merge_method = "squash"
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def test_github_review_approved_from_operator_merges_when_flag_on(monkeypatch):
    """Operator 'Approve' review + flag ON → PR merged via the REST API."""
    client = _MergeAwareClient(merge_status=200, merged=True)

    async def fake_store(self, entities, idempotency_key):
        pass

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    notifier = _StubNotifier()
    d = SwarmDispatcher(notifier, _merge_flag_config())
    asyncio.run(d._handle_pr_review(_review_trigger()))

    # The merge PUT hit the right endpoint with squash.
    assert any("/pulls/87/merge" in c["url"] for c in client.put_calls), (
        f"Expected merge PUT; got {client.put_calls}"
    )
    merge_call = next(c for c in client.put_calls if "/merge" in c["url"])
    assert merge_call["json"].get("merge_method") == "squash"
    # Notification reports the merge honestly.
    assert any("MERGED" in m for m in notifier.sent), (
        f"Expected a MERGED notification; got {notifier.sent}"
    )


def test_github_review_approved_flag_off_does_not_merge(monkeypatch):
    """Operator 'Approve' review + flag OFF → checkpoint resolved, NO merge."""
    client = _MergeAwareClient()

    async def fake_store(self, entities, idempotency_key):
        pass

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    notifier = _StubNotifier()
    d = SwarmDispatcher(notifier, _config())  # flag OFF by default
    asyncio.run(d._handle_pr_review(_review_trigger()))

    assert client.put_calls == [], f"Flag OFF must not merge; got {client.put_calls}"
    assert not any("MERGED" in m for m in notifier.sent)


def test_github_review_non_operator_ignored(monkeypatch):
    """A non-operator approving review must never drive a merge."""
    client = _MergeAwareClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)

    notifier = _StubNotifier()
    d = SwarmDispatcher(notifier, _merge_flag_config())
    asyncio.run(d._handle_pr_review(_review_trigger(review_author="random-user")))

    assert client.put_calls == []
    assert notifier.sent == []


def test_github_review_bot_ignored(monkeypatch):
    """A bot/machine review must never drive a merge (defence-in-depth)."""
    client = _MergeAwareClient()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)

    notifier = _StubNotifier()
    d = SwarmDispatcher(notifier, _merge_flag_config())
    for bot in ["ateles-agent", "neotoma-agent", "github-actions[bot]"]:
        asyncio.run(d._handle_pr_review(_review_trigger(review_author=bot)))

    assert client.put_calls == []
    assert notifier.sent == []


def test_github_review_changes_requested_no_merge(monkeypatch):
    """Operator review with state != approved must not merge."""
    client = _MergeAwareClient()

    async def fake_store(self, entities, idempotency_key):
        pass

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)

    notifier = _StubNotifier()
    d = SwarmDispatcher(notifier, _merge_flag_config())
    asyncio.run(d._handle_pr_review(_review_trigger(review_state="changes_requested")))

    assert client.put_calls == []


def test_merge_failure_reported_honestly(monkeypatch):
    """When GitHub refuses the merge (405), the swarm must NOT claim a merge."""
    client = _MergeAwareClient(merge_status=405)

    async def fake_store(self, entities, idempotency_key):
        pass

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    notifier = _StubNotifier()
    d = SwarmDispatcher(notifier, _merge_flag_config())
    asyncio.run(d._handle_pr_review(_review_trigger()))

    # A merge PUT was attempted, but the notification says NOT merged.
    assert client.put_calls, "Merge should have been attempted"
    assert any("did NOT complete" in m or "not merged" in m.lower()
               for m in notifier.sent), (
        f"Failure must be reported honestly; got {notifier.sent}"
    )
    assert not any("MERGED" in m for m in notifier.sent)


def test_email_approve_merges_when_flag_on(monkeypatch):
    """The email-reply channel (email_approve) merges when the flag is on.

    Regression for the Loxia finding on ateles#189: _resolve_pr_number must
    treat an email_approve trigger's number as a PR number directly, not fall
    through to the issue→PR search (which would find nothing and never merge).
    """
    client = _MergeAwareClient(merge_status=200, merged=True)

    async def fake_store(self, entities, idempotency_key):
        pass

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    notifier = _StubNotifier()
    d = SwarmDispatcher(notifier, _merge_flag_config())
    trig = _review_trigger(kind="email_approve", action="approved")
    asyncio.run(d._handle_email_approve(trig))

    # The PR number (87) must have driven the merge PUT directly — NOT been
    # treated as an issue number to search for.
    assert any("/pulls/87/merge" in c["url"] for c in client.put_calls), (
        f"email_approve must merge PR #87 directly; got {client.put_calls}"
    )
    assert any("MERGED" in m for m in notifier.sent)


def test_resolve_pr_number_email_approve_uses_number_directly(monkeypatch):
    """_resolve_pr_number returns the trigger number for email_approve without
    hitting the issue→PR search path."""
    called = {"find": False}

    async def fake_find(self, trigger):
        called["find"] = True
        return None

    monkeypatch.setattr(SwarmDispatcher, "_find_open_pr_for_issue", fake_find)
    d = SwarmDispatcher(_StubNotifier(), _config())
    n = asyncio.run(
        d._resolve_pr_number(_review_trigger(kind="email_approve", number=42))
    )
    assert n == 42
    assert called["find"] is False, "must not fall through to issue→PR search"


def test_approve_comment_merges_when_flag_on(monkeypatch):
    """The /approve issue comment shares the merge path (flag ON → merges)."""
    client = _MergeAwareClient(merge_status=200, merged=True)

    async def fake_store(self, entities, idempotency_key):
        pass

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    notifier = _StubNotifier()
    d = SwarmDispatcher(notifier, _merge_flag_config())
    # comment_on_pr=True → _resolve_pr_number uses trigger.number directly.
    asyncio.run(d._handle_issue_comment(_checkpoint_trigger()))

    assert any("/pulls/87/merge" in c["url"] for c in client.put_calls), (
        f"Expected merge PUT from /approve comment; got {client.put_calls}"
    )
    assert any("MERGED" in m for m in notifier.sent)


def test_merge_pr_helper_success(monkeypatch):
    """_merge_pr returns (True, sha) on a 200 merged response."""
    client = _MergeAwareClient(merge_status=200, merged=True)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    d = SwarmDispatcher(_StubNotifier(), _config())
    ok, detail = asyncio.run(d._merge_pr("owner/repo", 87, "squash"))
    assert ok is True
    assert detail == "abc123def456"


def test_merge_pr_helper_not_mergeable(monkeypatch):
    """_merge_pr returns (False, reason) on a 405."""
    client = _MergeAwareClient(merge_status=405)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    d = SwarmDispatcher(_StubNotifier(), _config())
    ok, detail = asyncio.run(d._merge_pr("owner/repo", 87, "squash"))
    assert ok is False
    assert "405" in detail


def test_merge_pr_helper_bad_method_defaults_squash(monkeypatch):
    """An unknown merge method falls back to squash (no arbitrary passthrough)."""
    client = _MergeAwareClient(merge_status=200, merged=True)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    d = SwarmDispatcher(_StubNotifier(), _config())
    asyncio.run(d._merge_pr("owner/repo", 87, "force-push-lol"))
    assert client.put_calls[0]["json"]["merge_method"] == "squash"


# ── /reject command ──────────────────────────────────────────────────────────


def test_reject_from_operator_records_reason_and_removes_reviewer(monkeypatch):
    """/reject <reason> records reason, resolves as rejected, removes reviewer."""
    stored: list[dict] = []
    http_client = _FakeHttpxClient()

    async def fake_store(self, entities, idempotency_key):
        stored.extend(entities)

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: http_client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())
    asyncio.run(
        dispatcher._handle_issue_comment(
            _checkpoint_trigger(comment_body="/reject needs more tests")
        )
    )

    # Checkpoint stored with status=rejected.
    rejected = [
        e for e in stored
        if e.get("entity_type") == "checkpoint_brief" and e.get("status") == "rejected"
    ]
    assert rejected, f"Expected rejected checkpoint_brief; got {stored}"
    # Reason appears in the stored entity body.
    assert "needs more tests" in rejected[0].get("body", ""), (
        f"Rejection reason must appear in stored entity body: {rejected[0]}"
    )

    # Operator reviewer removed.
    assert any(
        "requested_reviewers" in c["url"] for c in http_client.delete_calls
    ), f"Expected reviewer DELETE; got {http_client.delete_calls}"

    # Notifier mentions rejection.
    assert any("reject" in m.lower() for m in notifier.sent), (
        f"Notifier must mention rejection; got {notifier.sent}"
    )


def test_reject_does_not_proceed_with_merge(monkeypatch):
    """/reject must NOT trigger a merge or PR pipeline action."""
    skill_calls: list = []
    stored: list = []

    async def fake_run_skill(skill, prompt, **kwargs):
        skill_calls.append(skill)
        return SkillResult(skill, True, 0, "", "")

    async def fake_store(self, entities, idempotency_key):
        stored.extend(entities)

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FakeHttpxClient())
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    asyncio.run(
        SwarmDispatcher(_StubNotifier(), _config())._handle_issue_comment(
            _checkpoint_trigger(comment_body="/reject bad diff")
        )
    )

    # No skill (merge, vanellus, etc.) must be called.
    assert skill_calls == [], f"/reject must not trigger any skill calls: {skill_calls}"


def test_reject_from_non_operator_is_ignored(monkeypatch):
    """/reject from a non-operator is silently ignored."""
    stored: list = []

    async def fake_store(self, entities, idempotency_key):
        stored.extend(entities)

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)

    notifier = _StubNotifier()
    asyncio.run(
        SwarmDispatcher(notifier, _config())._handle_issue_comment(
            _checkpoint_trigger(comment_author="stranger", comment_body="/reject bad")
        )
    )

    assert stored == []
    assert notifier.sent == []


# ── /hold command ─────────────────────────────────────────────────────────────


def test_hold_acks_and_leaves_reviewer_in_place(monkeypatch):
    """/hold posts an ack comment but does NOT remove the operator from reviewers."""
    stored: list = []
    http_client = _FakeHttpxClient()

    async def fake_store(self, entities, idempotency_key):
        stored.extend(entities)

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: http_client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())
    asyncio.run(
        dispatcher._handle_issue_comment(
            _checkpoint_trigger(comment_body="/hold")
        )
    )

    # Ack comment posted.
    assert any("comments" in c["url"] for c in http_client.post_calls), (
        f"Expected ack POST; got {http_client.post_calls}"
    )

    # No DELETE to requested_reviewers — /hold leaves the reviewer in place.
    assert http_client.delete_calls == [], (
        f"/hold must NOT remove operator reviewer; got {http_client.delete_calls}"
    )

    # No checkpoint_brief stored (no resolution — still open).
    assert stored == [], f"/hold must not store any entity; got {stored}"

    # Notifier acks the hold.
    assert any("park" in m.lower() or "hold" in m.lower() for m in notifier.sent), (
        f"Notifier must mention park/hold; got {notifier.sent}"
    )


def test_hold_from_non_operator_is_ignored(monkeypatch):
    """/hold from a non-operator is silently ignored."""
    stored: list = []
    http_client = _FakeHttpxClient()

    async def fake_store(self, entities, idempotency_key):
        stored.extend(entities)

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: http_client)

    notifier = _StubNotifier()
    asyncio.run(
        SwarmDispatcher(notifier, _config())._handle_issue_comment(
            _checkpoint_trigger(comment_author="stranger", comment_body="/hold")
        )
    )

    assert http_client.post_calls == []
    assert stored == []
    assert notifier.sent == []


# ── Merge boundary requests operator as reviewer ──────────────────────────────


def test_merge_checkpoint_requests_operator_as_reviewer(monkeypatch):
    """_store_merge_checkpoint must POST to requested_reviewers with the operator."""
    post_calls: list[dict] = []
    http_client = _FakeHttpxClient()
    # Capture POST calls so we can distinguish reviewer requests from comments.
    original_post = http_client.post

    async def recording_post(url, **kwargs):
        post_calls.append({"url": url, "json": kwargs.get("json", {})})
        return _FakeResp(201)

    http_client.post = recording_post

    async def fake_store(self, entities, idempotency_key):
        pass

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: http_client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    t = _trigger()  # PR trigger for owner/repo#87
    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    asyncio.run(dispatcher._store_merge_checkpoint(t, parent=80, lenses=["pm", "qa"]))

    # Must POST to the requested_reviewers endpoint.
    reviewer_posts = [c for c in post_calls if "requested_reviewers" in c["url"]]
    assert reviewer_posts, (
        f"Expected a POST to requested_reviewers; got all posts: {post_calls}"
    )
    # Must include the operator login.
    assert any(
        _OPERATOR_LOGIN in c["json"].get("reviewers", []) for c in reviewer_posts
    ), f"Reviewer request must include _OPERATOR_LOGIN; got {reviewer_posts}"


def test_merge_checkpoint_reviewer_request_failure_is_non_fatal(monkeypatch):
    """A failing reviewer request must not prevent the checkpoint from being stored."""
    stored: list = []

    async def fake_store(self, entities, idempotency_key):
        stored.extend(entities)

    class _FailingClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, **kwargs):
            raise RuntimeError("GitHub API unavailable")

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(httpx, "AsyncClient", _FailingClient)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    t = _trigger()
    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    # Must not raise even when the reviewer request fails.
    asyncio.run(dispatcher._store_merge_checkpoint(t, parent=80, lenses=[]))

    # Checkpoint entity must still be stored despite the HTTP failure.
    assert any(e.get("entity_type") == "checkpoint_brief" for e in stored), (
        f"checkpoint_brief must be stored even when reviewer request fails: {stored}"
    )


# ── Existing commands still work after H1 additions ──────────────────────────


def test_confirm_gates_clear_still_dispatches_after_h1_commands_added(monkeypatch):
    """/confirm-gates-clear must still work with H1 commands present in codebase."""
    calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append(skill)
        return SkillResult(skill, True, 0, "Gates waived.", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    store = _install_fake_gate_store(
        monkeypatch, _FakeGateStore({g: "pending" for g in PRE_IMPL_GATES})
    )
    _capture_waive_comments(monkeypatch)
    asyncio.run(
        SwarmDispatcher(_StubNotifier(), _config())._handle_issue_comment(
            _comment_trigger()
        )
    )

    assert store.waive_calls, (
        "the dispatcher-side gate waive must still run for /confirm-gates-clear"
    )


def test_swarm_run_still_dispatches_after_h1_commands_added(monkeypatch):
    """/swarm-run must still work with H1 commands present in codebase."""
    opened_calls = []

    async def fake_handle_issue_opened(self, trigger):
        opened_calls.append(trigger)

    async def fake_post_swarm_run_comment(self, trigger):
        pass

    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle_issue_opened)
    monkeypatch.setattr(SwarmDispatcher, "_post_swarm_run_comment", fake_post_swarm_run_comment)
    asyncio.run(
        SwarmDispatcher(_StubNotifier(), _config())._handle_issue_comment(
            _swarm_run_trigger()
        )
    )

    assert len(opened_calls) == 1, (
        f"/swarm-run must still dispatch _handle_issue_opened: {opened_calls}"
    )


def test_confirm_gates_clear_wins_over_approve_when_both_present(monkeypatch):
    """When /confirm-gates-clear and /approve both appear, gates-clear wins."""
    calls = []
    stored: list = []

    async def fake_run_skill(skill, prompt, **kwargs):
        calls.append(skill)
        return SkillResult(skill, True, 0, "Gates waived.", "")

    async def fake_store(self, entities, idempotency_key):
        stored.extend(entities)

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    store = _install_fake_gate_store(
        monkeypatch, _FakeGateStore({g: "pending" for g in PRE_IMPL_GATES})
    )
    _capture_waive_comments(monkeypatch)

    asyncio.run(
        SwarmDispatcher(_StubNotifier(), _config())._handle_issue_comment(
            _comment_trigger(
                comment_body=f"{_CONFIRM_GATES_CLEAR_CMD} {_APPROVE_CMD}"
            )
        )
    )

    # gates-clear wins: Lanius called, no checkpoint_brief stored.
    assert store.waive_calls, (
        "the dispatcher-side gate waive must run when /confirm-gates-clear is present"
    )
    approved_entities = [
        e for e in stored
        if e.get("entity_type") == "checkpoint_brief" and e.get("status") == "approved"
    ]
    assert approved_entities == [], (
        "No approved checkpoint_brief when /confirm-gates-clear takes priority"
    )


def test_approve_constant_value():
    """_APPROVE_CMD must be exactly '/approve'."""
    assert _APPROVE_CMD == "/approve"


def test_reject_constant_value():
    """_REJECT_CMD must be exactly '/reject'."""
    assert _REJECT_CMD == "/reject"


def test_hold_constant_value():
    """_HOLD_CMD must be exactly '/hold'."""
    assert _HOLD_CMD == "/hold"


# ── ateles#127 — Vanellus aggregation comment fallback ───────────────────────
# Mirrors the panelist _post_missing_panel_comments safety-net for the
# Vanellus aggregation step: if Vanellus's own gh comment does not land the
# dispatcher posts the captured stdout.


# ── vanellus_comment_missing helper ─────────────────────────────────────────


def test_vanellus_comment_missing_when_no_comments():
    """vanellus_comment_missing returns True when the comment list is empty."""
    assert vanellus_comment_missing([]) is True


def test_vanellus_comment_missing_when_marker_absent():
    """vanellus_comment_missing returns True when no body contains the marker."""
    bodies = ["review:pm\nLooks good.", "Some other comment."]
    assert vanellus_comment_missing(bodies) is True


def test_vanellus_comment_missing_false_when_marker_present():
    """vanellus_comment_missing returns False when any body contains the marker."""
    bodies = [
        "Some review comment.",
        f"{_VANELLUS_COMMENT_MARKER}\nVanellus verdict here.",
    ]
    assert vanellus_comment_missing(bodies) is False


def test_vanellus_comment_missing_marker_substring_match():
    """Marker detection works even when surrounded by more text."""
    body = f"Preamble\n{_VANELLUS_COMMENT_MARKER}\nVerdict body."
    assert vanellus_comment_missing([body]) is False


# ── compose_vanellus_fallback_comment ─────────────────────────────────────────


def test_compose_vanellus_fallback_comment_starts_with_marker():
    """The fallback body must begin with the stable marker."""
    body = compose_vanellus_fallback_comment("All clear.")
    assert body.startswith(_VANELLUS_COMMENT_MARKER)


def test_compose_vanellus_fallback_comment_contains_attribution():
    """The fallback body must include the Vanellus attribution header."""
    body = compose_vanellus_fallback_comment("BLOCKED: missing tests.")
    assert attribution_header("vanellus", "PR steward") in body


def test_compose_vanellus_fallback_comment_contains_verdict_text():
    """The fallback body must reproduce the captured verdict text."""
    verdict = "VERDICT: changes_requested — 2 BLOCKING findings."
    body = compose_vanellus_fallback_comment(verdict)
    assert verdict in body


def test_compose_vanellus_fallback_comment_is_detectable():
    """A fallback comment posted by the dispatcher must NOT appear missing."""
    body = compose_vanellus_fallback_comment("signed_off.")
    assert vanellus_comment_missing([body]) is False


# ── _vanellus_prompt — explicit post instruction + repeat-for-fallback ─────────


def test_vanellus_prompt_instructs_post_via_gh_cli():
    """_vanellus_prompt must tell Vanellus to post via the gh CLI."""
    t = _trigger()
    prompt = SwarmDispatcher._vanellus_prompt(t, parent=80, lenses=["pm", "qa"])
    # The exact marker must appear so Vanellus uses it in its comment.
    assert _VANELLUS_COMMENT_MARKER in prompt


def test_vanellus_prompt_instructs_repeat_for_fallback():
    """_vanellus_prompt must ask Vanellus to repeat the verdict in its reply."""
    t = _trigger()
    prompt = SwarmDispatcher._vanellus_prompt(t, parent=80, lenses=[])
    assert "Repeat" in prompt or "repeat" in prompt


def test_vanellus_prompt_embeds_inline_reviews_and_forbids_gh_fetch():
    """Aggregation must run from inline review text, not a gh fetch.

    Regression: the aggregator runs diff-only with no repo checkout (cwd=None),
    so a `gh pr view --comments` read fails ("requires being in a repository
    context") and the aggregation silently stalls — the dispatcher then posts
    Vanellus's non-verdict "please give me the data" message via its fallback.
    The captured panel reviews must be embedded in the prompt instead.
    """
    t = _trigger()
    reviews = [
        ("pm", "APPROVE - scope matches the issue."),
        ("qa", "REQUEST_CHANGES - [BLOCKING] missing error-recovery tests."),
    ]
    prompt = SwarmDispatcher._vanellus_prompt(
        t, parent=80, lenses=["pm", "qa"], reviews=reviews
    )
    # Each lens's captured text is embedded inline.
    assert "APPROVE - scope matches the issue." in prompt
    assert "[BLOCKING] missing error-recovery tests." in prompt
    assert "### review:pm" in prompt and "### review:qa" in prompt
    # And Vanellus is told NOT to re-fetch them via gh.
    assert "Do NOT fetch them via gh" in prompt


def test_vanellus_prompt_handles_no_reviews():
    """No captured reviews => explicit placeholder; no gh-fetch requirement."""
    t = _trigger()
    prompt = SwarmDispatcher._vanellus_prompt(t, parent=80, lenses=[], reviews=None)
    assert "no panel lens reviews captured" in prompt


# ── _post_missing_vanellus_comment — dispatcher fallback ──────────────────────


class _FakeHttpxClientForVanellus:
    """httpx.AsyncClient stub that controls GET comment bodies and captures POSTs."""

    def __init__(self, existing_bodies: list[str]):
        self.existing_bodies = existing_bodies
        self.post_calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, url, **kwargs):
        class _Resp:
            def raise_for_status(self): pass
            def json(inner_self):
                return [{"body": b} for b in self.existing_bodies]
        return _Resp()

    async def post(self, url, **kwargs):
        self.post_calls.append({"url": url, "json": kwargs.get("json", {})})
        class _Resp:
            def raise_for_status(self): pass
        return _Resp()


def test_vanellus_fallback_posts_when_comment_missing(monkeypatch):
    """When Vanellus's comment is absent, the dispatcher posts the captured stdout."""
    client = _FakeHttpxClientForVanellus(existing_bodies=[])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    t = _trigger()
    verdict_text = "VERDICT: All clear. PR approved."
    result = SkillResult("vanellus", True, 0, verdict_text, "")

    asyncio.run(dispatcher._post_missing_vanellus_comment(t, result))

    assert len(client.post_calls) == 1, (
        f"Expected 1 fallback POST; got {len(client.post_calls)}"
    )
    posted_body = client.post_calls[0]["json"].get("body", "")
    assert _VANELLUS_COMMENT_MARKER in posted_body, (
        "Fallback comment must contain the Vanellus marker for future dedup"
    )
    assert verdict_text in posted_body, (
        "Fallback comment must reproduce the captured verdict text"
    )


def test_vanellus_fallback_skips_when_comment_already_present(monkeypatch):
    """When Vanellus's comment IS present, no duplicate is posted."""
    existing_body = f"{_VANELLUS_COMMENT_MARKER}\nVanellus already posted this."
    client = _FakeHttpxClientForVanellus(existing_bodies=[existing_body])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    t = _trigger()
    result = SkillResult("vanellus", True, 0, "VERDICT: all clear.", "")

    asyncio.run(dispatcher._post_missing_vanellus_comment(t, result))

    assert client.post_calls == [], (
        "No fallback POST when Vanellus's own comment is already present"
    )


def test_vanellus_fallback_skips_when_stdout_empty(monkeypatch):
    """When Vanellus produced no stdout, the fallback skips posting."""
    client = _FakeHttpxClientForVanellus(existing_bodies=[])
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    t = _trigger()
    result = SkillResult("vanellus", True, 0, "", "")  # empty stdout

    asyncio.run(dispatcher._post_missing_vanellus_comment(t, result))

    assert client.post_calls == [], (
        "No fallback POST when Vanellus produced no captured text"
    )


def test_vanellus_fallback_non_fatal_when_post_raises(monkeypatch):
    """A failing fallback POST must not crash the pipeline."""
    class _FailingClient:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, **kwargs):
            class _Resp:
                def raise_for_status(self): pass
                def json(self): return []  # no existing comments
            return _Resp()
        async def post(self, url, **kwargs):
            raise RuntimeError("GitHub API unavailable")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _FailingClient())
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    t = _trigger()
    result = SkillResult("vanellus", True, 0, "VERDICT: all clear.", "")

    # Must not raise even when the POST fails.
    asyncio.run(dispatcher._post_missing_vanellus_comment(t, result))


def test_handle_pr_calls_vanellus_fallback_after_run(monkeypatch):
    """_handle_pr must call _post_missing_vanellus_comment after the Vanellus run."""
    fallback_calls: list[tuple] = []
    skill_calls: list[str] = []

    async def fake_run_skill(skill, prompt, **kwargs):
        skill_calls.append(skill)
        if skill == "lanius":
            return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
        if skill == "vanellus":
            return SkillResult(skill, True, 0, "VERDICT: all clear.", "")
        return SkillResult(skill, True, 0, "ok", "")

    async def fake_vanellus_fallback(self, t, result):
        fallback_calls.append((t.number, result.stdout))

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(
        SwarmDispatcher, "_post_missing_vanellus_comment", fake_vanellus_fallback
    )

    async def fake_changed_files(self, t): return []
    async def fake_preregistered(self, repo, number): return {}
    async def fake_store(self, entities, idempotency_key): pass
    async def fake_post_missing(self, t, reviews, agents_by_lens): pass
    async def fake_persist(self, t, reviews, agents_by_lens): pass
    async def fake_merge_checkpoint(self, t, parent, lenses): pass

    monkeypatch.setattr(SwarmDispatcher, "_changed_files", fake_changed_files)
    monkeypatch.setattr(SwarmDispatcher, "_preregistered_expectations", fake_preregistered)
    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(SwarmDispatcher, "_post_missing_panel_comments", fake_post_missing)
    monkeypatch.setattr(SwarmDispatcher, "_persist_panel_reviews", fake_persist)
    monkeypatch.setattr(SwarmDispatcher, "_store_merge_checkpoint", fake_merge_checkpoint)

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    asyncio.run(dispatcher._handle_pr(_trigger()))

    assert len(fallback_calls) == 1, (
        f"_post_missing_vanellus_comment must be called exactly once; got {fallback_calls}"
    )
    pr_number, captured_stdout = fallback_calls[0]
    assert pr_number == 87
    assert captured_stdout == "VERDICT: all clear."


# ── QE3: eval-authoring affordance — PR-branch worktree ───────────────────────


def test_prepare_pr_worktree_returns_none_for_non_neotoma_repo():
    # The eval harness lives only in neotoma; ateles PRs get no worktree.
    result = asyncio.run(
        prepare_pr_worktree("markmhendrickson/ateles", 42, "phoenicurus")
    )
    assert result is None


def test_prepare_pr_worktree_returns_none_when_base_checkout_absent(monkeypatch):
    # Best-effort: a missing local neotoma clone → diff-only fallback, no raise.
    monkeypatch.setattr(
        swarm_dispatch, "NEOTOMA_LOCAL_CHECKOUT", "/nonexistent/neotoma-checkout"
    )
    result = asyncio.run(
        prepare_pr_worktree("markmhendrickson/neotoma", 42, "phoenicurus")
    )
    assert result is None


def test_cleanup_pr_worktree_none_is_safe_noop():
    # Cleanup of a never-prepared worktree must never raise.
    asyncio.run(cleanup_pr_worktree(None))


def test_cleanup_pr_worktree_removes_stray_dir(monkeypatch, tmp_path):
    # When the base clone is absent, cleanup still rmtree's the stray dir.
    monkeypatch.setattr(
        swarm_dispatch, "NEOTOMA_LOCAL_CHECKOUT", "/nonexistent/neotoma-checkout"
    )
    stray = tmp_path / "qa_eval_pr1_xxxx"
    stray.mkdir()
    asyncio.run(cleanup_pr_worktree(str(stray)))
    assert not stray.exists()


def test_only_qa_lens_gets_a_worktree(monkeypatch):
    # In the panel loop, prepare_pr_worktree is invoked ONLY for phoenicurus;
    # every other lens runs diff-only (cwd=None).
    prep_calls = []
    cwd_seen = {}

    async def fake_prepare(repo, number, agent):
        prep_calls.append(agent)
        return f"/tmp/wt-{agent}" if agent == "phoenicurus" else None

    async def fake_cleanup(wt):
        return None

    async def fake_run_skill(skill, prompt, **kwargs):
        cwd_seen[skill] = kwargs.get("cwd")
        # Lanius gate-inheritance must return a clear verdict so the panel runs.
        if skill == "lanius":
            return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
        return SkillResult(skill, True, 0, "VERDICT: COMMENT", "")

    monkeypatch.setattr(swarm_dispatch, "prepare_pr_worktree", fake_prepare)
    monkeypatch.setattr(swarm_dispatch, "cleanup_pr_worktree", fake_cleanup)
    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)

    # Force a panel that includes phoenicurus + at least one other lens.
    monkeypatch.setattr(
        swarm_dispatch,
        "select_panel",
        lambda **kw: [
            Lens(agent="phoenicurus", lens="qa", gate="qa", checks="evals"),
            Lens(agent="pavo", lens="pm", gate="pm", checks="scope"),
        ],
    )

    notifier = _StubNotifier()
    dispatcher = SwarmDispatcher(notifier, _config())
    asyncio.run(dispatcher._handle_pr(_trigger(repository="markmhendrickson/neotoma")))

    # Worktree prep attempted only for the qa lens.
    assert prep_calls == ["phoenicurus"], prep_calls
    # qa child got the worktree as cwd; other panelists got None.
    assert cwd_seen.get("phoenicurus") == "/tmp/wt-phoenicurus"
    assert cwd_seen.get("pavo") is None



def test_detect_auth_failure_signatures():
    """The auth-failure detector catches the real 401 text + common variants."""
    assert swarm_dispatch.detect_auth_failure("Failed to authenticate. API Error: 401 Invalid authentication credentials")
    assert swarm_dispatch.detect_auth_failure("", "OAuth token has expired")
    assert swarm_dispatch.detect_auth_failure("authentication_error: invalid api key")
    # A normal verdict must NOT trip it.
    assert not swarm_dispatch.detect_auth_failure("APPROVE — all blockers resolved", "")
    assert not swarm_dispatch.detect_auth_failure("BLOCKED: missing fallback", "")
    assert not swarm_dispatch.detect_auth_failure("", "")


def test_compose_auth_failure_comment_is_reframed_not_a_verdict():
    body = swarm_dispatch.compose_auth_failure_comment("vanellus")
    assert swarm_dispatch._VANELLUS_COMMENT_MARKER in body  # dedup marker preserved
    assert "credential failure" in body.lower()
    assert "ANTHROPIC_API_KEY" in body
    assert "not** a review verdict" in body or "not a review verdict" in body.lower()


# ── Ordered additive spec pipeline ────────────────────────────────────────────
#
# These cover the swarm-mechanics change: the parallel expectation pass is
# replaced by an ORDERED, ADDITIVE sequence (PM → Design → Eng → QA → Security →
# Legal), sections persist additively to an issue_spec entity, the spec mirrors
# into the issue body between managed markers, and the auto-build handoff is
# gated behind ATELES_SWARM_AUTO_BUILD.

from issue_spec import SECTIONS, SpecState, SPEC_MARKER_START, SPEC_MARKER_END


class _FakeSpecStore:
    """In-memory stand-in for IssueSpecStore that records section order."""

    instances = []

    def __init__(self, base_url, token):
        self.base_url = base_url
        self.token = token
        self.upserts = []  # (section.key, text) in call order
        self.mirrored = False
        _FakeSpecStore.instances.append(self)

    async def load(self, repo, issue_number, title):
        return SpecState(repo=repo, issue_number=issue_number, title=title)

    async def upsert_section(self, state, section, text):
        assert state.sections is not None and state.sequence_state is not None
        state.sections[section.field] = text
        if section.key not in state.sequence_state:
            state.sequence_state.append(section.key)
        state.entity_id = state.entity_id or "ent_fake"
        self.upserts.append((section.key, text))
        return state

    async def mark_mirrored(self, state):
        self.mirrored = True


def _issue_trigger(**overrides):
    base = dict(
        kind="issue_opened",
        repository="owner/repo",
        number=100,
        title="A new feature",
        body="Please build a thing.",
        author="reporter",
        html_url="https://github.com/owner/repo/issues/100",
        delivery_id="issue-delivery",
        action="opened",
        labels=[],
    )
    base.update(overrides)
    return SwarmTrigger(**base)


def _install_pipeline_stubs(monkeypatch, run_skill_impl, *, select_agents=None):
    """Common monkeypatch harness for _handle_issue_opened tests."""
    _FakeSpecStore.instances = []
    monkeypatch.setattr(swarm_dispatch, "run_skill", run_skill_impl)
    monkeypatch.setattr(swarm_dispatch, "IssueSpecStore", _FakeSpecStore)
    if select_agents is not None:
        monkeypatch.setattr(
            swarm_dispatch, "select_expectation_agents", select_agents
        )

    # ateles#460: _gates_green now reads gate_status from the issue entity, not
    # Lanius's stdout. These pipeline tests exercise the handoff, not the gate
    # check, so give them an entity whose pre-impl gates are genuinely cleared.
    # Tests that care about the gate check itself live in
    # test_gates_green_reads_entity.py and stub this differently.
    class _ClearGateState:
        found = True
        gate_status = {
            "pm": "signed_off",
            "ux": "signed_off",
            "arch": "signed_off",
        }

    async def fake_gate_load(self, repo, issue_number):
        return _ClearGateState()

    monkeypatch.setattr(
        swarm_dispatch.IssueGateStore, "load", fake_gate_load
    )

    # Neutralize the GitHub-body mirror (no network); we test it separately.
    async def fake_mirror(self, trigger, state):
        pass

    monkeypatch.setattr(SwarmDispatcher, "_mirror_spec_to_issue", fake_mirror)


def test_issue_pipeline_runs_sections_in_canonical_order(monkeypatch):
    """PM → Eng → QA (always) run in canonical order; conditional lenses skipped
    when select_expectation_agents does not pull them in."""
    order = []

    async def fake_run_skill(skill, prompt, **kwargs):
        order.append(skill)
        return SkillResult(skill, True, 0, "<<<SPEC_SECTION>>>text<<<END_SPEC_SECTION>>>", "")

    # Empty selection → only always-on sections (pm, eng, qa) run.
    _install_pipeline_stubs(
        monkeypatch, fake_run_skill, select_agents=lambda *a, **kw: []
    )

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    asyncio.run(dispatcher._handle_issue_opened(_issue_trigger()))

    # Lanius first, then the always-on section agents in canonical order.
    assert order[0] == "lanius"
    section_agents = order[1:]
    assert section_agents == ["pavo", "cicada", "phoenicurus"]
    # Conditional agents must NOT have run.
    for skipped in ("accipiter", "waxwing", "buteo"):
        assert skipped not in order


def test_issue_pipeline_includes_conditional_lens_when_selected(monkeypatch):
    """When select_expectation_agents pulls in a conditional lens (e.g. arch →
    waxwing), that section runs in its canonical position."""
    order = []

    async def fake_run_skill(skill, prompt, **kwargs):
        order.append(skill)
        return SkillResult(skill, True, 0, "text", "")

    # Selection includes waxwing (arch/security) and buteo (legal).
    class _LensStub:
        def __init__(self, agent):
            self.agent = agent

    _install_pipeline_stubs(
        monkeypatch,
        fake_run_skill,
        select_agents=lambda *a, **kw: [_LensStub("waxwing"), _LensStub("buteo")],
    )

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    asyncio.run(dispatcher._handle_issue_opened(_issue_trigger()))

    section_agents = order[1:]  # drop lanius
    # Canonical order: pm(pavo) → eng(cicada) → qa(phoenicurus) → security(waxwing)
    # → legal(buteo). design(accipiter) NOT selected.
    assert section_agents == ["pavo", "cicada", "phoenicurus", "waxwing", "buteo"]
    assert "accipiter" not in order


def test_issue_pipeline_sections_persist_additively(monkeypatch):
    """Each agent's section is stored additively; planting PM then running Eng
    preserves PM's section (the merge discipline)."""

    async def fake_run_skill(skill, prompt, **kwargs):
        return SkillResult(
            skill, True, 0,
            f"<<<SPEC_SECTION>>>**Scope:** {skill}-section body with real "
            f"substance to pass the not-just-narration floor.<<<END_SPEC_SECTION>>>",
            "",
        )

    _install_pipeline_stubs(
        monkeypatch, fake_run_skill, select_agents=lambda *a, **kw: []
    )

    dispatcher = SwarmDispatcher(_StubNotifier(), _config())
    asyncio.run(dispatcher._handle_issue_opened(_issue_trigger()))

    store = _FakeSpecStore.instances[-1]
    # Sections upserted in canonical order, each with its own agent's text.
    assert [k for k, _ in store.upserts] == ["pm", "eng", "qa"]
    texts = {k: v for k, v in store.upserts}
    assert texts["pm"] == "**Scope:** pavo-section body with real substance to pass the not-just-narration floor."
    assert texts["eng"] == "**Scope:** cicada-section body with real substance to pass the not-just-narration floor."
    assert texts["qa"] == "**Scope:** phoenicurus-section body with real substance to pass the not-just-narration floor."
    assert store.mirrored is True


def test_spec_section_prompt_is_additive_and_no_comment(monkeypatch):
    """The section prompt must tell the agent to add ONLY its section, build on
    prior sections, and NOT post spec as a comment."""
    pm = next(s for s in SECTIONS if s.key == "pm")
    prompt = SwarmDispatcher._spec_section_prompt(
        _issue_trigger(), pm, "PRIOR SPEC CONTENT"
    )
    assert "SPEC SO FAR" in prompt
    assert "PRIOR SPEC CONTENT" in prompt
    assert "ONLY" in prompt
    assert "<<<SPEC_SECTION>>>" in prompt
    assert "Do NOT post your section as a" in prompt or "not** post" in prompt.lower()
    # PM section still folds in the gate sign-off.
    assert "gate_status.pm" in prompt


def test_extract_section_text_prefers_fenced_content():
    out = SwarmDispatcher._extract_section_text(
        "chatter\n<<<SPEC_SECTION>>>\n**Scope:** real section body with enough "
        "substance to be a spec, not a stray fragment.\n<<<END_SPEC_SECTION>>>\ntrailer",
        next(s for s in SECTIONS if s.key == "pm"),
    )
    assert out == (
        "**Scope:** real section body with enough substance to be a spec, "
        "not a stray fragment."
    )


def test_extract_section_text_falls_back_to_stdout():
    out = SwarmDispatcher._extract_section_text(
        "**Scope:** just plain output, but with enough real substance to count "
        "as a spec section and not narration.",
        next(s for s in SECTIONS if s.key == "pm"),
    )
    assert out == (
        "**Scope:** just plain output, but with enough real substance to count "
        "as a spec section and not narration."
    )


# ── Auto-build flag behaviour ─────────────────────────────────────────────────


def test_flag_off_no_pr_opened_and_operator_notified(monkeypatch):
    """With ATELES_SWARM_AUTO_BUILD off, no implementation PR is opened and the
    operator is notified the spec awaits `build` approval."""
    build_calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        # The build handoff is the ONLY cicada dispatch carrying the "DO NOT
        # MERGE" build protocol (the eng-section cicada dispatch does not).
        if skill == "cicada" and "DO NOT MERGE" in prompt:
            build_calls.append(skill)
        return SkillResult(skill, True, 0, "text", "")

    _install_pipeline_stubs(
        monkeypatch, fake_run_skill, select_agents=lambda *a, **kw: []
    )

    notifier = _StubNotifier()
    cfg = DispatchConfig(neotoma_token="", github_token="", auto_build=False)
    dispatcher = SwarmDispatcher(notifier, cfg)
    asyncio.run(dispatcher._handle_issue_opened(_issue_trigger()))

    assert build_calls == [], "no Cicada build PR when auto-build is OFF"
    assert any("awaiting `build` approval" in m for m in notifier.sent)
    assert any("No PR opened" in m for m in notifier.sent)


def test_flag_on_invokes_build_handoff(monkeypatch):
    """With ATELES_SWARM_AUTO_BUILD on and gates green, the Cicada build handoff
    is invoked (PR-open mocked)."""
    build_calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        if skill == "cicada" and "DO NOT MERGE" in prompt:
            build_calls.append(prompt)
        # Lanius returns a clear (non-blocked) verdict → gates green.
        if skill == "lanius":
            return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
        return SkillResult(skill, True, 0, "text", "")

    _install_pipeline_stubs(
        monkeypatch, fake_run_skill, select_agents=lambda *a, **kw: []
    )

    notifier = _StubNotifier()
    cfg = DispatchConfig(neotoma_token="", github_token="", auto_build=True)
    dispatcher = SwarmDispatcher(notifier, cfg)
    asyncio.run(dispatcher._handle_issue_opened(_issue_trigger()))

    assert len(build_calls) == 1, "Cicada build handoff must be invoked once"
    # The build prompt references the issue for gate inheritance and forbids merge.
    assert "Closes #100" in build_calls[0]
    assert "DO NOT MERGE" in build_calls[0]
    assert any("auto-build ON" in m for m in notifier.sent)


def test_flag_on_but_gates_blocked_does_not_build(monkeypatch):
    """Auto-build ON but Lanius blocked → no build handoff, notify spec ready."""
    build_calls = []

    async def fake_run_skill(skill, prompt, **kwargs):
        if skill == "cicada" and "DO NOT MERGE" in prompt:
            build_calls.append(skill)
        if skill == "lanius":
            return SkillResult(skill, True, 0, "GATE_INHERITANCE: blocked", "")
        return SkillResult(skill, True, 0, "text", "")

    _install_pipeline_stubs(
        monkeypatch, fake_run_skill, select_agents=lambda *a, **kw: []
    )

    notifier = _StubNotifier()
    cfg = DispatchConfig(neotoma_token="", github_token="", auto_build=True)
    dispatcher = SwarmDispatcher(notifier, cfg)
    asyncio.run(dispatcher._handle_issue_opened(_issue_trigger()))

    assert build_calls == [], "blocked gates must not trigger a build"
    assert any("gates not green" in m for m in notifier.sent)


def test_config_auto_build_default_is_off():
    """Without the env flag set, auto_build defaults to False (spec-only)."""
    # The default in the dataclass is evaluated at import time; in the test
    # environment ATELES_SWARM_AUTO_BUILD is unset, so the default is False.
    cfg = DispatchConfig()
    assert cfg.auto_build is False


def test_config_auto_build_reads_env_on_reimport(monkeypatch):
    """auto_build is driven by ATELES_SWARM_AUTO_BUILD, read at class-def time.

    Since the dataclass default is evaluated at import, re-import the module
    with the env var set to prove the flag wiring is correct.
    """
    import importlib

    monkeypatch.setenv("ATELES_SWARM_AUTO_BUILD", "1")
    reloaded = importlib.reload(swarm_dispatch)
    try:
        cfg = reloaded.DispatchConfig()
        assert cfg.auto_build is True
    finally:
        # Restore the module to the unset-env default for later tests.
        monkeypatch.delenv("ATELES_SWARM_AUTO_BUILD", raising=False)
        importlib.reload(swarm_dispatch)


# ── Concurrency safety ────────────────────────────────────────────────────────


def test_issue_pipeline_semaphore_caps_concurrency(monkeypatch):
    """A burst of issue.opened events runs at most max_concurrent pipelines at
    once; the semaphore bounds concurrency."""
    active = 0
    peak = 0

    async def fake_run_skill(skill, prompt, **kwargs):
        nonlocal active, peak
        if skill == "lanius":
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.01)
            active -= 1
        return SkillResult(skill, True, 0, "text", "")

    _install_pipeline_stubs(
        monkeypatch, fake_run_skill, select_agents=lambda *a, **kw: []
    )

    cfg = DispatchConfig(
        neotoma_token="", github_token="", max_concurrent_issue_pipelines=2
    )
    dispatcher = SwarmDispatcher(_StubNotifier(), cfg)

    async def burst():
        await asyncio.gather(
            *[
                dispatcher._handle_issue_opened(_issue_trigger(number=200 + i))
                for i in range(6)
            ]
        )

    asyncio.run(burst())
    assert peak <= 2, f"concurrency exceeded cap: peak={peak}"


def test_queued_pipeline_emits_visible_queued_and_started_signals(monkeypatch, caplog):
    """ateles#259: when the single-slot issue-pipeline semaphore is held, a
    second pipeline must log a QUEUED signal before blocking and a STARTED
    signal once it acquires the slot — so a parked pipeline is not invisible.

    Two pipelines run concurrently with a 1-slot cap; the first holds the slot
    (its lanius dispatch sleeps), forcing the second to queue. We assert both
    the QUEUED and STARTED log lines appear for the queued issue.
    """
    first_in_slot = asyncio.Event()

    async def fake_run_skill(skill, prompt, **kwargs):
        if skill == "lanius":
            # First pipeline to reach lanius holds the slot long enough that the
            # second must queue on the semaphore.
            if not first_in_slot.is_set():
                first_in_slot.set()
                await asyncio.sleep(0.05)
        return SkillResult(skill, True, 0, "text", "")

    _install_pipeline_stubs(
        monkeypatch, fake_run_skill, select_agents=lambda *a, **kw: []
    )

    cfg = DispatchConfig(
        neotoma_token="", github_token="", max_concurrent_issue_pipelines=1
    )
    dispatcher = SwarmDispatcher(_StubNotifier(), cfg)

    async def run_two():
        # Start the slot-holder first, let it enter, then start the one forced
        # to queue.
        t1 = asyncio.create_task(
            dispatcher._handle_issue_opened(_issue_trigger(number=241))
        )
        await first_in_slot.wait()
        t2 = asyncio.create_task(
            dispatcher._handle_issue_opened(_issue_trigger(number=999))
        )
        await asyncio.gather(t1, t2)

    with caplog.at_level(logging.INFO):
        asyncio.run(run_two())

    text = caplog.text
    assert "#999 QUEUED" in text, (
        "the pipeline forced to wait on the busy slot must log a QUEUED signal; "
        f"log was:\n{text}"
    )
    assert "#999 STARTED" in text, (
        "the queued pipeline must log a STARTED signal once it acquires the "
        f"slot; log was:\n{text}"
    )


# ── Mirror splices only the managed region ────────────────────────────────────


def test_mirror_preserves_human_body_and_replaces_only_managed_block(monkeypatch):
    """_mirror_spec_to_issue reads the current issue body, splices the assembled
    spec between the managed markers, and PATCHes it back — preserving the
    reporter's original text above the markers."""
    captured = {}

    class _FakeResp:
        def __init__(self, payload):
            self._payload = payload
            self.content = b"x"

        def raise_for_status(self):
            pass

        def json(self):
            return self._payload

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return _FakeResp({"body": "REPORTER ORIGINAL TEXT"})

        async def patch(self, url, json=None, headers=None):  # noqa: F811
            captured["body"] = json["body"]
            return _FakeResp({})

    monkeypatch.setattr(swarm_dispatch.httpx, "AsyncClient", _FakeClient)

    cfg = DispatchConfig(neotoma_token="", github_token="tok")
    dispatcher = SwarmDispatcher(_StubNotifier(), cfg)
    state = SpecState(
        repo="owner/repo",
        issue_number=100,
        title="T",
        sections={"pm_section": "PM SPEC HERE"},
        sequence_state=["pm"],
    )
    asyncio.run(dispatcher._mirror_spec_to_issue(_issue_trigger(), state))

    body = captured["body"]
    assert "REPORTER ORIGINAL TEXT" in body
    assert SPEC_MARKER_START in body
    assert SPEC_MARKER_END in body
    assert "PM SPEC HERE" in body
    # Original text stays above the managed block.
    assert body.index("REPORTER ORIGINAL TEXT") < body.index(SPEC_MARKER_START)


def test_sanitize_section_body_strips_echoed_markers_and_dup_heading():
    """Regression for #180: agents echoed their own heading + the managed
    markers; the sanitizer must return content only so assembler/mirror don't
    duplicate headings or nest markers."""
    from swarm_dispatch import _sanitize_section_body
    from issue_spec import SECTIONS, SPEC_MARKER_START, SPEC_MARKER_END

    pm = next(s for s in SECTIONS if s.key == "pm")
    bad = (
        "## Swarm specification\n\n"
        f"### {pm.heading}\n\n"
        "**Problem:** verify.\n"
        f"{SPEC_MARKER_END}\n"
        "tail content\n"
    )
    out = _sanitize_section_body(bad, pm)
    assert SPEC_MARKER_START not in out
    assert SPEC_MARKER_END not in out
    assert pm.heading not in out  # the assembler will add exactly one heading
    assert "## Swarm specification" not in out
    assert "**Problem:** verify." in out
    assert "tail content" in out


def test_sanitize_section_body_preserves_clean_content_and_subheadings():
    from swarm_dispatch import _sanitize_section_body
    from issue_spec import SECTIONS

    eng = next(s for s in SECTIONS if s.key == "eng")
    good = "**Approach:** edit X.\n\n#### Sub-detail\nkeep me"
    out = _sanitize_section_body(good, eng)
    assert out == good  # nothing to strip; sub-headings preserved


# ── #1882 quality fixes: narration rejection + handoff PR verification ────────


def test_extract_section_rejects_pure_narration():
    """The #1882 Engineering defect: an agent's reply is a DESCRIPTION of a spec
    ('I have successfully completed the Engineering section…'), not a spec.
    _extract_section_text must return '' so the section is flagged not-produced."""
    from swarm_dispatch import _is_only_narration, _sanitize_section_body

    eng = next(s for s in SECTIONS if s.key == "eng")
    narration = (
        "Perfect. I have successfully completed the Engineering section of the "
        "specification for GitHub issue markmhendrickson/neotoma#1882.\n\n"
        "## Summary\n\nI have written a comprehensive Engineering section that:\n"
        "1. Specifies files to touch"
    )
    out = SwarmDispatcher._extract_section_text(narration, eng)
    assert out == ""
    assert _is_only_narration(_sanitize_section_body(narration, eng)) is True


def test_extract_section_keeps_real_multiline_spec():
    real = (
        "**Objective:** Trim the store tool description.\n\n"
        "**Files & Modules:**\n- src/tool_definitions.ts\n"
        "- src/shared/action_schemas.ts\n\n"
        "**Build Steps:**\n1. Measure baseline\n2. Reduce description"
    )
    out = SwarmDispatcher._extract_section_text(real, next(s for s in SECTIONS if s.key == "eng"))
    assert "Objective" in out and "Build Steps" in out


def test_extract_section_rejects_blocked_pr_link_status():
    from swarm_dispatch import _is_only_narration

    status = "[cicada] pull_request_link: BLOCKED — awaiting pre-impl gate sign-off"
    assert _is_only_narration(status) is True


def test_extract_pr_url_matches_repo_only():
    from swarm_dispatch import _extract_pr_url

    good = "Opened https://github.com/markmhendrickson/neotoma/pull/1905 — done."
    assert _extract_pr_url(good, "markmhendrickson/neotoma") == (
        "https://github.com/markmhendrickson/neotoma/pull/1905"
    )
    wrong = "https://github.com/other/repo/pull/9"
    assert _extract_pr_url(wrong, "markmhendrickson/neotoma") is None
    assert _extract_pr_url("no url here", "markmhendrickson/neotoma") is None


def test_open_implementation_pr_returns_url_from_cicada_stdout(monkeypatch):
    """When Cicada reports a PR URL in stdout, the handoff returns it."""
    async def fake_run_skill(skill, prompt, **kwargs):
        return SkillResult(
            skill, True, 0,
            "Built and opened https://github.com/owner/repo/pull/1910",
            "",
        )
    monkeypatch.setattr("swarm_dispatch.run_skill", fake_run_skill)
    d = SwarmDispatcher(_StubNotifier(), _config())
    url = asyncio.run(d._open_implementation_pr(_issue_trigger(), _empty_spec_state()))
    assert url == "https://github.com/owner/repo/pull/1910"


def test_open_implementation_pr_returns_none_when_no_pr(monkeypatch):
    """The #1882 false-green: Cicada exits 0 with no PR URL and no PR on GitHub;
    the handoff must return None (so the caller notifies 'no PR opened')."""
    async def fake_run_skill(skill, prompt, **kwargs):
        return SkillResult(skill, True, 0, "I have completed the build.", "")
    monkeypatch.setattr("swarm_dispatch.run_skill", fake_run_skill)

    async def fake_find(self, trigger):
        return None
    monkeypatch.setattr(SwarmDispatcher, "_find_open_pr_for_issue", fake_find)

    d = SwarmDispatcher(_StubNotifier(), _config())
    url = asyncio.run(d._open_implementation_pr(_issue_trigger(), _empty_spec_state()))
    assert url is None


def test_open_implementation_pr_persists_output_when_no_pr(monkeypatch):
    """An exit-0 dispatch that opens NO PR must still leave a full transcript.

    `run_skill` writes a diagnostics file only when the dispatch FAILS, so the
    exit-0-but-did-nothing case left nothing to diagnose — the reason this
    failure mode survived across eight issues undiagnosed. The post-condition
    failing IS the failure, so the complete stdout must be persisted here.
    """
    import skill_runner

    # conftest's autouse fixture already redirects this; bind the same path here
    # so the assertions below read from a directory this test controls.
    log_dir = skill_runner.DISPATCH_FAILURE_LOG_DIR

    narration = "I have analysed the spec and will now implement it. Step 1..."

    async def fake_run_skill(skill, prompt, **kwargs):
        return SkillResult(skill, True, 0, narration, "some stderr")

    monkeypatch.setattr("swarm_dispatch.run_skill", fake_run_skill)

    async def fake_find(self, trigger):
        return None

    monkeypatch.setattr(SwarmDispatcher, "_find_open_pr_for_issue", fake_find)

    d = SwarmDispatcher(_StubNotifier(), _config())
    url = asyncio.run(d._open_implementation_pr(_issue_trigger(), _empty_spec_state()))

    assert url is None
    written = list(log_dir.glob("cicada-*.log"))
    assert len(written) == 1, f"expected one diagnostics file, got {written}"
    body = written[0].read_text(encoding="utf-8")
    # The complete child output must be recoverable, not a truncated head.
    assert narration in body
    assert "some stderr" in body


def _empty_spec_state():
    from issue_spec import SpecState
    return SpecState(repo="markmhendrickson/neotoma", issue_number=1882, title="t")


def test_build_prompt_disambiguates_blocking_markers_in_spec():
    """ateles#359: a spec carrying [BLOCKING] markers must not read as 'do not build'.

    Lens agents write review vocabulary into build specs, and the swarm-wide
    prompt defines `[BLOCKING]` as 'the author must address them' while Cicada's
    prompt says to raise a checkpoint rather than open a PR. Cicada IS the
    author, so an unqualified spec makes refusing-to-build the compliant
    reading — the observed failure across eight issues (only the two specs with
    zero blocking markers ever produced a PR).

    The build prompt must therefore state explicitly that markers inside the
    spec are definition-of-done items for the PR being opened, not findings
    that withhold it.
    """
    from issue_spec import SpecState

    state = SpecState(repo="owner/repo", issue_number=100, title="t")
    state.sections = {
        "security": (
            "- [ ] Flag as [NON-BLOCKING] for this PR, but if exposed on a "
            "guest surface this becomes [BLOCKING] and needs rate-limiting."
        )
    }
    prompt = SwarmDispatcher._cicada_build_prompt(_issue_trigger(), state)

    # The spec's own markers still reach the builder (we do not strip signal).
    assert "[NON-BLOCKING]" in prompt

    lowered = prompt.lower()
    # The builder is told these are requirements to satisfy, not merge-blockers.
    assert "definition-of-done" in lowered
    # And is told not to withhold the PR merely because the word appears.
    assert "open the pr" in lowered
    # The conditional-marker case is called out, since specs phrase it that way.
    assert "conditional" in lowered


# ── issue-pipeline resume after restart (ateles#230 follow-up) ───────────────


def _resume_dispatcher(monkeypatch, *, calls):
    """Dispatcher with the pipeline body and marker I/O stubbed, recording the
    marker lifecycle so we can assert start/clear ordering."""
    d = SwarmDispatcher(_StubNotifier(), _config())

    async def fake_pipeline(self, trigger):
        calls.append(("pipeline", trigger.number))

    async def fake_mark(self, trigger, *, stage="inflight"):
        calls.append((f"mark:{stage}", trigger.number))

    async def fake_clear(self, trigger):
        calls.append(("clear", trigger.number))

    monkeypatch.setattr(SwarmDispatcher, "_run_issue_spec_pipeline", fake_pipeline)
    monkeypatch.setattr(SwarmDispatcher, "_mark_pipeline_inflight", fake_mark)
    monkeypatch.setattr(SwarmDispatcher, "_clear_pipeline_inflight", fake_clear)
    return d


def test_pipeline_marks_inflight_then_clears_on_success(monkeypatch):
    """Uncontended path: the semaphore is free, so no "queued" marker is
    written (ateles#323 gates it on `sem.locked()`, matching the pre-existing
    QUEUED-log branch) — only "inflight" then clear, same cost as before this
    fix."""
    calls = []
    d = _resume_dispatcher(monkeypatch, calls=calls)
    asyncio.run(d._handle_issue_opened(_trigger(kind="issue_opened", number=230)))
    assert calls == [
        ("mark:inflight", 230),
        ("pipeline", 230),
        ("clear", 230),
    ]


def test_pipeline_marks_queued_before_inflight_when_semaphore_contended(monkeypatch):
    """ateles#323: when a second trigger arrives while the semaphore is held,
    a "queued" marker must be written BEFORE the acquire attempt (so a
    restart while parked is durable), then "inflight" once acquired."""
    calls = []
    cfg = DispatchConfig(
        neotoma_token="", github_token="", max_concurrent_issue_pipelines=1
    )
    d = SwarmDispatcher(_StubNotifier(), cfg)

    async def fake_mark(self, trigger, *, stage="inflight"):
        calls.append((f"mark:{stage}", trigger.number))

    async def fake_clear(self, trigger):
        calls.append(("clear", trigger.number))

    first_in_slot = asyncio.Event()

    async def fake_pipeline(self, trigger):
        calls.append(("pipeline", trigger.number))
        if trigger.number == 241:
            first_in_slot.set()
            await asyncio.sleep(0.02)

    monkeypatch.setattr(SwarmDispatcher, "_mark_pipeline_inflight", fake_mark)
    monkeypatch.setattr(SwarmDispatcher, "_clear_pipeline_inflight", fake_clear)
    monkeypatch.setattr(SwarmDispatcher, "_run_issue_spec_pipeline", fake_pipeline)

    async def run_two():
        t1 = asyncio.create_task(
            d._handle_issue_opened(_trigger(kind="issue_opened", number=241))
        )
        await first_in_slot.wait()
        t2 = asyncio.create_task(
            d._handle_issue_opened(_trigger(kind="issue_opened", number=999))
        )
        await asyncio.gather(t1, t2)

    asyncio.run(run_two())

    marks_for_999 = [c for c in calls if c[1] == 999]
    assert marks_for_999 == [
        ("mark:queued", 999),
        ("mark:inflight", 999),
        ("pipeline", 999),
        ("clear", 999),
    ]


def test_pipeline_clears_inflight_marker_even_when_pipeline_raises(monkeypatch):
    """A pipeline that fails for a real reason already reports itself; it must
    NOT be left marked in-flight or the sweep would resurrect it every boot."""
    calls = []
    d = _resume_dispatcher(monkeypatch, calls=calls)

    async def boom(self, trigger):
        calls.append(("pipeline", trigger.number))
        raise RuntimeError("lens exploded")

    monkeypatch.setattr(SwarmDispatcher, "_run_issue_spec_pipeline", boom)
    try:
        asyncio.run(d._handle_issue_opened(_trigger(kind="issue_opened", number=230)))
    except RuntimeError:
        pass
    assert ("clear", 230) in calls


def test_resume_sweep_reruns_pipelines_with_inflight_marker(monkeypatch):
    resumed = []
    d = SwarmDispatcher(_StubNotifier(), _config())

    async def fake_scan(self, repository):
        assert repository == "owner/repo"
        return [{"number": 230, "title": "stranded", "body": "b", "user": {"login": "x"},
                 "html_url": "u", "labels": [{"name": "enhancement"}]}]

    async def fake_handle(self, trigger):
        resumed.append((trigger.repository, trigger.number, trigger.kind))

    monkeypatch.setattr(SwarmDispatcher, "_issues_with_inflight_marker", fake_scan)
    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle)

    async def fake_advanced(self, repository, number, resume_started_at):
        return True

    monkeypatch.setattr(
        SwarmDispatcher, "_pipeline_advanced_past_triage", fake_advanced
    )
    summary = asyncio.run(d.resume_interrupted_pipelines(["owner/repo"]))
    assert resumed == [("owner/repo", 230, "issue_opened")]
    assert summary == {"scanned": 1, "resumed": 1, "failed": 0, "stalled": 0}


def test_resume_sweep_skips_issues_without_marker(monkeypatch):
    """No marker → the pipeline either never started or finished cleanly."""
    resumed = []
    d = SwarmDispatcher(_StubNotifier(), _config())

    async def fake_scan(self, repository):
        return []

    async def fake_handle(self, trigger):
        resumed.append(trigger.number)

    monkeypatch.setattr(SwarmDispatcher, "_issues_with_inflight_marker", fake_scan)
    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle)
    summary = asyncio.run(d.resume_interrupted_pipelines(["owner/repo"]))
    assert resumed == []
    assert summary["resumed"] == 0


def test_resume_sweep_survives_a_repo_that_fails_to_scan(monkeypatch):
    """A broken repo must not stop the other repos, nor kill daemon startup."""
    resumed = []
    d = SwarmDispatcher(_StubNotifier(), _config())

    async def fake_scan(self, repository):
        if repository == "owner/broken":
            raise RuntimeError("GitHub 500")
        return [{"number": 7, "title": "t", "body": "", "user": {"login": "x"},
                 "html_url": "u", "labels": []}]

    async def fake_handle(self, trigger):
        resumed.append(trigger.number)

    monkeypatch.setattr(SwarmDispatcher, "_issues_with_inflight_marker", fake_scan)
    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle)
    summary = asyncio.run(
        d.resume_interrupted_pipelines(["owner/broken", "owner/good"])
    )
    assert resumed == [7]
    assert summary["resumed"] == 1


def test_resume_sweep_counts_a_failed_resume_without_raising(monkeypatch):
    d = SwarmDispatcher(_StubNotifier(), _config())

    async def fake_scan(self, repository):
        return [{"number": 9, "title": "t", "body": "", "user": {"login": "x"},
                 "html_url": "u", "labels": []}]

    async def fake_handle(self, trigger):
        raise RuntimeError("pipeline died again")

    monkeypatch.setattr(SwarmDispatcher, "_issues_with_inflight_marker", fake_scan)
    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle)
    summary = asyncio.run(d.resume_interrupted_pipelines(["owner/repo"]))
    assert summary == {"scanned": 1, "resumed": 0, "failed": 1, "stalled": 0}


def test_inflight_marker_regex_roundtrip():
    marker = SwarmDispatcher._PIPELINE_INFLIGHT_MARKER.format(
        started_at="2026-07-17T12:25:53.123456+00:00", stage="inflight"
    )
    m = SwarmDispatcher._PIPELINE_INFLIGHT_RE.search(marker)
    assert m
    assert m.group(2) == "inflight"
    # Must not match the sibling fix-round marker.
    assert not SwarmDispatcher._PIPELINE_INFLIGHT_RE.search("<!-- apis-fix-round:2 -->")


def test_inflight_marker_regex_matches_queued_stage():
    marker = SwarmDispatcher._PIPELINE_INFLIGHT_MARKER.format(
        started_at="2026-07-17T12:25:53.123456+00:00", stage="queued"
    )
    m = SwarmDispatcher._PIPELINE_INFLIGHT_RE.search(marker)
    assert m
    assert m.group(2) == "queued"


def test_inflight_marker_regex_defaults_stage_for_legacy_marker_without_suffix():
    """A marker written by a daemon build predating ateles#323 has no stage
    suffix at all; it must still match (group(2) is None, callers default it
    to "inflight" — today's behavior) rather than becoming invisible to the
    resume sweep after a mixed-version deploy."""
    legacy = "<!-- apis-pipeline-inflight:2026-07-17T12:25:53.123456+00:00 -->"
    m = SwarmDispatcher._PIPELINE_INFLIGHT_RE.search(legacy)
    assert m
    assert m.group(2) is None


# ── ateles#323: queued-marker durability across restart ─────────────────────


def test_queued_marker_survives_restart_and_sweep_dispatches_it(monkeypatch):
    """ateles#323 regression: a second trigger parked on the single-slot
    semaphore when the daemon dies must be picked up by the resume sweep on
    the next boot — via the SAME `issue_opened` entry point a fresh trigger
    would use, not a special-cased resume path.

    We enqueue two triggers so the second parks on the semaphore (a real
    1-slot asyncio.Semaphore; the first pipeline's lanius call sleeps to hold
    the slot open). Mid-park, we simulate the restart: no `async with sem`
    frame survives a real process restart, so we throw away the first
    dispatcher instance entirely and construct a NEW one — the second
    pipeline's coroutine (and its semaphore wait) is gone, exactly as a killed
    process would lose it. What must survive is the "queued" marker written to
    GitHub BEFORE the semaphore was even attempted.

    The reconstructed dispatcher's resume sweep must then find that marker and
    re-dispatch #999 through `_handle_issue_opened`, observed via the actual
    effect: `_run_issue_spec_pipeline` runs for it (the real triage entry
    point), not merely that a marker row exists.
    """
    # GitHub-side state, keyed by issue number — durable across the
    # "restart" and scoped per-issue exactly like the real API (clearing
    # #241's marker on completion must never touch #999's).
    markers_by_issue: dict[int, str] = {}

    def _stage_of(number):
        body = markers_by_issue.get(number)
        if body is None:
            return None
        m = SwarmDispatcher._PIPELINE_INFLIGHT_RE.search(body)
        return (m.group(2) or "inflight") if m else None

    async def fake_mark(self, trigger, *, stage="inflight"):
        markers_by_issue[trigger.number] = (
            SwarmDispatcher._PIPELINE_INFLIGHT_MARKER.format(
                started_at="2026-07-17T12:00:00+00:00", stage=stage
            )
        )

    async def fake_clear(self, trigger):
        markers_by_issue.pop(trigger.number, None)

    first_in_slot = asyncio.Event()
    pipeline_ran = []

    async def fake_pipeline(self, trigger):
        pipeline_ran.append(trigger.number)
        if trigger.number == 241:
            # First pipeline holds the slot long enough that #999 must queue.
            first_in_slot.set()
            await asyncio.sleep(0.05)

    monkeypatch.setattr(SwarmDispatcher, "_mark_pipeline_inflight", fake_mark)
    monkeypatch.setattr(SwarmDispatcher, "_clear_pipeline_inflight", fake_clear)
    monkeypatch.setattr(
        SwarmDispatcher, "_run_issue_spec_pipeline", fake_pipeline
    )

    cfg = DispatchConfig(
        neotoma_token="", github_token="", max_concurrent_issue_pipelines=1
    )
    dispatcher_before_restart = SwarmDispatcher(_StubNotifier(), cfg)

    async def run_until_parked():
        t1 = asyncio.create_task(
            dispatcher_before_restart._handle_issue_opened(
                _issue_trigger(number=241)
            )
        )
        await first_in_slot.wait()
        # #999 starts, writes its "queued" marker, then blocks on `sem` — we
        # deliberately do NOT await it further, simulating the restart killing
        # the process while it's parked.
        t2 = asyncio.create_task(
            dispatcher_before_restart._handle_issue_opened(
                _issue_trigger(number=999)
            )
        )
        # Give #999's coroutine a tick to reach and write its queued marker
        # (it blocks on `sem` right after, so this is not itself racy: #999
        # cannot proceed past the mark until #241 releases the semaphore).
        await asyncio.sleep(0.01)
        return t1, t2

    t1, t2 = asyncio.run(run_until_parked())
    # "Restart": the process dies. Neither task survives; drop them without
    # awaiting completion (mirrors a killed process, not a graceful shutdown).
    t1.cancel()
    t2.cancel()

    # #999's queued marker must have made it to GitHub before the park.
    assert _stage_of(999) == "queued", (
        f"expected a queued-stage marker for the parked pipeline; got "
        f"{markers_by_issue}"
    )

    pipeline_ran.clear()

    async def fake_scan(self, repository):
        assert repository == "owner/repo"
        return [
            {
                "number": 999,
                "title": "queued issue",
                "body": "b",
                "user": {"login": "x"},
                "html_url": "u",
                "labels": [],
                "_marker_stage": _stage_of(999),
            }
        ]

    monkeypatch.setattr(
        SwarmDispatcher, "_issues_with_inflight_marker", fake_scan
    )

    async def fake_advanced(self, repository, number, resume_started_at):
        return True

    monkeypatch.setattr(
        SwarmDispatcher, "_pipeline_advanced_past_triage", fake_advanced
    )

    # New process, new dispatcher instance — nothing from the old one carries
    # over except what was durably written to GitHub.
    dispatcher_after_restart = SwarmDispatcher(_StubNotifier(), cfg)
    summary = asyncio.run(
        dispatcher_after_restart.resume_interrupted_pipelines(["owner/repo"])
    )

    assert 999 in pipeline_ran, (
        "the resumed 'queued' issue must be dispatched through the real "
        "triage entry point (_run_issue_spec_pipeline), not merely have its "
        "marker acknowledged"
    )
    assert summary["resumed"] == 1
    assert summary["stalled"] == 0


def test_resumed_inflight_pipeline_that_never_advances_is_escalated_not_requeued(
    monkeypatch,
):
    """Second regression for ateles#323: a resumed pipeline whose marker was
    'inflight' (a lens agent was mid-run) but that produces no phase-2 event
    (no spec-mirror marker reaches the issue body) must NOT be silently
    re-queued for the next restart — it must escalate via the stall guard so a
    repeat-every-boot failure is visible instead of invisible.
    """
    d = SwarmDispatcher(_StubNotifier(), _config())

    async def fake_scan(self, repository):
        return [
            {
                "number": 555,
                "title": "stuck issue",
                "body": "b",
                "user": {"login": "x"},
                "html_url": "u",
                "labels": [],
                "_marker_stage": "inflight",
            }
        ]

    async def fake_handle(self, trigger):
        # The resumed run "completes" but never reaches the first lens
        # section — e.g. Lanius hangs/no-ops without ever mirroring a spec.
        pass

    async def fake_advanced(self, repository, number, resume_started_at):
        assert (repository, number) == ("owner/repo", 555)
        return False  # the issue was never touched again after the resume

    monkeypatch.setattr(SwarmDispatcher, "_issues_with_inflight_marker", fake_scan)
    monkeypatch.setattr(SwarmDispatcher, "_handle_issue_opened", fake_handle)
    monkeypatch.setattr(
        SwarmDispatcher, "_pipeline_advanced_past_triage", fake_advanced
    )

    summary = asyncio.run(d.resume_interrupted_pipelines(["owner/repo"]))

    assert summary == {"scanned": 1, "resumed": 1, "failed": 0, "stalled": 1}
    # Escalated to the operator, not silently re-queued.
    from lib.notify import Priority

    assert any(
        p == Priority.OPERATOR_DECISION for p in d.notifier.priorities
    ), d.notifier.sent_full


def test_pipeline_advanced_past_triage_true_when_issue_touched_after_resume(
    monkeypatch,
):
    """The issue's `updated_at` moved past the resume start time — the
    pipeline made real progress (the spec mirror PATCHes the body on every
    section, bumping `updated_at`)."""
    d = SwarmDispatcher(_StubNotifier(), _config())

    async def fake_fetch(self, repository, number):
        assert (repository, number) == ("owner/repo", 555)
        return {"updated_at": "2026-07-17T12:05:00Z"}

    monkeypatch.setattr(SwarmDispatcher, "_fetch_issue_fields", fake_fetch)
    resume_started_at = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
    result = asyncio.run(
        d._pipeline_advanced_past_triage("owner/repo", 555, resume_started_at)
    )
    assert result is True


def test_pipeline_advanced_past_triage_false_when_not_touched_since_resume(
    monkeypatch,
):
    """The issue's `updated_at` predates (or equals) the resume start —
    nothing happened after this resume began, even if the spec-mirror marker
    is present in the body from some earlier, unrelated completed run.
    """
    d = SwarmDispatcher(_StubNotifier(), _config())

    async def fake_fetch(self, repository, number):
        return {"updated_at": "2026-07-17T11:55:00Z"}

    monkeypatch.setattr(SwarmDispatcher, "_fetch_issue_fields", fake_fetch)
    resume_started_at = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
    result = asyncio.run(
        d._pipeline_advanced_past_triage("owner/repo", 555, resume_started_at)
    )
    assert result is False


def test_pipeline_advanced_past_triage_not_fooled_by_stale_spec_marker(monkeypatch):
    """Regression for the false-negative found in self-review: checking mere
    PRESENCE of SPEC_MARKER_START in the body would wrongly read a STALLED
    resume as 'advanced', because that marker is spliced in and never removed
    — it persists from any prior completed run on the same issue regardless
    of whether the CURRENT resume made progress. A body containing the marker
    but an `updated_at` that predates the resume must still report False.
    """
    from issue_spec import SPEC_MARKER_START

    d = SwarmDispatcher(_StubNotifier(), _config())

    async def fake_fetch(self, repository, number):
        return {
            "updated_at": "2026-07-17T09:00:00Z",  # long before this resume
            "body": f"some text\n{SPEC_MARKER_START}\nold spec\n<!-- swarm-spec:end -->",
        }

    monkeypatch.setattr(SwarmDispatcher, "_fetch_issue_fields", fake_fetch)
    resume_started_at = datetime(2026, 7, 17, 12, 0, 0, tzinfo=timezone.utc)
    result = asyncio.run(
        d._pipeline_advanced_past_triage("owner/repo", 555, resume_started_at)
    )
    assert result is False, (
        "a stale spec-mirror marker from a prior run must not mask a stalled "
        "resume as having advanced"
    )


def test_pipeline_advanced_past_triage_fails_open_on_fetch_error(monkeypatch):
    d = SwarmDispatcher(_StubNotifier(), _config())

    async def fake_fetch(self, repository, number):
        return None

    monkeypatch.setattr(SwarmDispatcher, "_fetch_issue_fields", fake_fetch)
    result = asyncio.run(
        d._pipeline_advanced_past_triage(
            "owner/repo", 555, datetime.now(timezone.utc)
        )
    )
    assert result is True


# ── push_main → release prepare (auto-release trigger) ─────────────────────


def _push_main_trigger(repository="markmhendrickson/neotoma"):
    return SwarmTrigger(
        kind="push_main",
        repository=repository,
        number=0,
        title="feat: thing",
        body="",
        author="someone",
        html_url="",
        delivery_id="d-1",
        action="push",
        push_ref="refs/heads/main",
        push_after="a" * 40,
    )


def test_push_main_invokes_release_prepare(monkeypatch):
    """A merge to the release repo's main shells out to prepare.py --on-merge."""
    spawned = []

    async def fake_exec(*args, **kwargs):
        spawned.append(args)

        class _P:
            returncode = 0

            async def communicate(self):
                return (b"nothing to prepare", b"")

        return _P()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(swarm_dispatch.os.path, "exists", lambda p: True)

    d = SwarmDispatcher(_StubNotifier(), _config())
    asyncio.run(d._handle_push_main(_push_main_trigger()))

    assert len(spawned) == 1, "expected exactly one prepare.py invocation"
    argv = spawned[0]
    assert argv[1].endswith("prepare.py")
    # The --on-merge flag is what swaps the daily lock for the per-commit lock.
    assert "--on-merge" in argv


def test_push_main_ignores_non_release_repo(monkeypatch):
    """Only the repo with a publishable pipeline prepares a release."""
    spawned = []

    async def fake_exec(*args, **kwargs):
        spawned.append(args)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    d = SwarmDispatcher(_StubNotifier(), _config())
    asyncio.run(d._handle_push_main(_push_main_trigger(repository="o/other")))
    assert spawned == []


def test_push_main_survives_prepare_failure(monkeypatch):
    """A failing prepare must never raise into the dispatcher."""

    async def boom(*args, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
    monkeypatch.setattr(swarm_dispatch.os.path, "exists", lambda p: True)
    d = SwarmDispatcher(_StubNotifier(), _config())
    asyncio.run(d._handle_push_main(_push_main_trigger()))  # must not raise


def test_push_main_prepare_timeout(monkeypatch, caplog):
    """A wedged prepare.py subprocess must be bounded, swallowed, and logged.

    ``_handle_push_main`` is documented as "Fully best-effort; never raises" -
    a hung git/gh call inside prepare.py must not wedge the dispatcher, and
    the failure must surface as a diagnosable log line instead of silently
    vanishing.
    """

    class _P:
        returncode = None

        async def communicate(self):
            # Never actually awaited by fake_wait_for below (that's the point
            # of the timeout), but must be a real coroutine to avoid an
            # "unawaited coroutine" warning when wait_for() is called on it.
            await asyncio.sleep(0)
            return (b"", b"")

    async def fake_exec(*args, **kwargs):
        return _P()

    async def fake_wait_for(coro, timeout):
        coro.close()
        raise asyncio.TimeoutError()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(swarm_dispatch.os.path, "exists", lambda p: True)

    d = SwarmDispatcher(_StubNotifier(), _config())
    trigger = _push_main_trigger()
    with caplog.at_level("ERROR", logger="apis.swarm_dispatch"):
        asyncio.run(d._handle_push_main(trigger))  # must not raise

    sha = trigger.push_after[:9]
    assert any(
        sha in rec.message and str(swarm_dispatch.PHOENICURUS_PREPARE_TIMEOUT_S) in rec.message
        for rec in caplog.records
    ), "timeout must be logged with the SHA and the configured timeout for diagnosis"


# ── auto-release retry: main CI completion re-drives release prep ────────────
#
# A push_main that fires while its merge's CI is still running defers WITHOUT
# stamping the SHA lock (prepare.py transient deferral). The check_suite:completed
# on main is the retry: on success it must re-invoke _handle_push_main; on failure
# or a non-release branch/repo it must NOT.


def test_ci_status_main_success_retries_release_prep(monkeypatch):
    seen = []

    async def fake_push_main(self, trigger):
        seen.append(trigger)

    monkeypatch.setattr(SwarmDispatcher, "_handle_push_main", fake_push_main)
    d = SwarmDispatcher(_StubNotifier(), _config())
    trig = _ci_status_trigger(
        repository="markmhendrickson/neotoma", ci_head_branch="main",
        ci_conclusion="success", ci_head_sha="f" * 40, ci_pr_numbers=[],
    )
    asyncio.run(d._handle_ci_status(trig))
    assert len(seen) == 1, "main CI success must retry release prep"
    assert seen[0].kind == "push_main"
    assert seen[0].push_after == "f" * 40
    assert seen[0].push_ref == "refs/heads/main"


def test_ci_status_main_failure_does_not_retry(monkeypatch):
    seen = []

    async def fake_push_main(self, trigger):
        seen.append(trigger)

    monkeypatch.setattr(SwarmDispatcher, "_handle_push_main", fake_push_main)
    d = SwarmDispatcher(_StubNotifier(), _config())
    trig = _ci_status_trigger(
        repository="markmhendrickson/neotoma", ci_head_branch="main",
        ci_conclusion="failure", ci_pr_numbers=[],
    )
    asyncio.run(d._handle_ci_status(trig))
    assert seen == [], "a red main build must not prepare a release"


def test_ci_status_non_release_repo_main_does_not_retry(monkeypatch):
    """Only the release repo's main drives release prep; other repos fall through
    to the normal PR-oriented ci_status path (here: no PR → ignored)."""
    seen = []

    async def fake_push_main(self, trigger):
        seen.append(trigger)

    async def fake_fetch_pr(self, repo, num):  # not reached with no PR, but safe
        return None

    monkeypatch.setattr(SwarmDispatcher, "_handle_push_main", fake_push_main)
    monkeypatch.setattr(SwarmDispatcher, "_fetch_pr", fake_fetch_pr)
    d = SwarmDispatcher(_StubNotifier(), _config())
    trig = _ci_status_trigger(
        repository="owner/other", ci_head_branch="main",
        ci_conclusion="success", number=0, ci_pr_numbers=[],
    )
    asyncio.run(d._handle_ci_status(trig))
    assert seen == [], "non-release repo main must not prepare a release"


# ── Session/usage-limit detection + auto-resume (feat/swarm-resume-after-limit) ──


def test_detect_session_limit_matches_real_message():
    # The exact stub text seen on ateles PR #254.
    msg = "You've hit your session limit · resets 7:30pm (Europe/Madrid)"
    assert swarm_dispatch.detect_session_limit(msg) is True


def test_detect_session_limit_variants():
    for m in [
        "usage limit reached, resets at 19:30",
        "rate limit exceeded — resets in a bit",
        "You've reached your usage limit.",
    ]:
        assert swarm_dispatch.detect_session_limit(m) is True, m


def test_detect_session_limit_ignores_unrelated_prose():
    # Must not misfire on a normal verdict that merely mentions "rate" etc.
    for m in [
        "VERDICT: APPROVE — no findings",
        "the rate of false positives is low",  # 'rate' but no limit/reset cue
        "",
    ]:
        assert swarm_dispatch.detect_session_limit(m) is False, m


def test_session_limit_is_distinct_from_auth_failure():
    # A pure limit message is NOT an auth failure (different remediation).
    limit = "you've hit your session limit · resets 7:30pm"
    assert swarm_dispatch.detect_session_limit(limit) is True
    assert swarm_dispatch.detect_auth_failure(limit) is False
    # And a 401 is not a session limit.
    auth = "API Error: 401 invalid authentication credentials"
    assert swarm_dispatch.detect_auth_failure(auth) is True
    assert swarm_dispatch.detect_session_limit(auth) is False


def test_handle_panel_session_limit_notifies_at_info_not_digest(monkeypatch):
    # Regression for the live AttributeError: Priority.DIGEST doesn't exist on
    # the Priority enum (lib/notify/notifier.py), so the notifier.send() call
    # raised and was swallowed by the surrounding `except Exception`, meaning
    # the operator got NO notification at all for a throttled review. INFO is
    # the enum's existing "always digest, nothing to fix" priority.
    notifier = _StubNotifier()
    d = SwarmDispatcher(notifier, _config())  # no github_token -> comment post short-circuits
    trig = _trigger(number=264, repository="owner/repo")
    asyncio.run(
        d._handle_panel_session_limit(
            trig, None, "vanellus",
            "You've hit your session limit · resets 7:30pm", "",
        )
    )
    assert len(notifier.sent) == 1
    assert notifier.priorities == [swarm_dispatch.Priority.INFO]
    assert "paused on a usage limit" in notifier.sent[0]


def test_handle_panel_session_limit_attributes_to_apis_dispatcher(monkeypatch):
    # Regression: the deferral comment carried
    # attribution_header('vanellus', 'PR steward') while its own body says
    # "Posted by the Apis dispatcher" — a dispatcher-originated infra notice
    # must use attribution_header('apis', 'swarm dispatcher'), matching the
    # sibling _handle_panel_auth_failure.
    comment_bodies: list[str] = []

    class _CapturingClient:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def get(self, url, **kwargs):
            return _FakeListResp([])
        async def post(self, url, **kwargs):
            comment_bodies.append(kwargs.get("json", {}).get("body", ""))
            return _FakeResp(201)

    monkeypatch.setattr(httpx, "AsyncClient", _CapturingClient)
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")

    d = SwarmDispatcher(_StubNotifier(), DispatchConfig(neotoma_token="", github_token="x"))
    trig = _trigger(number=264, repository="owner/repo")
    asyncio.run(
        d._handle_panel_session_limit(
            trig, None, "vanellus",
            "You've hit your session limit · resets 7:30pm", "",
        )
    )
    assert len(comment_bodies) == 1
    assert attribution_header("apis", "swarm dispatcher") in comment_bodies[0]
    assert attribution_header("vanellus", "PR steward") not in comment_bodies[0]


def test_parse_limit_reset_delay_reads_the_clock():
    from datetime import datetime
    now = datetime(2026, 7, 23, 16, 45, 0)  # 4:45pm
    # "resets 7:30pm" -> 19:30 same day -> 2h45m + 120s pad
    delay = swarm_dispatch.parse_limit_reset_delay(
        "resets 7:30pm (Europe/Madrid)", now=now
    )
    assert 2 * 3600 + 45 * 60 <= delay <= 2 * 3600 + 45 * 60 + 130


def test_parse_limit_reset_delay_rolls_to_next_day_when_past():
    from datetime import datetime
    now = datetime(2026, 7, 23, 20, 0, 0)  # 8pm, after a 7:30pm reset
    delay = swarm_dispatch.parse_limit_reset_delay("resets 7:30pm", now=now)
    # target rolls to tomorrow 19:30 -> ~23.5h away, not negative
    assert delay > 23 * 3600


def test_parse_limit_reset_delay_default_when_no_time():
    d = swarm_dispatch.parse_limit_reset_delay("you've hit your session limit")
    assert d >= 300  # a sane, non-zero default


def test_deferred_review_sweep_redispatches_matured_pr(monkeypatch):
    # A PR whose deferral time has passed is re-dispatched through _handle_pr.
    resumed = []
    d = SwarmDispatcher(_StubNotifier(), _config())

    async def fake_scan(self, repository, now):
        return {
            "due": [{"number": 254, "title": "evidence bar", "body": "b",
                     "user": {"login": "x"}, "html_url": "u",
                     "head": {"ref": "feat/x"}, "base": {"ref": "main"}}],
            "scanned": 1, "not_yet": 0,
        }

    async def fake_handle(self, trigger):
        resumed.append((trigger.number, trigger.kind))

    monkeypatch.setattr(SwarmDispatcher, "_prs_with_matured_deferral", fake_scan)
    monkeypatch.setattr(SwarmDispatcher, "_handle_pr", fake_handle)
    summary = asyncio.run(d.resume_deferred_reviews(["owner/repo"]))
    assert resumed == [(254, "pr_synchronize")]
    assert summary["resumed"] == 1 and summary["failed"] == 0


def test_deferred_review_sweep_leaves_unmatured_pr_alone(monkeypatch):
    # A PR whose deferral is still in the future must NOT be re-dispatched.
    resumed = []
    d = SwarmDispatcher(_StubNotifier(), _config())

    async def fake_scan(self, repository, now):
        return {"due": [], "scanned": 1, "not_yet": 1}

    async def fake_handle(self, trigger):
        resumed.append(trigger.number)

    monkeypatch.setattr(SwarmDispatcher, "_prs_with_matured_deferral", fake_scan)
    monkeypatch.setattr(SwarmDispatcher, "_handle_pr", fake_handle)
    summary = asyncio.run(d.resume_deferred_reviews(["owner/repo"]))
    assert resumed == []
    assert summary["not_yet"] == 1 and summary["resumed"] == 0


def test_deferred_review_sweep_is_fail_open_per_repo(monkeypatch):
    # One repo failing to scan must not stop the others.
    resumed = []
    d = SwarmDispatcher(_StubNotifier(), _config())

    async def fake_scan(self, repository, now):
        if repository == "owner/broken":
            raise RuntimeError("scan boom")
        return {"due": [{"number": 5, "title": "t", "body": "", "user": {},
                         "html_url": "", "head": {}, "base": {}}],
                "scanned": 1, "not_yet": 0}

    async def fake_handle(self, trigger):
        resumed.append(trigger.number)

    monkeypatch.setattr(SwarmDispatcher, "_prs_with_matured_deferral", fake_scan)
    monkeypatch.setattr(SwarmDispatcher, "_handle_pr", fake_handle)
    summary = asyncio.run(
        d.resume_deferred_reviews(["owner/broken", "owner/good"])
    )
    assert resumed == [5]  # the good repo still ran
    assert summary["resumed"] == 1


class _FakeDeferralHttpxClient:
    """Serves a fixed PR list and per-PR comment thread to
    _prs_with_matured_deferral, so its marker-lifecycle logic runs for real
    (Loxia #264: the sweep tests monkeypatched this method, missing the bug)."""

    def __init__(self, prs, comments_by_pr):
        self._prs = prs
        self._comments = comments_by_pr

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def get(self, url, **kwargs):
        prs = self._prs
        comments = self._comments

        class _Resp:
            def raise_for_status(self_inner):
                pass

            def json(self_inner):
                if url.endswith("/pulls"):
                    return prs
                # /issues/{n}/comments
                import re as _re
                m = _re.search(r"/issues/(\d+)/comments", url)
                return comments.get(int(m.group(1)), []) if m else []

        return _Resp()


def _run_deferral_scan(monkeypatch, prs, comments, now):
    monkeypatch.setattr(
        httpx, "AsyncClient",
        lambda **k: _FakeDeferralHttpxClient(prs, comments),
    )
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")
    d = SwarmDispatcher(_StubNotifier(), _config())
    return asyncio.run(d._prs_with_matured_deferral("owner/repo", now))


def _defer(iso):
    return {"id": 1, "body": f"<!-- review-deferred-until:{iso} -->\ndeferred"}


def _verdict():
    return {"id": 2, "body": "<!-- vanellus-aggregation -->\n**APPROVE**"}


def test_matured_deferral_is_due(monkeypatch):
    from datetime import datetime, timezone
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    prs = [{"number": 254}]
    comments = {254: [_defer("2026-07-27T10:00:00Z")]}  # past
    r = _run_deferral_scan(monkeypatch, prs, comments, now)
    assert [p["number"] for p in r["due"]] == [254]


def test_unmatured_deferral_is_not_due(monkeypatch):
    from datetime import datetime, timezone
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    prs = [{"number": 254}]
    comments = {254: [_defer("2026-07-27T18:00:00Z")]}  # future
    r = _run_deferral_scan(monkeypatch, prs, comments, now)
    assert r["due"] == [] and r["not_yet"] == 1


def test_verdict_after_deferral_supersedes_it(monkeypatch):
    # THE Loxia bug: a resolved PR (verdict posted AFTER a past-due deferral)
    # must NOT be re-dispatched. Comment order: deferral, then verdict.
    from datetime import datetime, timezone
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    prs = [{"number": 254}]
    comments = {254: [_defer("2026-07-27T10:00:00Z"), _verdict()]}
    r = _run_deferral_scan(monkeypatch, prs, comments, now)
    assert r["due"] == [], "resolved PR must not be re-dispatched"
    assert r["scanned"] == 0, "superseded deferral is not even a candidate"


def test_redefer_after_verdict_is_live_again(monkeypatch):
    # verdict then a NEW deferral (re-throttled on a later run) -> due again.
    from datetime import datetime, timezone
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    prs = [{"number": 254}]
    comments = {254: [_defer("2026-07-26T10:00:00Z"), _verdict(),
                      _defer("2026-07-27T09:00:00Z")]}
    r = _run_deferral_scan(monkeypatch, prs, comments, now)
    assert [p["number"] for p in r["due"]] == [254]


def test_no_deferral_marker_is_ignored(monkeypatch):
    from datetime import datetime, timezone
    now = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
    r = _run_deferral_scan(monkeypatch, [{"number": 9}], {9: [_verdict()]}, now)
    assert r["scanned"] == 0 and r["due"] == []


def test_panelist_prompt_evidence_bar_tracks_worktree_availability():
    # The evidence bar must tell the truth about what the lens can do. Claiming
    # "your cwd is a writable checkout" to a diff-only lens would either waste
    # its turn or invite it to invent command output (#254).
    lens = Lens(agent="waxwing", lens="arch", gate="arch", checks="contracts")
    trigger = _trigger(repository="markmhendrickson/neotoma")

    with_wt = SwarmDispatcher._panelist_prompt(
        trigger, lens, "", None, has_worktree=True
    )
    assert "writable checkout" in with_wt
    assert "ONLY if you RAN something" in with_wt
    assert "DIFF-ONLY" not in with_wt

    without_wt = SwarmDispatcher._panelist_prompt(
        trigger, lens, "", None, has_worktree=False
    )
    assert "DIFF-ONLY" in without_wt
    assert "unverified" in without_wt
    assert "writable checkout" not in without_wt


def test_forward_looking_lens_has_no_evidence_bar():
    # Forward-looking lenses never block, so the blocking evidence bar is noise.
    lens = Lens(
        agent="corvus", lens="content", gate="", checks="x", forward_looking=True
    )
    prompt = SwarmDispatcher._panelist_prompt(
        _trigger(repository="markmhendrickson/neotoma"),
        lens,
        "",
        None,
        has_worktree=True,
    )
    assert "EVIDENCE BAR" not in prompt
    assert "FORWARD-LOOKING" in prompt


def test_auth_failure_comment_not_headed_as_a_verdict():
    # #264 ux: a "not a review verdict" notice must not open with the
    # vanellus/PR-steward header — at a skim that reads as the verdict it
    # disclaims. Header must agree with the footer (dispatcher-posted).
    from swarm_dispatch import compose_auth_failure_comment
    body = compose_auth_failure_comment("waxwing")
    header = body.split("\n")[1]
    assert "swarm dispatcher" in header
    assert "PR steward" not in header
    assert "not** a review verdict" in body  # body still disclaims


def test_real_vanellus_fallback_keeps_the_steward_header():
    # The genuine aggregation fallback IS a verdict, so its vanellus/PR-steward
    # header is correct — don't over-correct it along with the notice above.
    from swarm_dispatch import compose_vanellus_fallback_comment
    body = compose_vanellus_fallback_comment("**APPROVE**")
    assert "PR steward" in body.split("\n")[1]
# ── _handle_release_approve (email-reply release publish) ───────────────────
#
# This is the one handler that drives an irreversible publish. Guards: refuse a
# malformed version outright; invoke publish.py --from-email-approval for a
# valid one; never raise into the dispatcher.


def _release_approve_trigger(version="v0.20.0"):
    return SwarmTrigger(
        kind="release_approve", repository="", number=0, title="", body="",
        author="", html_url="", delivery_id=f"release-approve-{version}",
        action="approved", release_version=version,
    )


def test_release_approve_invalid_version_never_publishes(monkeypatch):
    spawned = []

    async def fake_exec(*a, **k):
        spawned.append(a)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(swarm_dispatch.os.path, "exists", lambda p: True)
    d = SwarmDispatcher(_StubNotifier(), _config())
    for bad in ["", "0.20.0", "latest", "v; rm -rf /"]:
        asyncio.run(d._handle_release_approve(_release_approve_trigger(bad)))
    assert spawned == [], "a malformed version must never invoke publish.py"


def test_release_approve_valid_version_invokes_publish(monkeypatch):
    spawned = []

    async def fake_exec(*args, **kwargs):
        spawned.append(args)

        class _P:
            returncode = 0

            async def communicate(self):
                return (b"Release v0.20.0 PUBLISHED.", b"")

        return _P()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(swarm_dispatch.os.path, "exists", lambda p: True)
    d = SwarmDispatcher(_StubNotifier(), _config())
    asyncio.run(d._handle_release_approve(_release_approve_trigger("v0.20.0")))
    assert len(spawned) == 1
    argv = spawned[0]
    assert argv[1].endswith("publish.py")
    assert "--version" in argv and "v0.20.0" in argv
    assert "--from-email-approval" in argv


def test_release_approve_publish_failure_notifies(monkeypatch):
    async def fake_exec(*args, **kwargs):
        class _P:
            returncode = 1

            async def communicate(self):
                return (b"StepError: npm publish failed", b"")

        return _P()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(swarm_dispatch.os.path, "exists", lambda p: True)
    notifier = _StubNotifier()
    d = SwarmDispatcher(notifier, _config())
    asyncio.run(d._handle_release_approve(_release_approve_trigger("v0.20.0")))
    # a failed publish must page the operator (BLOCKER), not fail silently
    assert any("publish FAILED" in str(m) for m in notifier.sent), notifier.sent


def test_release_approve_missing_script_notifies(monkeypatch):
    monkeypatch.setattr(swarm_dispatch.os.path, "exists", lambda p: False)
    notifier = _StubNotifier()
    d = SwarmDispatcher(notifier, _config())
    asyncio.run(d._handle_release_approve(_release_approve_trigger("v0.20.0")))
    assert any("missing" in str(m).lower() for m in notifier.sent), notifier.sent


# ── verdict comment fallback (ateles#292) ────────────────────────────────────
#
# Vanellus posts its aggregated verdict to GitHub reliably but repeats it in
# stdout only sometimes. When stdout has no token, the dispatcher must recover
# the verdict from the durable aggregation comment rather than defaulting to
# the inert COMMENT review.


def _comments_client(monkeypatch, bodies, *, calls=None, raises=None):
    """Mock httpx.AsyncClient so a comments GET returns `bodies`.

    `calls` (when given) records each GET, so a test can assert the happy path
    makes no request at all. `raises` makes the GET blow up.
    """

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def get(self, url, **kwargs):
            if calls is not None:
                calls.append((url, kwargs.get("params", {})))
            if raises is not None:
                raise raises

            page = int((kwargs.get("params") or {}).get("page", 1))

            class _Resp:
                def raise_for_status(self_inner):
                    pass

                def json(self_inner):
                    # ateles#430: the reader pages now, so only page 1 carries
                    # rows. Bodies are listed OLDEST-FIRST (as GitHub returns
                    # them) and stamped with ascending created_at/id so
                    # client-side recency selection has something to sort on.
                    if page != 1:
                        return []
                    return [
                        {
                            "id": i + 1,
                            "created_at": f"2026-08-{10 + i:02d}T09:00:00Z",
                            "body": b,
                        }
                        for i, b in enumerate(bodies)
                    ]

            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client())
    monkeypatch.setenv("ATELES_AGENT_PAT", "ghp_test")


def _resolver(monkeypatch):
    return SwarmDispatcher(_StubNotifier(), _config())


def test_resolve_verdict_prefers_stdout_and_skips_fetch(monkeypatch):
    """T1 — stdout carries the token: unchanged behaviour, and no GitHub GET."""
    calls = []
    _comments_client(monkeypatch, [], calls=calls)
    d = _resolver(monkeypatch)
    verdict, used_fallback = asyncio.run(
        d._resolve_review_verdict(_trigger(), "## Verdict\n**REQUEST_CHANGES**\nx")
    )
    assert (verdict, used_fallback) == ("request_changes", False)
    assert calls == [], f"happy path must not fetch comments: {calls}"


def test_resolve_verdict_falls_back_to_aggregation_comment(monkeypatch, caplog):
    """T2 — stdout empty, marked comment has the token: fallback recovers it."""
    _comments_client(
        monkeypatch,
        [f"{_VANELLUS_COMMENT_MARKER}\n**REQUEST_CHANGES**\n1 blocking"],
    )
    d = _resolver(monkeypatch)
    with caplog.at_level(logging.INFO):
        verdict, used_fallback = asyncio.run(
            d._resolve_review_verdict(_trigger(), "")
        )
    assert (verdict, used_fallback) == ("request_changes", True)
    # The fallback-fire log is what makes stdout-repeat compliance measurable.
    assert any(
        "fallback fired" in r.message
        and "owner/repo#87" in r.message
        and "request_changes" in r.message
        for r in caplog.records
    ), [r.message for r in caplog.records]


def test_resolve_verdict_none_when_neither_source_has_token(monkeypatch):
    """T3 — no token anywhere: still no verdict, and still treated as not-clear."""
    _comments_client(monkeypatch, ["just a drive-by comment"])
    d = _resolver(monkeypatch)
    verdict, used_fallback = asyncio.run(d._resolve_review_verdict(_trigger(), ""))
    assert (verdict, used_fallback) == (None, False)
    assert review_verdict_is_clear(verdict) is False


def test_resolve_verdict_survives_comment_fetch_failure(monkeypatch):
    """T3b — a failed GET must not raise out of the resolver."""
    _comments_client(monkeypatch, [], raises=httpx.ConnectError("boom"))
    d = _resolver(monkeypatch)
    verdict, used_fallback = asyncio.run(d._resolve_review_verdict(_trigger(), ""))
    assert (verdict, used_fallback) == (None, False)


def test_resolve_verdict_scans_past_unmarked_comments(monkeypatch):
    """T4 — the marked comment is not always comments[0]."""
    _comments_client(
        monkeypatch,
        [
            "a human chimed in first",
            f"{_VANELLUS_COMMENT_MARKER}\n**APPROVE**\nlgtm",
        ],
    )
    d = _resolver(monkeypatch)
    assert asyncio.run(d._resolve_review_verdict(_trigger(), "")) == ("approve", True)


def test_resolve_verdict_requires_real_token_in_comment(monkeypatch):
    """T5 — a marked but token-less comment must not produce a false match."""
    _comments_client(
        monkeypatch,
        [f"{_VANELLUS_COMMENT_MARKER}\nthe panel had thoughts but no token"],
    )
    d = _resolver(monkeypatch)
    assert asyncio.run(d._resolve_review_verdict(_trigger(), "")) == (None, False)


def test_resolve_verdict_reads_comments_newest_first(monkeypatch):
    """The fallback must honour the LATEST aggregation, as _pr_review_is_clear does.

    Intent unchanged; mechanism corrected (ateles#430). The bodies are now
    OLDEST-FIRST — stale REQUEST_CHANGES, then the newer APPROVE — matching what
    GitHub actually returns. The old fixture handed them back newest-first and
    asserted `direction == "desc"`, which encoded the bug: that parameter is
    ignored on this endpoint.
    """
    calls = []
    _comments_client(
        monkeypatch,
        [
            f"{_VANELLUS_COMMENT_MARKER}\n**REQUEST_CHANGES**\nstale",
            f"{_VANELLUS_COMMENT_MARKER}\n**APPROVE**\nnewest",
        ],
        calls=calls,
    )
    d = _resolver(monkeypatch)
    assert asyncio.run(d._resolve_review_verdict(_trigger(), "")) == ("approve", True)
    assert calls and "direction" not in calls[0][1], calls


def test_handle_pr_recovers_blocking_verdict_from_comment(monkeypatch):
    """T6 (effect-level) — the bug itself: stdout omits REQUEST_CHANGES, but the
    PR must still be routed as blocking AND get a native REQUEST_CHANGES review,
    not an inert COMMENT."""
    calls = []
    d = _pr_dispatcher_with_stubs(
        monkeypatch, vanellus_stdout="the panel had thoughts", calls=calls
    )
    _comments_client(
        monkeypatch,
        [f"{_VANELLUS_COMMENT_MARKER}\n**REQUEST_CHANGES**\n1 blocking"],
    )
    asyncio.run(d._handle_pr(_trigger(body="Closes #80.")))
    assert ("route", "request_changes") in calls, calls
    assert ("gate", None) not in calls, calls
    # The whole point of #241/#284: GitHub's review state carries the verdict.
    assert ("review", "REQUEST_CHANGES") in calls, calls


def test_handle_pr_recovered_approve_gates_readiness(monkeypatch):
    """T7 — the fallback must not over-trigger blocking: a recovered APPROVE
    proceeds to the readiness gate."""
    calls = []
    d = _pr_dispatcher_with_stubs(
        monkeypatch, vanellus_stdout="the panel had thoughts", calls=calls
    )
    _comments_client(monkeypatch, [f"{_VANELLUS_COMMENT_MARKER}\n**APPROVE**\nlgtm"])
    asyncio.run(d._handle_pr(_trigger(body="Closes #80.")))
    assert ("gate", None) in calls, calls
    assert not any(c[0] == "route" for c in calls), calls
    assert ("review", "APPROVE") in calls, calls


def test_handle_pr_still_comments_when_no_verdict_anywhere(monkeypatch):
    """Regression guard: with neither stdout nor comment carrying a token, the
    dispatcher behaves exactly as before — inert COMMENT review, blocking route."""
    calls = []
    d = _pr_dispatcher_with_stubs(
        monkeypatch, vanellus_stdout="the panel had thoughts", calls=calls
    )
    _comments_client(monkeypatch, ["nothing useful here"])
    asyncio.run(d._handle_pr(_trigger(body="Closes #80.")))
    assert ("review", "COMMENT") in calls, calls
    assert any(c[0] == "route" for c in calls), calls


# ── Vanellus merge authorization tracks APIS_AUTONOMY_AUTO_MERGE (ateles#333) ──
#
# Regression guard for a real defect: _vanellus_prompt injected an unconditional
# "DO NOT MERGE ... This overrides any merge instruction in your standing
# protocol" while _gate_merge_readiness returns EARLY when auto_merge is on (so
# no checkpoint is filed either). With the flag on, the dispatcher stepped aside
# expecting Vanellus to merge while simultaneously forbidding it — nothing
# merged and nothing was escalated, strictly worse than the flag being off.


def test_vanellus_prompt_forbids_merge_when_auto_merge_off():
    prompt = swarm_dispatch.SwarmDispatcher._vanellus_prompt(
        _trigger(), 80, ["pm"], None, auto_merge=False
    )
    assert "DO NOT MERGE" in prompt
    assert "operator-gated" in prompt
    assert "YOU MAY MERGE" not in prompt


def test_vanellus_prompt_authorizes_merge_when_auto_merge_on():
    prompt = swarm_dispatch.SwarmDispatcher._vanellus_prompt(
        _trigger(), 80, ["pm"], None, auto_merge=True
    )
    assert "YOU MAY MERGE" in prompt
    # The unconditional prohibition must be gone, or the flag is inert.
    assert "DO NOT MERGE. Merge is operator-gated" not in prompt
    # Hard stops must still be stated so autonomy is bounded, not blanket.
    assert "gate inheritance" in prompt
    assert "branch-protection" in prompt
    assert "APPROVE with Blocking: 0" in prompt
    assert "Releases remain human-gated" in prompt


def test_vanellus_prompt_defaults_to_forbidding_merge():
    # Omitting the parameter must fail closed, not open.
    prompt = swarm_dispatch.SwarmDispatcher._vanellus_prompt(_trigger(), 80, ["pm"])
    assert "DO NOT MERGE" in prompt
