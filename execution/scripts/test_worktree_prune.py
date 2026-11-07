"""Tests for worktree_prune.py: throwaway repos and worktrees under a temp dir.

Every test builds a bare "origin", a clone of it (the main clone) and linked worktrees
inside a temporary HOME, so the pruner's refusal to touch anything outside $HOME is
exercised for real and nothing on the host is ever read or deleted.

Sections:
  plain tier       never removes dirty / local-only / recent / locked / in-use work
  protect-ignored  the plain-tier option that keeps worktrees holding non-regenerable ignored files
  node_modules     the node_modules-only tier, one test per safety
  configuration    env / CLI configuration, the hold, the 24 hour defaults, the removed options

Run: python3 -m pytest execution/scripts/test_worktree_prune.py -q
"""

from __future__ import annotations

import datetime
import inspect
import io
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
    saved_strip = dict(wp.STRIP)
    monkeypatch.setenv("HOME", str(home))
    wp.configure(env={}, cli={})
    wp.GIT_ENV = dict(wp.GIT_ENV, **GIT_IDENTITY)
    wp.STATS.calls = wp.STATS.errors = 0
    wp.STATS.aborted = False
    wp._merged.clear()
    wp._slug.clear()
    wp.FRESH_HOURS = wp.DEFAULT_FRESH_HOURS
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
    for n in ("dirty", "localonly", "detached", "untracked"):
        check(
            "analyze keeps %s with its own reason, nothing else is offered" % n,
            wp.analyze(ents[n], False)["status"]
            in ("dirty", "unpushed", "detached-unreachable"),
        )
    assert not fails, fails


# ======================================================================================
# protect-ignored (a plain-tier option: it only ever keeps more worktrees)
# ======================================================================================
def test_protect_ignored_keeps_non_regenerable_ignored_files_and_not_regenerable_ones(
    repo,
):
    main = repo.main
    with open(main + "/.git/info/exclude", "a") as fx:
        fx.write(".dev-serve/\n.mcp-venv/\n*.log\n")
    regen = repo.add_wt("regenonly", node_modules=False)
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
        os.makedirs(os.path.dirname(os.path.join(regen, p)), exist_ok=True)
        open(os.path.join(regen, p), "w").write(c)
    age(regen)
    e = wp.analyze(repo.entry(regen), False)
    res = wp.act(e, True, True)
    assert res == "REMOVED" and not os.path.exists(regen), (
        res,
        e.get("ignored_nonregen"),
    )
    # a non-regenerable ignored directory keeps the worktree under --protect-ignored, and only then
    kept = repo.add_wt("ignoredsmall", node_modules=False)
    os.makedirs(kept + "/data_backups")
    open(kept + "/data_backups/b.sql", "w").write("select 2;\n")
    age(kept)
    e2 = wp.analyze(repo.entry(kept), False)
    assert wp.act(e2, True, True) == "has-ignored-files"
    assert os.path.isfile(kept + "/data_backups/b.sql")
    e3 = wp.analyze(repo.entry(kept), False)
    assert (
        wp.act(e3, False, False) == "WOULD-REMOVE"
    )  # the control: the option is what keeps it
    assert e2["ignored_nonregen"] == ["data_backups/"]
    d1, d2 = repo.home + "/dtest1", repo.home + "/dtest2"
    os.makedirs(d1 + "/data/logs")
    os.makedirs(d1 + "/data/sources")
    os.makedirs(d2 + "/data")
    open(d2 + "/data/app.db", "w").write("x")
    assert wp.ignored_is_regen(d1, "data/")
    assert not wp.ignored_is_regen(d2, "data/")
    assert not wp.ignored_is_regen(d2, "data_backups/")
    assert wp.regen_path(".env.development.notloaded")


def test_the_plain_tier_never_forces_a_removal(repo):
    w = repo.add_wt("plain-noforce", node_modules=False)
    e = wp.analyze(repo.entry(w), False)
    seen = []
    real_run = wp.run

    def spy(args, *a, **k):
        seen.append(list(args))
        return real_run(args, *a, **k)

    wp.run = spy
    try:
        res = wp.act(e, True, False)
    finally:
        wp.run = real_run
    removals = [x for x in seen if "worktree" in x and "remove" in x]
    assert res == "REMOVED" and len(removals) == 1
    assert not any(a in ("--force", "-f") for a in removals[0])


