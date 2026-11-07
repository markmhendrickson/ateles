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

All tiers work on **linked** worktrees only (a worktree whose `.git` is a file pointing into
`<clone>/.git/worktrees/`). Main clones are never touched. Every condition is re-checked immediately before
the tier acts.

| Tier | Flag | What it does | Whole worktree removed? |
|---|---|---|---|
| Plain | always on | `git worktree remove` (no `--force`) | yes |
| node_modules only | `--strip-node-modules`, or `--strip-only` for this tier alone | deletes directories named `node_modules` | no, the worktree stays |
| Archive then remove | `--archive-remove` | verified recovery archive, then `git worktree remove --force` | yes |

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

`--strip-only` runs this tier alone: no whole-worktree removal, no archive tier, no `git worktree prune`.
With `--strip-node-modules` the tier runs after the other tiers in the same pass and only on worktrees they
leave in place (for example one idle 30 hours when `--fresh-hours` is 72, or one that `--protect-ignored`
keeps). A worktree the plain tier removes outright is simply removed.

Bytes freed are the apparent size of the deleted files (symlinks counted as links). They are reported per
worktree and in total in the runs log (`strip_detail`, `strip_bytes_freed`; a dry run reports
`would_strip_bytes`).

### Archive-then-remove tier

Off by default. A worktree idle for `--idle-hours` (default **72** for this tier when the flag is not given)
that the plain tier keeps as dirty, unpushed or detached (or that holds non-regenerable ignored files under
`--protect-ignored`) is first written to a verified archive under the archive root, outside every repo,
mode 0700: a bundle of local-only commits, `patch.diff` (staged and unstaged tracked changes), a tarball of
untracked and small non-regenerable ignored files, and `meta.json` with a restore hint. Archives over
`--archive-cap-mb` (default 25) are skipped, never truncated. Only if the worktree is unchanged after
archiving is it removed with `git worktree remove --force`; that flag appears nowhere else in the module.
`--prune-archives` lists archive day directories older than 30 days (`--archive-retention-days`); with
`--apply` it deletes them.

Restore: in the main clone, `git worktree add --detach <dir> <head>`; if a bundle exists,
`git fetch <archive>/local-commits.bundle HEAD:refs/heads/restored` first; then
`git -C <dir> apply <archive>/patch.diff` and `tar -xzf <archive>/untracked.tar.gz -C <dir>`.

`--idle-hours` is one flag for both tiers. Left unset it means 24 hours for the node_modules tier and 72
for the archive tier; a value given on the command line applies to both.

## Safeties that apply to every run

- Dry run unless `--apply`. A dry run is read-only (`GIT_OPTIONAL_LOCKS=0`; archives for the archive tier
  are built in a throwaway directory and discarded).
- Never stashes, deletes branches, pushes or commits. Branches survive worktree removal.
- Refuses any path outside the user's home directory, under `~/Library`, in the deployment checkouts
  (`~/ateles-rc-src`, `~/neotoma-rc-src`) or on the configured never-touch list.
- Single-instance lock; a second run exits quietly.
- Per-run cap `--max` (default 150) counts worktrees removed plus worktrees stripped; the rest waits for the
  next run (`over-cap-deferred`).
- Aborts the run (no further deletion) if more than 20% of git and gh calls fail once at least 20 calls
  were made.
- With `--yield-to-file`, `--apply` skips the run while that file changed in the last 30 minutes (a manual
  cleanup is in progress).
- Notes `LOW-DISK(<5GB)` in the log when free space on the home volume is low, and still runs.

## Configuration

Every operator-specific value is configuration, never code. A flag wins over the environment variable,
which wins over the default. Defaults are relative to the user's home directory. The hold is empty by
default.

| Environment variable | Flag | Default | Meaning |
|---|---|---|---|
| `ATELES_WORKTREE_PRUNE_STATE_DIR` | `--state-dir` | `~/.ateles-work/worktree-prune` | lock, `runs.jsonl`, `logs/` |
| `ATELES_WORKTREE_PRUNE_ARCHIVE_ROOT` | `--archive-root` | `~/.ateles-work/worktree-archive` | archive tier output (must be outside every repo) |
| `ATELES_WORKTREE_PRUNE_REPOS_DIRS` | `--repos-dir` (repeatable) | `~/repos` | directories whose children are the clones to scan (`os.pathsep`-separated in the variable) |
| `ATELES_WORKTREE_PRUNE_NEVER_TOUCH` | `--never-touch` (repeatable) | none, on top of the two deployment checkouts | extra directories never touched (`os.pathsep`-separated) |
| `ATELES_WORKTREE_PRUNE_HOLD_PREFIXES` | `--hold-prefix` (repeatable) | none | worktree directory-name prefixes to hold (comma-separated in the variable) |
| `ATELES_WORKTREE_PRUNE_HOLD_UNTIL` | `--hold-until` | none | `YYYY-MM-DD` the hold lapses on |
| `ATELES_WORKTREE_PRUNE_YIELD_TO_FILE` | `--yield-to-file` | none (never yields) | skip `--apply` while this file changed in the last 30 minutes |

The hold fails closed: prefixes with no date, or a date that is not `YYYY-MM-DD`, hold indefinitely (an
unreadable date is logged as a warning). With no prefixes nothing is held.

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

## Installing the launchd job (operator, per host)

The template `execution/scripts/com.ateles.worktree-prune.plist.template` uses two placeholders: `{{HOME}}`
(the home directory) and `{{CHECKOUT}}` (the absolute path of the Ateles checkout that holds the script).
The job runs whatever is in that working tree, so use a checkout that is kept clean and current, not a
development clone. Review the flags in the template first: it applies deletions (`--apply`) with every tier
on, runs at 03:30 local time, and a Mac that is asleep then runs it on wake.

```
CHECKOUT=/path/to/a/clean/ateles/checkout
mkdir -p "$HOME/.ateles-work/worktree-prune/logs"
sed -e "s|{{HOME}}|$HOME|g" -e "s|{{CHECKOUT}}|$CHECKOUT|g" \
  "$CHECKOUT/execution/scripts/com.ateles.worktree-prune.plist.template" \
  > "$HOME/Library/LaunchAgents/com.ateles.worktree-prune.plist"
plutil -lint "$HOME/Library/LaunchAgents/com.ateles.worktree-prune.plist"
launchctl bootstrap "gui/$(id -u)" "$HOME/Library/LaunchAgents/com.ateles.worktree-prune.plist"
```

Verify it took effect:

```
launchctl print "gui/$(id -u)/com.ateles.worktree-prune" | head
python3 "$CHECKOUT/execution/scripts/worktree_prune.py" --list     # a dry run against the real clones
```

To run once now (this applies): `launchctl kickstart "gui/$(id -u)/com.ateles.worktree-prune"`.
To disable: `launchctl bootout "gui/$(id -u)/com.ateles.worktree-prune"` and delete the plist.
