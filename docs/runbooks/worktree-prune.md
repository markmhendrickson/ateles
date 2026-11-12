# Pruning stale linked git worktrees

## Purpose

Linked git worktrees pile up under a clone directory (one per session, agent run or branch) and each
carries its own `node_modules`. `execution/scripts/worktree_prune.py` is the nightly job that gives that
disk space back without ever discarding work that exists only on this machine. This runbook describes what
it removes, what it refuses to touch, how to configure it, how to run its tests, and how to install the
launchd job. Dry run is the default; nothing is deleted without `--apply`. The scheduled job is ONE daily
check, `--daily` (next section), which replaces any older nightly cleaner on the host.

## Scope

Covers `execution/scripts/worktree_prune.py`, its tests (`execution/scripts/test_worktree_prune.py`) and the
launchd template (`execution/scripts/com.ateles.worktree-prune.plist.template`). It does not cover main
clones, deployment checkouts (see `docs/daemon_rc_autodeploy.md`), branch cleanup on the remote, or disk
cleanup outside git worktrees. Installing the launchd job is a per-host action; merging this code installs
nothing.

## Daily mode (`--daily`): the one scheduled check

`--daily` is the single documented invocation for the scheduled job: `--daily --apply` deletes, and
`--daily` alone (or with `--dry-run`) only reports, like every mode of this script. It turns on the settings
below together and never does anything outside them:

1. **Whole removal of linked worktrees that are merged and clean**, and nothing else. Merged means HEAD is in
   origin's default branch, or is (an ancestor of) the head of a merged pull request for the branch. A commit
   that is only pushed to some other origin branch is **not** enough (`pushed-not-merged`). Clean means
   `git status` is empty. Immediately before removal, any file under the worktree that a running process holds
   open or mapped keeps it (`in-use`), and so does a failed open-file scan. `--protect-ignored` is on and, in daily mode, strict: a worktree is removed with
   git-ignored files still inside it only if each of them is a **cache directory** (a name from the list in
   item 2) that passes the cache tier's own directory checks (a real directory inside the worktree, no `.git`
   beneath it, no mount point), or a known cache file (`.DS_Store`, `*.pyc`, `*.tsbuildinfo`,
   `*.eslintcache`). The one other exception is the dev env copy that project hooks place in every
   worktree: a file named exactly `.env.development` at the worktree root counts as disposable only if it is a
   regular file (not a link), git ignores it, and it is byte-identical to the same-named file in the main clone
   (compared byte for byte; its contents and any hash of them are never printed, logged or stored). A renamed
   variant, a differing copy, a missing main-clone file or a link keeps the worktree. Any other ignored entry
   (`tmp/`, `public/`, `data/`, logs, a nested repository under a `build/`) keeps the worktree and lists it
   (`has-ignored-files`).
2. **Deleting regenerable cache directories** inside worktrees that pass the strip gate (below): directories
   named like the existing regenerable classification (`node_modules`, `.venv`, `venv`, `dist`, `build`,
   `.next`, `.nuxt`, `.turbo`, `.cache`, `coverage`, `__pycache__`, `.pytest_cache`, `.mypy_cache`,
   `.ruff_cache`, `target`, `.parcel-cache`, `.vite`, `.vitest`, `.dev-serve`, `.mcp-venv`) that git **ignores**
   and holds no tracked file. The worktree stays. A same-named directory git does not ignore is refused
   (`not-git-ignored`) and listed. A cache directory with any file open or mapped by a running process (a
   virtualenv in use, for example, even when that process works elsewhere) is kept (`in-use-by-process`), and
   if the open-file scan itself fails every cache is kept. It does not touch user-level or system caches.
3. **A free-space alert** (see below).
4. **An approval list** of everything it did not act on (see below).

It never removes uncommitted, untracked or local-only work, never passes `--force` to anything, and leaves
worktrees with initialized submodules (which git refuses to remove without force) for approval. Detached
HEADs unreachable from origin are kept. Every condition is re-checked immediately before acting; the 24 hour
quiet period, the in-use (cwd) check, the lock, the never-touch list and the deployment checkouts
(`~/ateles-rc-src`, `~/neotoma-rc-src`) all apply. `--daily` cannot be combined with `--strip-only`.

The pieces are also available separately for ad-hoc use: `--require-merged`, `--strip-caches`,
`--approval-list`, `--alert`.

### Keep marks

Two things keep a worktree out of every tier (removal, cache deletion):

