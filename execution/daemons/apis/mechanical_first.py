"""Deterministic-first execution for mechanical work classes.

A mechanical dispatch should reach a model only when git or a generator cannot
finish the job by itself. This module holds the deterministic half: it does the
work with plain git / the caller's named generator, verifies the result against
the repository, and hands back either a finished outcome (no model call) or, for
an integration that stops on conflicts, a brief scoped to just the conflicted
hunks.

It is deliberately pure: it shells out to git and to the caller's generator,
and imports nothing from the harness. ``dispatch_role.dispatch`` owns the
orchestration (which outcome means "no model", which means "model, with this
brief"), so the routing decision stays in one place.

Two classes have a deterministic path:

* ``rebase``: integrate a named base into the current branch with
  ``git rebase`` or, for a branch that is already pushed and reviewed, a merge
  commit (``git merge --no-ff``). A clean integration is verified and reported
  with no model call. Conflicts leave the operation in progress and return the
  conflicted hunks as the model's brief.
* ``regenerate_generated_files``: run the generator command(s) the caller names.

Every other mechanical class has no deterministic path here and stays on the
model path. Nothing in this module pushes, force-pushes, or stashes.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from dataclasses import dataclass, field

MODE_REBASE = "rebase"
MODE_MERGE = "merge"
MODES = (MODE_REBASE, MODE_MERGE)

# Outcome statuses.
DONE = "done"            # finished and verified; no model needed
CONFLICTS = "conflicts"  # stopped on conflicts; operation left in progress
REFUSED = "refused"      # preconditions not met; nothing was changed
ERROR = "error"          # git or the generator failed for a non-conflict reason

# Characters of conflicted hunk text put in a model brief. The local window is
# small, so the brief is bounded; the model can `sed -n` the rest of a file.
DEFAULT_BRIEF_BUDGET_CHARS = 9000
GENERATOR_TIMEOUT_SECONDS = 900
_GIT_TIMEOUT_SECONDS = 300
_OUTPUT_TAIL_CHARS = 2000

_CONFLICT_LINE = re.compile(r"^<{7}( |$)")
_CONFLICT_END = re.compile(r"^>{7}( |$)")


@dataclass
class Outcome:
    status: str
    summary: str
    base_sha: str = ""
    conflicted_files: list[str] = field(default_factory=list)
    brief: str = ""
    changed_files: list[str] = field(default_factory=list)


def _git(cwd: str, *args: str, timeout: int = _GIT_TIMEOUT_SECONDS) -> subprocess.CompletedProcess:
    """Run git non-interactively: no editor, no credential prompt."""
    child = {
        **os.environ,
        "GIT_EDITOR": "true",
        "GIT_SEQUENCE_EDITOR": "true",
        "GIT_TERMINAL_PROMPT": "0",
    }
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=child
    )


def _out(proc: subprocess.CompletedProcess) -> str:
    return (proc.stdout or "").strip()


def _tail(text: str, limit: int = _OUTPUT_TAIL_CHARS) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def operation_in_progress(cwd: str) -> str:
    """'rebase', 'merge', or '' — whichever integration git is mid-way through."""
    for name, kind in (("rebase-merge", "rebase"), ("rebase-apply", "rebase"), ("MERGE_HEAD", "merge")):
        proc = _git(cwd, "rev-parse", "--git-path", name)
        if proc.returncode == 0:
            path = _out(proc)
            if not os.path.isabs(path):
                path = os.path.join(cwd, path)
            if os.path.exists(path):
                return kind
    return ""


def unmerged_files(cwd: str) -> list[str]:
    proc = _git(cwd, "diff", "--name-only", "--diff-filter=U")
    return [line for line in _out(proc).splitlines() if line]


def _dirty_tracked(cwd: str) -> list[str]:
    proc = _git(cwd, "status", "--porcelain", "--untracked-files=no")
    return [line for line in _out(proc).splitlines() if line]


def _head_sha(cwd: str) -> str:
    return _out(_git(cwd, "rev-parse", "HEAD"))


def _contains(cwd: str, base_sha: str) -> bool:
    return _git(cwd, "merge-base", "--is-ancestor", base_sha, "HEAD").returncode == 0


def verify_integration(cwd: str, base_sha: str, mode: str = MODE_REBASE) -> str | None:
    """Re-derive the integration's ground truth from git; None means verified.

    Independent of anything a model said: the operation is finished, no path is
    unmerged, the tracked tree is clean, and HEAD contains the base commit.
    """
    pending = operation_in_progress(cwd)
    if pending:
        return f"a {pending} is still in progress"
    files = unmerged_files(cwd)
    if files:
        return "unmerged paths remain: " + ", ".join(files[:10])
    dirty = _dirty_tracked(cwd)
    if dirty:
        return "the working tree is not clean: " + "; ".join(dirty[:5])
    if not _contains(cwd, base_sha):
        return f"HEAD does not contain the base commit {base_sha[:12]}"
    return None


def abort_integration(cwd: str) -> str:
    """Abort whatever integration is in progress; returns what was aborted."""
    pending = operation_in_progress(cwd)
    if pending == "rebase":
        _git(cwd, "rebase", "--abort")
    elif pending == "merge":
        _git(cwd, "merge", "--abort")
    return pending


def _conflict_hunks(path: str, budget: int) -> tuple[str, int]:
    """Numbered conflict blocks of one file, within ``budget`` chars.

    Returns (text, chars_used). A block is shown with two lines of context above
    it; blocks that do not fit are counted, not silently dropped.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError as exc:
        return f"  (could not read: {exc})\n", 0
    blocks: list[tuple[int, int]] = []
    start = None
    for idx, line in enumerate(lines):
        if start is None and _CONFLICT_LINE.match(line):
            start = idx
        elif start is not None and _CONFLICT_END.match(line):
            blocks.append((start, idx))
            start = None
    if not blocks:
        return "  (no conflict markers: a delete/rename or binary conflict; inspect with `git status`)\n", 0
    shown: list[str] = []
    used = 0
    omitted = 0
    for first, last in blocks:
        lo = max(0, first - 2)
        text = "\n".join(f"{n + 1:>5}| {lines[n]}" for n in range(lo, last + 1))
        if used + len(text) > budget:
            omitted += 1
            continue
        shown.append(f"  lines {first + 1}-{last + 1}:\n{text}\n")
        used += len(text)
    if omitted:
        shown.append(f"  ({omitted} more conflict block(s) in this file not shown; read the file)\n")
    return "".join(shown), used


