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
    "ALERT_GIB",
    "ALERT_REARM_GIB",
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
    saved_daily = dict(wp.DAILY)
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
    wp.DAILY.clear()
    wp.DAILY.update(saved_daily)
    wp.APPROVAL.clear()


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


def test_the_launchd_template_runs_exactly_the_one_daily_invocation():
    import plistlib

    template = Path(wp.__file__).with_name("com.ateles.worktree-prune.plist.template")
    text = template.read_text(encoding="utf-8")
    text = text.replace("{{HOME}}", "/h").replace("{{CHECKOUT}}", "/c")
    argv = plistlib.loads(text.encode("utf-8"))["ProgramArguments"]
    flags = argv[2:]  # after the interpreter and the script
    wp.reject_removed_options(wp.build_parser(), flags)  # no archive-tier option
    args = wp.build_parser().parse_args(flags)
    assert args.daily and args.apply and not args.dry_run and not args.strip_only
    assert (
        argv[0] == "/c/.venv/bin/python3"
    )  # the daemon venv convention, not /usr/bin/python3
    start = plistlib.loads(text.encode("utf-8"))["StartCalendarInterval"]
    assert (start["Hour"], start["Minute"]) == (
        8,
        30,
    )  # outside the notifier's 22:00-08:00 silence window
    assert args.max == 200
    assert args.idle_hours is None and wp.idle_windows(args.idle_hours) == 24
    assert args.fresh_hours == 24
    assert sorted(f for f in flags if f.startswith("--")) == [
        "--apply",
        "--daily",
        "--max",
    ]
    # the unreviewed off-repo cleaner must never be referenced, and no archive tier is enabled
    assert "prune.py" not in text.replace("worktree_prune.py", "")
    assert ".ateles-work/worktree-prune/prune" not in text


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


