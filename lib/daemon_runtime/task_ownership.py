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
_HEX_ID = r"[0-9a-f]{6,}"
# A marker run directly into a hex id (`chat0a1b2c3d`, `coordinator0a1b...`).
_MARKER_WITH_ID = re.compile(
    r"(?:" + "|".join(sorted(_SESSION_MARKERS)) + r")" + _HEX_ID
)
# `root` names a session only together with an id; bare `root` is a word.
_ROOT_WITH_ID = re.compile(r"root" + _HEX_ID)
_HEX_TOKEN = re.compile(_HEX_ID)
# Delimiters are anything non-alphanumeric. Underscore is a delimiter, which
# `\b` would not make it, so tokens are split explicitly.
_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def names_a_session(value: object) -> bool:
    """True when ``value`` names an interactive session as the owner.

    Only the first clause (text before the first ``;``) counts: the owner is
    named first, and what follows is commentary that may mention a session
    without the session owning anything. Non-strings and empty values are
    False.
    """
    if not isinstance(value, str) or not value.strip():
        return False
    first = value.split(";", 1)[0].lower()
    tokens = [t for t in _TOKEN_SPLIT.split(first) if t]
    for i, token in enumerate(tokens):
        if token in _SESSION_MARKERS:
            return True
        if _MARKER_WITH_ID.fullmatch(token) or _ROOT_WITH_ID.fullmatch(token):
            return True
        if token == "root" and i + 1 < len(tokens) and _HEX_TOKEN.fullmatch(tokens[i + 1]):
            return True
    return False


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
