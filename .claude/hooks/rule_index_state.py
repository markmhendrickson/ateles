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
    the same `state` dict for convenient chaining into `save_state`."""
    sig = row_signature(rows)
    state[STATE_KEY] = {"hash": hash_signature(sig), "rows": sig}
    return state


def last_delivered(state: dict) -> tuple[str | None, dict[str, str]]:
    """(hash, rows-signature) last recorded by either hook, or (None, {})
    when this session has never recorded a delivery."""
    delivered = state.get(STATE_KEY) or {}
    return delivered.get("hash"), (delivered.get("rows") or {})
