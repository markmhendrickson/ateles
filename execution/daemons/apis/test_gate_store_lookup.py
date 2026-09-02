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
    # ateles#390: the number may be stored as `number`, `github_number` or
    # `issue_number`, and load() tries them in turn. Which name is filtered
    # first is not the contract — filtering server-side on SOME number field,
    # scoped to the repo, is.
    number_filters = {k: v for k, v in filters.items() if k != "repo"}
    assert len(number_filters) == 1
    assert next(iter(number_filters.values()))["value"] == 513
    assert next(iter(number_filters)) in {
        "number",
        "github_number",
        "issue_number",
    }
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

    # ateles#390: load() tries each number field name before falling back, so
    # respond by payload SHAPE rather than by call index — a filtered query
    # misses, the first unfiltered query is page 1, the next is page 2.
    class _ShapedRecorder(_Recorder):
        async def __call__(self, path: str, payload: dict):  # noqa: ANN001
            self.payloads.append(payload)
            if payload.get("snapshot_filters"):
                return {"entities": [], "total": 0}
            return page2 if payload.get("cursor") else page1

    rec = _ShapedRecorder([])
    monkeypatch.setattr(store, "_post", rec)

    state = await store.load("o/r", 777)

    assert state.found, "the target was on page 2 of the fallback scan"
    scans = [p for p in rec.payloads if not p.get("snapshot_filters")]
    assert len(scans) >= 2, "the fallback must page, not read once"
    assert scans[1].get("cursor") == "c1", "second page must pass a cursor"


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


# ── ateles#390: issue entities are keyed by `number`, not `github_number` ────


def test_matches_accepts_the_number_field():
    """Prod issue entities store the issue number as ``number``.

    Measured 2026-09-02 against ent_e882a86eb583b828ac00f98b (ateles#390's own
    entity): it carries ``number: 390`` with NO ``issue_number`` and NO
    ``github_number``. The matcher knew only the latter two, so the gate store
    reported "no Neotoma issue entity" for an entity that exists — which is the
    error in issue #390's symptom log. Drop ``number`` from the matcher and this
    test fails.
    """
    from gate_waive import IssueGateStore

    snap = {
        "repo": "markmhendrickson/ateles",
        "repository": "markmhendrickson/ateles",
        "number": 390,
        "gate_status": {"arch": "pending"},
    }
    assert IssueGateStore._matches(snap, "markmhendrickson/ateles", 390)
    # Still repo-scoped: a matching number in another repo is not a match.
    assert not IssueGateStore._matches(snap, "markmhendrickson/neotoma", 390)
    # And a different number in the right repo is not a match.
    assert not IssueGateStore._matches(snap, "markmhendrickson/ateles", 391)


def test_load_falls_back_across_number_field_names(monkeypatch):
    """The server-side filter must try ``number`` as well as ``github_number``.

    A ``github_number``-only filter returned 0 rows against prod for an entity
    that a ``number`` filter returned immediately, so every lookup degraded to
    the bounded scan.
    """
    import asyncio

    from gate_waive import IssueGateStore

    store = IssueGateStore("https://example.invalid", "tok")
    seen: list[str] = []

    async def fake_post(path, payload):
        filters = payload.get("snapshot_filters") or {}
        field = next(
            (k for k in filters if k != "repo"), "<unfiltered-scan>"
        )
        seen.append(field)
        if field != "number":
            return {"entities": []}
        return {
            "entities": [
                {
                    "entity_id": "ent_390",
                    "snapshot": {
                        "repo": "markmhendrickson/ateles",
                        "number": 390,
                        "gate_status": {"arch": "pending"},
                    },
                }
            ]
        }

    monkeypatch.setattr(store, "_post", fake_post)
    state = asyncio.run(store.load("markmhendrickson/ateles", 390))

    assert state.found and state.entity_id == "ent_390"
    assert state.gate_status == {"arch": "pending"}
    # `number` is tried first, and the miss never reached the unbounded scan.
    assert seen[0] == "number"
    assert "<unfiltered-scan>" not in seen
