#!/usr/bin/env python3
"""Shared command-segment classification helpers for the Bash PreToolUse guards.

Extracted from the near-identical implementations that had grown independently
in `gmail_send_gate.py`, `git_stash_guard.py`, and `sibling_repo_worktree_guard.py`
(ateles#1265) — per the "extend the mechanism that already generalizes" rule
(docs/foundation/principles.md#6), the segment-classification approach
`gmail_send_gate.py` pioneered is extended here rather than hand-rolled a third
time.

Two things this module does NOT do, deliberately:

1. It never adds an interpreter or heredoc-consuming READER to the
   text-bearing-leader allowlist (`is_text_bearing_leader`). `python -c`,
   `python3 -c`, `node -e`, `sh -c`, `bash -c`, and `cat <<...` were in an
   early revision of `gmail_send_gate.py`'s allowlist and were live bypasses:
   a real mutating command rode through as the exempt leader's own argument.
   Closing that loophole is a hard requirement of ateles#1265 and must never
   be reopened by a future "helpful" addition here.

2. Stripping a heredoc's BODY (`strip_heredoc_bodies`) is not the same as
   exempting the command that CONSUMES it. Only the literal lines between the
   opening `<<TAG` and the line containing just `TAG` are removed — the rest
   of the segment, and any later segment, is untouched. A real mutating
   command following a heredoc in the same segment (e.g.
   `cat <<'EOF'\n...\nEOF\ngit stash`) must still be caught by the caller's
   own keyword/regex match against what remains.
"""
from __future__ import annotations

import re

# Superset alternation across the three pre-extraction implementations:
# gmail_send_gate.py / sibling_repo_worktree_guard.py used `&&|;|\||\n` (or
# `&&|[;\n|]`); git_stash_guard.py additionally split on a bare `&` (background
# execution). Using the broadest pattern here is a no-op behavior change for
# every existing call site while giving `&`-chained commands the same
# per-segment treatment everywhere.
SEGMENT_SPLIT = re.compile(r"&&|\|\||[;\n|&]")

# Base leader set common to all three guards: a segment beginning with one of
# these carries a gated phrase as TEXT (a commit message, a search query, an
# issue/PR body) rather than invoking it. `extra_leaders` lets an individual
# guard add its OWN leaders (e.g. more `gh` subcommands) without copy-pasting
# this list. Never add an interpreter or heredoc-consuming reader here — see
# the module docstring.
_BASE_TEXT_BEARING_LEADERS = (
    r"git\s+(?:commit|tag|notes)|echo|printf|grep|rg|"
    r"gh\s+(?:pr|issue|release)"
)

# A heredoc opener: `<<`, optionally `-` (tab-stripping form), optionally
# quoted (`'TAG'`/`"TAG"`) or bare (`TAG`). Capture the tag so the closing
# line can be matched exactly (word boundary, optional trailing whitespace,
# as shells require).
_HEREDOC_OPEN_RE = re.compile(
    r"<<-?[ \t]*(?:'(?P<tag_sq>[^']+)'|\"(?P<tag_dq>[^\"]+)\"|(?P<tag_bare>[A-Za-z_][A-Za-z0-9_]*))"
)


def join_line_continuations(command: str) -> str:
    r"""Fold `\<newline>` sequences so a continued command stays ONE segment.

    Without this, `git stash \<newline> pop` splits on the newline into two
    segments and the subcommand is evaluated separately from its leader — a
    bypass found in `gmail_send_gate.py`'s adversarial pass. A
    backslash-newline is shell line continuation, not a command separator.

    Moved verbatim from `gmail_send_gate.py` / `git_stash_guard.py` (identical
    in both) — behavior-preserving extraction, not a rewrite.
    """
    return re.sub(r"\\[ \t]*\n", " ", command)


def split_segments(command: str) -> list[str]:
    """Split a compound command into ordered segments on &&/||/;/|/&/newline.

    A plain split is enough here — this is not a shell parser, just enough to
    separate invocations so each can be classified independently instead of
    via first-match-in-the-whole-string regexes.
    """
    return [seg.strip() for seg in SEGMENT_SPLIT.split(command) if seg.strip()]


def strip_heredoc_bodies(command: str) -> str:
    """Remove the BODY lines of every heredoc in `command`, keeping the opener
    and closing-tag lines intact so segment splitting/structure is otherwise
    unaffected.

    This is what makes data quoted in a heredoc (an issue body, a Neotoma
    `agent_policy` string) invisible to a guard's keyword/regex match, even
    when that data verbatim contains a gated phrase like "git stash" or
    "merge-base". It does NOT exempt whatever command follows the heredoc in
    the same segment — only the lines strictly between the opener and the
    closing tag are removed.

    Handles `<<TAG`, `<<-TAG`, `<<'TAG'`, `<<"TAG"` — quoted vs. unquoted only
    matters to a real shell for variable expansion inside the body, which is
    irrelevant here since the body is discarded outright either way. Multiple
    heredocs in one command are each stripped independently. A line inside one
    heredoc's body that happens to look like a different heredoc's marker is
    not a real nested heredoc (shell has no such construct) and is correctly
    left alone: the scan below only looks for the CURRENT opener's own tag
    once inside a body.
    """
    lines = command.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        out.append(line)
        m = _HEREDOC_OPEN_RE.search(line)
        if not m:
            i += 1
            continue
        tag = m.group("tag_sq") or m.group("tag_dq") or m.group("tag_bare")
        dash = m.group(0).startswith("<<-")
        # Scan forward for the closing line: exactly `TAG` (optionally
        # leading tabs stripped for the `<<-` form), nothing else.
        j = i + 1
        close_pattern = re.compile(
            r"^\t*" + re.escape(tag) + r"[ \t]*$" if dash else r"^" + re.escape(tag) + r"[ \t]*$"
        )
        while j < n and not close_pattern.match(lines[j]):
            j += 1
        if j < n:
            # Real closing line found — body (lines i+1..j-1) is discarded,
            # closing line kept.
            out.append(lines[j])
            i = j + 1
        else:
            # Unterminated heredoc (malformed input, or a closing tag this
            # guard failed to recognize). Fail SAFE here, not fail-open: keep
            # every remaining line verbatim rather than silently dropping
            # them. Dropping them would erase a real trailing mutation
            # (`cat <<EOF\n...\ngit merge foo` with a typo'd/missing closing
            # tag) from both guards' view — worse than not stripping at all.
            out.extend(lines[i + 1 :])
            i = n
    return "\n".join(out)


def is_text_bearing_leader(segment: str, extra_leaders: str | None = None) -> bool:
    """True when `segment` is led by a command that carries a gated phrase as
    TEXT rather than invoking it — a commit message, a grep query, a
    gh pr/issue body. `extra_leaders` is an additional regex alternation
    (e.g. `r"gh\\s+release"`) unioned with the base set so a guard can extend
    the allowlist without duplicating it.

    Deliberately never includes an interpreter (`python -c`, `node -e`) or a
    heredoc-consuming reader (`cat`) — see the module docstring.
    """
    pattern = _BASE_TEXT_BEARING_LEADERS
    if extra_leaders:
        pattern = f"{pattern}|{extra_leaders}"
    return re.match(rf"^(?:{pattern})\b", segment) is not None
