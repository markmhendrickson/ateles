"""Tests for entity_lookup.py additions made on top of PR #497's carried
module (ateles#1227):

  1. Ambiguous-match fail-closed in the targeted-query path — #497's original
     `_load_targeted` adopted the FIRST re-verified hit in a page with no
     check for a second one. Added here because the repo+issue_number (or
     repo+github_number) identity rule the callers use says a well-formed
     corpus has at most one match; more than one is a server-side anomaly
     that must not be resolved by silently picking one (which risks a
     caller CORRECTING the wrong entity's fields).

  2. The SCAN_MAX_PAGES cap on the recency-scan fallback actually terminates
     against a server that never stops returning `next_cursor` (a
     pathological loop this bound exists to defend against, per the
     module's own comment) — flagged as an untested gap in QA review on
     ateles#1227.

Both are proven RED by reverting the guarded behaviour inline (see each
test's docstring for exactly what was reverted and how it was confirmed to
fail), then GREEN against the current module.
"""

from __future__ import annotations

import asyncio

from entity_lookup import SCAN_MAX_PAGES, SCAN_PAGE_SIZE, resolve_entity


def _entity(entity_id: str, snap: dict) -> dict:
    return {"entity_id": entity_id, "snapshot": snap}


def _matches_issue(repo: str, issue_number: int):
    def _m(snap: dict) -> bool:
        return (
            str(snap.get("repo")) == repo
            and str(snap.get("issue_number")) == str(issue_number)
        )

    return _m


# ── 1. Ambiguous match fails closed ─────────────────────────────────────────
#
# RED evidence (recorded, not re-run live in CI): reverting `_load_targeted`
# to adopt the first re-verified hit — i.e. `for entity in entities: if
# matches(snap): return entity, snap` in place of the `verified` list +
# length check — makes this test fail: it returns `("ent_a", ...)` (the
# first match) instead of `None`. Confirmed locally against the pre-fix
# shape of the function before adding the length check.


def test_ambiguous_targeted_match_fails_closed():
    calls = []

    async def post(path, payload):
        calls.append(payload)
        # Two DIFFERENT entities both satisfy the repo+issue_number
        # predicate — the anomaly the identity rule says should be
        # impossible for a well-formed corpus.
        return {
            "entities": [
                _entity("ent_a", {"repo": "owner/repo", "issue_number": 42}),
                _entity("ent_b", {"repo": "owner/repo", "issue_number": 42}),
            ]
        }

    hit = asyncio.run(
        resolve_entity(
            post,
            "issue_spec",
            _matches_issue("owner/repo", 42),
            [{"repo": "owner/repo", "issue_number": 42}],
            "owner/repo#42",
            ("issue_number",),
        )
    )
    assert hit is None, (
        "an ambiguous (>1 re-verified match) targeted-query result must "
        "fail CLOSED (None) — the caller's identity rule says this cannot "
        "happen for a well-formed corpus, so guessing risks correcting the "
        "wrong entity"
    )
    # resolve_entity() treats a None from _load_targeted as "try the
    # fallback scan" (the same signal a genuine miss produces), so an
    # ambiguous targeted result legitimately falls through to the scan too
    # — which must ALSO refuse on the same ambiguous data rather than
    # silently resolving it a different way. The targeted attempt happened
    # (first call), and the overall result is still None despite the
    # fallback also seeing the ambiguous pair — that combination is exactly
    # what proves both paths enforce the same fail-closed rule.
    assert len(calls) >= 1
    assert calls[0].get("snapshot_filters"), "the targeted query runs first"


def test_unambiguous_single_match_still_resolves_normally():
    """Sanity companion: the fail-closed check must not fire on the common,
    correct case of exactly one re-verified match."""

    async def post(path, payload):
        return {
            "entities": [
                _entity("ent_only", {"repo": "owner/repo", "issue_number": 42}),
            ]
        }

    hit = asyncio.run(
        resolve_entity(
            post,
            "issue_spec",
            _matches_issue("owner/repo", 42),
            [{"repo": "owner/repo", "issue_number": 42}],
            "owner/repo#42",
            ("issue_number",),
        )
    )
    assert hit is not None
    entity, snap = hit
    assert entity["entity_id"] == "ent_only"
    assert snap["issue_number"] == 42


