"""The release-review gate as the dispatcher really builds it.

``build_for_dispatcher`` runs over a real ``SwarmDispatcher``. Only the HTTP
transports (httpx for GitHub and Neotoma, the App's own request function), the
subprocess start and the notifier sink are faked, so every real dependency is
exercised end to end: GitHub reads, the release record read, the panel gate
command line, the App login and token, the notifier, the red alert, the pull
request comment and the publish command line.

The modules are imported as apis.py imports them: the daemon directory and the
repository root on ``sys.path`` (conftest.py adds exactly those two), nothing
else.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import httpx
import pytest
import release_review_approval as rra
import swarm_dispatch
from github_gateway import SwarmTrigger
from swarm_dispatch import DispatchConfig, SwarmDispatcher

from lib import github_app_token

REPO = "example-org/example-product"
OPERATOR = "example-operator"
APP_SLUG = "example-swarm"
APP = f"{APP_SLUG}[bot]"
HEAD = "a" * 40
PR = 42
PR_URL = f"https://github.com/{REPO}/pull/{PR}"
REVIEW_ID = 1000
VERSION = "v1.2.3"
NEOTOMA = "https://neotoma.example.test"
APP_TOKEN = "app-installation-token-example"

_DAEMONS = Path(__file__).resolve().parent.parent


def _private_key_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


class Notifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message, **_kw):
        self.sent.append(message)


class World:
    """The fake far side of every transport, and what each one was sent."""

    def __init__(self):
        self.pr = {
            "number": PR,
            "state": "open",
            "merged": False,
            "html_url": PR_URL,
            "head": {"ref": f"release/{VERSION}", "sha": HEAD, "repo": {"full_name": REPO}},
            "base": {"ref": "main", "repo": {"full_name": REPO}},
        }
        self.review = {
            "id": REVIEW_ID,
            "user": {"login": OPERATOR, "type": "User"},
            "state": "APPROVED",
            "commit_id": HEAD,
            "body": "",
        }
        self.app_review = {
            "id": 900,
            "user": {"login": APP, "type": "Bot"},
            "state": "APPROVED",
            "commit_id": HEAD,
            "body": "panel approval",
        }
        self.records = {
            "entities": [{
                "entity_id": "ent_example",
                "snapshot": {
                    "version": VERSION,
                    "status": "pending_approval",
                    "rc_pr_url": PR_URL,
                    "rc_head_sha": HEAD,
                },
            }],
            "total": 1,
            "has_more": False,
        }
        self.http: list[httpx.Request] = []
        self.app_calls: list[tuple[str, str, str]] = []
        self.execs: list[tuple[tuple, dict]] = []
        self.panel_result = {"exit_code": 0, "head_sha": HEAD, "overall_pass": True,
                             "refusing": "", "report": ""}
        self.publish_out = b"step\npublished v1.2.3\n"

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.http.append(request)
        url = request.url
        if url.host == "neotoma.example.test":
            assert request.method == "POST" and url.path == "/entities/query"
            return httpx.Response(200, json=self.records)
        assert url.host == "api.github.com", url
        path = url.path
        page = int(url.params.get("page", "1"))
        if request.method == "POST" and path == f"/repos/{REPO}/issues/{PR}/comments":
            return httpx.Response(201, json={"id": 1})
        assert request.method == "GET", (request.method, path)
        if path == f"/repos/{REPO}/pulls/{PR}":
            return httpx.Response(200, json=self.pr)
        if path == f"/repos/{REPO}/pulls/{PR}/reviews/{REVIEW_ID}":
            return httpx.Response(200, json=self.review)
        if path == f"/repos/{REPO}/pulls/{PR}/reviews":
            return httpx.Response(200, json=[self.app_review, self.review] if page == 1 else [])
        if path in (f"/repos/{REPO}/issues/{PR}/comments", f"/repos/{REPO}/pulls/{PR}/comments"):
            return httpx.Response(200, json=[])
        if path == f"/repos/{REPO}/compare/main...{HEAD}":
            return httpx.Response(200, json={"behind_by": 0})
        return httpx.Response(404, json={"message": "Not Found"})

    def app_request(self, method: str, path: str, bearer: str) -> dict:
        self.app_calls.append((method, path, bearer))
        if (method, path) == ("GET", "/app"):
            return {"slug": APP_SLUG}
        if method == "POST" and path.endswith("/access_tokens"):
            return {"token": APP_TOKEN, "expires_at": "2099-01-01T00:00:00Z"}
        raise github_app_token.AppTokenError(f"{method} {path} -> HTTP 404")

    async def create_subprocess_exec(self, *argv, **kw):
        self.execs.append((argv, kw))
        world = self
        script = os.path.basename(str(argv[1]))

        class Proc:
            returncode = 0

            async def communicate(self):
                if script == "approve_pr_as_app.py":
                    return json.dumps(world.panel_result).encode() + b"\n", None
                return world.publish_out, None

            def kill(self):
                pass

            async def wait(self):
                return 0

        return Proc()


@pytest.fixture
def wired(monkeypatch):
    # Import discipline: nothing beyond what apis.py adds is on the path.
    assert str(_DAEMONS / "phoenicurus-release") not in sys.path
    world = World()
    monkeypatch.setenv(rra.FLAG_ENV, "1")
    monkeypatch.setenv("ATELES_REVIEWER_APP_ID", "12345")
    monkeypatch.setenv("ATELES_REVIEWER_APP_PRIVATE_KEY", _private_key_pem())
    monkeypatch.setenv("ATELES_REVIEWER_APP_INSTALLATION_ID", "777")
    monkeypatch.delenv("ATELES_REVIEWER_APP_PRIVATE_KEY_PATH", raising=False)
    monkeypatch.setattr(swarm_dispatch, "PHOENICURUS_RELEASE_REPO", REPO)
    monkeypatch.setattr(swarm_dispatch, "PHOENICURUS_RELEASE_BRANCH", "main")
    monkeypatch.setattr(swarm_dispatch, "_OPERATOR_LOGIN", OPERATOR)
    monkeypatch.setattr(swarm_dispatch, "_APP_WRITE_TOKENS", {})
    monkeypatch.setattr(swarm_dispatch, "_APP_WRITE_MINT_RETRY_AT", {})
    monkeypatch.setattr(swarm_dispatch, "_token_for_repo", lambda _repo: "")
    # conftest switches the dispatcher's App off and adds a fixture PAT; this
    # test runs the real App path, so both are restored to the real code.
    monkeypatch.setattr(swarm_dispatch, "_resolve_github_write_token",
                        swarm_dispatch._REAL_RESOLVE_GITHUB_WRITE_TOKEN)
    monkeypatch.setattr(swarm_dispatch, "_app_write_configured",
                        lambda: github_app_token.app_configured(swarm_dispatch._REVIEWER_APP_ENV_PREFIX))
    github_app_token.clear_cache()
    monkeypatch.setattr(github_app_token, "_request", world.app_request)

    real_client = httpx.AsyncClient

    class Client(real_client):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx.MockTransport(world.handle)
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", world.create_subprocess_exec)

    merged: list[str] = []

    async def fake_merge(self, trigger, source):
        merged.append(source)

    monkeypatch.setattr(SwarmDispatcher, "_approve_and_maybe_merge", fake_merge)
    dispatcher = SwarmDispatcher(
        Notifier(),
        DispatchConfig(neotoma_base_url=NEOTOMA, neotoma_token="neotoma-token-example",
                       github_token=""),
    )
    yield world, dispatcher, merged
    github_app_token.clear_cache()


def _trigger(**over):
    base = dict(  # noqa: C408 — keyword form mirrors SwarmTrigger's fields
        kind="pr_review",
        repository=REPO,
        number=PR,
        title="Release",
        body="",
        author="example-author",
        html_url=PR_URL,
        delivery_id="d-1",
        action="submitted",
        head_ref=f"release/{VERSION}",
        base_ref="main",
        head_sha=HEAD,
        review_state="approved",
        review_author=OPERATOR,
        raw={"review": {"id": REVIEW_ID}},
    )
    base.update(over)
    return SwarmTrigger(**base)


def _github_reads(world):
    return [r for r in world.http if r.url.host == "api.github.com" and r.method == "GET"]


def test_real_wiring_publishes_once_through_every_real_dependency(wired):
    world, dispatcher, merged = wired
    asyncio.run(dispatcher._handle_pr_review(_trigger()))
    assert merged == []

    # GitHub reads: every one carries the App installation token.
    reads = _github_reads(world)
    assert reads, "no GitHub read happened"
    for r in reads:
        assert r.headers.get("authorization") == f"Bearer {APP_TOKEN}", r.url

    # The release record read goes to the dispatcher's Neotoma with its token.
    neo = [r for r in world.http if r.url.host == "neotoma.example.test"]
    assert len(neo) == 2, "the record is read before the panel gate and again before the start"
    for r in neo:
        assert r.headers.get("authorization") == "Bearer neotoma-token-example"
        body = json.loads(r.content)
        assert body["entity_type"] == "release_result" and body["include_snapshots"] is True

    # App login and token come through the App's own request function.
    assert ("GET", "/app") in [(m, p) for m, p, _ in world.app_calls]
    assert any(p == "/app/installations/777/access_tokens" for _, p, _ in world.app_calls)

    # The panel gate command line, and then the publish command line.
    assert len(world.execs) == 2
    (panel_argv, panel_kw), (pub_argv, pub_kw) = world.execs
    approve = Path(panel_argv[1])
    assert approve.is_file() and approve.name == "approve_pr_as_app.py"
    assert approve.parent.name == "scripts" and approve.parent.parent.name == "execution"
    assert list(panel_argv) == [sys.executable, str(approve), "--repo", REPO, "--pr", str(PR), "--json"]
    assert "--apply" not in panel_argv
    assert panel_kw.get("env") is None, "the gate inherits the dispatcher's environment unchanged"
    publish = Path(pub_argv[1])
    assert publish.is_file() and publish.name == "publish.py"
    assert publish.parent.name == "phoenicurus-release"
    assert list(pub_argv) == [
        sys.executable, str(publish), "--version", VERSION, "--entity-id", "ent_example",
        "--from-github-review", str(REVIEW_ID), "--approved-head", HEAD,
    ]
    assert pub_kw.get("start_new_session") is True
    assert pub_kw.get("env") is None

    # Nothing refused: no notice and no comment.
    assert dispatcher.notifier.sent == []
    assert not [r for r in world.http if r.method == "POST" and r.url.host == "api.github.com"]


def test_real_wiring_refusal_posts_app_comment_and_notice(wired):
    world, dispatcher, merged = wired
    world.records["entities"][0]["snapshot"]["status"] = "published"
    asyncio.run(dispatcher._handle_pr_review(_trigger()))
    assert merged == []
    assert [a for a, _ in world.execs if a[1].endswith("publish.py")] == []
    posts = [r for r in world.http if r.method == "POST" and r.url.host == "api.github.com"]
    assert len(posts) == 1
    assert posts[0].url.path == f"/repos/{REPO}/issues/{PR}/comments"
    assert posts[0].headers.get("authorization") == f"Bearer {APP_TOKEN}"
    assert rra.NOTICE_MARKER in json.loads(posts[0].content)["body"]
    assert any("was not published" in m and "pending_approval" in m for m in dispatcher.notifier.sent)


def test_real_wiring_incomplete_record_list_refuses(wired):
    world, dispatcher, _ = wired
    world.records["has_more"] = True
    world.records["total"] = 5
    asyncio.run(dispatcher._handle_pr_review(_trigger()))
    assert [a for a, _ in world.execs if a[1].endswith("publish.py")] == []
    assert any("may be incomplete" in m for m in dispatcher.notifier.sent)


def test_real_wiring_publish_failure_raises_the_dispatcher_red_alert(wired):
    world, dispatcher, _ = wired

    async def failing_exec(*argv, **kw):
        proc = await World.create_subprocess_exec(world, *argv, **kw)
        if str(argv[1]).endswith("publish.py"):
            proc.returncode = 1
            world.publish_out = b"publish step FAILED\n"
        return proc

    asyncio.run(_run_with(dispatcher, failing_exec))
    import red_conditions

    red = [m for m in dispatcher.notifier.sent if m.startswith("🔴 Release v1.2.3")]
    assert red and red_conditions.classify(red[-1]) == ("release-failed", VERSION)


async def _run_with(dispatcher, exec_fn):
    real = asyncio.create_subprocess_exec
    asyncio.create_subprocess_exec = exec_fn
    try:
        await dispatcher._handle_pr_review(_trigger())
    finally:
        asyncio.create_subprocess_exec = real


def test_full_evaluation_leaves_root_logging_and_environment_unchanged(wired):
    world, dispatcher, _ = wired
    root = logging.getLogger()
    handlers_before = list(root.handlers)
    level_before = root.level
    env_before = dict(os.environ)
    asyncio.run(dispatcher._handle_pr_review(_trigger()))
    assert len([a for a, _ in world.execs if a[1].endswith("publish.py")]) == 1
    assert list(root.handlers) == handlers_before
    assert root.level == level_before
    assert dict(os.environ) == env_before


def test_release_records_are_read_without_importing_publish(wired):
    _, dispatcher, _ = wired
    before = set(sys.modules)
    gate = rra.build_for_dispatcher(dispatcher, swarm_dispatch)
    rows, complete = asyncio.run(gate.deps.release_results())
    assert complete is True and rows[0]["entity_id"] == "ent_example"
    added = set(sys.modules) - before
    assert not any("publish" in m or m == "probe_gate" for m in added), added


@pytest.mark.parametrize("data, complete", [
    ({"entities": [], "has_more": False}, True),
    ({"entities": [{}], "total": 1}, True),
    ({"entities": [{}], "total": 2}, False),
    ({"entities": [{}], "has_more": True}, False),
    ({"entities": [{}]}, False),
    ({"entities": "x", "has_more": False}, False),
    ({"entities": [{}], "total": True}, False),
    (None, False),
    ([], False),
])
def test_record_list_completeness_fails_closed(data, complete):
    assert rra._parse_release_results(data)[1] is complete


def test_record_list_at_the_limit_is_incomplete():
    rows = [{} for _ in range(3)]
    assert rra._parse_release_results({"entities": rows, "has_more": False}, limit=3)[1] is False


def test_neotoma_error_refuses(wired):
    world, dispatcher, _ = wired
    real = world.handle

    def handle(request):
        if request.url.host == "neotoma.example.test":
            world.http.append(request)
            return httpx.Response(503)
        return real(request)

    world.handle = handle
    asyncio.run(dispatcher._handle_pr_review(_trigger()))
    assert [a for a, _ in world.execs if a[1].endswith("publish.py")] == []
    assert any("may be incomplete" in m for m in dispatcher.notifier.sent)
