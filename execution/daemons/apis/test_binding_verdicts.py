"""A verdict that cannot be recorded must BLOCK, not degrade its channel (ateles#682).

## The bug

`_emit_formal_review` posts the panel's aggregated verdict to GitHub's reviews
API — the only channel branch protection and the merge gates can enforce. When
that POST failed it caught the exception, logged a WARNING, and returned None.
The caller discarded the return value. The verdict then existed only as prose in
an issue comment, which enforces nothing, and the merge proceeded.

Measured on the neotoma v0.22.2 release:

    #2278 — Pavo posted REQUEST_CHANGES at 16:15:35Z, merged 16:15:42Z (7s).
    #2284 — Vanellus posted "Do not merge" at 16:18:19Z, merged 16:18:23Z (4s).
    Both PRs: GET /pulls/<n>/reviews -> []

The root cause is quoted verbatim in two Vanellus reviews on neotoma#2267:
`error connecting to api.github.com`. A transient blip degraded the verdict into
an unenforceable channel and the pipeline carried on as though review had passed.

## What these tests pin

1. A transport failure is RETRIED before it is allowed to fail at all.
2. When retries are exhausted, the PR is marked as carrying an unrecorded
   verdict and BOTH merge gates refuse to signal merge-ready.
3. The fail-closed path does not fire when recording succeeds — this is a gate,
   not a brake.

Run: pytest execution/daemons/apis/test_binding_verdicts.py -v
"""

from __future__ import annotations

import httpx
import pytest

import swarm_dispatch as sd


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


class _Resp:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"id": 999}
        self.content = b"{}"
        self.text = ""

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)


def _trigger(repo: str = "o/r", number: int = 1) -> sd.SwarmTrigger:
    return sd.SwarmTrigger(
        kind="pr_opened",
        repository=repo,
        number=number,
        title="A pull request",
        body="Closes #80.",
        author="someone",
        html_url=f"https://github.com/{repo}/pull/{number}",
        delivery_id="binding-verdicts-test",
        action="opened",
    )


def _comment(cid: int, created: str, body: str) -> dict:
    return {"id": cid, "created_at": created, "body": body}


UNRECORDED = sd._UNRECORDED_VERDICT_MARKER
RECORDED = sd._VERDICT_RECORDED_MARKER


def _dispatcher(monkeypatch) -> sd.SwarmDispatcher:
    sent: list = []
    d = sd.SwarmDispatcher(
        notifier=type("N", (), {"send": lambda *a, **k: sent.append(a)})()
    )
    monkeypatch.setattr(d, "_github_headers", lambda repo="": {})
    monkeypatch.setattr(sd, "_token_for_repo", lambda repo: "tok")
    d._sent = sent  # type: ignore[attr-defined]
    return d


# ---------------------------------------------------------------------------
# The pure marker selector
# ---------------------------------------------------------------------------


def test_unrecorded_marker_blocks():
    assert sd.verdict_recording_is_blocked(
        [_comment(1, "2026-09-01T10:00:00Z", f"{UNRECORDED}\nfailed")]
    )


def test_no_markers_does_not_block():
    """This gate fires only on evidence of a failed recording, never by default."""
    assert not sd.verdict_recording_is_blocked(
        [_comment(1, "2026-09-01T10:00:00Z", "an ordinary review comment")]
    )


def test_newest_marker_wins_retirement_clears_the_block():
    """A retirement newer than the block clears it.

    Input is oldest-first, mirroring the real API, so selection must be by
    ``created_at`` and never by position (ateles#430).
    """
    assert not sd.verdict_recording_is_blocked([
        _comment(1, "2026-09-01T10:00:00Z", f"{UNRECORDED}\nfailed"),
        _comment(2, "2026-09-01T11:00:00Z", f"{RECORDED}\nlanded"),
    ])


