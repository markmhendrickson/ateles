#!/usr/bin/env python3
"""Nightly prune of stale, fully-pushed, inactive linked git worktrees.

Default is DRY RUN. `--apply` actually removes or deletes. stdlib only.

Three tiers, cheapest first. Every tier works on LINKED worktrees only (never a
main clone) and re-checks every condition immediately before it acts.

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
   This script never stashes, deletes branches, pushes, or commits.

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
   runs log. `--strip-only` runs this tier alone (no whole-worktree removal, no archive).

3. Archive-then-remove tier (`--archive-remove`, off by default). Worktrees that the plain
   tier KEEPS as dirty / unpushed / detached (or that hold non-regenerable ignored files
   under --protect-ignored) and are idle >= --idle-hours (default 72 for this tier when the
   flag is not given) get a verified recovery archive (bundle of local-only commits,
   patch.diff, untracked/ignored-file tarball, meta.json) under the archive root (0700,
   <= 25 MB, otherwise SKIPPED); then, only if the worktree is unchanged since,
   `git worktree remove --force`. `--force` is used ONLY there; the plain tier never uses
   it. `--prune-archives` lists (or with --apply deletes) archives older than 30 days, and
   only those laid out the way this tool writes them.

Operator-specific values are configuration, never code. Each can be set by environment
variable or CLI flag (flag wins); the defaults are relative to the user's home directory:
  ATELES_WORKTREE_PRUNE_STATE_DIR      lock, runs.jsonl, logs   (--state-dir)
  ATELES_WORKTREE_PRUNE_ARCHIVE_ROOT   archive root             (--archive-root)
  ATELES_WORKTREE_PRUNE_REPOS_DIRS     clone parents, os.pathsep-separated (--repos-dir, repeatable)
  ATELES_WORKTREE_PRUNE_NEVER_TOUCH    extra never-touch dirs, os.pathsep-separated (--never-touch)
  ATELES_WORKTREE_PRUNE_HOLD_PREFIXES  comma-separated worktree directory-name prefixes to hold (--hold-prefix)
  ATELES_WORKTREE_PRUNE_HOLD_UNTIL     YYYY-MM-DD the hold lapses on  (--hold-until)
  ATELES_WORKTREE_PRUNE_YIELD_TO_FILE  --apply yields while this file changed in the last 30 minutes (--yield-to-file)
"""

import argparse
import atexit
import datetime
import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

ENV_PREFIX = "ATELES_WORKTREE_PRUNE_"
DEFAULT_FRESH_HOURS = 24
DEFAULT_STRIP_IDLE_HOURS = 24
DEFAULT_ARCHIVE_IDLE_HOURS = 72
ERR_ABORT_RATIO = 0.20
ERR_MIN_CALLS = 20
ARCHIVE_CAP_MB = 25
ARCHIVE_RETENTION_DAYS = 30
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
ARCHIVE_ROOT = ""
FRESH_HOURS = DEFAULT_FRESH_HOURS
# Settings of the optional archive-then-remove tier. Off unless --archive-remove is passed.
ARCH = {
    "enabled": False,
    "idle_hours": DEFAULT_ARCHIVE_IDLE_HOURS,
    "cap": ARCHIVE_CAP_MB * 1024 * 1024,
    "root": "",
    "dry_tmp": None,
}
ARCHIVE_KINDS = ("dirty", "unpushed", "detached-unreachable")
# Settings of the optional node_modules-only tier. Off unless --strip-node-modules / --strip-only is passed.
STRIP = {"enabled": False, "only": False, "idle_hours": DEFAULT_STRIP_IDLE_HOURS}
STRIP_DIR_NAME = "node_modules"

# Regenerable = never worth protecting (used by --protect-ignored, and to keep generated output out of archives).
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
ENV_COPY_PREFIX = ".env.development"  # also catches renamed copies (.notloaded, .bak): operator secrets, never archived
REGEN_NAMES = (
    ".env.development",
)  # husky-copied by scripts/copy-env-to-worktree.js; name only, never read
DATA_REGEN_CHILDREN = ("logs", "sources", "tmp")

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


# ----------------------------------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------------------------------
def _path_list(value):
    """os.pathsep-separated paths -> absolute, ~-expanded, de-duplicated list (empty entries dropped)."""
    out = []
    for part in (value or "").split(os.pathsep):
        part = part.strip()
        if part:
            p = os.path.abspath(os.path.expanduser(part))
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
        ARCHIVE_ROOT
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
    ARCHIVE_ROOT = os.path.abspath(
        os.path.expanduser(
            pick("ARCHIVE_ROOT", "archive_root")
            or os.path.join(H, ".ateles-work", "worktree-archive")
        )
    )
    ARCH["root"] = ARCHIVE_ROOT
    repos = _path_list(os.pathsep.join(cli.get("repos_dir") or [])) or _path_list(
        env.get(ENV_PREFIX + "REPOS_DIRS")
    )
    REPOS_DIRS = repos or [os.path.join(H, "repos")]
    never = [
        os.path.join(H, p[2:]) for p in DEFAULT_NEVER_TOUCH
    ]  # entries are '~/<name>'
    for p in _path_list(env.get(ENV_PREFIX + "NEVER_TOUCH")) + _path_list(
        os.pathsep.join(cli.get("never_touch") or [])
    ):
        if p not in never:
            never.append(p)
    NEVER = never
    prefixes = [
        p.strip()
        for p in (env.get(ENV_PREFIX + "HOLD_PREFIXES") or "").split(",")
        if p.strip()
    ]
    prefixes += [p for p in (cli.get("hold_prefix") or []) if p]
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


