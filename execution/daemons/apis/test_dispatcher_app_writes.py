"""The dispatcher writes to GitHub as the swarm App installation (ateles#1381).

The dispatcher's REST writes used to authenticate with the fine-grained
`ATELES_AGENT_PAT`, which GitHub refuses ("Resource not accessible by personal
access token") on a repository another account owns. They now carry the App's
installation token, with the PAT as the fallback only when the App is
unconfigured or a token cannot be minted.

No real network: the library's mint is stubbed and httpx is replaced by a
`MockTransport`. Tokens in these tests are fixtures, never real credentials.

Run: pytest execution/daemons/apis/test_dispatcher_app_writes.py -v
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import lens_authors
import swarm_dispatch as sd

APP_TOKEN = "app-installation-token-fixture"
PAT = "pat-fixture-token"
REPO = "owner/repo"
BOT = "ateles-agents[bot]"
PAT_LOGIN = "ateles-agent"


class _Notifier:
    def send(self, *a, **k):
        pass

    def clear_dedupe(self, key):
        pass


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    sd._clear_app_write_token_cache()
    monkeypatch.setenv("ATELES_AGENT_PAT", PAT)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    yield
    sd._clear_app_write_token_cache()


def _configure_app(monkeypatch, *, mint=None, calls=None):
    """Configure the App and stub the library's mint."""
    monkeypatch.setattr(sd._github_app_token, "app_configured", lambda prefix=None: True)

    def _info(repo=None, *, prefix=None):
        if calls is not None:
            calls.append((repo, threading.current_thread()))
        if mint is not None:
            return mint(repo)
        return sd._github_app_token.InstallationToken(
            token=APP_TOKEN,
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            installation_id="1",
            permissions={},
        )

    monkeypatch.setattr(sd._github_app_token, "installation_token_info", _info)
    monkeypatch.setattr(sd._github_app_token, "bot_login", lambda prefix=None: BOT)


def _unconfigure_app(monkeypatch):
    monkeypatch.setattr(sd._github_app_token, "app_configured", lambda prefix=None: False)


def _dispatcher() -> sd.SwarmDispatcher:
    return sd.SwarmDispatcher(notifier=_Notifier())


def _bearer(headers: dict) -> str:
    return headers.get("Authorization", "").removeprefix("Bearer ")


# ── the App is preferred; the PAT is the fallback ────────────────────────────


def test_app_token_is_preferred_when_configured(monkeypatch):
    _configure_app(monkeypatch)
    assert _bearer(_dispatcher()._github_headers(REPO)) == APP_TOKEN


def test_pat_is_used_when_the_app_is_unconfigured(monkeypatch):
    _unconfigure_app(monkeypatch)
    assert _bearer(_dispatcher()._github_headers(REPO)) == PAT


def test_pat_is_used_with_a_warning_when_minting_fails(monkeypatch, caplog):
    def _boom(repo):
        raise sd._github_app_token.AppTokenError("POST /app/installations/1 -> HTTP 401 Bad credentials")

    _configure_app(monkeypatch, mint=_boom)
    with caplog.at_level(logging.WARNING):
        headers = _dispatcher()._github_headers(REPO)
    assert _bearer(headers) == PAT
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("falling back to the agent PAT" in r.getMessage() for r in warnings)
    joined = "\n".join(r.getMessage() for r in caplog.records)
    assert PAT not in joined and APP_TOKEN not in joined


def test_a_failed_mint_is_not_retried_on_every_call(monkeypatch):
    calls: list = []

    def _boom(repo):
        raise sd._github_app_token.AppTokenError("HTTP 502")

    _configure_app(monkeypatch, mint=_boom, calls=calls)
    d = _dispatcher()
    for _ in range(5):
        assert _bearer(d._github_headers(REPO)) == PAT
    assert len(calls) == 1


def test_no_repo_keeps_the_instance_token(monkeypatch):
    _configure_app(monkeypatch)
    d = _dispatcher()
    d.config.github_token = "instance-token-fixture"
    assert _bearer(d._github_headers("")) == "instance-token-fixture"


# ── sync callers on and off the event loop ───────────────────────────────────


def test_a_sync_caller_without_a_running_loop_mints_inline(monkeypatch):
    calls: list = []
    _configure_app(monkeypatch, calls=calls)
    d = _dispatcher()
    assert _bearer(d._github_headers(REPO)) == APP_TOKEN
    assert _bearer(d._github_headers(REPO)) == APP_TOKEN
    assert len(calls) == 1, "the second call must be served from the cache"


