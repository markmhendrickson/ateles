#!/usr/bin/env python3
"""Nightly prune of stale, fully-pushed, inactive linked git worktrees.

Default is DRY RUN. `--apply` actually removes or deletes. stdlib only.

Two tiers. Every tier works on LINKED worktrees only (never a main clone) and
re-checks every condition immediately before it acts.

1. Plain tier (always on). A linked worktree is removed ONLY if every one of
   these holds, each re-checked immediately before removal:
     a) `git status --porcelain` is empty
     b) HEAD is contained in an origin/* remote-tracking ref, OR HEAD equals (or is an
        ancestor of) the head of a merged PR for the worktree's branch.
        A commit that exists only locally is NEVER eligible. This job backs nothing up.
     c) no file (outside node_modules/.git/dist/build) modified in the last 24h
        (`--fresh-hours`, default 24)
     d) no process has its cwd inside the worktree (lsof -d cwd)
     e) not locked
     f) not detached on a HEAD that is unreachable from origin
   Removal is `git worktree remove <wt>` WITHOUT --force; if git refuses the worktree is kept.
   This script never forces a removal, stashes, deletes branches, pushes, or commits.
   `--protect-ignored` additionally keeps worktrees holding git-ignored files that are not
   obvious regenerable output.

2. node_modules-only tier (`--strip-node-modules`, off by default). For a linked worktree
   idle for `--idle-hours` (default 24) or more, delete ONLY directories literally named
   `node_modules` (top level and nested; symlinks are never followed and a `node_modules`
   that is a symlink or a mount point, or spans a device boundary, is refused), when ALL
   of these hold, each re-checked immediately before deleting:
     a) `git status --porcelain` is empty
     b) HEAD is contained in an origin/* ref or is (an ancestor of) a merged PR head
        for the branch (same rule as the plain tier)
     c) no file modified within the idle window outside node_modules/.git/dist/build
     d) no process has its cwd inside the worktree
     e) not locked
     f) not detached on a HEAD unreachable from origin
     g) not on the never-touch list and not under the configurable hold
   The worktree itself stays. Bytes freed are reported per worktree and in total in the
   runs log. `--strip-only` runs this tier alone (no whole-worktree removal).

A tier that archives work before removing it is deliberately not part of this module.

Daily mode (`--daily`) bundles the safe automatic subset for one scheduled job, and nothing else:
  - whole removal of linked worktrees that are MERGED (HEAD in origin's default branch or a merged PR head)
    and clean, with --protect-ignored on, no --force, and submodule worktrees left for approval;
  - deleting regenerable cache directories (the REGEN_ANY_DEPTH directory names: node_modules, .venv,
    dist, build, ...) that git ignores and does not track, inside worktrees that pass the strip gate;
  - a free-space alert when the home volume drops below ATELES_WORKTREE_PRUNE_ALERT_GIB (default 25);
  - an approval list (approval_list.json and approval_list.md in the state dir) of every worktree or cache
    candidate it did NOT act on, with size and the exact reason. It takes no action on those.
A worktree holding a keep marker (a `.ateles-keep` file at its root) or locked by `git worktree lock` is
never touched by any tier; it appears on the approval list as kept.

Operator-specific values are configuration, never code. Each can be set by environment
variable or CLI flag (flag wins); the defaults are relative to the user's home directory:
  ATELES_WORKTREE_PRUNE_STATE_DIR      lock, runs.jsonl, logs   (--state-dir)
  ATELES_WORKTREE_PRUNE_REPOS_DIRS     clone parents, os.pathsep-separated (--repos-dir, repeatable)
  ATELES_WORKTREE_PRUNE_NEVER_TOUCH    extra never-touch dirs, os.pathsep-separated (--never-touch)
  ATELES_WORKTREE_PRUNE_HOLD_PREFIXES  comma-separated worktree directory-name prefixes to hold (--hold-prefix)
  ATELES_WORKTREE_PRUNE_HOLD_UNTIL     YYYY-MM-DD the hold lapses on  (--hold-until)
  ATELES_WORKTREE_PRUNE_YIELD_TO_FILE  --apply yields while this file changed in the last 30 minutes (--yield-to-file)
  ATELES_WORKTREE_PRUNE_ALERT_GIB      free-space alert threshold in GiB, default 25; 0 disables  (--alert-gib)
  ATELES_WORKTREE_PRUNE_ALERT_REARM_GIB  GiB above the threshold the alert re-arms at, default 10  (--alert-rearm-gib)
"""

import argparse
import datetime
import filecmp
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

ENV_PREFIX = "ATELES_WORKTREE_PRUNE_"
DEFAULT_FRESH_HOURS = 24
DEFAULT_STRIP_IDLE_HOURS = 24
DEFAULT_ALERT_GIB = 25.0
DEFAULT_ALERT_REARM_GIB = 10.0
CRITICAL_FREE_GIB = 5.0
# A file or directory with this name at a worktree's root marks it as kept: no tier ever touches it.
KEEP_MARKER = ".ateles-keep"
ERR_ABORT_RATIO = 0.20
ERR_MIN_CALLS = 20
# Deployment checkouts the swarm's daemons run from (documented in the repo's CLAUDE.md). Never touched,
# whatever the configuration says; ATELES_WORKTREE_PRUNE_NEVER_TOUCH adds to this list.
DEFAULT_NEVER_TOUCH = ("~/ateles-rc-src", "~/neotoma-rc-src")

# Resolved by configure(); module-level so the helpers below stay simple.
H = os.path.expanduser("~")
REPOS_DIRS = []
BASE = ""
LOGDIR = ""
RUNS = ""
LOCK = ""
MANUAL_PROGRESS = None
NEVER = []
HOLD_PREFIXES = ()
HOLD_UNTIL = None  # (y, m, d) or None. None with prefixes set means "hold indefinitely" (fails closed).
FRESH_HOURS = DEFAULT_FRESH_HOURS
# Settings of the optional node_modules-only tier. Off unless --strip-node-modules / --strip-only is passed.
STRIP = {
    "enabled": False,
    "only": False,
    "caches": False,
    "idle_hours": DEFAULT_STRIP_IDLE_HOURS,
}
STRIP_DIR_NAME = "node_modules"
# Daily-mode switches (set from the CLI in _main; all off by default so existing invocations are unchanged).
DAILY = {"merged_only": False, "approval": False, "alert": False}
ALERT_GIB = DEFAULT_ALERT_GIB
ALERT_REARM_GIB = DEFAULT_ALERT_REARM_GIB
APPROVAL = []  # approval-list items collected during a run

# Regenerable = never worth protecting (used by --protect-ignored).
REGEN_ANY_DEPTH = {
    "node_modules",
    "dist",
    "build",
    ".next",
    ".nuxt",
    ".turbo",
    ".cache",
    "coverage",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".DS_Store",
    "target",
    ".parcel-cache",
    ".vite",
    ".vitest",
    ".dev-serve",
    ".mcp-venv",
}
REGEN_TOP_LEVEL = ("tmp", "public", "site_pages", ".husky", "docs/site/generated")
REGEN_SUFFIXES = (".pyc", ".tsbuildinfo", ".log", ".eslintcache")
ENV_COPY_PREFIX = ".env.development"  # also catches renamed copies (.notloaded, .bak): regenerable by name
REGEN_NAMES = (
    ".env.development",
)  # husky-copied by scripts/copy-env-to-worktree.js; name only, never read
DATA_REGEN_CHILDREN = ("logs", "sources", "tmp")
# Directory names the cache tier may delete (daily mode): the directory entries of REGEN_ANY_DEPTH. A name is
# necessary, never sufficient: the directory must also be git-ignored and hold no tracked file.
CACHE_DIR_NAMES = frozenset(REGEN_ANY_DEPTH - {".DS_Store"})

# launchd starts jobs with a minimal PATH; add the usual Homebrew / system locations for git, gh and lsof.
TOOL_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
# Git context variables a parent process (a git hook, another git command) may export. Passed on to the
# git subprocesses below they would point every command at some other repository, so they are dropped.
GIT_CONTEXT_VARS = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
    "GIT_NAMESPACE",
    "GIT_CEILING_DIRECTORIES",
)


def build_git_env(parent):
    """The environment for git/gh/lsof subprocesses: the parent's minus inherited git context.

    GIT_OPTIONAL_LOCKS=0: read-only `git status` must not take index locks in other sessions' worktrees.
    """
    env = {k: v for k, v in parent.items() if k not in GIT_CONTEXT_VARS}
    env.update(
        GIT_OPTIONAL_LOCKS="0",
        GIT_TERMINAL_PROMPT="0",
        GH_PROMPT_DISABLED="1",
        PATH=TOOL_PATH + ":" + parent.get("PATH", ""),
    )
    return env


GIT_ENV = build_git_env(os.environ)

# Configuration that can make git report a changed tree as clean. Every git call in this module is built
# by git_cmd() below, which puts these on the command line (the highest-precedence source), so no global,
# system or repository setting can hide an untracked file or a change from the cleanliness checks or from
# the checks `git worktree remove` makes itself.
GIT_SAFE_CONFIG = (
    "status.ignoreSubmodules=none",
    "core.fsmonitor=false",
    "core.untrackedCache=false",
)


def git_cmd(repo, *args, untracked="all"):
    """The one place a git command line is built: `git -c <safe config> -C <repo> <args>`.

    `untracked` is the status.showUntrackedFiles value; "all" for every cleanliness decision and for
    `worktree remove`. Listing ignored files (--protect-ignored) passes "normal" so ignored directories
    stay collapsed to one entry instead of one entry per file.
    """
    cmd = ["git", "-c", "status.showUntrackedFiles=" + untracked]
    for setting in GIT_SAFE_CONFIG:
        cmd += ["-c", setting]
    return cmd + ["-C", repo] + list(args)


