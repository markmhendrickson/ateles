"""Who owns a task: the one recognizer for "an interactive session owns this".

Interactive sessions (a chat, a coordinator, a subagent) own many tasks, and no
dispatcher can spawn one. A task naming a session as its owner is therefore
neither unroutable nor a candidate for a guessed route: its owner session
writes its own state. Three readers need the same answer (the dispatcher's
router, the decision-queue classifier, the delivery check), so the rule lives
here once rather than as a regex per reader.

Stdlib only; no operator-specific literal. A session is recognised by generic
vocabulary alone.
"""

from __future__ import annotations

import re

# Values that encode "nobody owns this" as a STRING rather than as absence.
# They are worse than an empty field: `bool(assigned_to)` is True for them, so
# they satisfy an owner check (`readiness.assess_readiness(has_owner=...)`) and
# an `eq` filter without naming an owner any dispatcher can resolve.
# `routing.canonical_assignee` maps them to None so absence is spelled exactly
# one way. This is the single source; routing.py imports it.
SENTINEL_ASSIGNEES = frozenset({"unassigned", "none", "nobody", "tbd", "n/a", "-"})

_SESSION_MARKERS = frozenset(
    {"session", "subagent", "codex", "claude", "chat", "coordinator"}
)
# `root` names a session only together with an id; bare `root` is a word.
_ID_PREFIXES = _SESSION_MARKERS | {"root"}
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

    - the value leads with `codex`, or contains `subagent`, words no person,
      team or vendor here goes by;
    - `session` is the head noun: the last word before any parenthetical
      (`ateles session`, but not `session notes`);
    - two different markers (`Claude Code session`, `coordinator chat`);
    - a marker or `root` immediately followed by an id (`Foundation coordinator
      0a1b2c3d`, `chat0a1b2c3d`);
    - a space-free identifier starting with `session` (`session-email-review`).

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


def _real_assignee(assigned_to: object) -> bool:
    return (
        isinstance(assigned_to, str)
        and bool(assigned_to.strip())
        and assigned_to.strip().lower() not in SENTINEL_ASSIGNEES
    )


def session_owned(assigned_to: object, owner: object) -> bool:
    """True when a session owns the task.

    A real (non-sentinel) ``assigned_to`` decides alone: an assignee naming
    anyone else means ``owner`` is not consulted. Otherwise ``owner`` decides.
    """
    if _real_assignee(assigned_to):
        return names_a_session(assigned_to)
    return names_a_session(owner)
