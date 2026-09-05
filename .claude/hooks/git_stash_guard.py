#!/usr/bin/env python3
"""PreToolUse hook — block `git stash` mutations against this repo's shared stack.

The hazard: the git stash stack is stored in the **common** git dir
(`.git/refs/stash` + its reflog), which every linked worktree shares with the
main clone. Multiple Claude sessions run concurrently against this repo. One
session's `git stash` pushes an entry that ANOTHER session's `git stash pop`
consumes — silently destroying uncommitted work in a worktree the popping
session never touched. Worktrees isolate files; they do NOT isolate the stash.

Observed 2026-09-05: two agents ran `git stash` despite an explicit "never use
git stash" instruction written in their briefs. Both followed immediately with
`stash pop` and nothing was lost, but the window between push and pop was real,
and a concurrent pop inside it would have been unrecoverable. The instruction
demonstrably does not hold, so the enforcement is mechanical.

Blocked (every form that MUTATES the shared stack):
  - bare `git stash` (implicit push)
  - `git stash push` / `save` / `pop` / `apply` / `drop` / `clear` /
    `branch` / `create` / `store`
  - the same via `git -C <path> stash` and `git --git-dir=... stash`

Explicitly NOT blocked (read-only inspection of the stack):
  - `git stash list`
  - `git stash show`

That read-only carve-out is deliberate. Neither command writes a ref or a
reflog entry, so neither can destroy another session's work. Blocking them
would cost a session the ability to SEE what is on the stack — which is
exactly what the recovery procedure requires (`git stash list --format='%H %gs'`
to locate an entry left behind by a session that stashed before this hook
existed). A guard that blinds the recovery path makes the incident worse, not
better. `create` IS blocked despite not touching refs by default: it is the
staging half of `store`, and its only normal use is to feed one.

The remedy the block message names is a temporary WIP commit, which is
per-worktree and therefore invisible to every other session:

    git commit -am wip          # later: git reset HEAD~1

Compound commands (`&&`, `;`, `|`, `&`, newlines) are split into segments and
evaluated independently, so a stash hidden after an innocuous first segment is
still caught. Shell line continuations are folded first so a wrapped
invocation stays one segment.

Override: prefix ATELES_ALLOW_GIT_STASH=1 to that one invocation, e.g.

    ATELES_ALLOW_GIT_STASH=1 git stash pop

Recognised ONLY as an inline prefix on the gated segment itself, following
gmail_send_gate.py rather than sibling_repo_worktree_guard.py. The sibling
guard reads its override from os.environ in the HOOK's process, and hooks fire
before the shell runs — so an inline `VAR=1 cmd` prefix can never reach it and
only an `export` works. An export approves every stash for the rest of the
session, which is the "approval carries forward" failure this gate exists to
close. The inline prefix is scoped to the segment it prefixes and is re-typed
per command. An ambient/exported ATELES_ALLOW_GIT_STASH is deliberately ignored.

Fail-open: any error or unparseable input → exit 0 (never block a session on
our own bug).
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _session_integrity import read_hook_input  # noqa: E402

OVERRIDE_ENV = "ATELES_ALLOW_GIT_STASH"

# Subcommands that leave the shared stack unchanged. Matched only when they
# are the token immediately after `stash`; anything else (including a bare
# `git stash`, which is an implicit push) mutates.
READ_ONLY_SUBCOMMANDS = ("list", "show")

# A `git stash` INVOCATION, not the words appearing in prose. Only the flags
# this hook expects between `git` and its subcommand (-C, --git-dir,
# --work-tree, -c) may intervene; any other token there means `stash` is an
# argument or a different subcommand's payload, not the subcommand itself.
#
# `\bgit\b` also matches a path-qualified binary (`/usr/bin/git`, `./git`)
# because `/` is a word boundary — path variants are covered without a
# separate pattern.
GIT_STASH_RE = re.compile(
    r"\bgit\b"
    r"(?:\s+(?:-C\s+\S+|--git-dir[=\s]\S+|--work-tree[=\s]\S+|-c\s+\S+))*"
    r"\s+stash\b"
    r"(?:\s+(?P<sub>[A-Za-z-]+))?",
)

# An argv-LIST form, where `git` and `stash` are adjacent list elements rather
# than whitespace-separated shell words: `subprocess.run(['git','stash','pop'])`,
# `execFileSync("git", ["stash"])`, `spawn('git', ['stash', 'pop'])`. The
# whitespace-separated GIT_STASH_RE above cannot see these, because quoting and
# commas — not spaces — sit between the tokens. This was the one live bypass the
# adversarial pass found against the first revision of this hook.
#
# Deliberately narrow: `git` and `stash` must be adjacent, separated only by
# quote/comma/bracket/whitespace punctuation. That keeps prose like
# `"git" is a program and "stash" is its subcommand` from matching, since real
# words intervene. `(?P<sub>...)` mirrors the primary pattern so the read-only
# carve-out applies identically to this form.
GIT_STASH_ARGV_RE = re.compile(
    r"""['"]git['"][\s,\]\[]+['"]stash['"]"""
    r"""(?:[\s,\]\[]+['"](?P<sub>[A-Za-z-]+)['"])?""",
)