def tar_exclude(rel):
    """Untracked (non-ignored) files left OUT of an archive: generated / build output only."""
    parts = rel.split("/")
    if any(p in REGEN_ANY_DEPTH for p in parts):
        return True
    for t in REGEN_TOP_LEVEL:
        if t in (".husky",):
            continue
        if rel == t or rel.startswith(t + "/"):
            return True
    if parts[-1].startswith(ENV_COPY_PREFIX):
        return True
    return rel.endswith((".pyc", ".tsbuildinfo"))


# Secret-looking file names. The archive tier never copies such a file anywhere, so a worktree that
# holds one (untracked or ignored) is kept whole instead of being archived and removed. Decided on the
# name alone; file contents are never read. The ENV_COPY_PREFIX family is handled separately above
# (left out of archives, as before) and is not part of this list.
SECRET_EXACT = (".env", ".netrc", ".npmrc")
SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
SECRET_PREFIXES = ("credentials", "id_rsa", "id_ed25519")
SECRET_SUBSTRINGS = ("secret",)
SECRET_EXAMPLE_SUFFIXES = (".example", ".sample", ".template")
SECRET_SCAN_SKIP = ("node_modules", ".venv", "venv", "__pycache__", ".mcp-venv")


def secret_name(name):
    """True if a file's base name looks like it holds a secret (name only; the content is never read)."""
    n = name.lower()
    if n.startswith(ENV_COPY_PREFIX) or n.endswith(SECRET_EXAMPLE_SUFFIXES):
        return False
    return (
        n in SECRET_EXACT
        or n.startswith(".env.")
        or n.endswith(SECRET_SUFFIXES)
        or n.startswith(SECRET_PREFIXES)
        or any(s in n for s in SECRET_SUBSTRINGS)
    )


def secret_files(wt):
    """Relative paths of untracked or ignored files in the worktree whose names look secret.

    -> list, or None if it could not be decided (callers keep the worktree on None).
    Tracked files are not listed here: they live in git, not in the untracked archive.
    """
    rc, o, _ = run(["git", "-C", wt, "ls-files", "-z"], count=False)
    if rc != 0:
        return None
    tracked = set(o.split("\0"))
    hits = []
    try:
        for dp, dn, fn in os.walk(wt, followlinks=False, onerror=_raise):
            dn[:] = [d for d in dn if d != ".git" and d not in SECRET_SCAN_SKIP]
            for f in fn:
                if secret_name(f):
                    rel = os.path.relpath(os.path.join(dp, f), wt)
                    if rel not in tracked:
                        hits.append(rel)
    except OSError:
        return None
    return sorted(hits)


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


def log(*a):
    if LOGF:
        print(time.strftime("%H:%M:%S"), *a, file=LOGF, flush=True)


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
    rc, o, _ = run(["git", "-C", main, "worktree", "list", "--porcelain"])
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
        rc, o, _ = run(["git", "-C", main, "remote", "get-url", "origin"])
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
        [
            "git",
            "-C",
            wt,
            "for-each-ref",
            "--contains",
            head,
            "--count=1",
            "--format=%(refname)",
            "refs/remotes/origin/",
        ]
    )
    if rc == 0 and o.strip():
        return "origin"
    br = e.get("branch")
    if not br:
        return None  # detached and not on any origin ref: unreachable, never eligible
    oids = merged_heads(e["main"], br)
    if head in oids:
        return "merged-PR-head"
    for oid in oids:
        # exit 1 = "not an ancestor" (a legitimate answer); anything else is an error
        rc2, _, _ = run(
            ["git", "-C", wt, "merge-base", "--is-ancestor", head, oid], ok_rcs=(0, 1)
        )
        if rc2 == 0:
            return "ancestor-of-merged-PR-head"
    return None


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
    rc, o, _ = run(["git", "-C", wt, "status", "--porcelain"])
    if rc != 0:
        return None
    return not o.strip()


def ignored_entries(wt):
    rc, o, _ = run(["git", "-C", wt, "status", "--porcelain", "--ignored"], count=False)
    if rc != 0:
        return None
    return [line[3:] for line in o.splitlines() if line.startswith("!! ")]


def is_locked(e):
    rc, o, _ = run(["git", "-C", e["main"], "worktree", "list", "--porcelain"])
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
        return archive_gate(e)
    pv = pushed_via(e, e["head"])
    if not pv:
        e["status"] = "unpushed" if e.get("branch") else "detached-unreachable"
        return archive_gate(e)
    e["pushed_via"] = pv
    e["status"] = "ELIGIBLE"
    if want_size:
        e["kb"] = du_kb(wt)
    return e


