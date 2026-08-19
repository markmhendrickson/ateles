"""
Stale `apis-pipeline-inflight` markers on closed issues.

## The failure these cover

`_mark_pipeline_inflight` posts a hidden HTML-comment marker before the first
agent spawn so a daemon restart mid-run is resumable, and
`_clear_pipeline_inflight` deletes it in a `finally`. The clear is deliberately
best-effort — a failure only logs a warning — so a GitHub/Neotoma blip or a
daemon kill orphans the marker.

Nothing reclaimed those orphans. `resume_interrupted_pipelines` scans
`state=open` only (correctly — a closed issue's pipeline is moot), so a marker
on an issue that later closed was never looked at again. The marker is
invisible to the API, but GitHub renders a comment whose body is ONLY an HTML
comment as the "No description provided." placeholder, so each orphan shows up
as a blank swarm comment on the thread forever. As of 2026-08-19 six such
comments were live across ateles (#404, #412, #418, #419) and neotoma (#2073,
#2138).

The subtle half is what must NOT be deleted: Lanius and Vanellus both post real
comments that QUOTE a marker when reporting gate drift. Those carry
operator-visible reasoning, so the sweep matches the marker as the ENTIRE body
(`fullmatch`), never as a substring.

Run: pytest execution/daemons/apis/test_stale_inflight_markers.py -v
"""

from __future__ import annotations

import pytest

import swarm_dispatch as sd


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, message, priority=None, handler=None):
        self.sent.append(message)


def _dispatcher() -> sd.SwarmDispatcher:
    return sd.SwarmDispatcher(notifier=_Notifier())


MARKER_QUEUED = "<!-- apis-pipeline-inflight:2026-08-19T07:09:11.182674+00:00:queued -->"
MARKER_INFLIGHT = "<!-- apis-pipeline-inflight:2026-08-19T07:28:40.902497+00:00:inflight -->"
# Written by a daemon build predating the stage suffix (ateles#323).
MARKER_LEGACY = "<!-- apis-pipeline-inflight:2026-08-09T16:03:12.799298+00:00 -->"
# Real Lanius comment from neotoma#2054 — quotes a marker, must survive.
QUOTING_COMMENT = (
    "**Lanius — gate drift**\n\nNot writing gate_status right now since Apis's "
    "in-flight run (`apis-pipeline-inflight:2026-07-30T13:00:48Z`) is already "
    "reconciling triage — a concurrent write would race it."
)


def _fake_client(monkeypatch, *, issues, comments_by_number, deleted):
    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _FakeClient:
        async def get(self, url, params=None, headers=None):
            if url.endswith("/issues"):
                assert params.get("state") == "closed", (
                    "the stale-marker sweep must scan CLOSED issues; open ones "
                    "may hold a marker for a live run"
                )
                return _Resp(issues)
            if "/comments" in url:
                number = int(url.rstrip("/comments").rstrip("/").split("/")[-1])
                return _Resp(comments_by_number.get(number, []))
            raise AssertionError(f"unexpected URL {url}")

        async def delete(self, url, headers=None):
            deleted.append(int(url.split("/")[-1]))
            return _Resp({})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda timeout=30: _FakeClient())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "marker", [MARKER_QUEUED, MARKER_INFLIGHT, MARKER_LEGACY]
)
async def test_marker_only_comment_on_closed_issue_is_deleted(monkeypatch, marker):
    """Every marker shape the daemon has ever written gets reaped."""
    deleted: list[int] = []
    _fake_client(
        monkeypatch,
        issues=[{"number": 404}],
        comments_by_number={404: [{"id": 1, "body": marker}]},
        deleted=deleted,
    )
    cleared = await _dispatcher()._clear_closed_issue_markers(["owner/repo"])
    assert deleted == [1]
    assert cleared == 1


@pytest.mark.asyncio
async def test_comment_quoting_a_marker_is_preserved(monkeypatch):
    """A real agent comment that merely mentions a marker is audit trail."""
    deleted: list[int] = []
    _fake_client(
        monkeypatch,
        issues=[{"number": 2054}],
        comments_by_number={2054: [{"id": 9, "body": QUOTING_COMMENT}]},
        deleted=deleted,
    )
    cleared = await _dispatcher()._clear_closed_issue_markers(["owner/repo"])
    assert deleted == [], "deleted a real comment that only quoted a marker"
    assert cleared == 0


@pytest.mark.asyncio
async def test_pull_requests_are_skipped(monkeypatch):
    """/issues returns PRs too; a PR's pipeline is the PR handler's."""
    deleted: list[int] = []
    _fake_client(
        monkeypatch,
        issues=[{"number": 500, "pull_request": {"url": "..."}}],
        comments_by_number={500: [{"id": 3, "body": MARKER_INFLIGHT}]},
        deleted=deleted,
    )
    cleared = await _dispatcher()._clear_closed_issue_markers(["owner/repo"])
    assert deleted == []
    assert cleared == 0


@pytest.mark.asyncio
async def test_sweep_is_fail_open_and_continues_to_next_repo(monkeypatch):
    """A repo that errors must not stop the sweep or crash the boot path."""
    deleted: list[int] = []

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class _FakeClient:
        async def get(self, url, params=None, headers=None):
            if "bad/repo" in url:
                raise RuntimeError("GitHub 503")
            if url.endswith("/issues"):
                return _Resp([{"number": 418}])
            if "/comments" in url:
                return _Resp([{"id": 7, "body": MARKER_INFLIGHT}])
            raise AssertionError(f"unexpected URL {url}")

        async def delete(self, url, headers=None):
            deleted.append(int(url.split("/")[-1]))
            return _Resp({})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda timeout=30: _FakeClient())

    cleared = await _dispatcher()._clear_closed_issue_markers(
        ["bad/repo", "good/repo"]
    )
    assert deleted == [7], "a failing repo must not block the healthy one"
    assert cleared == 1
