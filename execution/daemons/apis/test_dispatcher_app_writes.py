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
    # conftest gives every test a fallback credential; these tests exercise the
    # real resolver, including "no credential at all".
    monkeypatch.setattr(sd, "_resolve_github_write_token", sd._REAL_RESOLVE_GITHUB_WRITE_TOKEN)
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


# ── App-only operation: no PAT anywhere ──────────────────────────────────────


@pytest.fixture
def no_pat(monkeypatch):
    monkeypatch.delenv("ATELES_AGENT_PAT", raising=False)
    monkeypatch.delenv("NEOTOMA_AGENT_PAT", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


def _recorder(monkeypatch, responder):
    """Replace httpx with a MockTransport that records (method, path, auth)."""
    seen: list[tuple[str, str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("Authorization", "")))
        return responder(request)

    monkeypatch.setattr(sd.httpx, "AsyncClient", _client_factory(handler))
    return seen


@pytest.mark.asyncio
async def test_app_only_spec_mirror_writes_as_the_app(monkeypatch, no_pat):
    _configure_app(monkeypatch)
    state = sd.SpecState(repo=REPO, issue_number=11, title="t", sections={"problem": "p"})

    def responder(request):
        if request.method == "GET":
            return httpx.Response(200, json={"body": "human text"})
        return httpx.Response(200, json={})

    seen = _recorder(monkeypatch, responder)
    await _dispatcher()._mirror_spec_to_issue(_trigger(), state)
    assert [m for m, _, _ in seen] == ["GET", "PATCH"]
    assert {a for _, _, a in seen} == {f"Bearer {APP_TOKEN}"}


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["_surface_unverified_signed_off_gates", "_surface_unreadable_gate_verdicts"])
async def test_app_only_gate_notices_are_delivered_and_deduped_under_the_bot(
    monkeypatch, no_pat, method
):
    _configure_app(monkeypatch)
    head = "c" * 40
    comments: list[dict] = []

    def responder(request):
        if request.method == "POST":
            body = request.content.decode()
            comments.append({"body": body, "user": {"login": BOT}})
            return httpx.Response(201, json={})
        return httpx.Response(200, json=list(comments))

    seen = _recorder(monkeypatch, responder)
    d = _dispatcher()
    if method == "_surface_unverified_signed_off_gates":
        call = lambda: d._surface_unverified_signed_off_gates(_trigger(), None, {"ux"}, head)
    else:
        entry = ("ux", "accipiter", "**🤖 Accipiter — Ateles swarm, ux review**", "no verdict line")
        call = lambda: d._surface_unreadable_gate_verdicts(_trigger(), None, [entry], head)
    await call()
    posts = [x for x in seen if x[0] == "POST"]
    assert len(posts) == 1, "the notice must be delivered with no PAT configured"
    assert {a for _, _, a in seen} == {f"Bearer {APP_TOKEN}"}
    await call()
    assert len([x for x in seen if x[0] == "POST"]) == 1, "the bot's own marker must dedupe a repeat"


@pytest.mark.asyncio
async def test_no_credential_at_all_skips_visibly(monkeypatch, no_pat, caplog):
    _unconfigure_app(monkeypatch)
    state = sd.SpecState(repo=REPO, issue_number=11, title="t", sections={"problem": "p"})
    seen = _recorder(monkeypatch, lambda request: httpx.Response(200, json={}))
    with caplog.at_level(logging.WARNING):
        await _dispatcher()._mirror_spec_to_issue(_trigger(), state)
        await _dispatcher()._surface_unverified_signed_off_gates(_trigger(), None, {"ux"}, "d" * 40)
    assert seen == []
    text = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "spec mirror skipped" in text and "no usable GitHub write credential" in text
    assert "surface unverified signed off gates skipped" in text


@pytest.mark.asyncio
async def test_empty_gate_inputs_stay_silent_no_ops(monkeypatch, no_pat, caplog):
    _unconfigure_app(monkeypatch)
    seen = _recorder(monkeypatch, lambda request: httpx.Response(200, json={}))
    with caplog.at_level(logging.WARNING):
        await _dispatcher()._surface_unverified_signed_off_gates(_trigger(), None, set(), "e" * 40)
    assert seen == [] and caplog.records == []


# ── cold start: the App is awaited before any write; the PAT only after failure ──


@pytest.mark.asyncio
async def test_cold_start_checkpoint_comment_goes_out_as_the_app(monkeypatch):
    """A caller that never awaits readiness itself (cold cache, PAT also set)
    must still write as the App: the write boundary awaits the mint."""
    calls: list = []
    _configure_app(monkeypatch, calls=calls)
    seen = _recorder(monkeypatch, lambda request: httpx.Response(201, json={}))
    assert sd._app_write_token_cached(REPO) is None
    await _dispatcher()._post_checkpoint_verdict_comment(_trigger(), "verdict text")
    assert [(m, a) for m, _, a in seen] == [("POST", f"Bearer {APP_TOKEN}")]
    assert PAT not in "".join(a for _, _, a in seen)