def final_check_and_remove(e, apply_, protect_ignored):
    """Fresh re-check of EVERY condition, then (if apply_) remove. Returns a status string."""
    wt = e["wt"]
    if never_touch(wt) or not os.path.isdir(wt) or not is_linked_worktree(e):
        return "recheck-never-touch"
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
    rc, o, _ = run(["git", "-C", wt, "rev-parse", "HEAD"])
    h = o.strip()
    if rc != 0 or h != e["head"]:
        return "head-moved"
    r = recent_file(wt)
    if r:
        return "recent" if r == "recent" else "find-error"
    c = status_clean(wt)
    if not c:
        return "dirty" if c is False else "status-error"
    pv = pushed_via(e, h)
    if not pv:
        return "unpushed"
    ig = ignored_entries(wt)
    if ig is None and protect_ignored:
        return "status-error"
    odd = [x for x in (ig or []) if not ignored_is_regen(wt, x)]
    if odd:
        e["ignored_nonregen"] = odd[:6]
        if protect_ignored:
            return "has-ignored-files"
    e["kb"] = e.get("kb") or du_kb(wt)
    if not apply_:
        return "WOULD-REMOVE"
    rc, o, err = run(
        ["git", "-C", e["main"], "worktree", "remove", wt], timeout=600
    )  # no --force
    if rc == 0:
        return "REMOVED"
    e["detail"] = err.strip().replace("\n", " ")[:160]
    return "git-refused"


# ----------------------------------------------------------------------------------------------
# node_modules-only tier (--strip-node-modules / --strip-only). Deletes directories literally named
# `node_modules` inside a clean, pushed, idle, unused, unlocked linked worktree. The worktree stays.
# It deletes nothing else: never a symlink, never anything whose real path leaves the worktree.
# ----------------------------------------------------------------------------------------------
def node_modules_contained(wt, path):
    """True only if `path` is a real (non-symlink) directory literally named node_modules whose real path
    lies inside the worktree's real path. Checked per directory immediately before it is deleted."""
    if (
        os.path.basename(path) != STRIP_DIR_NAME
        or os.path.islink(path)
        or not os.path.isdir(path)
    ):
        return False
    rw = os.path.realpath(wt)
    rp = os.path.realpath(path)
    if os.path.basename(rp) != STRIP_DIR_NAME or not rp.startswith(rw + os.sep):
        return False
    # an ancestor that is a symlink would make the real path differ from the walked path
    return rp == os.path.join(os.path.realpath(os.path.dirname(path)), STRIP_DIR_NAME)


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
            if d == STRIP_DIR_NAME:
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
    rc, o, _ = run(
        ["git", "-C", wt, "ls-files", "-z", "--", ":(literal)" + rel], count=False
    )
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
    rc, o, _ = run(["git", "-C", wt, "rev-parse", "HEAD"])
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


# ----------------------------------------------------------------------------------------------
# Archive-then-remove tier (--archive-remove). A worktree that the plain tier would KEEP as dirty /
# unpushed / detached-unreachable (or that holds non-regenerable ignored files) is first written to a
# verified recovery archive outside every repo, re-checked unchanged, and only then force-removed.
# `--force` appears nowhere else in this file; the plain tier never uses it.
# ----------------------------------------------------------------------------------------------
class ArchiveError(Exception):
    def __init__(self, reason, detail=""):
        Exception.__init__(self, reason + (": " + detail if detail else ""))
        self.reason = reason
        self.detail = detail


def archive_gate(e):
    """analyze() hook: with the archive tier on, a kept-for-work worktree becomes a candidate if long idle."""
    if not (ARCH["enabled"] and e["status"] in ARCHIVE_KINDS):
        return e
    e["kind"] = e["status"]
    r = recent_file(e["wt"], max(ARCH["idle_hours"], FRESH_HOURS))
    if r == "error":
        e["status"] = "find-error"
    elif r:
        e["status"] = "not-idle"
    else:
        e["status"] = "ARCHIVE-CANDIDATE"
    return e


def outside_every_repo(path):
    """True if no ancestor of `path` is a git work tree / .git dir (so the archive is not inside any repo)."""
    p = os.path.realpath(path)
    while True:
        if os.path.exists(os.path.join(p, ".git")):
            return False
        parent = os.path.dirname(p)
        if parent == p:
            return True
        p = parent


def run_bin(args, cwd=None, env=None, timeout=900):
    try:
        p = subprocess.run(
            args, cwd=cwd, capture_output=True, timeout=timeout, env=env or GIT_ENV
        )
        return p.returncode, p.stdout, p.stderr
    except Exception as ex:
        return -1, b"", str(ex).encode()


def _mk_private_dir(path):
    """mkdir -p with every newly created component set to 0700 (umask-proof)."""
    missing = []
    p = path
    while not os.path.isdir(p):
        missing.append(p)
        p = os.path.dirname(p)
    for d in reversed(missing):
        os.mkdir(d, 0o700)
        os.chmod(d, 0o700)


