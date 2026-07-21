#!/usr/bin/env python3
"""PreToolUse hook — block mutating a SIBLING repo's shared main clone.

The hazard (observed 2026-07-21): while operating from the Ateles repo, an
Edit/Write or a stray `git commit`/`git checkout -b` targeting another repo's
MAIN clone (e.g. ~/repos/neotoma) silently mutated a checkout that ANOTHER
session was actively using — landing a commit on their branch. Worktrees isolate
files; the shared main clone does not.

This hook refuses, at PreToolUse, any *mutating* operation whose target is a
sibling repo's main clone (not the Ateles repo, and not a dedicated linked
worktree). It tells the agent to create a worktree first:

    git worktree add ~/repos/<repo>-wt-<slug> origin/main

Detection of "main clone vs linked worktree" uses git's own signal:
`git rev-parse --git-dir` == `--git-common-dir`  → MAIN clone (shared) → blocked.
They differ for a linked worktree → allowed.

Covered:
  - Edit / Write / NotebookEdit whose file path is inside a sibling main clone.
  - Bash whose command contains a git *mutation* (commit, checkout -b, switch -c,
    reset, merge, rebase, cherry-pick, apply, stash pop/apply, branch -f, push
    to the shared clone, clean, rm --cached). Compound commands (`&&`, `;`,
    `|`, newlines) are split into segments and evaluated independently, so a
    later mutating segment is still caught even after an earlier `worktree
    add` or unrelated segment — each mutating segment resolves its own
    target dir from its own -C/--git-dir, or the most recently seen `cd` in
    the chain, or the hook's cwd as last resort.

Explicitly NOT covered (allowed):
  - Anything inside the Ateles repo itself (this is our home).
  - Anything inside a dedicated linked worktree (the safe path).
  - Read-only git (status/log/diff/show/rev-parse/worktree list) and all reads.
  - An actual `git worktree add` invocation itself (that is the remedy) —
    but only the segment(s) that ARE that invocation; a compound command
    mixing `worktree add` with an unrelated mutating segment still blocks
    on the mutating segment.

Fail-open: any error, missing git, or unparseable input → exit 0 (never block a
session on our own bug). Deliberate override: set
ATELES_ALLOW_SHARED_REPO_WRITES=1 in the environment.
"""
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_integrity import read_hook_input, log  # noqa: E402

# git subcommands / flag-forms that MUTATE the repo or its refs.
# A plain `checkout <branch>` / `switch <branch>` also mutates a shared clone's
# HEAD/working tree (the exact failure that opened this incident — a switch that
# aborted mid-way left the session on another branch), so those are matched too,
# not only the branch-CREATING forms.
_GIT_MUTATION_RE = re.compile(
    r"\bgit\b[^\n;|&]*?\b("
    r"commit|merge|rebase|cherry-pick|revert|"
    r"reset|apply|am|"
    r"checkout|switch|"                           # any checkout/switch (incl. plain branch move)
    r"branch\s+-\w*[fDdm]|"                       # force/delete/move branch
    r"stash\s+(pop|apply|drop|push|save)|"
    r"clean\b|"
    r"rm\s+--cached|"
    r"push\b|tag\s+-\w*[df]|"
    r"restore\b"
    r")",
    re.IGNORECASE,
)

# Read-only git we never want to touch even if the regex is fooled.
_GIT_READONLY_HINT = re.compile(
    r"\bgit\b[^\n;|&]*?\b(status|log|diff|show|rev-parse|worktree\s+list|"
    r"branch\s*$|remote|fetch|ls-remote|ls-files|cat-file|blame|describe)\b",
    re.IGNORECASE,
)

# The remedy itself: an actual `git ... worktree add` INVOCATION, not a bare
# substring match. A bare "worktree add" anywhere in command TEXT (e.g. a
# commit message `git commit -am "prep for worktree add"`, or that phrase
# appearing in a LATER, unrelated segment) must never be treated as the
# remedy — that was a real bypass in the prior substring-based check.
# `worktree` must be the actual subcommand token: only -C/--git-dir (the
# flags this hook itself parses) may appear between `git` and `worktree`;
# any other token there (a different subcommand, a quoted argument) means
# "worktree add" is just text, not an invocation.
_GIT_WORKTREE_ADD_RE = re.compile(
    r"\bgit\b\s*(?:-C\s+\S+\s*|--git-dir[=\s]\S+\s*)*\bworktree\s+add\b",
    re.IGNORECASE,
)


def _run(args, cwd=None):
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, timeout=5
    )


def ateles_root() -> Path:
    """The Ateles repo we are operating FROM — never guarded."""
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    try:
        r = _run(["git", "rev-parse", "--show-toplevel"], cwd=root)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip()).resolve()
    except Exception:  # noqa: BLE001
        pass
    return Path(root).resolve()


