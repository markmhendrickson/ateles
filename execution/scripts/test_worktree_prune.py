"""Tests for worktree_prune.py: throwaway repos and worktrees under a temp dir.

Every test builds a bare "origin", a clone of it (the main clone) and linked worktrees
inside a temporary HOME, so the pruner's refusal to touch anything outside $HOME is
exercised for real and nothing on the host is ever read or deleted.

Sections:
  plain tier       never removes dirty / local-only / recent / locked / in-use work
  archive tier     archive, verify, restore, size cap, safety skips
  node_modules     the node_modules-only tier, one test per safety
  configuration    env / CLI configuration, the hold, the 24 hour defaults

Run: python3 -m pytest execution/scripts/test_worktree_prune.py -q
"""

from __future__ import annotations

import datetime
import inspect
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import worktree_prune as wp  # noqa: E402

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@t",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}
CONFIG_NAMES = (
    "H",
    "REPOS_DIRS",
    "BASE",
    "LOGDIR",
    "RUNS",
    "LOCK",
    "MANUAL_PROGRESS",
    "NEVER",
    "HOLD_PREFIXES",
    "HOLD_UNTIL",
    "ARCHIVE_ROOT",
    "FRESH_HOURS",
    "GIT_ENV",
    "LOGF",
)


def sh(*args, cwd=None, ok=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_IDENTITY)
    r = subprocess.run(args, cwd=cwd, capture_output=True, text=True, env=env)
    if ok:
        assert r.returncode == 0, (args, r.stderr)
    return r.stdout.strip()


def age(path, hours=120):
    """Set every file's mtime (never following symlinks, never touching .git) to `hours` ago."""
    old = time.time() - hours * 3600
    for dp, dn, fn in os.walk(path):
        dn[:] = [d for d in dn if d != ".git"]
        for f in fn:
            try:
                os.utime(os.path.join(dp, f), (old, old), follow_symlinks=False)
            except OSError:
                pass