def archive_dir_for(e, root, day):
    repo = os.path.basename(e["main"].rstrip("/"))
    base = os.path.basename(e["wt"].rstrip("/"))
    if (
        base == repo
    ):  # e.g. agent worktrees all named after the repo: add the parent for uniqueness
        base = os.path.basename(os.path.dirname(e["wt"].rstrip("/"))) + "-" + base
    name = "%s-%s" % (base, e["head"][:8])
    d = os.path.join(root, day, repo, name)
    n = 2
    while os.path.exists(d):
        d = os.path.join(root, day, repo, "%s-%d" % (name, n))
        n += 1
    return d


def _status_text(wt):
    rc, o, _ = run(["git", "-C", wt, "status", "--porcelain"], count=False)
    return o if rc == 0 else None


def _gather_files(e):
    """-> (tar_files [rel], ignored_included [entries]). Raises ArchiveError for nested repos / odd entries."""
    wt = e["wt"]
    rc, o, _ = run_bin(["git", "-C", wt, "ls-files", "-z", "-o", "--exclude-standard"])
    if rc != 0:
        raise ArchiveError("git-ls-files-failed")
    files = []
    for rel in o.decode("utf8", "surrogateescape").split("\0"):
        if not rel:
            continue
        if rel.endswith("/"):
            raise ArchiveError("nested-repo", rel)
        if not tar_exclude(rel):
            files.append(rel)
    rc, o, _ = run_bin(
        [
            "git",
            "-C",
            wt,
            "ls-files",
            "-z",
            "-o",
            "-i",
            "--exclude-standard",
            "--directory",
        ]
    )
    if rc != 0:
        raise ArchiveError("git-ls-files-failed")
    ignored, raw_budget = [], ARCH["cap"] * 8
    for ent in o.decode("utf8", "surrogateescape").split("\0"):
        if not ent or ignored_is_regen(wt, ent):
            continue
        ignored.append(ent)
        if ent.endswith("/"):
            for dp, dn, fn in os.walk(os.path.join(wt, ent), followlinks=False):
                reldp = os.path.relpath(dp, wt)
                dn[:] = [d for d in dn if not regen_path(os.path.join(reldp, d))]
                for f in fn:
                    rel = os.path.join(reldp, f)
                    if regen_path(rel):
                        continue
                    files.append(rel)
                    raw_budget -= 1
                    if raw_budget < 0:
                        raise ArchiveError("archive-too-big", "too many files")
        else:
            files.append(ent)
    if any(
        secret_name(os.path.basename(f)) for f in files
    ):  # defence in depth: the walk above found none
        raise ArchiveError("secrets-present")
    return sorted(set(files)), ignored


