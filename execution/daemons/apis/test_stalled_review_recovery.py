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
from skill_runner import SkillResult


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, msg: str, priority=None, handler=None) -> None:  # noqa: ANN001
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


def _async_return(value):
    async def _coro():
        return value

    return _coro()


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


# ---------------------------------------------------------------------------
# Effect-verified: recovery must actually produce a formal review, not just
# correct bookkeeping (pm/pavo review guidance on PR #415, round 1)
# ---------------------------------------------------------------------------
#
# Every test above mocks BOTH `_prs_with_stalled_review` and `_handle_pr`, so
# none of them assert the reported effect — an ateles#408-shaped PR actually
# getting a formal review after recovery. They only prove the sweep's own
# bookkeeping (retry counts, escalation, fail-open) is correct; `_handle_pr`
# itself is a black box in every case above. This test drives `_handle_pr` for
# real from the sweep, mocking only the true external boundary (`run_skill`,
# the GitHub review-post call, and the other network-touching internals
# `test_swarm_dispatch.py`'s `_pr_dispatcher_with_stubs` already stubs for the
# same reason), and asserts a formal review actually lands.


def _stalled_recovery_dispatcher(monkeypatch, *, vanellus_stdout):
    """A dispatcher whose `_handle_pr` runs for real end-to-end, with only
    `run_skill` and other GitHub/network internals stubbed — mirrors
    `test_swarm_dispatch._pr_dispatcher_with_stubs`, reused here so the
    stalled-review sweep exercises the *actual* `_handle_pr`, not a mock of
    it."""
    posted_reviews: list[tuple[str, str]] = []

    async def fake_run_skill(skill, prompt, **kwargs):  # noqa: ANN001
        # Lanius clears gate inheritance (the #408 shape had nothing blocking
        # it); panelists return trivial reviews; Vanellus emits the configured
        # verdict.
        if skill == "lanius":
            return SkillResult(skill, True, 0, "GATE_INHERITANCE: clear", "")
        if skill == "vanellus":
            return SkillResult(skill, True, 0, vanellus_stdout, "")
        return SkillResult(skill, True, 0, "**COMMENT**\nlgtm", "")

    async def fake_changed_files(self, trigger):  # noqa: ANN001
        return ["src/x.ts"]

    async def fake_emit_review(self, trigger, verdict, body):  # noqa: ANN001
        # The GitHub-facing boundary this test exists to prove gets reached.
        posted_reviews.append((f"{trigger.repository}#{trigger.number}", verdict))
        return "rev-1"

    async def fake_persist(self, *a, **k):  # noqa: ANN001
        return None

    async def fake_route(self, trigger, parent, reviews, verdict):  # noqa: ANN001
        return None

    async def fake_gate(self, trigger, parent, panel):  # noqa: ANN001
        return None

    async def fake_post_missing_vanellus(self, trigger, result):  # noqa: ANN001
        return None

    monkeypatch.setattr(sd, "run_skill", fake_run_skill)
    monkeypatch.setattr(sd.SwarmDispatcher, "_changed_files", fake_changed_files)
    monkeypatch.setattr(sd.SwarmDispatcher, "_emit_formal_review", fake_emit_review)
    monkeypatch.setattr(sd.SwarmDispatcher, "_persist_panel_reviews", fake_persist)
    monkeypatch.setattr(
        sd.SwarmDispatcher, "_post_missing_panel_comments", fake_persist
    )
    monkeypatch.setattr(
        sd.SwarmDispatcher, "_route_blocking_findings", fake_route
    )
    monkeypatch.setattr(sd.SwarmDispatcher, "_gate_merge_readiness", fake_gate)
    monkeypatch.setattr(
        sd.SwarmDispatcher,
        "_post_missing_vanellus_comment",
        fake_post_missing_vanellus,
    )
    monkeypatch.setattr(
        sd.SwarmDispatcher,
        "_preregistered_expectations",
        lambda self, repo, parent: _async_return({}),
    )
    return _dispatcher(), posted_reviews


