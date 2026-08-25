"""
The gate check must find a recently-created issue entity (ateles#492).

## The failure

``IssueGateStore.load()`` fetched ONE unsorted page of 500 ``issue`` entities
out of ~4,039 and scanned it client-side. The default order is ``entity_id``
ascending, not recency, so the window was an arbitrary slice that excluded
essentially every recently-created issue. ``load()`` returned ``found == False``
and the caller (``_gates_are_green``) failed closed:

    markmhendrickson/ateles#491: no issue entity, so no gate_status to check
    — treating gates as NOT green (fail closed)

21 occurrences across both repos. The entities existed and had green gates
written by the swarm's own agents; the lookup simply could not see them. Net
effect: no newly filed issue could reach implementation.

Fail-closed is the right default for an UNREADABLE entity — the bug is that a
readable one was reported unreadable.

Run: pytest execution/daemons/apis/test_gate_entity_lookup.py -v
"""

from __future__ import annotations

import pytest

from gate_waive import IssueGateStore


REPO = "markmhendrickson/ateles"
TARGET = 494


def _issue_entity(number: int, *, repo: str = REPO, gates: str | None = None) -> dict:
    """An entity in the shape prod returns (nested snapshot, both spellings)."""
    return {
        "entity_id": f"ent_{number:024d}",
        "snapshot": {
            "snapshot": {
                "repo": repo,
                "repository": repo,
                "issue_number": number,
                "github_number": str(number),
                "gate_status": gates or '{"pm": "signed_off", "ux": "signed_off"}',
                "current_owner": "cicada",
                "owner_history": [],
            }
        },
    }


class _FakeStore(IssueGateStore):
    """Records every query and replays canned pages."""

    def __init__(self, corpus: list[dict], *, supports_snapshot_filters: bool):
        super().__init__("https://example.invalid", "tok")
        self.corpus = corpus
        self.supports_snapshot_filters = supports_snapshot_filters
        self.queries: list[dict] = []

    async def _post(self, path: str, payload: dict):  # type: ignore[override]
        self.queries.append(payload)
        sf = payload.get("snapshot_filters")
        if sf:
            if not self.supports_snapshot_filters:
                # The neotoma#2042/#2127 behaviour: filter accepted, ignored,
                # success returned with an unfiltered page.
                return {"entities": self.corpus[: payload.get("limit", 100)]}
            matched = []
            for ent in self.corpus:
                snap = ent["snapshot"]["snapshot"]
                if all(
                    str(snap.get(field)) == str(spec["value"])
                    for field, spec in sf.items()
                ):
                    matched.append(ent)
            return {"entities": matched[: payload.get("limit", 100)]}

        ordered = self.corpus
        if payload.get("sort_by") == "last_observation_at":
            if payload.get("sort_order") == "desc":
                ordered = list(reversed(self.corpus))
        offset = payload.get("offset", 0)
        limit = payload.get("limit", 100)
        return {"entities": ordered[offset : offset + limit]}


def _corpus_with_target_out_of_first_page() -> list[dict]:
    """Target sits far past the first page in ascending (default) order.

    This is the prod shape: ~4,039 entities, the recent one near the end.
    """
    older = [_issue_entity(n) for n in range(1, 1200)]
    return older + [_issue_entity(TARGET)]


@pytest.mark.asyncio
async def test_finds_recent_entity_via_targeted_filter():
    """When snapshot_filters works, one targeted query resolves the entity."""
    store = _FakeStore(
        _corpus_with_target_out_of_first_page(), supports_snapshot_filters=True
    )
    state = await store.load(REPO, TARGET)

    assert state.entity_id == f"ent_{TARGET:024d}"
    assert state.gate_status["pm"] == "signed_off"
    # The first query must be the targeted one, and it must be cheap.
    assert store.queries[0].get("snapshot_filters")
    assert store.queries[0]["limit"] <= 10


@pytest.mark.asyncio
async def test_finds_recent_entity_when_server_ignores_the_filter():
    """REGRESSION (ateles#492).

    With snapshot_filters silently ignored — the neotoma#2042/#2127 behaviour —
    the lookup must STILL find a recent entity via the recency-sorted fallback.
    Under the old single-unsorted-page implementation this returned not-found.
    """
    store = _FakeStore(
        _corpus_with_target_out_of_first_page(), supports_snapshot_filters=False
    )
    state = await store.load(REPO, TARGET)

    assert state.entity_id == f"ent_{TARGET:024d}", (
        "recent entity must be reachable even when the server drops the filter"
    )
    assert state.gate_status["pm"] == "signed_off"
    # A recency-sorted scan must have run.
    scans = [q for q in store.queries if q.get("sort_by") == "last_observation_at"]
    assert scans, "expected a recency-sorted fallback scan"
    assert scans[0]["sort_order"] == "desc"