def build_archive(e, dest):
    """Write + verify the archive into directory `dest` (created 0700). Raises ArchiveError. Never truncates."""
    wt, head, cap = e["wt"], e["head"], ARCH["cap"]
    _mk_private_dir(dest)
    info = {"members": [], "bundle_commits": 0}
    # (a) bundle of local-only commits (HEAD + branch tip, minus anything on any remote)
    rc, o, _ = run(
        ["git", "-C", wt, "rev-list", "--count", "HEAD", "--not", "--remotes"],
        count=False,
    )
    if rc != 0 or not o.strip().isdigit():
        raise ArchiveError("rev-list-failed")
    info["bundle_commits"] = int(o.strip())
    if info["bundle_commits"] > 0:
        refs = ["HEAD"] + (["refs/heads/" + e["branch"]] if e.get("branch") else [])
        bpath = os.path.join(dest, "local-commits.bundle")
        rc, _, err = run(
            ["git", "-C", wt, "bundle", "create", bpath]
            + refs
            + ["--not", "--remotes"],
            count=False,
        )
        if rc != 0:
            raise ArchiveError("bundle-create-failed", err.strip()[:120])
        rc, _, err = run(["git", "-C", wt, "bundle", "verify", bpath], count=False)
        if rc != 0:
            raise ArchiveError("bundle-verify-failed", err.strip()[:120])
        rc, o, _ = run(["git", "-C", wt, "bundle", "list-heads", bpath], count=False)
        if head not in o:
            raise ArchiveError("bundle-missing-head")
        info["members"].append("local-commits.bundle")
    # (b) tracked changes (staged + unstaged) as a binary patch against HEAD
    rc, patch, err = run_bin(["git", "-C", wt, "diff", "--binary", "HEAD"])
    if rc != 0:
        raise ArchiveError("diff-failed", err.decode("utf8", "replace")[:120])
    ppath = os.path.join(dest, "patch.diff")
    with open(ppath, "wb") as f:
        f.write(patch)
    info["patch_bytes"] = len(patch)
    info["members"].append("patch.diff")
    if patch:
        # cheap exact check: does the patch apply to HEAD's tree? (private temp index; no checkout, worktree untouched)
        idx = os.path.join(dest, ".verify-index")
        env = dict(GIT_ENV, GIT_INDEX_FILE=idx)
        rc, _, err = run_bin(["git", "-C", wt, "read-tree", "HEAD"], env=env)
        if rc == 0:
            rc, _, err = run_bin(
                ["git", "-C", wt, "apply", "--cached", "--check", ppath], env=env
            )
        if os.path.exists(idx):
            os.remove(idx)
        if rc != 0:
            raise ArchiveError(
                "patch-apply-check-failed", err.decode("utf8", "replace")[:120]
            )
    # (c)+(d) untracked files and small non-regenerable ignored files
    files, ignored = _gather_files(e)
    info["ignored_included"] = ignored[:20]
    tpath = os.path.join(dest, "untracked.tar.gz")
    raw = 0
    with tarfile.open(tpath, "w:gz", compresslevel=6) as tf:
        for rel in files:
            full = os.path.join(wt, rel)
            try:
                st = os.lstat(full)
            except OSError as ex:
                raise ArchiveError("file-vanished", str(ex)[:80])
            if stat.S_ISREG(st.st_mode):
                raw += st.st_size
            elif not stat.S_ISLNK(st.st_mode):
                raise ArchiveError("special-file", rel[:80])
            if raw > cap * 8:
                raise ArchiveError("archive-too-big", "raw bytes")
            try:
                tf.add(full, arcname=rel, recursive=False)
            except Exception as ex:
                raise ArchiveError("tar-add-failed", str(ex)[:80])
            if os.path.getsize(tpath) > cap:
                raise ArchiveError("archive-too-big", "tar.gz over cap")
    rc, o, err = run(["tar", "-tzf", tpath], count=False)
    if rc != 0 or len([line for line in o.split("\n") if line]) != len(files):
        raise ArchiveError("tar-verify-failed", err.strip()[:80])
    info["members"].append("untracked.tar.gz")
    info["tar_members"] = len(files)
    info["tar_names"] = files[:200]
    # meta.json
    porcelain = (_status_text(wt) or "").splitlines()
    meta = {
        "format": 1,
        "tool": "worktree-prune",
        "repo": os.path.basename(e["main"].rstrip("/")),
        "main_clone": e["main"],
        "original_path": wt,
        "branch": e.get("branch"),
        "head": head,
        "kind": e.get("kind"),
        "local_only_commits": info["bundle_commits"],
        "status_porcelain": porcelain[:2000],
        "status_porcelain_lines": len(porcelain),
        "patch_bytes": info["patch_bytes"],
        "tar_members": len(files),
        "ignored_included": ignored[:50],
        "created": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "restore": "in the main clone: git worktree add --detach <dir> <head>; [git fetch local-commits.bundle if present]; "
        "git -C <dir> apply patch.diff; tar -xzf untracked.tar.gz -C <dir>",
    }
    with open(os.path.join(dest, "meta.json"), "w") as f:
        json.dump(meta, f, indent=1)
    info["members"].append("meta.json")
    total = 0
    for n in os.listdir(dest):
        os.chmod(os.path.join(dest, n), 0o600)
        total += os.path.getsize(os.path.join(dest, n))
    if total > cap:
        raise ArchiveError("archive-too-big", "%d bytes" % total)
    info["bytes"] = total
    return info


def _has_initialized_submodules(wt):
    """True if a submodule is checked out here (`git submodule status` line not starting with '-'), or on any doubt.
    Uninitialized gitlinks (the common case: .gitmodules is tracked but nothing was ever cloned) are fine."""
    if not os.path.exists(wt + "/.gitmodules"):
        return False
    rc, o, _ = run(["git", "-C", wt, "submodule", "status"], count=False, timeout=120)
    if rc != 0:
        return True
    return any(line and line[0] != "-" for line in o.splitlines())


def _still_unchanged(e, status_before, hours):
    """Fresh re-check used before and after archiving. Returns '' if fine, else a keep-reason."""
    wt = e["wt"]
    if never_touch(wt) or not os.path.isdir(wt) or not is_linked_worktree(e):
        return "recheck-never-touch"
    if _has_initialized_submodules(wt):
        return "has-submodules"
    rc, cw = cwdset()
    if rc != 0 and not cw:
        return "recheck-lsof-error"
    if in_use(wt, cw):
        return "in-use"
    lk = is_locked(e)
    if lk is None:
        return "recheck-list-error"
    if lk:
        return "locked"
    rc, o, _ = run(["git", "-C", wt, "rev-parse", "HEAD"])
    if rc != 0 or o.strip() != e["head"]:
        return "head-moved"
    r = recent_file(wt, hours)
    if r:
        return "not-idle" if r == "recent" else "find-error"
    if status_before is not None and _status_text(wt) != status_before:
        return "changed-during-archive"
    return ""


def _secrets_block(e):
    """'' if the worktree holds no secret-named untracked/ignored file, else the keep reason.

    The archive tier never copies such a file and never removes the worktree that holds one.
    """
    hits = secret_files(e["wt"])
    if hits is None:
        return "secrets-check-error"
    if hits:
        e["secrets"] = hits[:6]
        return "secrets-present"
    return ""