# ----------------------------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------------------------
class ConfigError(ValueError):
    """A configuration value that would silently do the wrong thing; the run stops before doing anything."""


def _path_list(value, label):
    """os.pathsep-separated paths -> absolute, ~-expanded, de-duplicated list (empty entries dropped).

    A relative entry is an error, never resolved against the working directory: a scheduled job starts in
    `/`, so a relative never-touch entry would silently protect nothing.
    """
    out = []
    for part in (value or "").split(os.pathsep):
        part = part.strip()
        if part:
            expanded = os.path.expanduser(part)
            if not os.path.isabs(expanded):
                raise ConfigError(
                    "%s entry %r is a relative path; use an absolute path (or one starting with ~)"
                    % (label, part)
                )
            p = os.path.abspath(expanded)
            if p not in out:
                out.append(p)
    return out


def _parse_hold_until(text):
    """'YYYY-MM-DD' -> (y, m, d); None if empty. Raises ValueError on garbage (the caller fails closed)."""
    text = (text or "").strip()
    if not text:
        return None
    d = datetime.date.fromisoformat(text)
    return (d.year, d.month, d.day)


def configure(env=None, cli=None):
    """(Re)resolve every module-level setting. Precedence: CLI value, then environment, then default.

    The hold fails closed: prefixes with no (or an unreadable) expiry date hold indefinitely.
    Returns a list of warning strings (also logged once the log is open).
    """
    global \
        H, \
        REPOS_DIRS, \
        BASE, \
        LOGDIR, \
        RUNS, \
        LOCK, \
        MANUAL_PROGRESS, \
        NEVER, \
        HOLD_PREFIXES, \
        HOLD_UNTIL, \
        ALERT_GIB, \
        ALERT_REARM_GIB
    env = os.environ if env is None else env
    cli = cli or {}
    warnings = []
    H = os.path.realpath(os.path.expanduser("~"))

    def pick(name, flag):
        return cli.get(flag) or env.get(ENV_PREFIX + name)

    BASE = os.path.abspath(
        os.path.expanduser(
            pick("STATE_DIR", "state_dir")
            or os.path.join(H, ".ateles-work", "worktree-prune")
        )
    )
    LOGDIR = os.path.join(BASE, "logs")
    RUNS = os.path.join(BASE, "runs.jsonl")
    LOCK = os.path.join(BASE, "lock")
    repos = _path_list(
        os.pathsep.join(cli.get("repos_dir") or []), "--repos-dir"
    ) or _path_list(env.get(ENV_PREFIX + "REPOS_DIRS"), ENV_PREFIX + "REPOS_DIRS")
    REPOS_DIRS = repos or [os.path.join(H, "repos")]
    never = [
        os.path.join(H, p[2:]) for p in DEFAULT_NEVER_TOUCH
    ]  # entries are '~/<name>'
    for p in _path_list(
        env.get(ENV_PREFIX + "NEVER_TOUCH"), ENV_PREFIX + "NEVER_TOUCH"
    ) + _path_list(os.pathsep.join(cli.get("never_touch") or []), "--never-touch"):
        if p not in never:
            never.append(p)
    NEVER = never
    prefixes = [
        p.strip()
        for p in (env.get(ENV_PREFIX + "HOLD_PREFIXES") or "").split(",")
        if p.strip()
    ]
    prefixes += [p for p in (cli.get("hold_prefix") or []) if p]
    for p in prefixes:
        # a prefix is matched against the worktree directory's own name; a path never matches anything
        if os.sep in p:
            raise ConfigError(
                "hold prefix %r is a path, not a directory-name prefix; give the start of the worktree "
                "directory's name (for example 'wt-')" % p
            )
    HOLD_PREFIXES = tuple(dict.fromkeys(prefixes))
    until_text = pick("HOLD_UNTIL", "hold_until")
    try:
        HOLD_UNTIL = _parse_hold_until(until_text)
    except ValueError:
        HOLD_UNTIL = None
        if HOLD_PREFIXES:
            warnings.append(
                "hold-until %r is not YYYY-MM-DD: holding %s indefinitely"
                % (until_text, ",".join(HOLD_PREFIXES))
            )
    yield_file = pick("YIELD_TO_FILE", "yield_to_file")
    MANUAL_PROGRESS = (
        os.path.abspath(os.path.expanduser(yield_file)) if yield_file else None
    )
    for name, flag, default in (
        ("ALERT_GIB", "alert_gib", DEFAULT_ALERT_GIB),
        ("ALERT_REARM_GIB", "alert_rearm_gib", DEFAULT_ALERT_REARM_GIB),
    ):
        raw = pick(name, flag)
        value = default
        if raw not in (None, ""):
            try:
                value = float(raw)
                if value < 0 or value != value:
                    raise ValueError(raw)
            except ValueError:
                warnings.append(
                    "%s%s %r is not a non-negative number: using %s"
                    % (ENV_PREFIX, name, raw, default)
                )
                value = default
        if name == "ALERT_GIB":
            ALERT_GIB = value
        else:
            ALERT_REARM_GIB = value
    return warnings


def regen_path(rel):
    """True if a (relative) ignored path is regenerable output by name."""
    rel = rel.rstrip("/")
    parts = rel.split("/")
    if any(p in REGEN_ANY_DEPTH for p in parts):
        return True
    for t in REGEN_TOP_LEVEL:
        if rel == t or rel.startswith(t + "/"):
            return True
    for i in range(len(parts) - 1):  # data/logs, data/sources, data/tmp at any depth
        if parts[i] == "data" and parts[i + 1] in DATA_REGEN_CHILDREN:
            return True
    base = parts[-1]
    return (
        base in REGEN_NAMES
        or base.startswith(ENV_COPY_PREFIX)
        or base.endswith(REGEN_SUFFIXES)
    )


def ignored_is_regen(wt, entry):
    """Regenerable ignored entry. A `data/` dir counts only if it holds nothing but logs/sources/tmp subdirs."""
    rel = entry.rstrip("/")
    if regen_path(rel):
        return True
    if entry.endswith("/") and os.path.basename(rel) == "data":
        try:
            for n in os.listdir(os.path.join(wt, rel)):
                p = os.path.join(wt, rel, n)
                if (
                    n not in DATA_REGEN_CHILDREN
                    or os.path.islink(p)
                    or not os.path.isdir(p)
                ):
                    return False
            return True
        except OSError:
            return False
    return False


class Stats:
    def __init__(self):
        self.calls = 0
        self.errors = 0
        self.aborted = False

    def note(self, ok):
        self.calls += 1
        if not ok:
            self.errors += 1
            if (
                self.calls >= ERR_MIN_CALLS
                and self.errors / self.calls > ERR_ABORT_RATIO
            ):
                self.aborted = True


STATS = Stats()
LOGF = None


_CONTROL_CHARS = re.compile("[\x00-\x1f\x7f-\x9f  ]")


def escape_text(text):
    """Make text safe to put on one log line: every control character (newline included) becomes \\xNN."""
    return _CONTROL_CHARS.sub(lambda m: "\\x%02x" % ord(m.group()), str(text))


def log(*a):
    """Write one line to the day log. Every name and path in a line is escaped here, so no caller can
    forge or split a log line by way of a worktree name."""
    if LOGF:
        line = " ".join(escape_text(x) for x in a)
        print(time.strftime("%H:%M:%S"), line, file=LOGF, flush=True)


PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def ensure_private_dir(path):
    """Create `path` (and parents) mode 0700 and tighten an existing real directory to 0700."""
    os.makedirs(path, mode=PRIVATE_DIR_MODE, exist_ok=True)
    if not os.path.islink(path):
        os.chmod(path, PRIVATE_DIR_MODE)


def open_private(path, mode="a"):
    """Open `path` for appending (mode 'a' or 'a+') as a regular file with mode 0600.

    A symbolic link at `path` is refused (O_NOFOLLOW) rather than followed, and an existing file is
    tightened to 0600.
    """
    flags = os.O_APPEND | os.O_CREAT | (os.O_RDWR if "+" in mode else os.O_WRONLY)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, PRIVATE_FILE_MODE)
    try:
        os.fchmod(fd, PRIVATE_FILE_MODE)
    except OSError:
        os.close(fd)
        raise
    return os.fdopen(fd, mode, buffering=1)


def run(args, cwd=None, timeout=300, count=True, ok_rcs=(0,)):
    """Run a command -> (rc, stdout, stderr). Failures count toward the broad-failure abort."""
    try:
        p = subprocess.run(
            args, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=GIT_ENV
        )
        rc, o, e = p.returncode, p.stdout, p.stderr
    except Exception as ex:  # timeout, missing binary
        rc, o, e = -1, "", str(ex)
    if count:
        STATS.note(rc in ok_rcs)
    return rc, o, e


def dfree_gb():
    try:
        return shutil.disk_usage(H).free / 1024**3
    except Exception:
        return -1.0


def cwdset():
    rc, o, _ = run(["lsof", "-a", "-d", "cwd", "-Fn"], timeout=180, count=False)
    return rc, set(line[1:] for line in o.splitlines() if line.startswith("n"))


def open_file_paths():
    """(ok, set of absolute paths any process has open, mapped or as its cwd). One system-wide lsof scan.
    ok is False if the scan failed: callers must then keep everything."""
    rc, o, _ = run(["lsof", "-n", "-w", "-Fn"], timeout=300, count=False)
    paths = set(line[1:] for line in o.splitlines() if line.startswith("n"))
    return (rc == 0 or bool(paths)), paths


