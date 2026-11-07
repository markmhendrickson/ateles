# Pruning stale linked git worktrees

## Purpose

Linked git worktrees pile up under a clone directory (one per session, agent run or branch) and each
carries its own `node_modules`. `execution/scripts/worktree_prune.py` is the nightly job that gives that
disk space back without ever discarding work that exists only on this machine. This runbook describes what
it removes, what it refuses to touch, how to configure it, how to run its tests, and how to install the
launchd job. Dry run is the default; nothing is deleted without `--apply`.

## Scope

Covers `execution/scripts/worktree_prune.py`, its tests (`execution/scripts/test_worktree_prune.py`) and the
launchd template (`execution/scripts/com.ateles.worktree-prune.plist.template`). It does not cover main
clones, deployment checkouts (see `docs/daemon_rc_autodeploy.md`), branch cleanup on the remote, or disk
cleanup outside git worktrees. Installing the launchd job is a per-host action; merging this code installs
nothing.

## The tiers

Both tiers work on **linked** worktrees only (a worktree whose `.git` is a file pointing into
`<clone>/.git/worktrees/`). Main clones are never touched. Every condition is re-checked immediately before
the tier acts.

| Tier | Flag | What it does | Whole worktree removed? |
|---|---|---|---|
| Plain | always on | `git worktree remove` (no `--force`) | yes |
| node_modules only | `--strip-node-modules`, or `--strip-only` for this tier alone | deletes directories named `node_modules` | no, the worktree stays |

### Plain tier

A worktree is removed only if all of these hold:

1. `git status --porcelain` is empty (no tracked changes, no untracked files).
2. HEAD is contained in an `origin/*` remote-tracking ref, or equals (or is an ancestor of) the head of a
   merged pull request for the worktree's branch. A commit that exists only locally is never eligible: the
   job backs nothing up.
3. No file outside `node_modules`, `.git`, `dist` and `build` was modified in the last **24 hours**
   (`--fresh-hours`, default 24).
4. No process has its working directory inside the worktree (`lsof -d cwd`).
5. The worktree is not locked.
6. It is not detached on a HEAD that is unreachable from origin.
7. It is not on the never-touch list and not under the hold (see Configuration).

If `git worktree remove` refuses, the worktree is kept and the refusal is logged. `git worktree remove`
also deletes git-ignored files in the worktree. `--protect-ignored` additionally keeps any worktree that
holds ignored files which are not obvious regenerable output.

### node_modules-only tier

For a worktree idle for `--idle-hours` (default **24**) or more, delete only directories literally named
`node_modules`, top level and nested. The worktree itself stays. It runs only when all of these hold, each
re-checked immediately before deleting (the process scan last):

- `git status --porcelain` is empty;
- HEAD is contained in an `origin/*` ref or is (an ancestor of) a merged pull request head for the branch,
  the same rule as the plain tier, so a commit that exists only locally is never eligible;
- no file outside `node_modules`, `.git`, `dist` and `build` was modified inside the idle window;
- no process has its working directory inside the worktree;
- the worktree is not locked, not detached on an unreachable HEAD, not on the never-touch list and not held.

What it will never delete:

- anything that is not a directory literally named `node_modules` lying inside the worktree. A directory
  named `node_modules_backup` or a file named `node_modules` is left alone.
- a `node_modules` that is a symlink, whether it points inside or outside the worktree: it is refused and
  reported as `node_modules:symlink`, and its target is untouched. Symlinks are never followed during the
  walk, and symlinks inside a `node_modules` are removed as links, not followed.
- a `node_modules` whose real path leaves the worktree. Containment is checked against the real path for each
  directory right before it is deleted, so a directory swapped for a symlink after planning is refused too.
- a `node_modules` that contains files tracked by git, or one inside a nested git repository.
- a `node_modules` with a `.git` entry (file, directory or link) anywhere beneath it: a repository kept
  inside a dependency tree may hold commits that exist nowhere else. It is refused and reported as
  `node_modules:contains-git`; if the tree cannot be read to check, it is refused too.
- a `node_modules` that is a mount point, or has any directory beneath it on a different device from the
  worktree: refused and reported as `node_modules:mount-point`, so a separately mounted volume is never
  emptied. This is checked at planning time and again immediately before each directory is deleted.

Recovery: a stripped worktree only lost its installed dependencies. Reinstall them with the project's
package manager (for example `npm ci`, `pnpm install` or `yarn install`).

`--strip-only` runs this tier alone: no whole-worktree removal and no `git worktree prune`.
With `--strip-node-modules` the tier runs after the other tiers in the same pass and only on worktrees they
leave in place (for example one idle 30 hours when `--fresh-hours` is 72, or one that `--protect-ignored`
keeps). A worktree the plain tier removes outright is simply removed.

Bytes freed are the apparent size of the deleted files (symlinks counted as links). They are reported per
worktree and in total in the runs log (`strip_detail`, `strip_bytes_freed`; a dry run reports
`would_strip_bytes`).

`--idle-hours` is the idle window of the node_modules tier only (default 24).

### What is deliberately not here