- `git worktree lock <path>` (already honored);
- a file or directory named **`.ateles-keep`** at the worktree root (`touch <worktree>/.ateles-keep`). It need
  not be committed. A kept worktree is listed as kept on purpose (reason `keep-marked`), not as waiting for a
  decision.

### Approval list

Every run writes `<state dir>/approval_list.json` (everything) and `<state dir>/approval_list.md` (short, for
a person), replacing the previous ones. The Markdown prints the two commands once at the top (`git worktree
remove <path>` to approve, `touch <path>/.ateles-keep` to keep), says "Nothing needs your decision." when the
list is empty, shows sizes in MB or GB, and groups items: first "Work that may exist only on this Mac"
(uncommitted or untracked files, commits never uploaded, unmerged branches, untracked files that cannot be
rebuilt), then other items waiting for a decision, then items kept on purpose (not counted as waiting). Each
line shows the branch, path, size and a plain-language reason. The JSON carries the same with the reason
code, `group`, `decision_count` and `decision_kb`. The run takes **no action** on a listed item.

### Free-space alert

When free space on the home volume falls below `ATELES_WORKTREE_PRUNE_ALERT_GIB` (`--alert-gib`, default
**25** GiB; `0` disables) the run alerts. Once an alert is **delivered** it stays quiet while space remains
low, and re-arms only after free space recovers to the threshold plus `ATELES_WORKTREE_PRUNE_ALERT_REARM_GIB`
(`--alert-rearm-gib`, default **10**); recovery inside that band does not re-arm. A send that failed, could not
be attempted (notifier unavailable) or was not reported as sent does **not** disarm: it is recorded and tried
again on the next run (so, if the notifier holds the message for later and also reports not-sent, a later run
may send it again). Free space below 5 GiB is critical severity. State is `<state dir>/alert_state.json`.

The text is written for someone reading it on a phone: a plain first sentence with free space in GB; what this
run already cleared or removed and about how much it freed (or "freed nothing"); that nothing else was
deleted, with the count and size of items waiting for a decision; the next step (optional and safe, urgent
wording when critical); how to keep a folder; and the list's path last.

Each attempt is written to `<state dir>/alerts.jsonl` (the durable record, with `delivered`, written whether
or not delivery works), and every run's `runs.jsonl` line carries `alert` and `alert_severity` (0 = fine,
1 = below threshold, 2 = critical), also while the alert is suppressed. Delivery goes through the repo's
existing operator notifier, `lib/notify` (`Notifier.send`, Apprise/Telegram-backed, called with
`email_eligible=False`), imported lazily; this job adds no credentials, transport or scheduler. The notifier
needs an interpreter with its dependencies and its own transport settings in the job's environment: the
daemon venv (`~/ateles-rc-src/.venv`, from `execution/scripts/daemon-requirements.txt`) has `httpx` but
not `apprise`, which the notifier needs to send at all (without it the notifier only logs). This change does
**not** touch that requirements file: adding a notifier transport to the shared deployment venv would switch on
real alert delivery for the other daemons that use the same notifier too, which changes how the swarm
notifies the operator and is a separate operator decision. Until that is decided, an alert is recorded
durably in `alerts.jsonl` with `delivered: false` and retried on every run; nothing is lost and nothing is
silently dropped. Real delivery also needs the transport settings in the job's environment, and stays
**unproven until it is activated and verified on the host**. If it cannot deliver, the record says so (`delivered: false`) and the next run retries.

The job is scheduled at **08:30**, outside the notifier's 22:00 to 08:00 silence window, so a warn-level alert
is delivered instead of being held until some other process drains the held queue.

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

