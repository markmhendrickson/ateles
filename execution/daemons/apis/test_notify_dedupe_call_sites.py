"""Effect tests for Notifier dedupe_key wiring at apis call sites (ateles#1165)."""

from __future__ import annotations

import asyncio
import logging

from lib.notify import Notifier
from execution.daemons.apis import swarm_dispatch
from execution.daemons.apis.swarm_dispatch import (
    ReviewBindingReceipt,
    SwarmDispatcher,
    agent_github_login,
)
from execution.daemons.apis.test_swarm_dispatch import (
    SkillResult,
    _async_return,
    _config,
    _issue_trigger,
    _trigger,
)

# No configured window at all → _in_silence_window() always returns False
# (empty strings fail the int() parse in Notifier._in_silence_window, which
# is caught and treated as "not silent"). "22:00"-"08:00" is a real nightly
# window, not a never-silent one — that previous form made every one of
# these tests flaky between 22:00 and 08:00 Europe/Madrid, since the send it
# asserts on gets queued to the held-notice digest instead of delivered.
_NEVER_SILENT = {
    "timezone": "Europe/Madrid",
    "silence_start": "",
    "silence_end": "",
}


def _notifier(tmp_path, sent, *, deliver_kw=None):
    deliver_kw = deliver_kw if deliver_kw is not None else []

    def _deliver(m, force=False, email_eligible=True):
        deliver_kw.append({"email_eligible": email_eligible})
        sent.append(m)
        return True

    n = Notifier(rubric=_NEVER_SILENT)
    n._dedupe_path = tmp_path / "dedupe.json"
    n._digest_path = tmp_path / "digest.json"
    n._deliver = _deliver
    return n


def _dispatcher(tmp_path, sent, deliver_kw=None, **cfg):
    return SwarmDispatcher(
        _notifier(tmp_path, sent, deliver_kw=deliver_kw),
        _config(**cfg),
    )


# ── #1 CI exhausted ───────────────────────────────────────────────────────────


def test_ci_exhausted_second_identical_trigger_does_not_send(
    monkeypatch, tmp_path, caplog
):
    sent = []

    async def at_cap(self, trigger):
        return 2

    monkeypatch.setattr(SwarmDispatcher, "_fix_round_count", at_cap)
    monkeypatch.setattr(
        SwarmDispatcher, "_pr_head_sha", lambda self, t: _async_return("c" * 40)
    )
    monkeypatch.setattr(
        SwarmDispatcher,
        "_failing_check_runs",
        lambda self, t: _async_return(
            [{"name": "ci", "html_url": "https://github.com/runs/1"}]
        ),
    )

    d = _dispatcher(tmp_path, sent)
    trig = _trigger()
    with caplog.at_level(logging.INFO):
        assert asyncio.run(d._route_ci_failure(trig, 80)) is None
        first = d.notifier.send  # noqa: F841 — exercised via route
        asyncio.run(d._route_ci_failure(trig, 80))
    assert len(sent) == 1
    assert "call clear_dedupe() once it resolves" in caplog.text


def test_ci_exhausted_new_head_sends_again(monkeypatch, tmp_path):
    sent = []
    heads = ["a" * 40, "b" * 40]
    idx = {"i": 0}

    async def head(self, trigger):
        i = idx["i"]
        idx["i"] += 1
        return heads[min(i, 1)]

    async def at_cap(self, trigger):
        return 2

    monkeypatch.setattr(SwarmDispatcher, "_fix_round_count", at_cap)
    monkeypatch.setattr(SwarmDispatcher, "_pr_head_sha", head)
    monkeypatch.setattr(
        SwarmDispatcher,
        "_failing_check_runs",
        lambda self, t: _async_return(
            [{"name": "ci", "html_url": "https://github.com/runs/1"}]
        ),
    )

    d = _dispatcher(tmp_path, sent)
    asyncio.run(d._route_ci_failure(_trigger(), 80))
    asyncio.run(d._route_ci_failure(_trigger(), 80))
    assert len(sent) == 2


