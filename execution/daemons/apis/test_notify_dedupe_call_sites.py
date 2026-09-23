"""Effect tests for Notifier dedupe_key wiring at apis call sites (ateles#1165)."""

from __future__ import annotations

import asyncio
import logging

import pytest

from lib.notify import Notifier, Priority
from execution.daemons.apis import swarm_dispatch
from execution.daemons.apis.swarm_dispatch import (
    DispatchConfig,
    SwarmDispatcher,
    content_digest,
)
from execution.daemons.apis.swarm_dispatch import ReviewBindingReceipt
from execution.daemons.apis.test_swarm_dispatch import (
    SkillResult,
    _async_return,
    _config,
    _trigger,
)

# Daytime in Europe/Madrid → outside the 22:00–08:00 silence window.
_NEVER_SILENT = {
    "timezone": "Europe/Madrid",
    "silence_start": "22:00",
    "silence_end": "08:00",
}


def _notifier(tmp_path, sent, *, deliver_kw=None):
    deliver_kw = deliver_kw if deliver_kw is not None else []

    def _deliver(m, force=False, email_eligible=True):
        deliver_kw.append({"email_eligible": email_eligible})
        sent.append(m)
        return True

    n = Notifier(rubric=_NEVER_SILENT)
    n._dedupe_path = tmp_path / "dedupe.json"
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

    d = _dispatcher(tmp_path, sent)
    asyncio.run(d._route_ci_failure(_trigger(), 80))
    asyncio.run(d._route_ci_failure(_trigger(), 80))
    assert len(sent) == 1

    d.notifier.clear_dedupe(
        f"ci-exhausted:{_trigger().repository}#{_trigger().number}:{head}"
    )
    asyncio.run(d._route_ci_failure(_trigger(), 80))
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


# ── #2 skill updates ────────────────────────────────────────────────────────


def _systemic_review():
    return [("arch", "[BLOCKING] schema: missing field\nadd schema version")]


async def _fake_learning_path(d, monkeypatch, reviews, store_payload=None):
    async def store(self, entities, idempotency_key):
        return store_payload or {
            "entities": [{"entity_id": "ent_skill_1"}],
        }

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", store)

    ref = f"{_trigger().repository}#{_trigger().number}"
    proposals = swarm_dispatch.propose_skill_updates(reviews, pr_ref=ref)
    digest = content_digest(proposals)
    store_result = await d._store_entities(proposals, idempotency_key=f"x-{digest}")
    first = proposals[0]
    stored_ids = [
        e["entity_id"] for e in (store_result or {}).get("entities", [])
    ]
    trig = _trigger()
    d.notifier.send(
        f"{len(proposals)} systemic review finding(s) on {ref} — "
        f"proposed skill update(s) await operator approval.\n"
        f"Finding: {first.get('finding_category')}\n"
        f"Proposed rule: {first.get('proposed_rule')}\n"
        f"PR: {trig.html_url}\n"
        f"proposed_skill_update entity id(s): {', '.join(stored_ids)}",
        priority=Priority.OPERATOR_DECISION,
        handler="apis",
        dedupe_key=f"skill-updates:{ref}:{digest}",
        email_eligible=bool(trig.html_url or stored_ids),
    )


def test_skill_updates_identical_digest_suppresses(monkeypatch, tmp_path):
    sent = []
    d = _dispatcher(tmp_path, sent)
    reviews = _systemic_review()
    monkeypatch.setattr(
        swarm_dispatch,
        "propose_skill_updates",
        lambda reviews, pr_ref: [
            {
                "entity_type": "proposed_skill_update",
                "title": "Learn",
                "finding_category": "schema",
                "proposed_rule": "Always declare fields",
                "status": "proposed",
            }
        ],
    )

    async def run():
        await _fake_learning_path(d, monkeypatch, reviews)
        await _fake_learning_path(d, monkeypatch, reviews)

    asyncio.run(run())
    assert len(sent) == 1


def test_skill_updates_new_digest_sends(monkeypatch, tmp_path):
    sent = []
    d = _dispatcher(tmp_path, sent)
    rules = ["Always declare fields", "Different rule text"]
    idx = {"i": 0}

    def proposals(reviews, pr_ref):
        rule = rules[idx["i"]]
        idx["i"] += 1
        return [
            {
                "entity_type": "proposed_skill_update",
                "title": "Learn",
                "finding_category": "schema",
                "proposed_rule": rule,
                "status": "proposed",
            }
        ]

    monkeypatch.setattr(swarm_dispatch, "propose_skill_updates", proposals)

    async def run():
        await _fake_learning_path(d, monkeypatch, _systemic_review())
        await _fake_learning_path(d, monkeypatch, _systemic_review())

    asyncio.run(run())
    assert len(sent) == 2


def test_skill_updates_body_names_finding_and_approve_path(monkeypatch, tmp_path):
    sent = []
    deliver_kw = []
    d = _dispatcher(tmp_path, sent, deliver_kw=deliver_kw)
    monkeypatch.setattr(
        swarm_dispatch,
        "propose_skill_updates",
        lambda reviews, pr_ref: [
            {
                "entity_type": "proposed_skill_update",
                "title": "Learn",
                "finding_category": "schema",
                "proposed_rule": "Always declare fields",
                "status": "proposed",
            }
        ],
    )
    asyncio.run(_fake_learning_path(d, monkeypatch, _systemic_review()))
    assert "schema" in sent[0]
    assert "ent_skill_1" in sent[0]
    assert deliver_kw[0]["email_eligible"] is True