def snapshot(wt, skip=(".git", "node_modules", "tmp")):
    """Relative path -> bytes for every file outside `skip`, to prove nothing else changed."""
    out = {}
    for dp, dn, fn in os.walk(wt):
        dn[:] = [d for d in dn if d not in skip]
        for f in fn:
            if f == ".git" and dp == wt:
                continue
            p = os.path.join(dp, f)
            out[os.path.relpath(p, wt)] = open(p, "rb").read()
    return out


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A temp HOME holding a bare origin, a main clone with one pushed commit, and helpers."""
    home = tmp_path.resolve() / "home"
    home.mkdir()
    saved = {k: getattr(wp, k) for k in CONFIG_NAMES}
    saved_arch, saved_strip = dict(wp.ARCH), dict(wp.STRIP)
    monkeypatch.setenv("HOME", str(home))
    wp.configure(env={}, cli={})
    wp.GIT_ENV = dict(wp.GIT_ENV, **GIT_IDENTITY)
    wp.STATS.calls = wp.STATS.errors = 0
    wp.STATS.aborted = False
    wp._merged.clear()
    wp._slug.clear()
    wp.FRESH_HOURS = wp.DEFAULT_FRESH_HOURS
    wp.ARCH.update(enabled=False, idle_hours=72, dry_tmp=None)
    wp.STRIP.update(enabled=True, only=False, idle_hours=24)
    procs = []

    origin = str(home / "origin.git")
    main = str(home / "repos" / "main")
    sh("git", "init", "-q", "--bare", origin)
    os.makedirs(main)
    sh("git", "init", "-q", "-b", "main", main)
    sh("git", "remote", "add", "origin", origin, cwd=main)
    os.makedirs(main + "/src")
    open(main + "/src/app.js", "w").write("console.log(1)\n")
    open(main + "/a.txt", "w").write("a\n")
    open(main + "/del.txt", "w").write("to be deleted\n")
    open(main + "/bin.dat", "wb").write(os.urandom(2000))
    open(main + "/.gitignore", "w").write(
        "node_modules\ndist\nbuild\ndata_backups/\ntmp/\n.env.development\n.vitest/\n"
        "site_pages/\npublic/\n.husky/_/\ndata/\n"
    )
    sh("git", "add", ".", cwd=main)
    sh("git", "commit", "-qm", "init", cwd=main)
    sh("git", "push", "-q", "origin", "main", cwd=main)
    sh("git", "fetch", "-q", "origin", cwd=main)

    r = SimpleNamespace(home=str(home), origin=origin, main=main, procs=procs, wts={})
    r.mainset = {os.path.realpath(main)}

    def add_wt(name, base="origin/main", old=120, node_modules=True):
        """A linked worktree whose files are `old` hours old, with a small node_modules tree."""
        w = str(home / ("wt-" + name))
        sh("git", "worktree", "add", "-q", "-b", "b-" + name, w, base, cwd=main)
        if node_modules:
            os.makedirs(w + "/node_modules/pkg/lib")
            open(w + "/node_modules/pkg/index.js", "w").write("x" * 100)
            open(w + "/node_modules/pkg/lib/util.js", "w").write("y" * 50)
        age(w, old)
        r.wts[name] = w
        return w

    def entry(wt):
        return [e for e in wp.list_worktrees(main, r.mainset) if e["wt"] == wt][0]

    def spawn_in(path):
        p = subprocess.Popen(["sleep", "60"], cwd=path)
        procs.append(p)
        time.sleep(0.4)
        return p

    r.add_wt, r.entry, r.spawn_in = add_wt, entry, spawn_in
    yield r
    for p in procs:
        p.kill()
        p.wait()
    for w in list(r.wts.values()) + [str(home / "b-locked")]:
        subprocess.run(
            ["git", "-C", main, "worktree", "unlock", w], capture_output=True
        )
    for k, v in saved.items():
        setattr(wp, k, v)
    wp.ARCH.clear()
    wp.ARCH.update(saved_arch)
    wp.STRIP.clear()
    wp.STRIP.update(saved_strip)


# ======================================================================================
# plain tier (the original self-test, Part A)
# ======================================================================================
def test_plain_tier_never_removes_unsafe_worktrees(repo):
    fails = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    main = repo.main
    wts = {}
    for n in (
        "clean",
        "localonly",
        "dirty",
        "recent",
        "locked",
        "detached",
        "inuse",
        "untracked",
    ):
        wts[n] = repo.add_wt(n, node_modules=False)
    open(wts["localonly"] + "/new.txt", "w").write("x\n")
    sh("git", "add", ".", cwd=wts["localonly"])
    sh("git", "commit", "-qm", "local only", cwd=wts["localonly"])
    sh("git", "checkout", "-q", "--detach", cwd=wts["detached"])
    open(wts["detached"] + "/d.txt", "w").write("d\n")
    sh("git", "add", ".", cwd=wts["detached"])
    sh("git", "commit", "-qm", "detached local", cwd=wts["detached"])
    open(wts["dirty"] + "/a.txt", "a").write("change\n")
    open(wts["untracked"] + "/scratch.txt", "w").write("s\n")
    sh("git", "worktree", "lock", wts["locked"], cwd=main)
    for w in wts.values():
        age(w)
    os.utime(wts["recent"] + "/a.txt", None)
    repo.spawn_in(wts["inuse"])

    ents = {}
    for e in wp.list_worktrees(main, repo.mainset):
        ents[os.path.basename(e["wt"])[3:]] = e
    check("discovered all 8 linked worktrees (main clone excluded)", len(ents) == 8)

    expect = {
        "clean": "ELIGIBLE",
        "localonly": "unpushed",
        "dirty": "dirty",
        "recent": "recent",
        "locked": "locked",
        "detached": "detached-unreachable",
        "untracked": "dirty",
        "inuse": "ELIGIBLE",
    }
    for n, want in expect.items():
        got = wp.analyze(ents[n], False)["status"]
        check("analyze[%s] == %s (got %s)" % (n, want, got), got == want)
    res = wp.final_check_and_remove(ents["inuse"], False, False)
    check(
        "final check: process cwd inside worktree -> in-use (got %s)" % res,
        res == "in-use",
    )
    res = wp.final_check_and_remove(ents["clean"], False, False)
    check(
        "dry-run final check on clean+pushed -> WOULD-REMOVE (got %s)" % res,
        res == "WOULD-REMOVE",
    )
    check("dry run removed nothing", os.path.isdir(wts["clean"]))
    for e in ents.values():
        wp.final_check_and_remove(e, True, False)
    check("apply removed the clean+pushed worktree", not os.path.exists(wts["clean"]))
    for n in (
        "localonly",
        "dirty",
        "recent",
        "locked",
        "detached",
        "untracked",
        "inuse",
    ):
        check("apply kept %s" % n, os.path.isdir(wts[n]))
    check(
        "local-only branch ref intact",
        bool(sh("git", "rev-parse", "--verify", "b-localonly", cwd=main)),
    )
    check(
        "branch b-clean not deleted",
        bool(sh("git", "rev-parse", "--verify", "b-clean", cwd=main)),
    )
    check("archive tier is off by default", wp.ARCH["enabled"] is False)
    for n in ("dirty", "localonly", "detached", "untracked"):
        check(
            "default analyze never yields ARCHIVE-CANDIDATE [%s]" % n,
            wp.analyze(ents[n], False)["status"] != "ARCHIVE-CANDIDATE",
        )
    assert not fails, fails


# ======================================================================================
# archive tier (the original self-test, Part B)
# ======================================================================================
def test_archive_tier_archives_verifies_and_keeps_unsafe_worktrees(repo):
    fails = []

    def check(name, cond):
        if not cond:
            fails.append(name)

    main, origin, tmp = repo.main, repo.origin, repo.home
    aroot = tmp + "/archive"
    dry = tmp + "/dry"
    os.mkdir(dry)
    wp.ARCH.update(
        enabled=True, idle_hours=72, cap=25 * 1024 * 1024, root=aroot, dry_tmp=dry
    )
    today = datetime.date.today().isoformat()
    bm = {os.path.realpath(main)}

    def mk(name, base="origin/main"):
        w = tmp + "/b-" + name
        sh("git", "worktree", "add", "-q", "-b", "bb-" + name, w, base, cwd=main)
        return w

    def get(w):
        return [x for x in wp.list_worktrees(main, bm) if x["wt"] == w][0]

    def restore(adir, label):
        """Rebuild the worktree from the archive alone in a fresh clone of origin; return its file snapshot."""
        meta = json.load(open(adir + "/meta.json"))
        rd = tmp + "/restore-" + label
        sh("git", "clone", "-q", origin, rd + "-clone")
        if os.path.exists(adir + "/local-commits.bundle"):
            sh(
                "git",
                "fetch",
                "-q",
                adir + "/local-commits.bundle",
                "HEAD:refs/heads/restored",
                cwd=rd + "-clone",
            )
        sh(
            "git",
            "worktree",
            "add",
            "-q",
            "--detach",
            rd,
            meta["head"],
            cwd=rd + "-clone",
        )
        if os.path.getsize(adir + "/patch.diff"):
            sh("git", "apply", adir + "/patch.diff", cwd=rd)
        sh("tar", "-xzf", adir + "/untracked.tar.gz", "-C", rd)
        return rd, snapshot(rd)

    # (i) dirty tracked edit + staged add + deletion + binary edit + untracked + ignored small data
    w = mk("dirty")
    open(w + "/a.txt", "a").write("edit one\n")
    open(w + "/bin.dat", "wb").write(os.urandom(1500))
    os.remove(w + "/del.txt")
    open(w + "/staged.txt", "w").write("staged\n")
    sh("git", "add", "staged.txt", cwd=w)
    os.makedirs(w + "/notes")
    open(w + "/notes/n.md", "w").write("untracked note\n")
    os.makedirs(w + "/data_backups")
    open(w + "/data_backups/db.sql", "w").write("select 1;\n")
    os.makedirs(w + "/node_modules/x")
    open(w + "/node_modules/x/y.js", "w").write("regen\n")
    os.makedirs(w + "/tmp")
    open(w + "/tmp/scratch.txt", "w").write("regen\n")
    age(w)
    snap = snapshot(w)
    e = get(w)
    check(
        "dirty worktree idle>=72h becomes ARCHIVE-CANDIDATE",
        wp.analyze(e, False)["status"] == "ARCHIVE-CANDIDATE",
    )
    res = wp.act(e, False, False)
    check(
        "dry run -> WOULD-ARCHIVE-REMOVE (got %s)" % res, res == "WOULD-ARCHIVE-REMOVE"
    )
    check(
        "dry run removed nothing and left no archive behind",
        os.path.isdir(w) and not os.path.exists(aroot) and os.listdir(dry) == [],
    )
    res = wp.act(e, True, False)
    check(
        "apply -> ARCHIVED-REMOVED (got %s, %s)" % (res, e.get("detail")),
        res == "ARCHIVED-REMOVED",
    )
    check("worktree removed", not os.path.exists(w))
    adir = e.get("archive_dir")
    check(
        "archive dir exists under root/date/repo/<dir>-<sha>",
        bool(adir)
        and adir.startswith("%s/%s/main/b-dirty-%s" % (aroot, today, e["head"][:8]))
        and os.path.isdir(adir),
    )
    names = set(os.listdir(adir)) if adir else set()
    check(
        "archive holds patch.diff, untracked.tar.gz, meta.json and no bundle (nothing local-only)",
        {"patch.diff", "untracked.tar.gz", "meta.json"} <= names
        and "local-commits.bundle" not in names,
    )
    meta = json.load(open(adir + "/meta.json"))
    check(
        "meta.json records repo/branch/head/status/created/original path",
        meta["repo"] == "main"
        and meta["branch"] == "bb-dirty"
        and meta["head"] == e["head"]
        and meta["original_path"] == w
        and meta["status_porcelain"]
        and meta["created"],
    )
    lst = sh("tar", "-tzf", adir + "/untracked.tar.gz").split("\n")
    check(
        "tar has untracked + small ignored non-regenerable (notes/n.md, data_backups/db.sql)",
        "notes/n.md" in lst and "data_backups/db.sql" in lst,
    )
    check(
        "tar excludes node_modules and tmp (regenerable)",
        not any(x.startswith(("node_modules", "tmp")) for x in lst),
    )
    rd, rsnap = restore(adir, "i")
    check(
        "restore (worktree add + apply patch + extract tar) reproduces the exact files",
        rsnap == snap,
    )
    check(
        "branch not deleted",
        bool(sh("git", "rev-parse", "--verify", "bb-dirty", cwd=main)),
    )

    # (ii) unpushed commit (+ dirty edit) bundled and restorable; and a detached local-only commit
    w2 = mk("unpushed")
    open(w2 + "/l.txt", "w").write("local commit\n")
    sh("git", "add", "l.txt", cwd=w2)
    sh("git", "commit", "-qm", "local", cwd=w2)
    age(w2)
    snap2 = snapshot(w2)
    e2 = get(w2)
    wp.analyze(e2, False)
    check(
        "unpushed worktree is ARCHIVE-CANDIDATE",
        e2["status"] == "ARCHIVE-CANDIDATE" and e2["kind"] == "unpushed",
    )
    res = wp.act(e2, True, False)
    a2 = e2.get("archive_dir")
    check(
        "apply -> ARCHIVED-REMOVED (got %s, %s)" % (res, e2.get("detail")),
        res == "ARCHIVED-REMOVED" and not os.path.exists(w2),
    )
    check(
        "local-commits.bundle present and verifies",
        os.path.exists(a2 + "/local-commits.bundle")
        and subprocess.run(
            ["git", "-C", main, "bundle", "verify", a2 + "/local-commits.bundle"],
            capture_output=True,
        ).returncode
        == 0,
    )
    rd2, rsnap2 = restore(a2, "ii")
    check(
        "restore from bundle reproduces exact files incl. the local commit",
        rsnap2 == snap2 and "l.txt" in rsnap2,
    )
    w3 = mk("detached")
    sh("git", "checkout", "-q", "--detach", cwd=w3)
    open(w3 + "/det.txt", "w").write("only reachable from this HEAD\n")
    sh("git", "add", ".", cwd=w3)
    sh("git", "commit", "-qm", "det", cwd=w3)
    age(w3)
    sha3 = sh("git", "-C", w3, "rev-parse", "HEAD")
    e3 = get(w3)
    wp.analyze(e3, False)
    res = wp.act(e3, True, False)
    check(
        "detached-unreachable archived (got %s)" % res,
        res == "ARCHIVED-REMOVED" and e3["kind"] == "detached-unreachable",
    )
    rd3, rsnap3 = restore(e3["archive_dir"], "iii")
    check(
        "detached commit restorable from bundle",
        sh("git", "-C", rd3, "rev-parse", "HEAD") == sha3 and "det.txt" in rsnap3,
    )

    w2b = mk("unpushed-dirty")  # local commit AND a dirty edit on top
    open(w2b + "/l2.txt", "w").write("local commit 2\n")
    sh("git", "add", "l2.txt", cwd=w2b)
    sh("git", "commit", "-qm", "local2", cwd=w2b)
    open(w2b + "/a.txt", "a").write("dirty on top\n")
    open(w2b + "/untracked-new.txt", "w").write("u\n")
    age(w2b)
    snap2b = snapshot(w2b)
    e2b = get(w2b)
    wp.analyze(e2b, False)
    res = wp.act(e2b, True, False)
    rd2b, rsnap2b = restore(e2b["archive_dir"], "ii-b")
    check(
        "local commit + dirty edit + untracked archived and restored exactly (got %s)"
        % res,
        res == "ARCHIVED-REMOVED" and rsnap2b == snap2b,
    )

    # (iii) an archive over the cap is skipped, worktree kept, nothing left behind
    w4 = mk("big")
    open(w4 + "/huge.bin", "wb").write(os.urandom(26 * 1024 * 1024))
    age(w4)
    e4 = get(w4)
    wp.analyze(e4, False)
    before_dirs = sorted(os.listdir(aroot + "/" + today + "/main"))
    res = wp.act(e4, True, False)
    check(">25MB archive -> archive-too-big (got %s)" % res, res == "archive-too-big")
    check(
        "worktree kept and file intact",
        os.path.getsize(w4 + "/huge.bin") == 26 * 1024 * 1024,
    )
    check(
        "no partial/oversize archive left",
        sorted(os.listdir(aroot + "/" + today + "/main")) == before_dirs,
    )

    # (iv) a worktree modified 1 minute ago is NOT touched
    w5 = mk("fresh")
    open(w5 + "/a.txt", "a").write("just now\n")
    age(w5, 120)
    os.utime(w5 + "/a.txt", (time.time() - 60, time.time() - 60))
    e5 = get(w5)
    wp.analyze(e5, False)
    res = wp.act(e5, True, False)
    check(
        "modified 1 minute ago -> recent, untouched (got %s)" % res,
        res == "recent" and os.path.isdir(w5),
    )
    w5b = mk(
        "notidle"
    )  # modified 60h ago: past the 24h plain freshness, inside the 72h idle window
    open(w5b + "/a.txt", "a").write("60h ago\n")
    age(w5b, 60)
    e5b = get(w5b)
    wp.analyze(e5b, False)
    res = wp.act(e5b, True, False)
    check(
        "modified 60h ago (< idle 72h) -> not-idle, untouched (got %s)" % res,
        res == "not-idle" and os.path.isdir(w5b),
    )

    # (v) locked and in-use are NOT touched
    w6 = mk("locked")
    open(w6 + "/a.txt", "a").write("locked dirty\n")
    age(w6)
    sh("git", "worktree", "lock", w6, cwd=main)
    e6 = get(w6)
    wp.analyze(e6, False)
    res = wp.act(e6, True, False)
    check("locked -> untouched (got %s)" % res, res == "locked" and os.path.isdir(w6))
    w7 = mk("inuse")
    open(w7 + "/a.txt", "a").write("in use dirty\n")
    age(w7)
    repo.spawn_in(w7)
    e7 = get(w7)
    wp.analyze(e7, False)
    check(
        "in-use worktree still reaches the archive gate",
        e7["status"] == "ARCHIVE-CANDIDATE",
    )
    n_before = len(os.listdir(aroot + "/" + today + "/main"))
    res = wp.act(e7, True, False)
    check(
        "process cwd inside -> in-use, untouched, no archive (got %s)" % res,
        res == "in-use"
        and os.path.isdir(w7)
        and len(os.listdir(aroot + "/" + today + "/main")) == n_before,
    )
    repo.procs[-1].kill()

    # extra safety: changed during archive, nested repo, submodules
    w8 = mk("race")
    open(w8 + "/a.txt", "a").write("race\n")
    age(w8)
    e8 = get(w8)
    wp.analyze(e8, False)
    orig_build = wp.build_archive

    def racing_build(e, dest):
        info = orig_build(e, dest)
        open(e["wt"] + "/a.txt", "a").write("modified while archiving\n")
        return info

    wp.build_archive = racing_build
    n_before = len(os.listdir(aroot + "/" + today + "/main"))
    try:
        res = wp.act(e8, True, False)
    finally:
        wp.build_archive = orig_build
    check(
        "worktree changed while archiving -> kept, archive discarded (got %s)" % res,
        res in ("not-idle", "changed-during-archive")
        and os.path.isdir(w8)
        and len(os.listdir(aroot + "/" + today + "/main")) == n_before,
    )
    w9 = mk("nested")
    os.makedirs(w9 + "/vendor/inner")
    sh("git", "init", "-q", cwd=w9 + "/vendor/inner")
    open(w9 + "/vendor/inner/f", "w").write("x")
    sh("git", "add", "f", cwd=w9 + "/vendor/inner")
    sh("git", "commit", "-qm", "i", cwd=w9 + "/vendor/inner")
    age(w9)
    e9 = get(w9)
    wp.analyze(e9, False)
    res = wp.act(e9, True, False)
    check(
        "nested git repo inside -> kept (got %s)" % res,
        res == "nested-repo" and os.path.isdir(w9),
    )
    sub = tmp + "/subrepo"
    sh("git", "init", "-q", "-b", "main", sub)
    open(sub + "/s.txt", "w").write("s")
    sh("git", "add", ".", cwd=sub)
    sh("git", "commit", "-qm", "s", cwd=sub)
    w10 = mk("subm")
    sh(
        "git",
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        sub,
        "subdir",
        cwd=w10,
    )
    age(w10)
    e10 = get(w10)
    wp.analyze(e10, False)
    res = wp.act(e10, True, False)
    check(
        "initialized submodule present -> kept (got %s)" % res,
        res == "has-submodules" and os.path.isdir(w10),
    )
    w10b = mk(
        "subm-uninit"
    )  # tracked-style .gitmodules + gitlink but nothing cloned: the common real-world case
    open(w10b + "/.gitmodules", "w").write(
        '[submodule "subdir"]\n\tpath = subdir\n\turl = %s\n' % sub
    )
    sh(
        "git",
        "update-index",
        "--add",
        "--cacheinfo",
        "160000," + sh("git", "-C", sub, "rev-parse", "HEAD") + ",subdir",
        cwd=w10b,
    )
    age(w10b)
    e10b = get(w10b)
    wp.analyze(e10b, False)
    res = wp.act(e10b, True, False)
    check(
        "uninitialized gitlink + .gitmodules does NOT block archive (got %s, %s)"
        % (res, e10b.get("detail")),
        res == "ARCHIVED-REMOVED" and not os.path.exists(w10b),
    )

    # protect-ignored with the extended regenerable list
    with open(main + "/.git/info/exclude", "a") as fx:
        fx.write(".dev-serve/\n.mcp-venv/\n*.log\n")
    w11 = mk("regenonly")
    for p, c in (
        (".vitest/cache/x", "r"),
        ("site_pages/gen/p.html", "r"),
        ("public/b.js", "r"),
        (".husky/_/h", "r"),
        ("tmp/t", "r"),
        (".dev-serve/s", "r"),
        (".mcp-venv/v", "r"),
        ("data/logs/a.log", "r"),
        ("x.log", "r"),
        (".env.development", "NOT-READ-BY-TEST-OR-TOOL"),
    ):
        os.makedirs(os.path.dirname(os.path.join(w11, p)), exist_ok=True)
        open(os.path.join(w11, p), "w").write(c)
    age(w11)
    e11 = get(w11)
    wp.analyze(e11, False)
    res = wp.act(e11, True, True)
    check(
        "only-regenerable ignored files do not block plain removal under --protect-ignored (got %s, %s)"
        % (res, e11.get("ignored_nonregen")),
        res == "REMOVED" and not os.path.exists(w11),
    )
    d1, d2 = tmp + "/dtest1", tmp + "/dtest2"
    os.makedirs(d1 + "/data/logs")
    os.makedirs(d1 + "/data/sources")
    os.makedirs(d2 + "/data")
    open(d2 + "/data/app.db", "w").write("x")
    check(
        "data/ with only logs/sources subdirs is regenerable, data/ with a db file is not",
        wp.ignored_is_regen(d1, "data/")
        and not wp.ignored_is_regen(d2, "data/")
        and not wp.ignored_is_regen(d2, "data_backups/"),
    )
    w12 = mk("ignoredsmall")
    os.makedirs(w12 + "/data_backups")
    open(w12 + "/data_backups/b.sql", "w").write("select 2;\n")
    open(w12 + "/.env.development", "w").write("NOT-READ-BY-TEST-OR-TOOL")
    age(w12)
    e12 = get(w12)
    wp.analyze(e12, False)
    res = wp.act(e12, True, True)
    lst12 = (
        sh("tar", "-tzf", e12["archive_dir"] + "/untracked.tar.gz").split("\n")
        if e12.get("archive_dir")
        else []
    )
    check(
        "data_backups/ goes to the archive tier instead of blocking (got %s)" % res,
        res == "ARCHIVED-REMOVED"
        and e12["kind"] == "has-ignored-files"
        and "data_backups/b.sql" in lst12
        and ".env.development" not in lst12
        and not os.path.exists(w12),
    )

    # (vi) plain tier never uses --force
    w13 = mk("plain")
    age(w13)
    e13 = get(w13)
    wp.analyze(e13, False)
    seen = []
    orig_run = wp.run

    def spy(args, *a, **k):
        seen.append(list(args))
        return orig_run(args, *a, **k)

    wp.run = spy
    try:
        res = wp.act(e13, True, False)
    finally:
        wp.run = orig_run
    rm = [x for x in seen if "worktree" in x and "remove" in x]
    check(
        "plain tier removed via `worktree remove` without --force (got %s)" % res,
        res == "REMOVED"
        and len(rm) == 1
        and not any("--force" in x or "-f" in x for x in rm),
    )
    force = re.compile(
        r"""["']--force["']"""
    )  # either quote style, so a formatter cannot hide it
    src = open(wp.__file__).read()
    check(
        "'--force' appears exactly once in the module, inside archive_flow",
        len(force.findall(src)) == 1
        and force.search(inspect.getsource(wp.archive_flow))
        and not force.search(inspect.getsource(wp.final_check_and_remove))
        and not force.search(inspect.getsource(wp.act))
        and not force.search(inspect.getsource(wp.strip_flow)),
    )

    # (vii) archive dir is outside every repo and mode 0700 (files 0600)
    repos = (
        [main]
        + [x["wt"] for x in wp.list_worktrees(main, bm)]
        + [x["wt"] for x in (e, e2, e3)]
    )
    real_root = os.path.realpath(aroot)
    check(
        "archive root is outside every repo / worktree",
        wp.outside_every_repo(aroot)
        and not any(real_root.startswith(os.path.realpath(r_) + "/") for r_ in repos),
    )
    check(
        "default archive root is outside every repo and relative to the home dir",
        wp.outside_every_repo(wp.ARCHIVE_ROOT)
        and wp.ARCHIVE_ROOT.startswith(wp.H + "/"),
    )
    modes = {
        os.path.relpath(p, aroot): stat.S_IMODE(os.stat(p).st_mode)
        for p in [aroot, aroot + "/" + today, aroot + "/" + today + "/main", adir, a2]
    }
    check(
        "archive dirs are mode 0700 (%s)" % sorted(set(oct(m) for m in modes.values())),
        all(m == 0o700 for m in modes.values()),
    )
    fm = set(
        stat.S_IMODE(os.stat(os.path.join(adir, n)).st_mode) for n in os.listdir(adir)
    )
    check(
        "archive files are mode 0600 (%s)" % sorted(oct(m) for m in fm), fm == {0o600}
    )
    wp.ARCH["root"] = main + "/arch-inside"
    res = wp.archive_flow(get(mk("refuse")), True)
    wp.ARCH["root"] = aroot
    check(
        "archive root inside a repo is refused (got %s)" % res,
        res == "archive-root-inside-repo",
    )

    # retention: --prune-archives lists by default, deletes only with apply
    pr_root = tmp + "/prune-archives"
    for d in ("2020-01-01", "2020-02-02", today):
        os.makedirs(pr_root + "/" + d + "/r/x")
        open(pr_root + "/" + d + "/r/x/meta.json", "w").write("{}")
        open(pr_root + "/" + d + "/r/x/patch.diff", "w").write("x" * 100)
    wp.ARCH["root"] = pr_root
    lst_ = wp.prune_archives(False, 30)
    check(
        "retention listing shows only >30d dirs and deletes nothing",
        lst_["old_day_dirs"] == ["2020-01-01", "2020-02-02"]
        and os.path.isdir(pr_root + "/2020-01-01"),
    )
    wp.prune_archives(True, 30)
    check(
        "retention apply deletes old day dirs, keeps today",
        not os.path.exists(pr_root + "/2020-01-01")
        and os.path.isdir(pr_root + "/" + today),
    )
    wp.ARCH["root"] = aroot
    assert not fails, fails