@pytest.mark.asyncio
async def test_pat_is_used_only_after_the_mint_has_failed(monkeypatch, caplog):
    def _boom(repo):
        raise sd._github_app_token.AppTokenError("HTTP 500")

    _configure_app(monkeypatch, mint=_boom)
    seen = _recorder(monkeypatch, lambda request: httpx.Response(201, json={}))
    with caplog.at_level(logging.WARNING):
        await _dispatcher()._post_checkpoint_verdict_comment(_trigger(), "verdict text")
    assert [a for _, _, a in seen] == [f"Bearer {PAT}"]
    assert any("falling back to the agent PAT" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_entry_points_await_readiness_before_working(monkeypatch):
    calls: list = []
    _configure_app(monkeypatch, calls=calls)
    seen = _recorder(monkeypatch, lambda request: httpx.Response(200, json=[]))
    await _dispatcher().resume_interrupted_pipelines([REPO])
    assert [c[0] for c in calls] == [REPO]
    assert seen and {a for _, _, a in seen} == {f"Bearer {APP_TOKEN}"}


@pytest.mark.asyncio
async def test_startup_warm_up_mints_every_repository_and_is_bounded(monkeypatch):
    calls: list = []
    _configure_app(monkeypatch, calls=calls)
    d = _dispatcher()
    assert await d.warm_github_app_token([REPO, "owner/other"], timeout_s=5) is True
    assert sorted(c[0] for c in calls) == ["owner/other", REPO]

    sd._clear_app_write_token_cache()
    import time

    def _slow(repo):
        time.sleep(0.5)
        raise sd._github_app_token.AppTokenError("slow")

    _configure_app(monkeypatch, mint=_slow)
    assert await d.warm_github_app_token([REPO], timeout_s=0.05) is False
    await asyncio.sleep(0.6)  # let the abandoned worker thread finish


@pytest.mark.asyncio
async def test_warm_up_is_a_no_op_without_the_app(monkeypatch):
    _unconfigure_app(monkeypatch)
    assert await _dispatcher().warm_github_app_token([REPO]) is False


# ── the guard and the headers resolve the SAME credential ───────────────────


@pytest.mark.asyncio
async def test_instance_token_only_is_skipped_with_a_warning_never_sent_bare(
    monkeypatch, no_pat, caplog
):
    _unconfigure_app(monkeypatch)
    seen = _recorder(monkeypatch, lambda request: httpx.Response(201, json={}))
    d = _dispatcher()
    d.config.github_token = "instance-token-fixture"
    state = sd.SpecState(repo=REPO, issue_number=11, title="t", sections={"problem": "p"})
    with caplog.at_level(logging.WARNING):
        await d._post_checkpoint_verdict_comment(_trigger(), "verdict text")
        await d._mirror_spec_to_issue(_trigger(), state)
    assert seen == [], "a write was sent although the headers carry no credential"
    text = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "verdict comment skipped" in text and "spec mirror skipped" in text
    assert "instance-token-fixture" not in text


@pytest.mark.asyncio
@pytest.mark.parametrize("app", ["healthy", "fails", "unconfigured"])
@pytest.mark.parametrize("pat", [True, False])
async def test_guard_and_headers_agree_in_every_combination(monkeypatch, app, pat):
    if not pat:
        monkeypatch.delenv("ATELES_AGENT_PAT", raising=False)
    if app == "unconfigured":
        _unconfigure_app(monkeypatch)
    elif app == "fails":
        def _boom(repo):
            raise sd._github_app_token.AppTokenError("HTTP 500")
        _configure_app(monkeypatch, mint=_boom)
    else:
        _configure_app(monkeypatch)
    d = _dispatcher()
    d.config.github_token = "instance-token-fixture"
    guard = await d._require_github_write_credential(REPO, "x")
    sent = "Authorization" in d._github_headers(REPO)
    assert guard == sent, "the guard passed for a credential the headers do not send"


# ── every GitHub write goes through the credential guard ─────────────────────


@pytest.mark.asyncio
async def test_marker_with_no_credential_sends_nothing_and_warns(monkeypatch, no_pat, caplog):
    _unconfigure_app(monkeypatch)
    seen = _recorder(monkeypatch, lambda request: httpx.Response(201, json={}))
    with caplog.at_level(logging.WARNING):
        await _dispatcher()._mark_pipeline_inflight(_trigger())
    assert seen == [], "the in-flight marker was sent without a usable credential"
    text = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "in-flight pipeline marker skipped" in text
    assert "no usable GitHub write credential" in text


@pytest.mark.asyncio
async def test_other_marker_and_merge_writes_also_skip_without_a_credential(
    monkeypatch, no_pat, caplog
):
    _unconfigure_app(monkeypatch)
    seen = _recorder(monkeypatch, lambda request: httpx.Response(200, json=[]))
    d = _dispatcher()
    with caplog.at_level(logging.WARNING):
        await d._clear_pipeline_inflight(_trigger())
        await d._record_fix_round(_trigger(), 1)
        merged, detail = await d._merge_pr(REPO, 5)
        assert await d._claim_escalation(_trigger(), "kind") is True  # fail-open
        assert await d._clear_closed_issue_markers([REPO]) == 0
        assert await d._supersede_stale_verdicts(REPO, 5, "a" * 40) == {
            "comments": 0, "reviews": 0, "failures": 0
        }
    assert seen == []
    assert merged is False and "merge held" in detail


def test_every_github_write_in_the_dispatcher_goes_through_the_guard():
    """Structural guard against a new write that bypasses the credential guard:
    every POST/PATCH/PUT/DELETE issued on an httpx client in swarm_dispatch.py
    sits in a function that calls the guard. The exceptions are the App
    token mint itself (authenticated with a JWT) and the two Neotoma helpers."""
    import ast
    from pathlib import Path

    tree = ast.parse((Path(sd.__file__)).read_text())
    allowed = {"_mint_reviewer_app_installation_token", "_store_entities", "_neotoma_post"}
    unguarded: list[str] = []
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        src = ast.get_source_segment(Path(sd.__file__).read_text(), fn) or ""
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"post", "patch", "put", "delete", "request"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "client"
            ):
                if fn.name not in allowed and not (
                    "_require_github_write_credential" in src or "_github_write_token" in src
                ):
                    unguarded.append(f"{fn.name}:{node.lineno}")
    assert unguarded == [], f"GitHub writes without the credential guard: {unguarded}"