def test_every_removed_archive_option_is_pinned_in_the_refused_set():
    assert {
        "--archive-remove",
        "--archive-cap-mb",
        "--prune-archives",
        "--archive-retention-days",
        "--archive-root",
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


# ======================================================================================
# daily mode: the one safe daily check (keep marks, merged-only removal, caches, approval list, alert)
# ======================================================================================
def state_json(repo, name):
    return json.loads(open(repo.home + "/state/" + name).read())


def approval_by_path(repo):
    doc = state_json(repo, "approval_list.json")
    return {i["path"]: i for i in doc["items"]}


def push_branch(wt, name):
    """Commit one file in `wt` and push it to a non-default origin branch (pushed, not merged)."""
    open(wt + "/feature.txt", "w").write("feature\n")
    sh("git", "add", ".", cwd=wt)
    sh("git", "commit", "-qm", "feature", cwd=wt)
    sh("git", "push", "-q", "origin", "HEAD:refs/heads/" + name, cwd=wt)
    sh("git", "fetch", "-q", "origin", cwd=wt)
    age(wt)


def test_daily_removes_a_merged_clean_worktree_and_nothing_else(repo):
    w = repo.add_wt("merged")
    code, summ = run_main(repo, "--daily", "--apply")
    assert code == 0 and summ["daily"] is True and summ["mode"] == "apply"
    assert summ["removed"] == 1 and not os.path.exists(w)
    # the branch survives the worktree
    assert "b-merged" in sh("git", "branch", "--list", "b-merged", cwd=repo.main)


def test_daily_dry_run_flag_deletes_nothing(repo):
    w = repo.add_wt("dry")
    code, summ = run_main(repo, "--daily", "--dry-run")
    assert code == 0 and summ["mode"] == "dry-run" and summ["would_remove"] == 1
    assert os.path.isdir(w + "/node_modules")
    # the plain default is still a dry run
    code, summ = run_main(repo)
    assert summ["mode"] == "dry-run" and os.path.isdir(w)


def test_daily_keeps_and_lists_every_kind_of_unique_or_wanted_work(repo):
    m = repo.main
    keep = repo.add_wt("keepmark", node_modules=False)
    open(keep + "/.ateles-keep", "w").write("")
    age(keep)
    dirty = repo.add_wt("dirty", node_modules=False)
    open(dirty + "/a.txt", "a").write("change\n")
    age(dirty)
    untracked = repo.add_wt("untracked", node_modules=False)
    open(untracked + "/new.txt", "w").write("n\n")
    age(untracked)
    local = repo.add_wt("local", node_modules=False)
    open(local + "/n.txt", "w").write("n\n")
    sh("git", "add", ".", cwd=local)
    sh("git", "commit", "-qm", "local", cwd=local)
    age(local)
    detached = repo.add_wt("detached", node_modules=False)
    sh("git", "checkout", "-q", "--detach", cwd=detached)
    open(detached + "/d.txt", "w").write("d\n")
    sh("git", "add", ".", cwd=detached)
    sh("git", "commit", "-qm", "detached", cwd=detached)
    age(detached)
    ignored = repo.add_wt("ignored", node_modules=False)
    os.makedirs(ignored + "/data_backups")
    open(ignored + "/data_backups/db.bak", "w").write("precious")
    age(ignored)
    recent = repo.add_wt("recent", old=2, node_modules=False)
    locked = repo.add_wt("locked", node_modules=False)
    sh("git", "worktree", "lock", locked, cwd=m)
    inuse = repo.add_wt("inuse", node_modules=False)
    repo.spawn_in(inuse)
    pushed = repo.add_wt("pushed", node_modules=False)
    push_branch(pushed, "feat-pushed")
    code, summ = run_main(repo, "--daily", "--apply")
    assert code == 0 and summ["removed"] == 0
    wanted = {
        keep: "keep-marked",
        dirty: "dirty",
        untracked: "dirty",
        local: "unpushed",
        detached: "unpushed" if False else "detached-unreachable",
        ignored: "has-ignored-files",
        recent: "recent",
        locked: "locked",
        inuse: "in-use",
        pushed: "pushed-not-merged",
    }
    items = approval_by_path(repo)
    for path, reason in wanted.items():
        assert os.path.isdir(path), path  # nothing was removed
        assert items[path]["reason"] == reason, (path, items.get(path))
        assert items[path]["why"] and items[path]["size_kb"] >= 0
    assert os.path.isfile(ignored + "/data_backups/db.bak")
    assert os.path.isfile(local + "/n.txt") and os.path.isfile(untracked + "/new.txt")


def test_daily_keep_marker_protects_a_worktree_that_is_otherwise_removable(repo):
    # the marker is committed, so the tree is clean and merged: only the marker keeps it
    open(repo.main + "/.ateles-keep", "w").write("")
    sh("git", "add", ".", cwd=repo.main)
    sh("git", "commit", "-qm", "keep", cwd=repo.main)
    sh("git", "push", "-q", "origin", "main", cwd=repo.main)
    sh("git", "fetch", "-q", "origin", cwd=repo.main)
    w = repo.add_wt("marked")
    e = wp.analyze(repo.entry(w), False)
    assert e["status"] == "keep-marked"
    code, summ = run_main(repo, "--daily", "--apply")
    assert summ["removed"] == 0 and os.path.isdir(w)
    assert os.path.isdir(
        w + "/node_modules/pkg"
    )  # no tier, caches included, touched it
    assert approval_by_path(repo)[w]["reason"] == "keep-marked"
    # a marker added after the list was read is caught by the final re-check
    os.remove(repo.main + "/.ateles-keep")
    sh("git", "commit", "-qam", "unkeep", cwd=repo.main)
    sh("git", "push", "-q", "origin", "main", cwd=repo.main)
    sh("git", "fetch", "-q", "origin", cwd=repo.main)
    w2 = repo.add_wt("late", node_modules=False)
    e2 = wp.analyze(repo.entry(w2), False)
    assert e2["status"] == "ELIGIBLE"
    open(w2 + "/.ateles-keep", "w").write("")
    assert wp.final_check_and_remove(e2, True, True) == "keep-marked"
    assert os.path.isdir(w2)


def test_daily_pushed_but_unmerged_commits_are_kept_not_removed(repo):
    w = repo.add_wt("unmerged", node_modules=False)
    push_branch(w, "feat-unmerged")
    wp.DAILY["merged_only"] = True
    e = wp.analyze(repo.entry(w), False)
    assert e["status"] == "pushed-not-merged"
    wp.DAILY["merged_only"] = (
        False  # the older plain tier still treats a pushed commit as safe
    )
    assert wp.analyze(repo.entry(w), False)["status"] == "ELIGIBLE"


def test_daily_a_worktree_with_initialized_submodules_goes_to_the_approval_list(repo):
    sub = repo.home + "/subsrc"
    os.makedirs(sub)
    sh("git", "init", "-q", "-b", "main", sub)
    open(sub + "/s.txt", "w").write("s\n")
    sh("git", "add", ".", cwd=sub)
    sh("git", "commit", "-qm", "sub", cwd=sub)
    sh(
        "git",
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "add",
        "-q",
        sub,
        "libsub",
        cwd=repo.main,
    )
    sh("git", "commit", "-qm", "add submodule", cwd=repo.main)
    sh("git", "push", "-q", "origin", "main", cwd=repo.main)
    sh("git", "fetch", "-q", "origin", cwd=repo.main)
    w = repo.add_wt("withsub", node_modules=False)
    sh(
        "git",
        "-c",
        "protocol.file.allow=always",
        "submodule",
        "update",
        "--init",
        cwd=w,
    )
    age(w)
    code, summ = run_main(repo, "--daily", "--apply")
    assert code == 0 and summ["removed"] == 0 and os.path.isdir(w + "/libsub")
    assert approval_by_path(repo)[w]["reason"] == "has-submodules"


def test_daily_removes_regenerable_caches_but_keeps_the_worktree_and_non_regenerable_files(
    repo,
):
    with open(repo.main + "/.git/info/exclude", "a") as f:
        f.write(".venv/\n__pycache__/\n")
    w = repo.add_wt("caches")
    for d in (".venv/lib", "dist/js", "build", "src/__pycache__"):
        os.makedirs(w + "/" + d, exist_ok=True)
        open(w + "/" + d + "/x.bin", "w").write("z" * 40)
    os.makedirs(w + "/data_backups")
    open(w + "/data_backups/db.bak", "w").write("precious")
    age(w)
    code, summ = run_main(repo, "--daily", "--apply")
    assert code == 0 and summ["removed"] == 0 and summ["stripped_worktrees"] == 1
    for d in ("node_modules", ".venv", "dist", "build", "src/__pycache__"):
        assert not os.path.exists(w + "/" + d), d
    assert os.path.isfile(
        w + "/data_backups/db.bak"
    )  # non-regenerable ignored file survives
    assert os.path.isfile(w + "/src/app.js") and os.path.isfile(w + "/a.txt")
    assert approval_by_path(repo)[w]["reason"] == "has-ignored-files"


def test_daily_a_cache_named_directory_that_git_does_not_ignore_is_refused_and_listed(
    repo,
):
    w = repo.add_wt("notignored", node_modules=False)
    os.makedirs(w + "/out/.cache/emptychild")  # git sees no file, so the tree is clean
    age(w)
    wp.STRIP["caches"] = True
    e = repo.entry(w)
    res = wp.strip_flow(e, True)
    assert res == "refused-node-modules"
    assert "out/.cache:not-git-ignored" in e["strip_refused"]
    assert os.path.isdir(w + "/out/.cache")
    wp.note_cache_refusals(e)
    assert (
        wp.APPROVAL[0]["kind"] == "cache"
        and wp.APPROVAL[0]["reason"] == "not-git-ignored"
    )


def test_daily_never_passes_force_to_any_command_and_removes_with_one_plain_call(
    repo, monkeypatch
):
    for n in ("one", "two"):
        repo.add_wt(n)
    dirty = repo.add_wt("dirtyone")
    open(dirty + "/a.txt", "a").write("x\n")
    age(dirty)
    seen = []
    real_run = wp.run

    def spy(args, *a, **k):
        seen.append(list(args))
        return real_run(args, *a, **k)

    monkeypatch.setattr(wp, "run", spy)
    code, summ = run_main(repo, "--daily", "--apply")
    assert summ["removed"] == 2
    assert not [c for c in seen if any(x in ("--force", "-f", "-ff") for x in c)]
    removals = [c for c in seen if "worktree" in c and "remove" in c]
    assert len(removals) == 2 and os.path.isdir(dirty)


def test_daily_never_touches_deployment_checkouts_or_the_in_use_cwd_after_listing(repo):
    w = repo.add_wt("late-use")
    e = wp.analyze(repo.entry(w), False)
    assert e["status"] == "ELIGIBLE"
    repo.spawn_in(
        w
    )  # a process arrives after the analysis: the final re-check must see it
    wp.DAILY["merged_only"] = True
    assert wp.final_check_and_remove(e, True, True) == "in-use"
    assert os.path.isdir(w)
    assert (
        "~/ateles-rc-src" in wp.DEFAULT_NEVER_TOUCH
        and "~/neotoma-rc-src" in wp.DEFAULT_NEVER_TOUCH
    )


def test_daily_writes_an_approval_list_json_and_markdown_with_reasons(repo):
    dirty = repo.add_wt("lister", node_modules=False)
    open(dirty + "/a.txt", "a").write("x\n")
    age(dirty)
    code, summ = run_main(repo, "--daily", "--apply")
    assert summ["approval_items"] == 1 and summ["approval_list"].endswith(
        "approval_list.json"
    )
    doc = state_json(repo, "approval_list.json")
    assert doc["count"] == 1 and doc["items"][0]["reason"] == "dirty"
    assert doc["items"][0]["path"] == dirty
    md = open(repo.home + "/state/approval_list.md").read()
    assert (
        dirty in md
        and "not saved to git yet" in md
        and "Nothing below was changed" in md
    )
    assert _mode(repo.home + "/state/approval_list.json") == 0o600
    # a later run replaces the list rather than appending to it
    open(dirty + "/a.txt", "w").write("a\n")
    sh("git", "checkout", "--", "a.txt", cwd=dirty)
    age(dirty)
    code, summ = run_main(repo, "--daily", "--apply")
    assert state_json(repo, "approval_list.json")["count"] == 0


def _alert_run(repo, monkeypatch, free, *args):
    sent = []
    monkeypatch.setattr(wp, "dfree_gb", lambda: free)
    monkeypatch.setattr(
        wp,
        "send_notification",
        lambda msg, sev: (
            sent.append((msg, sev))
            or {"available": True, "returned": True, "error": ""}
        ),
    )
    code, summ = run_main(repo, "--daily", "--apply", *args)
    return summ, sent


def test_free_space_alert_fires_below_threshold_dedupes_and_rearms(repo, monkeypatch):
    summ, sent = _alert_run(repo, monkeypatch, 40.0)
    assert summ["alert_severity"] == 0 and sent == []
    summ, sent = _alert_run(repo, monkeypatch, 20.0)
    assert (
        summ["alert"]["state"] == "fired"
        and summ["alert_severity"] == 1
        and len(sent) == 1
    )
    rec = [json.loads(x) for x in open(repo.home + "/state/alerts.jsonl")]
    assert (
        len(rec) == 1
        and rec[0]["free_gib"] == 20.0
        and rec[0]["delivery"]["returned"] is True
    )
    # still low the next day: no second alert, but the summary still carries the severity
    summ, sent = _alert_run(repo, monkeypatch, 19.0)
    assert (
        summ["alert"]["state"] == "suppressed"
        and summ["alert_severity"] == 1
        and sent == []
    )
    assert len(open(repo.home + "/state/alerts.jsonl").read().splitlines()) == 1
    # recovering to just above the threshold is inside the hysteresis band: still not re-armed
    summ, sent = _alert_run(repo, monkeypatch, 30.0)
    assert (
        summ["alert_severity"] == 0
        and state_json(repo, "alert_state.json")["armed"] is False
    )
    summ, sent = _alert_run(repo, monkeypatch, 18.0)
    assert summ["alert"]["state"] == "suppressed" and sent == []
    # above threshold + 10 re-arms; the next drop alerts again
    summ, sent = _alert_run(repo, monkeypatch, 36.0)
    assert (
        summ["alert"]["state"] == "rearmed"
        and state_json(repo, "alert_state.json")["armed"] is True
    )
    summ, sent = _alert_run(repo, monkeypatch, 12.0)
    assert summ["alert"]["state"] == "fired" and len(sent) == 1


def test_free_space_alert_threshold_is_configurable_and_critical_when_very_low(
    repo, monkeypatch
):
    summ, sent = _alert_run(repo, monkeypatch, 40.0, "--alert-gib", "50")
    assert summ["alert"]["threshold_gib"] == 50.0 and summ["alert"]["state"] == "fired"
    monkeypatch.setenv("ATELES_WORKTREE_PRUNE_ALERT_GIB", "3")
    summ, sent = _alert_run(repo, monkeypatch, 4.0)
    assert summ["alert"]["threshold_gib"] == 3.0
    assert wp.DEFAULT_ALERT_GIB == 25.0 and wp.DEFAULT_ALERT_REARM_GIB == 10.0
    wp.configure(env={}, cli={})
    assert wp.ALERT_GIB == 25.0
    wp.configure(env={"ATELES_WORKTREE_PRUNE_ALERT_GIB": "junk"}, cli={})
    assert wp.ALERT_GIB == 25.0  # a bad value falls back to the default, with a warning
    os.remove(repo.home + "/state/alert_state.json")  # start armed again
    summ, sent = _alert_run(repo, monkeypatch, 2.0, "--alert-gib", "25")
    assert summ["alert_severity"] == 2 and sent[0][1] == 2


def test_free_space_alert_dry_run_sends_and_writes_nothing(repo, monkeypatch):
    summ, sent = _alert_run(repo, monkeypatch, 5.0, "--dry-run")
    assert (
        summ["alert"]["state"] == "would-fire"
        and summ["alert_severity"] == 1
        and sent == []
    )
    assert not os.path.exists(repo.home + "/state/alerts.jsonl")
    assert not os.path.exists(repo.home + "/state/alert_state.json")


def test_alert_is_recorded_durably_even_when_no_notifier_transport_exists(
    repo, monkeypatch
):
    monkeypatch.setattr(wp, "dfree_gb", lambda: 10.0)
    monkeypatch.setattr(
        wp,
        "send_notification",
        lambda msg, sev: {
            "available": False,
            "returned": None,
            "error": "ImportError: no httpx",
        },
    )
    code, summ = run_main(repo, "--daily", "--apply")
    assert code == 0 and summ["alert_severity"] == 1
    rec = json.loads(open(repo.home + "/state/alerts.jsonl").read().splitlines()[0])
    assert (
        rec["delivery"]["available"] is False and "no httpx" in rec["delivery"]["error"]
    )


def test_send_notification_never_raises_without_the_notifier_dependencies(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def deny(name, *a, **k):
        if name.startswith("lib.notify"):
            raise ImportError("no httpx")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", deny)
    out = wp.send_notification("m", 1)
    assert out["available"] is False and "ImportError" in out["error"]


def test_the_daily_flag_cannot_be_combined_with_strip_only(repo, capsys):
    with pytest.raises(SystemExit) as ex:
        wp.main(["--state-dir", repo.home + "/state", "--daily", "--strip-only"])
    assert ex.value.code == 2


# ======================================================================================
# round 1 fixes: strict ignored files, re-check pins, alert text, list text, delivery honesty
# ======================================================================================
def test_daily_alone_is_a_dry_run_and_deletes_nothing(repo):
    w = repo.add_wt("alone")
    code, summ = run_main(repo, "--daily")
    assert code == 0 and summ["mode"] == "dry-run" and summ["daily"] is True
    assert summ["removed"] == 0 and summ["stripped_worktrees"] == 0
    assert os.path.isdir(w + "/node_modules/pkg")
    assert os.path.isfile(
        repo.home + "/state/approval_list.md"
    )  # the report is still written


def test_daily_ignored_notes_settings_and_nested_repos_keep_the_worktree(repo):
    notes = repo.add_wt("notes", node_modules=False)
    os.makedirs(notes + "/tmp")
    open(notes + "/tmp/notes.txt", "w").write("my notes")
    age(notes)
    public = repo.add_wt("public", node_modules=False)
    os.makedirs(public + "/public")
    open(public + "/public/page.html", "w").write("<p>x</p>")
    age(public)
    envw = repo.add_wt("envfile", node_modules=False)
    open(envw + "/.env.development", "w").write("LOCAL=1")
    age(envw)
    nested = repo.add_wt("nested", node_modules=False)
    inner = nested + "/build/pkg"
    os.makedirs(inner)
    sh("git", "init", "-q", "-b", "main", inner)
    open(inner + "/work.txt", "w").write("unique")
    sh("git", "add", ".", cwd=inner)
    sh("git", "commit", "-qm", "unique local commit", cwd=inner)
    age(nested)
    code, summ = run_main(repo, "--daily", "--apply")
    assert code == 0 and summ["removed"] == 0
    items = approval_by_path(repo)
    for w in (notes, public, envw, nested):
        assert os.path.isdir(w), w
        assert items[w]["reason"] == "has-ignored-files", (w, items.get(w))
    assert open(notes + "/tmp/notes.txt").read() == "my notes"
    assert "unique local commit" in sh("git", "log", "--format=%s", cwd=inner)
    assert os.path.isdir(
        nested + "/build/pkg/.git"
    )  # the cache tier refused the nested repo too


def test_strict_ignored_classification_only_lets_real_cache_directories_go(repo):
    w = repo.add_wt("strict", node_modules=False)
    for d in ("dist", "tmp", "data"):
        os.makedirs(w + "/" + d)
    assert wp.ignored_removable_strict(w, "dist/") == ""
    assert wp.ignored_removable_strict(w, "tmp/") == "ignored-directory"
    assert wp.ignored_removable_strict(w, "data/") == "ignored-directory"
    assert wp.ignored_removable_strict(w, "debug.log") == "ignored-file"
    assert wp.ignored_removable_strict(w, ".env.development") == "env-copy-differs"
    assert wp.ignored_removable_strict(w, ".env.production") == "ignored-file"
    assert (
        wp.ignored_removable_strict(w, "x.pyc") == ""
        and wp.ignored_removable_strict(w, ".DS_Store") == ""
    )
    os.makedirs(w + "/dist/inner")
    sh("git", "init", "-q", "-b", "main", w + "/dist/inner")
    assert wp.ignored_removable_strict(w, "dist/") == "contains-git"
    shutil.rmtree(w + "/dist")
    os.symlink(repo.home, w + "/dist")
    assert wp.ignored_removable_strict(w, "dist/") == "symlink"


def _eligible(repo, name):
    w = repo.add_wt(name, node_modules=False)
    wp.DAILY["merged_only"] = True
    e = wp.analyze(repo.entry(w), False)
    assert e["status"] == "ELIGIBLE", e["status"]
    return w, e


def test_recheck_dirty_change_after_the_list_was_read_keeps_and_lists_the_worktree(
    repo,
):
    w, e = _eligible(repo, "late-dirty")
    open(w + "/a.txt", "a").write("changed after analysis\n")
    age(w)  # past the quiet window, so only the cleanliness re-check can stop it
    res = wp.final_check_and_remove(e, True, True)
    assert res == "dirty" and os.path.isdir(w)
    wp.note_approval(e, res)
    assert wp.APPROVAL[-1]["reason"] == "dirty" and wp.APPROVAL[-1]["path"] == w


def test_recheck_untracked_file_after_the_list_was_read_keeps_and_lists_the_worktree(
    repo,
):
    w, e = _eligible(repo, "late-untracked")
    open(w + "/brand_new.txt", "w").write("n\n")
    age(w)
    res = wp.final_check_and_remove(e, True, True)
    assert res == "dirty" and os.path.isfile(w + "/brand_new.txt")
    wp.note_approval(e, res)
    assert wp.APPROVAL[-1]["reason"] == "dirty"


def test_recheck_lock_taken_after_the_list_was_read_keeps_and_lists_the_worktree(repo):
    w, e = _eligible(repo, "late-lock")
    sh("git", "worktree", "lock", w, cwd=repo.main)
    res = wp.final_check_and_remove(e, True, True)
    assert res == "locked" and os.path.isdir(w)
    wp.note_approval(e, res)
    assert wp.APPROVAL[-1]["reason"] == "locked"


def test_recheck_head_moving_after_the_list_was_read_keeps_and_lists_the_worktree(repo):
    w, e = _eligible(repo, "late-head")
    open(w + "/moved.txt", "w").write("m\n")
    sh("git", "add", ".", cwd=w)
    sh("git", "commit", "-qm", "moved", cwd=w)
    age(w)
    res = wp.final_check_and_remove(e, True, True)
    assert res == "head-moved" and os.path.isdir(w)
    wp.note_approval(e, res)
    assert wp.APPROVAL[-1]["reason"] == "head-moved"


# ---- the alert text -------------------------------------------------------------------------
RUN_DONE = {
    "caches": 3,
    "removed": 2,
    "freed_bytes": int(4.2 * 2**30),
    "waiting": 12,
    "waiting_kb": 40 * 1048576,
}


def test_alert_message_is_plain_complete_and_ordered_for_a_phone_reader():
    msg = wp.build_alert_message(18.4, 1, RUN_DONE)
    lines = msg.splitlines()
    assert lines[0] == "Your Mac is low on disk space: 18 GB free."
    assert (
        "cleared 3 cache folders and removed 2 merged work folders, freeing about 4.2 GB"
        in lines[1]
    )
    assert "Nothing else was deleted." in lines[2]
    assert "12 items, about 40.0 GB, are waiting for your decision" in lines[2]
    assert "Nothing is urgent to delete" in lines[3]
    assert ".ateles-keep" in lines[3]
    assert lines[-1].startswith("List: ") and lines[-1].endswith("approval_list.md")
    for jargon in ("GiB", "home volume", "threshold", "HEAD", "origin", "worktree"):
        assert jargon not in "\n".join(lines[:-1]), (
            jargon
        )  # the path line may contain any word


def test_alert_message_says_when_nothing_was_freed_and_nothing_waits():
    msg = wp.build_alert_message(20.0, 1, {})
    assert "Today's check freed nothing." in msg
    assert "Nothing is waiting for your decision." in msg
    one = wp.build_alert_message(
        20.0,
        1,
        {
            "caches": 1,
            "removed": 1,
            "freed_bytes": 5 * 2**20,
            "waiting": 1,
            "waiting_kb": 2048,
        },
    )
    assert (
        "cleared 1 cache folder and removed 1 merged work folder, freeing about 5 MB"
        in one
    )
    assert "1 item, about 2 MB, is waiting" in one


def test_alert_message_wording_differs_when_critical():
    normal = wp.build_alert_message(3.0, 1, RUN_DONE)
    critical = wp.build_alert_message(3.0, 2, RUN_DONE)
    assert critical.startswith(
        "URGENT: your Mac is almost out of disk space: 3 GB free."
    )
    assert "Please free space today" in critical and "Nothing is urgent" not in critical
    assert "Nothing is urgent" in normal and "URGENT" not in normal
    assert critical.splitlines()[-1].startswith("List: ")


def test_the_sent_alert_carries_the_run_facts(repo, monkeypatch):
    dirty = repo.add_wt("alertdirty", node_modules=False)
    open(dirty + "/a.txt", "a").write("x\n")
    age(dirty)
    gone = repo.add_wt("alertgone")
    summ, sent = _alert_run(repo, monkeypatch, 12.0)
    msg = sent[0][0]
    assert msg.startswith("Your Mac is low on disk space: 12 GB free.")
    assert (
        "removed 1 merged work folder" in msg
        and "1 item," in msg
        and "are waiting" not in msg
    )
    assert (
        not os.path.exists(gone)
        and msg.splitlines()[-1] == "List: " + repo.home + "/state/approval_list.md"
    )


# ---- the approval list text ---------------------------------------------------------------
def _item(kind, reason, kb, branch="feat-x", path="/h/wt-a"):
    return {
        "kind": kind,
        "path": path,
        "branch": branch,
        "reason": reason,
        "why": wp.REASON_TEXT[reason],
        "size_kb": kb,
    }


def test_sizes_are_readable_at_every_scale_never_zero_point_zero_zero():
    assert wp.fmt_size(0) == "under 1 MB" and wp.fmt_size(700) == "under 1 MB"
    assert wp.fmt_size(5 * 1024) == "5 MB" and wp.fmt_size(900 * 1024) == "900 MB"
    assert wp.fmt_size(2 * 1048576) == "2.0 GB"
    md = wp.render_approval_markdown(
        [
            _item("worktree", "recent", 700, path="/h/a"),
            _item("worktree", "dirty", 5 * 1024, path="/h/b"),
        ],
        "T",
        "apply",
    )
    assert "0.00" not in md and "(under 1 MB)" in md and "(5 MB)" in md


def test_every_reason_text_is_plain_language_without_git_internals():
    for code, text in wp.REASON_TEXT.items():
        assert not re.search(r"HEAD|origin|--|\bPR\b|upstream|ref\b", text), (
            code,
            text,
        )


def test_work_that_exists_only_here_is_visibly_separate_and_branches_are_shown():
    items = [
        _item("worktree", "recent", 10 * 1024, branch="safe-looking", path="/h/safe"),
        _item(
            "worktree", "unpushed", 20 * 1024, branch="my-unique-work", path="/h/unique"
        ),
        _item("worktree", "keep-marked", 30 * 1024, branch="held", path="/h/held"),
        _item(
            "cache",
            "not-git-ignored",
            1024,
            branch="safe-looking",
            path="/h/safe/out/.cache",
        ),
    ]
    md = wp.render_approval_markdown(items, "T", "apply")
    only = md.index("Work that may exist only on this Mac")
    other = md.index("Other items waiting for your decision")
    kept = md.index("Kept on purpose")
    assert only < other < kept
    assert only < md.index("my-unique-work") < other
    assert other < md.index("/h/safe`") < kept and md.index("/h/held") > kept
    assert "**my-unique-work** `/h/unique`" in md and "**safe-looking** `/h/safe`" in md
    assert (
        "3 items, about 31 MB, are waiting for your decision" in md
    )  # the kept item is not counted


def test_approve_and_keep_commands_are_printed_once_at_the_top():
    md = wp.render_approval_markdown([_item("worktree", "dirty", 2048)], "T", "apply")
    assert (
        md.count("git worktree remove <path>") == 1
        and md.count("touch <path>/.ateles-keep") == 1
    )
    assert md.index("git worktree remove <path>") < md.index("## ")


def test_empty_approval_list_says_nothing_needs_a_decision():
    md = wp.render_approval_markdown([], "T", "apply")
    assert "Nothing needs your decision." in md and "## " not in md
    only_kept = wp.render_approval_markdown(
        [_item("worktree", "locked", 2048)], "T", "apply"
    )
    assert (
        "Nothing needs your decision." in only_kept and "Kept on purpose" in only_kept
    )


# ---- alert delivery honesty ---------------------------------------------------------------
def test_a_failed_or_unavailable_notifier_does_not_disarm_the_alert_and_is_retried(
    repo, monkeypatch
):
    replies = [
        {"available": False, "returned": None, "error": "ImportError: no httpx"},
        {"available": True, "returned": False, "error": ""},
        {"available": True, "returned": True, "error": ""},
    ]
    calls = []
    monkeypatch.setattr(wp, "dfree_gb", lambda: 10.0)
    monkeypatch.setattr(
        wp,
        "send_notification",
        lambda m, sv: calls.append(m) or replies[len(calls) - 1],
    )
    states = []
    for _ in range(4):
        code, summ = run_main(repo, "--daily", "--apply")
        states.append(summ["alert"]["state"])
    assert states == ["fired-undelivered", "fired-undelivered", "fired", "suppressed"]
    assert len(calls) == 3  # retried until delivered, then deduped
    recs = [json.loads(x) for x in open(repo.home + "/state/alerts.jsonl")]
    assert [r["delivered"] for r in recs] == [False, False, True]
    assert state_json(repo, "alert_state.json")["armed"] is False


def test_the_notifier_is_called_without_email_routing(monkeypatch):
    import types

    seen = {}

    class FakeNotifier:
        def send(self, message, priority, handler="", email_eligible=True, **kw):
            seen.update(
                message=message,
                priority=priority,
                handler=handler,
                email_eligible=email_eligible,
            )
            return True

    mod = types.ModuleType("lib.notify")
    mod.Notifier = FakeNotifier
    mod.Priority = SimpleNamespace(CRITICAL="critical", WARN="warn")
    monkeypatch.setitem(sys.modules, "lib.notify", mod)
    out = wp.send_notification("hello", 1)
    assert out == {"available": True, "returned": True, "error": ""}
    assert seen["email_eligible"] is False and seen["priority"] == "warn"
    wp.send_notification("hello", 2)
    assert seen["priority"] == "critical"


def test_the_low_disk_alert_reaches_the_neotoma_sink(monkeypatch):
    # Unmocked path: the real Notifier routes the alert, and the sink records it.
    import json
    from pathlib import Path

    from lib.notify import neotoma_sink

    monkeypatch.setenv(neotoma_sink.MODE_ENV, "shadow")
    out = wp.send_notification("Your Mac is low on disk space: 9 GB free.", 2)
    assert out["available"] is True
    rows = [
        json.loads(line)
        for p in Path(neotoma_sink.sink_dir()).glob("shadow-*.jsonl")
        for line in p.read_text().splitlines()
    ]
    assert len(rows) == 1
    ent = rows[0]["entity"]
    assert ent["body"].endswith("Your Mac is low on disk space: 9 GB free.")
    assert ent["title"].startswith("[worktree-prune/critical] Your Mac is low on disk space: 9 GB free.")
    assert ent["severity"] == "critical"
    assert {"notify", "level:critical", "daemon:worktree-prune"} <= set(ent["tags"])


# ---- caches in use by a running process -------------------------------------------------------
def test_a_cache_directory_with_a_file_open_by_a_running_process_is_kept_and_listed(
    repo,
):
    with open(repo.main + "/.git/info/exclude", "a") as f:
        f.write(".venv/\n")
    w = repo.add_wt("busycache", node_modules=False)
    os.makedirs(w + "/.venv/lib")
    open(w + "/.venv/lib/mod.so", "w").write("binary")
    os.makedirs(w + "/dist")
    open(w + "/dist/out.js", "w").write("o")
    age(w)
    fh = open(w + "/.venv/lib/mod.so")
    p = subprocess.Popen(
        ["sleep", "60"], stdin=fh, cwd="/"
    )  # cwd is elsewhere: only the open file shows it
    repo.procs.append(p)
    fh.close()
    time.sleep(0.4)
    wp.STRIP["caches"] = True
    e = repo.entry(w)
    res = wp.strip_flow(e, True)
    assert res == "STRIPPED"
    assert ".venv:in-use-by-process" in e["strip_refused"]
    assert os.path.isfile(w + "/.venv/lib/mod.so")  # kept
    assert not os.path.exists(w + "/dist")  # the idle cache next to it still went
    wp.note_cache_refusals(e)
    assert (
        wp.APPROVAL[-1]["reason"] == "in-use-by-process"
        and wp.APPROVAL[-1]["branch"] == e["branch"]
    )


def test_a_failed_open_file_scan_keeps_every_cache(repo, monkeypatch):
    w = repo.add_wt("lsoffail")
    wp.STRIP["caches"] = True
    monkeypatch.setattr(wp, "open_file_paths", lambda: (False, set()))
    e = repo.entry(w)
    assert wp.strip_flow(e, True) == "recheck-lsof-error"
    assert os.path.isdir(w + "/node_modules/pkg")


def test_a_dry_run_alert_shows_the_message_but_sends_and_writes_nothing(
    repo, monkeypatch
):
    summ, sent = _alert_run(repo, monkeypatch, 9.0, "--dry-run")
    assert summ["alert"]["state"] == "would-fire" and sent == []
    assert summ["alert"]["message"].startswith(
        "Your Mac is low on disk space: 9 GB free."
    )
    assert not os.path.exists(repo.home + "/state/alerts.jsonl")


# ---- the one disposable ignored file: an identical .env.development copy ----------------------
def _env_wt(repo, name, content, main_content="SETTING=one\n"):
    open(repo.main + "/.env.development", "w").write(main_content)
    w = repo.add_wt(name, node_modules=False)
    open(w + "/.env.development", "w").write(content)
    age(w)
    return w


def test_an_env_development_copy_identical_to_the_main_clone_goes_with_the_worktree(
    repo,
):
    w = _env_wt(repo, "envsame", "SETTING=one\n")
    code, summ = run_main(repo, "--daily", "--apply")
    assert code == 0 and summ["removed"] == 1 and not os.path.exists(w)
    assert os.path.isfile(
        repo.main + "/.env.development"
    )  # the main clone's own file is untouched


def test_an_env_development_copy_that_differs_keeps_the_worktree_and_lists_it(repo):
    differing = "SETTING=two\n"
    w = _env_wt(repo, "envdiff", differing)
    code, summ = run_main(repo, "--daily", "--apply")
    assert summ["removed"] == 0 and os.path.isdir(w)
    assert open(w + "/.env.development").read() == differing
    item = approval_by_path(repo)[w]
    assert item["reason"] == "has-ignored-files"
    # no content (or hash of it) leaks into any record the job writes
    blob = (
        open(repo.home + "/state/approval_list.json").read()
        + open(repo.home + "/state/approval_list.md").read()
    )
    blob += open(repo.home + "/state/runs.jsonl").read()
    for log_name in os.listdir(repo.home + "/state/logs"):
        blob += open(repo.home + "/state/logs/" + log_name).read()
    assert "SETTING=two" not in blob and "SETTING=one" not in blob


def test_env_development_exception_is_narrow(repo):
    same = "SETTING=one\n"
    w = _env_wt(repo, "envnarrow", same)
    assert wp.env_copy_is_disposable(w, repo.main) is True
    assert wp.env_copy_is_disposable(w, None) is False
    os.remove(repo.main + "/.env.development")  # missing in the main clone
    assert wp.env_copy_is_disposable(w, repo.main) is False
    open(repo.main + "/.env.development", "w").write(same)
    os.remove(w + "/.env.development")
    os.symlink(
        repo.main + "/.env.development", w + "/.env.development"
    )  # a link is not a copy
    assert wp.env_copy_is_disposable(w, repo.main) is False
    os.remove(w + "/.env.development")
    open(w + "/.env.development.bak", "w").write(
        same
    )  # renamed variants are never disposable
    assert (
        wp.ignored_removable_strict(w, ".env.development.bak", repo.main)
        == "ignored-file"
    )
    os.makedirs(w + "/sub")
    open(w + "/sub/.env.development", "w").write(same)  # only at the worktree root
    assert (
        wp.ignored_removable_strict(w, "sub/.env.development", repo.main)
        == "ignored-file"
    )


# ---- round 3: open files block whole removal; the byte comparison ignores filecmp's cache ---------
def test_a_file_open_in_the_worktree_by_a_running_process_blocks_whole_removal(repo):
    w, e = _eligible(repo, "openfile")
    fh = open(w + "/a.txt")
    p = subprocess.Popen(
        ["sleep", "60"], stdin=fh, cwd="/"
    )  # cwd is elsewhere: only the open file shows it
    repo.procs.append(p)
    fh.close()
    time.sleep(0.4)
    res = wp.final_check_and_remove(e, True, True)
    assert res == "in-use" and os.path.isdir(w)
    wp.note_approval(e, res)
    assert wp.APPROVAL[-1]["reason"] == "in-use"


def test_a_failed_open_file_scan_keeps_the_worktree_on_whole_removal(repo, monkeypatch):
    w, e = _eligible(repo, "scanfail")
    monkeypatch.setattr(wp, "open_file_paths", lambda: (False, set()))
    assert wp.final_check_and_remove(e, True, True) == "recheck-lsof-error"
    assert os.path.isdir(w)


def test_the_env_copy_comparison_is_not_fooled_by_filecmps_cache(repo):
    w = _env_wt(repo, "envcache", "SETTING=one\n")
    assert wp.env_copy_is_disposable(w, repo.main) is True  # populates filecmp's cache
    mine, theirs = w + "/.env.development", repo.main + "/.env.development"
    st = os.stat(theirs)
    open(theirs, "w").write("SETTING=two\n")  # same length
    os.utime(
        theirs, ns=(st.st_atime_ns, st.st_mtime_ns)
    )  # same size and mtime: the cache key is unchanged
    assert os.stat(mine).st_size == os.stat(theirs).st_size
    assert wp.env_copy_is_disposable(w, repo.main) is False
