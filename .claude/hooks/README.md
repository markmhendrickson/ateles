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