@pytest.mark.asyncio
async def test_targeted_hit_is_reverified_not_trusted_blindly():
    """A filter that returns the WRONG entity must not be accepted.

    neotoma#2127 returns unrelated entities for a targeted lookup while
    reporting success. Trusting that would read another issue's gate_status —
    strictly worse than the old honest not-found.
    """
    wrong_only = [_issue_entity(9999)]

    class _WrongHit(_FakeStore):
        async def _post(self, path: str, payload: dict):  # type: ignore[override]
            self.queries.append(payload)
            # Always answer with the wrong entity, filtered or not.
            return {"entities": wrong_only}

    store = _WrongHit(wrong_only, supports_snapshot_filters=False)
    state = await store.load(REPO, TARGET)

    assert state.entity_id == "", "must not adopt an entity that fails _matches"
    assert not state.found


@pytest.mark.asyncio
async def test_absent_entity_still_reports_not_found():
    """Fail-closed must survive: a genuinely absent entity is still not-found."""
    store = _FakeStore(
        [_issue_entity(n) for n in range(1, 50)], supports_snapshot_filters=True
    )
    state = await store.load(REPO, TARGET)

    assert state.entity_id == ""
    assert not state.found


@pytest.mark.asyncio
async def test_scan_stops_on_short_page():
    """The scan must not keep paging past the end of the listing."""
    store = _FakeStore(
        [_issue_entity(n) for n in range(1, 30)], supports_snapshot_filters=False
    )
    await store.load(REPO, TARGET)

    scans = [q for q in store.queries if q.get("sort_by") == "last_observation_at"]
    assert len(scans) == 1, "a short first page means the listing is exhausted"


@pytest.mark.asyncio
async def test_matches_tolerates_both_field_spellings():
    """Prod carries repo/repository and issue_number/github_number."""
    snap_repo_only = {"repo": REPO, "issue_number": TARGET}
    snap_repository_only = {"repository": REPO, "github_number": str(TARGET)}

    assert IssueGateStore._matches(snap_repo_only, REPO, TARGET)
    assert IssueGateStore._matches(snap_repository_only, REPO, TARGET)
    assert not IssueGateStore._matches(snap_repo_only, REPO, TARGET + 1)
    assert not IssueGateStore._matches(
        {"repo": "other/repo", "issue_number": TARGET}, REPO, TARGET
    )


@pytest.mark.asyncio
async def test_targeted_combos_are_not_abandoned_on_a_nonmatching_page():
    """A non-empty, non-matching page must NOT abandon the remaining combos.

    Under the neotoma#2127 behaviour every targeted query comes back non-empty
    with unrelated entities. Treating "non-empty but nothing matched" as a
    negative signal — the obvious latency optimization — would skip the field
    combos that follow, including one that resolves the entity. This pins the
    trap: the corpus is reachable ONLY via the `repository`/`github_number`
    spelling, while the earlier combos return noise.
    """
    target = {
        "entity_id": "ent_target",
        "snapshot": {
            "snapshot": {
                # No `repo` and no `issue_number`: only the LAST combo matches.
                "repository": REPO,
                "github_number": str(TARGET),
                "gate_status": '{"pm": "signed_off"}',
                "current_owner": "cicada",
                "owner_history": [],
            }
        },
    }
    noise = [_issue_entity(n) for n in range(1, 6)]

    class _NoisyFilter(_FakeStore):
        async def _post(self, path: str, payload: dict):  # type: ignore[override]
            self.queries.append(payload)
            sf = payload.get("snapshot_filters") or {}
            # Only the repository+github_number combo resolves; others return
            # a non-empty page of unrelated entities (the #2127 shape).
            if "repository" in sf and "github_number" in sf:
                return {"entities": [target]}
            if sf:
                return {"entities": noise}
            return {"entities": noise}

    store = _NoisyFilter(noise + [target], supports_snapshot_filters=False)
    state = await store.load(REPO, TARGET)

    assert state.entity_id == "ent_target", (
        "must keep trying field combos after a non-matching page"
    )
    assert state.gate_status["pm"] == "signed_off"