# ======================================================================================
# archive tier: secret-named files, archive retention
# ======================================================================================
SECRET_NAMES = [
    ".env",
    ".env.local",
    ".env.production",
    "credentials.json",
    "credentials",
    "deploy.pem",
    "tls.key",
    "bundle.p12",
    "bundle.pfx",
    "my_secret_notes.txt",
    "CLIENT-SECRET.txt",
    ".netrc",
    "id_rsa",
    "id_rsa.bak",
    "id_ed25519",
    ".npmrc",
]
NOT_SECRET_NAMES = [
    ".env.example",
    ".env.sample",
    ".env.template",
    ".env.local.example",
    "credentials.json.sample",
    "secret.template",
    ".env.development",
    ".env.development.bak",
    ".env.development.local",
    "notes.md",
    "app.js",
    "monkey.txt",
]


@pytest.mark.parametrize("name", SECRET_NAMES)
def test_secret_names_are_recognised(name):
    assert wp.secret_name(name)


@pytest.mark.parametrize("name", NOT_SECRET_NAMES)
def test_example_and_ordinary_names_are_not_secret(name):
    assert not wp.secret_name(name)


def test_the_template_rule_is_the_repos_single_definition_and_fails_closed(monkeypatch):
    import importlib.util

    repo_root = Path(wp.__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "ct_for_test", repo_root / "lib" / "credential_templates.py"
    )
    shared = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shared)
    for suffix in (
        shared.PLACEHOLDER_TEMPLATE_SUFFIXES
    ):  # every suffix the repo defines, none copied here
        assert not wp.secret_name(".env" + suffix), suffix
        assert not wp.secret_name("credentials.json" + suffix.upper()), suffix
    # if the shared definition cannot be loaded, nothing counts as a template: more is kept, never less
    monkeypatch.setattr(
        importlib.util, "spec_from_file_location", lambda *a, **k: 1 / 0
    )
    broken = wp._load_template_rule()
    assert broken(".env.example") is False
    monkeypatch.setattr(wp, "is_placeholder_template", broken)
    assert wp.secret_name(".env.example")


