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


def _limited(retry_after="7"):
    return _Resp(
        403,
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
