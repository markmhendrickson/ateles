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