def test_a_stale_retirement_cannot_clear_a_current_block():
    """The dangerous direction: an old success must not unblock a new failure."""
    assert sd.verdict_recording_is_blocked([
        _comment(1, "2026-09-01T10:00:00Z", f"{RECORDED}\nlanded"),
        _comment(2, "2026-09-01T11:00:00Z", f"{UNRECORDED}\nfailed again"),
    ])


# ---------------------------------------------------------------------------
# Retry — a transient blip must not become a non-binding verdict
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_connection_error_is_retried_and_succeeds(monkeypatch):
    """The api.github.com blip from neotoma#2267, recovered on attempt 2."""
    d = _dispatcher(monkeypatch)
    attempts = {"n": 0}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):  # noqa: ANN001
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise httpx.ConnectError("error connecting to api.github.com")
            return _Resp()

        async def get(self, url, params=None, headers=None):  # noqa: ANN001
            return _Resp(payload=[])

    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda **kw: _Client())
    monkeypatch.setattr(sd.asyncio, "sleep", _no_sleep)

    review_id = await d._emit_formal_review(
        _trigger(), "request_changes", "**REQUEST_CHANGES**\n[BLOCKING] x: y"
    )

    assert attempts["n"] == 2, "a transport blip must be retried, not accepted"
    assert review_id == "999"


async def _no_sleep(_seconds):
    return None


@pytest.mark.asyncio
async def test_non_transport_error_is_not_retried_but_still_fails_closed(
    monkeypatch,
):
    """A programming error is a bug, not a blip — fail closed on attempt 1.

    Retrying a misconfigured header three times just delays the block. The catch
    is narrowed to `httpx.TransportError` so only genuine transport failures are
    retried; everything else still lands on the fail-closed path immediately.
    """
    d = _dispatcher(monkeypatch)
    attempts = {"n": 0}
    posted: list[str] = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):  # noqa: ANN001
            if url.endswith("/reviews"):
                attempts["n"] += 1
                raise ValueError("misconfigured header")
            posted.append((json or {}).get("body", ""))
            return _Resp()

        async def get(self, url, params=None, headers=None):  # noqa: ANN001
            return _Resp(payload=[])

    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda **kw: _Client())
    monkeypatch.setattr(sd.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(d, "_claim_escalation", _true)

    review_id = await d._emit_formal_review(_trigger(), "approve", "**APPROVE**")

    assert attempts["n"] == 1, "a non-transport error must not be retried"
    assert review_id is None
    assert any(UNRECORDED in b for b in posted), (
        "a bug must still fail closed — an unrecorded verdict is unenforceable "
        "whatever the cause"
    )


# ---------------------------------------------------------------------------
# THE test — fail closed when the verdict cannot be recorded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unreachable_reviews_api_marks_the_pr_and_blocks_merge(monkeypatch):
    """THE test (ateles#682). Reviews API unreachable -> PR must NOT be mergeable.

    Reproduces neotoma#2278/#2284: the panel reaches REQUEST_CHANGES, the reviews
    API is unreachable for every attempt, and the old code returned None quietly
    while the merge gate went on to report the PR merge-ready.

    Asserts the inversion: the failure is recorded on the PR, and
    `_gate_merge_readiness` files NO merge checkpoint.
    """
    d = _dispatcher(monkeypatch)
    posted: list[str] = []

    class _DeadClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):  # noqa: ANN001
            # The reviews API is unreachable; the marker comment still posts.
            if "/pulls/" in url and url.endswith("/reviews"):
                raise httpx.ConnectError("error connecting to api.github.com")
            posted.append((json or {}).get("body", ""))
            return _Resp()

        async def get(self, url, params=None, headers=None):  # noqa: ANN001
            return _Resp(payload=[])

    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda **kw: _DeadClient())
    monkeypatch.setattr(sd.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(d, "_claim_escalation", _true)

    t = _trigger()
    review_id = await d._emit_formal_review(
        t, "request_changes", "**REQUEST_CHANGES**\n[BLOCKING] deploy: parity"
    )

    assert review_id is None
    assert any(UNRECORDED in b for b in posted), (
        "an unrecordable verdict must be marked on the PR — otherwise nothing "
        "downstream can tell review from silence"
    )

    # Now the gate: with the marker standing, merge-ready must NOT be signalled.
    checkpoints: list = []
    monkeypatch.setattr(
        d, "_store_merge_checkpoint",
        lambda *a, **k: checkpoints.append(a) or _async_none(),
    )
    monkeypatch.setattr(d, "_required_ci_state", lambda *a, **k: _async("green"))
    monkeypatch.setattr(d, "_verdict_recording_blocked", _true)

    await d._gate_merge_readiness(t, None, panel=[])

    assert checkpoints == [], (
        "the merge checkpoint was filed on a verdict GitHub never recorded — "
        "this is the 4-second merge on neotoma#2284"
    )


@pytest.mark.asyncio
async def test_pr_review_is_clear_is_false_while_the_verdict_is_unrecorded(
    monkeypatch,
):
    """The CI-green gate must also refuse, even on a clean-reading aggregation.

    The aggregation comment can read a perfect APPROVE while GitHub holds no
    review at all — that is exactly what made this bug invisible.
    """
    d = _dispatcher(monkeypatch)
    comments = [
        _comment(
            1, "2026-09-01T10:00:00Z",
            "<!-- vanellus-aggregation -->\n**APPROVE**\nVerdict: APPROVE",
        ),
        _comment(2, "2026-09-01T10:05:00Z", f"{UNRECORDED}\nunreachable"),
    ]

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda **kw: _C())
    monkeypatch.setattr(
        d, "_all_issue_comments", lambda *a, **k: _async(comments)
    )

    assert await d._pr_review_is_clear("o/r", 1) is False, (
        "a clear aggregation over an unrecorded verdict must not clear the gate"
    )