@pytest.mark.asyncio
async def test_a_sync_caller_on_the_loop_never_mints_on_the_loop(monkeypatch):
    """Cold cache + running loop: the PAT this once, a background mint, then the App."""
    calls: list = []
    _configure_app(monkeypatch, calls=calls)
    d = _dispatcher()
    main_thread = threading.current_thread()

    cold = d._github_headers(REPO)
    assert _bearer(cold) == PAT, "a cold sync call must not block the loop to mint"
    assert calls == [], "the mint must not have run inline on the event loop"

    for _ in range(50):  # let the scheduled background mint finish
        await asyncio.sleep(0.01)
        if calls:
            break
    assert calls and calls[0][1] is not main_thread, "the mint must run in a worker thread"
    await asyncio.sleep(0.05)
    assert _bearer(d._github_headers(REPO)) == APP_TOKEN


@pytest.mark.asyncio
async def test_ensure_mints_in_a_thread_and_sync_callers_then_use_the_app(monkeypatch):
    calls: list = []
    _configure_app(monkeypatch, calls=calls)
    token = await sd._ensure_app_write_token(REPO)
    assert token == APP_TOKEN
    assert calls[0][1] is not threading.current_thread()
    assert _bearer(_dispatcher()._github_headers(REPO)) == APP_TOKEN
    assert await sd._ensure_app_write_token(REPO) == APP_TOKEN
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_token_near_expiry_is_refreshed(monkeypatch):
    minted = iter(["first-fixture", "second-fixture"])

    def _mint(repo):
        return sd._github_app_token.InstallationToken(
            token=next(minted),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            installation_id="1",
            permissions={},
        )

    _configure_app(monkeypatch, mint=_mint)
    assert await sd._ensure_app_write_token(REPO) == "first-fixture"
    # 5 minutes left is inside the refresh window, so the next ensure re-mints.
    assert await sd._ensure_app_write_token(REPO) == "second-fixture"


@pytest.mark.asyncio
async def test_an_expired_token_is_never_served(monkeypatch):
    _configure_app(monkeypatch)
    sd._APP_WRITE_TOKENS[REPO] = (
        "stale-fixture",
        datetime.now(timezone.utc) + timedelta(seconds=30),
    )
    assert sd._app_write_token_cached(REPO) is None


# ── the dispatcher recognises its own markers under the bot login ────────────


def _client_factory(handler):
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient
    return lambda timeout=30: real(transport=transport, timeout=timeout)


@pytest.mark.asyncio
async def test_own_logins_include_the_bot_and_the_pat_account(monkeypatch):
    _configure_app(monkeypatch)
    seen_auth: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_auth.append(request.headers.get("Authorization", ""))
        assert request.url.path == "/user"
        return httpx.Response(200, json={"login": "Ateles-Agent"})

    monkeypatch.setattr(sd.httpx, "AsyncClient", _client_factory(handler))
    async with sd.httpx.AsyncClient() as client:
        logins = await _dispatcher()._own_github_logins(client, REPO)
    assert logins == {BOT.casefold(), PAT_LOGIN}
    # GET /user is asked with the PAT, never with the installation token.
    assert seen_auth == [f"Bearer {PAT}"]


@pytest.mark.asyncio
async def test_own_logins_survive_an_unreadable_pat_account(monkeypatch):
    _configure_app(monkeypatch)
    monkeypatch.setattr(
        sd.httpx,
        "AsyncClient",
        _client_factory(lambda request: httpx.Response(401, json={})),
    )
    async with sd.httpx.AsyncClient() as client:
        assert await _dispatcher()._own_github_logins(client, REPO) == {BOT.casefold()}


@pytest.mark.asyncio
async def test_without_the_app_own_login_is_the_pat_account(monkeypatch):
    _unconfigure_app(monkeypatch)
    monkeypatch.setattr(
        sd.httpx,
        "AsyncClient",
        _client_factory(lambda request: httpx.Response(200, json={"login": PAT_LOGIN})),
    )
    async with sd.httpx.AsyncClient() as client:
        assert await _dispatcher()._own_github_logins(client, REPO) == {PAT_LOGIN}


@pytest.mark.asyncio
async def test_a_notice_marker_written_by_the_bot_suppresses_a_repeat(monkeypatch):
    """The once-per-head notice dedupe reads the dispatcher's OWN comments; the
    bot's marker must count, or every run reposts it."""
    _configure_app(monkeypatch)
    head = "b" * 40
    marker = sd.gate_cleared_unverified_marker("ux", head)
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": PAT_LOGIN})
        if request.method == "POST":
            posted.append({"url": str(request.url)})
            return httpx.Response(201, json={})
        return httpx.Response(
            200, json=[{"body": marker, "user": {"login": BOT}}]
        )

    monkeypatch.setattr(sd.httpx, "AsyncClient", _client_factory(handler))
    trigger = sd.SwarmTrigger(
        kind="pr_opened", repository=REPO, number=7, title="t", body="",
        author="someone", html_url="https://github.com/owner/repo/pull/7",
        delivery_id="d", action="opened", head_sha=head,
    )
    await _dispatcher()._surface_unverified_signed_off_gates(trigger, None, {"ux"}, head)
    assert posted == []