def test_ci_exhausted_clear_on_green_allows_resend(monkeypatch, tmp_path):
    sent = []
    head = "d" * 40

    async def at_cap(self, trigger):
        return 2

    monkeypatch.setattr(SwarmDispatcher, "_fix_round_count", at_cap)
    monkeypatch.setattr(
        SwarmDispatcher, "_pr_head_sha", lambda self, t: _async_return(head)
    )
    monkeypatch.setattr(
        SwarmDispatcher,
        "_failing_check_runs",
        lambda self, t: _async_return(
            [{"name": "ci", "html_url": "https://github.com/runs/1"}]
        ),
    )
    monkeypatch.setattr(
        SwarmDispatcher,
        "_store_merge_checkpoint",
        lambda self, *a, **k: _async_return(None),
    )

    d = _dispatcher(tmp_path, sent)
    asyncio.run(d._route_ci_failure(_trigger(), 80))
    asyncio.run(d._route_ci_failure(_trigger(), 80))
    assert len(sent) == 1

    # Merge-readiness green path clears the exhaustion key.
    asyncio.run(
        d._gate_merge_readiness(
            _trigger(), 80, panel=[], ci_state="green", reviewed_head=head
        )
    )
    asyncio.run(d._route_ci_failure(_trigger(), 80))
    assert len(sent) >= 2


def test_ci_exhausted_clears_when_ci_status_green_even_if_review_uncleared(
    monkeypatch, tmp_path
):
    """CI-status green must clear even when review is not yet clear."""
    sent = []
    head = "c" * 40

    async def at_cap(self, trigger):
        return 2

    monkeypatch.setattr(SwarmDispatcher, "_fix_round_count", at_cap)
    monkeypatch.setattr(
        SwarmDispatcher, "_pr_head_sha", lambda self, t: _async_return(head)
    )
    monkeypatch.setattr(
        SwarmDispatcher,
        "_failing_check_runs",
        lambda self, t: _async_return(
            [{"name": "ci", "html_url": "https://github.com/runs/1"}]
        ),
    )
    monkeypatch.setattr(
        SwarmDispatcher,
        "_required_ci_state",
        lambda self, t: _async_return("green"),
    )
    monkeypatch.setattr(
        SwarmDispatcher,
        "_pr_review_is_clear",
        lambda self, *a, **k: _async_return(False),
    )
    monkeypatch.setattr(
        SwarmDispatcher,
        "_pr_trigger_from_api",
        lambda self, repo, pr: _trigger(head_sha=head),
    )
    monkeypatch.setattr(
        SwarmDispatcher, "_parent_issue_number", lambda self, *a, **k: 80
    )

    d = _dispatcher(tmp_path, sent)
    asyncio.run(d._route_ci_failure(_trigger(head_sha=head), 80))
    assert len(sent) == 1

    pr = {
        "number": 87,
        "title": "t",
        "body": "Closes #80.",
        "user": {"login": "someone"},
        "html_url": "https://github.com/owner/repo/pull/87",
        "head": {"ref": "feature", "sha": head},
        "base": {"ref": "main"},
    }
    asyncio.run(
        d._handle_ci_status_for_current_head(
            _trigger(kind="ci_status", head_sha=head), pr, head
        )
    )
    asyncio.run(d._route_ci_failure(_trigger(head_sha=head), 80))
    assert len(sent) == 2


def test_ci_exhausted_unactionable_body_sets_email_eligible_false(
    monkeypatch, tmp_path
):
    sent = []
    deliver_kw = []

    async def at_cap(self, trigger):
        return 2

    monkeypatch.setattr(SwarmDispatcher, "_fix_round_count", at_cap)
    monkeypatch.setattr(
        SwarmDispatcher, "_pr_head_sha", lambda self, t: _async_return("e" * 40)
    )
    monkeypatch.setattr(
        SwarmDispatcher, "_failing_check_runs", lambda self, t: _async_return([])
    )

    d = _dispatcher(tmp_path, sent, deliver_kw=deliver_kw)
    asyncio.run(d._route_ci_failure(_trigger(), 80))
    assert deliver_kw and deliver_kw[0]["email_eligible"] is False


