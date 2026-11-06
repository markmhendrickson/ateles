"""A panel stopped by a Neotoma read that did not complete resumes on its own.

A lens or Vanellus is refused at launch when its agent_policy or
agent_definition cannot be read. The refusal stays (no agent runs without its
policy); what changes is the panel's response: a timeout, connection error or
server 5xx is a temporary failure that auto-resumes through the deferred-review
sweep, bounded by APIS_NEOTOMA_RETRY_LIMIT, rather than stopping for operator
attention. A policy that was read and refuses still stops the panel.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx

from lib import github_app_token as _github_app_token
import swarm_dispatch
from review_panel import Lens
from skill_runner import SkillResult
from swarm_dispatch import DispatchConfig, SwarmDispatcher

import test_swarm_dispatch as tsd

HEAD = "a" * 40
SWARM_LOGIN = "ateles-agent"

POLICY_TIMEOUT = (
    "agent_policy unavailable: agent_policy for 'pavo' is unavailable "
    "(query, timeout); the agent was not run — nothing was dispatched; repair "
    "the load, the task is retried"
)
VANELLUS_POLICY_TIMEOUT = POLICY_TIMEOUT.replace("'pavo'", "'vanellus'")
DEFINITION_REFUSED = (
    "agent_definition unavailable: agent_definition for 'pavo' is unavailable "
    "(refused_status, 1 attempt(s)); the agent was not run — nothing was "
    "dispatched; repair the load, the task is retried"
)


def _policy_timeout(skill: str, error: str = POLICY_TIMEOUT) -> SkillResult:
    return SkillResult(skill, False, None, "", "", error=error, policy_unavailable=True)


def _pm_panel(monkeypatch):
    monkeypatch.setattr(
        swarm_dispatch,
        "select_panel",
        lambda **kwargs: [Lens(agent="pavo", lens="pm", gate="pm", checks="scope")],
    )


class _Comments:
    """A fake GitHub issue-comments endpoint: lists, records posts and deletes."""

    def __init__(self, comments=None):
        self.comments = list(comments or [])
        self.posted: list[str] = []

    def client(self):
        store = self

        class _Client:
            def __init__(self, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, url, **kwargs):
                return tsd._FakeListResp(list(store.comments))

            async def post(self, url, **kwargs):
                body = kwargs.get("json", {}).get("body", "")
                store.posted.append(body)
                store.comments.append(
                    {
                        "id": len(store.comments) + 1,
                        "body": body,
                        "user": {"login": SWARM_LOGIN},
                        "created_at": datetime.now(timezone.utc).strftime(
                            "%Y-%m-%dT%H:%M:%SZ"
                        ),
                    }
                )
                return tsd._FakeResp(201)

            async def delete(self, url, **kwargs):
                comment_id = int(url.rsplit("/", 1)[-1])
                store.comments = [c for c in store.comments if c.get("id") != comment_id]
                return tsd._FakeResp(204)

        return _Client


def _wire_github(monkeypatch, comments: _Comments):
    monkeypatch.setattr(httpx, "AsyncClient", comments.client())
    monkeypatch.setenv("ATELES_AGENT_PAT", "test-token")
    monkeypatch.setattr(
        swarm_dispatch, "lens_comment_authors", lambda: frozenset({SWARM_LOGIN})
    )
    monkeypatch.setattr(_github_app_token, "lens_has_own_app", lambda lens: False)


def _dispatcher(notifier=None):
    return SwarmDispatcher(
        notifier or tsd._StubNotifier(),
        DispatchConfig(neotoma_token="", github_token="x"),
    )


# ── classification ──────────────────────────────────────────────────────────


def test_policy_read_timeout_is_temporary_not_execution_failure():
    """Red before: review_failure_class returned "execution failure"."""
    assert (
        swarm_dispatch.review_failure_class(_policy_timeout("pavo"))
        == swarm_dispatch.NEOTOMA_TEMPORARILY_UNAVAILABLE
    )
    for code in ("transport_error", "server_error"):
        result = _policy_timeout("pavo", POLICY_TIMEOUT.replace("timeout", code))
        assert swarm_dispatch.neotoma_read_failure_is_transient(result)
    # With a provider limit in the same panel, the provider's later reset wins.
    assert swarm_dispatch.incomplete_panel_reason(
        [swarm_dispatch.NEOTOMA_TEMPORARILY_UNAVAILABLE, "usage limit"]
    ) == "provider capacity exhausted"


def test_policy_that_was_read_and_refuses_stays_execution_failure():
    """A real answer from Neotoma is not temporary and stays fail-closed."""
    refused = SkillResult(
        "pavo", False, None, "", "", error=DEFINITION_REFUSED,
        definition_unavailable=True,
    )
    malformed = _policy_timeout("pavo", POLICY_TIMEOUT.replace("timeout", "bad_response"))
    for result in (refused, malformed):
        assert swarm_dispatch.review_failure_class(result) == "execution failure"
        assert swarm_dispatch.incomplete_panel_reason(
            [swarm_dispatch.review_failure_class(result)]
        ) == "incomplete review panel"


# ── panel and Vanellus routing ──────────────────────────────────────────────


def _capture_deferral(monkeypatch):
    deferred: dict = {}

    async def fake_deferral(self, *args, **kwargs):
        deferred.update(kwargs)
        deferred["agent"] = args[2] if len(args) > 2 else None

    monkeypatch.setattr(SwarmDispatcher, "_handle_panel_session_limit", fake_deferral)
    return deferred


def test_pavo_policy_timeout_schedules_resume_without_operator_notice(monkeypatch):
    """Red before: the panel passed reason "incomplete review panel" (operator
    attention) and the comment carried no deferral marker."""
    calls: list = []
    d = tsd._pr_dispatcher_with_stubs(monkeypatch, vanellus_stdout="**APPROVE**", calls=calls)
    _pm_panel(monkeypatch)
    ran: list[str] = []

    async def fake_run_skill(skill, prompt, **kwargs):
        ran.append(skill)
        if skill == "lanius":
            return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
        if skill == "pavo":
            return _policy_timeout("pavo")
        raise AssertionError(f"{skill} must not run after the panel stopped")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    deferred = _capture_deferral(monkeypatch)
    asyncio.run(d._handle_pr(tsd._trigger(body="Closes #80.")))

    assert "vanellus" not in ran
    assert deferred["reason"] == swarm_dispatch.NEOTOMA_TEMPORARILY_UNAVAILABLE
    assert deferred["failed_lenses"] == (
        ("pm", swarm_dispatch.NEOTOMA_TEMPORARILY_UNAVAILABLE),
    )

    # The real deferral: marker posted, INFO notice, no operator attention.
    comments = _Comments()
    notifier = tsd._StubNotifier()
    monkeypatch.undo()  # restore the real _handle_panel_session_limit
    _wire_github(monkeypatch, comments)
    asyncio.run(
        _dispatcher(notifier)._handle_panel_session_limit(
            tsd._trigger(), None, "pm", "", "", reason=deferred["reason"],
            failed_lenses=deferred["failed_lenses"],
        )
    )
    assert notifier.priorities == [swarm_dispatch.Priority.INFO]
    assert len(comments.posted) == 1
    body = comments.posted[0]
    assert SwarmDispatcher._REVIEW_DEFERRED_RE.search(body)
    assert SwarmDispatcher._NEOTOMA_DEFERRAL_RE.search(body).group(1) == "1"
    assert "operator attention" not in body.lower()
    assert "operator attention" not in notifier.sent[0].lower()


def test_vanellus_authority_read_timeout_schedules_resume(monkeypatch):
    """Red before: Vanellus's refusal became "review execution failure"."""
    calls: list = []
    d = tsd._pr_dispatcher_with_stubs(monkeypatch, vanellus_stdout="", calls=calls)
    _pm_panel(monkeypatch)

    async def fake_run_skill(skill, prompt, **kwargs):
        if skill == "lanius":
            return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
        if skill == "vanellus":
            return _policy_timeout("vanellus", VANELLUS_POLICY_TIMEOUT)
        return SkillResult(skill, True, 0, "**APPROVE**", "")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    deferred = _capture_deferral(monkeypatch)
    asyncio.run(d._handle_pr(tsd._trigger(body="Closes #80.")))

    assert deferred["agent"] == "vanellus"
    assert deferred["reason"] == swarm_dispatch.NEOTOMA_TEMPORARILY_UNAVAILABLE
    assert not [c for c in calls if c[0] in {"route", "gate"}]


