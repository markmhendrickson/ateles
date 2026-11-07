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
from lib.daemon_runtime import AgentDefinitionUnavailable
import skill_runner
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


def test_pavo_definition_timeout_schedules_resume_without_operator_notice(monkeypatch):
    """A definition read that timed out defers like a policy timeout. Red when
    the definition classification is removed: the panel passed reason
    "incomplete review panel" (operator attention) instead of deferring."""
    # The exact refusal skill_runner builds from a definition-read timeout.
    definition_timeout = (
        f"{skill_runner.DEFINITION_UNAVAILABLE_PREFIX}: "
        f"{AgentDefinitionUnavailable('pavo', 'timeout', 3)} — nothing was "
        "dispatched; repair the load, the task is retried"
    )
    calls: list = []
    d = tsd._pr_dispatcher_with_stubs(monkeypatch, vanellus_stdout="**APPROVE**", calls=calls)
    _pm_panel(monkeypatch)
    ran: list[str] = []

    async def fake_run_skill(skill, prompt, **kwargs):
        ran.append(skill)
        if skill == "lanius":
            return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
        if skill == "pavo":
            return SkillResult(
                skill, False, None, "", "", error=definition_timeout,
                definition_unavailable=True,
            )
        raise AssertionError(f"{skill} must not run after the panel stopped")

    monkeypatch.setattr(swarm_dispatch, "run_skill", fake_run_skill)
    deferred = _capture_deferral(monkeypatch)
    asyncio.run(d._handle_pr(tsd._trigger(body="Closes #80.")))

    # No agent ran: pavo was refused at launch and nothing ran after it.
    assert ran == ["lanius", "pavo"]
    assert not [c for c in calls if c[0] in {"route", "gate"}]
    assert deferred["reason"] == swarm_dispatch.NEOTOMA_TEMPORARILY_UNAVAILABLE

    # The real deferral: INFO notice, resume marker, no operator attention.
    pr = {"number": 87, "head": {"sha": HEAD}}

    class _Sweep(_Comments):
        def client(self):
            base = super().client()

            class _Client(base):
                async def get(self, url, **kwargs):
                    if url.endswith("/pulls"):
                        return tsd._FakeListResp([pr])
                    return await super().get(url, **kwargs)

            return _Client

    comments = _Sweep()
    notifier = tsd._StubNotifier()
    monkeypatch.undo()
    _wire_github(monkeypatch, comments)
    dispatcher = _dispatcher(notifier)
    asyncio.run(
        dispatcher._handle_panel_session_limit(
            tsd._trigger(), None, "pm", "", "", reason=deferred["reason"],
            failed_lenses=deferred["failed_lenses"],
        )
    )
    assert notifier.priorities == [swarm_dispatch.Priority.INFO]
    assert len(comments.posted) == 1
    body = comments.posted[0]
    assert "operator attention" not in body.lower()
    assert "operator attention" not in notifier.sent[0].lower()
    due_iso = SwarmDispatcher._REVIEW_DEFERRED_RE.search(body).group(1)
    assert SwarmDispatcher._NEOTOMA_DEFERRAL_RE.search(body).group(1) == "1"

    # A later sweep, after the marker matures, resumes the panel on this PR.
    due = datetime.strptime(due_iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    later = asyncio.run(
        dispatcher._prs_with_matured_deferral("owner/repo", due + timedelta(seconds=1))
    )
    assert later["due"] == [pr]


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


def test_malformed_retry_limit_allows_no_automatic_retry(monkeypatch, caplog):
    """Red before: a malformed limit fell back to 3, so this posted a resume marker."""
    monkeypatch.setenv("APIS_NEOTOMA_RETRY_LIMIT", "three")
    comments = _Comments()
    _wire_github(monkeypatch, comments)
    notifier = tsd._StubNotifier()
    with caplog.at_level("WARNING", logger="apis.swarm_dispatch"):
        asyncio.run(
            _dispatcher(notifier)._handle_panel_session_limit(
                tsd._trigger(), None, "pm", "", "",
                reason=swarm_dispatch.NEOTOMA_TEMPORARILY_UNAVAILABLE,
            )
        )
    assert notifier.priorities == [swarm_dispatch.Priority.BLOCKER]
    last = comments.posted[-1]
    assert SwarmDispatcher._REVIEW_DEFERRED_RE.search(last) is None
    assert SwarmDispatcher._NEOTOMA_DEFERRAL_RE.search(last) is None
    assert "operator attention is required" in last.lower()
    warnings = [r.getMessage() for r in caplog.records if "APIS_NEOTOMA_RETRY_LIMIT" in r.getMessage()]
    assert warnings and all("three" not in w for w in warnings)


def test_unset_retry_limit_defaults_to_three(monkeypatch):
    monkeypatch.delenv("APIS_NEOTOMA_RETRY_LIMIT", raising=False)
    assert swarm_dispatch._neotoma_retry_limit() == 3
    monkeypatch.setenv("APIS_NEOTOMA_RETRY_LIMIT", "5")
    assert swarm_dispatch._neotoma_retry_limit() == 5
    monkeypatch.setenv("APIS_NEOTOMA_RETRY_LIMIT", "x")
    assert swarm_dispatch._neotoma_retry_limit() == 0


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


def test_resumed_panel_reruns_lens(monkeypatch):
    """A lens that already has a comment at this head runs again; its fresh
    verdict is what is persisted and handed to the comment backfill."""
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
    asyncio.run(d._handle_pr(tsd._trigger(kind="pr_synchronize", body="Closes #80.")))

    assert "pavo" in ran
    assert persisted == [("pm", "**REQUEST_CHANGES** fresh")]
    assert backfill["reviews"] == [("pm", "**REQUEST_CHANGES** fresh")]


# ── long threads: every page of comments is read ────────────────────────────


class _PagedComments(_Comments):
    """`_Comments` that serves GitHub's 100-per-page pagination and can fail
    a chosen page."""

    def __init__(self, comments=None, fail_page=None):
        super().__init__(comments)
        self.fail_page = fail_page
        self.next_id = max((c["id"] for c in self.comments), default=0) + 1

    def client(self):
        store = self
        base = super().client()

        class _Client(base):
            async def get(self, url, **kwargs):
                params = kwargs.get("params") or {}
                page = int(params.get("page", 1))
                per_page = int(params.get("per_page", 30))
                if page == store.fail_page:
                    return tsd._FakeListResp([], status_code=502)
                start = (page - 1) * per_page
                return tsd._FakeListResp(list(store.comments[start:start + per_page]))

            async def post(self, url, **kwargs):
                body = kwargs.get("json", {}).get("body", "")
                store.posted.append(body)
                store.comments.append(
                    {"id": store.next_id, "body": body,
                     "user": {"login": SWARM_LOGIN},
                     "created_at": datetime.now(timezone.utc).strftime(
                         "%Y-%m-%dT%H:%M:%SZ")}
                )
                store.next_id += 1
                return tsd._FakeResp(201)

        return _Client


def _chatter(n):
    return [
        {"id": i, "body": f"discussion {i}", "user": {"login": "someone"},
         "created_at": "2026-11-01T10:00:00Z"}
        for i in range(1, n + 1)
    ]


def _wire_long_thread(monkeypatch, comments):
    _wire_github(monkeypatch, comments)
    monkeypatch.setattr(
        SwarmDispatcher, "_swarm_comment_authors",
        lambda self, repo, number: tsd._async_return(frozenset({SWARM_LOGIN})),
    )


def test_retry_limit_holds_on_a_thread_past_100_comments(monkeypatch):
    """Red before: only the first 100 comments were read, so the marker (on a
    later page) was never seen; every deferral said "retry 1 of 3" and "No
    action needed", and the limit was never reached."""
    monkeypatch.setenv("APIS_NEOTOMA_RETRY_LIMIT", "3")
    comments = _PagedComments(_chatter(130))
    _wire_long_thread(monkeypatch, comments)
    notifier = tsd._StubNotifier()
    d = _dispatcher(notifier)

    def defer():
        asyncio.run(
            d._handle_panel_session_limit(
                tsd._trigger(), None, "pm", "", "",
                reason=swarm_dispatch.NEOTOMA_TEMPORARILY_UNAVAILABLE,
            )
        )

    for attempt in (1, 2, 3):
        defer()
        markers = [
            c["body"] for c in comments.comments
            if SwarmDispatcher._REVIEW_DEFERRED_RE.search(c["body"])
        ]
        assert len(markers) == 1  # the older marker past page 1 was removed
        assert SwarmDispatcher._NEOTOMA_DEFERRAL_RE.search(markers[0]).group(1) == str(attempt)
        assert f"retry {attempt} of 3" in notifier.sent[-1]
    assert notifier.priorities == [swarm_dispatch.Priority.INFO] * 3

    defer()  # past the limit: stops for attention and names Neotoma
    assert notifier.priorities[-1] == swarm_dispatch.Priority.BLOCKER
    assert "neotoma availability" in notifier.sent[-1].lower()
    assert "after 3 automatic retries" in notifier.sent[-1]
    assert "no action needed" not in notifier.sent[-1].lower()
    assert "operator attention is required" in comments.posted[-1].lower()
    assert SwarmDispatcher._REVIEW_DEFERRED_RE.search(comments.posted[-1]) is None


def test_failed_read_of_a_later_comment_page_stops_the_panel(monkeypatch):
    """Red before: page 2 was never requested, so its failure went unseen and
    the count restarted at zero (another "retry 1", no attention)."""
    marker = (
        SwarmDispatcher._REVIEW_DEFERRED_MARKER.format(iso="2026-11-06T10:00:00Z")
        + "\n" + SwarmDispatcher._NEOTOMA_DEFERRAL_MARKER.format(attempt=1, head=HEAD)
    )
    comments = _PagedComments(
        _chatter(120) + [{"id": 121, "body": marker, "user": {"login": SWARM_LOGIN}}],
        fail_page=2,
    )
    _wire_long_thread(monkeypatch, comments)
    notifier = tsd._StubNotifier()
    d = _dispatcher(notifier)
    assert asyncio.run(d._prior_neotoma_deferral_attempts(tsd._trigger())) is None
    asyncio.run(
        d._handle_panel_session_limit(
            tsd._trigger(), None, "pm", "", "",
            reason=swarm_dispatch.NEOTOMA_TEMPORARILY_UNAVAILABLE,
        )
    )
    assert notifier.priorities == [swarm_dispatch.Priority.BLOCKER]
    assert "neotoma availability" in notifier.sent[-1].lower()
    assert "retry count could not be confirmed" in notifier.sent[-1]


def _deferral(cid, attempt):
    return {
        "id": cid,
        "body": SwarmDispatcher._REVIEW_DEFERRED_MARKER.format(
            iso="2026-11-06T10:00:00Z"
        ) + "\n" + SwarmDispatcher._NEOTOMA_DEFERRAL_MARKER.format(
            attempt=attempt, head=HEAD
        ),
        "user": {"login": SWARM_LOGIN},
    }


def test_malformed_latest_marker_after_one_at_the_limit_does_not_retry(monkeypatch):
    """Red before: the malformed latest marker failed the count regex, so the
    count read as 0 and a fresh "retry 1" was scheduled although an earlier
    marker had already reached the limit."""
    monkeypatch.setenv("APIS_NEOTOMA_RETRY_LIMIT", "2")
    malformed = _deferral(2, "x")
    assert SwarmDispatcher._NEOTOMA_DEFERRAL_RE.search(malformed["body"]) is None
    comments = _Comments([_deferral(1, 2), malformed])
    _wire_long_thread(monkeypatch, comments)
    notifier = tsd._StubNotifier()
    d = _dispatcher(notifier)
    assert asyncio.run(d._prior_neotoma_deferral_attempts(tsd._trigger())) is None
    asyncio.run(
        d._handle_panel_session_limit(
            tsd._trigger(), None, "pm", "", "",
            reason=swarm_dispatch.NEOTOMA_TEMPORARILY_UNAVAILABLE,
        )
    )
    assert notifier.priorities == [swarm_dispatch.Priority.BLOCKER]
    assert "retry count could not be confirmed" in notifier.sent[-1]
    assert SwarmDispatcher._REVIEW_DEFERRED_RE.search(comments.posted[-1]) is None


def test_deferral_of_another_kind_still_counts_as_no_neotoma_retries(monkeypatch):
    other = {
        "id": 2,
        "body": SwarmDispatcher._REVIEW_DEFERRED_MARKER.format(
            iso="2026-11-06T10:00:00Z"
        ),
        "user": {"login": SWARM_LOGIN},
    }
    comments = _Comments([_deferral(1, 2), other])
    _wire_long_thread(monkeypatch, comments)
    assert asyncio.run(_dispatcher()._prior_neotoma_deferral_attempts(tsd._trigger())) == 0


def test_deferred_sweep_sees_a_marker_past_the_first_page(monkeypatch):
    """Red before: both sweep reads stopped at the first 100 comments."""
    marker = SwarmDispatcher._REVIEW_DEFERRED_MARKER.format(iso="2026-11-06T10:00:00Z")
    thread = _chatter(140) + [{"id": 141, "body": marker, "user": {"login": SWARM_LOGIN}}]
    pr = {"number": 87, "head": {"sha": HEAD}}

    class _Sweep(_PagedComments):
        def client(self):
            base = super().client()

            class _Client(base):
                async def get(self, url, **kwargs):
                    if url.endswith("/pulls"):
                        return tsd._FakeListResp([pr])
                    return await super().get(url, **kwargs)

            return _Client

    comments = _Sweep(thread)
    _wire_long_thread(monkeypatch, comments)
    d = _dispatcher()
    now = datetime(2026, 11, 6, 11, 0, tzinfo=timezone.utc)
    result = asyncio.run(d._prs_with_matured_deferral("owner/repo", now))
    assert result["due"] == [pr]

    async def live():
        async with httpx.AsyncClient() as client:
            return await d._has_live_deferral_marker(client, "owner/repo", 87)

    comments.comments.append(
        {"id": 142, "body": "<!-- vanellus-aggregation -->\n**APPROVE**",
         "user": {"login": SWARM_LOGIN}}
    )
    assert asyncio.run(live()) is False
    comments.comments.pop()
    assert asyncio.run(live()) is True


# ── backoff ─────────────────────────────────────────────────────────────────


def test_neotoma_retry_delay_backs_off_and_is_capped(monkeypatch):
    """Red before: every attempt used the same flat delay."""
    monkeypatch.setenv("APIS_NEOTOMA_RETRY_DELAY_SECONDS", "120")
    schedule = [swarm_dispatch._neotoma_retry_delay_seconds(n) for n in range(1, 8)]
    cap = swarm_dispatch._NEOTOMA_RETRY_MAX_DELAY_SECONDS
    assert schedule == [120, 240, 480, 960, 1920, cap, cap]
    assert cap == 3600


def test_successive_deferrals_schedule_increasing_resume_times(monkeypatch):
    monkeypatch.setenv("APIS_NEOTOMA_RETRY_LIMIT", "5")
    monkeypatch.setenv("APIS_NEOTOMA_RETRY_DELAY_SECONDS", "120")
    comments = _Comments()
    _wire_github(monkeypatch, comments)
    d = _dispatcher()
    delays = []
    for _ in range(4):
        before = datetime.now(timezone.utc)
        asyncio.run(
            d._handle_panel_session_limit(
                tsd._trigger(), None, "pm", "", "",
                reason=swarm_dispatch.NEOTOMA_TEMPORARILY_UNAVAILABLE,
            )
        )
        iso = SwarmDispatcher._REVIEW_DEFERRED_RE.search(comments.posted[-1]).group(1)
        due = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        delays.append(round((due - before).total_seconds() / 60))
    assert delays == [2, 4, 8, 16]


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
    d.__dict__["_panel_heads_running"] = {("owner/repo", 87, HEAD)}
    summary = asyncio.run(d.resume_deferred_reviews(["owner/repo"]))
    assert handled == []
    assert summary["already_running"] == 1