def test_the_module_source_never_contains_a_forced_worktree_removal():
    src = Path(wp.__file__).read_text(encoding="utf-8")
    # the option as a string literal (either quote style), and the short form as an argument
    assert not re.search(r"""["']--force["']""", src)
    assert not re.search(r"""["']-f["']""", src)
    assert not re.search(r"""["']--force-""", src)
    assert "remove --force" not in src
    # exactly one place issues `worktree remove`, and a refusal by git keeps the worktree
    assert len(re.findall(r'"worktree",\s*"remove"', src)) == 1
    assert "git-refused" in src


# ======================================================================================
# git configuration that could hide changes
# ======================================================================================
def _hide_untracked(repo, how, tmp_path):
    """Make plain `git status` omit untracked files, through global-style or repository config."""
    if how == "local":
        sh("git", "config", "--local", "status.showUntrackedFiles", "no", cwd=repo.main)
    else:
        cfg = tmp_path / "hiding-gitconfig"
        cfg.write_text("[status]\n\tshowUntrackedFiles = no\n")
        wp.GIT_ENV = dict(wp.GIT_ENV, GIT_CONFIG_GLOBAL=str(cfg))


@pytest.mark.parametrize("how", ["local", "global"])
def test_config_hiding_untracked_files_does_not_make_a_worktree_look_clean(
    repo, tmp_path, how
):
    w = repo.add_wt("hidden", node_modules=True)
    open(w + "/precious-notes.txt", "w").write("only copy")
    age(w)
    _hide_untracked(repo, how, tmp_path)
    # the instrument: with this configuration and no override, git itself reports a clean tree
    rc, out, _ = wp.run(["git", "-C", w, "status", "--porcelain"])
    assert rc == 0 and out.strip() == "", "the hiding configuration did not take effect"
    e = repo.entry(w)
    assert wp.status_clean(w) is False
    assert wp.analyze(e, False)["status"] == "dirty"  # plain tier keeps it
    assert wp.strip_flow(repo.entry(w), True) == "dirty"  # and so does the strip gate
    assert wp.act(wp.analyze(e, False), True, False) == "dirty"
    assert os.path.isfile(w + "/precious-notes.txt")
    assert os.path.isfile(w + "/node_modules/pkg/index.js")
    # the removal itself: even if a cleanliness check were fooled, git's own re-check refuses
    wp.final_check_and_remove.__globals__["status_clean"], saved = (
        lambda wt: True,
        wp.status_clean,
    )
    try:
        e2 = repo.entry(w)
        res = wp.final_check_and_remove(e2, True, False)
    finally:
        wp.final_check_and_remove.__globals__["status_clean"] = saved
    assert res == "git-refused", (res, e2.get("detail"))
    assert os.path.isfile(w + "/precious-notes.txt")


def test_ignore_submodules_and_fsmonitor_settings_cannot_hide_a_change(repo, tmp_path):
    cfg = tmp_path / "other-gitconfig"
    cfg.write_text(
        "[status]\n\tignoreSubmodules = all\n\tshowUntrackedFiles = no\n"
        "[core]\n\tuntrackedCache = true\n"
    )
    wp.GIT_ENV = dict(wp.GIT_ENV, GIT_CONFIG_GLOBAL=str(cfg))
    w = repo.add_wt("cfg2", node_modules=False)
    open(w + "/new-file.txt", "w").write("x")
    age(w)
    assert wp.status_clean(w) is False
    cmd = wp.git_cmd(w, "status", "--porcelain")
    for setting in (
        "status.showUntrackedFiles=all",
        "status.ignoreSubmodules=none",
        "core.fsmonitor=false",
        "core.untrackedCache=false",
    ):
        assert ["-c", setting] == cmd[cmd.index(setting) - 1 : cmd.index(setting) + 1]


def test_every_git_call_is_built_by_the_one_helper():
    src = Path(wp.__file__).read_text(encoding="utf-8")
    # the only literal git command list in the module is the helper's own
    assert len(re.findall(r"""\[\s*["']git["']""", src)) == 1
    assert re.search(r"""\[\s*["']git["']""", inspect.getsource(wp.git_cmd))
    assert "git_cmd(" in src and src.count("git_cmd(") >= 12


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


