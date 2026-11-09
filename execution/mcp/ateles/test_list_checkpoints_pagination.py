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
from lib.daemon_runtime.gating import MAX_QUERY_OFFSET  # noqa: E402


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


def _neotoma_like_post(briefs: list[dict], calls: list[dict]):
    """A `_post` that pages the way Neotoma's `POST /entities/query` does.

    Honours `limit` and `offset`, returns `total` and a `next_cursor` on a full
    page — and, like the real server, refuses a `cursor` sent beside
    `snapshot_filters` with HTTP 400 (ERR_CURSOR_COMBINATION) and an `offset`
    past the limit with HTTP 400 (ERR_OFFSET_TOO_DEEP). `_request` turns a 400
    into None after recording the transport error, so this fake does the same.
    """

    def _fake_post(path: str, body: dict):
        if path != "/entities/query":
            return None
        calls.append(dict(body))
        if body.get("cursor") and body.get("snapshot_filters"):
            server._record_transport_error(
                "request_failed", "POST", path, "HTTP 400 ERR_CURSOR_COMBINATION"
            )
            return None
        offset = int(body.get("offset") or 0)
        if offset > MAX_QUERY_OFFSET:
            server._record_transport_error(
                "request_failed", "POST", path, "HTTP 400 ERR_OFFSET_TOO_DEEP"
            )
            return None
        limit = int(body.get("limit") or 100)
        page = briefs[offset : offset + limit]
        nxt = offset + limit
        return {
            "entities": page,
            "total": len(briefs),
            # An opaque server cursor the next filtered request cannot use.
            "next_cursor": f"srv-{nxt}" if nxt < len(briefs) else None,
        }

    return _fake_post


@pytest.fixture
def prod_like_queue(monkeypatch):
    """A transport holding TOTAL pending briefs, paged the way Neotoma pages."""
    briefs = [_brief(i) for i in range(TOTAL)]
    calls: list[dict] = []
    monkeypatch.setattr(server, "_post", _neotoma_like_post(briefs, calls))
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


# ── Paging a FILTERED queue (ateles#1598) ────────────────────────────────────


def _walk(max_pages: int = 50, **kwargs) -> tuple[list[str], list[dict]]:
    seen: list[str] = []
    pages: list[dict] = []
    cursor = None
    for _ in range(max_pages):  # bounded: a paging bug must fail, not hang
        result = server._list_checkpoints(cursor=cursor, **kwargs)
        pages.append(result)
        assert not result.get("error"), f"page {len(pages)} failed: {result}"
        seen.extend(c["checkpoint_id"] for c in result["checkpoints"])
        cursor = result.get("next_cursor")
        if not cursor:
            break
    return seen, pages


def test_two_page_filtered_queue_is_walked_to_the_end(monkeypatch):
    """A queue one brief deeper than a page is reached in full via the cursor."""
    briefs = [_brief(i) for i in range(server.CHECKPOINT_PAGE_SIZE + 1)]
    calls: list[dict] = []
    monkeypatch.setattr(server, "_post", _neotoma_like_post(briefs, calls))
    monkeypatch.setattr(server, "_get", lambda path, params=None: None)

    seen, pages = _walk()

    assert len(pages) == 2
    assert pages[0]["truncated"] is True
    assert not pages[1].get("truncated")
    assert seen == [b["entity_id"] for b in briefs], "pages missing or repeated"
    for body in calls:
        assert "snapshot_filters" in body
        assert "cursor" not in body, "cursor sent beside snapshot_filters"


def test_fake_server_refuses_a_cursor_beside_a_filter(monkeypatch):
    """The fake's refusal is real: the request shape the old code sent fails."""
    calls: list[dict] = []
    post = _neotoma_like_post([_brief(i) for i in range(3)], calls)
    refused = post(
        "/entities/query",
        {
            "entity_type": "checkpoint_brief",
            "snapshot_filters": {"status": {"op": "eq", "value": "awaiting_operator"}},
            "cursor": "srv-50",
            "limit": 50,
        },
    )
    assert refused is None


