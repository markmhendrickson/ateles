"""Who owns a task: the one recognizer for "an interactive session owns this".

Interactive sessions (a chat, a coordinator, a subagent) own many tasks, and no
dispatcher can spawn one. A task naming a session as its owner is therefore
neither unroutable nor a candidate for a guessed route: its owner session
writes its own state. The dispatcher's router and the delivery check must give
the same answer, so the rule lives here once rather than once per reader.

Stdlib only; no operator-specific literal. A session is recognised by generic
vocabulary alone.
"""

from __future__ import annotations

import re
from collections.abc import Callable


def _required_profile_fields(value: object, expected: dict) -> bool:
    """Require this consumer's evidence, including explicit null end markers."""
    return isinstance(value, dict) and all(
        key in value and type(value[key]) is type(wanted) and value[key] == wanted
        for key, wanted in expected.items()
    )


def _profile_scope_compatible(response: dict) -> bool:
    """A current-contract identity needs affirmative, nonforeign scope proof."""
    if any(response.get(key) is not None for key in ("error", "error_code", "errors", "detail")):
        return False
    if "success" in response and response["success"] is not True:
        return False
    if "read_contract" not in response:
        return True  # compatibility with the earlier counted HTTP envelope
    contract = response["read_contract"]
    return (
        _required_profile_fields(contract, {"version": "1", "surface": "entity_collection"})
        and _required_profile_fields(contract.get("applied_scope"), {
            "owner": "authenticated", "entity_types": ["operator_profile"],
            "include_merged": False, "include_deleted": False,
            "include_snapshots": True, "predicates": [],
        })
        and _required_profile_fields(contract.get("request_options"), {"ignored": []})
    )


def _complete_current_profile_contract(response: dict, count: int) -> bool:
    """The complete v1 negative proof, as constructed by the current producer.

    A sparse present contract is never the counted-legacy branch. Required
    scope, mode, population counts and explicit exhaustion/continuation must
    agree as a whole. Ordering, timestamps and common_view are not this proof.
    """
    contract = response["read_contract"]
    limit = response.get("limit")
    if type(limit) is not int or not 0 <= count < limit:
        return False
    if not _profile_scope_compatible(response):
        return False
    coverage = contract.get("coverage")
    return (
        _required_profile_fields(response, {"total": count, "offset": 0})
        and _required_profile_fields(contract.get("mode"), {"actual": "structured", "fallback_reason": None})
        and _required_profile_fields(contract["applied_scope"].get("pagination"), {
            "kind": "keyset", "limit": limit, "offset": 0,
        })
        and _required_profile_fields(coverage, {
            "kind": "predicate_population", "state": "complete", "reasons": [],
            "returned_count": count, "page_exhausted": True, "scope_exhausted": True,
        })
        and _required_profile_fields(coverage.get("total"), {"value": count, "relation": "exact", "unit": "entities"})
        and _required_profile_fields(coverage.get("continuation"), {
            "kind": "none", "next_cursor": None, "next_offset": None,
        })
    )


def _profile_page_problem(response: dict, count: int) -> str | None:
    """Only a coherent complete read can disprove an unmatched assignee.

    Counted legacy envelopes remain supported. When the current API includes
    its read contract, its explicit uncertainty and scope-exhaustion markers
    govern; separate-acquisition/read-interval coherence is supported.
    """
    total = response.get("total")
    if type(total) is not int or total < 0:
        return "malformed_operator_profiles"
    if total != count:
        return "partial_operator_profiles"
    for key in ("next_cursor", "next_offset"):
        if key in response and response[key] is not None:
            return "partial_operator_profiles"
    for key, expected in (("complete", True), ("has_more", False), ("truncated", False)):
        if key in response and response[key] is not expected:
            return "partial_operator_profiles"
    if "offset" in response and (type(response["offset"]) is not int or response["offset"] != 0):
        return "partial_operator_profiles"
    if "limit" in response and (type(response["limit"]) is not int or response["limit"] < max(1, count)):
        return "malformed_operator_profiles"
    if "read_contract" not in response:
        return None
    return None if _complete_current_profile_contract(response, count) else "partial_operator_profiles"


