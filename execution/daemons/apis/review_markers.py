"""Shared lens-review marker + verdict parsers (one source, principles #9).

Consumed by Apis (`swarm_dispatch`, `skill_runner`) and by the PreToolUse
approve guard (`.claude/hooks/lib/panel_verdicts.py`). Stdlib-only so the
hook process can import without pulling daemon runtime deps.

Do not paste a second copy of these patterns into any caller.
"""

from __future__ import annotations

import re

# Extend this tuple — never a second regex or a second hand-typed list — when
# the swarm needs a new verdict token. skill_runner renders SWARM_GITHUB_CONTRACT
# from it; REVIEW_VERDICT_RE is built from it; both must stay in lockstep
# (ateles#938 / test_instructed_review_verdict_tokens_subseteq_parser).
REVIEW_VERDICT_TOKENS: tuple[str, ...] = (
    "APPROVE",
    "REQUEST_CHANGES",
    "COMMENT",
    "BLOCKED",
    "SIGNED_OFF",
)

# HTML marker lenses post at the top of a PR issue-comment.
# Example: <!-- review:security commit=abcdef0123... -->
LENS_MARKER_RE = re.compile(
    r"<!--\s*review:(?P<lens>[a-z0-9_-]+)\s+commit=(?P<sha>[0-9a-f]{40})\s*-->",
    re.IGNORECASE,
)

REVIEW_VERDICT_RE = re.compile(
    r"\*\*(" + "|".join(re.escape(tok) for tok in REVIEW_VERDICT_TOKENS) + r")\*\*",
    re.I,
)

# `[BLOCKING] category: summary`, optionally wrapped in markdown emphasis.
# Negative lookbehind on NON- so `[NON-BLOCKING]` is not a hit (ateles#595).
BLOCKING_MARKER_RE = re.compile(
    r"(?<!NON-)(?<!NON_)\[BLOCKING\]", re.IGNORECASE
)


def parse_review_verdict(stdout: str) -> str | None:
    """Extract a bold verdict token; return lower-case name or None."""
    m = REVIEW_VERDICT_RE.search(stdout or "")
    return m.group(1).lower() if m else None


def body_has_blocking_findings(body: str | None) -> bool:
    """True when a review body carries at least one `[BLOCKING]` finding."""
    if not body:
        return False
    return bool(BLOCKING_MARKER_RE.search(body))
