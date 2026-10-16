"""`list_checkpoints` must never report a page as the queue (ateles#1037).

The defect, measured against Neotoma prod on 2026-09-16: the tool reported
`count: 50` while `POST /entities/query` for `checkpoint_brief` filtered to
`status=awaiting_operator` returned `total: 372`.

`_list_checkpoints` passed a hardcoded `limit=50`, never a cursor, and returned
`{"count": len(checkpoints)}` — the size of the PAGE, under a name that reads as
the size of the QUEUE. `_retrieve_entities` read neither `total` nor
`next_cursor` from the response, both of which the endpoint returns.

Because entities come back by `entity_id` ascending and ids are immutable, the
50 returned were a stable lexical prefix (`ent_01b8…` through `ent_20b7…`). The
same 50 reappeared on every call, so 322 pending checkpoints had never been
surfaced to anyone through this tool and no amount of re-calling it would have
surfaced them. Every triage built on this output worked a 13.4% slice while
reading it as the whole queue.

That is why this is the defect that hid the others: the terminal-task sweep
(ateles#1038) and the action-type miscategorisation (ateles#682) were both
invisible from behind it.

What these looked like RED, before the fix:

    test_reports_the_true_total_not_the_page_size
        AssertionError: reported 50 pending against a queue of 372 — a
        truncated page is indistinguishable from the whole queue

    test_every_pending_checkpoint_is_reachable
        AssertionError: 322 of 372 checkpoints were unreachable; the tool
        returns a stable lexical prefix, so they never appear

    test_truncation_is_stated_when_the_page_is_capped
        AssertionError: nothing in the response says the list is partial

Run: pytest execution/mcp/ateles/test_list_checkpoints_pagination.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import server  # noqa: E402


TOTAL = 372  # the live queue depth measured on 2026-09-16


def _brief(i: int) -> dict:
    """A brief whose entity_id sorts lexically, mirroring how ids come back."""
    return {
        "entity_id": f"ent_{i:024x}",
        "snapshot": {
            "status": "awaiting_operator",
            "title": f"PLAN checkpoint: task {i}",
            "handler": "apis",
            "task_entity_id": f"ent_task{i:019x}",
            "confidence": 0.0,
            "blast_radius": "high",
            "gate_action": "checkpoint_plan_approval",
            "reason": "below confidence threshold",
        },
    }


@pytest.fixture
def prod_like_queue(monkeypatch):
    """A transport holding TOTAL pending briefs, paged the way Neotoma pages.

    Honours `limit` and `cursor` and returns `total` and `next_cursor`, so a
    caller that ignores any of those is caught here rather than in production.
    """
    briefs = [_brief(i) for i in range(TOTAL)]
    calls: list[dict] = []

    def _fake_post(path: str, body: dict):
        if path != "/entities/query":
            return None
        calls.append(dict(body))
        limit = int(body.get("limit") or 100)
        start = int(body.get("cursor") or 0)
        page = briefs[start : start + limit]
        nxt = start + limit
        return {
            "entities": page,
            "total": len(briefs),
            "next_cursor": str(nxt) if nxt < len(briefs) else None,
        }

    monkeypatch.setattr(server, "_post", _fake_post)
    # Task-title hydration is a per-brief GET; keep it off the network.
    monkeypatch.setattr(server, "_get", lambda path, params=None: None)
    return calls


# ── The number the operator reads ────────────────────────────────────────────


def test_reports_the_true_total_not_the_page_size(prod_like_queue):
    """The headline defect: `count: 50` against a queue of 372."""
    result = server._list_checkpoints()
    assert result.get("total") == TOTAL, (
        f"reported {result.get('total')} pending against a queue of {TOTAL} — "
        "a truncated page is indistinguishable from the whole queue"
    )


def test_a_caller_reading_one_field_gets_the_alarming_number(prod_like_queue):
    """Whatever field a caller reads first must not understate the queue.

    The original `count` was the page length. A caller reading only `count`
    was told 50. No field in the response may now report fewer than the true
    total without also being explicitly a page-size field.
    """
    result = server._list_checkpoints()
    assert result.get("total") == TOTAL
    returned = result.get("returned", len(result.get("checkpoints", [])))
    assert returned <= result["total"]
    if returned < result["total"]:
        assert result.get("truncated") is True


# ── Reachability ─────────────────────────────────────────────────────────────


def test_every_pending_checkpoint_is_reachable(prod_like_queue):
    """322 briefs had never been surfaced to anyone. They must be now."""
    seen: set[str] = set()
    cursor = None
    for _ in range(50):  # bounded: a paging bug must fail, not hang
        result = server._list_checkpoints(cursor=cursor) if cursor else server._list_checkpoints()
        seen.update(c["checkpoint_id"] for c in result["checkpoints"])
        cursor = result.get("next_cursor")
        if not cursor:
            break
    missing = TOTAL - len(seen)
    assert missing == 0, (
        f"{missing} of {TOTAL} checkpoints were unreachable; the tool returns "
        "a stable lexical prefix, so they never appear"
    )


def test_the_newest_checkpoints_are_reachable(prod_like_queue):
    """The prefix was `ent_01b8…`-`ent_20b7…` — never the tail.

    Sorting by entity_id is not sorting by age, so the unreachable 322 were not
    "the oldest" or "the least urgent". They were an arbitrary set nobody chose.
    """
    seen: set[str] = set()
    cursor = None
    for _ in range(50):
        result = server._list_checkpoints(cursor=cursor) if cursor else server._list_checkpoints()
        seen.update(c["checkpoint_id"] for c in result["checkpoints"])
        cursor = result.get("next_cursor")
        if not cursor:
            break
    assert _brief(TOTAL - 1)["entity_id"] in seen


def test_truncation_is_stated_when_the_page_is_capped(prod_like_queue):
    """A partial list must say so, in the response, not only by implication."""
    result = server._list_checkpoints()
    if len(result["checkpoints"]) < TOTAL:
        assert result.get("truncated") is True, (
            "nothing in the response says the list is partial"
        )
        assert result.get("next_cursor"), "no way to reach the rest"


# ── It must still be correct on a small queue ────────────────────────────────


def test_short_queue_is_not_marked_truncated(monkeypatch):
    """A queue that fits in one page must not claim truncation, or the signal
    becomes noise and gets ignored — which is how the original `count` was
    read."""
    briefs = [_brief(i) for i in range(3)]
    monkeypatch.setattr(
        server,
        "_post",
        lambda path, body: {"entities": briefs, "total": 3, "next_cursor": None},
    )
    monkeypatch.setattr(server, "_get", lambda path, params=None: None)
    result = server._list_checkpoints()
    assert result["total"] == 3
    assert len(result["checkpoints"]) == 3
    assert not result.get("truncated")
    assert not result.get("next_cursor")


def test_transport_failure_does_not_report_an_empty_queue(monkeypatch):
    """An unreachable Neotoma must not read as "no pending checkpoints".

    A false zero here is the most dangerous possible output: it says the
    operator has nothing to decide. CLAUDE.md's instrument rule in one case.
    """
    monkeypatch.setattr(server, "_post", lambda path, body: None)
    monkeypatch.setattr(server, "_get", lambda path, params=None: None)
    result = server._list_checkpoints()
    assert result.get("error") or result.get("total") is None, (
        "a failed read reported as an empty queue"
    )