def test_the_server_cursor_is_never_handed_to_the_caller(prod_like_queue):
    """The tool's cursor is its own; the server's cannot be sent back."""
    result = server._list_checkpoints()
    assert result["next_cursor"] != f"srv-{server.CHECKPOINT_PAGE_SIZE}"
    second = server._list_checkpoints(cursor=result["next_cursor"])
    assert not second.get("error"), second
    assert second["checkpoints"][0]["checkpoint_id"] == _brief(
        server.CHECKPOINT_PAGE_SIZE
    )["entity_id"]


@pytest.mark.parametrize("foreign", ["srv-50", "50", "not-a-cursor", "eyJ4IjoxfQ"])
def test_a_cursor_the_tool_did_not_issue_is_rejected(prod_like_queue, foreign):
    """A foreign cursor is an error, never silently read as page one."""
    result = server._list_checkpoints(cursor=foreign)
    assert result.get("error"), f"foreign cursor {foreign!r} was accepted: {result}"
    assert "cursor" in result["error"]
    assert result["checkpoints"] == []
    assert prod_like_queue == [], "a foreign cursor still reached the server"


def test_an_offset_past_the_server_limit_is_an_error(prod_like_queue):
    """A page Neotoma would refuse as too deep is reported, naming the limit."""
    # Built the way the tool builds its cursor, so on code that reads no
    # cursor of its own this is a cursor that cannot fail for being foreign.
    too_deep = server._encode_checkpoint_cursor(
        MAX_QUERY_OFFSET + 1,
        total=MAX_QUERY_OFFSET + 10,
        page_size=server.CHECKPOINT_PAGE_SIZE,
        first_digest="d",
        last_id="ent_x",
    )
    result = server._list_checkpoints(cursor=too_deep)
    assert result.get("error"), result
    assert str(MAX_QUERY_OFFSET) in result["error"]
    assert result.get("total") is None and not result.get("count")
    assert prod_like_queue == [], "a too-deep page was still requested"


# ── A queue that changes while it is walked (PR #1706 security review) ───────
#
# Neotoma refuses a cursor beside a filter, so the queue is walked by offset,
# and offset paging over a queue that changes between calls can skip a record
# with no page looking wrong. A walk across calls may claim completeness only
# when continuity is shown. Before this round each of these returned a clean,
# complete-looking last page (no `incomplete`, no `truncated`, no error).


def _mutable_queue(monkeypatch, size: int) -> list[dict]:
    """A queue the test can change between calls, paged like Neotoma."""
    briefs = [_brief(i) for i in range(size)]
    calls: list[dict] = []
    monkeypatch.setattr(
        server, "_post", lambda path, body: _neotoma_like_post(briefs, calls)(path, body)
    )
    monkeypatch.setattr(server, "_get", lambda path, params=None: None)
    return briefs


def test_a_record_resolved_between_pages_is_not_reported_complete(monkeypatch):
    """Security's scenario: 50 of 51, one resolved, page two empty with total 50.

    Red before this round: page two reported total 50, returned 0, no
    `truncated` and no error, so the walk read as the complete queue while the
    51st brief was never shown.
    """
    size = server.CHECKPOINT_PAGE_SIZE + 1
    briefs = _mutable_queue(monkeypatch, size)
    first = server._list_checkpoints()
    assert first["truncated"] is True and first["returned"] == size - 1
    del briefs[10]  # resolved before page two is read
    second = server._list_checkpoints(cursor=first["next_cursor"])
    assert second.get("incomplete") is True, second
    assert second.get("truncated") is True
    assert "queue changed while it was read" in second["incomplete_reason"]
    assert not second.get("next_cursor")


def test_a_shift_with_an_unchanged_total_is_not_reported_complete(monkeypatch):
    """One resolved and one added between pages: the total holds, a record is skipped.

    Red before this round: page two returned the new brief, the totals agreed,
    and the walk ended clean although the brief that slid onto page one was
    never handed out.
    """
    size = server.CHECKPOINT_PAGE_SIZE + 1
    briefs = _mutable_queue(monkeypatch, size)
    first = server._list_checkpoints()
    del briefs[10]
    briefs.append(_brief(size + 100))  # sorts last
    second = server._list_checkpoints(cursor=first["next_cursor"])
    seen = {c["checkpoint_id"] for c in first["checkpoints"] + second["checkpoints"]}
    assert _brief(server.CHECKPOINT_PAGE_SIZE)["entity_id"] not in seen  # the skip
    assert second.get("incomplete") is True, second
    assert "queue changed while it was read" in second["incomplete_reason"]