An archive-then-remove tier (write a recovery archive of dirty or unpushed work, then force-remove the
worktree) is deliberately not part of this module. A host that runs one keeps it out of tree until a
separate, reviewed port. Nothing in this module forces a removal: `git worktree remove` is only ever run
without `--force`. The launchd template does not enable any such tier, and the options of the old one
(`--archive-remove`, `--archive-cap-mb`, `--prune-archives`, `--archive-retention-days`, `--archive-root`)
are refused with an error rather than ignored, so a stale command line or template cannot silently run
without the tier it expects.

## Safeties that apply to every run

- Dry run unless `--apply`. A dry run is read-only (`GIT_OPTIONAL_LOCKS=0`).
- Never forces a removal, stashes, deletes branches, pushes or commits. Branches survive worktree removal.
- Refuses any path outside the user's home directory, under `~/Library`, in the deployment checkouts
  (`~/ateles-rc-src`, `~/neotoma-rc-src`) or on the configured never-touch list.
- Single-instance lock; a second run exits quietly.
- Per-run cap `--max` (default 150; the launchd template passes `--max 200`) counts worktrees removed plus worktrees stripped; the rest waits for the
  next run (`over-cap-deferred`).
- Aborts the run (no further deletion) if more than 20% of git and gh calls fail once at least 20 calls
  were made.
- With `--yield-to-file`, `--apply` skips the run while that file changed in the last 30 minutes (a manual
  cleanup is in progress).
- Notes `LOW-DISK(<5GB)` in the log when free space on the home volume is low, and still runs.
- Git configuration cannot hide a change. Every git command the module runs is built in one place with
  `-c status.showUntrackedFiles=all -c status.ignoreSubmodules=none -c core.fsmonitor=false
  -c core.untrackedCache=false` on the command line, which outranks global, system and repository config.
  That covers every cleanliness check and the `git worktree remove` call, whose own re-check of the tree
  therefore sees untracked files too. (Only the `--protect-ignored` listing of ignored files uses
  `status.showUntrackedFiles=normal`, so ignored directories stay collapsed.)
- The state directory is created mode 0700 and `runs.jsonl`, `lock` and the logs mode 0600 (an existing
  looser directory or file is tightened); the process runs with umask 077 and restores it on exit. A symbolic
  link where a state or log file belongs is refused, not followed. Log rotation never follows, deletes or
  truncates a symbolic link.
- Every name and path written to the day log has control characters (newlines included) replaced by
  `\xNN` escapes, so a worktree name cannot add or split log lines.
- The node_modules tier assumes no other process running with the same user rights is modifying the
  worktree while the job runs (creating links, mounting volumes). The containment and mount checks are
  repeated immediately before each directory is deleted, but a change made inside that very short window
  by such a process is not defended against.

## Configuration

Every operator-specific value is configuration, never code. A flag wins over the environment variable,
which wins over the default. Defaults are relative to the user's home directory. The hold is empty by
default.

| Environment variable | Flag | Default | Meaning |
|---|---|---|---|
| `ATELES_WORKTREE_PRUNE_STATE_DIR` | `--state-dir` | `~/.ateles-work/worktree-prune` | lock, `runs.jsonl`, `logs/` |
| `ATELES_WORKTREE_PRUNE_REPOS_DIRS` | `--repos-dir` (repeatable) | `~/repos` | directories whose children are the clones to scan (`os.pathsep`-separated in the variable) |
| `ATELES_WORKTREE_PRUNE_NEVER_TOUCH` | `--never-touch` (repeatable) | none, on top of the two deployment checkouts | extra directories never touched (`os.pathsep`-separated) |
| `ATELES_WORKTREE_PRUNE_HOLD_PREFIXES` | `--hold-prefix` (repeatable) | none | worktree directory-name prefixes to hold (comma-separated in the variable) |
| `ATELES_WORKTREE_PRUNE_HOLD_UNTIL` | `--hold-until` | none | `YYYY-MM-DD` the hold lapses on |
| `ATELES_WORKTREE_PRUNE_YIELD_TO_FILE` | `--yield-to-file` | none (never yields) | skip `--apply` while this file changed in the last 30 minutes |

The hold fails closed: prefixes with no date, or a date that is not `YYYY-MM-DD`, hold indefinitely (an
unreadable date is logged as a warning). With no prefixes nothing is held.

Relative paths are refused. A never-touch or repos-dir entry (flag or environment variable) that is not
absolute, or does not start with `~`, stops the run with an error and exit code 2 before anything is read,
written or deleted: a scheduled job starts in `/`, so a relative entry would silently protect or scan
nothing. A hold prefix must be a directory-name prefix; one containing a path separator is refused the same
way.

## Running it

```
python3 execution/scripts/worktree_prune.py                        # dry run, prints one JSON summary line
python3 execution/scripts/worktree_prune.py --list                 # also lists each would-remove worktree
python3 execution/scripts/worktree_prune.py --strip-only           # dry run of the node_modules tier alone
python3 execution/scripts/worktree_prune.py --strip-only --apply   # delete node_modules where eligible
```

## Self-test

