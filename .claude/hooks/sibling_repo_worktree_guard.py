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
    to the shared clone, clean, rm --cached) AND whose cwd resolves into a
    sibling main clone.

Explicitly NOT covered (allowed):
  - Anything inside the Ateles repo itself (this is our home).
  - Anything inside a dedicated linked worktree (the safe path).
  - Read-only git (status/log/diff/show/rev-parse/worktree list) and all reads.
  - `git worktree add` itself (that is the remedy).

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
_GIT_MUTATION_RE = re.compile(
    r"\bgit\b[^\n;|&]*?\b("
    r"commit|merge|rebase|cherry-pick|revert|"
    r"reset|apply|am|"
    r"checkout\s+-\w*b|switch\s+-\w*c|"          # branch-creating checkout/switch
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
    except Exception:  # noqa: BLE001
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
        f"another session's branch on 2026-07-21). Create a dedicated worktree "
        f"first, then target that path:\n"
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


def check_bash(command: str, ateles: Path):
    if not command:
        return None
    # Allow the remedy itself and obvious read-only git.
    if "worktree add" in command:
        return None
    if _GIT_READONLY_HINT.search(command) and not _GIT_MUTATION_RE.search(command):
        return None
    if not _GIT_MUTATION_RE.search(command):
        return None
    # Determine the cwd the command runs in: an explicit `cd <dir>` prefix, else PWD.
    target_dir = None
    m = re.search(r"\bcd\s+([^\s;&|]+)", command)
    if m:
        cand = m.group(1).strip().strip("'\"")
        try:
            cand_p = Path(os.path.expanduser(cand))
            target_dir = cand_p if cand_p.is_absolute() else (Path.cwd() / cand_p)
        except Exception:  # noqa: BLE001
            target_dir = None
    if target_dir is None:
        target_dir = Path.cwd()
    top, is_shared = shared_main_clone_for(target_dir)
    if not top or not is_shared or top == ateles:
        return None
    return guidance(top)


def main() -> int:
    if os.environ.get("ATELES_ALLOW_SHARED_REPO_WRITES") == "1":
        return 0
    ev = read_hook_input()
    tool = ev.get("tool_name", "")
    ti = ev.get("tool_input", {}) or {}

    # Cheap early-out BEFORE any git subprocess: a Bash command with no git
    # mutation, or an editless tool, cannot trip the guard. Keeps the common
    # case (most Bash calls) at pure-Python cost.
    if tool == "Bash":
        cmd = ti.get("command", "")
        if not _GIT_MUTATION_RE.search(cmd) or "worktree add" in cmd:
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