@pytest.mark.asyncio
async def test_422_downgrade_to_comment_also_blocks_merge(monkeypatch):
    """A downgraded COMMENT sets no reviewDecision — so it must block too.

    The 422 self-review path already told the operator "Merge held," but no gate
    read the downgrade, so the hold was never implemented. Measured 2026-09-01:
    11 of 15 unreviewed open ateles PRs are ateles-agent reviewing itself.
    """
    d = _dispatcher(monkeypatch)
    posted: list[str] = []

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None, headers=None):  # noqa: ANN001
            if url.endswith("/reviews"):
                body = json or {}
                if body.get("event") != "COMMENT":
                    return _Resp(status_code=422)
                return _Resp()
            posted.append((json or {}).get("body", ""))
            return _Resp()

        async def get(self, url, params=None, headers=None):  # noqa: ANN001
            return _Resp(payload=[])

    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda **kw: _Client())
    monkeypatch.setattr(sd.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(d, "_claim_escalation", _true)

    await d._emit_formal_review(
        _trigger(), "approve", "**APPROVE**\nlooks good"
    )

    assert any(UNRECORDED in b for b in posted), (
        "a verdict downgraded to a non-binding COMMENT must be marked, or the "
        "'Merge held' notification is a promise nothing keeps"
    )


@pytest.mark.asyncio
async def test_gate_still_passes_when_the_verdict_was_recorded(monkeypatch):
    """Not a fail-closed-always fix: a recorded clear verdict still merges."""
    d = _dispatcher(monkeypatch)
    checkpoints: list = []
    monkeypatch.setattr(
        d, "_store_merge_checkpoint",
        lambda *a, **k: checkpoints.append(a) or _async_none(),
    )
    monkeypatch.setattr(d, "_required_ci_state", lambda *a, **k: _async("green"))
    monkeypatch.setattr(d, "_verdict_recording_blocked", _false)

    await d._gate_merge_readiness(_trigger(), None, panel=[])

    assert checkpoints, "a recorded, clear verdict with green CI must still gate"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _async(value):
    return value


async def _async_none():
    return None


async def _true(*a, **k):
    return True


async def _false(*a, **k):
    return False