def shared_main_clone_for(path: Path):
    """If `path` lives inside a git repo that is the MAIN clone (not a linked
    worktree), return (toplevel, is_shared_main). Else (None, False)."""
    try:
        # Walk up to the first ancestor that actually exists — a Write may create
        # brand-new nested dirs (e.g. docs/infrastructure/), whose parent does not
        # exist yet, so git run from there would fail and silently allow the write.
        d = path if path.is_dir() else path.parent
        while not d.exists() and d != d.parent:
            d = d.parent
        top = _run(["git", "rev-parse", "--show-toplevel"], cwd=str(d))
        if top.returncode != 0 or not top.stdout.strip():
            return None, False
        toplevel = Path(top.stdout.strip()).resolve()
        gd = _run(["git", "rev-parse", "--git-dir"], cwd=str(d))
        cd = _run(["git", "rev-parse", "--git-common-dir"], cwd=str(d))
        if gd.returncode != 0 or cd.returncode != 0:
            return None, False
        # git prints these relative to the CWD it ran in (`d`), not the toplevel:
        # from a subdir, --git-dir may be absolute while --git-common-dir is
        # "../.git". Resolve BOTH against `d` so the comparison is apples-to-apples.
        base = Path(d).resolve()
        git_dir = Path(os.path.join(base, gd.stdout.strip())).resolve()
        common_dir = Path(os.path.join(base, cd.stdout.strip())).resolve()
        # Equal → main clone (shared). Differ → linked worktree (safe).
        return toplevel, (git_dir == common_dir)
    except FileNotFoundError:
        try:
            log("guard: git not found, sibling-repo check skipped for this call")
        except Exception:  # noqa: BLE001 — logging must never block
            pass
        return None, False
    except Exception as exc:  # noqa: BLE001
        try:
            log(f"guard: could not evaluate git state for {path}: {exc}")
        except Exception:  # noqa: BLE001 — logging must never block
            pass
        return None, False


def deny(reason: str) -> int:
    """Emit a PreToolUse deny and block."""
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    return 2


def guidance(toplevel: Path) -> str:
    name = toplevel.name
    return (
        f"Refused: this would mutate the SHARED main clone at {toplevel} — "
        f"another session may be using it (this exact hazard landed a commit on "
        f"another session's branch on 2026-07-21, see ateles#246). Create a "
        f"dedicated worktree first, then target that path:\n"
        f"  git worktree add ~/repos/{name}-wt-<slug> origin/main\n"
        f"  cd ~/repos/{name}-wt-<slug>\n"
        f"Do all edits/commits there. (Override for a deliberate case: set "
        f"ATELES_ALLOW_SHARED_REPO_WRITES=1.)"
    )


def check_path_target(raw_path: str, ateles: Path):
    """Return a deny-reason if raw_path is inside a sibling shared main clone."""
    if not raw_path:
        return None
    try:
        p = Path(os.path.expanduser(raw_path))
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        else:
            p = p.resolve()
    except Exception:  # noqa: BLE001
        return None
    top, is_shared = shared_main_clone_for(p)
    if not top or not is_shared:
        return None
    if top == ateles or ateles == top:
        return None  # our own repo
    # sibling repo, shared main clone → block
    return guidance(top)


def _split_segments(command: str):
    """Split a compound command into ordered &&/;/|/newline segments.

    A plain split is enough here — we don't need a shell parser, just enough
    to separate invocations so each one can be evaluated on its own terms
    instead of via first-match-in-the-whole-string regexes. re.split keeps
    this simple; segments are stripped but otherwise left as raw text for
    the existing per-segment regexes to match against. Splits on newlines
    too: a multi-line Bash block (`cmd1\ncmd2`) is just as much a compound
    command as one joined with `&&`.
    """
    return [seg.strip() for seg in re.split(r"&&|;|\||\n", command) if seg.strip()]


def _resolve_cand(cand: str):
    try:
        cp = Path(os.path.expanduser(cand.strip().strip("'\"")))
        return cp if cp.is_absolute() else (Path.cwd() / cp)
    except Exception:  # noqa: BLE001
        return None