def test_ci_exhausted_body_includes_failing_check_and_run_link(monkeypatch, tmp_path):
    sent = []

    async def at_cap(self, trigger):
        return 2

    monkeypatch.setattr(SwarmDispatcher, "_fix_round_count", at_cap)
    monkeypatch.setattr(
        SwarmDispatcher, "_pr_head_sha", lambda self, t: _async_return("f" * 40)
    )
    monkeypatch.setattr(
        SwarmDispatcher,
        "_failing_check_runs",
        lambda self, t: _async_return(
            [{"name": "unit-tests", "html_url": "https://github.com/runs/99"}]
        ),
    )

    d = _dispatcher(tmp_path, sent)
    asyncio.run(d._route_ci_failure(_trigger(), 80))
    assert "unit-tests" in sent[0]
    assert "https://github.com/runs/99" in sent[0]
    assert "APIS_MAX_FIX_ROUNDS" in sent[0]


# ── #2 skill updates (drive _handle_pr learning pass) ───────────────────────


def _skill_proposal(rule: str = "Always declare fields") -> list[dict]:
    return [
        {
            "entity_type": "proposed_skill_update",
            "title": "Learn",
            "finding_category": "schema",
            "proposed_rule": rule,
            "status": "proposed",
        }
    ]


def _pr_dispatcher_learning(monkeypatch, tmp_path, sent, *, deliver_kw=None):
    """Reach the learning-pass Notifier.send inside production `_handle_pr`."""

    async def fake_run_skill(skill, prompt, **kwargs):
        if skill == "lanius":
            return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
        if skill == "vanellus":
            return SkillResult(skill, True, 0, "**APPROVE**\nlgtm", "")
        return SkillResult(skill, True, 0, "**COMMENT**\nlgtm", "")

    async def fake_store(self, entities, idempotency_key):
        return {"entities": [{"entity_id": "ent_skill_1"}]}

    async def fake_emit(self, trigger, verdict, body, **kwargs):
        return ReviewBindingReceipt(
            review_id="1",
            reviewer_login=agent_github_login("vanellus"),
            commit_id="a" * 40,
            state="APPROVED",
        )

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(SwarmDispatcher, "_store_entities", fake_store)
    monkeypatch.setattr(
        SwarmDispatcher, "_pr_head_sha", lambda self, t: _async_return("a" * 40)
    )
    monkeypatch.setattr(
        SwarmDispatcher, "_changed_files", lambda self, t: _async_return(["x"])
    )
    monkeypatch.setattr(
        SwarmDispatcher, "_route_blocking_findings", lambda *a, **k: _async_return(None)
    )
    monkeypatch.setattr(
        SwarmDispatcher, "_gate_merge_readiness", lambda *a, **k: _async_return(None)
    )
    monkeypatch.setattr(
        SwarmDispatcher, "_persist_panel_reviews", lambda *a, **k: _async_return(True)
    )
    monkeypatch.setattr(
        SwarmDispatcher, "_post_missing_panel_comments", lambda *a, **k: _async_return(None)
    )
    monkeypatch.setattr(
        SwarmDispatcher,
        "_post_missing_vanellus_comment",
        lambda *a, **k: _async_return(None),
    )
    monkeypatch.setattr(
        SwarmDispatcher,
        "_preregistered_expectations",
        lambda self, r, p: _async_return({}),
    )
    monkeypatch.setattr(
        SwarmDispatcher, "_claim_escalation", lambda *a, **k: _async_return(True)
    )
    monkeypatch.setattr(SwarmDispatcher, "_emit_formal_review", fake_emit)
    return SwarmDispatcher(
        _notifier(tmp_path, sent, deliver_kw=deliver_kw), _config()
    )


def _skill_sends(sent):
    return [m for m in sent if "systemic review finding" in m]


