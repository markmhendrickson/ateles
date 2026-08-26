"""The gate lookup must not be bounded by an arbitrary first page.

`IssueGateStore.load` previously issued ONE unpaginated `POST /entities/query`
with `limit: 500` and scanned the result client-side. The live corpus holds
~4,144 `issue` entities, so roughly 88% of them were unreachable: an issue
outside that window read as `found == False`.

Every caller of this loader fails CLOSED — gate checks, waives, and the
auto-merge readiness path all decline when the entity "cannot be read". So the
truncation did not surface as an error. It surfaced as a permanent silent
block, logged as "no issue entity", which misattributes a pagination bug to
missing data.

Observed on ateles#513: entity present with pm/ux/arch signed off, while the
dispatcher logged "no issue entity, so no gate_status to check (fail closed)".

Run: pytest execution/daemons/apis/test_gate_store_lookup.py -v
"""

from __future__ import annotations

import pytest

from gate_waive import IssueGateStore


def _entity(number: int, repo: str, gates: dict | None = None) -> dict:
    return {
        "entity_id": f"ent_{number}",
        "snapshot": {
            "github_number": number,
            "repo": repo,
            "gate_status": gates if gates is not None else {"pm": "signed_off"},
        },
    }


class _Recorder:
    """Captures the payloads `load` sends, and replays canned responses."""

    def __init__(self, responses: list[dict | None]) -> None:
        self.responses = responses
        self.payloads: list[dict] = []

    async def __call__(self, path: str, payload: dict):  # noqa: ANN001
        self.payloads.append(payload)
        i = min(len(self.payloads) - 1, len(self.responses) - 1)
        return self.responses[i]


@pytest.mark.asyncio
async def test_lookup_filters_server_side(monkeypatch):
    """The query must constrain by github_number + repo, not fetch-then-scan."""
    store = IssueGateStore("http://x", "tok")
    rec = _Recorder([{"entities": [_entity(513, "o/r")], "total": 1}])
    monkeypatch.setattr(store, "_post", rec)

    state = await store.load("o/r", 513)

    assert state.found and state.triaged
    filters = rec.payloads[0].get("snapshot_filters")
    assert filters, "the lookup must filter server-side"
    assert filters["github_number"]["value"] == 513
    assert filters["repo"]["value"] == "o/r"
    assert rec.payloads[0]["limit"] <= 10, (
        "a filtered lookup needs a tiny limit, not a 500-row window"
    )


@pytest.mark.asyncio
async def test_entity_beyond_the_old_window_is_found(monkeypatch):
    """The regression, directly.

    An entity that a 500-row unfiltered page would never have contained must
    still be found. Under the old implementation this returned found == False
    and every caller failed closed.
    """
    store = IssueGateStore("http://x", "tok")
    rec = _Recorder([{"entities": [_entity(4000, "o/r")], "total": 1}])
    monkeypatch.setattr(store, "_post", rec)

    state = await store.load("o/r", 4000)

    assert state.found, "an entity outside the legacy window must be found"
    assert state.gate_status == {"pm": "signed_off"}


@pytest.mark.asyncio
async def test_legacy_entity_falls_back_to_a_paged_scan(monkeypatch):
    """Entities lacking composite snapshot fields still resolve — via paging.

    The fallback must PAGE rather than read one truncated window, so a miss
    means absent rather than beyond an arbitrary boundary.
    """
    store = IssueGateStore("http://x", "tok")
    page1 = {"entities": [_entity(1, "o/r")], "next_cursor": "c1"}
    page2 = {"entities": [_entity(777, "o/r")], "next_cursor": ""}
    rec = _Recorder([{"entities": [], "total": 0}, page1, page2])
    monkeypatch.setattr(store, "_post", rec)

    state = await store.load("o/r", 777)

    assert state.found, "the target was on page 2 of the fallback scan"
    assert len(rec.payloads) >= 3, "the fallback must page, not read once"
    assert rec.payloads[2].get("cursor") == "c1", "second page must pass a cursor"


@pytest.mark.asyncio
async def test_genuinely_absent_entity_is_not_found(monkeypatch):
    """Fail-closed must still work: nothing anywhere means found == False."""
    store = IssueGateStore("http://x", "tok")
    rec = _Recorder([{"entities": [], "total": 0, "next_cursor": ""}])
    monkeypatch.setattr(store, "_post", rec)

    state = await store.load("o/r", 999)

    assert not state.found and not state.triaged


@pytest.mark.asyncio
async def test_transport_failure_degrades_to_empty_state(monkeypatch):
    """A None response must not raise into the dispatch pipeline."""
    store = IssueGateStore("http://x", "tok")
    rec = _Recorder([None])
    monkeypatch.setattr(store, "_post", rec)

    state = await store.load("o/r", 513)

    assert not state.found