@pytest.fixture
def archive_on(repo):
    aroot = repo.home + "/archive"
    dry = repo.home + "/dry"
    os.mkdir(dry)
    wp.ARCH.update(
        enabled=True, idle_hours=72, cap=25 * 1024 * 1024, root=aroot, dry_tmp=dry
    )
    return aroot


def _dirty_archive_candidate(repo, name):
    w = repo.add_wt(name, node_modules=False)
    open(w + "/a.txt", "a").write("edit\n")
    age(w)
    e = repo.entry(w)
    assert wp.analyze(e, False)["status"] == "ARCHIVE-CANDIDATE"
    return w, e


@pytest.mark.parametrize(
    "secret",
    [".env", ".env.local", "credentials.json", "deploy.pem", "my_secret_notes.txt"],
)
def test_a_worktree_with_an_untracked_secret_named_file_is_kept_whole(
    repo, archive_on, secret
):
    w, e = _dirty_archive_candidate(repo, "sec")
    open(w + "/" + secret, "w").write("NOT-READ-BY-TEST-OR-TOOL")
    age(w)
    for apply_ in (
        False,
        True,
    ):  # a dry run reports the same, and the real run changes nothing
        res = wp.archive_flow(e, apply_)
        assert res == "secrets-present", (res, e.get("detail"))
    assert e["secrets"] == [secret]
    assert (
        os.path.isdir(w) and open(w + "/" + secret).read() == "NOT-READ-BY-TEST-OR-TOOL"
    )
    assert open(w + "/a.txt").read().endswith("edit\n")
    assert not os.path.exists(archive_on)  # nothing archived anywhere
    assert os.listdir(repo.home + "/dry") == []


def test_an_ignored_secret_named_file_keeps_the_worktree_too(repo, archive_on):
    w, e = _dirty_archive_candidate(repo, "sec-ignored")
    os.makedirs(w + "/data_backups/inner")  # ignored by the repo's .gitignore
    open(w + "/data_backups/inner/id_ed25519", "w").write("NOT-READ-BY-TEST-OR-TOOL")
    age(w)
    res = wp.act(wp.analyze(repo.entry(w), False), True, False)
    assert res == "secrets-present"
    assert os.path.isfile(w + "/data_backups/inner/id_ed25519")
    assert not os.path.exists(archive_on)


def test_a_secret_found_through_protect_ignored_is_kept_not_archived(repo, archive_on):
    w = repo.add_wt(
        "sec-protect", node_modules=False
    )  # clean and pushed: plain-tier eligible
    os.makedirs(w + "/data_backups")
    open(w + "/data_backups/.env", "w").write("NOT-READ-BY-TEST-OR-TOOL")
    age(w)
    e = wp.analyze(repo.entry(w), False)
    assert e["status"] == "ELIGIBLE"
    res = wp.act(e, True, True)
    assert res == "secrets-present"
    assert os.path.isfile(w + "/data_backups/.env")
    assert not os.path.exists(archive_on)


def test_a_secret_appearing_while_archiving_discards_the_archive(
    repo, archive_on, monkeypatch
):
    w, e = _dirty_archive_candidate(repo, "sec-late")
    real_build = wp.build_archive

    def build_then_drop_a_secret(entry, dest):
        info = real_build(entry, dest)
        # ignored and old, so the unchanged-during-archive checks stay quiet: only the name check can catch it
        os.makedirs(w + "/data_backups", exist_ok=True)
        open(w + "/data_backups/.netrc", "w").write("NOT-READ-BY-TEST-OR-TOOL")
        age(w)
        return info

    monkeypatch.setattr(wp, "build_archive", build_then_drop_a_secret)
    res = wp.archive_flow(e, True)
    assert res == "secrets-present", (res, e.get("detail"))
    assert os.path.isdir(w) and os.path.isfile(w + "/data_backups/.netrc")
    leftovers = [
        p
        for p in Path(archive_on).rglob("*")
        if p.is_dir() and p.name.startswith("wt-sec-late")
    ]
    assert leftovers == []  # the finished archive is not left behind either


def test_example_names_and_the_dev_env_family_do_not_block_archiving(repo, archive_on):
    w, e = _dirty_archive_candidate(repo, "sec-ok")
    for n in (
        ".env.example",
        "credentials.json.sample",
        ".env.development",
        ".env.development.bak",
    ):
        open(w + "/" + n, "w").write("NOT-READ-BY-TEST-OR-TOOL")
    age(w)
    e = wp.analyze(repo.entry(w), False)
    res = wp.act(e, True, False)
    assert res == "ARCHIVED-REMOVED", (res, e.get("detail"))
    members = sh("tar", "-tzf", e["archive_dir"] + "/untracked.tar.gz").split("\n")
    assert ".env.example" in members and "credentials.json.sample" in members
    assert not any(m.startswith(".env.development") for m in members)  # as before
    assert not os.path.exists(w)


def test_a_tracked_secret_named_file_is_not_an_untracked_secret(repo, archive_on):
    w = repo.add_wt("sec-tracked", node_modules=False)
    open(w + "/credentials.txt", "w").write("committed on purpose")
    sh("git", "add", "credentials.txt", cwd=w)
    sh("git", "commit", "-qm", "tracked", cwd=w)
    age(w)
    e = wp.analyze(repo.entry(w), False)
    assert e["status"] == "ARCHIVE-CANDIDATE"  # unpushed commit
    assert wp.secret_files(w) == []
    assert wp.act(e, True, False) == "ARCHIVED-REMOVED"


def _write_archive(root, day, repo_name="r", name="x-1234abcd", members=("meta.json",)):
    d = os.path.join(root, day, repo_name, name)
    os.makedirs(d)
    for m in members:
        open(os.path.join(d, m), "w").write("{}" if m == "meta.json" else "data")
    return d


