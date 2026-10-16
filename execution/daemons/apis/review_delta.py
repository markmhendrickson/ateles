"""execution/daemons/apis/review_delta.py — the size of a re-review round's change.

Operator ruling `small_rereview_rounds_run_mid` (2026-09-29): a pm, qa or ux
re-review of a SMALL change runs mid-tier. "Small" has to be the change the
re-review actually looks at — what the PR's author changed since the last
reviewed head — not the whole PR, and not whatever a merge of the base branch
dragged in either.

That last point is why this is not a plain compare of the two heads. A branch
that merged ``main`` since the last review has, between those heads, all of
what ``main`` brought in; measured that way a one-line fix reads as 1700 lines
touching security paths. The change a reviewer owns is the PR's OWN diff, so the
delta is the INTERDIFF: the PR's diff against its merge-base at the last reviewed
head, compared with the PR's diff against its merge-base now. Content that came
from the base branch sits on both sides and cancels.

Both sides come from GitHub's three-dot compare (``base...head``), which is the
diff from the merge-base to the head, so no local checkout is needed. Pure
functions, no I/O: ``swarm_dispatch`` fetches the compares and asks here.

Fail-up by construction. ``interdiff`` never returns a number smaller than the
change it could see: a file it cannot read a patch for counts at its full size,
and callers fall back to the WHOLE diff (``None``) whenever either side is
truncated or unreadable. An unmeasurable delta must never look like a small one.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass

# GitHub lists at most this many files for a compare and gives no signal that it
# truncated, so a list this long may be missing files: not trustworthy.
COMPARE_FILE_CAP = 300

# Above this many changed lines on one file, sequence-matching two patches is
# not worth its cost; the file counts at the larger of its two sizes instead.
_MAX_PATCH_LINES_TO_MATCH = 20000


@dataclass(frozen=True)
class Delta:
    """What changed since the last reviewed head: measured lines and files."""

    lines: int
    files: tuple[str, ...]


def compare_files(body: object) -> list[dict] | None:
    """The PR-side file list of a three-dot compare response, or None when it
    cannot be trusted (not a compare body, a malformed entry, or a list long
    enough that GitHub may have truncated it)."""
    if not isinstance(body, dict):
        return None
    files = body.get("files")
    if not isinstance(files, list) or len(files) >= COMPARE_FILE_CAP:
        return None
    for entry in files:
        if not isinstance(entry, dict) or not isinstance(entry.get("filename"), str):
            return None
    return files


def _entry_size(entry: dict) -> int:
    """Changed lines of one compare file entry, at least 1 (a binary file
    reports 0 changes but did change)."""
    try:
        changes = int(entry.get("changes") or 0)
        if not changes:
            changes = int(entry.get("additions") or 0) + int(entry.get("deletions") or 0)
    except (TypeError, ValueError):
        changes = 0
    return max(changes, 1)


def _change_lines(entry: dict) -> list[str] | None:
    """The +/- lines of a file's patch, in order, without context or hunk
    headers; None when the entry carries no patch (binary, or too large)."""
    patch = entry.get("patch")
    if not isinstance(patch, str):
        return None
    return [
        line for line in patch.splitlines() if line[:1] in ("+", "-")
    ]


def _file_interdiff(old: dict, new: dict) -> int:
    """Changed lines between one file's old PR-side patch and its new one."""
    old_sha, new_sha = old.get("sha"), new.get("sha")
    if old_sha and old_sha == new_sha:
        return 0  # the same blob: the file did not change, whatever its patch text
    old_lines, new_lines = _change_lines(old), _change_lines(new)
    if old_lines is None or new_lines is None:
        # No patch on a side: equal blobs mean the file is identical at both
        # heads, otherwise count it at its full size (never under-measure).
        if old.get("sha") and old.get("sha") == new.get("sha"):
            return 0
        return max(_entry_size(old), _entry_size(new))
    if old_lines == new_lines:
        return _unmeasured_change(old, new)
    if len(old_lines) + len(new_lines) > _MAX_PATCH_LINES_TO_MATCH:
        return max(len(old_lines), len(new_lines))
    # An ORDERED match, not a multiset one, so a block of the PR's own change
    # that merely moved within the file still counts as changed.
    matched = sum(
        block.size
        for block in difflib.SequenceMatcher(
            None, old_lines, new_lines, autojunk=False
        ).get_matching_blocks()
    )
    return (len(old_lines) - matched) + (len(new_lines) - matched)


def _unmeasured_change(old: dict, new: dict) -> int:
    """1 when the +/- lines match but the file did not stay the same, else 0.

    The line measure drops context and position, so a guard moved past the call
    it protects reads as zero changed lines (security review of ateles#1368).
    A file present on both sides whose blob or patch text differs changed,
    whatever the line measure says. Callers that classify files (a carried
    sign-off) read this as touched; tiering reads it as one line."""
    if old.get("sha") != new.get("sha") or old.get("patch") != new.get("patch"):
        return 1
    return 0


def interdiff(old_files: list[dict], new_files: list[dict]) -> Delta:
    """The PR's own change between two reviewed states.

    ``old_files`` and ``new_files`` are the file lists of ``base...old_head``
    and ``base...new_head`` (see ``compare_files``). A file appearing on one
    side only counts at its full size there: the PR started touching it, or
    stopped, and either is part of what changed since the last review.
    """
    old = {e["filename"]: e for e in old_files}
    new = {e["filename"]: e for e in new_files}
    total = 0
    changed: list[str] = []
    for name in sorted(set(old) | set(new)):
        before, after = old.get(name), new.get(name)
        if before is not None and after is not None:
            size = _file_interdiff(before, after)
        else:
            size = _entry_size(before if before is not None else after)
        if size <= 0:
            continue
        total += size
        changed.append(name)
        # A rename moves the path a security-path check would have matched.
        for entry in (before, after):
            previous = (entry or {}).get("previous_filename")
            if isinstance(previous, str) and previous and previous not in changed:
                changed.append(previous)
    return Delta(lines=total, files=tuple(changed))
