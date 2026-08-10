"""
Recovery for reviews that die without leaving a trace.

## The failure these cover

Two independent gaps let ateles#408 sit for two days — mergeable, every check
green, zero formal reviews, nothing reporting a problem:

1. **No fail-open on a missing verdict.** The retry block's comment promised
   "one sharper retry before failing open", but nothing failed open: a
   still-missing verdict fell through to `if verdict == "blocked"`, which
   `None` fails, so the PR proceeded with gate inheritance unresolved. On
   2026-08-09 Lanius omitted its verdict, the retry ran while Neotoma writes
   were failing (neotoma#2141), and no verdict came back.

2. **No sweep for a review that left no marker.** `resume_deferred_reviews`
   only rescues PRs that got far enough to post a `review-deferred-until:`
   marker. A review dying earlier leaves nothing to find, and with auto-merge
   keyed on a formal approval the PR is simply never looked at again.

The second is the one the operator asked for: the swarm must recover on its
own rather than waiting for someone to notice.

Run: pytest execution/daemons/apis/test_stalled_review_recovery.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import swarm_dispatch as sd


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, msg: str, priority=None) -> None:  # noqa: ANN001
        self.sent.append(msg)


def _dispatcher() -> sd.SwarmDispatcher:
    return sd.SwarmDispatcher(notifier=_Notifier())


def _pr(number: int, *, age_minutes: int = 120, draft: bool = False) -> dict:
    when = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return {
        "number": number,
        "title": f"PR {number}",
        "body": "",
        "draft": draft,
        "updated_at": when.isoformat().replace("+00:00", "Z"),
        "user": {"login": "someone"},
        "html_url": f"https://github.com/o/r/pull/{number}",
        "head": {"ref": "feature"},
        "base": {"ref": "main"},
    }


# ---------------------------------------------------------------------------
# The sweep re-dispatches a stalled PR
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stalled_pr_is_redispatched(monkeypatch):
    """The ateles#408 shape: old, open, no formal review of any kind."""
    d = _dispatcher()
    handled: list[int] = []

    async def fake_candidates(repo, now, stall_after):  # noqa: ANN001
        return [_pr(408)]

    async def fake_handle(trigger):  # noqa: ANN001
        handled.append(trigger.number)

    monkeypatch.setattr(d, "_prs_with_stalled_review", fake_candidates)
    monkeypatch.setattr(d, "_handle_pr", fake_handle)

    summary = await d.resume_stalled_reviews(["o/r"])

    assert handled == [408], "a review that left no trace must be re-dispatched"
    assert summary["resumed"] == 1


@pytest.mark.asyncio
async def test_retries_are_bounded_then_escalated(monkeypatch):
    """
    An agent that cannot review a PR will not start being able to on the
    twentieth attempt. Silent infinite retry is how a real defect stays
    invisible — so stop and tell the operator.
    """
    d = _dispatcher()
    monkeypatch.setenv("APIS_REVIEW_STALL_MAX_RETRIES", "2")
    calls: list[int] = []

    async def fake_candidates(repo, now, stall_after):  # noqa: ANN001
        return [_pr(408)]

    async def fake_handle(trigger):  # noqa: ANN001
        calls.append(trigger.number)

    monkeypatch.setattr(d, "_prs_with_stalled_review", fake_candidates)
    monkeypatch.setattr(d, "_handle_pr", fake_handle)

    for _ in range(5):
        await d.resume_stalled_reviews(["o/r"])

    assert len(calls) == 2, f"expected 2 bounded retries, got {len(calls)}"
    assert any("no formal review after" in m for m in d.notifier.sent), (
        "exhausting the retry budget must reach the operator, not go quiet"
    )


@pytest.mark.asyncio
async def test_escalates_only_once(monkeypatch):
    """A sweep every 10 minutes must not page the operator every 10 minutes."""
    d = _dispatcher()
    monkeypatch.setenv("APIS_REVIEW_STALL_MAX_RETRIES", "1")

    async def fake_candidates(repo, now, stall_after):  # noqa: ANN001
        return [_pr(408)]

    async def fake_handle(trigger):  # noqa: ANN001
        return None

    monkeypatch.setattr(d, "_prs_with_stalled_review", fake_candidates)
    monkeypatch.setattr(d, "_handle_pr", fake_handle)

    for _ in range(6):
        await d.resume_stalled_reviews(["o/r"])

    assert len(d.notifier.sent) == 1, "repeated escalation is alert noise"


@pytest.mark.asyncio
async def test_sweep_is_fail_open(monkeypatch):
    """A broken sweep must never stop the daemon loop."""
    d = _dispatcher()

    async def boom(repo, now, stall_after):  # noqa: ANN001
        raise RuntimeError("github unreachable")

    monkeypatch.setattr(d, "_prs_with_stalled_review", boom)

    summary = await d.resume_stalled_reviews(["o/r"])  # must not raise
    assert summary["scanned"] == 0


@pytest.mark.asyncio
async def test_handle_pr_failure_does_not_abort_the_sweep(monkeypatch):
    """One bad PR must not prevent the others from being rescued."""
    d = _dispatcher()
    seen: list[int] = []

    async def fake_candidates(repo, now, stall_after):  # noqa: ANN001
        return [_pr(1), _pr(2)]

    async def flaky(trigger):  # noqa: ANN001
        seen.append(trigger.number)
        if trigger.number == 1:
            raise RuntimeError("panel exploded")

    monkeypatch.setattr(d, "_prs_with_stalled_review", fake_candidates)
    monkeypatch.setattr(d, "_handle_pr", flaky)

    summary = await d.resume_stalled_reviews(["o/r"])

    assert seen == [1, 2], "a failure on the first PR must not skip the second"
    assert summary["failed"] == 1 and summary["resumed"] == 1