def test_prune_archives_apply_only_deletes_what_this_tool_wrote(repo):
    root = repo.home + "/archive-root"
    outside = repo.home + "/outside-target"
    os.makedirs(outside)
    open(outside + "/precious.txt", "w").write("keep")
    wp.ARCH["root"] = root
    real_old = _write_archive(
        root,
        "2020-01-01",
        members=("meta.json", "patch.diff", "untracked.tar.gz", "local-commits.bundle"),
    )
    foreign = {  # date-named directories holding something else
        "2020-02-01": lambda d: (
            os.makedirs(d + "/notes"),
            open(d + "/notes/todo.txt", "w").write("mine"),
        ),
        "2020-02-02": lambda d: open(d + "/stray.txt", "w").write(
            "a file at the top level"
        ),
        "2020-02-03": lambda d: _write_archive(
            root, "2020-02-03", members=("meta.json", "extra.sh")
        ),
        "2020-02-04": lambda d: _write_archive(
            root, "2020-02-04", members=("patch.diff",)
        ),
        "2020-02-05": lambda d: os.symlink(outside, d + "/linked-repo"),
        "2020-02-06": lambda d: _write_archive(
            root, "2020-02-06", name="x-1234abcd.partial"
        ),
        "2020-02-07": lambda d: None,  # empty
    }
    for day, make in foreign.items():
        os.makedirs(root + "/" + day, exist_ok=True)
        make(root + "/" + day)
    # a date-named symlink to somewhere else entirely, whose target looks exactly like a valid archive day
    elsewhere = repo.home + "/elsewhere-days/2020-02-08"
    _write_archive(repo.home + "/elsewhere-days", "2020-02-08")
    os.symlink(elsewhere, root + "/2020-02-08")
    assert wp.archive_day_dir_matches(
        elsewhere
    )  # the control: only the symlink makes it foreign
    listing = wp.prune_archives(False, 30)
    assert listing["old_day_dirs"] == ["2020-01-01"]
    assert listing["skipped_foreign_dirs"] == sorted(list(foreign) + ["2020-02-08"])
    result = wp.prune_archives(True, 30)
    assert result["deleted"] and result["old_day_dirs"] == ["2020-01-01"]
    assert not os.path.exists(real_old) and not os.path.exists(root + "/2020-01-01")
    for day in foreign:
        assert os.path.lexists(root + "/" + day), day  # foreign content left untouched
    assert open(root + "/2020-02-01/notes/todo.txt").read() == "mine"
    assert open(root + "/2020-02-02/stray.txt").read() == "a file at the top level"
    assert open(outside + "/precious.txt").read() == "keep"
    assert os.path.islink(root + "/2020-02-08")
    assert os.path.isfile(elsewhere + "/r/x-1234abcd/meta.json")


def test_an_archive_this_tool_really_wrote_is_removed_when_old(repo, archive_on):
    w, e = _dirty_archive_candidate(repo, "real-old")
    open(w + "/new-note.txt", "w").write("untracked")
    age(w)
    e = wp.analyze(repo.entry(w), False)
    assert wp.act(e, True, False) == "ARCHIVED-REMOVED"
    day_dir = os.path.dirname(os.path.dirname(e["archive_dir"]))
    assert wp.archive_day_dir_matches(day_dir)
    old_day = os.path.join(archive_on, "2020-01-01")
    os.rename(day_dir, old_day)
    assert wp.prune_archives(True, 30)["old_day_dirs"] == ["2020-01-01"]
    assert not os.path.exists(old_day)


def test_prune_archives_skips_are_logged_and_numeric_dates_are_not_dates(repo):
    root = repo.home + "/archive-log"
    wp.ARCH["root"] = root
    os.makedirs(root + "/2020-03-01/stray")  # empty repo dir: not an archive
    os.makedirs(
        root + "/20200301/r/x"
    )  # a compact date is not the name this tool writes
    open(root + "/20200301/r/x/meta.json", "w").write("{}")
    logfile = repo.home + "/prune.log"
    wp.LOGF = open(logfile, "w", buffering=1)
    try:
        res = wp.prune_archives(True, 30)
    finally:
        wp.LOGF.close()
    assert res["skipped_foreign_dirs"] == ["2020-03-01"]
    assert os.path.isdir(root + "/2020-03-01/stray") and os.path.isdir(
        root + "/20200301/r/x"
    )
    assert "SKIP prune-archives" in open(logfile).read()


def test_the_run_summary_counts_worktrees_kept_for_secrets(repo):
    w = repo.add_wt("sec-main", node_modules=False)
    open(w + "/a.txt", "a").write("edit\n")
    open(w + "/.env.local", "w").write("NOT-READ-BY-TEST-OR-TOOL")
    age(w)
    plain = repo.add_wt("sec-main-ok", node_modules=False)
    open(plain + "/a.txt", "a").write("edit\n")
    age(plain)
    code, summ = run_main(repo, "--archive-remove", "--apply")
    assert code == 0
    assert summ["secrets_present"] == 1 and summ["kept"]["secrets-present"] == 1
    assert summ["archived_removed"] == 1
    assert os.path.isfile(w + "/.env.local") and not os.path.exists(plain)


# ======================================================================================
# node_modules-only tier
# ======================================================================================
def test_strip_removes_only_node_modules_from_an_eligible_worktree(repo, monkeypatch):
    w = repo.add_wt("eligible")
    # nested node_modules at three depths, and look-alikes that must survive
    for rel in (
        "packages/a/node_modules/dep",
        "packages/b/sub/node_modules/dep",
        "node_modules/pkg/node_modules/inner",
    ):
        os.makedirs(os.path.join(w, rel))
        open(os.path.join(w, rel, "f.js"), "w").write("z" * 10)
    for rel in (
        "node_modules_backup",
        "my_node_modules",
        "packages/a/node_modules_old",
    ):
        os.makedirs(os.path.join(w, rel), exist_ok=True)
        open(os.path.join(w, rel, "keep.txt"), "w").write("keep")
    open(w + "/node_modules.md", "w").write("keep")
    sh(
        "git",
        "add",
        "-f",
        "node_modules.md",
        "node_modules_backup",
        "my_node_modules",
        "packages/a/node_modules_old",
        cwd=w,
    )
    sh("git", "commit", "-qm", "lookalikes", cwd=w)
    sh("git", "push", "-q", "origin", "b-eligible", cwd=w)
    sh("git", "fetch", "-q", "origin", cwd=repo.main)
    age(w)
    before = snapshot(w)
    expected = sum(
        os.path.getsize(os.path.join(dp, f))
        for nm in (
            "node_modules",
            "packages/a/node_modules",
            "packages/b/sub/node_modules",
        )
        for dp, _, fn in os.walk(os.path.join(w, nm))
        for f in fn
    )
    deleted = []
    real_rmtree = shutil.rmtree

    def audit_rmtree(path, *a, **k):
        deleted.append(os.path.relpath(path, w))
        assert os.path.basename(path) == "node_modules"
        assert os.path.realpath(path).startswith(os.path.realpath(w) + os.sep)
        return real_rmtree(path, *a, **k)

    monkeypatch.setattr(wp.shutil, "rmtree", audit_rmtree)
    e = repo.entry(w)
    res = wp.strip_flow(e, True)
    assert res == "STRIPPED", (res, e.get("detail"), e.get("strip_refused"))
    assert sorted(deleted) == [
        "node_modules",
        "packages/a/node_modules",
        "packages/b/sub/node_modules",
    ]
    assert not os.path.exists(w + "/node_modules")
    assert not os.path.exists(w + "/packages/a/node_modules")
    assert not os.path.exists(w + "/packages/b/sub/node_modules")
    assert e["strip_bytes"] == expected and expected > 0
    assert (
        snapshot(w) == before
    )  # every other file byte-identical, look-alikes included
    assert os.path.isdir(w) and os.path.isfile(w + "/.git")  # the worktree itself stays
    assert sh("git", "status", "--porcelain", cwd=w) == ""
    assert sh("git", "rev-parse", "--verify", "b-eligible", cwd=repo.main)
    # idempotent: a second pass finds nothing to do
    assert wp.strip_flow(repo.entry(w), True) == "no-node-modules"


def test_dry_run_frees_nothing_but_reports_the_bytes(repo):
    w = repo.add_wt("dry")
    e = repo.entry(w)
    res = wp.strip_flow(e, False)
    assert res == "WOULD-STRIP"
    assert e["strip_bytes"] == 150
    assert e["strip_dirs"] == ["node_modules"]
    assert os.path.isfile(w + "/node_modules/pkg/index.js")
    assert os.path.isfile(w + "/node_modules/pkg/lib/util.js")


def test_dirty_worktree_is_untouched(repo):
    w = repo.add_wt("dirty")
    open(w + "/a.txt", "a").write("change\n")
    age(w)
    assert wp.strip_flow(repo.entry(w), True) == "dirty"
    assert os.path.isfile(w + "/node_modules/pkg/index.js")
    w2 = repo.add_wt("untracked")
    open(w2 + "/scratch.txt", "w").write("s\n")
    age(w2)
    assert wp.strip_flow(repo.entry(w2), True) == "dirty"
    assert os.path.isfile(w2 + "/node_modules/pkg/index.js")


def test_unpushed_commit_is_untouched(repo):
    w = repo.add_wt("localonly")
    open(w + "/new.txt", "w").write("x\n")
    sh("git", "add", ".", cwd=w)
    sh("git", "commit", "-qm", "local only", cwd=w)
    age(w)
    assert wp.strip_flow(repo.entry(w), True) == "unpushed"
    assert os.path.isfile(w + "/node_modules/pkg/index.js")
    d = repo.add_wt("detached")
    sh("git", "checkout", "-q", "--detach", cwd=d)
    open(d + "/d.txt", "w").write("d\n")
    sh("git", "add", ".", cwd=d)
    sh("git", "commit", "-qm", "detached local", cwd=d)
    age(d)
    assert wp.strip_flow(repo.entry(d), True) == "detached-unreachable"
    assert os.path.isfile(d + "/node_modules/pkg/index.js")


def test_merged_pr_head_counts_as_pushed_like_the_plain_tier(repo, monkeypatch):
    w = repo.add_wt("merged")
    open(w + "/m.txt", "w").write("merged work\n")
    sh("git", "add", ".", cwd=w)
    sh("git", "commit", "-qm", "merged via PR", cwd=w)
    head = sh("git", "-C", w, "rev-parse", "HEAD")
    age(w)
    monkeypatch.setattr(wp, "merged_heads", lambda main, branch: {head})
    assert wp.strip_flow(repo.entry(w), False) == "WOULD-STRIP"
    # an ancestor of a merged PR head qualifies too (squash/rebase leaves the PR head further on)
    monkeypatch.setattr(wp, "merged_heads", lambda main, branch: {"0" * 40})
    assert wp.strip_flow(repo.entry(w), False) == "unpushed"
    monkeypatch.setattr(wp, "merged_heads", lambda main, branch: set())
    assert wp.strip_flow(repo.entry(w), False) == "unpushed"