def path_open(path, open_paths):
    rp = os.path.realpath(path)
    return any(p == q or p.startswith(q + "/") for p in open_paths for q in (path, rp))


def in_use(wt, cwds):
    rw = os.path.realpath(wt)
    return any(c == w or c.startswith(w + "/") for c in cwds for w in (wt, rw))


def discover_mains():
    out = []
    never_real = [os.path.realpath(x) for x in NEVER]
    for repos in REPOS_DIRS:
        try:
            names = sorted(os.listdir(repos))
        except Exception:
            continue
        for n in names:
            p = os.path.join(repos, n)
            if (
                os.path.isdir(p + "/.git")
                and not os.path.islink(p)
                and os.path.realpath(p) not in never_real
            ):
                if p not in out:
                    out.append(p)
    return out


def list_worktrees(main, mainset):
    rc, o, _ = run(git_cmd(main, "worktree", "list", "--porcelain"))
    ents, cur = [], None
    if rc != 0:
        return ents
    for line in o.splitlines() + [""]:
        if line.startswith("worktree "):
            cur = {
                "main": main,
                "wt": line[9:],
                "flags": [],
                "branch": None,
                "head": None,
            }
        elif line == "" and cur:
            if os.path.realpath(cur["wt"]) not in mainset:
                ents.append(cur)
            cur = None
        elif cur:
            k, _, v = line.partition(" ")
            if k == "HEAD":
                cur["head"] = v
            elif k == "branch":
                cur["branch"] = v.replace("refs/heads/", "")
            elif k in ("detached", "locked", "prunable", "bare"):
                cur["flags"].append(k)
    return ents


def is_linked_worktree(e):
    """Its .git must be a FILE whose gitdir points into <main>/.git/worktrees/."""
    g = e["wt"] + "/.git"
    try:
        if not os.path.isfile(g):
            return False
        with open(g) as f:
            first = f.readline().strip()
        if not first.startswith("gitdir:"):
            return False
        d = os.path.realpath(first[7:].strip())
        return d.startswith(os.path.realpath(e["main"] + "/.git/worktrees") + "/")
    except Exception:
        return False


def held(wt):
    """Operator hold: worktree directories whose name starts with a configured prefix are kept until the
    configured date. No prefixes configured = nothing held. Prefixes with no usable date = held for good."""
    if not HOLD_PREFIXES:
        return False
    base = os.path.basename(os.path.realpath(wt).rstrip("/"))
    if not base.startswith(HOLD_PREFIXES):
        return False
    return HOLD_UNTIL is None or time.localtime()[:3] < HOLD_UNTIL


def is_kept(wt):
    """True if the worktree root holds the keep marker (any entry named KEEP_MARKER, a dangling link included).
    A kept worktree is never touched by any tier."""
    return os.path.lexists(os.path.join(wt, KEEP_MARKER))


def never_touch(wt):
    rw = os.path.realpath(wt)
    if held(wt):
        return True
    for n in NEVER:
        rn = os.path.realpath(n)
        if wt == n or rw == rn or rw.startswith(rn + "/") or wt.startswith(n + "/"):
            return True
    if rw.startswith(H + "/Library") or wt.startswith(H + "/Library"):
        return True
    return not rw.startswith(H + "/")


_slug = {}


def slug_of(main):
    if main not in _slug:
        rc, o, _ = run(git_cmd(main, "remote", "get-url", "origin"))
        mm = re.search(r"github\.com[:/]([^/]+/[^/.]+?)(\.git)?$", o.strip())
        _slug[main] = mm.group(1) if mm else None
    return _slug[main]


_merged = {}


def merged_heads(main, branch):
    """Head oids of merged PRs whose head ref is `branch` (gh pr list --state merged --head)."""
    key = (main, branch)
    if key in _merged:
        return _merged[key]
    s = slug_of(main)
    oids = set()
    if s:
        rc, o, _ = run(
            [
                "gh",
                "pr",
                "list",
                "-R",
                s,
                "--state",
                "merged",
                "--head",
                branch,
                "--limit",
                "30",
                "--json",
                "headRefOid",
            ],
            timeout=90,
        )
        if rc == 0:
            try:
                oids = set(p["headRefOid"] for p in json.loads(o))
            except Exception:
                STATS.note(False)
    _merged[key] = oids
    return oids


def pushed_via(e, head):
    wt = e["wt"]
    rc, o, _ = run(
        git_cmd(
            wt,
            "for-each-ref",
            "--contains",
            head,
            "--count=1",
            "--format=%(refname)",
            "refs/remotes/origin/",
        )
    )
    if rc == 0 and o.strip():
        return "origin"
    return merged_pr_via(
        e, head
    )  # None for a detached HEAD: unreachable, never eligible


def merged_pr_via(e, head):
    """'merged-PR-head' / 'ancestor-of-merged-PR-head' if HEAD is (an ancestor of) a merged PR head for the branch."""
    wt = e["wt"]
    br = e.get("branch")
    if not br:
        return None
    oids = merged_heads(e["main"], br)
    if head in oids:
        return "merged-PR-head"
    for oid in oids:
        # exit 1 = "not an ancestor" (a legitimate answer); anything else is an error
        rc2, _, _ = run(
            git_cmd(wt, "merge-base", "--is-ancestor", head, oid), ok_rcs=(0, 1)
        )
        if rc2 == 0:
            return "ancestor-of-merged-PR-head"
    return None


def merged_into_default(e, head):
    """True if HEAD is an ancestor of origin's default branch (origin/HEAD, else origin/main or origin/master)."""
    wt = e["wt"]
    refs = []
    rc, o, _ = run(
        git_cmd(wt, "symbolic-ref", "-q", "refs/remotes/origin/HEAD"),
        count=False,
        ok_rcs=(0, 1),
    )
    if rc == 0 and o.strip():
        refs.append(o.strip())
    refs += [
        r
        for r in ("refs/remotes/origin/main", "refs/remotes/origin/master")
        if r not in refs
    ]
    for ref in refs:
        rc2, _, _ = run(
            git_cmd(wt, "merge-base", "--is-ancestor", head, ref),
            count=False,
            ok_rcs=(0, 1),
        )
        if rc2 == 0:
            return True
    return False


def removal_basis(e, head):
    """(via, why): why is '' when whole-worktree removal is justified, else the keep reason.

    Base rule: HEAD is on an origin ref or is (an ancestor of) a merged PR head. In merged-only mode (daily)
    a commit that is merely pushed to some non-default origin ref is NOT enough: it must be merged.
    """
    pv = pushed_via(e, head)
    if not pv:
        return None, ("unpushed" if e.get("branch") else "detached-unreachable")
    if DAILY["merged_only"] and pv == "origin":
        mv = merged_pr_via(e, head)
        if mv:
            return mv, ""
        if merged_into_default(e, head):
            return "origin-default", ""
        return None, "pushed-not-merged"
    return pv, ""


def has_submodules(wt):
    """True if the worktree has an initialized submodule (git refuses to remove it without force) or git
    could not say. Kept for approval: this module never forces."""
    if not os.path.lexists(os.path.join(wt, ".gitmodules")):
        return False
    rc, o, _ = run(git_cmd(wt, "submodule", "status"))
    if rc != 0:
        return True
    return any(line and line[0] != "-" for line in o.splitlines())


def recent_file(wt, hours=None):
    rc, o, _ = run(
        [
            "find",
            wt,
            "(",
            "-name",
            "node_modules",
            "-o",
            "-name",
            ".git",
            "-o",
            "-name",
            "dist",
            "-o",
            "-name",
            "build",
            ")",
            "-prune",
            "-o",
            "-type",
            "f",
            "-mmin",
            "-%d" % ((hours or FRESH_HOURS) * 60),
            "-print",
            "-quit",
        ],
        timeout=300,
    )
    if rc != 0 and not o.strip():
        return "error"
    return "recent" if o.strip() else None


def status_clean(wt):
    rc, o, _ = run(git_cmd(wt, "status", "--porcelain"))
    if rc != 0:
        return None
    return not o.strip()


def ignored_entries(wt):
    rc, o, _ = run(
        git_cmd(wt, "status", "--porcelain", "--ignored", untracked="normal"),
        count=False,
    )
    if rc != 0:
        return None
    return [line[3:] for line in o.splitlines() if line.startswith("!! ")]


def is_locked(e):
    rc, o, _ = run(git_cmd(e["main"], "worktree", "list", "--porcelain"))
    if rc != 0:
        return None
    for b in o.split("\n\n"):
        if b.startswith("worktree " + e["wt"] + "\n"):
            return "\nlocked" in b or b.rstrip().endswith("locked")
    return None


def du_kb(wt):
    rc, o, _ = run(["du", "-sk", wt], timeout=300, count=False)
    try:
        return int(o.split()[0])
    except Exception:
        return 0


def analyze(e, want_size):
    """Cheap-to-expensive eligibility pass. Sets e['status'] (ELIGIBLE or a keep reason)."""
    wt = e["wt"]
    if "prunable" in e["flags"]:
        e["status"] = "prunable"
        return e
    if never_touch(wt):
        e["status"] = "never-touch"
        return e
    if not os.path.isdir(wt):
        e["status"] = "missing"
        return e
    if not is_linked_worktree(e):
        e["status"] = "not-linked-worktree"
        return e
    if is_kept(wt):
        e["status"] = "keep-marked"
        return e
    if "locked" in e["flags"]:
        e["status"] = "locked"
        return e
    if not e.get("head"):
        e["status"] = "no-head"
        return e
    r = recent_file(wt)
    if r == "error":
        e["status"] = "find-error"
        return e
    if r:
        e["status"] = "recent"
        return e
    c = status_clean(wt)
    if c is None:
        e["status"] = "status-error"
        return e
    if not c:
        e["status"] = "dirty"
        return e
    pv, why = removal_basis(e, e["head"])
    if why:
        e["status"] = why
        return e
    e["pushed_via"] = pv
    e["status"] = "ELIGIBLE"
    if want_size:
        e["kb"] = du_kb(wt)
    return e