SEGMENT_SPLIT = re.compile(r"&&|\|\||[;\n|&]")

# Commands that merely CARRY the pattern as text rather than invoking it: a
# commit message documenting this hazard, a grep for it, a PR body explaining
# the hook. Matching those would block work that touches no stash — the first
# casualty is this hook's own commit and PR.
#
# This list must contain ONLY commands that cannot themselves execute another
# command. Interpreters (`python -c`, `node -e`, `sh -c`, `perl -e`) and
# heredoc-consuming readers (`cat`) must NEVER appear here: they were live
# bypasses in an early revision of gmail_send_gate.py, because the gated
# command rode through as the exempt leader's argument. Do not add an executor.
TEXT_BEARING_LEADERS = re.compile(
    r"^(?:git\s+(?:commit|tag|notes)|echo|printf|grep|rg|"
    r"gh\s+(?:pr|issue|release))\b"
)

# An override must PREFIX the stashing segment itself (optionally after `env`),
# which is the documented usage. Anchored at the segment start so an override
# on an unrelated earlier segment cannot vouch for a later stash.
OVERRIDE_PREFIX = re.compile(rf"^(?:env\s+)?{OVERRIDE_ENV}=1\b")


def log(msg: str) -> None:
    """Local rather than imported: the shared helper's `log` hardcodes a
    `[session-integrity]` prefix, which would mislabel this guard's
    diagnostics. `read_hook_input` IS shared — its fail-open stdin semantics
    are exactly what this hook wants."""
    try:
        print(f"[git_stash_guard] {msg}", file=sys.stderr)
    except Exception:  # noqa: BLE001 — logging must never block
        pass


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


def guidance(label: str) -> str:
    return (
        f"Refused: `{label}` mutates the git stash stack, which is SHARED across "
        f"the main clone and every linked worktree — a concurrent session can pop "
        f"your entry and destroy uncommitted work.\n\n"
        f"Use a temporary WIP commit instead; it is per-worktree and invisible to "
        f"other sessions:\n"
        f"  git commit -am wip\n"
        f"  # ... do the thing ...\n"
        f"  git reset HEAD~1        # undo the WIP commit, keep the changes\n\n"
        f"`git stash list` and `git stash show` stay allowed — they read the stack "
        f"without writing it.\n\n"
        f"(Deliberate case: prefix the override to that one command — "
        f"{OVERRIDE_ENV}=1 <the same command>.)"
    )


def _join_line_continuations(command: str) -> str:
    r"""Fold `\<newline>` sequences so a continued command stays ONE segment.

    Without this, `git stash \<newline> pop` splits on the newline into two
    segments and the subcommand is evaluated separately from `git stash` — a
    bypass found in gmail_send_gate.py's adversarial pass. A backslash-newline
    is shell line continuation, not a command separator.
    """
    return re.sub(r"\\[ \t]*\n", " ", command)


def find_stash_mutation(command: str):
    """Return a label for the first unapproved stash-mutating segment, or None.

    The override is evaluated PER SEGMENT: it must prefix the stashing segment
    itself. An override prefixing some earlier, unrelated segment does not
    vouch for a later stash — otherwise
    `ATELES_ALLOW_GIT_STASH=1 echo ok && git stash` would smuggle one past.
    """
    for segment in SEGMENT_SPLIT.split(_join_line_continuations(command)):
        normalized = " ".join(segment.split())
        if not normalized:
            continue
        # A segment led by git-commit/echo/grep/gh-pr carries the pattern as
        # TEXT and invokes nothing. Not a bypass: the leader must be the first
        # token, so a real invocation cannot hide behind it — `echo x && git
        # stash` splits into two segments and the stashing one is judged alone.
        if TEXT_BEARING_LEADERS.match(normalized):
            continue
        match = GIT_STASH_RE.search(normalized) or GIT_STASH_ARGV_RE.search(normalized)
        if not match:
            continue
        sub = (match.group("sub") or "").lower()
        if sub in READ_ONLY_SUBCOMMANDS:
            continue  # reads the stack, cannot destroy another session's work
        if OVERRIDE_PREFIX.match(normalized):
            log("inline override prefixes this git stash; allowing")
            continue
        return f"git stash {sub}".strip() if sub else "git stash"
    return None


def main() -> int:
    payload = read_hook_input()  # shared helper; fail-open to {} on any error

    if payload.get("tool_name") != "Bash":
        return 0

    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str) or "stash" not in command:
        return 0

    # The inline form (ATELES_ALLOW_GIT_STASH=1 git stash ...) is the ONLY
    # approval path, evaluated per segment inside find_stash_mutation so it
    # vouches solely for the segment it prefixes.
    #
    # Deliberately absent: an ambient `os.environ[OVERRIDE_ENV]` check — see
    # the module docstring for why an export is the wrong shape here.
    label = find_stash_mutation(command)
    if label is None:
        return 0

    log(f"blocking {label}")
    return deny(guidance(label))


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open, never break a session
        log(f"internal error, failing open: {exc}")
        sys.exit(0)