def conflict_brief(
    cwd: str, base: str, base_sha: str, mode: str, files: list[str],
    *, budget: int = DEFAULT_BRIEF_BUDGET_CHARS,
) -> str:
    """The model's task after a deterministic integration stopped on conflicts."""
    branch = _out(_git(cwd, "symbolic-ref", "--short", "-q", "HEAD")) or "(detached)"
    method = "a rebase" if mode == MODE_REBASE else "a merge commit (`git merge --no-ff`)"
    finish = (
        "`GIT_EDITOR=true git rebase --continue`"
        if mode == MODE_REBASE
        else "`git commit --no-edit`"
    )
    parts = [
        f"Git already attempted to integrate `{base}` ({base_sha[:12]}) into branch "
        f"`{branch}` by {method} and stopped on conflicts in {len(files)} file(s). "
        "The operation is IN PROGRESS in this worktree. Resolve only the conflicts "
        "below; do not start the integration again, and do not abort it.",
        "",
    ]
    remaining = budget
    for name in files:
        text, used = _conflict_hunks(os.path.join(cwd, name), max(remaining, 0))
        remaining -= used
        parts.append(f"### {name}\n{text}")
    parts += [
        "",
        f"When every conflict is resolved: `git add` the resolved files, then run {finish}. "
        "If git stops again on more conflicts, resolve those the same way. "
        "Do not push. When git reports the integration finished, reply `done`.",
    ]
    return "\n".join(parts)