CACHE_FILE_SUFFIXES = (".pyc", ".tsbuildinfo", ".eslintcache")
CACHE_FILE_NAMES = (".DS_Store",)
# The one disposable ignored file: the dev env copy project hooks place at every worktree root.
ENV_COPY_NAME = ".env.development"


def env_copy_is_disposable(wt, main):
    """True only if `<wt>/.env.development` is a regular file (not a link), git-ignored, and byte-identical to
    the same-named regular file in the main clone. Compared with filecmp (no shallow mode); the contents and
    any hash of them are never read into a log, a message or a record."""
    if not main:
        return False
    mine = os.path.join(wt, ENV_COPY_NAME)
    theirs = os.path.join(main, ENV_COPY_NAME)
    try:
        for f in (mine, theirs):
            st = os.lstat(f)
            if not stat.S_ISREG(st.st_mode):
                return False
        if git_ignored(wt, ENV_COPY_NAME) is not True:
            return False
        return filecmp.cmp(mine, theirs, shallow=False)
    except OSError:
        return False


def ignored_removable_strict(wt, entry, main=None):
    """Daily mode: may this git-ignored entry be deleted together with its worktree? '' if yes, else why not.

    Name-based regenerability is NOT enough here (tmp/, public/, *.log, .env.development* can hold notes and
    local settings). Only a cache directory name that passes the cache tier's own per-directory checks
    (a real directory inside the worktree, no .git beneath it, no mount crossing), or a known cache file
    (.DS_Store, *.pyc, ...), goes with the worktree. Everything else keeps it for approval.
    """
    rel = entry.rstrip("/")
    base = os.path.basename(rel)
    path = os.path.join(wt, rel)
    if not entry.endswith("/"):
        if base in CACHE_FILE_NAMES or base.endswith(CACHE_FILE_SUFFIXES):
            return ""
        if rel == ENV_COPY_NAME:  # only at the worktree root, never a renamed variant
            return "" if env_copy_is_disposable(wt, main) else "env-copy-differs"
        return "ignored-file"
    if base not in CACHE_DIR_NAMES:
        return "ignored-directory"
    if os.path.islink(path) or not os.path.isdir(path):
        return "symlink"
    rw = os.path.realpath(wt)
    if not os.path.realpath(path).startswith(rw + os.sep):
        return "not-contained"
    if contains_git(path):
        return "contains-git"
    if crosses_mount(wt, path):
        return "mount-point"
    return ""


def final_check_and_remove(e, apply_, protect_ignored):
    """Fresh re-check of EVERY condition, then (if apply_) remove. Returns a status string."""
    wt = e["wt"]
    if never_touch(wt) or not os.path.isdir(wt) or not is_linked_worktree(e):
        return "recheck-never-touch"
    if is_kept(wt):
        return "keep-marked"
    rc, cw = cwdset()
    if rc != 0 and not cw:
        return "recheck-lsof-error"  # cannot prove nobody is inside: keep
    if in_use(wt, cw):
        return "in-use"
    lk = is_locked(e)
    if lk is None:
        return "recheck-list-error"
    if lk:
        return "locked"
    rc, o, _ = run(git_cmd(wt, "rev-parse", "HEAD"))
    h = o.strip()
    if rc != 0 or h != e["head"]:
        return "head-moved"
    r = recent_file(wt)
    if r:
        return "recent" if r == "recent" else "find-error"
    c = status_clean(wt)
    if not c:
        return "dirty" if c is False else "status-error"
    pv, why = removal_basis(e, h)
    if why:
        return "unpushed" if why in ("unpushed", "detached-unreachable") else why
    if has_submodules(wt):
        return "has-submodules"
    ig = ignored_entries(wt)
    if ig is None and protect_ignored:
        return "status-error"
    if DAILY["merged_only"]:
        odd = [x for x in (ig or []) if ignored_removable_strict(wt, x, e["main"])]
        protect_ignored = True
    else:
        odd = [x for x in (ig or []) if not ignored_is_regen(wt, x)]
    if odd:
        e["ignored_nonregen"] = odd[:6]
        if protect_ignored:
            return "has-ignored-files"
    e["kb"] = e.get("kb") or du_kb(wt)
    if not apply_:
        return "WOULD-REMOVE"
    rc, o, err = run(
        git_cmd(e["main"], "worktree", "remove", wt), timeout=600
    )  # no --force; git re-checks the tree itself, under the same safe config
    if rc == 0:
        return "REMOVED"
    e["detail"] = err.strip().replace("\n", " ")[:160]
    return "git-refused"


# ----------------------------------------------------------------------------------------------
# node_modules-only tier (--strip-node-modules / --strip-only). Deletes directories literally named
# `node_modules` inside a clean, pushed, idle, unused, unlocked linked worktree. The worktree stays.
# It deletes nothing else: never a symlink, never anything whose real path leaves the worktree.
# ----------------------------------------------------------------------------------------------
def strip_names():
    """Directory names the strip tier may delete: node_modules, or every regenerable cache name in daily mode."""
    return CACHE_DIR_NAMES if STRIP.get("caches") else frozenset((STRIP_DIR_NAME,))


def git_ignored(wt, rel):
    """True/False if git ignores `rel`; None if git could not say."""
    rc, _, _ = run(
        git_cmd(wt, "check-ignore", "-q", "--", rel),
        count=False,
        ok_rcs=(0, 1),
    )
    if rc == 0:
        return True
    return False if rc == 1 else None


def node_modules_contained(wt, path):
    """True only if `path` is a real (non-symlink) directory literally named node_modules whose real path
    lies inside the worktree's real path. Checked per directory immediately before it is deleted."""
    names = strip_names()
    if (
        os.path.basename(path) not in names
        or os.path.islink(path)
        or not os.path.isdir(path)
    ):
        return False
    rw = os.path.realpath(wt)
    rp = os.path.realpath(path)
    if os.path.basename(rp) not in names or not rp.startswith(rw + os.sep):
        return False
    # an ancestor that is a symlink would make the real path differ from the walked path
    return rp == os.path.join(
        os.path.realpath(os.path.dirname(path)), os.path.basename(path)
    )


def _device(path):
    """st_dev of `path` itself (a symlink is not followed)."""
    return os.lstat(path).st_dev


def _raise(err):
    raise err


def crosses_mount(wt, path):
    """'' if `path` and everything beneath it is on the worktree's own device, else 'mount-point'.

    A volume mounted at (or anywhere under) a node_modules directory would otherwise be emptied by
    rmtree. Unreadable or vanished entries count as a crossing: when in doubt, keep.
    """
    try:
        if os.path.ismount(path):
            return "mount-point"
        root_dev = _device(wt)
        if _device(path) != root_dev:
            return "mount-point"
        for dp, dn, _ in os.walk(path, followlinks=False, onerror=_raise):
            for d in dn:
                full = os.path.join(dp, d)
                if os.path.islink(full):
                    continue
                if _device(full) != root_dev or os.path.ismount(full):
                    return "mount-point"
    except OSError:
        return "mount-point"
    return ""


def find_node_modules(wt):
    """Walk the worktree without following symlinks -> (real node_modules dirs, refused 'rel:reason' list).

    Does not descend into node_modules (nested ones go with their parent), into .git, or into a nested
    git repository (its own dirty state is invisible to the worktree's status).
    """
    found, refused = [], []
    for dp, dn, _ in os.walk(wt, topdown=True, followlinks=False):
        keep = []
        for d in sorted(dn):
            full = os.path.join(dp, d)
            rel = os.path.relpath(full, wt)
            if d == ".git":
                continue
            if d in strip_names():
                if os.path.islink(full):
                    refused.append("%s:symlink" % rel)
                else:
                    found.append(full)
                continue
            if os.path.islink(full):
                continue
            if os.path.lexists(os.path.join(full, ".git")):
                refused.append("%s:nested-repo" % rel)
                continue
            keep.append(d)
        dn[:] = keep
    # a symlink named node_modules to a file shows up in the file list, not the dir list: it is never touched
    return found, refused


def contains_git(path):
    """True if any `.git` entry (file, directory or link) exists beneath `path`, or if that cannot be told.

    A repository inside node_modules may hold commits that exist nowhere else; the walk that finds
    node_modules does not descend into it, so this looks for one before the directory is deleted.
    """
    try:
        for _, dn, fn in os.walk(path, followlinks=False, onerror=_raise):
            if ".git" in dn or ".git" in fn:
                return True
    except OSError:
        return True
    return False


def tree_bytes(path):
    """Apparent bytes under `path`, never following symlinks."""
    total = 0
    for dp, dn, fn in os.walk(path, followlinks=False):
        for name in fn + dn:
            try:
                st = os.lstat(os.path.join(dp, name))
            except OSError:
                continue
            if not stat.S_ISDIR(st.st_mode):
                total += st.st_size
    return total


def tracked_under(wt, rel):
    """True/False if git tracks anything under rel; None if git could not say."""
    rc, o, _ = run(git_cmd(wt, "ls-files", "-z", "--", ":(literal)" + rel), count=False)
    if rc != 0:
        return None
    return bool(o.strip("\0"))