@pytest.mark.parametrize("kind", ["repo-dir", "git-file", "deep-repo"])
def test_a_node_modules_that_contains_a_git_entry_is_refused(repo, kind):
    w = repo.add_wt("nm-git-" + kind)
    os.makedirs(
        w + "/packages/x/node_modules/clean"
    )  # a sibling without any .git: still stripped
    open(w + "/packages/x/node_modules/clean/i.js", "w").write("i")
    if kind == "repo-dir":
        inner = w + "/node_modules/pkg"
        sh("git", "init", "-q", inner)
        open(inner + "/work.txt", "w").write("local-only work")
        sh("git", "add", ".", cwd=inner)
        sh("git", "commit", "-qm", "only here", cwd=inner)
        marker = inner + "/work.txt"
    elif kind == "git-file":
        marker = w + "/node_modules/pkg/lib/.git"
        open(marker, "w").write("gitdir: /somewhere/else\n")
    else:
        deep = w + "/node_modules/a/b/c/d"
        os.makedirs(deep)
        sh("git", "init", "-q", deep)
        marker = deep + "/.git/HEAD"
    age(w)
    e = repo.entry(w)
    assert (
        wp.strip_flow(e, False) == "WOULD-STRIP"
    )  # the dry run plans only the clean sibling
    assert e["strip_dirs"] == ["packages/x/node_modules"]
    e = repo.entry(w)
    res = wp.strip_flow(e, True)
    assert res == "STRIPPED", (res, e.get("detail"))
    assert e["strip_dirs"] == ["packages/x/node_modules"]
    assert "node_modules:contains-git" in e["strip_refused"]
    assert os.path.exists(marker)
    assert os.path.isfile(w + "/node_modules/pkg/index.js")
    assert not os.path.exists(w + "/packages/x/node_modules")


def test_a_node_modules_with_only_a_git_entry_to_refuse_reports_it_alone(repo):
    w = repo.add_wt("nm-git-only", node_modules=False)
    os.makedirs(w + "/node_modules/pkg/.git")
    open(w + "/node_modules/pkg/.git/HEAD", "w").write("ref: refs/heads/main\n")
    age(w)
    e = repo.entry(w)
    assert wp.strip_flow(e, True) == "refused-node-modules"
    assert e["strip_refused"] == ["node_modules:contains-git"]
    assert os.path.isfile(w + "/node_modules/pkg/.git/HEAD")


def test_contains_git_fails_closed_when_the_tree_cannot_be_read(tmp_path, monkeypatch):
    d = tmp_path / "node_modules"
    (d / "p").mkdir(parents=True)
    assert wp.contains_git(str(d)) is False
    real_walk = os.walk

    def broken_walk(path, **kw):
        kw["onerror"](PermissionError("denied"))
        return real_walk(path, **kw)

    monkeypatch.setattr(wp.os, "walk", broken_walk)
    assert wp.contains_git(str(d)) is True


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


def test_idle_hours_is_the_node_modules_window_default_24(repo):
    run_main(repo, "--strip-only")
    assert wp.STRIP["idle_hours"] == 24
    run_main(repo, "--strip-node-modules", "--idle-hours", "30")
    assert wp.STRIP["idle_hours"] == 30
    assert (
        wp.idle_windows(None) == 24 and wp.idle_windows(0) == 1
    )  # never below an hour
    assert "archive" not in wp.build_parser().format_help().lower()


def test_the_launchd_template_runs_exactly_the_plain_and_strip_tiers():
    import plistlib

    template = Path(wp.__file__).with_name("com.ateles.worktree-prune.plist.template")
    text = template.read_text(encoding="utf-8")
    text = text.replace("{{HOME}}", "/h").replace("{{CHECKOUT}}", "/c")
    argv = plistlib.loads(text.encode("utf-8"))["ProgramArguments"]
    flags = argv[2:]  # after the interpreter and the script
    wp.reject_removed_options(wp.build_parser(), flags)  # no archive-tier option
    args = wp.build_parser().parse_args(flags)
    assert args.apply and args.protect_ignored and args.strip_node_modules
    assert args.max == 200 and not args.strip_only
    assert args.idle_hours is None and wp.idle_windows(args.idle_hours) == 24
    assert args.fresh_hours == 24
    assert sorted(f for f in flags if f.startswith("--")) == [
        "--apply",
        "--max",
        "--protect-ignored",
        "--strip-node-modules",
    ]


