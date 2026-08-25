"""
execution/daemons/apis/entity_lookup.py — resolve ONE entity by identity from
Neotoma's ``entities/query``, without an unbounded client-side scan (ateles#492).

WHY THIS MODULE EXISTS
----------------------
Two daemon stores resolved an entity by fetching an unfiltered, UNSORTED page
and scanning it client-side:

  * ``IssueGateStore.load``  — ``issue``      entities, ``limit: 500``
  * ``IssueSpecStore.load``  — ``issue_spec`` entities, ``limit: 200``

The default order is ``entity_id`` ascending, not recency, so once a corpus
exceeds the page size the window becomes an arbitrary slice that excludes
recently-created rows. With ~4,039 ``issue`` and 317 ``issue_spec`` entities in
prod, BOTH were past their page size.

The two failure shapes differ, and the spec one is worse:

  * gate lookup miss  → ``found == False`` → ``_gates_are_green`` fails CLOSED.
    Honest, but no new issue could ever reach implementation (21 logged
    occurrences across both repos).
  * spec lookup miss  → empty ``SpecState`` → each lens CREATES a spec entity
    instead of CORRECTING the existing one, silently duplicating it.

THE LOOKUP CONTRACT
-------------------
Two steps, and the second is not redundancy for its own sake:

1. A targeted ``snapshot_filters`` query. This is the CANONICAL wired filter
   path — it compiles to real ``snapshot->>field`` predicates in Neotoma's query
   builder. It is deliberately NOT the flat ``filters`` map (neotoma#2042) nor
   the ``entity_ids`` array (neotoma#2127), both of which are accepted and then
   silently ignored while returning a success.
2. A recency-sorted (``last_observation_at desc``) paged scan when the targeted
   query yields nothing.

Neotoma has four open issues in which scoping arguments are dropped with a
success response (#2042, #2127, #2156, #2205 — collected in neotoma#2213), so
this code must not DEPEND on the targeted path working, only benefit when it
does.

Two properties carry the correctness:

  * Every targeted hit is RE-VERIFIED client-side by the caller's ``matches``
    predicate before it is trusted. A filter that returns an unrelated entity
    (the neotoma#2127 behaviour) would otherwise have the gate check read
    another issue's ``gate_status`` — strictly worse than an honest not-found.
  * Exhausting the scan bound LOGS that the listing was not exhausted, so
    "not found in the scanned window" stays distinguishable from "no such
    entity". Conflating those is what #492 reports sent its reporter chasing a
    missing entity that was there all along.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

log = logging.getLogger("apis.entity_lookup")

# Recency-scan bounds for the fallback lookup. 8 x 200 = 1,600 most-recently
# observed entities — sized to cover the active window comfortably against the
# largest corpus in play (~4,039 `issue` entities, the overwhelming majority
# long-settled) while keeping a pathological miss bounded. Exceeding the bound
# LOGS rather than returning a silent "not found".
SCAN_PAGE_SIZE = 200
SCAN_MAX_PAGES = 8

# Poster signature: (path, payload) -> parsed JSON dict or None.
Poster = Callable[[str, dict], Awaitable[dict | None]]
# Identity predicate over an entity's flat snapshot field map.
Matcher = Callable[[dict], bool]


def unwrap_snapshot(entity: dict) -> dict:
    """Return the flat field map for *entity*, tolerating one nesting level.

    Prod responses sometimes nest the field map one level deeper
    (``{"snapshot": {"snapshot": {...}}}``); both shapes appear in the wild.
    """
    snap = entity.get("snapshot") or {}
    inner = snap.get("snapshot")
    if isinstance(inner, dict):
        return inner
    return snap


async def _load_targeted(
    post: Poster,
    entity_type: str,
    matches: Matcher,
    filter_combos: list[dict[str, str]],
    ref: str,
    presence_fields: tuple[str, ...],
) -> tuple[dict, dict] | None:
    """Resolve via server-side ``snapshot_filters``, re-verifying every hit.

    ``filter_combos`` is an ordered list of ``{field_name: value}`` maps to try,
    because prod entities carry duplicated spellings (``repo``/``repository``,
    ``issue_number``/``github_number``).

    Combos are abandoned early only on evidence that the FIELD NAMES are wrong
    for this corpus — a returned entity carrying none of ``presence_fields``.
    Deliberately NOT on "the page came back non-empty but nothing matched":
    under the neotoma#2127 behaviour every query returns unrelated entities, so
    treating that as a negative signal would skip the remaining combos,
    including one that would have worked. The cheap-looking early exit is the
    one that reintroduces the bug.
    """
    seen_presence = False
    for combo in filter_combos:
        data = await post(
            "entities/query",
            {
                "entity_type": entity_type,
                "limit": 10,
                "include_snapshots": True,
                "snapshot_filters": {
                    field: {"op": "eq", "value": str(value)}
                    for field, value in combo.items()
                },
            },
        )
        if not data:
            continue
        entities = data.get("entities") or []
        for entity in entities:
            snap = unwrap_snapshot(entity)
            if any(snap.get(key) is not None for key in presence_fields):
                seen_presence = True
            # Re-verify: never trust the server-side filter blindly.
            if matches(snap):
                return entity, snap
        if entities and not seen_presence:
            log.debug(
                "[apis.entity_lookup] %s: %s snapshots carry none of %s — "
                "skipping remaining targeted combos, falling back to scan",
                ref,
                entity_type,
                ", ".join(presence_fields),
            )
            return None
    return None


async def _load_by_scan(
    post: Poster,
    entity_type: str,
    matches: Matcher,
    ref: str,
) -> tuple[dict, dict] | None:
    """Fallback: page a RECENCY-SORTED listing and match client-side.

    Sorting by ``last_observation_at`` descending puts the entities a dispatch
    actually asks about at the front, and paging bounds the work without
    silently truncating.
    """
    for page in range(SCAN_MAX_PAGES):
        data = await post(
            "entities/query",
            {
                "entity_type": entity_type,
                "limit": SCAN_PAGE_SIZE,
                "offset": page * SCAN_PAGE_SIZE,
                "include_snapshots": True,
                "sort_by": "last_observation_at",
                "sort_order": "desc",
            },
        )
        if not data:
            return None
        entities = data.get("entities") or []
        for entity in entities:
            snap = unwrap_snapshot(entity)
            if matches(snap):
                return entity, snap
        # A short (or empty) page means the listing is exhausted.
        if len(entities) < SCAN_PAGE_SIZE:
            return None
    log.warning(
        "[apis.entity_lookup] %s: scanned %d %s entities (%d pages) without a "
        "match — the listing was NOT exhausted, so this is 'not found in the "
        "scanned window', not 'no such entity'",
        ref,
        SCAN_MAX_PAGES * SCAN_PAGE_SIZE,
        entity_type,
        SCAN_MAX_PAGES,
    )
    return None


async def resolve_entity(
    post: Poster,
    entity_type: str,
    matches: Matcher,
    filter_combos: list[dict[str, str]],
    ref: str,
    presence_fields: tuple[str, ...],
) -> tuple[dict, dict] | None:
    """Resolve one entity by identity: targeted filter, then recency scan.

    Returns ``(entity, snapshot_field_map)`` or ``None``. ``None`` means the
    entity was not found — check the log to tell "absent" from "outside the
    scanned window".
    """
    hit = await _load_targeted(
        post, entity_type, matches, filter_combos, ref, presence_fields
    )
    if hit is None:
        hit = await _load_by_scan(post, entity_type, matches, ref)
    return hit