def strip_gate(e, hours, full):
    """Every precondition of the node_modules tier. Returns '' if all hold, else the keep reason.

    full=True adds the system-wide `lsof` cwd scan; it runs immediately before any deletion.
    """
    wt = e["wt"]
    if STATS.aborted:
        return "aborted-error-ratio"
    if never_touch(wt):
        return "never-touch"
    if not os.path.isdir(wt):
        return "missing"
    if not is_linked_worktree(e):
        return "not-linked-worktree"
    if is_kept(wt):
        return "keep-marked"
    if "prunable" in e["flags"]:
        return "prunable"
    if "locked" in e["flags"]:
        return "locked"
    if not e.get("head"):
        return "no-head"
    lk = is_locked(e)
    if lk is None:
        return "recheck-list-error"
    if lk:
        return "locked"
    rc, o, _ = run(git_cmd(wt, "rev-parse", "HEAD"))
    if rc != 0 or o.strip() != e["head"]:
        return "head-moved"
    r = recent_file(wt, hours)
    if r:
        return "recent" if r == "recent" else "find-error"
    c = status_clean(wt)
    if not c:
        return "dirty" if c is False else "status-error"
    if not pushed_via(e, e["head"]):
        return "unpushed" if e.get("branch") else "detached-unreachable"
    if full:
        rc, cw = cwdset()
        if rc != 0 and not cw:
            return "recheck-lsof-error"  # cannot prove nobody is inside: keep
        if in_use(wt, cw):
            return "in-use"
    return ""


def strip_flow(e, apply_):
    """Strip node_modules from one worktree. Returns a status string; the worktree is never removed."""
    wt = e["wt"]
    hours = STRIP["idle_hours"]
    why = strip_gate(e, hours, full=False)
    if why:
        return why
    dirs, refused = find_node_modules(wt)
    plan = []
    for d in dirs:
        rel = os.path.relpath(d, wt)
        t = tracked_under(wt, rel)
        if t is None:
            refused.append("%s:tracked-check-error" % rel)
        elif t:
            refused.append("%s:tracked-files" % rel)
        elif os.path.basename(d) != STRIP_DIR_NAME and git_ignored(wt, rel) is not True:
            refused.append("%s:not-git-ignored" % rel)
        elif contains_git(d):
            refused.append("%s:contains-git" % rel)
        elif crosses_mount(wt, d):
            refused.append("%s:mount-point" % rel)
        else:
            plan.append((d, tree_bytes(d)))
    e["strip_refused"] = refused
    if not plan:
        return "refused-node-modules" if refused else "no-node-modules"
    e["strip_dirs"] = [os.path.relpath(d, wt) for d, _ in plan]
    e["strip_bytes"] = sum(n for _, n in plan)
    why = strip_gate(
        e, hours, full=True
    )  # fresh re-check of EVERY condition, immediately before deleting
    if why:
        e["strip_dirs"], e["strip_bytes"] = [], 0
        return why
    if STRIP.get("caches"):
        # a cache such as .venv can be the runtime of a process whose cwd is elsewhere: keep any directory
        # with a file open (or mapped) inside it, and keep everything if the scan itself failed
        ok, held_paths = open_file_paths()
        if not ok:
            e["strip_dirs"], e["strip_bytes"] = [], 0
            return "recheck-lsof-error"
        busy = [d for d, _ in plan if path_open(d, held_paths)]
        for d in busy:
            refused.append("%s:in-use-by-process" % os.path.relpath(d, wt))
        plan = [(d, n) for d, n in plan if d not in busy]
        e["strip_dirs"] = [os.path.relpath(d, wt) for d, _ in plan]
        e["strip_bytes"] = sum(n for _, n in plan)
        if not plan:
            return "refused-node-modules"
    if not apply_:
        return "WOULD-STRIP"
    freed, done, failed = 0, [], False
    for d, nbytes in plan:
        if not node_modules_contained(wt, d):
            refused.append("%s:not-contained" % os.path.relpath(d, wt))
            continue
        if crosses_mount(
            wt, d
        ):  # re-checked now: a volume may have been mounted since planning
            refused.append("%s:mount-point" % os.path.relpath(d, wt))
            continue
        try:
            shutil.rmtree(d)
        except OSError as ex:
            failed = True
            e["detail"] = ("%s: %s" % (os.path.relpath(d, wt), ex))[:160]
            left = tree_bytes(d) if os.path.isdir(d) else 0
            freed += max(0, nbytes - left)
            break
        freed += nbytes
        done.append(os.path.relpath(d, wt))
    e["strip_dirs"], e["strip_bytes"] = done, freed
    if failed:
        return "strip-failed"
    return "STRIPPED" if done else "refused-node-modules"


def act(e, apply_, protect_ignored):
    """Decide + act on one analyzed entry. Returns the final status string."""
    st = e["status"]
    if st != "ELIGIBLE":
        return st
    return final_check_and_remove(e, apply_, protect_ignored)


def rotate_logs():
    """Delete day logs older than 30 days and empty launchd logs over 1 MB. Works on regular files only:
    a symbolic link in the log directory is never followed, deleted or truncated."""
    cutoff = time.time() - 30 * 86400
    try:
        names = os.listdir(LOGDIR)
    except OSError:
        return
    for n in names:
        p = os.path.join(LOGDIR, n)
        try:
            st = os.lstat(p)
            if not stat.S_ISREG(st.st_mode):
                if stat.S_ISLNK(st.st_mode):
                    log("SKIP rotate %s: symbolic link" % p)
                continue
            if re.match(r"^\d{4}-\d{2}-\d{2}\.log$", n) and st.st_mtime < cutoff:
                os.remove(p)
            elif n.startswith("launchd.") and st.st_size > 1048576:
                fd = os.open(p, os.O_WRONLY | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0))
                os.close(fd)
        except OSError:
            continue


def write_summary(d):
    with open_private(RUNS, "a") as f:
        f.write(json.dumps(d, sort_keys=True) + "\n")


# ----------------------------------------------------------------------------------------------
# Approval list: every worktree or cache candidate the run did NOT act on, for the operator to approve.
# The run takes no action on anything listed. Written to the state dir on every daily run.
# ----------------------------------------------------------------------------------------------
# Plain-language reasons: no git flags, no HEAD/origin wording. The key is the reason code used everywhere else.
REASON_TEXT = {
    "keep-marked": "You marked this folder to keep (a %s file is inside it)."
    % KEEP_MARKER,
    "locked": "You locked this work folder, so it is kept.",
    "dirty": "Has changes that are not saved to git yet (edited or new files).",
    "unpushed": "Has saved work that exists only on this Mac (it was never uploaded).",
    "detached-unreachable": "Has saved work that is not on any branch and exists only on this Mac.",
    "pushed-not-merged": "Its work is uploaded but has not been merged yet.",
    "has-submodules": "Contains a linked sub-repository, which this tool does not remove automatically.",
    "has-ignored-files": "Holds files that git does not track and that cannot be rebuilt "
    "(such as notes, logs, local settings or a nested repository).",
    "recent": "Edited in the last day, so it is probably still in use.",
    "in-use": "A program is running inside it right now.",
    "head-moved": "It changed while the check was running.",
    "git-refused": "Git declined to remove it.",
    "find-error": "A safety check could not finish, so it was left alone.",
    "status-error": "A safety check could not finish, so it was left alone.",
    "recheck-list-error": "A safety check could not finish, so it was left alone.",
    "recheck-lsof-error": "A safety check could not finish, so it was left alone.",
    "aborted-error-ratio": "The check stopped early because too many commands failed.",
    "tracked-files": "This cache folder contains files that are saved in git.",
    "not-git-ignored": "This cache-like folder is not one that git ignores, so it may hold real work.",
    "contains-git": "This cache folder contains another git repository.",
    "nested-repo": "This cache folder is inside another git repository.",
    "symlink": "This cache folder is a shortcut (symbolic link).",
    "mount-point": "This cache folder is, or contains, another mounted disk.",
    "not-contained": "This cache folder points outside its work folder.",
    "tracked-check-error": "A safety check could not finish, so it was left alone.",
    "in-use-by-process": "A running program has files open inside this cache folder.",
}
# Reasons that mean the work exists only on this Mac (or may): shown first, with a warning.
ONLY_HERE_REASONS = (
    "dirty",
    "unpushed",
    "detached-unreachable",
    "pushed-not-merged",
    "has-ignored-files",
)
# The operator already decided these: listed for completeness, not counted as waiting for a decision.
KEPT_ON_PURPOSE = ("keep-marked", "locked")
# Statuses that are not approval candidates: not a usable linked worktree, off-limits by configuration, or
# simply deferred to the next run by the per-run cap.
APPROVAL_EXCLUDE = (
    "never-touch",
    "recheck-never-touch",
    "prunable",
    "missing",
    "not-linked-worktree",
    "no-head",
    "over-cap-deferred",
)


def fmt_size(kb):
    """Readable size for a person: MB under 1 GiB, GB above; never '0.00'."""
    if kb < 1024:
        return "under 1 MB"
    if kb < 1048576:
        return "%d MB" % round(kb / 1024)
    return "%.1f GB" % (kb / 1048576)


def note_approval(e, reason):
    """Record a whole-worktree candidate that was not acted on."""
    if reason in APPROVAL_EXCLUDE or reason in REMOVAL_RESULTS:
        return
    item = {
        "kind": "worktree",
        "path": e["wt"],
        "branch": e.get("branch"),
        "reason": reason,
        "why": REASON_TEXT.get(reason, reason),
    }
    if e.get("ignored_nonregen"):
        item["non_regenerable_ignored"] = list(e["ignored_nonregen"])
    if e.get("detail"):
        item["detail"] = e["detail"]
    if e.get("kb"):
        item["size_kb"] = e["kb"]
    APPROVAL.append(item)


