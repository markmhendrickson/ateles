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

ateles#446: the clear was correct (#437) but unreachable in production —
awaited AFTER `resume_interrupted_pipelines` inside `resume_sweep()`, and the
daemon restarts faster than resume returns. The reachability tests below assert
the clear runs as a gather sibling while resume hangs.

Run: pytest execution/daemons/apis/test_stale_inflight_markers.py -v
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from unittest.mock import AsyncMock

import pytest

import apis
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


# ── ateles#446: reachability under hung resume (gather siblings) ─────────────


def _resume_sweep_body_source() -> str:
    """Extract the nested `resume_sweep` body from apis.main source text."""
    src = inspect.getsource(apis.main)
    start = src.find("async def resume_sweep()")
    assert start != -1, "resume_sweep missing from apis.main"
    # Next sibling coroutine after resume_sweep inside main.
    end = src.find("async def clear_closed_issue_markers_sweep()", start + 1)
    if end == -1:
        end = src.find("async def workflow_drift_check()", start + 1)
    assert end != -1, "could not bound resume_sweep body"
    return src[start:end]


async def _mirror_clear_closed_issue_markers_sweep(clear_fn, log, daemon_name="apis"):
    """Local reconstruction of the production coroutine shape (nested, not importable)."""
    try:
        cleared = await clear_fn()
        log.info(f"[{daemon_name}] closed-issue marker sweep: cleared={cleared}")
    except Exception as exc:
        log.error(
            f"[{daemon_name}] closed-issue marker sweep failed: {exc} "
            "— will retry next boot; housekeeping independent of resume",
            exc_info=True,
        )


@pytest.mark.asyncio
async def test_clear_closed_markers_runs_while_resume_hangs():
    """P0 effect: marker clear completes while resume is still blocked forever.

    Pre-fix sequencing (clear awaited after hanging resume) fails this test;
    post-fix gather-sibling scheduling passes it (foundation invariant 4).
    """
    clear_done = asyncio.Event()
    resume_returned = False
    clear_calls = 0

    async def resume_like():
        nonlocal resume_returned
        await asyncio.Event().wait()  # hang forever
        resume_returned = True

    async def clear_fn():
        nonlocal clear_calls
        clear_calls += 1
        clear_done.set()
        return 0

    class _Log:
        def info(self, *a, **k):
            return None

        def error(self, *a, **k):
            return None

    async def clear_like():
        await _mirror_clear_closed_issue_markers_sweep(clear_fn, _Log())

    async def boot_gather():
        await asyncio.gather(resume_like(), clear_like())

    gather_task = asyncio.create_task(boot_gather())
    try:
        await asyncio.wait_for(clear_done.wait(), timeout=1.0)
        assert clear_calls == 1
        assert resume_returned is False
    finally:
        gather_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await gather_task


def test_clear_closed_markers_not_sequenced_after_resume_in_resume_sweep():
    """resume_sweep body must not call _clear_closed_issue_markers (ateles#446)."""
    body = _resume_sweep_body_source()
    assert "_clear_closed_issue_markers" not in body


def test_startup_gather_includes_marker_sweep_coroutine():
    """clear_closed_issue_markers_sweep is a gather sibling, not nested under resume."""
    src = inspect.getsource(apis.main)
    assert "async def clear_closed_issue_markers_sweep()" in src
    gather_idx = src.rfind("await asyncio.gather(")
    assert gather_idx != -1
    gather_block = src[gather_idx:]
    assert "resume_sweep()," in gather_block
    assert "clear_closed_issue_markers_sweep()," in gather_block
    resume_body = _resume_sweep_body_source()
    assert "clear_closed_issue_markers_sweep" not in resume_body


@pytest.mark.asyncio
async def test_closed_issue_marker_sweep_logs_completion_when_cleared_zero(caplog):
    """UX P0: cleared=0 still emits the stable completion phrase."""
    clear_fn = AsyncMock(return_value=0)
    with caplog.at_level(logging.INFO):
        await _mirror_clear_closed_issue_markers_sweep(
            clear_fn, logging.getLogger("apis")
        )
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    matching = [
        m for m in info_msgs if "closed-issue marker sweep" in m and "cleared=0" in m
    ]
    assert len(matching) == 1


@pytest.mark.asyncio
async def test_clear_closed_markers_sweep_survives_internal_exception(caplog):
    """Sweep-level exception is fail-open; sibling gather continues."""

    async def clear_fn():
        raise RuntimeError("boom")

    sibling_done = asyncio.Event()

    async def sibling():
        sibling_done.set()

    with caplog.at_level(logging.ERROR):
        await asyncio.gather(
            _mirror_clear_closed_issue_markers_sweep(
                clear_fn, logging.getLogger("apis")
            ),
            sibling(),
        )
    assert sibling_done.is_set()
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("closed-issue marker sweep failed" in r.getMessage() for r in errors)
    assert any(r.exc_info for r in errors)


@pytest.mark.asyncio
async def test_marker_sweep_and_resume_start_concurrently():
    """Both siblings enter within one gather tick — no implicit ordering."""
    resume_entered = asyncio.Event()
    clear_entered = asyncio.Event()

    async def resume_like():
        resume_entered.set()
        await asyncio.sleep(0)

    async def clear_like():
        clear_entered.set()
        await asyncio.sleep(0)

    await asyncio.gather(resume_like(), clear_like())
    assert resume_entered.is_set() and clear_entered.is_set()


@pytest.mark.asyncio
async def test_marker_sweep_runs_once_per_gather_not_per_resume_issue():
    """One boot gather → one clear call, even if resume processes N issues."""
    clear_calls = 0
    n_issues = 5

    async def resume_like():
        for _ in range(n_issues):
            await asyncio.sleep(0)

    async def clear_fn():
        nonlocal clear_calls
        clear_calls += 1
        return 0

    class _Log:
        def info(self, *a, **k):
            return None

        def error(self, *a, **k):
            return None

    async def clear_like():
        await _mirror_clear_closed_issue_markers_sweep(clear_fn, _Log())

    await asyncio.gather(resume_like(), clear_like())
    assert clear_calls == 1


@pytest.mark.asyncio
async def test_clear_closed_markers_sweep_logs_success_count(caplog):
    """cleared > 0 still uses the stable completion phrase with the count."""
    clear_fn = AsyncMock(return_value=3)
    with caplog.at_level(logging.INFO):
        await _mirror_clear_closed_issue_markers_sweep(
            clear_fn, logging.getLogger("apis")
        )
    info_msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        "closed-issue marker sweep" in m and "cleared=3" in m for m in info_msgs
    )