def operator_names(requester: Callable) -> tuple[frozenset[str], str | None]:
    """Observed valid identities, plus any uncertainty about negative matches.

    A returned name is positive evidence even when collection exhaustion is
    unknown. An unmatched name proves absence only when the reason is None
    (or no_operator_profile for a complete empty collection). Malformed rows
    cannot supply identities, and their uncertainty survives other valid rows.
    """
    try:
        response = requester(
            "POST", "/entities/query",
            {"entity_type": "operator_profile", "include_snapshots": True, "limit": 5},
        )
        if not isinstance(response, dict) or not _profile_scope_compatible(response):
            return frozenset(), "malformed_operator_profiles"
        rows = response.get("entities")
        if not isinstance(rows, list):
            return frozenset(), "malformed_operator_profiles"
        reason = _profile_page_problem(response, len(rows))
        names: set[str] = set()
        ids: set[str] = set()
        for row in rows:
            if (not isinstance(row, dict)
                    or ("entity_type" in row and row["entity_type"] != "operator_profile")):
                reason = "malformed_operator_profiles"
                continue
            if "entity_id" in row:
                entity_id = row["entity_id"]
                if (not isinstance(entity_id, str)
                        or not re.fullmatch(r"ent_[A-Za-z0-9_]+", entity_id) or entity_id in ids):
                    reason = "malformed_operator_profiles"
                    continue
                ids.add(entity_id)
            snapshot = row.get("snapshot", row)
            if not isinstance(snapshot, dict):
                reason = "malformed_operator_profiles"
                continue
            fields = [snapshot[field] for field in ("first_name", "display_name", "handle") if field in snapshot]
            if any(not isinstance(value, str) for value in fields):
                reason = "malformed_operator_profiles"
                continue
            row_names = {value.strip().lower() for value in fields if value.strip()}
            if not row_names:
                reason = "operator_profile_has_no_names"
                continue
            names.update(row_names)
    except Exception as exc:  # noqa: BLE001 - category only, never raw text
        return frozenset(), type(exc).__name__
    return frozenset(names), reason or (None if rows else "no_operator_profile")

# Values that encode "nobody owns this" as a STRING rather than as absence.
# They are worse than an empty field: `bool(assigned_to)` is True for them, so
# they satisfy an owner check (`readiness.assess_readiness(has_owner=...)`) and
# an `eq` filter without naming an owner any dispatcher can resolve.
# `canonical_assignee` maps them to None so absence is spelled exactly one way.
# This is the single source; routing.py imports it.
SENTINEL_ASSIGNEES = frozenset({"unassigned", "none", "nobody", "tbd", "n/a", "-"})

# AAuth subjects are written `<agent>@ateles-swarm` (lib/daemon_runtime/
# aauth_signer.py builds them as f"{name}@ateles-swarm"). The local part IS the
# agent name by construction, so an assignee carrying the suffix names the same
# owner as the bare form and must resolve identically.
_AAUTH_SUFFIXES = ("@ateles-swarm", "@ateles")

# The dispatcher itself. A task assigned to it names no owner, because the
# dispatcher is what is asking who owns the task.
_DISPATCHER_ASSIGNEE = "apis"

_SESSION_MARKERS = frozenset(
    {"session", "subagent", "codex", "claude", "chat", "coordinator"}
)
# `root` names a session only together with an id; bare `root` is a word.
_ID_PREFIXES = _SESSION_MARKERS | {"root"}
# Words a server-side `contains` filter can search on to narrow task rows to
# the ones this recognizer might accept. Invariant: every value
# `names_a_session` accepts contains one of these words, case-insensitively, as
# a substring. It holds because each accepting rule needs a marker, `root` with
# an id, `subagent`, `codex` or `session`, all of which are in `_ID_PREFIXES`.
# Derived, not a second list, so a new marker widens the search automatically.
SESSION_WORDS = tuple(sorted(_ID_PREFIXES))
_HEX_ID = re.compile(r"[0-9a-f]{6,}")
# A marker run directly into an id (`chat0a1b2c3d`, `coordinator0a1b...`).
_PREFIX_RUN_INTO_ID = re.compile(
    r"(?:" + "|".join(sorted(_ID_PREFIXES)) + r")([0-9a-f]{6,})"
)
# Delimiters are anything non-alphanumeric. Underscore is a delimiter, which
# `\b` would not make it, so tokens are split explicitly.
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return [t for t in _TOKEN_SPLIT.split(text) if t]


def _is_id(text: str) -> bool:
    # A digit keeps ordinary words made of a-f (`facade`, `decade`) from reading
    # as an id.
    return bool(_HEX_ID.fullmatch(text)) and any(c.isdigit() for c in text)