def test_a_record_added_mid_walk_that_repeats_one_is_not_complete(monkeypatch):
    """A record added ahead of the offset shifts a seen brief onto the next page."""
    size = 2 * server.CHECKPOINT_PAGE_SIZE + 1
    briefs = _mutable_queue(monkeypatch, size)
    first = server._list_checkpoints()
    briefs.insert(0, {**_brief(0), "entity_id": "ent_" + "0" * 23 + "_"})
    second = server._list_checkpoints(cursor=first["next_cursor"])
    assert second.get("incomplete") is True, second


def test_a_stable_multi_page_queue_is_still_complete(monkeypatch):
    """With nothing changing, a three-page walk ends clean, every brief once."""
    size = 2 * server.CHECKPOINT_PAGE_SIZE + 7
    briefs = _mutable_queue(monkeypatch, size)
    seen, pages = _walk()
    assert len(pages) == 3
    assert seen == [b["entity_id"] for b in briefs]
    assert not pages[-1].get("incomplete")
    assert not pages[-1].get("truncated")


# ── A malformed page never reads as a complete queue ─────────────────────────


def _one_page(monkeypatch, page: object):
    monkeypatch.setattr(server, "_post", lambda path, body: page)
    monkeypatch.setattr(server, "_get", lambda path, params=None: None)
    return server._list_checkpoints()


@pytest.mark.parametrize(
    "page, field",
    [
        ({"total": 0}, "entities"),
        ({"entities": None, "total": 0}, "entities"),
        ({"entities": {"a": 1}, "total": 0}, "entities"),
        ({"entities": []}, "total"),
        ({"entities": [], "total": "0"}, "total"),
        ({"entities": [], "total": 1.0}, "total"),
        ({"entities": [], "total": -1}, "total"),
        ({"entities": [], "total": False}, "total"),
        ({"entities": [], "total": 0, "next_cursor": 5}, "next_cursor"),
        ({"entities": [], "total": 0, "offset": "0"}, "offset"),
        ({"entities": [], "total": 0, "offset": 7}, "offset"),
        ({"entities": [{"snapshot": {}}], "total": 1}, "entities[0].entity_id"),
        ({"entities": [{"entity_id": ""}], "total": 1}, "entities[0].entity_id"),
        ({"entities": ["ent_x"], "total": 1}, "entities[0]"),
        (["not", "an", "object"], "response"),
    ],
)
def test_a_malformed_page_is_an_error_naming_the_field(monkeypatch, page, field):
    """Red before this round: each of these read as a complete queue
    (`data.get("entities", [])`, a tolerated missing total), mostly `total: 0`."""
    result = _one_page(monkeypatch, page)
    assert result.get("error"), f"malformed page read as a listing: {result}"
    assert result.get("malformed_field", "").startswith(field), result
    assert field.split("[")[0] in result["error"]
    assert result["checkpoints"] == []
    assert result.get("total") is None and not result.get("count")


def test_a_malformed_first_page_reread_leaves_the_walk_incomplete(monkeypatch):
    """The end-of-walk re-read is validated like any page."""
    size = server.CHECKPOINT_PAGE_SIZE + 1
    briefs = _mutable_queue(monkeypatch, size)
    first = server._list_checkpoints()
    real_post = server._post

    def _post(path, body):
        if not body.get("offset"):
            return {"entities": [], "total": "51"}
        return real_post(path, body)

    monkeypatch.setattr(server, "_post", _post)
    second = server._list_checkpoints(cursor=first["next_cursor"])
    assert second.get("incomplete") is True, second
    assert "total" in second["incomplete_reason"]
    assert len(briefs) == size
