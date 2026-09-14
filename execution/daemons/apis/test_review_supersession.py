"""Head-scoped swarm review verdicts and stale-verdict supersession (#507)."""

from __future__ import annotations

import asyncio

import pytest

import github_gateway as gg
import swarm_dispatch as sd


HEAD_A = "a" * 40
HEAD_B = "b" * 40


def _comment(body: str, *, cid: int = 1, login: str = "github-actions[bot]") -> dict:
    return {
        "id": cid,
        "body": body,
        "created_at": f"2026-09-14T00:00:{cid:02d}Z",
        "user": {"login": login},
    }


def _trigger(**over) -> gg.SwarmTrigger:
    values = dict(
        kind="pr_synchronize",
        repository="o/r",
        number=7,
        title="t",
        body="",
        author="a",
        html_url="https://example.test/o/r/pull/7",
        delivery_id="d",
        action="synchronize",
        head_sha=HEAD_B,
    )
    values.update(over)
    return gg.SwarmTrigger(**values)


def test_aggregation_marker_round_trip_and_head_selection():
    current = sd.compose_vanellus_fallback_comment("**APPROVE**", HEAD_A, "content")
    parsed = sd.parse_aggregation_marker(current)
    assert parsed == {"commit": HEAD_A, "block_kind": "content", "superseded_by": None}

    stale_newer = _comment(
        sd.compose_vanellus_fallback_comment("**REQUEST_CHANGES**", HEAD_B), cid=9
    )
    current_older = _comment(current, cid=2)
    assert (
        sd.latest_aggregation_comment([current_older, stale_newer], head_sha=HEAD_A)[
            "id"
        ]
        == 2
    )
    assert sd.latest_aggregation_comment([current_older], head_sha=HEAD_B) is None


def test_legacy_and_prose_sha_are_never_current():
    legacy = _comment("<!-- vanellus-aggregation -->\nReviewed commit: " + HEAD_A)
    assert sd.parse_aggregation_marker(legacy["body"])["commit"] is None
    assert sd.latest_aggregation_comment([legacy], head_sha=HEAD_A) is None


def test_superseded_banner_is_idempotent_and_preserves_original():
    original = sd.compose_vanellus_fallback_comment(
        "**REQUEST_CHANGES**\nkeep me", HEAD_A
    )
    once = sd.compose_superseded_verdict(original, HEAD_B)
    twice = sd.compose_superseded_verdict(once, HEAD_B)
    assert once == twice
    assert once.count("Superseded by commit") == 1
    assert f"<!-- vanellus-aggregation-superseded by={HEAD_B} -->" in once
    assert original in once
    assert sd.parse_aggregation_marker(once)["superseded_by"] == HEAD_B


def test_gateway_populates_head_sha_and_missing_is_safe():
    base = {
        "action": "synchronize",
        "repository": {"full_name": "o/r"},
        "pull_request": {
            "number": 7,
            "title": "t",
            "body": "",
            "user": {"login": "a"},
            "html_url": "u",
            "labels": [],
            "head": {"ref": "x", "sha": HEAD_B},
            "base": {"ref": "main"},
        },
    }
    assert gg.parse_github_event("pull_request", base).head_sha == HEAD_B
    base["pull_request"]["head"].pop("sha")
    assert gg.parse_github_event("pull_request", base).head_sha == ""

    base["action"] = "reopened"
    base["pull_request"]["head"]["sha"] = HEAD_B
    assert gg.parse_github_event("pull_request", base).head_sha == HEAD_B

    review_payload = {
        **base,
        "action": "submitted",
        "review": {"state": "approved", "user": {"login": "person"}},
    }
    assert (
        gg.parse_github_event("pull_request_review", review_payload).head_sha == HEAD_B
    )


def test_clean_blocked_routes_process_notice_not_unparseable(monkeypatch):
    d = sd.SwarmDispatcher(
        notifier=type("N", (), {"send": lambda self, *a, **k: calls.append(a[0])})()
    )
    calls: list[str] = []
    claimed: list[str] = []

    async def claim(trigger, kind):
        claimed.append(kind)
        return True

    monkeypatch.setattr(d, "_claim_escalation", claim)
    asyncio.run(
        d._route_blocking_findings(
            _trigger(), 1, [("arch", "**BLOCKED** no issue")], "blocked"
        )
    )
    assert claimed == ["process-blocked"]
    assert calls and "process, not content" in calls[0]