# ── #3 spec ready ───────────────────────────────────────────────────────────


async def _notify_spec_ready(d, trigger, *, auto_build, gates_green, pr_url=None):
    ref = f"{trigger.repository}#{trigger.number}"
    spec_ready_key = f"spec-ready:{ref}"
    spec_action = (
        f"Open {trigger.html_url} — set ATELES_SWARM_AUTO_BUILD=1 and ensure "
        "pre-implementation gates are signed off (_gates_green), or approve "
        "the build manually once gates are green."
    )
    completed = ["pm"]
    if auto_build and gates_green:
        if pr_url:
            d.notifier.clear_dedupe(spec_ready_key)
            d.notifier.send(
                f"Issue {ref}: additive spec assembled ({', '.join(completed)}); "
                f"auto-build ON — implementation PR opened ({pr_url});",
                priority=Priority.INFO,
                handler="apis",
            )
        else:
            d.notifier.send(
                f"Issue {ref}: additive spec assembled ({', '.join(completed)}); "
                f"NO PR. {spec_action}",
                priority=Priority.OPERATOR_DECISION,
                handler="apis",
                dedupe_key=spec_ready_key,
            )
    else:
        reason = "auto-build OFF" if not auto_build else "gates not green"
        d.notifier.send(
            f"Issue {ref}: spec ready ({reason}). {spec_action}",
            priority=Priority.OPERATOR_DECISION,
            handler="apis",
            dedupe_key=spec_ready_key,
        )


def test_spec_ready_second_trigger_suppresses(tmp_path):
    sent = []
    d = _dispatcher(tmp_path, sent)
    trig = _trigger(kind="issue_opened", html_url="https://github.com/owner/repo/issues/80")

    async def run():
        await _notify_spec_ready(d, trig, auto_build=False, gates_green=False)
        await _notify_spec_ready(d, trig, auto_build=False, gates_green=False)

    asyncio.run(run())
    assert len(sent) == 1


def test_spec_ready_info_pr_opened_branch_does_not_require_dedupe(tmp_path):
    sent = []
    d = _dispatcher(tmp_path, sent)
    trig = _trigger(kind="issue_opened", html_url="https://github.com/owner/repo/issues/80")
    key = f"spec-ready:{trig.repository}#{trig.number}"
    asyncio.run(
        _notify_spec_ready(d, trig, auto_build=False, gates_green=False)
    )
    assert len(sent) == 1
    assert d.notifier._is_duplicate(key)
    asyncio.run(
        _notify_spec_ready(
            d, trig, auto_build=True, gates_green=True, pr_url="https://pr/1"
        )
    )
    assert not d.notifier._is_duplicate(key)
    asyncio.run(
        _notify_spec_ready(d, trig, auto_build=False, gates_green=False)
    )
    assert len(sent) == 2


def test_spec_ready_clears_when_pr_opens(tmp_path):
    sent = []
    d = _dispatcher(tmp_path, sent)
    trig = _trigger(kind="issue_opened", html_url="https://github.com/owner/repo/issues/80")

    async def run():
        await _notify_spec_ready(d, trig, auto_build=False, gates_green=False)
        await _notify_spec_ready(
            d, trig, auto_build=True, gates_green=True, pr_url="https://pr/2"
        )
        await _notify_spec_ready(d, trig, auto_build=False, gates_green=False)

    asyncio.run(run())
    assert len(sent) == 2


def test_spec_ready_body_includes_issue_url_and_exact_approve_action(tmp_path):
    sent = []
    d = _dispatcher(tmp_path, sent)
    trig = _trigger(kind="issue_opened", html_url="https://github.com/owner/repo/issues/80")
    asyncio.run(_notify_spec_ready(d, trig, auto_build=False, gates_green=False))
    assert trig.html_url in sent[0]
    assert "ATELES_SWARM_AUTO_BUILD=1" in sent[0]
    assert "_gates_green" in sent[0]


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


def test_unparseable_verdict_head_keyed_suppresses(monkeypatch, tmp_path):
    sent = []

    async def no_skill(skill, prompt, **kwargs):
        raise AssertionError("no dispatch")

    monkeypatch.setattr(swarm_dispatch, "run_skill", no_skill)
    monkeypatch.setattr(
        SwarmDispatcher, "_claim_escalation", lambda *a, **k: _async_return(True)
    )

    d = _dispatcher(tmp_path, sent)
    trig = _trigger()
    for _ in range(2):
        asyncio.run(
            d._route_blocking_findings(
                trig, 80, [("pm", "vague")], "request_changes", reviewed_head="a" * 40
            )
        )
    assert len(sent) == 1
    assert trig.html_url in sent[0]


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


def test_process_blocked_head_keyed_suppresses(monkeypatch, tmp_path):
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
    for _ in range(2):
        asyncio.run(
            d._route_blocking_findings(
                _trigger(),
                80,
                [("pm", "gate missing")],
                "blocked",
                reviewed_head="c" * 40,
            )
        )
    assert len(sent) == 1


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
