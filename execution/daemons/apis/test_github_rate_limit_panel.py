"""A short GitHub 403/429 rate-limit burst must not end a review panel.

The PR read waits out a rate-limit answer (403/429 with `Retry-After`, or a
body naming a secondary rate limit) in place; when it does not clear, the panel
is deferred with a resume marker instead of stopping for attention. A plain 403
and a head that really changed keep stopping the panel.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

import swarm_dispatch
import test_swarm_dispatch as tsd
from swarm_dispatch import DispatchConfig, SwarmDispatcher

HEAD = "a" * 40


class _Resp:
    def __init__(self, status, *, headers=None, text="", payload=None):
        self.status_code = status
        self.headers = headers or {}
        self.text = text
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}", request=None, response=None
            )

    def json(self):
        return self._payload


def _client_returning(monkeypatch, responses, gets):
    queue = list(responses)

    class _Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, **kwargs):
            gets.append(url)
            return queue.pop(0)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)


def _limited(retry_after="7", status=403):
    return _Resp(
        status,
        headers={"Retry-After": retry_after, "X-RateLimit-Remaining": "4321"},
        text='{"message": "You have exceeded a secondary rate limit."}',
    )


def _ok():
    return _Resp(200, payload={"head": {"sha": HEAD}})


def _dispatcher():
    return SwarmDispatcher(
        tsd._StubNotifier(), DispatchConfig(neotoma_token="", github_token="x")
    )


def _record_sleeps(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(swarm_dispatch, "_rate_limit_sleep", fake_sleep)
    return sleeps


def test_burst_that_clears_within_retries_returns_the_head(monkeypatch, caplog):
    gets: list[str] = []
    sleeps = _record_sleeps(monkeypatch)
    _client_returning(monkeypatch, [_limited("7"), _limited("500"), _ok()], gets)
    d = _dispatcher()
    with caplog.at_level(logging.WARNING):
        head = asyncio.run(d._pr_head_sha_for("owner/repo", 87))
    assert head == HEAD
    assert len(gets) == 3
    # The header's wait, capped at 120 seconds.
    assert sleeps == [7, 120]
    assert not d._pr_read_rate_limited("owner/repo", 87)
    logged = caplog.text
    assert "Retry-After=7" in logged and "X-RateLimit-Remaining=4321" in logged
    assert "test-token" not in logged


def test_burst_that_does_not_clear_defers_the_panel(monkeypatch):
    gets: list[str] = []
    _record_sleeps(monkeypatch)
    _client_returning(monkeypatch, [_limited()] * 3, gets)
    d = _dispatcher()
    assert asyncio.run(d._pr_head_sha_for("owner/repo", 87)) is None
    assert len(gets) == swarm_dispatch._GITHUB_RATE_LIMIT_ATTEMPTS
    assert d._pr_read_rate_limited("owner/repo", 87)

    # The deferral itself: a resume marker and no call for attention.
    comment_bodies: list[str] = []

    class _CapturingClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, **kwargs):
            return tsd._FakeListResp([])

        async def post(self, url, **kwargs):
            comment_bodies.append(kwargs.get("json", {}).get("body", ""))
            return tsd._FakeResp(201)

    monkeypatch.setattr(httpx, "AsyncClient", _CapturingClient)
    monkeypatch.setenv("ATELES_AGENT_PAT", "test-token")
    asyncio.run(
        d._handle_panel_session_limit(
            tsd._trigger(number=87), None, "panel", "", "",
            reason=swarm_dispatch.GITHUB_TEMPORARILY_RATE_LIMITED,
        )
    )
    assert d.notifier.priorities == [swarm_dispatch.Priority.INFO]
    assert len(comment_bodies) == 1
    assert d._REVIEW_DEFERRED_RE.search(comment_bodies[0])


def test_429_burst_is_retried_bounded_then_marked_and_deferred(monkeypatch):
    # The classifier takes 429 as well as 403; this drives `_github_pr_get`
    # (through `_pr_head_sha_for`) with 429s the same way the 403 test does.
    # Red check: with 429 dropped from `github_rate_limit_wait`'s status set,
    # the first 429 was returned at once (one GET, no sleep, no marking).
    gets: list[str] = []
    sleeps = _record_sleeps(monkeypatch)
    _client_returning(
        monkeypatch, [_limited("9", status=429)] * swarm_dispatch._GITHUB_RATE_LIMIT_ATTEMPTS, gets
    )
    d = _dispatcher()
    assert asyncio.run(d._pr_head_sha_for("owner/repo", 87)) is None
    assert len(gets) == swarm_dispatch._GITHUB_RATE_LIMIT_ATTEMPTS
    assert sleeps == [9] * (swarm_dispatch._GITHUB_RATE_LIMIT_ATTEMPTS - 1)
    assert d._pr_read_rate_limited("owner/repo", 87)

    comment_bodies: list[str] = []

    class _CapturingClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def get(self, url, **kwargs):
            return tsd._FakeListResp([])

        async def post(self, url, **kwargs):
            comment_bodies.append(kwargs.get("json", {}).get("body", ""))
            return tsd._FakeResp(201)

    monkeypatch.setattr(httpx, "AsyncClient", _CapturingClient)
    monkeypatch.setenv("ATELES_AGENT_PAT", "test-token")
    asyncio.run(
        d._handle_panel_session_limit(
            tsd._trigger(number=87), None, "panel", "", "",
            reason=swarm_dispatch.GITHUB_TEMPORARILY_RATE_LIMITED,
        )
    )
    assert d.notifier.priorities == [swarm_dispatch.Priority.INFO]
    assert len(comment_bodies) == 1
    assert d._REVIEW_DEFERRED_RE.search(comment_bodies[0])


def test_rate_limited_head_read_in_panel_defers_instead_of_stopping(monkeypatch):
    calls: list = []
    d = tsd._pr_dispatcher_with_stubs(monkeypatch, vanellus_stdout="**APPROVE**", calls=calls)
    heads = iter([HEAD, None])

    async def head(_t):
        value = next(heads)
        limited = d.__dict__.setdefault("_rate_limited_pr_reads", set())
        if value is None:
            limited.add(("owner/repo", 87))
        return value

    monkeypatch.setattr(d, "_pr_head_sha", head)
    reasons: list[str] = []

    async def capture(self_t, parent, agent, stdout, stderr, *, reason="usage limit", **kw):
        reasons.append(reason)

    monkeypatch.setattr(d, "_handle_panel_session_limit", capture)
    tsd._comments_client(monkeypatch, [])
    asyncio.run(d._handle_pr(tsd._trigger(body="Closes #80.")))
    assert reasons == [swarm_dispatch.GITHUB_TEMPORARILY_RATE_LIMITED]
    assert not any(kind in {"review", "route", "gate"} for kind, _ in calls), calls


def test_plain_403_is_still_a_permanent_failure(monkeypatch):
    gets: list[str] = []
    sleeps = _record_sleeps(monkeypatch)
    _client_returning(
        monkeypatch, [_Resp(403, text='{"message": "Resource not accessible"}')], gets
    )
    d = _dispatcher()
    assert asyncio.run(d._pr_head_sha_for("owner/repo", 87)) is None
    assert len(gets) == 1 and sleeps == []
    assert not d._pr_read_rate_limited("owner/repo", 87)
    assert swarm_dispatch.github_rate_limit_wait(_Resp(403)) is None
    assert swarm_dispatch.github_rate_limit_wait(_Resp(500, headers={"Retry-After": "5"})) is None


def test_real_head_change_still_stops_even_after_a_rate_limit(monkeypatch):
    calls: list = []
    d = tsd._pr_dispatcher_with_stubs(monkeypatch, vanellus_stdout="**APPROVE**", calls=calls)
    heads = iter([HEAD, "b" * 40])

    async def head(_t):
        # A rate-limited read earlier in the run must not turn a head that was
        # read and differs into a deferral.
        d.__dict__.setdefault("_rate_limited_pr_reads", set()).add(("owner/repo", 87))
        return next(heads)

    monkeypatch.setattr(d, "_pr_head_sha", head)
    reasons: list[str] = []

    async def capture(self_t, parent, agent, stdout, stderr, *, reason="usage limit", **kw):
        reasons.append(reason)

    monkeypatch.setattr(d, "_handle_panel_session_limit", capture)
    tsd._comments_client(monkeypatch, [])
    asyncio.run(d._handle_pr(tsd._trigger(body="Closes #80.")))
    assert reasons == ["PR head changed during review or could not be verified"]
    assert not any(kind in {"review", "route", "gate"} for kind, _ in calls), calls


# ── the deferral is bounded (failure_posture.md rule 5) ─────────────────────

from test_panel_neotoma_resume import _Comments, _wire_github  # noqa: E402


def _defer_on_rate_limit(d):
    asyncio.run(
        d._handle_panel_session_limit(
            tsd._trigger(number=87), None, "panel", "", "",
            reason=swarm_dispatch.GITHUB_TEMPORARILY_RATE_LIMITED,
        )
    )


def test_rate_limit_deferral_counts_retries_at_one_head(monkeypatch):
    # Red before this fix, observed in the code: the GitHub deferral carried
    # no count, so neither notice said "retry N of L" and no GitHub marker
    # was posted.
    monkeypatch.delenv("APIS_NEOTOMA_RETRY_LIMIT", raising=False)
    comments = _Comments()
    _wire_github(monkeypatch, comments)
    d = _dispatcher()
    _defer_on_rate_limit(d)
    _defer_on_rate_limit(d)
    assert d.notifier.priorities == [swarm_dispatch.Priority.INFO] * 2
    assert "(retry 1 of 3)" in d.notifier.sent[0]
    assert "(retry 2 of 3)" in d.notifier.sent[1]
    assert "retry 1 of 3" in comments.posted[0]
    assert "retry 2 of 3" in comments.posted[1]
    live = [c["body"] for c in comments.comments]
    assert len(live) == 1  # the older marker was replaced, not stacked
    m = SwarmDispatcher._GITHUB_DEFERRAL_RE.search(live[0])
    assert m and m.group(1) == "2"
    # A GitHub marker is a deferral of another kind for the Neotoma count.
    assert asyncio.run(d._prior_neotoma_deferral_attempts(tsd._trigger(number=87))) == 0


def test_rate_limit_deferral_stops_at_the_limit(monkeypatch):
    # Red before this fix, observed in the code: every rate-limit deferral
    # auto-resumed with a fixed delay, so the third one posted a fourth resume
    # marker and an INFO notice instead of stopping.
    monkeypatch.setenv("APIS_NEOTOMA_RETRY_LIMIT", "2")
    comments = _Comments()
    _wire_github(monkeypatch, comments)
    d = _dispatcher()
    d.__dict__["_github_rate_limit_headers"] = {("owner/repo", 87): ("7", "0")}
    _defer_on_rate_limit(d)
    _defer_on_rate_limit(d)
    _defer_on_rate_limit(d)  # past the limit of 2
    assert d.notifier.priorities[-1] == swarm_dispatch.Priority.BLOCKER
    notice = d.notifier.sent[-1]
    assert "GitHub rate limits" in notice and "after 2 automatic retries" in notice
    assert "Retry-After=7" in notice and "X-RateLimit-Remaining=0" in notice
    last = comments.posted[-1]
    assert "operator attention is required" in last.lower()
    assert SwarmDispatcher._REVIEW_DEFERRED_RE.search(last) is None
    assert SwarmDispatcher._GITHUB_DEFERRAL_RE.search(last) is None
    assert "test-token" not in notice and "test-token" not in last


def test_rate_limit_deferral_count_unreadable_stops(monkeypatch):
    # Red before this fix, inferred: the count was never read, so an
    # unreadable comment thread still deferred with a resume marker.
    comments = _Comments()
    _wire_github(monkeypatch, comments)

    async def unreadable(self, repository, number, client):
        raise httpx.ConnectError("comments unreadable")

    monkeypatch.setattr(SwarmDispatcher, "_issue_comments_scan", unreadable)
    d = _dispatcher()
    assert asyncio.run(d._prior_github_deferral_attempts(tsd._trigger(number=87))) is None
    _defer_on_rate_limit(d)
    assert d.notifier.priorities == [swarm_dispatch.Priority.BLOCKER]
    assert "GitHub rate limits" in d.notifier.sent[-1]
    assert "retry count could not be confirmed" in d.notifier.sent[-1]
    assert "Retry-After=absent" in d.notifier.sent[-1]


def test_first_panel_head_read_rate_limited_defers(monkeypatch):
    # QA: the pre-review head read (before any lens is seated) stays
    # rate-limited. Red before this fix: not observed; this pins an existing
    # path the earlier test (which limits only the aggregation read) left
    # uncovered. A resume marker is posted; no lens, routing or gate action.
    calls: list = []
    d = tsd._pr_dispatcher_with_stubs(monkeypatch, vanellus_stdout="**APPROVE**", calls=calls)

    async def head(_t):
        d.__dict__.setdefault("_rate_limited_pr_reads", set()).add(("owner/repo", 87))
        return None

    monkeypatch.setattr(d, "_pr_head_sha", head)
    comments = _Comments()
    _wire_github(monkeypatch, comments)
    asyncio.run(d._handle_pr(tsd._trigger(body="Closes #80.")))
    assert comments.posted, "a deferral notice must be posted"
    assert SwarmDispatcher._REVIEW_DEFERRED_RE.search(comments.posted[-1])
    assert SwarmDispatcher._GITHUB_DEFERRAL_RE.search(comments.posted[-1])
    assert not any(kind in {"review", "route", "gate"} for kind, _ in calls), calls
