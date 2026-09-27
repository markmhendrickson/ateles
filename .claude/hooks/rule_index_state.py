#!/usr/bin/env python3
"""Shared signature/hash helpers for the rule-index delivery pair
(`session_rule_index.py`, the SessionStart delivery, and
`session_rule_delivery.py`, the UserPromptSubmit re-delivery — ateles#1261
follow-up, audit ent_b66293f0dcc8c887d4fdbeae).

ONE definition of "what did we deliver" so the two hooks can never silently
diverge on it (CLAUDE.md: "extend the mechanism that already generalizes; do
not build a parallel one" — the exact failure mode a hand-copied predicate
produces elsewhere in this repo, per `agent_loader.policy_binds_agent`'s own
docstring).

Identity for change-detection is a content hash of the fields that actually
reach a rendered rule (`rule`, `title`, `applies_when`, `scope`, `agent_sub`,
`rule_kind`, `domain`), keyed by entity id — NOT a server-side
`last_observation_at` timestamp. `policy_skill_renderer.fetch_active_policy_rows`
returns rows already run through `agent_loader.unwrap_policy_entities`, which
flattens the entity envelope down to the bare snapshot dict (stamping only
`_entity_id` onto it) and discards the outer envelope entirely — so
`last_observation_at` is not available to a caller of the shared reader
without a second, parallel fetch that reads the envelope before it is
unwrapped. Reusing the EXACT shared reader (rather than adding a second
transport just to recover one field) is the point of this whole design, so
change-detection is defined on the snapshot content itself: a `correct()`
call is the only way an `agent_policy` row's content changes, and any
content change is exactly what this must catch, so hashing the content
directly is both simpler and more literal than hashing a timestamp that is a
proxy for it.

Stdlib-only (hashlib, json). No Neotoma access here — callers already have
the rows from `fetch_active_policy_rows`.
"""
from __future__ import annotations

import hashlib
import json
import re

STATE_KEY = "rule_index_delivered"  # shared per-session state key

# The fields whose content defines a row's delivered identity. Anything
# outside this set (status, rationale, canonical_name, ...) does not affect
# what a session is told, so a change to it must not trigger a re-delivery.
_CONTENT_FIELDS = (
    "rule", "title", "applies_when", "scope", "agent_sub", "rule_kind", "domain",
)


def _entity_id(row: dict) -> str:
    return str(row.get("_entity_id") or row.get("entity_id") or "")


def _content_fingerprint(row: dict) -> str:
    values = [str(row.get(f) or "") for f in _CONTENT_FIELDS]
    blob = json.dumps(values, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def row_signature(rows: list[dict]) -> dict[str, str]:
    """entity_id -> content fingerprint, for a list of unwrapped snapshot
    dicts (the shape `policy_skill_renderer.fetch_active_policy_rows` /
    `agent_loader.unwrap_policy_entities` returns: a flat dict with
    `_entity_id` stamped on, not `{"entity_id": ..., "snapshot": {...}}`)."""
    sig: dict[str, str] = {}
    for r in rows:
        eid = _entity_id(r)
        if not eid:
            continue
        sig[eid] = _content_fingerprint(r)
    return sig


def hash_signature(sig: dict[str, str]) -> str:
    """Stable sha256 over the sorted (entity_id, fingerprint) pairs —
    order-independent, so two fetches of the same live set hash identically
    regardless of what order Neotoma happened to return rows in."""
    blob = json.dumps(sorted(sig.items()), separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def record_delivery(state: dict, rows: list[dict]) -> dict:
    """Mutates `state[STATE_KEY]` to the signature/hash of `rows` and returns
    the same `state` dict for convenient chaining into `save_state`.

    Callers that only rendered a SUBSET of `rows` this turn (a budget-bound
    delta, or the no-baseline full-index path when even that degrades to
    tier C) must pre-filter `rows` down to that subset before calling this —
    see `record_delivered_subset` for the common case of filtering by a set
    of entity ids. Recording a row here that was not actually rendered with
    at least a one-line mention is the exact defect this module exists to
    prevent (task ent_3f5bc7138628cf5b4116d569).
    """
    sig = row_signature(rows)
    state[STATE_KEY] = {"hash": hash_signature(sig), "rows": sig}
    return state


def record_delivered_subset(state: dict, rows: list[dict], delivered_ids: set[str]) -> dict:
    """Like `record_delivery`, but only for the rows in `rows` whose entity
    id is in `delivered_ids` — the rest are simply absent from the recorded
    signature, so a later prompt (or the next SessionStart) treats them as
    still-undelivered and retries them, rather than permanently exempting a
    row that was fetched but never actually rendered to the session."""
    subset = [r for r in rows if _entity_id(r) in delivered_ids]
    return record_delivery(state, subset)


def last_delivered(state: dict) -> tuple[str | None, dict[str, str]]:
    """(hash, rows-signature) last recorded by either hook, or (None, {})
    when this session has never recorded a delivery."""
    delivered = state.get(STATE_KEY) or {}
    return delivered.get("hash"), (delivered.get("rows") or {})


# Every line-producing helper in policy_skill_renderer.py (preamble lines,
# tier A/A2/B conditional lines, tier C's KEPT lines, and this hook's own
# mandatory-full-block / advisory-summary-line) ends the line it renders for
# a row with a bracketed entity id: "...  [ent_xxx]". Tier C's one-line
# omitted-count statement ("N more ... rule(s) omitted for space. Query
# Neotoma directly: ...") is the sole exception — it names a COUNT, never an
# id, precisely because it speaks for rows that got no line of their own.
# Scanning rendered text for this bracket shape is therefore an exact
# read of "which rows actually reached the session as at least one line,"
# without duplicating or reaching into the renderer's tier-selection logic
# (CLAUDE.md: "extend the mechanism that already generalizes; do not build a
# parallel one" — here that means reading the renderer's OUTPUT contract
# rather than adding a second return channel to the renderer itself).
_ENTITY_ID_IN_LINE = re.compile(r"\[([^\[\]\s]+)\]\s*$")


def rendered_entity_ids(text: str) -> set[str]:
    """Entity ids that appear as a trailing bracketed token on some line of
    `text` — i.e. rows that were actually rendered with at least a one-line
    index entry, as opposed to a row silently dropped by tier C and spoken
    for only by its omitted-count line.

    Used to decide what is safe to mark delivered: recording a row as
    delivered because it was in the CANDIDATE set, when the rendered text
    that reached the session never actually mentioned it, is exactly the
    defect this module exists to prevent (a rule marked delivered that the
    session never saw even a trigger line for).
    """
    ids: set[str] = set()
    for line in text.splitlines():
        m = _ENTITY_ID_IN_LINE.search(line.strip())
        if m:
            ids.add(m.group(1))
    return ids