def archive_flow(e, apply_):
    """Archive, verify, re-check, then (apply) `git worktree remove --force`. Any failure keeps the worktree."""
    wt = e["wt"]
    hours = max(ARCH["idle_hours"], FRESH_HOURS)
    if not outside_every_repo(ARCH["root"]):
        return "archive-root-inside-repo"
    why = _still_unchanged(e, None, hours)
    if why:
        return why
    why = _secrets_block(e)
    if why:
        return why
    before = _status_text(wt)
    if before is None:
        return "status-error"
    if apply_:
        dest_final = archive_dir_for(e, ARCH["root"], datetime.date.today().isoformat())
        dest = dest_final + ".partial"
        _mk_private_dir(os.path.dirname(dest))
    else:
        if not ARCH.get("dry_tmp"):
            return "archive-no-dry-tmp"
        dest_final = None
        dest = os.path.join(
            ARCH["dry_tmp"], "%s-%s" % (os.path.basename(wt.rstrip("/")), e["head"][:8])
        )
    try:
        info = build_archive(e, dest)
    except ArchiveError as ex:
        shutil.rmtree(dest, ignore_errors=True)
        e["detail"] = str(ex)[:160]
        return (
            ex.reason
            if ex.reason in ("archive-too-big", "nested-repo", "secrets-present")
            else "archive-failed"
        )
    except Exception as ex:
        shutil.rmtree(dest, ignore_errors=True)
        e["detail"] = ("unexpected: " + str(ex))[:160]
        return "archive-failed"
    e["archive_bytes"] = info["bytes"]
    e["archive_members"] = info["members"]
    e["archive_tar_names"] = info.get("tar_names", [])
    e["archive_bundle_commits"] = info["bundle_commits"]
    e["archive_ignored"] = info.get("ignored_included", [])
    why = _still_unchanged(e, before, hours) or _secrets_block(e)
    if why:
        shutil.rmtree(dest, ignore_errors=True)
        return why
    e["kb"] = e.get("kb") or du_kb(wt)
    if not apply_:
        shutil.rmtree(dest, ignore_errors=True)
        return "WOULD-ARCHIVE-REMOVE"
    os.rename(dest, dest_final)
    os.chmod(dest_final, 0o700)
    e["archive_dir"] = dest_final
    rc, o, err = run(
        ["git", "-C", e["main"], "worktree", "remove", "--force", wt],
        timeout=900,
        count=False,
    )
    if rc == 0:
        return "ARCHIVED-REMOVED"
    e["detail"] = err.strip().replace("\n", " ")[
        :160
    ]  # archive is kept: the tree may be partly deleted
    return "git-refused"


def act(e, apply_, protect_ignored):
    """Decide + act on one analyzed entry. Returns the final status string."""
    st = e["status"]
    if st == "ARCHIVE-CANDIDATE":
        return archive_flow(e, apply_)
    if st != "ELIGIBLE":
        return st
    res = final_check_and_remove(e, apply_, protect_ignored)
    if res == "has-ignored-files" and ARCH["enabled"]:
        e["kind"] = "has-ignored-files"
        if recent_file(e["wt"], max(ARCH["idle_hours"], FRESH_HOURS)):
            return "not-idle"
        return archive_flow(e, apply_)
    return res


ARCHIVE_MEMBER_NAMES = (
    "meta.json",
    "patch.diff",
    "untracked.tar.gz",
    "local-commits.bundle",
)
ARCHIVE_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _real_subdirs(path):
    """Entries of `path` if every one is a real (non-symlink) directory and there is at least one, else None."""
    try:
        with os.scandir(path) as it:
            entries = list(it)
    except OSError:
        return None
    if not entries or any(
        x.is_symlink() or not x.is_dir(follow_symlinks=False) for x in entries
    ):
        return None
    return [x.path for x in entries]


def archive_day_dir_matches(day_dir):
    """True only if `day_dir` has exactly the layout this tool writes:
    <date dir>/<repo dir>/<archive dir>/{meta.json plus any of patch.diff, untracked.tar.gz, local-commits.bundle}.

    Anything else (a file at the top level, a symlink, an unknown member, an archive without meta.json,
    an unfinished `.partial` directory) is foreign, and --prune-archives --apply never deletes it.
    """
    if os.path.islink(day_dir):
        return False
    repo_dirs = _real_subdirs(day_dir)
    if repo_dirs is None:
        return False
    for repo_dir in repo_dirs:
        archive_dirs = _real_subdirs(repo_dir)
        if archive_dirs is None:
            return False
        for archive_dir in archive_dirs:
            if archive_dir.endswith(".partial"):
                return False
            try:
                with os.scandir(archive_dir) as it:
                    members = list(it)
            except OSError:
                return False
            names = [m.name for m in members]
            if "meta.json" not in names:
                return False
            for m in members:
                if (
                    m.name not in ARCHIVE_MEMBER_NAMES
                    or m.is_symlink()
                    or not m.is_file(follow_symlinks=False)
                ):
                    return False
    return True