def test_policy_that_refuses_stops_panel_for_operator_attention(monkeypatch):
    """Unchanged behaviour: a read that answered with a refusal stays stopped."""
    calls: list = []
    d = tsd._pr_dispatcher_with_stubs(monkeypatch, vanellus_stdout="**APPROVE**", calls=calls)
    _pm_panel(monkeypatch)

    async def fake_run_skill(skill, prompt, **kwargs):
        if skill == "lanius":
            return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
        return SkillResult(
            skill, False, None, "", "", error=DEFINITION_REFUSED,
            definition_unavailable=True,
        )

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    deferred = _capture_deferral(monkeypatch)
    asyncio.run(d._handle_pr(tsd._trigger(body="Closes #80.")))
    assert deferred["reason"] == "incomplete review panel"

    comments = _Comments()
    notifier = tsd._StubNotifier()
    monkeypatch.undo()
    _wire_github(monkeypatch, comments)
    asyncio.run(
        _dispatcher(notifier)._handle_panel_session_limit(
            tsd._trigger(), None, "pm", "", "", reason=deferred["reason"],
        )
    )
    assert notifier.priorities == [swarm_dispatch.Priority.BLOCKER]
    assert SwarmDispatcher._REVIEW_DEFERRED_RE.search(comments.posted[0]) is None
    assert "operator attention is required" in comments.posted[0].lower()