def check_bash(command: str, ateles: Path):
    if not command:
        return None
    if not _GIT_MUTATION_RE.search(command):
        return None
    # Determine the dir EACH mutating git invocation actually acts on.
    # Compound commands (`&&`, `;`, `|`) can contain several invocations, so
    # this walks segments left-to-right rather than running -C/--git-dir/cd
    # regexes against the whole string, which would always key off the FIRST
    # match anywhere — wrong when a later segment is the mutating one, or
    # when an earlier `worktree add` segment shielded a later mutation.
    #
    # Per segment, precedence:
    #   1. `git -C <path>` / `git --git-dir=<path>` on THAT segment — git's
    #      own explicit target, overriding cwd/cd (Loxia caveat).
    #   2. the most recently seen `cd <dir>` target, tracked while walking
    #      segments in order (a later `cd` overrides an earlier one for
    #      subsequent commands in the same chain, matching real shell
    #      semantics).
    #   3. the hook's own cwd (PWD).
    #
    # LIMITATION (inherent to the PreToolUse signal): the Bash tool's cwd
    # persists across calls, but the hook only sees THIS command's text. If a
    # PRIOR call already `cd`'d into a sibling clone, a later bare `git
    # commit` in a NEW call is evaluated against the hook's cwd, not the real
    # one — so this is defense-in-depth, not a hermetic seal. It reliably
    # catches the single-command forms that caused the incident; the
    # persistent-cwd-across-calls case is why the never-work-in-shared-clones
    # rule also lives in operator memory, not only in this hook.
    cd_state = None
    for segment in _split_segments(command):
        # This segment's own `cd` (if any) updates state for THIS and later
        # segments — extracted once up front regardless of which branch
        # below fires. Note: a real shell would apply `cd` before evaluating
        # the rest of an `&&`-joined segment, but a `cd` and a mutating `git`
        # never coexist as a single _split_segments() segment (the `&&`/`;`
        # between them is exactly what splits them apart), so ordering here
        # doesn't matter in practice.
        m_cd = re.search(r"\bcd\s+([^\s;&|]+)", segment)
        if m_cd:
            cd_state = _resolve_cand(m_cd.group(1))
        if _GIT_WORKTREE_ADD_RE.search(segment):
            continue
        if not _GIT_MUTATION_RE.search(segment):
            continue
        target_dir = None
        m_c = re.search(r"\bgit\b[^\n;|&]*?\s-C\s+([^\s;&|]+)", segment)
        m_gd = re.search(r"--git-dir[=\s]+([^\s;&|]+)", segment)
        if m_c:
            target_dir = _resolve_cand(m_c.group(1))
        elif m_gd:
            gd = _resolve_cand(m_gd.group(1))
            # --git-dir points at the .git dir; its parent is the worktree.
            target_dir = gd.parent if gd and gd.name == ".git" else gd
        elif cd_state is not None:
            target_dir = cd_state
        if target_dir is None:
            target_dir = Path.cwd()
        top, is_shared = shared_main_clone_for(target_dir)
        if top and is_shared and top != ateles:
            return guidance(top)
    return None


def main() -> int:
    if os.environ.get("ATELES_ALLOW_SHARED_REPO_WRITES") == "1":
        return 0
    ev = read_hook_input()
    tool = ev.get("tool_name", "")
    ti = ev.get("tool_input", {}) or {}

    # Cheap early-out BEFORE any git subprocess: a Bash command with no git
    # mutation, or an editless tool, cannot trip the guard. Keeps the common
    # case (most Bash calls) at pure-Python cost. A bare substring check for
    # "worktree add" is NOT safe here — a compound command can carry a
    # `worktree add` segment AND a separate mutating segment (e.g. `git
    # worktree add ... && git -C <sibling> reset --hard`), so only skip when
    # EVERY mutation-matching segment is itself an actual `worktree add`
    # invocation (_GIT_WORKTREE_ADD_RE, not a bare substring match — a
    # mutating command whose commit message/branch name merely CONTAINS the
    # text "worktree add" must not be treated as the remedy); otherwise fall
    # through to check_bash for the real per-segment check.
    if tool == "Bash":
        cmd = ti.get("command", "")
        if not _GIT_MUTATION_RE.search(cmd):
            return 0
        if _GIT_WORKTREE_ADD_RE.search(cmd):
            segments = _split_segments(cmd)
            if all(
                _GIT_WORKTREE_ADD_RE.search(seg) or not _GIT_MUTATION_RE.search(seg)
                for seg in segments
            ):
                return 0
    elif tool not in ("Edit", "Write", "NotebookEdit"):
        return 0

    ateles = ateles_root()
    reason = None
    if tool in ("Edit", "Write", "NotebookEdit"):
        reason = check_path_target(
            ti.get("file_path") or ti.get("notebook_path") or "", ateles
        )
    elif tool == "Bash":
        reason = check_bash(ti.get("command", ""), ateles)

    if reason:
        log(f"blocked {tool} against shared sibling clone")
        return deny(reason)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open, never block on our own bug
        log(f"sibling_repo_worktree_guard error (ignored): {exc}")
        sys.exit(0)
