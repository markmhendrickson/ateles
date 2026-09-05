# `.claude/hooks`

Repo-scoped Claude Code hooks, registered in `../settings.json`.

## `ateles-session-start.sh` (SessionStart)

Makes **Ateles** — the T2 resident "primary operator interface" / orchestrator
of the Ateles swarm — the default agent for **every** session (CLI and web), not
just when `/ateles` is invoked manually or reached via Telegram.

On each SessionStart it reads `../skills/ateles/SKILL.md` (the canonical
definition, regenerated from Neotoma entity `ent_706f1432822b4a9d9d71c127`) and
prints it to stdout, which Claude Code appends to the session context. A short
directive ahead of the SOUL reinforces the two behaviors that matter most in an
interactive session:

1. **Delegate through the swarm** — route T3/T4 work to the owning agent rather
   than doing it inline.
2. **Neotoma first** — check for pending blockers before accepting new goals.

Because the hook reads the SKILL.md at runtime, it never drifts from the
Neotoma-sourced definition. If that file is missing the hook exits silently and
the session falls back to generic Claude Code.

To temporarily run a plain (non-Ateles) session, comment out the SessionStart
entry in `../settings.json` or rename this script.

## `sibling_repo_worktree_guard.py` (PreToolUse)

Blocks mutating a **sibling repo's shared main clone** — e.g. running `Edit`,
`Write`, `NotebookEdit`, or a git-mutating `Bash` command against
`~/repos/neotoma` when that path is a plain clone, not a dedicated worktree.
Motivated by a 2026-07-21 incident (see `ateles#246`): a stray `Write` plus
`git commit` landed on **another session's branch** in a shared clone, because
a prior `git checkout -b` had silently aborted on that session's uncommitted
changes.

**Fires on:** `PreToolUse`, matcher `Edit|Write|NotebookEdit|Bash`.

**Detection:** for the target path (or `Bash` cwd, honoring `-C`/`--git-dir`/a
leading `cd`), compares `git rev-parse --git-dir` against
`--git-common-dir` — equal means a main clone (shared); different means a
linked worktree (safe).

| Target | Operation | Result |
|---|---|---|
| Ateles repo itself | any | allow |
| Sibling repo, dedicated linked worktree | any | allow |
| Sibling repo, shared main clone | read-only git (`status`, `log`, `diff`, …) | allow |
| Sibling repo, shared main clone | `git worktree add` (the remedy) | allow |
| Sibling repo, shared main clone | mutating (`Edit`/`Write`/`NotebookEdit`, or git `commit`/`checkout -b`/`reset`/`merge`/`push`/…) | **deny** |

**Override:** set `ATELES_ALLOW_SHARED_REPO_WRITES=1` in the environment for a
deliberate, one-off case. The deny message always names this variable.