def note_cache_refusals(e):
    """Record each cache directory the strip tier refused ('rel:reason') as an approval candidate."""
    for r in e.get("strip_refused", []):
        rel, _, reason = r.rpartition(":")
        APPROVAL.append(
            {
                "kind": "cache",
                "path": os.path.join(e["wt"], rel),
                "worktree": e["wt"],
                "branch": e.get("branch"),
                "reason": reason,
                "why": REASON_TEXT.get(reason, reason),
            }
        )


def _size_item(item):
    if "size_kb" not in item:
        item["size_kb"] = du_kb(item["path"])
    return item


def item_group(item):
    if item["reason"] in KEPT_ON_PURPOSE:
        return "kept-on-purpose"
    if item["reason"] in ONLY_HERE_REASONS:
        return "only-on-this-mac"
    return "needs-decision"


def decision_summary(items):
    """(count, kb) of the items waiting for a decision (kept-on-purpose items excluded)."""
    waiting = [i for i in items if item_group(i) != "kept-on-purpose"]
    return len(waiting), sum(i.get("size_kb", 0) for i in waiting)


def write_private_replace(path, text):
    """Atomically replace `path` with `text`, mode 0600, never following a symlink at the temp name."""
    tmp = path + ".tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, PRIVATE_FILE_MODE)
    try:
        os.fchmod(fd, PRIVATE_FILE_MODE)
        with os.fdopen(fd, "w") as f:
            f.write(text)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def render_approval_markdown(items, ts, mode):
    """The short, plain-language list the operator reads. Pure: no I/O."""
    count, total_kb = decision_summary(items)
    lines = [
        "# Worktree prune: your decisions",
        "",
        "Checked %s%s. Nothing below was changed by the check."
        % (ts, " (report-only run)" if mode == "dry-run" else ""),
        "",
        "To remove an item you have checked, run this from your main project folder:",
        "",
        "    git worktree remove <path>",
        "",
        "(For a cache folder, just delete that one folder.) To keep an item for good, run:",
        "",
        "    touch <path>/%s" % KEEP_MARKER,
        "",
    ]
    if count == 0:
        lines += ["Nothing needs your decision.", ""]
    else:
        lines += [
            "%d items, about %s, are waiting for your decision."
            % (count, fmt_size(total_kb)),
            "",
        ]
    sections = (
        (
            "only-on-this-mac",
            "Work that may exist only on this Mac",
            "Do not remove these unless you are sure you no longer need the work.",
        ),
        ("needs-decision", "Other items waiting for your decision", ""),
        ("kept-on-purpose", "Kept on purpose (no action needed)", ""),
    )
    shown = 0
    for group, title, note in sections:
        part = [i for i in items if item_group(i) == group]
        if not part:
            continue
        lines += ["## %s" % title, ""]
        if note:
            lines += [note, ""]
        for i in part:
            if shown >= 300:
                break
            shown += 1
            label = (
                "cache folder"
                if i["kind"] == "cache"
                else (i.get("branch") or "no branch")
            )
            lines.append(
                "- **%s** `%s` (%s): %s"
                % (
                    escape_text(label),
                    escape_text(i["path"]),
                    fmt_size(i.get("size_kb", 0)),
                    escape_text(i["why"]),
                )
            )
        lines.append("")
    if len(items) > shown:
        lines += ["... and %d more in approval_list.json." % (len(items) - shown), ""]
    return "\n".join(lines)


def write_approval_list(items, ts, mode, workers):
    """Write approval_list.json (everything) and approval_list.md (short). Returns the JSON path."""
    with ThreadPoolExecutor(max(1, workers)) as pool:
        list(pool.map(_size_item, items))
    items.sort(key=lambda i: (-i.get("size_kb", 0), i["path"]))
    for i in items:
        i["group"] = item_group(i)
    count, total_kb = decision_summary(items)
    doc = {
        "generated": ts,
        "mode": mode,
        "note": "Nothing on this list was changed. Approve an item by acting on it yourself, or add a "
        "%s file to its worktree root to keep it." % KEEP_MARKER,
        "count": len(items),
        "total_kb": sum(i.get("size_kb", 0) for i in items),
        "decision_count": count,
        "decision_kb": total_kb,
        "items": items,
    }
    jpath = os.path.join(BASE, "approval_list.json")
    write_private_replace(jpath, json.dumps(doc, indent=1, sort_keys=True) + "\n")
    write_private_replace(
        os.path.join(BASE, "approval_list.md"),
        render_approval_markdown(items, ts, mode) + "\n",
    )
    return jpath


# ----------------------------------------------------------------------------------------------
# Free-space alert with hysteresis. State: <state dir>/alert_state.json. Every firing is also written to
# <state dir>/alerts.jsonl (the durable record, written whether or not delivery works).
# ----------------------------------------------------------------------------------------------
def _alert_path(name):
    return os.path.join(BASE, name)


def load_alert_state():
    try:
        with open(_alert_path("alert_state.json")) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _plural(n, word):
    return "%d %s%s" % (n, word, "" if n == 1 else "s")


def build_alert_message(free_gb, severity, run=None):
    """The alert text, written to be read on a phone by someone who does not know the tool.

    `run` describes what this run did: {"caches": n, "removed": n, "freed_bytes": n, "waiting": n,
    "waiting_kb": n}. Order: the problem, what was already done, what is waiting, the next step, the path.
    """
    run = run or {}
    critical = severity >= 2
    if critical:
        head = "URGENT: your Mac is almost out of disk space: %.0f GB free." % free_gb
    else:
        head = "Your Mac is low on disk space: %.0f GB free." % free_gb
    done = []
    if run.get("caches"):
        done.append("cleared %s" % _plural(run["caches"], "cache folder"))
    if run.get("removed"):
        done.append("removed %s" % _plural(run["removed"], "merged work folder"))
    if done:
        did = "Today's check already %s, freeing about %s." % (
            " and ".join(done),
            fmt_size(run.get("freed_bytes", 0) / 1024),
        )
    else:
        did = "Today's check freed nothing."
    waiting = run.get("waiting", 0)
    if waiting:
        left = "%s, about %s, %s waiting for your decision." % (
            _plural(waiting, "item"),
            fmt_size(run.get("waiting_kb", 0)),
            "is" if waiting == 1 else "are",
        )
    else:
        left = "Nothing is waiting for your decision."
    if critical:
        step = (
            "Please free space today, or the Mac may start failing: open the list and remove what "
            "you no longer need."
        )
    else:
        step = (
            "Nothing is urgent to delete. If you want more space, open the list and remove what "
            "you no longer need."
        )
    keep = (
        "To keep a work folder from ever being cleaned, add a file named %s inside it."
        % KEEP_MARKER
    )
    return "\n".join(
        [
            head,
            did,
            "Nothing else was deleted. " + left,
            step + " " + keep,
            "List: " + _alert_path("approval_list.md"),
        ]
    )


def send_notification(message, severity):
    """Deliver through the repo's existing operator notifier (lib/notify, Apprise/Telegram-backed).

    Returns {"available": bool, "returned": bool|None, "error": str}. `returned` is the notifier's own
    result: True = sent now, False = held for later delivery, dropped or undelivered. No credentials are
    handled here: the notifier reads its own transport settings from its environment. Sent with
    email_eligible=False: the message points at a file, so it must never become an inbox item.
    """
    try:
        root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        if root not in sys.path:
            sys.path.insert(0, root)
        from lib.notify import Notifier, Priority  # noqa: PLC0415

        prio = Priority.CRITICAL if severity >= 2 else Priority.WARN
        sent = Notifier().send(
            message, prio, handler="worktree-prune", email_eligible=False
        )
        return {"available": True, "returned": bool(sent), "error": ""}
    except Exception as ex:  # missing deps (httpx, apprise), no transport, anything: never crash the run
        return {
            "available": False,
            "returned": None,
            "error": ("%s: %s" % (type(ex).__name__, ex))[:200],
        }


def evaluate_alert(free_gb, apply_, run=None):
    """Check free space against the threshold. Returns the alert summary dict (severity 0 = nothing wrong).

    Fires when free space falls below ALERT_GIB; once DELIVERED it stays quiet (suppressed) while space stays
    low and re-arms only after free space recovers to ALERT_GIB + ALERT_REARM_GIB. A send that failed, was
    unavailable or was not reported as sent does NOT disarm: it is recorded and retried on the next run.
    A dry run reports what it would do and writes and sends nothing.
    """
    rearm_at = ALERT_GIB + ALERT_REARM_GIB
    res = {
        "threshold_gib": ALERT_GIB,
        "rearm_gib": rearm_at,
        "free_gib": round(free_gb, 1),
        "severity": 0,
        "state": "ok",
    }
    if ALERT_GIB <= 0:
        res["state"] = "disabled"
        return res
    if free_gb < 0:
        res["state"] = "free-space-unknown"
        return res
    st = load_alert_state()
    armed = st.get("armed", True)
    if free_gb >= rearm_at:
        if not armed:
            res["state"] = "rearmed"
            if apply_:
                st["armed"] = True
                write_private_replace(
                    _alert_path("alert_state.json"), json.dumps(st, sort_keys=True)
                )
        return res
    if free_gb >= ALERT_GIB:
        return res  # inside the hysteresis band: no new alert, no re-arm
    res["severity"] = 2 if free_gb < CRITICAL_FREE_GIB else 1
    if not armed:
        res["state"] = "suppressed"
        return res
    res["state"] = "fired" if apply_ else "would-fire"
    msg = build_alert_message(free_gb, res["severity"], run)
    if not apply_:
        res["message"] = (
            msg  # shown so a dry run can check the wording; nothing is sent or written
        )
        return res
    ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    delivery = send_notification(msg, res["severity"])
    delivered = bool(delivery.get("available") and delivery.get("returned"))
    res["delivery"] = delivery
    res["delivered"] = delivered
    if not delivered:
        res["state"] = "fired-undelivered"
    record = {
        "ts": ts,
        "kind": "low-free-space",
        "severity": res["severity"],
        "free_gib": res["free_gib"],
        "threshold_gib": ALERT_GIB,
        "message": msg,
        "delivery": delivery,
        "delivered": delivered,
    }
    with open_private(_alert_path("alerts.jsonl"), "a") as f:
        f.write(json.dumps(record, sort_keys=True) + "\n")
    if delivered:
        state = {"armed": False, "last_alert": ts, "last_free_gib": res["free_gib"]}
    else:  # stays armed: the next run retries
        state = {"armed": True, "last_failed": ts, "last_free_gib": res["free_gib"]}
    write_private_replace(
        _alert_path("alert_state.json"), json.dumps(state, sort_keys=True)
    )
    return res


