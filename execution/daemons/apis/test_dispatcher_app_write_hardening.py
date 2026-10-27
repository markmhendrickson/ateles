"""Review-round hardening for the dispatcher's App write path (PR #1459).

Companion to `test_dispatcher_app_writes.py` (same helpers and fixtures). Each
test here fails without the change it names:

  1. the in-flight clear deletes only the dispatcher's OWN marker-only comments;
  2. stale-verdict supersession touches only the dispatcher's own logins;
  3. a 401 to the cached App token drops it and retries once;
  4. a malformed repository slug never reaches the App mint;
  5. dry run mints nothing;
  6. skipped-write log lines name the real condition, and a transient notice
     cannot suppress a real mint failure.

No real network; tokens are fixtures.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import swarm_dispatch as sd
from test_dispatcher_app_writes import (
    APP_TOKEN,
    BOT,
    PAT,
    PAT_LOGIN,
    REPO,
    _bearer,
    _client_factory,
    _configure_app,
    _dispatcher,
    _unconfigure_app,
)

@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # Same setup as test_dispatcher_app_writes.py: the real credential resolver
    # (conftest gives every other test a fallback credential) and a clean cache.
    monkeypatch.setattr(sd, "_resolve_github_write_token", sd._REAL_RESOLVE_GITHUB_WRITE_TOKEN)
    sd._clear_app_write_token_cache()
    monkeypatch.setenv("ATELES_AGENT_PAT", PAT)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    yield
    sd._clear_app_write_token_cache()


@pytest.fixture
def no_pat(monkeypatch):
    monkeypatch.delenv("ATELES_AGENT_PAT", raising=False)
    monkeypatch.delenv("NEOTOMA_AGENT_PAT", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


MARKER = "<!-- apis-pipeline-inflight:2026-10-14T10:00:00+00:00:inflight -->"


def _issue_trigger(number: int = 9) -> sd.SwarmTrigger:
    return sd.SwarmTrigger(
        kind="issue_opened", repository=REPO, number=number, title="t", body="",
        author="someone", html_url=f"https://example.test/owner/repo/issues/{number}",
        delivery_id="d", action="opened",
    )


# ── 1. the in-flight clear ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_clear_inflight_deletes_only_own_marker_only_comments(monkeypatch):
    """The App credential can delete any comment, so the clear must match the
    WHOLE body (a comment that quotes a marker is someone's reasoning) and the
    author must be one of the dispatcher's own logins."""
    _configure_app(monkeypatch)
    deleted: list[str] = []
    comments = [
        {"id": 1, "body": MARKER, "user": {"login": "some-human"}},
        {"id": 2, "body": f"gate drift report quoting {MARKER} verbatim", "user": {"login": BOT}},
        {"id": 3, "body": MARKER, "user": {"login": "another-app[bot]"}},
        {"id": 4, "body": MARKER, "user": {"login": BOT}},
        {"id": 5, "body": f"  {MARKER}\n", "user": {"login": BOT}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deleted.append(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(204)
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": PAT_LOGIN})
        return httpx.Response(200, json=comments)

    monkeypatch.setattr(sd.httpx, "AsyncClient", _client_factory(handler))
    await _dispatcher()._clear_pipeline_inflight(_issue_trigger())
    assert sorted(deleted) == ["4", "5"]


# ── 2. supersession is limited to the dispatcher's own logins ─────────────────


@pytest.mark.asyncio
async def test_supersession_never_retires_another_bots_verdict_or_review(monkeypatch):
    """`_is_bot_author` admits any `[bot]` login; supersession must not."""
    _configure_app(monkeypatch)
    head_a, head_b = "a" * 40, "b" * 40
    stale = sd.compose_vanellus_fallback_comment("**REQUEST_CHANGES**", head_a)
    comments = [
        {"id": 1, "body": stale, "user": {"login": "another-app[bot]"}},
        {"id": 2, "body": stale, "user": {"login": BOT}},
    ]
    reviews = [
        {"id": 10, "state": "CHANGES_REQUESTED", "commit_id": head_a,
         "user": {"login": "another-app[bot]"}},
        {"id": 11, "state": "CHANGES_REQUESTED", "commit_id": head_a,
         "user": {"login": BOT}},
    ]
    patched: list[str] = []
    dismissed: list[str] = []
    bodies: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "PATCH":
            cid = path.rsplit("/", 1)[-1]
            patched.append(cid)
            bodies[cid] = json.loads(request.content)["body"]
            return httpx.Response(200, json={})
        if request.method == "PUT":
            dismissed.append(path.split("/")[-2])
            return httpx.Response(200, json={})
        if path == "/user":
            return httpx.Response(200, json={"login": PAT_LOGIN})
        if path.endswith("/issues/7/comments"):
            return httpx.Response(200, json=comments)
        if "/issues/comments/" in path:
            return httpx.Response(200, json={"body": bodies[path.rsplit("/", 1)[-1]]})
        if path.endswith("/pulls/7/reviews"):
            return httpx.Response(200, json=reviews)
        if "/reviews/" in path:
            return httpx.Response(200, json={"state": "DISMISSED"})
        raise AssertionError(path)

    monkeypatch.setattr(sd.httpx, "AsyncClient", _client_factory(handler))
    result = await _dispatcher()._supersede_stale_verdicts(REPO, 7, head_b)
    assert patched == ["2"] and dismissed == ["11"]
    assert result == {"comments": 1, "reviews": 1, "failures": 0}


# ── 3. a server-side-invalidated App token is dropped and retried once ────────


def _minting(monkeypatch, tokens):
    """Configure the App; each mint hands out the next token in *tokens*."""
    it = iter(tokens)

    def mint(repo):
        return sd._github_app_token.InstallationToken(
            token=next(it),
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            installation_id="1",
            permissions={},
        )

    _configure_app(monkeypatch, mint=mint)


_COMMENT_URL = f"https://api.github.com/repos/{REPO}/issues/comments/1"


@pytest.mark.asyncio
async def test_a_401_to_the_cached_app_token_drops_it_and_retries_once(monkeypatch):
    _minting(monkeypatch, ["revoked-token", "fresh-token"])
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        seen.append(auth)
        if auth == "Bearer revoked-token":
            return httpx.Response(401, json={"message": "Bad credentials"})
        return httpx.Response(204)

    monkeypatch.setattr(sd.httpx, "AsyncClient", _client_factory(handler))
    d = _dispatcher()
    await sd._ensure_app_write_token(REPO)
    async with sd.httpx.AsyncClient() as client:
        resp = await d._github_write(client, "delete", _COMMENT_URL, REPO)
    assert resp.status_code == 204
    assert seen == ["Bearer revoked-token", "Bearer fresh-token"]
    assert _bearer(d._github_headers(REPO)) == "fresh-token"  # the next call uses it


@pytest.mark.asyncio
async def test_a_second_401_is_returned_not_retried_forever(monkeypatch):
    _minting(monkeypatch, ["t1", "t2", "t3"])
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization", ""))
        return httpx.Response(401, json={})

    monkeypatch.setattr(sd.httpx, "AsyncClient", _client_factory(handler))
    d = _dispatcher()
    await sd._ensure_app_write_token(REPO)
    async with sd.httpx.AsyncClient() as client:
        resp = await d._github_write(
            client, "post", f"https://api.github.com/repos/{REPO}/issues/1/comments",
            REPO, json={"body": "x"},
        )
    assert resp.status_code == 401
    assert seen == ["Bearer t1", "Bearer t2"]


@pytest.mark.asyncio
async def test_a_401_to_the_pat_is_not_retried(monkeypatch):
    _unconfigure_app(monkeypatch)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization", ""))
        return httpx.Response(401, json={})

    monkeypatch.setattr(sd.httpx, "AsyncClient", _client_factory(handler))
    async with sd.httpx.AsyncClient() as client:
        resp = await _dispatcher()._github_write(client, "delete", _COMMENT_URL, REPO)
    assert resp.status_code == 401 and seen == [f"Bearer {PAT}"]


@pytest.mark.asyncio
async def test_a_401_is_not_retried_with_the_same_credential(monkeypatch):
    """A failed re-mint with no PAT leaves nothing new to send."""
    calls = {"n": 0}

    def mint(repo):
        calls["n"] += 1
        if calls["n"] > 1:
            raise sd._github_app_token.AppTokenError("HTTP 500")
        return sd._github_app_token.InstallationToken(
            token="only-token",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
            installation_id="1", permissions={},
        )

    _configure_app(monkeypatch, mint=mint)
    monkeypatch.delenv("ATELES_AGENT_PAT", raising=False)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization", ""))
        return httpx.Response(401, json={})

    monkeypatch.setattr(sd.httpx, "AsyncClient", _client_factory(handler))
    d = _dispatcher()
    await sd._ensure_app_write_token(REPO)
    async with sd.httpx.AsyncClient() as client:
        resp = await d._github_write(client, "delete", _COMMENT_URL, REPO)
    assert resp.status_code == 401
    assert seen == ["Bearer only-token"]


@pytest.mark.asyncio
async def test_the_in_flight_clear_recovers_from_a_revoked_app_token(monkeypatch):
    _minting(monkeypatch, ["revoked-token", "fresh-token"])
    deletes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("Authorization", "")
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": PAT_LOGIN})
        if request.method == "DELETE":
            deletes.append(auth)
            return httpx.Response(401 if auth == "Bearer revoked-token" else 204)
        return httpx.Response(200, json=[{"id": 5, "body": MARKER, "user": {"login": BOT}}])

    monkeypatch.setattr(sd.httpx, "AsyncClient", _client_factory(handler))
    await sd._ensure_app_write_token(REPO)
    await _dispatcher()._clear_pipeline_inflight(_issue_trigger())
    assert deletes == ["Bearer revoked-token", "Bearer fresh-token"]


def test_forget_token_drops_the_library_copy():
    """Without this the re-mint would hand the revoked token straight back."""
    lib = sd._github_app_token
    lib.clear_cache()
    key = ("PFX", "1")
    lib._token_cache[key] = lib.InstallationToken(
        token="x-revoked", expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        installation_id="1", permissions={},
    )
    assert lib.forget_token("other") is False and key in lib._token_cache
    assert lib.forget_token("x-revoked") is True and key not in lib._token_cache
    assert lib.forget_token("") is False
    lib.clear_cache()


# ── 4. repository-scope hardening for the App write path ──────────────────────


@pytest.mark.parametrize(
    "bad",
    ["", "owner", "a/b/c", "../x", "a/..", "./b", "a b/c", "a/b\n", "a/b?x=1", "a/b#f",
     "a/b%2e", "/a/b", "a/b/", "ownér/repo", "a/b\r\nHost: x"],
)
def test_a_malformed_repository_never_reaches_the_app_mint(monkeypatch, bad):
    calls: list = []
    _configure_app(monkeypatch, calls=calls)
    assert sd._mint_app_write_token_blocking(bad) is None
    assert calls == []
    if bad:  # the caller falls back to the PAT per the resolver, never a minted token
        assert _bearer(_dispatcher()._github_headers(bad)) == PAT
        assert calls == []


@pytest.mark.asyncio
async def test_a_malformed_repository_is_refused_on_the_async_path_too(monkeypatch):
    calls: list = []
    _configure_app(monkeypatch, calls=calls)
    assert await sd._ensure_app_write_token("owner/../../app") is None
    assert calls == []


@pytest.mark.parametrize("good", ["owner/repo", "Mark-H/my.repo_1", "a/b"])
def test_a_well_formed_repository_still_mints(monkeypatch, good):
    _configure_app(monkeypatch)
    assert sd._mint_app_write_token_blocking(good) == APP_TOKEN


# ── 5. dry run stays side-effect free ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_does_not_mint_an_app_token(monkeypatch):
    calls: list = []
    _configure_app(monkeypatch, calls=calls)
    d = _dispatcher()
    d.config.dry_run = True
    await d.handle_trigger(_issue_trigger())
    assert calls == []


@pytest.mark.asyncio
async def test_a_live_trigger_still_awaits_the_app_token_first(monkeypatch):
    calls: list = []
    _configure_app(monkeypatch, calls=calls)
    d = _dispatcher()
    d.config.dry_run = False
    reached: list[int] = []

    async def harness(trigger):
        reached.append(len(calls))  # the mint already happened by now
        raise RuntimeError("stop here")  # handle_trigger logs and returns

    monkeypatch.setattr(d, "_log_harness_event", harness)
    await d.handle_trigger(_issue_trigger())
    assert reached and reached[0] >= 1


# ── 6. log wording ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skipped_writes_name_the_real_condition_not_a_missing_token(
    monkeypatch, no_pat, caplog
):
    _unconfigure_app(monkeypatch)
    d = _dispatcher()
    trig = _issue_trigger()
    result = sd.SkillResult(skill="vanellus", ok=True, returncode=0, stdout="verdict text", stderr="")
    with caplog.at_level(logging.WARNING):
        await d._post_checkpoint_verdict_comment(trig, "body")
        await d._request_operator_reviewer(trig)
        await d._remove_operator_reviewer(trig)
        await d._post_swarm_run_comment(trig)
        assert await d._post_pipeline_bypass_comment(trig) is False
        await d._post_missing_panel_comments(trig, [])
        await d._post_missing_vanellus_comment(trig, result)
    text = "\n".join(r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert "no GitHub token" not in text
    assert text.count("no usable GitHub write credential") == 7
    assert f"{REPO}#{trig.number}" in text


@pytest.mark.asyncio
async def test_a_pending_mint_notice_cannot_hide_a_real_mint_failure(monkeypatch, caplog):
    def boom(repo):
        raise sd._github_app_token.AppTokenError("HTTP 500")

    _configure_app(monkeypatch, mint=boom)
    d = _dispatcher()
    with caplog.at_level(logging.INFO):
        assert _bearer(d._github_headers(REPO)) == PAT  # cold: schedules a background mint
        await asyncio.gather(*list(sd._APP_WRITE_BACKGROUND_TASKS))
    pending = [r for r in caplog.records if "not minted yet" in r.getMessage()]
    assert pending and all(r.levelno == logging.INFO for r in pending)
    failures = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "minting the GitHub App installation token failed" in r.getMessage()
    ]
    assert failures, "the real mint failure was suppressed by the transient notice"