def attempt_integration(
    cwd: str, base: str, mode: str = MODE_REBASE,
    *, brief_budget: int = DEFAULT_BRIEF_BUDGET_CHARS,
) -> Outcome:
    """Integrate ``base`` into the current branch with git alone.

    DONE when git finishes cleanly (or the branch already contains the base) and
    the result verifies; CONFLICTS when git stops on conflicts, leaving the
    operation in progress with a scoped brief; REFUSED when a precondition fails,
    with nothing changed; ERROR when git fails for any other reason (the
    operation is aborted so the worktree is left as it was found).
    """
    if mode not in MODES:
        return Outcome(REFUSED, f"unknown integration mode {mode!r} (expected rebase or merge)")
    if not base or base.startswith("-"):
        return Outcome(REFUSED, f"invalid base ref {base!r}")
    if _git(cwd, "rev-parse", "--is-inside-work-tree").returncode != 0:
        return Outcome(REFUSED, f"{cwd} is not a git worktree")
    resolved = _git(cwd, "rev-parse", "--verify", "--quiet", f"{base}^{{commit}}")
    if resolved.returncode != 0:
        return Outcome(REFUSED, f"base ref {base!r} does not resolve to a commit here (fetch it first?)")
    base_sha = _out(resolved)
    pending = operation_in_progress(cwd)
    if pending:
        return Outcome(REFUSED, f"a {pending} is already in progress in this worktree", base_sha)
    if _git(cwd, "symbolic-ref", "-q", "HEAD").returncode != 0:
        return Outcome(REFUSED, "HEAD is detached; check out the branch to integrate first", base_sha)
    dirty = _dirty_tracked(cwd)
    if dirty:
        return Outcome(
            REFUSED,
            "the working tree has uncommitted tracked changes: " + "; ".join(dirty[:5]),
            base_sha,
        )

    if _contains(cwd, base_sha):
        return Outcome(
            DONE,
            f"branch already contains {base} ({base_sha[:12]}); nothing to integrate",
            base_sha,
        )

    if mode == MODE_REBASE:
        proc = _git(cwd, "rebase", base_sha)
    else:
        proc = _git(cwd, "merge", "--no-ff", "--no-edit", base_sha)

    if proc.returncode == 0:
        problem = verify_integration(cwd, base_sha, mode)
        if problem:
            return Outcome(ERROR, f"git reported success but verification failed: {problem}", base_sha)
        return Outcome(
            DONE,
            f"{mode} of {base} ({base_sha[:12]}) finished cleanly; HEAD is now {_head_sha(cwd)[:12]}",
            base_sha,
        )

    files = unmerged_files(cwd)
    if files and operation_in_progress(cwd):
        return Outcome(
            CONFLICTS,
            f"{mode} of {base} stopped on conflicts in {len(files)} file(s)",
            base_sha,
            conflicted_files=files,
            brief=conflict_brief(cwd, base, base_sha, mode, files, budget=brief_budget),
        )
    detail = _tail((proc.stderr or "") + "\n" + (proc.stdout or ""))
    abort_integration(cwd)
    return Outcome(ERROR, f"git {mode} failed without conflicts: {detail}", base_sha)


_FORBIDDEN_ARGV = (("git", "stash"),)


def _generator_refusal(argv: list[str]) -> str | None:
    if not argv:
        return "empty generator command"
    for head, word in _FORBIDDEN_ARGV:
        if os.path.basename(argv[0]) == head and word in argv[1:]:
            return f"refusing `{head} {word}` as a generator"
    if "--no-verify" in argv:
        return "refusing a generator that passes --no-verify"
    return None


def run_generators(
    cwd: str, commands: list[str], *, timeout: int = GENERATOR_TIMEOUT_SECONDS
) -> Outcome:
    """Run the caller's named generator command(s) in order, without a shell.

    DONE lists the tracked and untracked files the generators changed. The first
    failure stops the run and is returned verbatim, because a model cannot repair
    a generator that fails; it would only be tempted to hand-edit the output.
    """
    if not commands:
        return Outcome(REFUSED, "no generator command was named")
    for command in commands:
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            return Outcome(REFUSED, f"cannot parse generator command {command!r}: {exc}")
        refusal = _generator_refusal(argv)
        if refusal:
            return Outcome(REFUSED, refusal)
    for command in commands:
        argv = shlex.split(command)
        try:
            proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return Outcome(ERROR, f"generator not found: {argv[0]}")
        except subprocess.TimeoutExpired:
            return Outcome(ERROR, f"generator timed out after {timeout}s: {command}")
        if proc.returncode != 0:
            return Outcome(
                ERROR,
                f"generator `{command}` exited {proc.returncode}:\n"
                + _tail((proc.stdout or "") + "\n" + (proc.stderr or "")),
            )
    status = _git(cwd, "status", "--porcelain")
    changed = [line[3:] for line in (status.stdout or "").splitlines() if line.strip()]
    return Outcome(
        DONE,
        f"ran {len(commands)} generator command(s); "
        + (f"{len(changed)} file(s) changed" if changed else "no files changed"),
        changed_files=changed,
    )