The tests build a bare origin, a clone and linked worktrees inside a temporary `HOME`, so nothing on the
host is read or deleted. They need `git`, `find`, `du` and `lsof` on the path.

```
python3 -m pytest execution/scripts/test_worktree_prune.py -q
```

CI runs them in the `ateles package tests` workflow (`Run execution/scripts tests`).

## Logs

- `<state dir>/runs.jsonl`: one JSON summary per run (status, counts, GB, kept-by-reason, free disk, and
  for the node_modules tier `stripped_worktrees`, `strip_bytes_freed`, `would_strip_bytes`, `strip_kept`
  and a per-worktree `strip_detail`).
- `<state dir>/logs/YYYY-MM-DD.log`: per-worktree decisions (`REMOVED`, `WOULD-REMOVE`, `STRIPPED`,
  `WOULD-STRIP`, `KEEP <reason>`); files older than 30 days are deleted.
- `<state dir>/logs/launchd.{out,err}.log`: stdout and stderr from launchd, truncated when over 1 MB.

## Run status and exit codes

Each run appends one line to `runs.jsonl` whose `status` is one of:

| `status` | Exit code | Meaning |
|---|---|---|
| `ok` | 0 | the run completed |
| `skipped-locked` | 0 | another run holds the lock; nothing was done |
| `skipped-manual-cleanup-active` | 0 | `--yield-to-file` named a file changed in the last 30 minutes |
| `aborted-error-ratio` | 2 | more than 20% of git and gh calls failed; no further deletion happened |
| `crashed` | 1 | an unexpected exception; the line carries a truncated `error` |

## Reasons a worktree was kept

The `kept` map in the run summary (and `KEEP <reason>` in the day log) counts worktrees by why they were left
alone. The node_modules tier has its own map, `strip_kept`.

| Reason | Meaning |
|---|---|
| `never-touch`, `recheck-never-touch` | on the never-touch list, held, outside the home directory, or under `~/Library` |
| `prunable`, `missing`, `not-linked-worktree`, `no-head` | not a usable linked worktree |
| `locked`, `in-use` | locked by git, or a process has its working directory inside |
| `recent` | modified inside the fresh window (plain tier) or the idle window (node_modules tier) |
| `dirty`, `unpushed`, `detached-unreachable` | uncommitted changes, a commit that exists only locally, or a detached HEAD unreachable from origin |
| `head-moved` | HEAD changed between the check and the action |
| `has-ignored-files` | `--protect-ignored` and non-regenerable ignored files are present |
| `find-error`, `status-error`, `recheck-list-error`, `recheck-lsof-error` | a check could not be completed, so the worktree was kept |
| `git-refused` | `git worktree remove` refused (logged with git's message) |
| `over-cap-deferred` | the `--max` cap was reached; next run |
| `no-node-modules`, `refused-node-modules`, `strip-failed` | node_modules tier: nothing to do, every candidate refused (`strip_refused` in the log names each with `symlink`, `not-contained`, `mount-point`, `tracked-files`, `nested-repo`, `contains-git`), or a delete failed part-way |
| `aborted-error-ratio` | the error-abort ratio was reached |

## Installing the launchd job (operator, per host)

The template `execution/scripts/com.ateles.worktree-prune.plist.template` uses two placeholders: `{{HOME}}`
(the home directory) and `{{CHECKOUT}}` (the absolute path of the Ateles checkout that holds the script).
The job runs whatever is in that working tree, so use a checkout that is kept clean and current, not a
development clone. Review the flags in the template first: it applies deletions with `--apply`, runs the
plain tier with `--protect-ignored` and the node_modules tier with `--strip-node-modules` (24 hours idle,
since no `--idle-hours` is passed), and caps a run at `--max 200`. It does not enable any archive tier. It
runs at 03:30 local time, and a Mac that is asleep then runs it on wake.

```
CHECKOUT=/path/to/a/clean/ateles/checkout
mkdir -p "$HOME/.ateles-work/worktree-prune/logs"
sed -e "s|{{HOME}}|$HOME|g" -e "s|{{CHECKOUT}}|$CHECKOUT|g" \
  "$CHECKOUT/execution/scripts/com.ateles.worktree-prune.plist.template" \
  > "$HOME/Library/LaunchAgents/com.ateles.worktree-prune.plist"
plutil -lint "$HOME/Library/LaunchAgents/com.ateles.worktree-prune.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.ateles.worktree-prune.plist"
```

Re-installing over an existing job: run `launchctl bootout "gui/$(id -u)/com.ateles.worktree-prune"` first,
then the `bootstrap` line above (bootstrap fails while the old job is loaded).

Verify it took effect:

```
launchctl print "gui/$(id -u)/com.ateles.worktree-prune" | head
# a dry run of the tiers the nightly job runs, against the real clones
python3 "$CHECKOUT/execution/scripts/worktree_prune.py" --protect-ignored --strip-node-modules --list
```

To run once now (this applies): `launchctl kickstart "gui/$(id -u)/com.ateles.worktree-prune"`.
To disable: `launchctl bootout "gui/$(id -u)/com.ateles.worktree-prune"` and delete the plist.