def test_repeated_neotoma_failure_past_limit_stops_and_names_neotoma(monkeypatch):
    """Red before: there was no bound and no Neotoma-specific stop at all."""
    monkeypatch.setenv("APIS_NEOTOMA_RETRY_LIMIT", "2")
    comments = _Comments()
    _wire_github(monkeypatch, comments)
    notifier = tsd._StubNotifier()
    d = _dispatcher(notifier)

    def defer():
        asyncio.run(
            d._handle_panel_session_limit(
                tsd._trigger(), None, "pm", "", "",
                reason=swarm_dispatch.NEOTOMA_TEMPORARILY_UNAVAILABLE,
            )
        )

    defer()
    defer()
    assert notifier.priorities == [swarm_dispatch.Priority.INFO] * 2
    live = [c["body"] for c in comments.comments]
    assert len(live) == 1  # the older marker was replaced, not stacked
    assert SwarmDispatcher._NEOTOMA_DEFERRAL_RE.search(live[0]).group(1) == "2"

    defer()  # third consecutive failure: past the limit of 2
    assert notifier.priorities[-1] == swarm_dispatch.Priority.BLOCKER
    assert "neotoma availability" in notifier.sent[-1].lower()
    last = comments.posted[-1]
    assert "operator attention is required" in last.lower()
    assert "neotoma availability" in last.lower()
    assert SwarmDispatcher._REVIEW_DEFERRED_RE.search(last) is None


def test_neotoma_retry_count_resets_after_a_verdict(monkeypatch):
    comments = _Comments(
        [
            {
                "id": 1,
                "body": "<!-- review-deferred-until:2026-11-06T10:00:00Z -->\n"
                + SwarmDispatcher._NEOTOMA_DEFERRAL_MARKER.format(attempt=3, head=HEAD),
                "user": {"login": SWARM_LOGIN},
            },
            {"id": 2, "body": "<!-- vanellus-aggregation -->\n**APPROVE**",
             "user": {"login": SWARM_LOGIN}},
        ]
    )
    _wire_github(monkeypatch, comments)
    monkeypatch.setattr(
        SwarmDispatcher, "_swarm_comment_authors",
        lambda self, repo, number: tsd._async_return(frozenset({SWARM_LOGIN})),
    )
    assert asyncio.run(_dispatcher()._prior_neotoma_deferral_attempts(tsd._trigger())) == 0


# ── resumed panel: fresh verdict is the one saved and shown ─────────────────


def test_resumed_panel_reruns_lens_and_passes_run_start(monkeypatch):
    """A lens that already has a comment at this head runs again; its fresh
    verdict is what is persisted, and the comment backfill is told when this run
    started so the older comment cannot stand in for it."""
    calls: list = []
    d = tsd._pr_dispatcher_with_stubs(monkeypatch, vanellus_stdout="**APPROVE**", calls=calls)
    _pm_panel(monkeypatch)
    ran: list[str] = []

    async def fake_run_skill(skill, prompt, **kwargs):
        ran.append(skill)
        if skill == "lanius":
            return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
        if skill == "pavo":
            return SkillResult(skill, True, 0, "**REQUEST_CHANGES** fresh", "")
        return SkillResult(skill, True, 0, "**APPROVE**", "")

    persisted: list = []
    backfill: dict = {}

    async def fake_persist(self, trigger, reviews, agents, **kwargs):
        persisted.extend(reviews)

    async def fake_backfill(self, trigger, reviews, agents, **kwargs):
        backfill.update(kwargs, reviews=list(reviews))

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    monkeypatch.setattr(SwarmDispatcher, "_persist_panel_reviews", fake_persist)
    monkeypatch.setattr(SwarmDispatcher, "_post_missing_panel_comments", fake_backfill)
    before = datetime.now(timezone.utc).replace(microsecond=0)
    asyncio.run(d._handle_pr(tsd._trigger(kind="pr_synchronize", body="Closes #80.")))

    assert "pavo" in ran
    assert persisted == [("pm", "**REQUEST_CHANGES** fresh")]
    assert backfill["reviews"] == [("pm", "**REQUEST_CHANGES** fresh")]
    assert backfill["posted_since"] is not None and backfill["posted_since"] >= before