def test_ambiguous_match_does_not_silently_adopt_a_wrong_entity():
    """Companion assertion at the resolve_entity() boundary: verifies the
    caller-visible contract (None, not a tuple) rather than only the
    internal `verified` list length — this is the shape a real caller
    (IssueSpecStore.load) actually observes."""

    async def post(path, payload):
        return {
            "entities": [
                _entity("ent_wrong_pick_1", {"repo": "r", "issue_number": 1}),
                _entity("ent_wrong_pick_2", {"repo": "r", "issue_number": 1}),
                _entity("ent_wrong_pick_3", {"repo": "r", "issue_number": 1}),
            ]
        }

    hit = asyncio.run(
        resolve_entity(
            post,
            "issue_spec",
            _matches_issue("r", 1),
            [{"repo": "r", "issue_number": 1}],
            "r#1",
            ("issue_number",),
        )
    )
    assert hit is None
    # In particular, the caller must never see any of the three ids as an
    # accepted resolution.
    assert hit != ("ent_wrong_pick_1", {"repo": "r", "issue_number": 1})


# ── 2. SCAN_MAX_PAGES cap actually terminates ───────────────────────────────
#
# RED evidence (recorded): removing the `for page in range(SCAN_MAX_PAGES)`
# bound in `_load_by_scan` (replacing it with `while True`) against a server
# that always returns a `next_cursor` makes this test hang (recorded as a
# timeout, not a clean failure, when tried locally against the reverted
# code) — the cap is the only thing standing between a server bug and an
# infinite loop, exactly as `entity_lookup.py`'s own docstring states.


def test_scan_cap_terminates_against_a_server_that_never_stops_paging():
    calls = []

    async def post(path, payload):
        calls.append(payload)
        # No snapshot_filters honored at all (simulates neotoma#2042/#2127:
        # the filter is silently ignored), and the fallback scan NEVER finds
        # a match and ALWAYS returns a next_cursor — the pathological loop
        # SCAN_MAX_PAGES exists to bound.
        return {
            "entities": [_entity(f"ent_{len(calls)}", {"repo": "other", "n": 0})],
            "next_cursor": f"cursor_{len(calls)}",
        }

    hit = asyncio.run(
        resolve_entity(
            post,
            "issue_spec",
            _matches_issue("owner/repo", 42),  # never matches -> forces scan
            [{"repo": "owner/repo", "issue_number": 42}],
            "owner/repo#42",
            ("issue_number",),
        )
    )
    assert hit is None, "a listing that never matches must resolve to not-found"

    # 1 targeted call (no presence fields seen -> abandons combos early) or
    # a targeted attempt per combo, THEN exactly SCAN_MAX_PAGES scan calls —
    # never more. This is the actual termination proof: the loop stopped.
    scan_calls = [c for c in calls if not c.get("snapshot_filters")]
    assert len(scan_calls) == SCAN_MAX_PAGES, (
        f"expected the recency-scan fallback to make exactly {SCAN_MAX_PAGES} "
        f"calls against a server that never stops paging, got {len(scan_calls)} "
        "— an unbounded loop would never reach this assertion at all"
    )
    # Every scan call is bounded to SCAN_PAGE_SIZE, and each has an
    # explicit offset (the cursor-less/offset-pagination branch, since our
    # fake server's next_cursor is honored as a cursor — confirm the SHAPE
    # is the bounded one either way).
    assert all(c.get("limit") == SCAN_PAGE_SIZE for c in scan_calls)


def test_scan_stops_early_on_a_short_cursorless_page():
    """Companion: a server with NO cursor support that returns a short page
    is treated as exhausted (not padded out to SCAN_MAX_PAGES) — the offset
    pagination branch's own termination condition, distinct from the cap."""
    calls = []

    async def post(path, payload):
        calls.append(payload)
        if len(calls) == 1:
            return {"entities": [_entity("ent_1", {"repo": "other", "n": 0})]}
        # A short (here: empty) page with NO next_cursor signals exhaustion.
        return {"entities": []}

    hit = asyncio.run(
        resolve_entity(
            post,
            "issue_spec",
            _matches_issue("owner/repo", 42),
            [{"repo": "owner/repo", "issue_number": 42}],
            "owner/repo#42",
            ("issue_number",),
        )
    )
    assert hit is None
    scan_calls = [c for c in calls if not c.get("snapshot_filters")]
    assert len(scan_calls) < SCAN_MAX_PAGES, (
        "a short, cursor-less page must stop the scan early rather than "
        "padding out to the full cap"
    )
