"""An issue entity must be found under ANY of the four number spellings.

Issue entities carry the GitHub number under `github_number` (61.3%),
`issue_number` (8.7%), `github_issue_number` (1.8%) or `number` (1.2%) —
measured against prod on 2026-09-02 over all 4,434 rows. A reader keyed on one
spelling misses 8.5% of the numbered rows, and because every gate/dispatch
caller in this package fails CLOSED, the miss is a silent permanent block, not
an error. ateles#390 was exactly this: a `github_number` filter returned 0 rows
for an entity stored under `number`, reported as "no Neotoma issue entity".

The parametrisation over all four field names is the point of this file. A test
that exercised only the canonical name would pass while the other three stayed
invisible — which IS the bug. Each test below therefore asserts the behaviour
for every spelling, so reverting the fix for any one of them fails here.

Run: pytest execution/daemons/apis/test_issue_number_fields.py -v
"""

from __future__ import annotations

import pytest

from gate_waive import IssueGateStore
from lib.issue_number import (
    ISSUE_NUMBER_FIELDS,
    extract_issue_number,
    issue_matches,
    number_filter_candidates,
)

# Every spelling the corpus actually uses, with its measured prod share.
ALL_FOUR = [
    ("github_number", "61.3% of rows — canonical, declared in the composite identity"),
    ("issue_number", "8.7% of rows — written by the triage/spec paths"),
    ("github_issue_number", "1.8% of rows"),
    ("number", "1.2% of rows — the spelling that caused ateles#390"),
]


def test_all_four_spellings_are_covered():
    """Guards the field list itself against someone trimming it back."""
    assert set(ISSUE_NUMBER_FIELDS) == {name for name, _ in ALL_FOUR}
    # Canonical must lead, so the 61.3% case matches on the first attempt.
    assert ISSUE_NUMBER_FIELDS[0] == "github_number"


@pytest.mark.parametrize(("field", "why"), ALL_FOUR)
def test_extract_finds_number_under_every_field(field, why):
    """The number is recovered whichever of the four fields holds it."""
    assert extract_issue_number({field: 682, "repo": "o/r"}) == 682, why


@pytest.mark.parametrize(("field", "why"), ALL_FOUR)
def test_extract_tolerates_string_values(field, why):
    """136 `github_number` values in prod are strings, not ints.

    Server-side filters coerce these, but in-process comparisons do not:
    `snap.get(...) == 682` is False against `"682"`. Normalise on the way out.
    """
    assert extract_issue_number({field: "682", "repo": "o/r"}) == 682, why


@pytest.mark.parametrize(("field", "why"), ALL_FOUR)
def test_matcher_accepts_every_field_and_both_repo_spellings(field, why):
    """`repo` and `repository` both identify the repo; neither may be required."""
    for repo_field in ("repo", "repository"):
        assert issue_matches({field: 682, repo_field: "o/r"}, "o/r", 682), (
            f"{field} + {repo_field}: {why}"
        )


def test_extract_returns_none_for_local_issues():
    """1,457 rows are local/non-GitHub and legitimately carry no number.

    These must read as None, not as a spurious 0 — a 0 would be indistinguishable
    from a real issue number in a filter and would collide across repos.
    """
    assert extract_issue_number({"local_issue_id": "local:o/r:abc", "repo": "o/r"}) is None


def test_malformed_value_does_not_mask_a_later_good_field():
    """One empty/garbage field must not shadow a valid number after it."""
    assert extract_issue_number({"github_number": "", "issue_number": 682}) == 682


def test_filter_candidates_cover_every_field_pair():
    """The server-side filters must span all four names x both repo spellings."""
    cands = number_filter_candidates(682, "o/r")
    covered = {
        (nf, rf)
        for c in cands
        for nf in c
        if nf not in ("repo", "repository")
        for rf in c
        if rf in ("repo", "repository")
    }
    assert covered == {(n, r) for n, _ in ALL_FOUR for r in ("repo", "repository")}


# --- End-to-end through the real gate loader --------------------------------
# The unit tests above pin the helper. These drive `IssueGateStore.load`, the
# actual fail-closed consumer, so a regression in the wiring (not just the
# helper) is caught too.


def _entity(field: str, number: int, repo_field: str, repo: str) -> dict:
    """An issue entity carrying its number under exactly ONE of the spellings."""
    return {
        "entity_id": f"ent_{field}_{number}",
        "snapshot": {
            field: number,
            repo_field: repo,
            "gate_status": {"pm": "signed_off"},
        },
    }


class _OnlyMatchingFilter:
    """A fake prod: returns the entity ONLY when the query filter actually matches it.

    This is what makes the test meaningful. A stub that returned the entity for
    any query would pass even with a reader keyed on one field — it would never
    exercise whether the right filter was sent. Here, a `load` that does not try
    the entity's own spelling gets empty pages and reports `found == False`,
    exactly as prod does.
    """

    def __init__(self, entity: dict) -> None:
        self.entity = entity
        self.snapshot = entity["snapshot"]

    async def __call__(self, path: str, payload: dict):  # noqa: ANN001
        filters = payload.get("snapshot_filters")
        if filters is None:
            # The unfiltered paged fallback scan.
            return {"entities": [self.entity], "next_cursor": ""}
        for field, clause in filters.items():
            expected = clause.get("value") if isinstance(clause, dict) else clause
            actual = self.snapshot.get(field)
            if actual is None or str(actual) != str(expected):
                return {"entities": [], "total": 0}
        return {"entities": [self.entity], "total": 1}


@pytest.mark.parametrize(("field", "why"), ALL_FOUR)
@pytest.mark.parametrize("repo_field", ["repo", "repository"])
@pytest.mark.asyncio
async def test_gate_load_finds_entity_under_every_spelling(
    field, why, repo_field, monkeypatch
):
    """`IssueGateStore.load` resolves the entity for all four names.

    Reverting the widening in `gate_waive.load` to a single `github_number` +
    `repo` filter makes every case here fail except that one pair.
    """
    store = IssueGateStore("http://x", "tok")
    monkeypatch.setattr(
        store, "_post", _OnlyMatchingFilter(_entity(field, 682, repo_field, "o/r"))
    )

    state = await store.load("o/r", 682)

    assert state.found, f"gate lookup missed an entity stored under {field}/{repo_field}: {why}"
    assert state.entity_id == f"ent_{field}_682"
    assert state.gate_status.get("pm") == "signed_off"


@pytest.mark.parametrize(("field", "why"), ALL_FOUR)
@pytest.mark.asyncio
async def test_gate_load_matches_string_typed_numbers(field, why, monkeypatch):
    """A number stored as a string must still resolve the gate."""
    entity = _entity(field, 682, "repo", "o/r")
    entity["snapshot"][field] = "682"
    store = IssueGateStore("http://x", "tok")
    monkeypatch.setattr(store, "_post", _OnlyMatchingFilter(entity))

    state = await store.load("o/r", 682)

    assert state.found, f"string-typed {field} did not resolve: {why}"