@pytest.mark.asyncio
async def test_formal_review_is_pinned_to_head(monkeypatch):
    d = sd.SwarmDispatcher(notifier=type("N", (), {"send": lambda *a, **k: None})())
    payloads = []

    class Response:
        status_code = 200
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {"id": 99}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            payloads.append(json)
            return Response()

    monkeypatch.setattr(sd, "_token_for_repo", lambda repo: "token")
    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda **kw: Client())
    await d._emit_formal_review(_trigger(), "comment", "**COMMENT**")
    assert payloads[0]["commit_id"] == HEAD_B


@pytest.mark.asyncio
async def test_formal_review_fetches_head_when_trigger_omits_it(monkeypatch):
    d = sd.SwarmDispatcher(notifier=type("N", (), {"send": lambda *a, **k: None})())
    payloads = []
    fetched = []

    class Response:
        status_code = 200
        text = ""

        def raise_for_status(self):
            pass

        def json(self):
            return {"id": 99}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            payloads.append(json)
            return Response()

    async def head(trigger):
        fetched.append(trigger.number)
        return HEAD_A

    monkeypatch.setattr(sd, "_token_for_repo", lambda repo: "token")
    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda **kw: Client())
    monkeypatch.setattr(d, "_pr_head_sha", head)
    await d._emit_formal_review(_trigger(head_sha=""), "comment", "**COMMENT**")
    assert fetched == [7]
    assert payloads[0]["commit_id"] == HEAD_A


class _Response:
    def __init__(self, payload=None, status=200):
        self.payload = payload
        self.status_code = status
        self.text = "failure" if status >= 400 else ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self.payload


@pytest.mark.asyncio
async def test_supersession_patches_only_bot_verdicts_and_dismisses_stale_block(
    monkeypatch,
):
    comments = [
        _comment(
            sd.compose_vanellus_fallback_comment("**REQUEST_CHANGES**", HEAD_A), cid=1
        ),
        _comment(
            sd.compose_vanellus_fallback_comment("**REQUEST_CHANGES**", HEAD_A),
            cid=2,
            login="person",
        ),
        _comment(sd.compose_vanellus_fallback_comment("**APPROVE**", HEAD_B), cid=3),
        _comment("review:qa\nlegacy lens", cid=4),
    ]
    reviews = [
        {
            "id": 10,
            "state": "CHANGES_REQUESTED",
            "commit_id": HEAD_A,
            "user": {"login": "github-actions[bot]"},
        },
        {
            "id": 11,
            "state": "APPROVED",
            "commit_id": HEAD_A,
            "user": {"login": "github-actions[bot]"},
        },
        {
            "id": 12,
            "state": "CHANGES_REQUESTED",
            "commit_id": HEAD_A,
            "user": {"login": "person"},
        },
        {
            "id": 13,
            "state": "DISMISSED",
            "commit_id": HEAD_A,
            "user": {"login": "github-actions[bot]"},
        },
    ]
    patched, dismissed = [], []
    patched_bodies = {}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            if url.endswith("/comments"):
                return _Response(comments)
            if url.endswith("/reviews"):
                return _Response(reviews)
            if url.endswith("/reviews/10"):
                return _Response({"state": "DISMISSED"})
            if "/issues/comments/" in url:
                return _Response({"body": patched_bodies[int(url.rsplit("/", 1)[-1])]})
            raise AssertionError(url)

        async def patch(self, url, json=None, headers=None):
            patched.append((url, json["body"]))
            patched_bodies[int(url.rsplit("/", 1)[-1])] = json["body"]
            return _Response({})

        async def post(self, url, json=None, headers=None):
            if url.endswith("/reviews/10/dismissals"):
                dismissed.append((url, json["message"]))
                return _Response({"state": "DISMISSED"})
            raise AssertionError(url)

    d = sd.SwarmDispatcher(notifier=type("N", (), {"send": lambda *a, **k: None})())
    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda **kw: Client())
    result = await d._supersede_stale_verdicts("o/r", 7, HEAD_B)
    assert result == {"comments": 2, "reviews": 1, "failures": 0}
    assert {url.rsplit("/", 1)[-1] for url, _ in patched} == {"1", "4"}
    assert len(dismissed) == 1 and "Superseded by bbbbbbb" in dismissed[0][1]
    assert "earlier head and no longer applies" in dismissed[0][1]
    assert "fresh panel review will run" in dismissed[0][1]