def prune_archives(apply_, retention_days):
    """List (dry run) or delete (--apply) archive day-directories older than the retention. Never touches repos.

    Only a date-named directory whose layout matches what this tool writes is ever listed or deleted;
    any other date-named directory is skipped and logged.
    """
    root = ARCH["root"]
    cutoff = datetime.date.today() - datetime.timedelta(days=retention_days)
    old, keep, skipped = [], 0, []
    if os.path.isdir(root):
        for d in sorted(os.listdir(root)):
            if not ARCHIVE_DAY_RE.match(d):
                continue
            try:
                day = datetime.date.fromisoformat(d)
            except ValueError:
                continue
            path = os.path.join(root, d)
            if day >= cutoff:
                keep += 1
            elif archive_day_dir_matches(path):
                old.append(path)
            else:
                skipped.append(d)
                log(
                    "SKIP prune-archives %s: layout is not what this tool writes" % path
                )
    total = 0
    for d in old:
        for dp, _, fn in os.walk(d):
            for f in fn:
                try:
                    total += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        if apply_:
            shutil.rmtree(d, ignore_errors=True)
    return {
        "old_day_dirs": [os.path.basename(d) for d in old],
        "skipped_foreign_dirs": skipped,
        "kept_day_dirs": keep,
        "mb": round(total / 1048576, 2),
        "deleted": bool(apply_),
    }


def rotate_logs():
    cutoff = time.time() - 30 * 86400
    try:
        for n in os.listdir(LOGDIR):
            p = os.path.join(LOGDIR, n)
            if (
                re.match(r"^\d{4}-\d{2}-\d{2}\.log$", n)
                and os.path.getmtime(p) < cutoff
            ):
                os.remove(p)
            elif n.startswith("launchd.") and os.path.getsize(p) > 1048576:
                open(p, "w").close()
    except Exception:
        pass


def write_summary(d):
    with open(RUNS, "a") as f:
        f.write(json.dumps(d, sort_keys=True) + "\n")


