"""
`IssueSpecStore.load()` must find a spec outside an unsorted first-200 window
(ateles#492 parity, ateles#498).

## The failure

`issue_spec.py` had the same defect as `gate_waive.py`: one unfiltered, unsorted
page (`limit: 200`) of `issue_spec` entities, scanned client-side. Default order
is `entity_id` ascending, not recency, so once the corpus passes 200 the window
excludes recent specs. There are 317 `issue_spec` entities in prod, so this was
already live.

## Why it is worse here than for the gate check

A gate lookup miss fails CLOSED — honest, if obstructive. A spec lookup miss
degrades to an empty `SpecState`, so each lens **creates** a spec entity instead
of **correcting** the existing one. The create succeeds, the pipeline continues,
and nothing reports that a prior spec existed — the duplicate-entity shape of
neotoma#1919.

Run: pytest execution/daemons/apis/test_issue_spec_entity_lookup.py -v
"""

from __future__ import annotations

import pytest

from entity_lookup import SCAN_PAGE_SIZE
from issue_spec import IssueSpecStore


REPO = "markmhendrickson/ateles"
TARGET = 494
TITLE = "Gate sign-off is not order-enforced"


def _spec_entity(number: int, *, repo: str = REPO) -> dict:
    return {
        "entity_id": f"ent_spec{number:020d}",
        "snapshot": {
            "repo": repo,
            "repository": repo,
            "issue_number": number,
            "github_number": str(number),
            "title": f"spec for #{number}",
            "pm_section": "PM section body",
            "sequence_state": ["pm", "arch"],
        },
    }


class _FakeSpecStore(IssueSpecStore):
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
                # neotoma#2042/#2127: filter accepted, ignored, success returned.
                return {"entities": self.corpus[: payload.get("limit", 100)]}
            matched = [
                e
                for e in self.corpus
                if all(
                    str(e["snapshot"].get(f)) == str(spec["value"])
                    for f, spec in sf.items()
                )
            ]
            return {"entities": matched[: payload.get("limit", 100)]}

        ordered = self.corpus
        if payload.get("sort_by") == "last_observation_at" and payload.get(
            "sort_order"
        ) == "desc":
            ordered = list(reversed(self.corpus))
        offset = payload.get("offset", 0)
        return {"entities": ordered[offset : offset + payload.get("limit", 100)]}


@pytest.mark.asyncio
async def test_issue_spec_store_load_finds_entity_outside_unsorted_first_200():
    """DoD regression: target sits past an unsorted first-200 window."""
    decoys = [_spec_entity(n) for n in range(1, 301)]
    store = _FakeSpecStore(
        decoys + [_spec_entity(TARGET)], supports_snapshot_filters=False
    )

    state = await store.load(REPO, TARGET, TITLE)

    assert state.entity_id == f"ent_spec{TARGET:020d}", (
        "a recent spec must be reachable, else the lens creates a duplicate"
    )
    assert state.sections.get("pm_section") == "PM section body"
    assert state.sequence_state == ["pm", "arch"]


@pytest.mark.asyncio
async def test_issue_spec_store_load_query_is_filtered_not_unbounded_scan():
    """DoD: the first query is a filtered identity lookup, not a 200-row scan."""
    store = _FakeSpecStore([_spec_entity(TARGET)], supports_snapshot_filters=True)
    await store.load(REPO, TARGET, TITLE)

    first = store.queries[0]
    assert first.get("snapshot_filters"), "first query must be server-side filtered"
    assert first["limit"] <= 10
    assert first["limit"] != SCAN_PAGE_SIZE, "must not be the old unfiltered scan"


@pytest.mark.asyncio
async def test_issue_spec_absent_returns_empty_state_for_create():
    """A genuinely absent spec still degrades to an empty state (create path)."""
    store = _FakeSpecStore(
        [_spec_entity(n) for n in range(1, 20)], supports_snapshot_filters=True
    )

    state = await store.load(REPO, TARGET, TITLE)

    assert state.entity_id == ""
    assert state.sections == {}
    assert state.title == TITLE


@pytest.mark.asyncio
async def test_issue_spec_targeted_hit_is_reverified():
    """A wrong-entity hit must not be adopted — that would CORRECT the wrong spec."""
    wrong = [_spec_entity(9999)]

    class _WrongHit(_FakeSpecStore):
        async def _post(self, path: str, payload: dict):  # type: ignore[override]
            self.queries.append(payload)
            return {"entities": wrong}

    store = _WrongHit(wrong, supports_snapshot_filters=False)
    state = await store.load(REPO, TARGET, TITLE)

    assert state.entity_id == "", "must not adopt a spec that fails _matches"