@pytest.mark.parametrize("option", list(wp.REMOVED_OPTIONS))
def test_removed_archive_options_are_refused_with_a_clear_message(option, capsys, repo):
    for argv in ([option], [option + "=3"], ["--apply", option, "5"]):
        with pytest.raises(SystemExit) as raised:
            wp.main(
                ["--state-dir", repo.home + "/state-removed", "--strip-only", *argv]
            )
        assert raised.value.code == 2
        err = capsys.readouterr().err
        assert option + " is no longer supported" in err
        assert "archive-then-remove tier is not part of this module" in err
    assert not os.path.exists(
        repo.home + "/state-removed"
    )  # refused before anything ran


def test_the_four_named_options_are_among_the_refused_ones():
    assert {
        "--archive-remove",
        "--archive-cap-mb",
        "--prune-archives",
        "--archive-retention-days",
    } <= set(wp.REMOVED_OPTIONS)


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
    assert not hasattr(wp, "ARCHIVE_ROOT") and not hasattr(wp, "ARCH")
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


# ======================================================================================
# relative path settings, log hygiene, private files
# ======================================================================================
@pytest.mark.parametrize(
    "flag,env_name",
    [("--never-touch", "NEVER_TOUCH"), ("--repos-dir", "REPOS_DIRS")],
)
@pytest.mark.parametrize("how", ["flag", "env"])
def test_a_relative_path_setting_is_refused_before_anything_runs(
    repo, monkeypatch, capsys, flag, env_name, how
):
    state = repo.home + "/state-relative"
    argv = ["--state-dir", state, "--strip-only"]
    if how == "flag":
        argv += [flag, "relative/dir"]
    else:
        monkeypatch.setenv(
            wp.ENV_PREFIX + env_name, repo.home + "/ok" + os.pathsep + "rel"
        )
    with pytest.raises(SystemExit) as raised:
        wp.main(argv)
    assert raised.value.code == 2
    err = capsys.readouterr().err
    assert "relative path" in err and "absolute" in err
    assert not os.path.exists(state)  # refused before the state directory was created


def test_relative_hold_prefixes_are_refused_but_names_and_tilde_paths_are_fine(repo):
    with pytest.raises(wp.ConfigError):
        wp.configure(env={}, cli={"hold_prefix": ["some/dir-"]})
    with pytest.raises(wp.ConfigError):
        wp.configure(env={wp.ENV_PREFIX + "HOLD_PREFIXES": "ok-,a/b"}, cli={})
    with pytest.raises(wp.ConfigError):
        wp.configure(env={}, cli={"never_touch": ["x" + os.pathsep + "/abs"]})
    wp.configure(
        env={}, cli={"hold_prefix": ["wt-"], "never_touch": ["~/kept", "/abs/kept"]}
    )
    assert wp.HOLD_PREFIXES == ("wt-",)
    assert repo.home + "/kept" in wp.NEVER and "/abs/kept" in wp.NEVER
    wp.configure(env={}, cli={"repos_dir": ["~/clones"]})
    assert wp.REPOS_DIRS == [repo.home + "/clones"]


def test_the_log_escapes_control_characters_in_one_place():
    out = io.StringIO()
    saved, wp.LOGF = wp.LOGF, out
    try:
        wp.log("KEEP", "/h/wt-evil\nFAKE 12:00:00 REMOVED /x\x1b[2J\r\x00", "ok")
    finally:
        wp.LOGF = saved
    text = out.getvalue()
    assert text.count("\n") == 1 and text.endswith("\n")
    assert "\\x0a" in text and "\\x1b" in text and "\\x0d" in text and "\\x00" in text
    assert "\x1b" not in text and "\r" not in text
    assert "\u2028" not in wp.escape_text("a\u2028b")