@pytest.mark.asyncio
async def test_supersession_continues_after_patch_and_dismiss_failures(monkeypatch):
    comments = [
        _comment(
            sd.compose_vanellus_fallback_comment("**REQUEST_CHANGES**", HEAD_A), cid=1
        ),
        _comment(sd.compose_vanellus_fallback_comment("**COMMENT**", HEAD_A), cid=2),
    ]
    reviews = [
        {
            "id": 10,
            "state": "CHANGES_REQUESTED",
            "commit_id": HEAD_A,
            "user": {"login": "github-actions[bot]"},
        },
    ]
    patched = []
    patched_bodies = {}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            if url.endswith("/comments"):
                return _Response(comments)
            if url.endswith("/reviews"):
                return _Response(reviews)
            if "/issues/comments/" in url:
                return _Response({"body": patched_bodies[int(url.rsplit("/", 1)[-1])]})
            return _Response(reviews)

        async def patch(self, url, json=None, headers=None):
            patched.append(url)
            if not url.endswith("/1"):
                patched_bodies[int(url.rsplit("/", 1)[-1])] = json["body"]
            return _Response({}, 404 if url.endswith("/1") else 200)

        async def post(self, url, json=None, headers=None):
            if url.endswith("/dismissals"):
                return _Response({}, 422)
            return _Response({})  # first-failure durability marker

    d = sd.SwarmDispatcher(notifier=type("N", (), {"send": lambda *a, **k: None})())
    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda **kw: Client())
    result = await d._supersede_stale_verdicts("o/r", 7, HEAD_B)
    assert result["comments"] == 1
    assert result["failures"] == 2
    assert len(patched) == 2


@pytest.mark.asyncio
async def test_periodic_sweep_uses_each_open_pr_current_head(monkeypatch):
    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            return _Response([{"number": 7, "head": {"sha": HEAD_B}}])

    calls = []
    d = sd.SwarmDispatcher(notifier=type("N", (), {"send": lambda *a, **k: None})())

    async def supersede(repo, number, head):
        calls.append((repo, number, head))
        return {"comments": 0, "reviews": 0, "failures": 0}

    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda **kw: Client())
    monkeypatch.setattr(d, "_supersede_stale_verdicts", supersede)
    result = await d.supersede_stale_review_verdicts(["o/r"])
    assert calls == [("o/r", 7, HEAD_B)]
    assert result["prs"] == 1


@pytest.mark.asyncio
async def test_merge_gate_ignores_stale_block_and_requires_current_clear(monkeypatch):
    comments = [
        _comment(
            sd.compose_vanellus_fallback_comment("**REQUEST_CHANGES**", HEAD_A), cid=1
        ),
        _comment(sd.compose_vanellus_fallback_comment("**APPROVE**", HEAD_B), cid=2),
    ]

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            return _Response(comments)

    d = sd.SwarmDispatcher(notifier=type("N", (), {"send": lambda *a, **k: None})())
    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda **kw: Client())
    assert await d._pr_review_is_clear("o/r", 7, HEAD_B) is True
    assert await d._pr_review_is_clear("o/r", 7, "c" * 40) is False


@pytest.mark.asyncio
async def test_495_dismissal_does_not_open_merge_window_before_current_clear(
    monkeypatch,
):
    """Retiring A cannot clear B until B has its own head-pinned approval."""
    comments = [
        _comment(
            sd.compose_vanellus_fallback_comment("**REQUEST_CHANGES**", HEAD_A),
            cid=1,
        )
    ]

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            return _Response(comments)

    d = sd.SwarmDispatcher(notifier=type("N", (), {"send": lambda *a, **k: None})())
    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda **kw: Client())

    assert await d._pr_review_is_clear("o/r", 7, HEAD_B) is False
    comments.append(
        _comment(sd.compose_vanellus_fallback_comment("**APPROVE**", HEAD_B), cid=2)
    )
    assert await d._pr_review_is_clear("o/r", 7, HEAD_B) is True


@pytest.mark.asyncio
async def test_repeated_supersession_failure_escalates_once(monkeypatch):
    failure_marker = f"<!-- apis-supersede-failed commit={HEAD_B} -->"
    comments = [
        _comment(failure_marker, cid=1),
        _comment(
            sd.compose_vanellus_fallback_comment("**REQUEST_CHANGES**", HEAD_A), cid=2
        ),
    ]
    notices, claimed = [], []

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, params=None, headers=None):
            return _Response(comments if url.endswith("/comments") else [])

        async def patch(self, url, json=None, headers=None):
            return _Response({}, 404)

    class Notifier:
        def send(self, message, **kwargs):
            notices.append(message)

    d = sd.SwarmDispatcher(notifier=Notifier())

    async def claim(trigger, kind):
        claimed.append(kind)
        return len(claimed) == 1

    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda **kw: Client())
    monkeypatch.setattr(d, "_claim_escalation", claim)
    await d._supersede_stale_verdicts("o/r", 7, HEAD_B)
    assert claimed == ["supersede-failed"]
    assert len(notices) == 1
