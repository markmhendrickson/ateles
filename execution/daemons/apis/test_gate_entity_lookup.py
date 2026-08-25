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

import logging

from entity_lookup import SCAN_MAX_PAGES, SCAN_PAGE_SIZE
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

        # Unsorted (default) order is entity_id ASCENDING — the prod behaviour
        # that put recent entities outside the old single-page window. Only an
        # explicit recency sort reverses it.
        ordered = self.corpus
        if (
            payload.get("sort_by") == "last_observation_at"
            and payload.get("sort_order") == "desc"
        ):
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


# ── QA DoD (ateles#492): named required tests ────────────────────────────────


@pytest.mark.asyncio
async def test_issue_gate_store_load_finds_entity_outside_unsorted_first_500():
    """DoD regression: the target sits outside an unsorted first-500 window."""
    decoys = [_issue_entity(n) for n in range(1, 601)]
    target = _issue_entity(TARGET)
    store = _FakeStore(decoys + [target], supports_snapshot_filters=False)

    state = await store.load(REPO, TARGET)

    assert state.found is True
    assert state.entity_id == f"ent_{TARGET:024d}"
    assert state.gate_status["pm"] == "signed_off"
    assert state.current_owner == "cicada"


@pytest.mark.asyncio
async def test_issue_gate_store_load_query_is_filtered_not_unbounded_scan():
    """DoD: the FIRST query carries a server-side identity filter, not a scan."""
    store = _FakeStore([_issue_entity(TARGET)], supports_snapshot_filters=True)
    await store.load(REPO, TARGET)

    first = store.queries[0]
    sf = first.get("snapshot_filters")
    assert sf, "first query must be server-side filtered"
    assert first["limit"] <= 10, "a filtered identity lookup must not fetch 500"

    # Across all targeted combos, both spellings of each field are covered.
    targeted = [q for q in store.queries if q.get("snapshot_filters")]
    fields = {f for q in targeted for f in q["snapshot_filters"]}
    assert {"repo", "repository"} & fields
    assert {"issue_number", "github_number"} & fields


@pytest.mark.asyncio
async def test_gate_load_logs_not_found_within_window_on_bounded_fallback_cap(caplog):
    """DoD: exhausting the scan bound says so, instead of a bare not-found.

    'entity not found' and 'not found within the fetched window' are different
    diagnoses; #492 reports the original conflation sent its reporter chasing a
    missing entity that was there all along.
    """
    # Every page is FULL and never matches, so the scan runs to its cap.
    # Decoys only — TARGET must be genuinely absent so the cap is what ends the scan.
    full_corpus = [
        _issue_entity(n)
        for n in range(1, SCAN_PAGE_SIZE * SCAN_MAX_PAGES + 50)
        if n != TARGET
    ]
    store = _FakeStore(full_corpus, supports_snapshot_filters=False)

    with caplog.at_level(logging.WARNING, logger="apis.entity_lookup"):
        state = await store.load(REPO, TARGET)

    assert not state.found
    scans = [q for q in store.queries if q.get("sort_by") == "last_observation_at"]
    assert len(scans) == SCAN_MAX_PAGES, "scan must run to its cap, then stop"

    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert "NOT exhausted" in warning
    assert "not found in the scanned window" in warning
    assert "no such entity" in warning


@pytest.mark.asyncio
async def test_post_returning_none_degrades_to_empty_state():
    """A dead transport must degrade to not-found, never raise or half-populate."""

    class _Dead(_FakeStore):
        async def _post(self, path: str, payload: dict):  # type: ignore[override]
            self.queries.append(payload)
            return None

    store = _Dead([_issue_entity(TARGET)], supports_snapshot_filters=False)
    state = await store.load(REPO, TARGET)

    assert not state.found
    assert state.gate_status == {}


@pytest.mark.asyncio
async def test_impl_gate_path_does_not_fail_closed_when_entity_exists_outside_window(
    monkeypatch,
):
    """DoD caller-path test: the REAL fail-closed branch, not `load()` alone.

    ateles#492's observable symptom was `_gates_green` taking the
    "no issue entity ... fail closed" branch for an entity that existed with
    green gates. Everything below `load()` is exercised for real here — only
    the HTTP layer is stubbed — so this fails if the lookup regresses, which a
    test that stubs `load()` itself (as `test_gates_green_reads_entity.py`
    necessarily does for its own purpose) cannot catch.
    """
    import swarm_dispatch as sd

    green = '{"pm": "signed_off", "ux": "not_required", "arch": "signed_off"}'
    target = _issue_entity(TARGET, gates=green)
    # The target sits past an unsorted first-500 window, as in prod.
    corpus = [_issue_entity(n, gates=green) for n in range(1, 601)] + [target]

    async def fake_post(self, path: str, payload: dict):  # noqa: ANN001
        if payload.get("snapshot_filters"):
            # The neotoma#2127 behaviour: filter ignored, success returned.
            return {"entities": corpus[: payload.get("limit", 100)]}
        ordered = corpus
        if payload.get("sort_by") == "last_observation_at":
            ordered = list(reversed(corpus))
        offset = payload.get("offset", 0)
        return {"entities": ordered[offset : offset + payload.get("limit", 100)]}

    monkeypatch.setattr(sd.IssueGateStore, "_post", fake_post)

    class _Notifier:
        def send(self, msg, priority=None, handler=None):  # noqa: ANN001
            pass

    class _Ok:
        ok = True
        stdout = "Triage complete."
        error = None
        returncode = 0

    dispatcher = sd.SwarmDispatcher(notifier=_Notifier())

    assert await dispatcher._gates_green(_Ok(), REPO, TARGET) is True, (
        "gates are signed off on an entity that exists — the impl handoff must "
        "not take the 'no issue entity -> NOT green' branch"
    )