def test_activity_inside_the_24_hour_window_is_untouched_just_past_it_is_eligible(repo):
    assert wp.STRIP["idle_hours"] == 24 and wp.DEFAULT_STRIP_IDLE_HOURS == 24
    w = repo.add_wt("window")
    os.utime(w + "/a.txt", (time.time() - 23 * 3600, time.time() - 23 * 3600))
    assert wp.strip_flow(repo.entry(w), True) == "recent"
    assert os.path.isfile(w + "/node_modules/pkg/index.js")
    os.utime(w + "/a.txt", (time.time() - 25 * 3600, time.time() - 25 * 3600))
    assert wp.strip_flow(repo.entry(w), True) == "STRIPPED"
    assert not os.path.exists(w + "/node_modules")
    # activity inside node_modules, .git, dist and build does not count as the worktree being active
    w2 = repo.add_wt("ignored-activity")
    os.makedirs(w2 + "/dist")
    open(w2 + "/dist/out.js", "w").write("fresh build output")
    open(w2 + "/node_modules/pkg/index.js", "w").write("fresh install")
    assert wp.strip_flow(repo.entry(w2), True) == "STRIPPED"


def test_a_live_process_inside_the_worktree_blocks_the_strip(repo):
    w = repo.add_wt("inuse")
    proc = repo.spawn_in(
        w + "/node_modules/pkg"
    )  # cwd deep inside, even inside node_modules itself
    e = repo.entry(w)
    assert wp.strip_flow(e, True) == "in-use"
    assert os.path.isfile(w + "/node_modules/pkg/index.js")
    # negative control: the same worktree is stripped once the process is gone (the check, not the setup, blocked it)
    proc.kill()
    proc.wait()
    time.sleep(0.3)
    assert wp.strip_flow(e, True) == "STRIPPED"
    assert not os.path.exists(w + "/node_modules")


def test_a_locked_worktree_is_untouched(repo):
    w = repo.add_wt("locked")
    sh("git", "worktree", "lock", w, cwd=repo.main)
    assert wp.strip_flow(repo.entry(w), True) == "locked"
    assert os.path.isfile(w + "/node_modules/pkg/index.js")
    # locked AFTER the worktree list was read: the re-check still catches it
    w2 = repo.add_wt("locked-late")
    stale = repo.entry(w2)
    sh("git", "worktree", "lock", w2, cwd=repo.main)
    assert "locked" not in stale["flags"]
    assert wp.strip_flow(stale, True) == "locked"
    assert os.path.isfile(w2 + "/node_modules/pkg/index.js")


def test_head_moving_after_the_list_was_read_blocks_the_strip(repo):
    w = repo.add_wt("moved")
    stale = repo.entry(w)
    open(w + "/new.txt", "w").write("x\n")
    sh("git", "add", ".", cwd=w)
    sh("git", "commit", "-qm", "moved on", cwd=w)
    age(w)
    assert wp.strip_flow(stale, True) == "head-moved"
    assert os.path.isfile(w + "/node_modules/pkg/index.js")


def test_never_touch_and_hold_leave_the_worktree_alone(repo):
    w = repo.add_wt("keepme")
    wp.NEVER.append(w)
    assert wp.strip_flow(repo.entry(w), True) == "never-touch"
    assert os.path.isfile(w + "/node_modules/pkg/index.js")
    wp.NEVER.remove(w)
    assert (
        wp.strip_flow(repo.entry(w), False) == "WOULD-STRIP"
    )  # the control: not held, so eligible

    future = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    past = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
    # a hold prefix that is still in force
    wp.configure(env={}, cli={"hold_prefix": ["wt-keep"], "hold_until": future})
    assert wp.strip_flow(repo.entry(w), True) == "never-touch"
    assert os.path.isfile(w + "/node_modules/pkg/index.js")
    # a prefix that does not match holds nothing
    wp.configure(env={}, cli={"hold_prefix": ["other-"], "hold_until": future})
    assert wp.strip_flow(repo.entry(w), False) == "WOULD-STRIP"
    # the hold lapses on its date
    wp.configure(env={}, cli={"hold_prefix": ["wt-keep"], "hold_until": past})
    assert wp.strip_flow(repo.entry(w), False) == "WOULD-STRIP"
    # no date, or an unreadable one, fails closed: held for good
    for bad in (None, "next-friday"):
        warnings = wp.configure(
            env={}, cli={"hold_prefix": ["wt-keep"], "hold_until": bad}
        )
        assert wp.strip_flow(repo.entry(w), True) == "never-touch"
        assert bool(warnings) == (bad is not None)
    assert os.path.isfile(w + "/node_modules/pkg/index.js")


def test_a_symlinked_node_modules_pointing_outside_is_refused_and_the_target_survives(
    repo,
):
    outside = os.path.join(repo.home, "outside-secrets")
    os.makedirs(outside + "/deep")
    open(outside + "/canary.txt", "w").write("do not delete")
    open(outside + "/deep/canary2.txt", "w").write("do not delete either")
    w = repo.add_wt("symlink", node_modules=False)
    os.symlink(outside, w + "/node_modules")  # top level
    os.makedirs(w + "/packages/b")
    os.symlink(outside, w + "/packages/b/node_modules")  # nested
    age(w)
    e = repo.entry(w)
    assert (
        sh("git", "status", "--porcelain", cwd=w) == ""
    )  # ignored, so the worktree reads as clean
    res = wp.strip_flow(e, True)
    assert res == "refused-node-modules", (res, e.get("detail"))
    assert sorted(e["strip_refused"]) == [
        "node_modules:symlink",
        "packages/b/node_modules:symlink",
    ]
    assert os.path.isfile(outside + "/canary.txt") and os.path.isfile(
        outside + "/deep/canary2.txt"
    )
    assert os.path.islink(w + "/node_modules") and os.path.islink(
        w + "/packages/b/node_modules"
    )
    # and the dry run says the same
    assert wp.strip_flow(repo.entry(w), False) == "refused-node-modules"


def test_a_symlink_swapped_in_after_planning_is_refused_at_delete_time(
    repo, monkeypatch
):
    outside = os.path.join(repo.home, "outside-race")
    os.makedirs(outside)
    open(outside + "/canary.txt", "w").write("do not delete")
    w = repo.add_wt("race")
    real_cwdset = wp.cwdset
    swapped = []

    def swap_then_scan():
        # runs inside the final gate, after node_modules was enumerated and before it is deleted
        if not swapped:
            shutil.rmtree(w + "/node_modules")
            os.symlink(outside, w + "/node_modules")
            swapped.append(True)
        return real_cwdset()

    monkeypatch.setattr(wp, "cwdset", swap_then_scan)
    e = repo.entry(w)
    res = wp.strip_flow(e, True)
    assert swapped
    assert res == "refused-node-modules", (res, e.get("detail"))
    assert e["strip_refused"] == ["node_modules:not-contained"]
    assert os.path.isfile(outside + "/canary.txt")
    assert os.path.islink(w + "/node_modules")


def test_symlinks_inside_node_modules_are_unlinked_never_followed(repo):
    outside = os.path.join(repo.home, "outside-linktarget")
    os.makedirs(outside)
    open(outside + "/canary.txt", "w").write("do not delete")
    open(repo.home + "/outside-file.txt", "w").write("do not delete")
    w = repo.add_wt("inner-links")
    os.symlink(outside, w + "/node_modules/pkg/dir-link")
    os.symlink(repo.home + "/outside-file.txt", w + "/node_modules/pkg/file-link")
    os.symlink(
        "../../..", w + "/node_modules/pkg/up-link"
    )  # points at the worktree's own parent chain
    age(w)
    assert wp.strip_flow(repo.entry(w), True) == "STRIPPED"
    assert not os.path.lexists(w + "/node_modules")
    assert os.path.isfile(outside + "/canary.txt")
    assert open(repo.home + "/outside-file.txt").read() == "do not delete"
    assert os.path.isfile(w + "/a.txt") and os.path.isfile(w + "/src/app.js")


def test_node_modules_containment_is_checked_against_the_real_path(repo):
    w = repo.add_wt("contain")
    outside = os.path.join(repo.home, "outside-contain")
    os.makedirs(outside + "/node_modules")
    assert wp.node_modules_contained(w, w + "/node_modules")
    assert not wp.node_modules_contained(w, w + "/src")  # not named node_modules
    assert not wp.node_modules_contained(
        w, w + "/node_modules/pkg"
    )  # inside, but not itself node_modules
    assert not wp.node_modules_contained(
        w, outside + "/node_modules"
    )  # a node_modules outside the worktree
    os.symlink(
        outside + "/node_modules", w + "/linked"
    )  # an ancestor that is a symlink out of the tree
    assert not wp.node_modules_contained(w, w + "/linked/../node_modules")
    os.makedirs(w + "/packages")
    os.symlink(outside, w + "/packages/out")
    assert not wp.node_modules_contained(w, w + "/packages/out/node_modules")
    assert not wp.node_modules_contained(w, w + "/missing/node_modules")


def test_tracked_node_modules_content_and_nested_repos_are_skipped(repo):
    w = repo.add_wt("tracked")
    os.makedirs(w + "/packages/c/node_modules")
    open(w + "/packages/c/node_modules/keep.js", "w").write("tracked on purpose")
    sh("git", "add", "-f", "packages/c/node_modules/keep.js", cwd=w)
    sh("git", "commit", "-qm", "tracked node_modules", cwd=w)
    sh("git", "push", "-q", "origin", "b-tracked", cwd=w)
    sh("git", "fetch", "-q", "origin", cwd=repo.main)
    with open(repo.main + "/.git/info/exclude", "a") as fx:
        fx.write("vendor/\n")  # a cloned dependency checkout the worktree ignores
    os.makedirs(
        w + "/vendor/inner/node_modules"
    )  # a nested repository's own dependency tree
    open(w + "/vendor/inner/node_modules/x.js", "w").write("x")
    sh("git", "init", "-q", cwd=w + "/vendor/inner")
    age(w)
    e = repo.entry(w)
    assert sh("git", "status", "--porcelain", cwd=w) == ""
    res = wp.strip_flow(e, True)
    assert res == "STRIPPED", (res, e.get("detail"), e.get("strip_refused"))
    assert e["strip_dirs"] == ["node_modules"]  # only the top-level one
    assert os.path.isfile(w + "/packages/c/node_modules/keep.js")
    assert os.path.isfile(w + "/vendor/inner/node_modules/x.js")
    assert "packages/c/node_modules:tracked-files" in e["strip_refused"]
    assert "vendor/inner:nested-repo" in e["strip_refused"]