def test_a_worktree_name_with_control_characters_cannot_forge_a_log_line(
    repo, monkeypatch
):
    evil = repo.home + "/wt-evil\nFAKE 12:00:00 REMOVED " + repo.home + "/victim"
    entry = {
        "main": repo.main,
        "wt": evil,
        "flags": [],
        "branch": None,
        "head": "0" * 40,
    }
    monkeypatch.setattr(wp, "list_worktrees", lambda main, mainset: [dict(entry)])
    run_main(repo)  # a plain dry run logs the KEEP line for the missing path
    log_dir = os.path.join(repo.home, "state", "logs")
    text = "".join(open(os.path.join(log_dir, n)).read() for n in os.listdir(log_dir))
    lines = text.splitlines()
    assert any(
        "wt-evil\\x0aFAKE" in line for line in lines
    )  # the name is there, on one line
    assert all(re.match(r"^\d\d:\d\d:\d\d ", line) for line in lines)
    assert not any(line.startswith("FAKE") for line in lines)


def _mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


def test_state_and_log_files_are_private_whatever_the_umask(repo):
    old = os.umask(0o022)
    try:
        run_main(repo, "--strip-only")
        assert os.umask(0o022) == 0o022  # main restored the umask it found
    finally:
        os.umask(old)
    state = repo.home + "/state"
    assert _mode(state) == 0o700 and _mode(state + "/logs") == 0o700
    assert _mode(state + "/runs.jsonl") == 0o600 and _mode(state + "/lock") == 0o600
    day_logs = os.listdir(state + "/logs")
    assert day_logs and all(_mode(state + "/logs/" + n) == 0o600 for n in day_logs)


def test_existing_loose_state_is_tightened(repo):
    state = repo.home + "/state"
    os.makedirs(state + "/logs", mode=0o755)
    os.chmod(state, 0o755)
    os.chmod(state + "/logs", 0o755)
    for name in ("runs.jsonl", "lock"):
        open(state + "/" + name, "w").close()
        os.chmod(state + "/" + name, 0o644)
    run_main(repo, "--strip-only")
    assert _mode(state) == 0o700 and _mode(state + "/logs") == 0o700
    assert _mode(state + "/runs.jsonl") == 0o600 and _mode(state + "/lock") == 0o600


def test_rotate_logs_never_follows_a_symlink(repo):
    logs = repo.home + "/rotate-logs"
    outside = repo.home + "/outside-logs"
    os.makedirs(logs)
    os.makedirs(outside)
    big = b"x" * (1048576 + 10)
    old = time.time() - 40 * 86400
    for name in ("victim-big.bin", "victim-old.log"):
        open(outside + "/" + name, "wb").write(big)
    os.utime(outside + "/victim-old.log", (old, old))
    os.symlink(outside + "/victim-big.bin", logs + "/launchd.out.log")
    os.symlink(outside + "/victim-old.log", logs + "/2020-01-01.log")
    os.utime(logs + "/2020-01-01.log", (old, old), follow_symlinks=False)
    for name, size in (("launchd.err.log", len(big)), ("2020-01-02.log", 5)):
        open(logs + "/" + name, "wb").write(b"y" * size)  # the controls: real files
    os.utime(logs + "/2020-01-02.log", (old, old))
    wp.LOGDIR = logs
    wp.rotate_logs()
    assert os.path.getsize(outside + "/victim-big.bin") == len(
        big
    )  # not truncated via the link
    assert os.path.getsize(outside + "/victim-old.log") == len(big)
    assert os.path.islink(logs + "/launchd.out.log")
    assert os.path.islink(logs + "/2020-01-01.log")
    assert (
        os.path.getsize(logs + "/launchd.err.log") == 0
    )  # a real oversized log is emptied
    assert not os.path.exists(logs + "/2020-01-02.log")  # a real old day log is removed


def test_a_symlink_where_a_state_file_belongs_is_refused_not_followed(repo):
    outside = repo.home + "/outside-state-file"
    open(outside, "w").write("precious")
    state = repo.home + "/state"
    argv = ["--state-dir", state, "--strip-only", "--repos-dir", repo.home + "/repos"]
    os.makedirs(state + "/logs")
    os.symlink(outside, state + "/runs.jsonl")
    with pytest.raises(OSError):
        wp.main(argv)
    assert open(outside).read() == "precious"
    os.remove(state + "/runs.jsonl")
    day_log = state + "/logs/%s.log" % datetime.date.today().isoformat()
    if os.path.lexists(day_log):
        os.remove(day_log)  # the first run created it; plant the link in its place
    os.symlink(outside, day_log)
    with pytest.raises(OSError):
        wp.main(argv)
    assert open(outside).read() == "precious"
    with pytest.raises(OSError):
        wp.open_private(day_log)