**Fail-open:** any internal error, missing `git`, or unparseable hook input
allows the call through (never blocks a session on the hook's own bug). The
git-state evaluation in `shared_main_clone_for()` — where `git` missing or any
other subprocess/path error would otherwise degrade to a silent allow — and
the top-level `main()` handler emit a non-blocking stderr notice via the
shared `log()` helper so the guard going blind is visible after the fact
rather than silent.

### Worked example — the original incident

```
$ git checkout -b my-fix        # cwd: ~/repos/neotoma (shared main clone)
```

is denied with:

```
Refused: this would mutate the SHARED main clone at /Users/you/repos/neotoma — another
session may be using it (this exact hazard landed a commit on another session's branch
on 2026-07-21, see ateles#246). Create a dedicated worktree first, then target that path:
  git worktree add ~/repos/neotoma-wt-<slug> origin/main
  cd ~/repos/neotoma-wt-<slug>
Do all edits/commits there. (Override for a deliberate case: set ATELES_ALLOW_SHARED_REPO_WRITES=1.)
```

The remedy:

```
$ git worktree add ~/repos/neotoma-wt-issue-246 origin/main
$ cd ~/repos/neotoma-wt-issue-246
$ git checkout -b my-fix        # now on a dedicated worktree — allowed
```

## `git_stash_guard.py` (PreToolUse)

Blocks every `git stash` operation that **mutates the stash stack**. The stack
lives in the *common* git dir (`.git/refs/stash` and its reflog), so the main
clone and all linked worktrees share one stack. With several Claude sessions
running concurrently against this repo, one session's `git stash` can be popped
by another — silently destroying uncommitted work in a worktree the popping
session never touched. Worktrees isolate files; they do not isolate the stash.

Motivated by two violations on 2026-09-05: two agents ran `git stash` despite an
explicit "never use git stash" instruction in their briefs. Both popped
immediately and nothing was lost, but the window was real. The instruction did
not hold, so the enforcement is mechanical.

**Fires on:** `PreToolUse`, matcher `Edit|Write|NotebookEdit|Bash` (acts only on
`Bash`).

| Subcommand | Result |
|---|---|
| bare `git stash` (implicit push), `push`, `save` | **deny** |
| `pop`, `apply`, `drop`, `clear`, `branch` | **deny** |
| `create`, `store` | **deny** |
| `list`, `show` | allow — read-only |

`list` and `show` are deliberately allowed: neither writes a ref or a reflog
entry, so neither can destroy another session's work, and blocking them would
break the recovery path (`git stash list --format='%H %gs'` is how you find an
entry left behind by a session that stashed before this hook existed). A guard
that blinds recovery makes the incident worse. `create` **is** blocked despite
not writing refs by default — it is the staging half of `store`, and feeding
`store` is its only normal use.

**Remedy named in the block message:** a temporary WIP commit, which is
per-worktree and invisible to other sessions.

```
git commit -am wip
# ... do the thing ...
git reset HEAD~1        # undo the WIP commit, keep the changes
```

**Evasion handling** (shared with `gmail_send_gate.py`): compound commands are
split on `&&`, `||`, `;`, `|`, `&`, and newlines and judged per segment;
backslash line continuations are folded first; `git -C`, `--git-dir`,
`--work-tree`, and `-c` may precede the subcommand; path-qualified binaries
(`/usr/bin/git`) match. Interpreters and heredoc readers are **not** exempt —
`python3 -c`, `node -e`, `sh -c`, and `cat` were live bypasses in an early
revision of the gmail gate. A second pattern catches the argv-list form
(`subprocess.run(['git','stash','pop'])`), where quoting rather than whitespace
separates the tokens; that was the one live bypass an adversarial pass found
against this hook's first revision.

Text-bearing leaders (`git commit -m`, `echo`, `printf`, `grep`, `rg`,
`gh pr create`) carry the pattern as prose and are skipped — but only for their
own segment, so a real invocation chained after one is still caught.

**Override:** prefix `ATELES_ALLOW_GIT_STASH=1` to that single invocation.
Unlike `sibling_repo_worktree_guard.py`'s exported
`ATELES_ALLOW_SHARED_REPO_WRITES=1`, this is parsed out of the command string
itself, following the gmail gate. Hooks fire *before* the shell runs, so an
inline `VAR=1 cmd` prefix never reaches `os.environ` in the hook's own process —
only an `export` would, and an export approves every stash for the rest of the
session. The inline prefix is scoped to the segment it prefixes and is re-typed
per command; an ambient/exported value is deliberately ignored.

**Fail-open:** any internal error or unparseable hook input allows the call
through, with a non-blocking stderr notice.

**Tests:** `test_git_stash_guard.py` — 85 cases covering every mutating
subcommand, the read-only carve-out, git-level path flags, compound chains,
interpreter and argv-list bypasses, wrapper/substitution/subshell forms,
text-bearing-leader exemptions, override scoping (including that an exported
value does not approve), and fail-open on malformed input. The suite pipes
synthetic JSON to the hook's stdin and never runs `git` — writing it does not
stash anything.