@pytest.mark.asyncio
async def test_the_marker_sweeps_do_not_filter_by_author(monkeypatch):
    """A bot-authored in-flight marker is cleared exactly like a PAT-authored one."""
    _configure_app(monkeypatch)
    deleted: list[str] = []
    marker = "<!-- apis-pipeline-inflight:2026-10-14T10:00:00+00:00:inflight -->"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(204)
        return httpx.Response(200, json=[{"id": 5, "body": marker, "user": {"login": BOT}}])

    monkeypatch.setattr(sd.httpx, "AsyncClient", _client_factory(handler))
    trigger = sd.SwarmTrigger(
        kind="issue_opened", repository=REPO, number=9, title="t", body="",
        author="someone", html_url="https://github.com/owner/repo/issues/9",
        delivery_id="d", action="opened",
    )
    await _dispatcher()._clear_pipeline_inflight(trigger)
    assert deleted == [f"/repos/{REPO}/issues/comments/5"]


def test_the_bot_login_is_a_swarm_identity(monkeypatch):
    """Comment supersession (`_is_bot_author`) and the lens-author set both admit it."""
    assert sd._is_bot_author(BOT)
    assert sd._is_bot_author(PAT_LOGIN)
    _configure_app(monkeypatch)
    monkeypatch.setattr(lens_authors._github_app_token, "app_configured", lambda prefix=None: True)
    monkeypatch.setattr(lens_authors._github_app_token, "bot_login", lambda prefix=None: BOT)
    monkeypatch.setattr(lens_authors, "_login_for_token", lambda token: PAT_LOGIN)
    # conftest stubs the App lookup out for every test; this one needs the real one
    monkeypatch.setattr(
        lens_authors,
        "_app_bot_logins",
        lambda: ({BOT.casefold()}, False),
    )
    resolution = lens_authors.resolve()
    assert BOT.casefold() in resolution.authors
    assert PAT_LOGIN in resolution.authors


# ── a 403 explains itself ────────────────────────────────────────────────────

FORBIDDEN_BODY = '{"message":"Resource not accessible by integration","documentation_url":"https://docs.github.com"}'


def _trigger():
    return sd.SwarmTrigger(
        kind="issue_opened", repository=REPO, number=11, title="t", body="",
        author="someone", html_url="https://github.com/owner/repo/issues/11",
        delivery_id="d", action="opened",
    )


@pytest.mark.asyncio
async def test_marker_post_403_logs_the_response_body(monkeypatch, caplog):
    _configure_app(monkeypatch)
    used: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        used.append(request.headers.get("Authorization", ""))
        return httpx.Response(403, text=FORBIDDEN_BODY)

    monkeypatch.setattr(sd.httpx, "AsyncClient", _client_factory(handler))
    with caplog.at_level(logging.WARNING):
        await _dispatcher()._mark_pipeline_inflight(_trigger())
    assert used == [f"Bearer {APP_TOKEN}"], "the marker POST must carry the App token"
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "Resource not accessible by integration" in text
    assert "answered 403" in text
    assert APP_TOKEN not in text and PAT not in text


@pytest.mark.asyncio
async def test_a_logged_403_body_is_truncated_to_300_chars(monkeypatch, caplog):
    _unconfigure_app(monkeypatch)
    long_body = "x" * 1000
    monkeypatch.setattr(
        sd.httpx,
        "AsyncClient",
        _client_factory(lambda request: httpx.Response(403, text=long_body)),
    )
    with caplog.at_level(logging.WARNING):
        await _dispatcher()._mark_pipeline_inflight(_trigger())
    answered = [r.getMessage() for r in caplog.records if "answered 403" in r.getMessage()]
    assert answered and "x" * 300 in answered[0] and "x" * 301 not in answered[0]


@pytest.mark.asyncio
async def test_spec_mirror_403_logs_the_response_body(monkeypatch, caplog):
    _configure_app(monkeypatch)
    state = sd.SpecState(repo=REPO, issue_number=11, title="t", sections={"problem": "a problem"})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"body": "human text"})
        assert request.headers.get("Authorization") == f"Bearer {APP_TOKEN}"
        return httpx.Response(403, text=FORBIDDEN_BODY)

    monkeypatch.setattr(sd.httpx, "AsyncClient", _client_factory(handler))
    with caplog.at_level(logging.WARNING):
        await _dispatcher()._mirror_spec_to_issue(_trigger(), state)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "spec mirror PATCH answered 403" in text
    assert "Resource not accessible by integration" in text


@pytest.mark.asyncio
async def test_keep_warm_mints_for_each_repository_then_waits(monkeypatch):
    calls: list = []
    _configure_app(monkeypatch, calls=calls)
    task = asyncio.create_task(
        _dispatcher().keep_github_app_token_warm([REPO, "owner/other"], interval_s=3600)
    )
    for _ in range(100):
        await asyncio.sleep(0.01)
        if len(calls) >= 2:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sorted(c[0] for c in calls) == ["owner/other", REPO]
    assert _bearer(_dispatcher()._github_headers(REPO)) == APP_TOKEN
