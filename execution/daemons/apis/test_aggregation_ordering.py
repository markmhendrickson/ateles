"""
The newest aggregation comment must win (ateles#430).

## The bug

GitHub's issue-comments endpoint SILENTLY IGNORES `sort` and `direction` — it
always returns oldest-first. Two call sites passed `sort=created&direction=desc`,
believed the response was newest-first, and `return`ed on the first
`<!-- vanellus-aggregation -->` match. On any PR with more than one aggregation
they read the OLDEST verdict.

Reproduced against the live API on 2026-08-19:

    gh api ".../issues/2153/comments?per_page=100&direction=desc" --jq '[.[].id]'
    gh api ".../issues/2153/comments?per_page=100&direction=asc"  --jq '[.[].id]'
    # -> byte-identical

On neotoma#2153 that republished a nine-day-old REQUEST_CHANGES over a current
COMMENT / Blocking: 0.

## Why the merge-gate case is the dangerous one

`_pr_review_is_clear` gates merges and failed UNSAFE: an early APPROVE
superseded by a later REQUEST_CHANGES read as clear, so a PR could merge on an
approval the panel had already withdrawn. The fallback bug re-blocks cleared
work (wasteful); this one ships blocked work.

Every fixture below returns comments OLDEST-FIRST, mirroring the real API. A
test that fed them newest-first would pass against the buggy code and prove
nothing.

Run: pytest execution/daemons/apis/test_aggregation_ordering.py -v
"""

from __future__ import annotations

import pytest

import swarm_dispatch as sd

MARKER = "<!-- vanellus-aggregation -->"


def _c(cid: int, created: str, verdict: str | None, marker: bool = True) -> dict:
    body = f"{MARKER}\n**Vanellus**\n" if marker else "just a human comment\n"
    if verdict:
        body += f"\n**{verdict}**\n\nVerdict: {verdict}\n"
    return {"id": cid, "created_at": created, "body": body}


# ---------------------------------------------------------------------------
# The pure selector
# ---------------------------------------------------------------------------


def test_selects_newest_not_first_in_list():
    """The #2153 shape: old REQUEST_CHANGES, then a current COMMENT."""
    comments = [
        _c(1, "2026-08-10T11:54:23Z", "REQUEST_CHANGES"),
        _c(2, "2026-08-19T10:08:46Z", "COMMENT"),
    ]
    picked = sd.latest_aggregation_comment(comments)
    assert picked is not None and picked["id"] == 2, (
        "must select the NEWEST aggregation, not the first one returned"
    )


def test_ignores_non_marker_comments():
    comments = [
        _c(1, "2026-08-19T12:00:00Z", "APPROVE", marker=False),
        _c(2, "2026-08-10T09:00:00Z", "REQUEST_CHANGES"),
    ]
    picked = sd.latest_aggregation_comment(comments)
    assert picked is not None and picked["id"] == 2


def test_none_when_no_aggregation():
    assert sd.latest_aggregation_comment([_c(1, "2026-08-19T12:00:00Z", None, marker=False)]) is None
    assert sd.latest_aggregation_comment([]) is None


def test_ties_break_on_id():
    """Identical timestamps: the higher id is the later comment."""
    comments = [
        _c(7, "2026-08-19T10:00:00Z", "REQUEST_CHANGES"),
        _c(9, "2026-08-19T10:00:00Z", "APPROVE"),
    ]
    picked = sd.latest_aggregation_comment(comments)
    assert picked is not None and picked["id"] == 9


def test_survives_missing_fields():
    """A malformed comment must not crash the selector."""
    comments = [{"body": MARKER}, _c(3, "2026-08-19T10:00:00Z", "APPROVE")]
    picked = sd.latest_aggregation_comment(comments)
    assert picked is not None and picked["id"] == 3


# ---------------------------------------------------------------------------
# The merge gate — the unsafe-direction case
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: list[dict]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> list[dict]:
        return self._payload