With `--daily`, the node_modules tier becomes the cache tier described above (same gate, more directory
names, each also required to be git-ignored). Locked and keep-marked worktrees are skipped by it too.

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
python3 execution/scripts/worktree_prune.py --daily                 # the daily check, report only (dry run)
python3 execution/scripts/worktree_prune.py --daily --apply         # the daily check, applying (what launchd runs)
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
- `<state dir>/approval_list.json` and `approval_list.md`: the latest approval list (daily mode).
- `<state dir>/alerts.jsonl` and `alert_state.json`: alert records and the armed/disarmed state (daily mode).
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
| `keep-marked` | the worktree root holds a `.ateles-keep` marker |
| `in-use-by-process` | cache tier: a running process has a file open inside that cache directory |
| `pushed-not-merged` | `--require-merged` / `--daily`: pushed to a non-default origin ref but not merged |
| `has-submodules` | an initialized submodule is present; git would refuse removal and this job never forces |
| `find-error`, `status-error`, `recheck-list-error`, `recheck-lsof-error` | a check could not be completed, so the worktree was kept |
| `git-refused` | `git worktree remove` refused (logged with git's message) |
| `over-cap-deferred` | the `--max` cap was reached; next run |
| `no-node-modules`, `refused-node-modules`, `strip-failed` | node_modules tier: nothing to do, every candidate refused (`strip_refused` in the log names each with `symlink`, `not-contained`, `mount-point`, `tracked-files`, `nested-repo`, `contains-git`), or a delete failed part-way |
| `aborted-error-ratio` | the error-abort ratio was reached |

## Installing the launchd job (operator, per host)

The template `execution/scripts/com.ateles.worktree-prune.plist.template` uses two placeholders: `{{HOME}}`
(the home directory) and `{{CHECKOUT}}` (the absolute path of the Ateles checkout that holds the script).
The job runs whatever is in that working tree, so use a checkout that is kept clean and current, not a
development clone. Review the flags in the template first: it runs `--daily --apply` (merged-only whole removal with strict
`--protect-ignored`, 24 hours idle for the cache tier, the alert and the approval list) and caps a run at
`--max 200`. It does not enable any archive tier and does not reference any script outside the repo. It
uses the daemon convention for the interpreter, `{{CHECKOUT}}/.venv/bin/python3` (the deployment checkout,
`~/ateles-rc-src`, as `docs/daemon_rc_autodeploy.md` prescribes), not `/usr/bin/python3`. It runs at 08:30
local time, and a Mac that is asleep then runs it on wake. For the alert to be delivered, the notifier's
transport settings must be in `EnvironmentVariables` and the interpreter's venv must have a notifier
transport (the deployment venv does not today; that is a separate operator decision); otherwise alerts are
only recorded in `alerts.jsonl` (`delivered: false`) and retried on each run.

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
# a dry run of the daily check against the real clones (deletes nothing); then read the approval list it wrote
"$CHECKOUT/.venv/bin/python3" "$CHECKOUT/execution/scripts/worktree_prune.py" --daily --list
less "$HOME/.ateles-work/worktree-prune/approval_list.md"
```

To run once now (this applies): `launchctl kickstart "gui/$(id -u)/com.ateles.worktree-prune"`.
To disable: `launchctl bootout "gui/$(id -u)/com.ateles.worktree-prune"` and delete the plist.

## Retiring the older nightly cleaners (host step, after this merges)

The host previously ran two nightly jobs: `com.ateles.worktree-prune` (03:30) running an unreviewed
off-repo script with an archive-then-force-remove tier, and `com.ateles.worktree-prune-strip` (04:30)
running this script without `--strip-only`, which also removed whole worktrees. The daily check above
replaces both. Retiring them is an operator step on the host, done only after this change has merged and
the checkout the job runs from has been updated; merging changes nothing on any host.

```
launchctl bootout "gui/$(id -u)/com.ateles.worktree-prune" 2>/dev/null
launchctl bootout "gui/$(id -u)/com.ateles.worktree-prune-strip" 2>/dev/null
rm -f "$HOME/Library/LaunchAgents/com.ateles.worktree-prune-strip.plist"
# then install the daily job from the template (previous section) under the label com.ateles.worktree-prune
```

Verify: `launchctl list | grep worktree-prune` shows exactly one job, its `ProgramArguments` contain `--daily`
and no path outside the checkout, and a `--daily` dry run writes `approval_list.md`. Verify the alert with
dry runs and one test message, never with an applying run:

```
# 1. the wording and the firing logic: a dry run with a high threshold reports "would-fire" and shows the
#    message in runs.jsonl / the printed summary; it sends and writes nothing and deletes nothing
ATELES_WORKTREE_PRUNE_ALERT_GIB=100000 "$CHECKOUT/.venv/bin/python3" \
  "$CHECKOUT/execution/scripts/worktree_prune.py" --daily
# 2. delivery: send one plain test message through the same notifier, with the same interpreter and the
#    environment the job uses; True means the notifier reports it sent, and a phone must show it arrive
"$CHECKOUT/.venv/bin/python3" -c "import sys; sys.path.insert(0, '$CHECKOUT/execution/scripts'); \
import worktree_prune as w; print(w.send_notification('Worktree prune: delivery test, please ignore.', 1))"
```

Keep the old off-repo script and its archives until the operator has reviewed them; deleting them is a
separate decision.