REMOVAL_RESULTS = (
    "REMOVED",
    "WOULD-REMOVE",
    "ARCHIVED-REMOVED",
    "WOULD-ARCHIVE-REMOVE",
)
# Statuses that put a worktree out of reach of every tier: do not even attempt the node_modules tier on them.
STRIP_SKIP_STATUS = (
    "never-touch",
    "prunable",
    "missing",
    "not-linked-worktree",
    "no-head",
    "locked",
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
    ap.add_argument("--workers", type=int, default=6)
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
        help="run only the node_modules-only tier: no whole-worktree removal, no archive tier (implies --strip-node-modules)",
    )
    ap.add_argument(
        "--archive-remove",
        action="store_true",
        help="archive-then-remove tier: worktrees idle >= --idle-hours that would be kept as dirty/unpushed/"
        "detached (or hold non-regenerable ignored files) get a verified recovery archive under the "
        "archive root, then `git worktree remove --force` (off by default)",
    )
    ap.add_argument(
        "--idle-hours",
        type=int,
        default=None,
        help="idle window, hours: no file modified (outside node_modules/.git/dist/build) in this long. "
        "Default %d for the node_modules tier, %d for the archive tier; a value given here applies to both"
        % (DEFAULT_STRIP_IDLE_HOURS, DEFAULT_ARCHIVE_IDLE_HOURS),
    )
    ap.add_argument(
        "--archive-cap-mb",
        type=int,
        default=ARCHIVE_CAP_MB,
        help="archive tier: skip (never truncate) a worktree whose archive would exceed this many MB (default 25)",
    )
    ap.add_argument(
        "--prune-archives",
        action="store_true",
        help="list archive day-directories older than --archive-retention-days (with --apply: delete them) and exit",
    )
    ap.add_argument(
        "--archive-retention-days", type=int, default=ARCHIVE_RETENTION_DAYS
    )
    ap.add_argument(
        "--state-dir",
        help="lock, runs.jsonl and logs (env %sSTATE_DIR; default ~/.ateles-work/worktree-prune)"
        % ENV_PREFIX,
    )
    ap.add_argument(
        "--archive-root",
        help="archive root (env %sARCHIVE_ROOT; default ~/.ateles-work/worktree-archive)"
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
    """(node_modules tier hours, archive tier hours) for the `--idle-hours` value (None = flag not given).

    The flag is shared: left unset each tier keeps its own default, given explicitly it sets both.
    """
    if idle_hours_arg is None:
        return DEFAULT_STRIP_IDLE_HOURS, DEFAULT_ARCHIVE_IDLE_HOURS
    n = max(1, idle_hours_arg)
    return n, n


def main(argv=None):
    global FRESH_HOURS, LOGF
    a = build_parser().parse_args(argv)
    warnings = configure(
        os.environ,
        {
            "state_dir": a.state_dir,
            "archive_root": a.archive_root,
            "repos_dir": a.repos_dir,
            "never_touch": a.never_touch,
            "hold_prefix": a.hold_prefix,
            "hold_until": a.hold_until,
            "yield_to_file": a.yield_to_file,
        },
    )
    FRESH_HOURS = max(1, a.fresh_hours)
    STRIP["only"] = bool(a.strip_only)
    STRIP["enabled"] = bool(a.strip_node_modules or a.strip_only)
    ARCH["enabled"] = bool(a.archive_remove) and not a.strip_only
    STRIP["idle_hours"], ARCH["idle_hours"] = idle_windows(a.idle_hours)
    ARCH["cap"] = max(1, a.archive_cap_mb) * 1024 * 1024

    os.makedirs(LOGDIR, exist_ok=True)
    t0 = time.time()
    ts = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    mode = "apply" if a.apply else "dry-run"

    lockf = open(LOCK, "a+")
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

    LOGF = open(
        "%s/%s.log" % (LOGDIR, datetime.date.today().isoformat()), "a", buffering=1
    )
    for w in warnings:
        log("WARN " + w)
    rotate_logs()
    if a.prune_archives:
        res = prune_archives(a.apply, a.archive_retention_days)
        log("prune-archives " + json.dumps(res, sort_keys=True))
        write_summary(dict(res, ts=ts, mode=mode, status="ok", action="prune-archives"))
        print(json.dumps(res, sort_keys=True))
        return 0
    if (
        ARCH["enabled"] and not a.apply
    ):  # dry run: build + verify archives in a throwaway dir, then discard them
        ARCH["dry_tmp"] = tempfile.mkdtemp(prefix="dryrun-archive-", dir=BASE)
        atexit.register(shutil.rmtree, ARCH["dry_tmp"], True)
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
    archived = 0
    would_archive = 0
    arch_bytes = 0
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
                if st in ("ELIGIBLE", "ARCHIVE-CANDIDATE"):
                    if a.apply and removed + len(stripped) >= a.max:
                        reasons["over-cap-deferred"] += 1
                        log("DEFER over-cap %s" % e["wt"])
                        continue
                    res = act(e, a.apply, a.protect_ignored)
                    if res in REMOVAL_RESULTS:
                        kb_total += e.get("kb", 0)
                        arch_tier = (
                            res.endswith("ARCHIVE-REMOVE") or res == "ARCHIVED-REMOVED"
                        )
                        if arch_tier:
                            arch_bytes += e.get("archive_bytes", 0)
                            log(
                                "%s %s (%s) %.2fGB kind=%s archive=%.2fMB commits=%d dir=%s"
                                % (
                                    res,
                                    e["wt"],
                                    e.get("branch") or "detached",
                                    e.get("kb", 0) / 1048576,
                                    e.get("kind"),
                                    e.get("archive_bytes", 0) / 1048576,
                                    e.get("archive_bundle_commits", 0),
                                    e.get("archive_dir", "(dry)"),
                                )
                            )
                        else:
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
                        if res in ("REMOVED", "ARCHIVED-REMOVED"):
                            removed += 1
                            if arch_tier:
                                archived += 1
                        else:
                            e["tier"] = "archive" if arch_tier else "plain"
                            eligible.append(e)
                            if arch_tier:
                                would_archive += 1
                    else:
                        reasons[res] += 1
                        log(
                            "KEEP %-18s %s %s%s"
                            % (
                                res,
                                e["wt"],
                                e.get("detail", ""),
                                " secret-named=" + ",".join(e["secrets"])
                                if e.get("secrets")
                                else "",
                            )
                        )
                else:
                    reasons[st] += 1
                    if st != "never-touch":
                        log(
                            "KEEP %-18s %s %s"
                            % (st, e["wt"], e.get("branch") or "(detached)")
                        )
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
            _, o, _ = run(
                ["git", "-C", m, "worktree", "list", "--porcelain"], count=False
            )
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
                ["git", "-C", m, "worktree", "prune"]
                + ([] if a.apply else ["--dry-run"]),
                count=False,
            )
        log(
            "worktree prune: %d admin entries %s"
            % (pruned, "pruned" if a.apply else "would be pruned")
        )

    free1 = dfree_gb()
    strip_done = stripped if a.apply else would_strip
    summ = {
        "ts": ts,
        "mode": mode,
        "status": status,
        "discovered": len(entries),
        "removed": removed,
        "would_remove": len(eligible),
        "archive_tier": ARCH["enabled"],
        "archived_removed": archived,
        "would_archive_remove": would_archive,
        "archive_mb": round(arch_bytes / 1048576, 2),
        "would_remove_over_cap": max(0, len(eligible) - a.max) if not a.apply else 0,
        "gb": round(kb_total / 1048576, 2),
        "kept": dict(reasons),
        "secrets_present": reasons.get("secrets-present", 0),
        "prunable_entries": pruned,
        "git_gh_calls": STATS.calls,
        "git_gh_errors": STATS.errors,
        "free_gb_before": round(free0, 1),
        "free_gb_after": round(free1, 1),
        "low_disk": 0 <= free1 < 5,
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
            "kind",
            "archive_bytes",
            "archive_members",
            "archive_bundle_commits",
            "archive_ignored",
            "archive_tar_names",
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
                % (e["wt"], e.get("kb", 0) / 1048576, e.get("pushed_via") or "archive")
            )
        for e in strip_done:
            print(
                "%s\t%.2fMB\tnode_modules"
                % (e["wt"], e.get("strip_bytes", 0) / 1048576)
            )
    print(json.dumps(summ, sort_keys=True))
    return 0 if status == "ok" else 2


configure()

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
