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
import os
import time

STATE_KEY = "rule_index_delivered"  # shared per-session state key
LIFECYCLE_RECEIPT_KEY = "rule_index_lifecycle_receipt"
_DUPLICATE_WINDOW_SECONDS = 60

# The fields whose content defines a row's delivered identity. Anything
# outside this set (status, rationale, canonical_name, ...) does not affect
# what a session is told, so a change to it must not trigger a re-delivery.
_CONTENT_FIELDS = (
    "rule",
    "title",
    "applies_when",
    "scope",
    "agent_sub",
    "rule_kind",
    "domain",
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


def record_delivered_subset(
    state: dict, rows: list[dict], delivered_ids: set[str]
) -> dict:
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


def delivered_subset_hash(rows: list[dict], delivered_ids: set[str]) -> str:
    """Content hash for exactly the rows the renderer says it emitted."""
    return hash_signature(
        row_signature([r for r in rows if _entity_id(r) in delivered_ids])
    )


def lifecycle_revision(event: dict) -> str:
    """Hash the documented fields that identify one Codex lifecycle point.

    SessionStart has no hook-call id.  The start source plus a metadata-only
    transcript revision distinguishes startup/resume/compact occurrences;
    SubagentStart additionally has stable turn and agent ids.  Paths are
    hashed, never persisted in clear text.
    """
    transcript = event.get("transcript_path")
    transcript_revision: tuple[int, int] | None = None
    if transcript:
        try:
            stat = os.stat(transcript)
            transcript_revision = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            pass
    fields = {
        "hook_event_name": event.get("hook_event_name"),
        "source": event.get("source"),
        "turn_id": event.get("turn_id"),
        "agent_id": event.get("agent_id"),
        "agent_type": event.get("agent_type"),
        "transcript_path_hash": (
            hashlib.sha256(str(transcript).encode()).hexdigest() if transcript else None
        ),
        "transcript_revision": transcript_revision,
    }
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def lifecycle_delivery_is_duplicate(
    state: dict, event: dict, content_hash: str, *, now: float | None = None
) -> bool:
    """Whether this exact lifecycle revision/content was just delivered.

    The bounded window collapses concurrently launched project+user handlers
    without permanently consuming a legitimate later resume whose documented
    input happens to be byte-for-byte identical.
    """
    receipt = state.get(LIFECYCLE_RECEIPT_KEY) or {}
    observed_at = time.time() if now is None else now
    try:
        age = observed_at - float(receipt.get("recorded_at", 0))
    except (TypeError, ValueError):
        return False
    return (
        0 <= age <= _DUPLICATE_WINDOW_SECONDS
        and receipt.get("revision") == lifecycle_revision(event)
        and receipt.get("content_hash") == content_hash
    )


def record_lifecycle_delivery(
    state: dict, event: dict, content_hash: str, *, now: float | None = None
) -> dict:
    state[LIFECYCLE_RECEIPT_KEY] = {
        "revision": lifecycle_revision(event),
        "content_hash": content_hash,
        "recorded_at": time.time() if now is None else now,
    }
    return state


# NOTE (ateles#1323 follow-up, Falco security review, task
# ent_bd3fcf561449b6ebbabc36ca): an earlier revision of this module offered
# `rendered_entity_ids(text)`, a regex scan for a trailing `[entity_id]`
# bracket, as the way to decide "what did the session actually see." It was
# removed. `policy_skill_renderer.py` guarantees a mandatory row's `body`
# (the raw `agent_policy.rule` text) is NEVER sanitized for index rendering,
# and this repo's own rule bodies routinely cite other entity ids in prose —
# so a bracket inside one row's OWN body reads to a text scanner exactly
# like a different row's real index line, marking that other row delivered
# even when it was the one actually omitted for budget. Falco reproduced
# this directly. The fix is structural, not textual: both
# `policy_skill_renderer.render_index_text_with_ids` and
# `session_rule_delivery.py`'s own `_render_delta` now return the exact set
# of entity ids whose OWN line/block was included in the list the render
# loop kept — built from the `PolicySkill` objects themselves, never by
# reading a row's rendered text back out. Use `record_delivered_subset`
# with that structurally-produced id set; do not reintroduce a text scan.