REMOVAL_RESULTS = ("REMOVED", "WOULD-REMOVE")
# Statuses that put a worktree out of reach of every tier: do not even attempt the node_modules tier on them.
STRIP_SKIP_STATUS = (
    "never-touch",
    "prunable",
    "missing",
    "not-linked-worktree",
    "no-head",
    "locked",
    "keep-marked",
)


def build_parser():
    ap = argparse.ArgumentParser(
        description="Prune stale, pushed, inactive linked git worktrees (dry run by default)."
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="actually remove / delete (default: dry run)",
    )
    ap.add_argument(
        "--daily",
        action="store_true",
        help="the one daily invocation: --protect-ignored, merged-only whole-worktree removal, "
        "regenerable-cache deletion, the free-space alert and the approval list. Like every mode it is a dry "
        "run unless --apply is also given.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="never delete, whatever else is given (overrides --apply and --daily's apply)",
    )
    ap.add_argument(
        "--strip-caches",
        action="store_true",
        help="cache tier: also delete other regenerable cache directories (.venv, dist, build, ...) that git "
        "ignores and does not track, not only node_modules (implies --strip-node-modules)",
    )
    ap.add_argument(
        "--require-merged",
        action="store_true",
        help="whole-worktree removal needs HEAD merged (origin's default branch or a merged PR head), not merely pushed",
    )
    ap.add_argument(
        "--approval-list",
        action="store_true",
        help="write approval_list.json and approval_list.md (every candidate not acted on) to the state dir",
    )
    ap.add_argument(
        "--alert",
        action="store_true",
        help="alert when free space on the home volume is below the threshold (see --alert-gib)",
    )
    ap.add_argument(
        "--alert-gib",
        help="free-space alert threshold in GiB (env %sALERT_GIB; default %g; 0 disables)"
        % (ENV_PREFIX, DEFAULT_ALERT_GIB),
    )
    ap.add_argument(
        "--alert-rearm-gib",
        help="GiB above the threshold at which the alert re-arms (env %sALERT_REARM_GIB; default %g)"
        % (ENV_PREFIX, DEFAULT_ALERT_REARM_GIB),
    )
    ap.add_argument(
        "--fresh-hours",
        type=int,
        default=DEFAULT_FRESH_HOURS,
        help="skip worktrees with files modified within this many hours (default %d)"
        % DEFAULT_FRESH_HOURS,
    )
    ap.add_argument(
        "--max",
        type=int,
        default=150,
        help="max worktrees acted on (removed or node_modules-stripped) per run (default 150)",
    )
    ap.add_argument(
        "--workers",
        type=int,
        default=6,
        help="threads used for the read-only per-worktree checks (default 6); deleting is done one worktree at a time",
    )
    ap.add_argument(
        "--protect-ignored",
        action="store_true",
        help="also keep worktrees holding git-ignored files that are not regenerable build/cache dirs",
    )
    ap.add_argument(
        "--list", action="store_true", help="print the eligible list (dry run)"
    )
    ap.add_argument(
        "--json-out", help="write eligible entries as JSON here (dry run inspection)"
    )
    ap.add_argument(
        "--strip-node-modules",
        action="store_true",
        help="node_modules-only tier: in a worktree idle >= --idle-hours and otherwise as safe to remove as the "
        "plain tier requires, delete only directories named node_modules; the worktree stays (off by default)",
    )
    ap.add_argument(
        "--strip-only",
        action="store_true",
        help="run only the node_modules-only tier: no whole-worktree removal (implies --strip-node-modules)",
    )
    ap.add_argument(
        "--idle-hours",
        type=int,
        default=None,
        help="node_modules tier idle window, hours: no file modified (outside node_modules/.git/dist/build) "
        "in this long (default %d)" % DEFAULT_STRIP_IDLE_HOURS,
    )
    ap.add_argument(
        "--state-dir",
        help="lock, runs.jsonl and logs (env %sSTATE_DIR; default ~/.ateles-work/worktree-prune)"
        % ENV_PREFIX,
    )
    ap.add_argument(
        "--repos-dir",
        action="append",
        help="directory whose children are clones to scan; repeatable (env %sREPOS_DIRS; default ~/repos)"
        % ENV_PREFIX,
    )
    ap.add_argument(
        "--never-touch",
        action="append",
        help="extra directory never touched; repeatable (env %sNEVER_TOUCH); the swarm deployment checkouts are always protected"
        % ENV_PREFIX,
    )
    ap.add_argument(
        "--hold-prefix",
        action="append",
        help="hold worktree directories whose name starts with this; repeatable (env %sHOLD_PREFIXES, comma-separated; default none)"
        % ENV_PREFIX,
    )
    ap.add_argument(
        "--hold-until",
        help="YYYY-MM-DD the hold lapses on (env %sHOLD_UNTIL); unset or unreadable = held indefinitely"
        % ENV_PREFIX,
    )
    ap.add_argument(
        "--yield-to-file",
        help="with --apply, skip the run while this file changed in the last 30 minutes (env %sYIELD_TO_FILE; default none)"
        % ENV_PREFIX,
    )
    return ap


def idle_windows(idle_hours_arg):
    """The node_modules tier's idle window in hours for the `--idle-hours` value (None = flag not given)."""
    if idle_hours_arg is None:
        return DEFAULT_STRIP_IDLE_HOURS
    return max(1, idle_hours_arg)


# Options of the archive-then-remove tier this module no longer has. They are refused by name, with
# a message, so a stale copy of an old command line or template cannot silently run without the tier
# it expects.
REMOVED_OPTIONS = (
    "--archive-remove",
    "--archive-cap-mb",
    "--prune-archives",
    "--archive-retention-days",
    "--archive-root",
)


def reject_removed_options(parser, argv):
    for tok in argv:
        name = tok.split("=", 1)[0]
        if name in REMOVED_OPTIONS:
            parser.error(
                "%s is no longer supported: the archive-then-remove tier is not part of this module, "
                "and a command line or template that passes it would run without the tier it expects"
                % name
            )


def main(argv=None):
    """Run with a private umask (state and logs are created 0700/0600), restored afterwards."""
    old_umask = os.umask(0o077)
    try:
        return _main(argv)
    finally:
        os.umask(old_umask)


def _main(argv):
    global FRESH_HOURS, LOGF
    parser = build_parser()
    reject_removed_options(parser, sys.argv[1:] if argv is None else list(argv))
    a = parser.parse_args(argv)
    if a.daily and a.strip_only:
        parser.error("--daily cannot be combined with --strip-only")
    if a.dry_run:
        a.apply = False
    if a.daily:
        a.protect_ignored = True
    try:
        warnings = configure(
            os.environ,
            {
                "state_dir": a.state_dir,
                "repos_dir": a.repos_dir,
                "never_touch": a.never_touch,
                "hold_prefix": a.hold_prefix,
                "hold_until": a.hold_until,
                "yield_to_file": a.yield_to_file,
                "alert_gib": a.alert_gib,
                "alert_rearm_gib": a.alert_rearm_gib,
            },
        )
    except ConfigError as ex:
        parser.error(str(ex))  # exit 2 before anything is read, written or deleted
    FRESH_HOURS = max(1, a.fresh_hours)
    STRIP["only"] = bool(a.strip_only)
    STRIP["caches"] = bool(a.daily or a.strip_caches)
    STRIP["enabled"] = bool(a.strip_node_modules or a.strip_only or STRIP["caches"])
    DAILY["merged_only"] = bool(a.daily or a.require_merged)
    DAILY["approval"] = bool(a.daily or a.approval_list)
    DAILY["alert"] = bool(a.daily or a.alert)
    APPROVAL.clear()
    STRIP["idle_hours"] = idle_windows(a.idle_hours)

    ensure_private_dir(BASE)
    ensure_private_dir(LOGDIR)
    t0 = time.time()
    ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    mode = "apply" if a.apply else "dry-run"

    lockf = open_private(LOCK, "a+")
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        write_summary({"ts": ts, "mode": mode, "status": "skipped-locked"})
        return 0
    if (
        a.apply and MANUAL_PROGRESS
    ):  # a dry run is read-only, so only apply yields to a manual cleanup
        try:
            if time.time() - os.path.getmtime(MANUAL_PROGRESS) < 1800:
                write_summary(
                    {"ts": ts, "mode": mode, "status": "skipped-manual-cleanup-active"}
                )
                return 0
        except OSError:
            pass

    LOGF = open_private("%s/%s.log" % (LOGDIR, datetime.date.today().isoformat()), "a")
    for w in warnings:
        log("WARN " + w)
    rotate_logs()
    free0 = dfree_gb()
    log(
        "=== start mode=%s max=%d free=%.1fGB%s%s"
        % (
            mode,
            a.max,
            free0,
            " LOW-DISK(<5GB)" if 0 <= free0 < 5 else "",
            " strip-only"
            if STRIP["only"]
            else (" +strip-node-modules" if STRIP["enabled"] else ""),
        )
        + (" daily" if a.daily else "")
    )
    if not shutil.which("gh", path=GIT_ENV["PATH"]):
        log(
            "WARN gh not found on PATH; merged-PR check unavailable (origin-contained commits still eligible)"
        )

    mains = discover_mains()
    mainset = set(os.path.realpath(m) for m in mains)
    entries = []
    for m in mains:
        entries += list_worktrees(m, mainset)
    log("discovered %d linked worktrees in %d clones" % (len(entries), len(mains)))

    reasons = Counter()
    strip_reasons = Counter()
    removed = 0
    kb_total = 0
    stripped = []  # applied: worktrees whose node_modules were deleted
    would_strip = []  # dry run: worktrees whose node_modules would be deleted
    eligible = []  # dry run: confirmed would-remove entries
    ex = ThreadPoolExecutor(a.workers)
    futs = [ex.submit(analyze, e, not a.apply and not STRIP["only"]) for e in entries]
    try:
        for f in as_completed(futs):
            if STATS.aborted:
                break
            e = f.result()
            st = e["status"]
            res = st
            if not STRIP["only"]:
                if st == "ELIGIBLE":
                    if a.apply and removed + len(stripped) >= a.max:
                        reasons["over-cap-deferred"] += 1
                        log("DEFER over-cap %s" % e["wt"])
                        continue
                    res = act(e, a.apply, a.protect_ignored)
                    if res in REMOVAL_RESULTS:
                        kb_total += e.get("kb", 0)
                        tag = (
                            " IGNORED=" + ",".join(e["ignored_nonregen"])
                            if e.get("ignored_nonregen")
                            else ""
                        )
                        log(
                            "%s %s (%s) %.2fGB via=%s%s"
                            % (
                                res,
                                e["wt"],
                                e.get("branch") or "detached",
                                e.get("kb", 0) / 1048576,
                                e["pushed_via"],
                                tag,
                            )
                        )
                        if res == "REMOVED":
                            removed += 1
                        else:
                            e["tier"] = "plain"
                            eligible.append(e)
                    else:
                        reasons[res] += 1
                        log("KEEP %-18s %s %s" % (res, e["wt"], e.get("detail", "")))
                else:
                    reasons[st] += 1
                    if st != "never-touch":
                        log(
                            "KEEP %-18s %s %s"
                            % (st, e["wt"], e.get("branch") or "(detached)")
                        )
            if DAILY["approval"] and not STRIP["only"]:
                note_approval(e, res)
            if (
                not STRIP["enabled"]
                or res in REMOVAL_RESULTS
                or st in STRIP_SKIP_STATUS
            ):
                continue
            if a.apply and removed + len(stripped) >= a.max:
                strip_reasons["over-cap-deferred"] += 1
                log("DEFER strip over-cap %s" % e["wt"])
                continue
            sres = strip_flow(e, a.apply)
            if DAILY["approval"]:
                note_cache_refusals(e)
            if sres in ("STRIPPED", "WOULD-STRIP"):
                log(
                    "%s %s (%s) %.2fMB dirs=%s refused=%s"
                    % (
                        sres,
                        e["wt"],
                        e.get("branch") or "detached",
                        e.get("strip_bytes", 0) / 1048576,
                        ",".join(e.get("strip_dirs", [])),
                        ",".join(e.get("strip_refused", [])) or "-",
                    )
                )
                e["tier"] = "strip"
                (stripped if sres == "STRIPPED" else would_strip).append(e)
            else:
                strip_reasons[sres] += 1
                if sres not in ("no-node-modules", "never-touch"):
                    log(
                        "KEEP-STRIP %-18s %s %s refused=%s"
                        % (
                            sres,
                            e["wt"],
                            e.get("detail", ""),
                            ",".join(e.get("strip_refused", [])) or "-",
                        )
                    )
    finally:
        ex.shutdown(wait=False, cancel_futures=True)

    status = "ok"
    if STATS.aborted:
        status = "aborted-error-ratio"
        log(
            "ABORT: %d/%d git/gh calls failed (>%d%%); no further removals"
            % (STATS.errors, STATS.calls, int(ERR_ABORT_RATIO * 100))
        )

    # `git worktree prune` per clone: admin entries whose directory is already gone
    pruned = 0
    if status == "ok" and not STRIP["only"]:
        for m in mains:
            _, o, _ = run(git_cmd(m, "worktree", "list", "--porcelain"), count=False)
            prunable = [
                b.split("\n")[0][9:] for b in o.split("\n\n") if "\nprunable" in b
            ]
            if not prunable:
                continue
            if any(not p.startswith(H + "/") for p in prunable):
                log(
                    "SKIP prune %s: prunable entry outside $HOME (maybe an unmounted volume)"
                    % m
                )
                continue
            pruned += len(prunable)
            run(
                git_cmd(m, "worktree", "prune", *([] if a.apply else ["--dry-run"])),
                count=False,
            )
        log(
            "worktree prune: %d admin entries %s"
            % (pruned, "pruned" if a.apply else "would be pruned")
        )

    free1 = dfree_gb()
    strip_done = stripped if a.apply else would_strip
    approval_path = None
    if DAILY["approval"] and not STRIP["only"] and status == "ok":
        try:
            approval_path = write_approval_list(APPROVAL, ts, mode, a.workers)
        except OSError as ex:
            log("WARN approval list not written: %s" % ex)
    alert = {"severity": 0, "state": "off"}
    if DAILY["alert"]:
        try:
            waiting, waiting_kb = decision_summary(APPROVAL if approval_path else [])
            alert = evaluate_alert(
                free1,
                a.apply,
                {
                    "caches": sum(len(x.get("strip_dirs", [])) for x in stripped),
                    "removed": removed,
                    "freed_bytes": kb_total * 1024
                    + sum(x.get("strip_bytes", 0) for x in stripped),
                    "waiting": waiting,
                    "waiting_kb": waiting_kb,
                },
            )
        except Exception as ex:  # an alert problem must not hide the run's own result
            alert = {"severity": 0, "state": "error", "error": str(ex)[:200]}
            log("WARN alert check failed: %s" % ex)
    summ = {
        "ts": ts,
        "mode": mode,
        "status": status,
        "discovered": len(entries),
        "removed": removed,
        "would_remove": len(eligible),
        "would_remove_over_cap": max(0, len(eligible) - a.max) if not a.apply else 0,
        "gb": round(kb_total / 1048576, 2),
        "kept": dict(reasons),
        "prunable_entries": pruned,
        "git_gh_calls": STATS.calls,
        "git_gh_errors": STATS.errors,
        "free_gb_before": round(free0, 1),
        "free_gb_after": round(free1, 1),
        "low_disk": 0 <= free1 < 5,
        "daily": bool(a.daily),
        "alert": alert,
        "alert_severity": alert.get("severity", 0),
        "approval_items": len(APPROVAL) if approval_path else 0,
        "approval_list": approval_path,
        "secs": round(time.time() - t0),
        "strip_tier": STRIP["enabled"],
        "strip_only": STRIP["only"],
        "strip_idle_hours": STRIP["idle_hours"],
        "stripped_worktrees": len(stripped),
        "would_strip_worktrees": len(would_strip),
        "strip_bytes_freed": sum(x.get("strip_bytes", 0) for x in stripped),
        "would_strip_bytes": sum(x.get("strip_bytes", 0) for x in would_strip),
        "strip_kept": dict(strip_reasons),
        "strip_detail": [
            {
                "wt": x["wt"],
                "bytes": x.get("strip_bytes", 0),
                "dirs": len(x.get("strip_dirs", [])),
            }
            for x in strip_done[:200]
        ],
    }
    write_summary(summ)
    log("=== end " + json.dumps(summ, sort_keys=True))
    if a.json_out:
        keys = (
            "wt",
            "main",
            "branch",
            "head",
            "pushed_via",
            "kb",
            "ignored_nonregen",
            "tier",
            "strip_bytes",
            "strip_dirs",
        )
        with open(a.json_out, "w") as jf:
            json.dump(
                [{k: x.get(k) for k in keys} for x in eligible + strip_done],
                jf,
                indent=1,
            )
    if a.list:
        for e in eligible:
            print(
                "%s\t%.2fGB\t%s"
                % (
                    escape_text(e["wt"]),
                    e.get("kb", 0) / 1048576,
                    e.get("pushed_via"),
                )
            )
        for e in strip_done:
            print(
                "%s\t%.2fMB\tnode_modules"
                % (escape_text(e["wt"]), e.get("strip_bytes", 0) / 1048576)
            )
    print(json.dumps(summ, sort_keys=True))
    return 0 if status == "ok" else 2


try:
    configure()
except ConfigError:
    pass  # main() runs configure() again and reports the error before doing anything

if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as ex:  # never crash silently under launchd; record it
        try:
            write_summary(
                {
                    "ts": datetime.datetime.now()
                    .astimezone()
                    .isoformat(timespec="seconds"),
                    "status": "crashed",
                    "error": str(ex)[:200],
                }
            )
        except Exception:
            pass
        sys.exit(1)