def _prefix_with_id(tokens: list[str]) -> bool:
    for i, token in enumerate(tokens):
        run = _PREFIX_RUN_INTO_ID.fullmatch(token)
        if run and _is_id(run.group(1)):
            return True
        if token in _ID_PREFIXES and i + 1 < len(tokens) and _is_id(tokens[i + 1]):
            return True
    return False


def names_a_session(value: object) -> bool:
    """True when ``value`` names an interactive session as the owner.

    A session verdict silences the dispatcher's only escalation for a task, so
    one common word is not enough: `Claude` and `Chat` are in people's names,
    `Session Bank` is a vendor, and `Marketing coordinator` is a job title. All
    of those must stay unroutable and reported. One of these is required:

    - the value leads with `codex`, or contains `subagent`;
    - `session` is the head noun: the last word before any parenthetical
      (`ateles session`, but not `session notes`);
    - two different markers (`Claude Code session`, `coordinator chat`);
    - a marker or `root` immediately followed by an id (`Foundation coordinator
      0a1b2c3d`, `chat0a1b2c3d`);
    - a space-free identifier starting with `session` (`session-email-review`).

    This reads free text, so it can be wrong in both directions, and word order
    matters: `Session Bank` is reported, while `Bank session`, `Training
    session` and `Codex Smith` read as sessions. That is accepted because the
    effect of a wrong yes is limited: nothing is spawned and no status is
    written, but the task is not reported as unroutable.

    Only the first clause (text before the first ``;``) counts: the owner is
    named first, and what follows is commentary that may mention a session
    without the session owning anything. Non-strings and empty values are
    False. A value this misses is reported as unroutable, which is the safe way
    to be wrong.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    first = value.split(";", 1)[0].lower().strip()
    tokens = _tokens(first)
    if not tokens:
        return False
    if tokens[0] == "codex" or "subagent" in tokens:
        return True
    head = _tokens(first.split("(", 1)[0])
    if head and head[-1] == "session":
        return True
    if len(_SESSION_MARKERS.intersection(tokens)) >= 2:
        return True
    if _prefix_with_id(tokens):
        return True
    return tokens[0] == "session" and not any(c.isspace() for c in first)


def canonical_assignee(assigned_to: object) -> str | None:
    """
    Reduce an ``assigned_to`` value to the one form the dispatcher resolves.

    The canonical form is not a preference: it is whatever the router's
    ``ASSIGNED_TO_ROUTES`` is keyed by, a bare, lowercase, stripped agent name.
    This normalizes the three mechanical variations observed in stored tasks
    (surrounding whitespace, capitalization, and the AAuth-subject suffix) and
    maps the "unassigned" sentinel family to ``None`` so absence has a single
    spelling.

    Returns ``None`` for absence, sentinels and non-strings, otherwise the
    reduced string. A value that is not a known agent is returned reduced but
    unchanged: this function normalizes *form*, and never invents or guesses an
    owner (a human name, or prose, is left for the caller to treat as
    unroutable).
    """
    if not isinstance(assigned_to, str):
        return None
    key = assigned_to.strip().lower()
    # Strip to a fixed point, and test for a sentinel only afterwards. A second
    # reduction must change nothing: the dispatcher reduces before asking the
    # router, which reduces again, while the delivery check reduces once.
    while suffix := next((s for s in _AAUTH_SUFFIXES if key.endswith(s)), None):
        key = key[: -len(suffix)].strip()
    if not key or key in SENTINEL_ASSIGNEES:
        return None
    return key


def deciding_assignee(assigned_to: object) -> str | None:
    """The canonical assignee when it decides ownership, else ``None``.

    ``None`` means ``owner`` is consulted: absence, a sentinel, or the dispatcher
    itself. Every reader of task ownership takes this answer from here, so the
    router and the delivery check cannot disagree about whether ``owner`` counts.
    """
    key = canonical_assignee(assigned_to)
    return None if key == _DISPATCHER_ASSIGNEE else key


def session_owned(assigned_to: object, owner: object) -> bool:
    """True when a session owns the task.

    An assignee that decides (see ``deciding_assignee``) decides alone: naming
    anyone else means ``owner`` is not consulted. Otherwise ``owner`` decides.
    The reduced form is what is read, as the router reads it.
    """
    key = deciding_assignee(assigned_to)
    if key:
        return names_a_session(key)
    return names_a_session(owner)