@pytest.mark.asyncio
async def test_stalled_recovery_actually_posts_a_formal_review(monkeypatch):
    """The ateles#408 effect: a PR recovered by the stalled-review sweep must
    end up with a real formal review on GitHub, not just correct retry/
    escalation bookkeeping. Drives `_handle_pr` for real — only `run_skill`
    and other GitHub/network internals are stubbed."""
    d, posted_reviews = _stalled_recovery_dispatcher(
        monkeypatch, vanellus_stdout="**APPROVE**\nlgtm"
    )

    async def fake_candidates(repo, now, stall_after):  # noqa: ANN001
        # ateles#408 had a parent issue (`Closes #80.`) — give the fixture the
        # same shape so `_handle_pr` takes the normal parent-linked path
        # instead of the (unrelated) no-parent pipeline-bypass path.
        pr = _pr(408)
        pr["body"] = "Closes #80."
        return [pr]

    monkeypatch.setattr(d, "_prs_with_stalled_review", fake_candidates)

    summary = await d.resume_stalled_reviews(["o/r"])

    assert summary["resumed"] == 1
    assert posted_reviews == [("o/r#408", "approve")], (
        "recovery must end with a real formal review posted for the "
        "previously-silent PR, not just bookkeeping — this is the effect "
        "ateles#408 actually needed"
    )


# ---------------------------------------------------------------------------
# `_prs_with_stalled_review` must not double-handle a PR the deferred-review
# sweep already owns (pm/pavo review guidance on PR #415, round 1, finding 2)
# ---------------------------------------------------------------------------


def _comments_response(bodies: list[str]):
    class _Resp:
        def __init__(self, bodies):
            self._bodies = bodies

        def raise_for_status(self):
            return None

        def json(self):
            return [{"body": b} for b in self._bodies]

    return _Resp(bodies)


@pytest.mark.asyncio
async def test_stalled_sweep_excludes_pr_with_live_deferral_marker(monkeypatch):
    """A PR with an unmatured `review-deferred-until:` marker and zero formal
    reviews looks identical to a true stall (old, open, no review) — but it is
    the deferred sweep's PR, not this one's. Sweeping it here too would
    re-dispatch it twice for the same underlying cause."""
    d = _dispatcher()
    stalled_pr = _pr(408)
    future_iso = (
        datetime.now(timezone.utc) + timedelta(hours=2)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    class _FakeClient:
        async def get(self, url, params=None, headers=None):  # noqa: ANN001
            if url.endswith("/pulls"):
                class _ListResp:
                    def raise_for_status(self):
                        return None

                    def json(self):
                        return [stalled_pr]

                return _ListResp()
            if "/reviews" in url:
                class _ReviewsResp:
                    def raise_for_status(self):
                        return None

                    def json(self):
                        return []  # zero formal reviews — otherwise a stall

                return _ReviewsResp()
            if "/comments" in url:
                return _comments_response(
                    [f"<!-- review-deferred-until:{future_iso} -->"]
                )
            raise AssertionError(f"unexpected URL {url}")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        sd.httpx, "AsyncClient", lambda timeout=30: _FakeClient()
    )

    candidates = await d._prs_with_stalled_review(
        "o/r", datetime.now(timezone.utc), stall_after_seconds=60
    )

    assert candidates == [], (
        "a PR with a live deferral marker belongs to the deferred sweep — "
        "the stalled-review sweep must not also re-dispatch it (ateles#414)"
    )


@pytest.mark.asyncio
async def test_stalled_sweep_still_includes_pr_with_no_marker(monkeypatch):
    """Sanity check on the fix above: a genuinely marker-less stall (the
    #408 shape) must still be picked up — the deferral-marker exclusion must
    not become an accidental exclude-everything."""
    d = _dispatcher()
    stalled_pr = _pr(408)

    class _FakeClient:
        async def get(self, url, params=None, headers=None):  # noqa: ANN001
            if url.endswith("/pulls"):
                class _ListResp:
                    def raise_for_status(self):
                        return None

                    def json(self):
                        return [stalled_pr]

                return _ListResp()
            if "/reviews" in url:
                class _ReviewsResp:
                    def raise_for_status(self):
                        return None

                    def json(self):
                        return []

                return _ReviewsResp()
            if "/comments" in url:
                return _comments_response([])  # no marker at all
            raise AssertionError(f"unexpected URL {url}")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        sd.httpx, "AsyncClient", lambda timeout=30: _FakeClient()
    )

    candidates = await d._prs_with_stalled_review(
        "o/r", datetime.now(timezone.utc), stall_after_seconds=60
    )

    assert [c["number"] for c in candidates] == [408]