def test_backfill_posts_fresh_verdict_over_an_earlier_runs_comment(monkeypatch):
    """Red before: lenses_missing_comments saw the earlier comment at the same
    head, posted nothing, and the stale APPROVE stayed the newest one shown."""
    old_time = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    stale = swarm_dispatch.compose_fallback_comment("pm", "pavo", "**APPROVE** stale", HEAD)
    comments = _Comments(
        [{"id": 1, "body": stale, "user": {"login": SWARM_LOGIN}, "created_at": old_time}]
    )
    _wire_github(monkeypatch, comments)
    run_start = datetime.now(timezone.utc).replace(microsecond=0)
    asyncio.run(
        _dispatcher()._post_missing_panel_comments(
            tsd._trigger(),
            [("pm", "**REQUEST_CHANGES** fresh")],
            {"pm": "pavo"},
            reviewed_head=HEAD,
            posted_since=run_start,
        )
    )
    assert len(comments.posted) == 1
    assert "fresh" in comments.posted[0]
    # The newest pm comment at this head is the fresh one.
    newest = [c for c in comments.comments if "review:pm" in c["body"]][-1]
    assert "fresh" in newest["body"]

    # A comment the lens posted during THIS run still counts: no duplicate.
    comments.posted.clear()
    asyncio.run(
        _dispatcher()._post_missing_panel_comments(
            tsd._trigger(),
            [("pm", "**REQUEST_CHANGES** fresh")],
            {"pm": "pavo"},
            reviewed_head=HEAD,
            posted_since=run_start,
        )
    )
    assert comments.posted == []


# ── one panel per head at a time ────────────────────────────────────────────


def test_second_sweep_skips_running_panel_and_guard_clears(monkeypatch):
    """Red before: there was no guard, so the second entry ran a second panel."""
    d = _dispatcher()
    entered: list[str] = []
    release = asyncio.Event()

    async def slow_panel(self, trigger):
        entered.append(trigger.delivery_id)
        await release.wait()

    monkeypatch.setattr(SwarmDispatcher, "_handle_pr_panel", slow_panel)

    async def scenario():
        first = asyncio.create_task(d._handle_pr(tsd._trigger(delivery_id="one")))
        await asyncio.sleep(0)
        assert d._panel_running(tsd._trigger())
        await d._handle_pr(tsd._trigger(delivery_id="two"))  # skipped
        release.set()
        await first
        assert not d._panel_running(tsd._trigger())
        await d._handle_pr(tsd._trigger(delivery_id="three"))  # runs again

    asyncio.run(scenario())
    assert entered == ["one", "three"]


def test_panel_guard_clears_after_an_exception(monkeypatch):
    d = _dispatcher()

    async def boom(self, trigger):
        raise RuntimeError("panel crashed")

    monkeypatch.setattr(SwarmDispatcher, "_handle_pr_panel", boom)
    try:
        asyncio.run(d._handle_pr(tsd._trigger()))
    except RuntimeError:
        pass
    assert not d._panel_running(tsd._trigger())


def test_deferred_sweep_skips_a_head_whose_panel_is_running(monkeypatch):
    d = _dispatcher()
    pr = {"number": 87, "title": "t", "head": {"sha": HEAD, "ref": "b"},
          "base": {"ref": "main"}, "user": {"login": "someone"}}

    async def due(self, repository, now):
        return {"due": [pr], "scanned": 1, "not_yet": 0}

    handled: list = []

    async def fake_handle(self, trigger):
        handled.append(trigger.number)

    monkeypatch.setattr(SwarmDispatcher, "_await_github_app_ready", lambda self, r: tsd._async_return(None))
    monkeypatch.setattr(SwarmDispatcher, "_prs_with_matured_deferral", due)
    monkeypatch.setattr(SwarmDispatcher, "_label_gate_allows", lambda self, t, source: tsd._async_return(True))
    monkeypatch.setattr(SwarmDispatcher, "_handle_pr", fake_handle)
    d.__dict__["_panels_running"] = {("owner/repo", 87, HEAD)}
    summary = asyncio.run(d.resume_deferred_reviews(["owner/repo"]))
    assert handled == []
    assert summary["already_running"] == 1