class _FakeClient:
    """Serves comments OLDEST-FIRST, exactly as GitHub does."""

    def __init__(self, pages: list[list[dict]]) -> None:
        self.pages = pages
        self.requested: list[int] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, params=None, headers=None):  # noqa: ANN001
        page = int((params or {}).get("page", 1))
        self.requested.append(page)
        idx = page - 1
        return _FakeResponse(self.pages[idx] if idx < len(self.pages) else [])


def _dispatcher(monkeypatch, pages: list[list[dict]]) -> tuple[sd.SwarmDispatcher, _FakeClient]:
    d = sd.SwarmDispatcher(notifier=type("N", (), {"send": lambda *a, **k: None})())
    client = _FakeClient(pages)
    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda **kw: client)
    monkeypatch.setattr(d, "_github_headers", lambda repo="": {})
    return d, client


@pytest.mark.asyncio
async def test_merge_gate_does_not_clear_on_a_withdrawn_approval(monkeypatch):
    """THE test. Old APPROVE, newer REQUEST_CHANGES -> must NOT be clear.

    This is the failure that ships blocked work: reading the oldest aggregation
    returns the superseded APPROVE and the PR merges.
    """
    pages = [[
        _c(1, "2026-08-10T09:00:00Z", "APPROVE"),
        _c(2, "2026-08-19T09:00:00Z", "REQUEST_CHANGES"),
    ]]
    d, _ = _dispatcher(monkeypatch, pages)

    clear = await d._pr_review_is_clear("o/r", 1)

    assert clear is False, (
        "a later REQUEST_CHANGES supersedes an earlier APPROVE — clearing here "
        "merges a PR on a withdrawn approval"
    )


@pytest.mark.asyncio
async def test_merge_gate_clears_when_the_newest_verdict_is_clear(monkeypatch):
    """The converse must still work — this is not a fail-closed-always fix."""
    pages = [[
        _c(1, "2026-08-10T09:00:00Z", "REQUEST_CHANGES"),
        _c(2, "2026-08-19T09:00:00Z", "APPROVE"),
    ]]
    d, _ = _dispatcher(monkeypatch, pages)
    assert await d._pr_review_is_clear("o/r", 1) is True


@pytest.mark.asyncio
async def test_merge_gate_reads_past_the_first_page(monkeypatch):
    """>100 comments: the newest aggregation sits on page 2.

    The old docstring claimed direction=desc protected this case. It did not —
    the parameter is inert, so the newest verdict was never even fetched.
    """
    page1 = [_c(i, f"2026-08-10T09:{i:02d}:00Z", None, marker=False) for i in range(100)]
    page1[0] = _c(1, "2026-08-10T09:00:00Z", "APPROVE")
    page2 = [_c(500, "2026-08-19T09:00:00Z", "REQUEST_CHANGES")]
    d, client = _dispatcher(monkeypatch, [page1, page2])

    clear = await d._pr_review_is_clear("o/r", 1)

    assert client.requested == [1, 2], "must page past a full first page"
    assert clear is False, "the newest verdict lived on page 2 and blocks the merge"


@pytest.mark.asyncio
async def test_merge_gate_fails_closed_with_no_aggregation(monkeypatch):
    d, _ = _dispatcher(monkeypatch, [[_c(1, "2026-08-19T09:00:00Z", None, marker=False)]])
    assert await d._pr_review_is_clear("o/r", 1) is False


# ---------------------------------------------------------------------------
# The verdict-recovery fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fallback_recovers_the_newest_verdict(monkeypatch):
    """The exact neotoma#2153 shape."""
    pages = [[
        _c(1, "2026-08-10T11:54:23Z", "REQUEST_CHANGES"),
        _c(2, "2026-08-19T10:08:46Z", "COMMENT"),
    ]]
    d, _ = _dispatcher(monkeypatch, pages)
    trigger = type("T", (), {"repository": "o/r", "number": 2153})()

    verdict, fired = await d._resolve_review_verdict(trigger, "no token here")

    assert fired is True
    assert verdict == "comment", (
        "recovered the stale REQUEST_CHANGES instead of the current verdict"
    )