def test_the_error_abort_ratio_stops_the_strip(repo):
    w = repo.add_wt("abort")
    for _ in range(wp.ERR_MIN_CALLS):
        wp.STATS.note(False)
    assert wp.STATS.aborted
    assert wp.strip_flow(repo.entry(w), True) == "aborted-error-ratio"
    assert os.path.isfile(w + "/node_modules/pkg/index.js")
    # and the ratio is not tripped by a few failures among many successes
    wp.STATS.calls = wp.STATS.errors = 0
    wp.STATS.aborted = False
    for i in range(100):
        wp.STATS.note(i % 10 != 0)
    assert not wp.STATS.aborted


def _fake_ismount(monkeypatch, mounted):
    """os.path.ismount that reports True for the paths in `mounted()` (a callable, so it can change)."""
    real = os.path.ismount
    monkeypatch.setattr(
        wp.os.path, "ismount", lambda p: os.path.abspath(p) in mounted() or real(p)
    )


def test_a_mount_point_at_node_modules_is_refused_at_plan_time(repo, monkeypatch):
    w = repo.add_wt("mounted")
    _fake_ismount(monkeypatch, lambda: {w + "/node_modules"})
    e = repo.entry(w)
    res = wp.strip_flow(e, True)
    assert res == "refused-node-modules", (res, e.get("detail"))
    assert e["strip_refused"] == ["node_modules:mount-point"]
    assert os.path.isfile(w + "/node_modules/pkg/index.js")
    assert os.path.isfile(w + "/node_modules/pkg/lib/util.js")
    # a dry run reports the same refusal instead of promising a strip
    assert wp.strip_flow(repo.entry(w), False) == "refused-node-modules"


def test_a_nested_directory_on_another_device_is_refused(repo, monkeypatch):
    w = repo.add_wt("otherdev")
    other = w + "/node_modules/pkg/lib"
    real_device = wp._device
    root = os.path.realpath(w)
    monkeypatch.setattr(
        wp,
        "_device",
        lambda p: real_device(p) + 1 if os.path.abspath(p) == other else real_device(p),
    )
    assert real_device(root) == real_device(
        w
    )  # the control: same device everywhere else
    e = repo.entry(w)
    res = wp.strip_flow(e, True)
    assert res == "refused-node-modules", (res, e.get("detail"))
    assert e["strip_refused"] == ["node_modules:mount-point"]
    assert os.path.isfile(other + "/util.js")
    # a mount point nested deeper is refused too
    monkeypatch.setattr(wp, "_device", real_device)
    _fake_ismount(monkeypatch, lambda: {other})
    assert wp.strip_flow(repo.entry(w), True) == "refused-node-modules"
    assert os.path.isfile(other + "/util.js")


def test_a_mount_appearing_after_planning_is_refused_at_delete_time(repo, monkeypatch):
    w = repo.add_wt("late-mount")
    state = {"mounted": False}
    _fake_ismount(
        monkeypatch, lambda: {w + "/node_modules"} if state["mounted"] else set()
    )
    real_cwdset = wp.cwdset

    def mount_then_scan():
        state["mounted"] = (
            True  # runs inside the final gate, after planning, before deleting
        )
        return real_cwdset()

    monkeypatch.setattr(wp, "cwdset", mount_then_scan)
    e = repo.entry(w)
    res = wp.strip_flow(e, True)
    assert state["mounted"]
    assert res == "refused-node-modules", (res, e.get("detail"))
    assert e["strip_refused"] == ["node_modules:mount-point"]
    assert os.path.isfile(w + "/node_modules/pkg/index.js")


def test_a_device_change_appearing_after_planning_is_refused_at_delete_time(
    repo, monkeypatch
):
    w = repo.add_wt("late-device")
    nested = w + "/node_modules/pkg"
    state = {"moved": False}
    real_device = wp._device
    monkeypatch.setattr(
        wp,
        "_device",
        lambda p: (
            real_device(p) + 1
            if state["moved"] and os.path.abspath(p) == nested
            else real_device(p)
        ),
    )
    real_cwdset = wp.cwdset

    def move_then_scan():
        state["moved"] = True
        return real_cwdset()

    monkeypatch.setattr(wp, "cwdset", move_then_scan)
    e = repo.entry(w)
    res = wp.strip_flow(e, True)
    assert res == "refused-node-modules", (res, e.get("detail"))
    assert os.path.isfile(nested + "/index.js")


def run_main(repo, *args):
    """Run the CLI end to end against the temp HOME; return (exit code, last runs.jsonl line)."""
    state = repo.home + "/state"
    code = wp.main(
        [
            "--state-dir",
            state,
            "--repos-dir",
            repo.home + "/repos",
            "--workers",
            "2",
            *args,
        ]
    )
    lines = [json.loads(x) for x in open(state + "/runs.jsonl").read().splitlines()]
    return code, lines[-1]


def test_main_dry_run_reports_would_strip_bytes_and_deletes_nothing(repo):
    ws = [repo.add_wt("m%d" % i) for i in range(3)]
    code, summ = run_main(repo, "--strip-only")
    assert code == 0 and summ["mode"] == "dry-run"
    assert summ["would_strip_worktrees"] == 3 and summ["stripped_worktrees"] == 0
    assert summ["would_strip_bytes"] == 450 and summ["strip_bytes_freed"] == 0
    assert sorted(d["bytes"] for d in summ["strip_detail"]) == [150, 150, 150]
    for w in ws:
        assert os.path.isfile(w + "/node_modules/pkg/index.js")


def test_main_apply_strips_and_reports_bytes_per_worktree_and_in_total(repo):
    ws = [repo.add_wt("a%d" % i) for i in range(2)]
    open(ws[1] + "/node_modules/pkg/big.bin", "wb").write(b"0" * 1000)
    age(ws[1])
    code, summ = run_main(repo, "--strip-only", "--apply")
    assert code == 0 and summ["mode"] == "apply"
    assert summ["stripped_worktrees"] == 2
    assert summ["strip_bytes_freed"] == 150 + 1150
    by_wt = {d["wt"]: d["bytes"] for d in summ["strip_detail"]}
    assert by_wt == {ws[0]: 150, ws[1]: 1150}
    for w in ws:
        assert not os.path.exists(w + "/node_modules") and os.path.isdir(w)
    # the runs log line is what the operator reads: it is on disk, one JSON object per run
    assert len(open(repo.home + "/state/runs.jsonl").read().splitlines()) == 1


def test_main_honours_the_per_run_cap(repo):
    ws = [repo.add_wt("c%d" % i) for i in range(3)]
    code, summ = run_main(repo, "--strip-only", "--apply", "--max", "2")
    assert code == 0
    assert summ["stripped_worktrees"] == 2
    assert summ["strip_kept"] == {"over-cap-deferred": 1}
    left = [w for w in ws if os.path.isdir(w + "/node_modules")]
    assert len(left) == 1
    # the next run picks up the deferred one
    code, summ = run_main(repo, "--strip-only", "--apply", "--max", "2")
    assert summ["stripped_worktrees"] == 1
    assert not any(os.path.isdir(w + "/node_modules") for w in ws)


def test_main_without_the_flag_does_not_strip(repo):
    w = repo.add_wt(
        "off", old=30
    )  # inside the 72h plain window, so nothing else takes it; the strip tier is off
    code, summ = run_main(repo, "--fresh-hours", "72", "--apply")
    assert code == 0
    assert summ["strip_tier"] is False and summ["stripped_worktrees"] == 0
    assert os.path.isfile(w + "/node_modules/pkg/index.js")


def test_main_strip_leaves_the_whole_worktree_to_the_plain_tier(repo):
    # idle 30h: inside a 72h plain window (kept whole), outside the 24h strip window (node_modules go)
    w = repo.add_wt("tiers", old=30)
    code, summ = run_main(
        repo, "--fresh-hours", "72", "--strip-node-modules", "--apply"
    )
    assert code == 0 and summ["removed"] == 0 and summ["stripped_worktrees"] == 1
    assert os.path.isdir(w) and not os.path.exists(w + "/node_modules")
    # a worktree the plain tier removes outright is not stripped first: it is simply removed
    w2 = repo.add_wt("whole", old=30)
    code, summ = run_main(
        repo, "--fresh-hours", "24", "--strip-node-modules", "--apply"
    )
    assert summ["removed"] >= 1 and summ["stripped_worktrees"] == 0
    assert not os.path.exists(w2)
    # --strip-only never removes a worktree even when the plain tier would
    w3 = repo.add_wt("only", old=30)
    code, summ = run_main(repo, "--strip-only", "--apply")
    assert summ["removed"] == 0 and summ["stripped_worktrees"] == 1
    assert os.path.isdir(w3) and not os.path.exists(w3 + "/node_modules")