def test_skill_updates_identical_digest_suppresses(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(
        swarm_dispatch, "propose_skill_updates", lambda reviews, pr_ref: _skill_proposal()
    )
    d = _pr_dispatcher_learning(monkeypatch, tmp_path, sent)
    asyncio.run(d._handle_pr(_trigger()))
    asyncio.run(d._handle_pr(_trigger()))
    assert len(_skill_sends(sent)) == 1


def test_skill_updates_new_digest_sends(monkeypatch, tmp_path):
    sent = []
    rules = ["Always declare fields", "Different rule text"]
    idx = {"i": 0}

    def proposals(reviews, pr_ref):
        rule = rules[idx["i"]]
        idx["i"] += 1
        return _skill_proposal(rule)

    monkeypatch.setattr(swarm_dispatch, "propose_skill_updates", proposals)
    d = _pr_dispatcher_learning(monkeypatch, tmp_path, sent)
    asyncio.run(d._handle_pr(_trigger()))
    asyncio.run(d._handle_pr(_trigger()))
    assert len(_skill_sends(sent)) == 2


def test_skill_updates_body_names_finding_and_approve_path(monkeypatch, tmp_path):
    sent = []
    deliver_kw = []
    monkeypatch.setattr(
        swarm_dispatch, "propose_skill_updates", lambda reviews, pr_ref: _skill_proposal()
    )
    d = _pr_dispatcher_learning(monkeypatch, tmp_path, sent, deliver_kw=deliver_kw)
    trig = _trigger()
    asyncio.run(d._handle_pr(trig))
    body = _skill_sends(sent)[0]
    assert "Finding: schema" in body
    assert "Proposed rule: Always declare fields" in body
    assert trig.html_url in body
    assert "ent_skill_1" in body
    skill_kw = [
        kw
        for kw, msg in zip(deliver_kw, sent)
        if "systemic review finding" in msg
    ]
    assert skill_kw and skill_kw[0]["email_eligible"] is True


# ── #3 spec ready (drive _run_issue_spec_pipeline) ──────────────────────────


def _spec_pipeline_dispatcher(monkeypatch, tmp_path, sent, *, auto_build, pr_url=None):
    """Stub the issue pipeline against *this* module's swarm_dispatch import.

    ``_install_pipeline_stubs`` patches the short-name ``swarm_dispatch`` module
    used by ``test_swarm_dispatch``; this file imports
    ``execution.daemons.apis.swarm_dispatch``, which is a separate module object
    under pytest's path setup. Patch here so production ``run_skill`` is never
    invoked (that would spawn ``claude`` and hang the suite).
    """
    from execution.daemons.apis.test_swarm_dispatch import _FakeSpecStore

    async def fake_run_skill(skill, prompt, **kwargs):
        if skill == "lanius":
            return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
        return SkillResult(
            skill, True, 0, "<<<SPEC_SECTION>>>text<<<END_SPEC_SECTION>>>", ""
        )

    _FakeSpecStore.instances = []
    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(swarm_dispatch, "IssueSpecStore", _FakeSpecStore)
    monkeypatch.setattr(
        swarm_dispatch, "select_expectation_agents", lambda *a, **kw: []
    )

    class _ClearGateState:
        found = True
        gate_status = {
            "pm": "signed_off",
            "ux": "signed_off",
            "arch": "signed_off",
        }

    async def fake_gate_load(self, repo, issue_number):
        return _ClearGateState()

    monkeypatch.setattr(swarm_dispatch.IssueGateStore, "load", fake_gate_load)
    monkeypatch.setattr(
        SwarmDispatcher, "_mirror_spec_to_issue", lambda *a, **k: _async_return(None)
    )
    monkeypatch.setattr(
        SwarmDispatcher, "_gates_green", lambda *a, **k: _async_return(True)
    )
    monkeypatch.setattr(
        SwarmDispatcher,
        "_open_implementation_pr",
        lambda *a, **k: _async_return(pr_url),
    )
    return SwarmDispatcher(
        _notifier(tmp_path, sent),
        _config(auto_build=auto_build),
    )


def _spec_ready_sends(sent):
    return [
        m
        for m in sent
        if "additive spec assembled" in m or "awaiting `build` approval" in m
    ]


def test_spec_ready_second_trigger_suppresses(monkeypatch, tmp_path):
    sent = []
    d = _spec_pipeline_dispatcher(monkeypatch, tmp_path, sent, auto_build=False)
    trig = _issue_trigger(html_url="https://github.com/owner/repo/issues/100")
    asyncio.run(d._run_issue_spec_pipeline(trig))
    asyncio.run(d._run_issue_spec_pipeline(trig))
    assert len(_spec_ready_sends(sent)) == 1


def test_spec_ready_info_path_does_not_consume_dedupe_key(monkeypatch, tmp_path):
    sent = []
    d_info = _spec_pipeline_dispatcher(
        monkeypatch, tmp_path, sent, auto_build=True, pr_url="https://pr/1"
    )
    trig = _issue_trigger(html_url="https://github.com/owner/repo/issues/100")
    key = f"spec-ready:{trig.repository}#{trig.number}"
    asyncio.run(d_info._run_issue_spec_pipeline(trig))
    # Priority.INFO is never delivered (Notifier drops it) and must not mark
    # the dedupe key — so a later OPERATOR_DECISION for the same condition
    # can still reach the inbox.
    assert sent == []
    assert not d_info.notifier._is_duplicate(key)

    d_wait = SwarmDispatcher(d_info.notifier, _config(auto_build=False))
    monkeypatch.setattr(
        SwarmDispatcher, "_gates_green", lambda *a, **k: _async_return(True)
    )
    monkeypatch.setattr(
        SwarmDispatcher,
        "_open_implementation_pr",
        lambda *a, **k: _async_return(None),
    )
    asyncio.run(d_wait._run_issue_spec_pipeline(trig))
    assert len(_spec_ready_sends(sent)) == 1
    assert d_wait.notifier._is_duplicate(key)


def test_spec_ready_clears_when_pr_opens(monkeypatch, tmp_path):
    sent = []
    trig = _issue_trigger(html_url="https://github.com/owner/repo/issues/100")
    d_wait = _spec_pipeline_dispatcher(monkeypatch, tmp_path, sent, auto_build=False)
    asyncio.run(d_wait._run_issue_spec_pipeline(trig))
    assert len(_spec_ready_sends(sent)) == 1
    key = f"spec-ready:{trig.repository}#{trig.number}"
    assert d_wait.notifier._is_duplicate(key)

    d_build = SwarmDispatcher(d_wait.notifier, _config(auto_build=True))
    monkeypatch.setattr(
        SwarmDispatcher, "_gates_green", lambda *a, **k: _async_return(True)
    )
    monkeypatch.setattr(
        SwarmDispatcher,
        "_open_implementation_pr",
        lambda *a, **k: _async_return("https://pr/2"),
    )
    asyncio.run(d_build._run_issue_spec_pipeline(trig))
    assert not d_build.notifier._is_duplicate(key)

    d_wait2 = SwarmDispatcher(d_wait.notifier, _config(auto_build=False))
    monkeypatch.setattr(
        SwarmDispatcher, "_gates_green", lambda *a, **k: _async_return(True)
    )
    asyncio.run(d_wait2._run_issue_spec_pipeline(trig))
    # wait + INFO(clear, undelivered) + wait-again → 2 delivered sends
    assert len(_spec_ready_sends(sent)) == 2


def test_spec_ready_body_includes_issue_url_and_exact_approve_action(
    monkeypatch, tmp_path
):
    sent = []
    d = _spec_pipeline_dispatcher(monkeypatch, tmp_path, sent, auto_build=False)
    trig = _issue_trigger(html_url="https://github.com/owner/repo/issues/100")
    asyncio.run(d._run_issue_spec_pipeline(trig))
    body = _spec_ready_sends(sent)[0]
    assert trig.html_url in body
    assert "ATELES_SWARM_AUTO_BUILD=1" in body
    assert "_gates_green" in body


# ── #4 self-review refused ──────────────────────────────────────────────────


def _pr_dispatcher_binding_fail(monkeypatch, tmp_path, sent, *, claim_returns=True):
    calls = {"claim": 0}

    async def fake_claim(self, trigger, kind):
        calls["claim"] += 1
        return claim_returns

    async def fake_run_skill(skill, prompt, **kwargs):
        if skill == "lanius":
            return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
        if skill == "vanellus":
            return SkillResult(skill, True, 0, "**APPROVE**\nlgtm", "")
        return SkillResult(skill, True, 0, "**COMMENT**\nlgtm", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(SwarmDispatcher, "_pr_head_sha", lambda self, t: _async_return("a" * 40))
    monkeypatch.setattr(SwarmDispatcher, "_changed_files", lambda self, t: _async_return(["x"]))
    monkeypatch.setattr(SwarmDispatcher, "_route_blocking_findings", lambda *a, **k: _async_return(None))
    monkeypatch.setattr(SwarmDispatcher, "_gate_merge_readiness", lambda *a, **k: _async_return(None))
    monkeypatch.setattr(SwarmDispatcher, "_persist_panel_reviews", lambda *a, **k: _async_return(True))
    monkeypatch.setattr(SwarmDispatcher, "_post_missing_panel_comments", lambda *a, **k: _async_return(None))
    monkeypatch.setattr(
        SwarmDispatcher, "_preregistered_expectations", lambda self, r, p: _async_return({})
    )
    monkeypatch.setattr(SwarmDispatcher, "_claim_escalation", fake_claim)

    async def fake_emit(self, trigger, verdict, body, **kwargs):
        return None

    monkeypatch.setattr(SwarmDispatcher, "_emit_formal_review", fake_emit)
    return SwarmDispatcher(_notifier(tmp_path, sent), _config()), calls


def test_self_review_refused_global_key_once_across_prs(monkeypatch, tmp_path):
    sent = []
    d, _ = _pr_dispatcher_binding_fail(monkeypatch, tmp_path, sent, claim_returns=True)
    asyncio.run(d._handle_pr(_trigger(number=1)))
    asyncio.run(d._handle_pr(_trigger(number=2)))
    assert len(sent) == 1
    assert "1139" in sent[0]


def test_self_review_refused_claim_false_still_sends_once(monkeypatch, tmp_path):
    sent = []
    d, calls = _pr_dispatcher_binding_fail(
        monkeypatch, tmp_path, sent, claim_returns=False
    )
    asyncio.run(d._handle_pr(_trigger()))
    assert calls["claim"] == 1
    assert len(sent) == 1


def test_self_review_refused_clear_then_resend(monkeypatch, tmp_path):
    sent = []
    d, _ = _pr_dispatcher_binding_fail(monkeypatch, tmp_path, sent, claim_returns=True)
    asyncio.run(d._handle_pr(_trigger()))
    assert len(sent) == 1

    async def fake_emit(self, trigger, verdict, body, **kwargs):
        return ReviewBindingReceipt(
            review_id="1",
            reviewer_login="markmhendrickson-ateles-vanellus",
            commit_id="a" * 40,
            state="APPROVED",
        )

    monkeypatch.setattr(SwarmDispatcher, "_emit_formal_review", fake_emit)
    asyncio.run(d._handle_pr(_trigger()))
    refusal_sends = [m for m in sent if "1139" in m]
    assert len(refusal_sends) == 1

    async def fake_emit_fail(self, trigger, verdict, body, **kwargs):
        return None

    monkeypatch.setattr(SwarmDispatcher, "_emit_formal_review", fake_emit_fail)
    asyncio.run(d._handle_pr(_trigger()))
    refusal_sends = [m for m in sent if "1139" in m]
    assert len(refusal_sends) == 2


def test_self_review_refused_body_cites_1139_and_is_not_per_pr(monkeypatch, tmp_path):
    sent = []
    d, _ = _pr_dispatcher_binding_fail(monkeypatch, tmp_path, sent)
    asyncio.run(d._handle_pr(_trigger()))
    assert "issues/1139" in sent[0]
    assert "standing self-review defect" in sent[0]


# ── #6 fix exhausted ────────────────────────────────────────────────────────


def test_fix_exhausted_head_keyed_suppresses_then_new_head_sends(
    monkeypatch, tmp_path
):
    sent = []

    async def fake_count(self, trigger):
        return 2

    monkeypatch.setattr(SwarmDispatcher, "_fix_round_count", fake_count)
    monkeypatch.setattr(SwarmDispatcher, "_claim_escalation", lambda *a, **k: _async_return(True))
    monkeypatch.setattr(
        SwarmDispatcher, "_pr_head_sha", lambda self, t: _async_return("a" * 40)
    )

    d = _dispatcher(tmp_path, sent)
    reviews = [("qa", "[BLOCKING] x: y\nz")]
    asyncio.run(
        d._route_blocking_findings(
            _trigger(), 80, reviews, "request_changes", reviewed_head="a" * 40
        )
    )
    asyncio.run(
        d._route_blocking_findings(
            _trigger(), 80, reviews, "request_changes", reviewed_head="a" * 40
        )
    )
    assert len(sent) == 1
    assert _trigger().html_url in sent[0]

    monkeypatch.setattr(
        SwarmDispatcher, "_pr_head_sha", lambda self, t: _async_return("b" * 40)
    )
    asyncio.run(
        d._route_blocking_findings(
            _trigger(), 80, reviews, "request_changes", reviewed_head="b" * 40
        )
    )
    assert len(sent) == 2


def test_fix_exhausted_claim_false_still_sends_once(monkeypatch, tmp_path):
    sent = []

    async def fake_count(self, trigger):
        return 2

    monkeypatch.setattr(SwarmDispatcher, "_fix_round_count", fake_count)
    monkeypatch.setattr(
        SwarmDispatcher, "_claim_escalation", lambda *a, **k: _async_return(False)
    )
    monkeypatch.setattr(
        SwarmDispatcher, "_pr_head_sha", lambda self, t: _async_return("a" * 40)
    )

    d = _dispatcher(tmp_path, sent)
    reviews = [("qa", "[BLOCKING] x: y\nz")]
    asyncio.run(
        d._route_blocking_findings(
            _trigger(), 80, reviews, "request_changes", reviewed_head="a" * 40
        )
    )
    assert len(sent) == 1


# ── #7 unparseable ──────────────────────────────────────────────────────────


def test_unparseable_verdict_head_keyed_suppresses_then_new_head_sends(
    monkeypatch, tmp_path
):
    sent = []

    async def no_skill(skill, prompt, **kwargs):
        raise AssertionError("no dispatch")

    monkeypatch.setattr(swarm_dispatch, "run_skill", no_skill)
    monkeypatch.setattr(
        SwarmDispatcher, "_claim_escalation", lambda *a, **k: _async_return(True)
    )

    d = _dispatcher(tmp_path, sent)
    reviews = [("pm", "vague")]
    trig = _trigger()
    asyncio.run(
        d._route_blocking_findings(
            trig, 80, reviews, "request_changes", reviewed_head="a" * 40
        )
    )
    asyncio.run(
        d._route_blocking_findings(
            trig, 80, reviews, "request_changes", reviewed_head="a" * 40
        )
    )
    assert len(sent) == 1
    assert trig.html_url in sent[0]

    asyncio.run(
        d._route_blocking_findings(
            trig, 80, reviews, "request_changes", reviewed_head="b" * 40
        )
    )
    assert len(sent) == 2


def test_unparseable_verdict_claim_false_still_sends_once(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(
        swarm_dispatch,
        "run_skill",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no")),
    )
    monkeypatch.setattr(
        SwarmDispatcher, "_claim_escalation", lambda *a, **k: _async_return(False)
    )
    d = _dispatcher(tmp_path, sent)
    asyncio.run(
        d._route_blocking_findings(
            _trigger(), 80, [("pm", "vague")], "request_changes", reviewed_head="a" * 40
        )
    )
    assert len(sent) == 1


# ── #8 process blocked ──────────────────────────────────────────────────────


def test_process_blocked_head_keyed_suppresses_then_new_head_sends(
    monkeypatch, tmp_path
):
    sent = []
    monkeypatch.setattr(
        swarm_dispatch,
        "run_skill",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no")),
    )
    monkeypatch.setattr(
        SwarmDispatcher, "_claim_escalation", lambda *a, **k: _async_return(True)
    )
    d = _dispatcher(tmp_path, sent)
    reviews = [("pm", "gate missing")]
    asyncio.run(
        d._route_blocking_findings(
            _trigger(),
            80,
            reviews,
            "blocked",
            reviewed_head="c" * 40,
        )
    )
    asyncio.run(
        d._route_blocking_findings(
            _trigger(),
            80,
            reviews,
            "blocked",
            reviewed_head="c" * 40,
        )
    )
    assert len(sent) == 1

    asyncio.run(
        d._route_blocking_findings(
            _trigger(),
            80,
            reviews,
            "blocked",
            reviewed_head="d" * 40,
        )
    )
    assert len(sent) == 2


def test_process_blocked_claim_false_still_sends_once(monkeypatch, tmp_path):
    sent = []
    monkeypatch.setattr(
        swarm_dispatch,
        "run_skill",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("no")),
    )
    monkeypatch.setattr(
        SwarmDispatcher, "_claim_escalation", lambda *a, **k: _async_return(False)
    )
    d = _dispatcher(tmp_path, sent)
    asyncio.run(
        d._route_blocking_findings(
            _trigger(), 80, [("pm", "gate missing")], "blocked", reviewed_head="c" * 40
        )
    )
    assert len(sent) == 1