def test_main_strip_skips_unsafe_worktrees_end_to_end(repo):
    clean = repo.add_wt("clean")
    dirty = repo.add_wt("dirty")
    open(dirty + "/a.txt", "a").write("change\n")
    age(dirty)
    local = repo.add_wt("local")
    open(local + "/n.txt", "w").write("n\n")
    sh("git", "add", ".", cwd=local)
    sh("git", "commit", "-qm", "local", cwd=local)
    age(local)
    locked = repo.add_wt("locked")
    sh("git", "worktree", "lock", locked, cwd=repo.main)
    inuse = repo.add_wt("inuse")
    repo.spawn_in(inuse)
    recent = repo.add_wt("recent", old=2)
    code, summ = run_main(repo, "--strip-only", "--apply")
    assert code == 0 and summ["stripped_worktrees"] == 1
    assert not os.path.exists(clean + "/node_modules")
    for w in (dirty, local, locked, inuse, recent):
        assert os.path.isfile(w + "/node_modules/pkg/index.js"), w
    assert summ["strip_kept"] == {"dirty": 1, "unpushed": 1, "in-use": 1, "recent": 1}


# ======================================================================================
# configuration and defaults
# ======================================================================================
def test_fresh_hours_defaults_to_24_everywhere(repo):
    assert wp.DEFAULT_FRESH_HOURS == 24
    assert wp.build_parser().parse_args([]).fresh_hours == 24
    assert (
        wp.build_parser().parse_args(["--fresh-hours", "6"]).fresh_hours == 6
    )  # the flag stays
    assert "last 24h" in wp.__doc__ and "48" not in wp.__doc__
    # behaviour, not just the constant: 23h old is protected as recent, 25h old is eligible
    young = repo.add_wt("young", old=23, node_modules=False)
    older = repo.add_wt("older", old=25, node_modules=False)
    assert wp.analyze(repo.entry(young), False)["status"] == "recent"
    assert wp.analyze(repo.entry(older), False)["status"] == "ELIGIBLE"


def test_idle_hours_default_is_24_for_node_modules_and_72_for_the_archive_tier(repo):
    run_main(repo, "--strip-only")
    assert wp.STRIP["idle_hours"] == 24
    run_main(repo, "--strip-node-modules", "--archive-remove")
    assert wp.STRIP["idle_hours"] == 24 and wp.ARCH["idle_hours"] == 72
    run_main(repo, "--strip-node-modules", "--archive-remove", "--idle-hours", "30")
    assert wp.STRIP["idle_hours"] == 30 and wp.ARCH["idle_hours"] == 30


def test_the_launchd_template_keeps_each_tier_at_its_own_idle_window():
    import plistlib

    template = Path(wp.__file__).with_name("com.ateles.worktree-prune.plist.template")
    text = template.read_text(encoding="utf-8")
    text = text.replace("{{HOME}}", "/h").replace("{{CHECKOUT}}", "/c")
    argv = plistlib.loads(text.encode("utf-8"))["ProgramArguments"]
    args = wp.build_parser().parse_args(
        argv[2:]
    )  # after the interpreter and the script
    assert (
        args.strip_node_modules and args.archive_remove
    )  # both tiers run from the template
    assert args.idle_hours is None, "the shared flag would also set the archive window"
    assert wp.idle_windows(args.idle_hours) == (24, 72)
    # the control that shows why: given explicitly the flag sets both windows
    assert wp.idle_windows(24) == (24, 24)


def test_inherited_git_context_does_not_reach_git_subprocesses(repo, tmp_path):
    parent = dict(
        PATH="/usr/bin",
        HOME="/h",
        **{name: "/somewhere/else" for name in wp.GIT_CONTEXT_VARS},
    )
    env = wp.build_git_env(parent)
    assert not [name for name in wp.GIT_CONTEXT_VARS if name in env]
    assert env["HOME"] == "/h" and env["GIT_OPTIONAL_LOCKS"] == "0"
    assert env["PATH"].endswith(":/usr/bin")
    for name in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES",
    ):
        assert name in wp.GIT_CONTEXT_VARS
    # effect: a GIT_DIR inherited from the parent must not redirect the pruner's git calls
    w = repo.add_wt("ctx", node_modules=False)
    other = tmp_path / "other"
    sh("git", "init", "-q", str(other))
    polluted = dict(os.environ, GIT_DIR=str(other / ".git"), GIT_WORK_TREE=str(other))
    monkey_env = wp.build_git_env(polluted)
    wp.GIT_ENV = dict(
        monkey_env, **GIT_IDENTITY
    )  # the fixture restores GIT_ENV afterwards
    rc, out, _ = wp.run(["git", "-C", w, "rev-parse", "--show-toplevel"])
    assert rc == 0 and os.path.realpath(out.strip()) == os.path.realpath(w)
    # and the module applies it at import time, to whatever environment it is started in
    probe = (
        "import sys; sys.path.insert(0, %r); import worktree_prune as w; "
        "print(sorted(k for k in w.GIT_ENV if k in w.GIT_CONTEXT_VARS))"
    ) % str(Path(wp.__file__).parent)
    child_env = dict(os.environ, GIT_DIR="/x", GIT_INDEX_FILE="/y", GIT_COMMON_DIR="/z")
    r = subprocess.run(
        [sys.executable, "-c", probe], env=child_env, capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "[]"


def test_defaults_are_relative_to_home_with_an_empty_hold(repo):
    warnings = wp.configure(env={}, cli={})
    assert warnings == []
    assert wp.H == repo.home
    assert wp.BASE == repo.home + "/.ateles-work/worktree-prune"
    assert wp.ARCHIVE_ROOT == repo.home + "/.ateles-work/worktree-archive"
    assert wp.REPOS_DIRS == [repo.home + "/repos"]
    assert wp.HOLD_PREFIXES == () and wp.HOLD_UNTIL is None
    assert wp.MANUAL_PROGRESS is None
    assert (
        repo.home + "/ateles-rc-src" in wp.NEVER
        and repo.home + "/neotoma-rc-src" in wp.NEVER
    )
    assert not wp.held(repo.home + "/anything-wt-x")  # nothing held by default


def test_environment_configures_and_flags_win(repo):
    other = repo.home + "/elsewhere"
    env = {
        "ATELES_WORKTREE_PRUNE_STATE_DIR": other + "/state",
        "ATELES_WORKTREE_PRUNE_ARCHIVE_ROOT": other + "/archive",
        "ATELES_WORKTREE_PRUNE_REPOS_DIRS": other + "/r1" + os.pathsep + other + "/r2",
        "ATELES_WORKTREE_PRUNE_NEVER_TOUCH": other + "/prod",
        "ATELES_WORKTREE_PRUNE_HOLD_PREFIXES": "alpha-, beta-",
        "ATELES_WORKTREE_PRUNE_HOLD_UNTIL": "2999-01-02",
        "ATELES_WORKTREE_PRUNE_YIELD_TO_FILE": other + "/progress.log",
    }
    wp.configure(env=env, cli={})
    assert (
        wp.BASE == other + "/state"
        and wp.RUNS == other + "/state/runs.jsonl"
        and wp.LOCK == other + "/state/lock"
    )
    assert (
        wp.ARCHIVE_ROOT == other + "/archive" and wp.ARCH["root"] == other + "/archive"
    )
    assert wp.REPOS_DIRS == [other + "/r1", other + "/r2"]
    assert (
        other + "/prod" in wp.NEVER and repo.home + "/ateles-rc-src" in wp.NEVER
    )  # extras add to the defaults
    assert wp.HOLD_PREFIXES == ("alpha-", "beta-") and wp.HOLD_UNTIL == (2999, 1, 2)
    assert wp.MANUAL_PROGRESS == other + "/progress.log"
    assert wp.held(repo.home + "/alpha-one") and not wp.held(repo.home + "/gamma-one")
    wp.configure(
        env=env,
        cli={
            "state_dir": repo.home + "/cli-state",
            "hold_until": "2000-01-01",
            "repos_dir": [repo.home + "/cli-repos"],
        },
    )
    assert wp.BASE == repo.home + "/cli-state" and wp.REPOS_DIRS == [
        repo.home + "/cli-repos"
    ]
    assert wp.HOLD_UNTIL == (2000, 1, 1) and not wp.held(
        repo.home + "/alpha-one"
    )  # flag date wins, hold lapsed


def test_nothing_outside_home_or_in_the_deployment_checkouts_is_ever_touched(
    repo, tmp_path
):
    outside = (
        tmp_path.resolve() / "elsewhere" / "wt"
    )  # a real directory, but not under HOME
    outside.mkdir(parents=True)
    assert wp.never_touch(str(outside))
    assert wp.never_touch(repo.home + "/ateles-rc-src") and wp.never_touch(
        repo.home + "/ateles-rc-src/sub"
    )
    assert wp.never_touch(repo.home + "/Library/anything")
    assert not wp.never_touch(repo.home + "/some-worktree")


def test_the_yield_file_only_applies_when_configured(repo):
    progress = repo.home + "/progress.log"
    open(progress, "w").write("manual cleanup running")
    w = repo.add_wt("yield")
    code, summ = run_main(repo, "--strip-only", "--apply", "--yield-to-file", progress)
    assert summ["status"] == "skipped-manual-cleanup-active"
    assert os.path.isfile(w + "/node_modules/pkg/index.js")
    code, summ = run_main(
        repo, "--strip-only", "--apply"
    )  # not configured: no yielding
    assert summ["status"] == "ok" and summ["stripped_worktrees"] == 1


def test_the_module_stays_free_of_operator_specifics():
    src = open(wp.__file__).read()
    home_dir = os.path.expanduser("~")
    assert home_dir not in src
    for needle in ("/Users/", "/home/", "(2026,", "PROTECT_UNTIL", "PROTECT_PREFIXES"):
        assert needle not in src, needle
    # the strip tier must reach deletion only through the guarded helper
    code = inspect.getsource(wp.strip_flow)
    assert code.count("shutil.rmtree") == 1 and "node_modules_contained" in code
