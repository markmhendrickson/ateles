"""The runner's clone and the import of an agent's work (deploy/fly-runner/repo_delivery.py).

The agent's checkout is hostile once the agent has run. These tests play the
agent: they edit its config, plant hooks and links, rewrite its branch, and
hand root crafted bundles, and check that nothing reaches the root-owned
``origin.git`` unless every check passes. Offline: the "GitHub" remote is a
local bare repository and the token is a placeholder. The tests run as one
user, so the uid switch itself is not exercised here.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

_RUNNER_DIR = Path(__file__).resolve().parents[3] / "deploy" / "fly-runner"


def _load():
    spec = importlib.util.spec_from_file_location("fly_repo_delivery", _RUNNER_DIR / "repo_delivery.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["fly_repo_delivery"] = module
    spec.loader.exec_module(module)
    return module


rd = _load()

TOKEN = "ghs_placeholder_delivery_token_value_0042"
BRANCH = "cicada/fix-the-gate-20261015T120000-a1b2c3"
REPO = "owner/repo"
AUTHOR = ("cicada", "cicada@agents.example.invalid")


def _env(home):
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "LC_ALL": "C"}


def git(cwd, *args, home, input=None):
    done = subprocess.run(["git", *args], cwd=cwd, env=_env(home), capture_output=True,
                          input=input, check=False)
    assert done.returncode == 0, done.stderr.decode()
    return done.stdout.decode().strip()


@pytest.fixture
def world(tmp_path):
    """A stand-in GitHub repository with history, and a prepared job."""
    home = tmp_path / "home"
    home.mkdir()
    src = tmp_path / "src"
    git(tmp_path, "init", "-q", "-b", "main", str(src), home=home)
    for i in range(3):
        (src / f"f{i}.txt").write_text(f"{i}\n")
        git(src, "add", ".", home=home)
        git(src, "commit", "-qm", f"c{i}", home=home)
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "-q", "--bare", str(src), str(remote), home=home)
    root = tmp_path / "work-root"
    work = rd.prepare_work(root, run_id="fly-test-0001")
    base = rd.clone_origin(work, url=f"file://{remote}", base_branch="main", token=TOKEN,
                           allow="file")
    rd.make_checkout(work, branch=BRANCH, base_branch="main", repository=REPO, ident=None,
                     author=AUTHOR)
    return {"home": home, "src": src, "remote": remote, "root": root, "work": work,
            "base": base, "old": git(src, "rev-parse", "HEAD~1", home=home)}


def agent_commit(world, name="new.txt", content="work\n", n=1):
    co = world["work"].checkout
    for i in range(n):
        (co / f"{i}-{name}").write_text(content)
        git(co, "add", ".", home=world["home"])
        git(co, "commit", "-qm", f"agent {i}", home=world["home"])
    return git(co, "rev-parse", "HEAD", home=world["home"])


def deliver(world, **limits):
    w = world["work"]
    if not rd.export_agent_work(w, branch=BRANCH, base=world["base"], ident=None):
        return None
    return rd.import_agent_work(w, branch=BRANCH, base=world["base"], **limits)


def origin_ref(world, ref=f"refs/heads/{BRANCH}"):
    done = subprocess.run(["git", "-C", str(world["work"].origin), "rev-parse", "--verify", "-q",
                           ref], env=_env(world["home"]), capture_output=True)
    return done.stdout.decode().strip() or None


def plant_bundle(world, *refspecs, cwd=None):
    """Write a bundle of the agent's choosing where root will read it."""
    out = world["work"].export / rd.BUNDLE_NAME
    out.unlink(missing_ok=True)
    git(cwd or world["work"].checkout, "bundle", "create", "-q", str(out), *refspecs,
        home=world["home"])


def refused(world, match, **limits):
    with pytest.raises(rd.RepoWorkError, match=match) as exc:
        rd.import_agent_work(world["work"], branch=BRANCH, base=world["base"], **limits)
    assert exc.value.category == rd.IMPORT_REFUSED
    assert origin_ref(world) is None  # the branch was never recorded
    assert origin_ref(world, rd.QUARANTINE_REF) is None  # and the quarantine is gone


# ── The clone ────────────────────────────────────────────────────────────────


def test_the_clone_and_checkout_hold_the_base_and_no_credential(world):
    w = world["work"]
    assert world["base"] == git(world["src"], "rev-parse", "HEAD", home=world["home"])
    assert git(w.checkout, "rev-parse", "--abbrev-ref", "HEAD", home=world["home"]) == BRANCH
    assert git(w.checkout, "config", "remote.origin.url", home=world["home"]) == \
        f"https://github.com/{REPO}.git"
    header = rd.auth_config(TOKEN)[0][1]
    for dirpath, _, files in os.walk(world["root"]):
        for name in files:
            data = Path(dirpath, name).read_bytes()
            assert TOKEN.encode() not in data and header.encode() not in data, name
    assert "extraheader" not in (w.origin / "config").read_text()


def test_the_clone_is_shallow_and_single_branch(world):
    w = world["work"]
    refs = git(w.origin, "for-each-ref", "--format=%(refname)", home=world["home"]).split()
    assert refs == ["refs/heads/main"]
    assert (w.origin / "shallow").is_file()


def test_a_clone_larger_than_the_cap_is_refused(tmp_path, world):
    work = rd.prepare_work(tmp_path / "other-root", run_id="r2")
    with pytest.raises(rd.RepoWorkError, match="clone_refused: the clone is larger than the accepted size"):
        rd.clone_origin(work, url=f"file://{world['remote']}", base_branch="main", token=TOKEN,
                        allow="file", max_bytes=10)


def test_a_failed_clone_names_no_credential(tmp_path, world):
    work = rd.prepare_work(tmp_path / "other-root", run_id="r3")
    with pytest.raises(rd.RepoWorkError) as exc:
        rd.clone_origin(work, url=f"file://{world['remote']}", base_branch="no-such-branch",
                        token=TOKEN, allow="file")
    assert TOKEN not in str(exc.value)


def test_the_clone_uses_only_the_allowed_transport(tmp_path, world):
    work = rd.prepare_work(tmp_path / "other-root", run_id="r4")
    with pytest.raises(rd.RepoWorkError, match="clone_failed: fetching the base branch"):
        rd.clone_origin(work, url=f"file://{world['remote']}", base_branch="main", token=TOKEN)


@pytest.mark.parametrize("base", ["", "-main", "a..b", "refs/heads/main", "main branch", "x" * 101])
def test_an_unacceptable_base_branch_is_refused(tmp_path, base):
    work = rd.prepare_work(tmp_path / "r", run_id="r")
    with pytest.raises(rd.RepoWorkError, match="base branch"):
        rd.clone_origin(work, url="file:///nowhere", base_branch=base, token=TOKEN, allow="file")


# ── Work paths ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("root", ["/data", "/data/work", "/run/job", "/run/job/x", "/run", "/"])
def test_the_work_root_never_overlaps_the_volume_or_the_job_directory(root):
    with pytest.raises(rd.RepoWorkError, match="overlaps"):
        rd.check_work_root(Path(root))


@pytest.mark.parametrize("root", ["work", "/work/../data", "/work/"])
def test_the_work_root_must_be_absolute_and_normalised(root):
    with pytest.raises(rd.RepoWorkError, match="absolute"):
        rd.check_work_root(Path(root) if root != "/work/" else root)  # type: ignore[arg-type]


def test_a_work_root_reached_through_a_link_is_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    with pytest.raises(rd.RepoWorkError, match="link"):
        rd.prepare_work(tmp_path / "link" / "work", run_id="r")


def test_leftovers_are_cleared_without_following_links(tmp_path):
    root = tmp_path / "root"
    (root / "job-old" / "origin.git").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (root / "escape").symlink_to(outside)
    (root / "job-old" / "escape").symlink_to(outside)
    rd.prepare_work(root, run_id="new")
    assert sorted(os.listdir(root)) == ["job-new"]
    assert (outside / "keep.txt").read_text() == "keep"
    rd.remove_work(root)
    assert os.listdir(root) == [] and (outside / "keep.txt").is_file()


# ── Importing the agent's work: the accepted case ───────────────────────────


def test_the_agents_commits_are_imported_onto_the_assigned_branch(world):
    head = agent_commit(world, n=2)
    imported = deliver(world)
    assert imported == rd.Imported(head=head, commits=2)
    assert origin_ref(world) == head and origin_ref(world, rd.QUARANTINE_REF) is None


def test_no_new_commits_means_nothing_to_import(world):
    assert deliver(world) is None
    assert origin_ref(world) is None


def test_a_deleted_branch_is_refused(world):
    agent_commit(world)
    git(world["work"].checkout, "checkout", "-q", "--detach", home=world["home"])
    git(world["work"].checkout, "branch", "-D", BRANCH, home=world["home"])
    with pytest.raises(rd.RepoWorkError, match="assigned branch"):
        deliver(world)


# ── Hostile checkouts ───────────────────────────────────────────────────────


def test_the_agents_own_config_and_hooks_never_reach_root(world, tmp_path):
    co = world["work"].checkout
    marker = tmp_path / "hook-ran"
    hooks = co / ".git" / "hooks"
    for name in ("pre-push", "post-checkout", "reference-transaction", "post-merge",
                 "pre-auto-gc", "fsmonitor-watchman"):
        hook = hooks / name
        hook.write_text(f"#!/bin/sh\necho {name} >> {marker}\n")
        hook.chmod(0o755)
    for key, value in (("remote.origin.url", "https://elsewhere.example.invalid/x.git"),
                       ("url.https://elsewhere.example.invalid/.insteadOf", "https://github.com/"),
                       ("core.hooksPath", str(hooks)), ("credential.helper", "!echo leaked"),
                       ("core.fsmonitor", f"echo fsmonitor >> {marker}"),
                       ("include.path", str(tmp_path / "evil.cfg"))):
        git(co, "config", key, value, home=world["home"])
    before = (world["work"].origin / "config").read_text()
    head = agent_commit(world)
    marker.unlink(missing_ok=True)  # the agent's own commit may run its own hooks
    imported = rd.import_agent_work(world["work"], branch=BRANCH, base=world["base"]) \
        if rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None) \
        else None
    assert imported is not None and imported.head == head
    assert (world["work"].origin / "config").read_text() == before
    assert not marker.exists()  # nothing of the agent's ran during export or import


def test_a_bundle_carrying_other_refs_is_refused(world):
    agent_commit(world)
    git(world["work"].checkout, "tag", "v9", home=world["home"])
    git(world["work"].checkout, "branch", "main-copy", home=world["home"])
    plant_bundle(world, f"refs/heads/{BRANCH}", "refs/tags/v9", "refs/heads/main-copy",
                 f"^{world['base']}")
    refused(world, "refs other than the assigned branch")


def test_a_bundle_naming_another_branch_is_refused(world):
    agent_commit(world)
    git(world["work"].checkout, "branch", "-f", "main", BRANCH, home=world["home"])
    plant_bundle(world, "refs/heads/main", f"^{world['base']}")
    refused(world, "refs other than the assigned branch")


def test_unrelated_history_is_refused(world):
    co = world["work"].checkout
    git(co, "checkout", "-q", "--orphan", "orphan", home=world["home"])
    git(co, "rm", "-rqf", ".", home=world["home"])
    (co / "other.txt").write_text("unrelated\n")
    git(co, "add", ".", home=world["home"])
    git(co, "commit", "-qm", "unrelated", home=world["home"])
    git(co, "branch", "-f", BRANCH, "orphan", home=world["home"])
    plant_bundle(world, f"refs/heads/{BRANCH}")
    refused(world, "does not descend from the base")


def test_a_branch_rewritten_onto_older_history_is_refused(world):
    # Built on a commit before the base: its prerequisite is missing from the
    # shallow origin, or it does not descend from the base.
    src = world["src"]
    git(src, "checkout", "-q", "-b", BRANCH, world["old"], home=world["home"])
    (src / "side.txt").write_text("side\n")
    git(src, "add", ".", home=world["home"])
    git(src, "commit", "-qm", "side", home=world["home"])
    plant_bundle(world, f"refs/heads/{BRANCH}", f"^{world['old']}", cwd=src)
    refused(world, "could not be imported|does not descend")


def test_a_full_history_bundle_that_skips_the_base_is_refused(world):
    src = world["src"]
    git(src, "checkout", "-q", "-b", BRANCH, world["old"], home=world["home"])
    (src / "side.txt").write_text("side\n")
    git(src, "add", ".", home=world["home"])
    git(src, "commit", "-qm", "side", home=world["home"])
    plant_bundle(world, f"refs/heads/{BRANCH}", cwd=src)
    refused(world, "does not descend from the base")


def test_more_commits_than_the_cap_are_refused(world):
    agent_commit(world, n=4)
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    refused(world, "more commits than accepted", max_commits=3)


def test_a_merge_commit_is_refused(world):
    co = world["work"].checkout
    git(co, "checkout", "-q", "-b", "side", home=world["home"])
    agent_commit(world, name="side.txt")
    git(co, "checkout", "-q", BRANCH, home=world["home"])
    agent_commit(world, name="main.txt")
    git(co, "merge", "-q", "--no-ff", "-m", "merge", "side", home=world["home"])
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    refused(world, "merge commit")


def test_an_oversized_file_is_refused(world):
    agent_commit(world, content="x" * 5000)
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    refused(world, "file is larger than accepted", max_blob_bytes=1000)


def test_an_oversized_bundle_is_refused_before_it_is_read(world):
    agent_commit(world, content="y" * 5000)
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    refused(world, "bundle is larger than accepted", max_bundle_bytes=100)


@pytest.mark.parametrize("target", ["/etc/passwd", "../../outside", "../.git/config", "a/../../x"])
def test_a_link_pointing_outside_the_repository_is_refused(world, target):
    co = world["work"].checkout
    (co / "sub").mkdir()
    os.symlink(target, co / "sub" / "link")
    git(co, "add", ".", home=world["home"])
    git(co, "commit", "-qm", "link", home=world["home"])
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    refused(world, "link points outside")


def test_a_link_inside_the_repository_is_accepted(world):
    co = world["work"].checkout
    (co / "sub").mkdir()
    os.symlink("../f0.txt", co / "sub" / "link")
    git(co, "add", ".", home=world["home"])
    git(co, "commit", "-qm", "link", home=world["home"])
    assert deliver(world).commits == 1


def _crafted_tree_commit(world, name):
    co, home = world["work"].checkout, world["home"]
    blob = git(co, "hash-object", "-w", "--stdin", home=home, input=b"evil\n")
    entry = b"100644 " + name.encode() + b"\0" + bytes.fromhex(blob)
    tree = git(co, "hash-object", "-t", "tree", "-w", "--literally", "--stdin", home=home,
               input=entry)
    commit = git(co, "commit-tree", tree, "-p", "HEAD", "-m", "crafted", home=home)
    git(co, "update-ref", f"refs/heads/{BRANCH}", commit, home=home)


@pytest.mark.parametrize("name", [".git", ".GIT", ".git.", "..", "git~1"])
def test_a_crafted_path_into_git_or_out_of_the_repository_is_refused(world, name):
    _crafted_tree_commit(world, name)
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    refused(world, "could not be imported|leaves the repository")


def test_a_gitmodules_file_is_refused(world):
    (world["work"].checkout / ".gitmodules").write_text(
        '[submodule "x"]\n\tpath = x\n\turl = https://example.invalid/x.git\n')
    git(world["work"].checkout, "add", ".", home=world["home"])
    git(world["work"].checkout, "commit", "-qm", "modules", home=world["home"])
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    refused(world, "submodule definition")


def test_a_submodule_entry_is_refused(world):
    co = world["work"].checkout
    git(co, "update-index", "--add", "--cacheinfo", f"160000,{world['base']},vendored",
        home=world["home"])
    git(co, "commit", "-qm", "gitlink", home=world["home"])
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    refused(world, "submodule entry")


def test_a_bundle_replaced_by_a_link_is_refused(world, tmp_path):
    agent_commit(world)
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    real = tmp_path / "elsewhere.bundle"
    (world["work"].export / rd.BUNDLE_NAME).rename(real)
    (world["work"].export / rd.BUNDLE_NAME).symlink_to(real)
    refused(world, "missing or is a link")


def test_an_export_directory_replaced_by_a_link_is_refused(world, tmp_path):
    agent_commit(world)
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    moved = tmp_path / "moved-export"
    world["work"].export.rename(moved)
    world["work"].export.symlink_to(moved)
    refused(world, "export directory is missing or is a link")


def test_a_bundle_that_is_not_a_regular_file_is_refused(world):
    os.mkfifo(world["work"].export / rd.BUNDLE_NAME)
    refused(world, "not a regular file")


def test_a_garbage_bundle_is_refused(world):
    (world["work"].export / rd.BUNDLE_NAME).write_bytes(b"# v2 git bundle\nnot really\n")
    refused(world, "bundle could not be read")


# ── Branch names ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("branch", [
    "main", "master", "release/1.0", "v1.2.3", "refs/heads/cicada/x-20261015T120000-a1b2c3",
    "cicada/../main", "cicada/Fix-20261015T120000-a1b2c3", "cicada/fix-20261015T120000-A1B2C3",
    "cicada/fix", "lanius/fix-20261015T120000-a1b2c3", "cicada/fix-20261015T120000-a1b2c3/x",
    "cicada/-fix-20261015T120000-a1b2c3", "cicada/fix--x-20261015T120000-a1b2c3", "", None,
])
def test_a_branch_that_fails_the_strict_pattern_is_refused(world, branch):
    with pytest.raises(rd.RepoWorkError, match="builder pattern"):
        rd.import_agent_work(world["work"], branch=branch, base=world["base"])
    with pytest.raises(rd.RepoWorkError, match="builder pattern"):
        rd.export_agent_work(world["work"], branch=branch, base=world["base"], ident=None)


def test_the_strict_pattern_accepts_the_local_build_branch_shape():
    assert rd.check_branch(BRANCH) == BRANCH


# ── The git environment ──────────────────────────────────────────────────────


def test_the_git_environment_inherits_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    monkeypatch.setenv("GIT_TRACE", "1")
    env = rd.git_env(tmp_path, allow="https")
    assert "GIT_DIR" not in env and "GIT_TRACE" not in env
    pairs = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
             for i in range(int(env["GIT_CONFIG_COUNT"]))}
    assert pairs["core.hooksPath"] == "/dev/null" and pairs["credential.helper"] == ""
    assert pairs["transfer.fsckObjects"] == "true" and pairs["protocol.allow"] == "never"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert TOKEN not in str(env)


def test_the_origin_repository_is_private_to_root(world):
    assert os.stat(world["work"].origin).st_mode & 0o077 == 0


# ── Errors carry a category and a fixed reason, never a value ────────────────

SENTINELS = (TOKEN, "/srv/sentinel-path-7f3a", "cicada/sentinel-branch-20261015T120000-ffffff",
             "https://sentinel.example.invalid/owner/repo.git", "sentinel-base-branch")


def _assert_value_free(exc, *extra):
    texts = [str(exc), repr(exc), *map(str, exc.args), exc.reason, exc.status]
    for value in (*SENTINELS, rd.auth_config(TOKEN)[0][1], *extra):
        assert not any(value in t for t in texts), value
    assert exc.category in {rd.WORK_ROOT_REFUSED, rd.BRANCH_REFUSED, rd.CLONE_FAILED,
                            rd.CLONE_REFUSED, rd.CHECKOUT_FAILED, rd.EXPORT_FAILED,
                            rd.IMPORT_FAILED, rd.IMPORT_REFUSED, rd.QUARANTINE_FAILED}
    assert exc.status in {"", "exit_nonzero", "timed_out", "not_started", "killed_by_signal"}


def _failing(monkeypatch, *, when):
    """Make the git call whose argv contains every word in *when* fail, with
    every sentinel in its stderr and stdout."""
    real = subprocess.run
    noisy = " ".join(SENTINELS).encode()

    def fake(args, *a, **k):
        if all(w in args for w in when):
            return subprocess.CompletedProcess(args, 128, stdout=noisy, stderr=b"fatal: " + noisy)
        return real(args, *a, **k)

    monkeypatch.setattr(rd.subprocess, "run", fake)


def test_a_failed_clone_names_no_value(tmp_path, world):
    work = rd.prepare_work(tmp_path / "other-root", run_id="r3")
    url = f"file://{world['remote']}-{SENTINELS[1].strip('/')}"
    with pytest.raises(rd.RepoWorkError) as exc:
        rd.clone_origin(work, url=url, base_branch="sentinel-base-branch", token=TOKEN,
                        allow="file")
    assert exc.value.category == rd.CLONE_FAILED and exc.value.status == "exit_nonzero"
    _assert_value_free(exc.value, url, str(world["remote"]), str(tmp_path))


@pytest.mark.parametrize("step,when,category", [
    ("clone", ("fetch", "--depth=1"), rd.CLONE_FAILED),
    ("clone", ("init",), rd.CLONE_FAILED),
    ("export", ("bundle", "create"), rd.EXPORT_FAILED),
    ("export", ("rev-parse", "-q"), rd.EXPORT_FAILED),
    ("import", ("bundle", "list-heads"), rd.IMPORT_REFUSED),
    ("import", ("fetch", "--no-tags"), rd.IMPORT_REFUSED),
    ("import", ("diff-tree",), rd.IMPORT_FAILED),
    ("import", ("cat-file",), rd.IMPORT_FAILED),
    ("import", ("update-ref", "00" * 20), rd.IMPORT_FAILED),
])
def test_no_git_output_or_argument_reaches_the_error(world, monkeypatch, tmp_path, step, when,
                                                     category):
    if step == "clone":
        work = rd.prepare_work(tmp_path / "clone-root", run_id="r5")
        _failing(monkeypatch, when=when)
        with pytest.raises(rd.RepoWorkError) as exc:
            rd.clone_origin(work, url=f"file://{world['remote']}", base_branch="main",
                            token=TOKEN, allow="file")
    else:
        agent_commit(world)
        if step == "import":
            rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
        _failing(monkeypatch, when=when)
        with pytest.raises(rd.RepoWorkError) as exc:
            deliver(world) if step == "export" else rd.import_agent_work(
                world["work"], branch=BRANCH, base=world["base"])
    assert exc.value.category == category
    _assert_value_free(exc.value, str(tmp_path), BRANCH, world["base"])


def test_a_git_that_cannot_start_or_times_out_reports_only_that(world, monkeypatch, tmp_path):
    work = rd.prepare_work(tmp_path / "t-root", run_id="r6")

    def timeout(args, *a, **k):
        raise subprocess.TimeoutExpired(args, 1, output=SENTINELS[0].encode())

    monkeypatch.setattr(rd.subprocess, "run", timeout)
    with pytest.raises(rd.RepoWorkError) as exc:
        rd.clone_origin(work, url="file:///x", base_branch="main", token=TOKEN, allow="file")
    assert exc.value.status == "timed_out"
    _assert_value_free(exc.value)


# ── The quarantine ref: an import is complete only when it is gone ───────────


def _quarantine_delete(monkeypatch, *, fail_times, check_rc=None, branch_delete=None,
                       branch_check_rc=None):
    """Make deleting the quarantine ref do nothing (exit 1) *fail_times* times;
    optionally make the branch record's deletion fail ("noop" or "timeout")
    and its read-back answer *branch_check_rc*."""
    real = subprocess.run
    state = {"left": fail_times}
    branch_ref = f"refs/heads/{BRANCH}"

    def fake(args, *a, **k):
        if "update-ref" in args and "-d" in args and rd.QUARANTINE_REF in args and state["left"]:
            state["left"] -= 1
            return subprocess.CompletedProcess(args, 1, b"", b"")
        if check_rc is not None and "rev-parse" in args and rd.QUARANTINE_REF in args:
            return subprocess.CompletedProcess(args, check_rc, b"", b"")
        if branch_delete and "update-ref" in args and "-d" in args and branch_ref in args:
            if branch_delete == "timeout":
                raise subprocess.TimeoutExpired(args, 1)
            return subprocess.CompletedProcess(args, 1, b"", b"")
        if branch_check_rc is not None and "rev-parse" in args and branch_ref in args:
            return subprocess.CompletedProcess(args, branch_check_rc, b"", b"")
        return real(args, *a, **k)

    monkeypatch.setattr(rd.subprocess, "run", fake)


def test_a_quarantine_that_cannot_be_removed_withdraws_the_import(world, monkeypatch):
    agent_commit(world)
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    _quarantine_delete(monkeypatch, fail_times=99)
    with pytest.raises(rd.RepoWorkError) as exc:
        rd.import_agent_work(world["work"], branch=BRANCH, base=world["base"])
    assert exc.value.category == rd.QUARANTINE_FAILED
    assert exc.value.reason == rd.WITHDRAWAL_CONFIRMED and "nothing may be pushed" in str(exc.value)
    assert origin_ref(world) is None  # read back absent: the confirmed case
    assert origin_ref(world, rd.QUARANTINE_REF) is not None  # left over, and reported


@pytest.mark.parametrize("branch_delete,branch_check_rc", [
    ("noop", None),       # the branch deletion fails
    ("timeout", None),    # the branch deletion times out
    (None, 128),          # deleted, but absence cannot be read back
    ("noop", 128),        # both
])
def test_an_unconfirmed_withdrawal_never_claims_one(world, monkeypatch, branch_delete,
                                                    branch_check_rc):
    head = agent_commit(world)
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    _quarantine_delete(monkeypatch, fail_times=99, branch_delete=branch_delete,
                       branch_check_rc=branch_check_rc)
    with pytest.raises(rd.RepoWorkError) as exc:
        rd.import_agent_work(world["work"], branch=BRANCH, base=world["base"])
    assert exc.value.category == rd.QUARANTINE_FAILED
    assert exc.value.reason == rd.WITHDRAWAL_UNRESOLVED
    assert "withdrawn and read back" not in str(exc.value)
    assert "nothing may be pushed" in str(exc.value)
    monkeypatch.undo()
    remaining = origin_ref(world)
    # The ref state is what it is; the error claimed nothing about it.
    assert remaining in (None, head)
    if branch_delete:
        assert remaining == head  # still recorded, and the error did not say otherwise
    assert origin_ref(world, rd.QUARANTINE_REF) is not None


def test_a_quarantine_removed_on_the_second_attempt_completes(world, monkeypatch):
    head = agent_commit(world)
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    _quarantine_delete(monkeypatch, fail_times=1)
    assert rd.import_agent_work(world["work"], branch=BRANCH, base=world["base"]).head == head
    assert origin_ref(world) == head and origin_ref(world, rd.QUARANTINE_REF) is None


def test_an_unconfirmed_quarantine_removal_is_not_a_clean_import(world, monkeypatch):
    agent_commit(world)
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    _quarantine_delete(monkeypatch, fail_times=0, check_rc=128)  # absence cannot be read
    with pytest.raises(rd.RepoWorkError) as exc:
        rd.import_agent_work(world["work"], branch=BRANCH, base=world["base"])
    assert exc.value.category == rd.QUARANTINE_FAILED and origin_ref(world) is None


def test_a_refused_import_with_a_leftover_quarantine_is_still_refused(world, monkeypatch):
    agent_commit(world, n=4)
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    _quarantine_delete(monkeypatch, fail_times=99)
    with pytest.raises(rd.RepoWorkError) as exc:
        rd.import_agent_work(world["work"], branch=BRANCH, base=world["base"], max_commits=3)
    assert exc.value.category == rd.IMPORT_REFUSED and origin_ref(world) is None


# ── Every entry point that deletes or creates under a work root is guarded ───


@pytest.fixture
def forbidden(tmp_path, monkeypatch):
    """Stand-ins for the login volume and the job directory, each with a
    sentinel file in it and in a child."""
    data, job = tmp_path / "data", tmp_path / "run" / "job"
    for d in (data, data / "child", job, job / "child"):
        d.mkdir(parents=True)
        (d / "sentinel").write_text("keep")
    monkeypatch.setattr(rd, "FORBIDDEN_ROOTS", (str(data), str(job)))
    return [data, data / "child", job, job / "child", tmp_path / "run"]


def _intact(paths):
    for d in paths:
        if d.name != "run":
            assert (d / "sentinel").read_text() == "keep", d


def _entry_points():
    return {
        "clear_work_root": lambda root: rd.clear_work_root(root),
        "remove_work": lambda root: rd.remove_work(root),
        "prepare_work": lambda root: rd.prepare_work(root, run_id="r"),
    }


@pytest.mark.parametrize("entry", sorted(_entry_points()))
def test_cleanup_never_reaches_a_forbidden_root(forbidden, entry):
    for root in forbidden:
        with pytest.raises(rd.RepoWorkError) as exc:
            _entry_points()[entry](root)
        assert exc.value.category == rd.WORK_ROOT_REFUSED
    _intact(forbidden)


@pytest.mark.parametrize("entry", sorted(_entry_points()))
def test_cleanup_never_reaches_a_root_through_a_link(tmp_path, entry):
    real = tmp_path / "real"
    (real / "inner").mkdir(parents=True)
    for d in (real, real / "inner"):
        (d / "sentinel").write_text("keep")
    (tmp_path / "link").symlink_to(real)
    for root in (tmp_path / "link", tmp_path / "link" / "inner"):
        with pytest.raises(rd.RepoWorkError) as exc:
            _entry_points()[entry](root)
        assert exc.value.category == rd.WORK_ROOT_REFUSED
    _intact([real, real / "inner"])


@pytest.mark.parametrize("entry", sorted(_entry_points()))
def test_cleanup_never_reaches_a_relative_or_unnormalised_root(tmp_path, monkeypatch, entry):
    target = tmp_path / "rel" / "dir"
    target.mkdir(parents=True)
    (target / "sentinel").write_text("keep")
    monkeypatch.chdir(tmp_path)
    for root in (Path("rel/dir"), f"{tmp_path}/rel/../rel/dir", f"{tmp_path}/rel/dir/"):
        with pytest.raises(rd.RepoWorkError) as exc:
            _entry_points()[entry](root)
        assert exc.value.category == rd.WORK_ROOT_REFUSED
    _intact([target])


def test_a_work_layout_not_from_prepare_work_is_refused_everywhere(world, tmp_path):
    w = world["work"]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "sentinel").write_text("keep")
    forged = [
        rd.Work(root=w.root, job=w.job, origin=elsewhere, checkout=w.checkout, export=w.export,
                home=w.home),
        rd.Work(root=w.root, job=w.job, origin=w.origin, checkout=elsewhere, export=w.export,
                home=w.home),
        rd.Work(root=elsewhere, job=elsewhere / "job-x", origin=elsewhere / "job-x" / "origin.git",
                checkout=elsewhere / "job-x" / "checkout", export=elsewhere / "job-x" / "export",
                home=elsewhere / "job-x" / "home"),
    ]
    calls = [
        lambda f: rd.clone_origin(f, url="file:///x", base_branch="main", token=TOKEN, allow="file"),
        lambda f: rd.make_checkout(f, branch=BRANCH, base_branch="main", repository=REPO,
                                   ident=None, author=AUTHOR),
        lambda f: rd.export_agent_work(f, branch=BRANCH, base=world["base"], ident=None),
        lambda f: rd.import_agent_work(f, branch=BRANCH, base=world["base"]),
    ]
    for f in forged[:2]:
        for call in calls:
            with pytest.raises(rd.RepoWorkError) as exc:
                call(f)
            assert exc.value.category == rd.WORK_ROOT_REFUSED
    assert sorted(os.listdir(elsewhere)) == ["sentinel"]
    # A layout of the right shape under an accepted root is not refused for its shape.
    assert rd._check_work(forged[2]) == forged[2]


def test_remove_work_on_a_missing_root_does_nothing(tmp_path):
    rd.remove_work(tmp_path / "never-made")
    assert not (tmp_path / "never-made").exists()


# ── Exact limits are accepted ────────────────────────────────────────────────


def test_exactly_the_commit_cap_is_accepted(world):
    agent_commit(world, n=3)
    assert deliver(world, max_commits=3).commits == 3


def test_a_file_of_exactly_the_size_cap_is_accepted(world):
    agent_commit(world, content="x" * 1000)
    assert deliver(world, max_blob_bytes=1000).commits == 1


def test_a_bundle_of_exactly_the_size_cap_is_accepted(world):
    agent_commit(world)
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    size = (world["work"].export / rd.BUNDLE_NAME).stat().st_size
    imported = rd.import_agent_work(world["work"], branch=BRANCH, base=world["base"],
                                    max_bundle_bytes=size)
    assert imported.commits == 1


def test_a_bundle_one_byte_over_the_cap_is_refused(world):
    agent_commit(world)
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    size = (world["work"].export / rd.BUNDLE_NAME).stat().st_size
    refused(world, "bundle is larger than accepted", max_bundle_bytes=size - 1)


# ── A link may not pass through another link, inherited ones included ────────


@pytest.fixture
def linked_world(tmp_path):
    """A base tree that already holds a link pointing outside the repository."""
    home = tmp_path / "home"
    home.mkdir()
    src = tmp_path / "src"
    git(tmp_path, "init", "-q", "-b", "main", str(src), home=home)
    (src / "f0.txt").write_text("0\n")
    os.symlink("/etc", src / "inherited")
    (src / "dir").mkdir()
    (src / "dir" / "f1.txt").write_text("1\n")
    git(src, "add", ".", home=home)
    git(src, "commit", "-qm", "base", home=home)
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "-q", "--bare", str(src), str(remote), home=home)
    work = rd.prepare_work(tmp_path / "work-root", run_id="linked")
    base = rd.clone_origin(work, url=f"file://{remote}", base_branch="main", token=TOKEN,
                           allow="file")
    rd.make_checkout(work, branch=BRANCH, base_branch="main", repository=REPO, ident=None,
                     author=AUTHOR)
    return {"home": home, "work": work, "base": base, "src": src}


@pytest.mark.parametrize("path,target,accepted", [
    ("through", "inherited/passwd", False),   # through the inherited link
    ("sub/through", "../inherited/x", False),
    ("to-link", "inherited", False),          # a link to a link
    ("plain", "f0.txt", True),
    ("sub/plain", "../dir/f1.txt", True),
])
def test_a_link_through_an_inherited_link_is_refused(linked_world, path, target, accepted):
    w = linked_world
    co = w["work"].checkout
    (co / path).parent.mkdir(parents=True, exist_ok=True)
    os.symlink(target, co / path)
    git(co, "add", ".", home=w["home"])
    git(co, "commit", "-qm", "link", home=w["home"])
    rd.export_agent_work(w["work"], branch=BRANCH, base=w["base"], ident=None)
    if accepted:
        assert rd.import_agent_work(w["work"], branch=BRANCH, base=w["base"]).commits == 1
    else:
        with pytest.raises(rd.RepoWorkError, match="link points outside"):
            rd.import_agent_work(w["work"], branch=BRANCH, base=w["base"])


# ── Filesystem failures become fixed categories, chained to nothing ──────────

FS_SENTINEL = "/srv/fs-sentinel-path-91c2"
FS_STRERROR = "fs-sentinel-strerror-91c2"


def _fs_error():
    return PermissionError(13, FS_STRERROR, FS_SENTINEL)


class _OsProxy:
    """The os module for repo_delivery only, with chosen calls failing."""

    def __init__(self, fail, when=lambda *a, **k: True):
        self._fail, self._when = fail, when

    def __getattr__(self, name):
        real = getattr(os, name)
        if name not in self._fail:
            return real

        def failing(*a, **k):
            if self._when(*a, **k):
                raise _fs_error()
            return real(*a, **k)

        return failing


def _assert_chain_free(exc):
    seen = exc
    texts = []
    while seen is not None:
        texts += [str(seen), repr(seen), *map(str, getattr(seen, "args", ()))]
        seen = seen.__cause__ or seen.__context__
    assert exc.__cause__ is None and exc.__context__ is None
    for t in texts:
        assert FS_SENTINEL not in t and FS_STRERROR not in t


def test_a_filesystem_failure_preparing_work_is_value_free(tmp_path, monkeypatch):
    real_mkdir = Path.mkdir

    def failing(self, *a, **k):
        if self.name.startswith("job-"):
            raise _fs_error()
        return real_mkdir(self, *a, **k)

    monkeypatch.setattr(Path, "mkdir", failing)
    with pytest.raises(rd.RepoWorkError) as exc:
        rd.prepare_work(tmp_path / "root", run_id="r")
    assert exc.value.category == rd.WORK_FAILED and exc.value.status == "os_error"
    _assert_chain_free(exc.value)


@pytest.mark.parametrize("call", ["os.listdir:clear_work_root", "shutil.rmtree:remove_work"])
def test_a_filesystem_failure_clearing_work_is_value_free(tmp_path, monkeypatch, call):
    target, entry = call.split(":")
    root = tmp_path / "root"
    (root / "job-old").mkdir(parents=True)
    module, name = target.split(".")
    if module == "os":
        monkeypatch.setattr(rd, "os", _OsProxy({name}))
    else:
        def rmtree(*a, **k):
            raise _fs_error()
        monkeypatch.setattr(rd.shutil, "rmtree", rmtree)
    with pytest.raises(rd.RepoWorkError) as exc:
        getattr(rd, entry)(root)
    assert exc.value.category == rd.WORK_FAILED
    _assert_chain_free(exc.value)


def test_a_filesystem_failure_in_the_clone_is_value_free(tmp_path, world, monkeypatch):
    work = rd.prepare_work(tmp_path / "fs-root", run_id="r")
    monkeypatch.setattr(rd, "os", _OsProxy({"chmod"}))
    with pytest.raises(rd.RepoWorkError) as exc:
        rd.clone_origin(work, url=f"file://{world['remote']}", base_branch="main", token=TOKEN,
                        allow="file")
    assert exc.value.category == rd.CLONE_FAILED and exc.value.status == "os_error"
    _assert_chain_free(exc.value)


def test_a_filesystem_failure_sizing_the_clone_is_value_free(tmp_path, world, monkeypatch):
    work = rd.prepare_work(tmp_path / "fs-root", run_id="r")
    monkeypatch.setattr(rd, "os", _OsProxy({"walk"}))
    with pytest.raises(rd.RepoWorkError) as exc:
        rd.clone_origin(work, url=f"file://{world['remote']}", base_branch="main", token=TOKEN,
                        allow="file")
    assert exc.value.category == rd.CLONE_FAILED
    _assert_chain_free(exc.value)


def test_a_filesystem_failure_handing_over_the_checkout_is_value_free(tmp_path, world,
                                                                      monkeypatch):
    work = rd.prepare_work(tmp_path / "fs-root", run_id="r")
    rd.clone_origin(work, url=f"file://{world['remote']}", base_branch="main", token=TOKEN,
                    allow="file")
    monkeypatch.setattr(rd, "os", _OsProxy({"chown"}))
    with pytest.raises(rd.RepoWorkError) as exc:
        rd.make_checkout(work, branch=BRANCH, base_branch="main", repository=REPO,
                         ident=(os.getuid(), os.getgid()), author=AUTHOR)
    assert exc.value.category == rd.CHECKOUT_FAILED
    _assert_chain_free(exc.value)


@pytest.mark.parametrize("failing", ["fstat", "read", "open-copy", "unlink-copy"])
def test_a_filesystem_failure_copying_the_bundle_is_value_free(world, monkeypatch, failing):
    agent_commit(world)
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    if failing == "open-copy":
        proxy = _OsProxy({"open"}, when=lambda path, *a, **k: str(path).endswith(rd.IMPORT_COPY_NAME))
    elif failing == "unlink-copy":
        (world["work"].job / rd.IMPORT_COPY_NAME).write_bytes(b"left over")
        proxy = _OsProxy({"unlink"})
    else:
        proxy = _OsProxy({failing})
    monkeypatch.setattr(rd, "os", proxy)
    with pytest.raises(rd.RepoWorkError) as exc:
        rd.import_agent_work(world["work"], branch=BRANCH, base=world["base"])
    assert exc.value.category == rd.IMPORT_FAILED and exc.value.status == "os_error"
    _assert_chain_free(exc.value)
    monkeypatch.undo()
    assert origin_ref(world) is None


@pytest.mark.parametrize("missing", ["bundle", "export-directory"])
def test_a_refusal_opening_the_export_is_chained_to_nothing(world, tmp_path, missing):
    agent_commit(world)
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    if missing == "bundle":
        (world["work"].export / rd.BUNDLE_NAME).unlink()
    else:
        world["work"].export.rename(tmp_path / "moved-away")
    with pytest.raises(rd.RepoWorkError) as exc:
        rd.import_agent_work(world["work"], branch=BRANCH, base=world["base"])
    assert exc.value.category == rd.IMPORT_REFUSED
    assert exc.value.__context__ is None and exc.value.__cause__ is None


# ── Repeated imports and the private copy ───────────────────────────────────


def test_each_import_attempt_starts_from_a_fresh_private_copy(world):
    head = agent_commit(world, n=2)
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    copy = world["work"].job / rd.IMPORT_COPY_NAME
    with pytest.raises(rd.RepoWorkError):
        rd.import_agent_work(world["work"], branch=BRANCH, base=world["base"], max_commits=1)
    assert not copy.exists()  # removed when the attempt ended
    copy.write_bytes(b"a copy an interrupted attempt left behind")
    imported = rd.import_agent_work(world["work"], branch=BRANCH, base=world["base"])
    assert imported.head == head and not copy.exists()


def test_a_second_import_of_a_recorded_branch_is_refused(world):
    agent_commit(world)
    assert deliver(world).commits == 1
    with pytest.raises(rd.RepoWorkError) as exc:
        rd.import_agent_work(world["work"], branch=BRANCH, base=world["base"])
    assert exc.value.category == rd.IMPORT_FAILED  # the branch must not already exist
    assert origin_ref(world, rd.QUARANTINE_REF) is None


# ── Path names are compared as raw bytes ────────────────────────────────────

ODD_NAMES = [b"caf\xc3\xa9", b"\xff\xfe-not-utf8", b"sp ace", b'quo"te', b"back\\slash",
             b"tab\tname", b"new\nline", b"plain-name"]


def _blob(repo, home, data):
    return git(repo, "hash-object", "-w", "--stdin", home=home, input=data)


def _tree(repo, home, entries):
    """``git mktree -z`` from (mode, type, oid, name-bytes) entries."""
    data = b"".join(b"%s %s %s\t%s\0" % (m.encode(), t.encode(), o.encode(), n)
                    for m, t, o, n in entries)
    return git(repo, "mktree", "-z", home=home, input=data)


@pytest.fixture
def odd_world(tmp_path):
    """A source repository built with plumbing only (no checkout, so any byte
    is a valid name), cloned into origin.git; the agent's commits are made
    in the source and handed over as a bundle."""
    home = tmp_path / "home"
    home.mkdir()
    src = tmp_path / "src.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(src), home=home)
    return {"home": home, "src": src, "tmp": tmp_path}


def _base_commit(w, extra):
    home, src = w["home"], w["src"]
    f0 = _blob(src, home, b"0\n")
    tree = _tree(src, home, [("100644", "blob", f0, b"f0.txt"), *extra])
    commit = git(src, "commit-tree", tree, "-m", "base", home=home)
    git(src, "update-ref", "refs/heads/main", commit, home=home)
    work = rd.prepare_work(w["tmp"] / "work-root", run_id="odd")
    base = rd.clone_origin(work, url=f"file://{src}", base_branch="main", token=TOKEN,
                           allow="file")
    work.export.mkdir(mode=0o700)
    assert base == commit
    return work, base, tree


def _agent_link(w, work, base, base_entries, name, target):
    home, src = w["home"], w["src"]
    link = _blob(src, home, target)
    tree = _tree(src, home, [*base_entries, ("120000", "blob", link, name)])
    commit = git(src, "commit-tree", tree, "-p", base, "-m", "agent", home=home)
    git(src, "update-ref", f"refs/heads/{BRANCH}", commit, home=home)
    out = work.export / rd.BUNDLE_NAME
    out.unlink(missing_ok=True)
    git(src, "bundle", "create", "-q", str(out), f"refs/heads/{BRANCH}", f"^{base}", home=home)
    return commit


def _import(work, base):
    return rd.import_agent_work(work, branch=BRANCH, base=base)


@pytest.mark.parametrize("name", ODD_NAMES)
def test_a_link_through_an_inherited_link_with_any_name_is_refused(odd_world, name):
    w = odd_world
    etc = _blob(w["src"], w["home"], b"/etc")
    f0 = _blob(w["src"], w["home"], b"0\n")
    entries = [("100644", "blob", f0, b"f0.txt"), ("120000", "blob", etc, name)]
    work, base, _ = _base_commit(w, entries[1:])
    _agent_link(w, work, base, entries, b"through", name + b"/passwd")
    with pytest.raises(rd.RepoWorkError, match="link points outside") as exc:
        _import(work, base)
    assert exc.value.category == rd.IMPORT_REFUSED


@pytest.mark.parametrize("name", ODD_NAMES)
def test_a_link_to_an_inherited_link_with_any_name_is_refused(odd_world, name):
    w = odd_world
    etc = _blob(w["src"], w["home"], b"/etc")
    f0 = _blob(w["src"], w["home"], b"0\n")
    entries = [("100644", "blob", f0, b"f0.txt"), ("120000", "blob", etc, name)]
    work, base, _ = _base_commit(w, entries[1:])
    _agent_link(w, work, base, entries, b"to-link", name)
    with pytest.raises(rd.RepoWorkError, match="link points outside"):
        _import(work, base)


@pytest.mark.parametrize("name", ODD_NAMES)
def test_a_link_into_a_directory_with_any_name_is_accepted(odd_world, name):
    w = odd_world
    f = _blob(w["src"], w["home"], b"inside\n")
    sub = _tree(w["src"], w["home"], [("100644", "blob", f, b"f")])
    f0 = _blob(w["src"], w["home"], b"0\n")
    entries = [("100644", "blob", f0, b"f0.txt"), ("040000", "tree", sub, name)]
    work, base, _ = _base_commit(w, entries[1:])
    head = _agent_link(w, work, base, entries, b"inside", name + b"/f")
    assert _import(work, base).head == head


@pytest.mark.parametrize("name", ODD_NAMES)
def test_a_changed_link_with_any_name_is_judged_by_its_target(odd_world, name):
    w = odd_world
    f0 = _blob(w["src"], w["home"], b"0\n")
    entries = [("100644", "blob", f0, b"f0.txt")]
    work, base, _ = _base_commit(w, [])
    head = _agent_link(w, work, base, entries, name, b"f0.txt")
    assert _import(work, base).head == head
    _agent_link(w, work, base, entries, name, b"/etc/passwd")
    with pytest.raises(rd.RepoWorkError, match="link points outside"):
        # A fresh branch name is not needed: the earlier import recorded the
        # branch, so record-time refusal would hide the link check. Remove it.
        git(work.origin, "update-ref", "-d", f"refs/heads/{BRANCH}", home=w["home"])
        _import(work, base)


@pytest.mark.parametrize("name", ODD_NAMES)
def test_a_changed_link_inside_any_named_directory_resolves_by_bytes(odd_world, name):
    """A link added inside a directory with an odd name, whose target passes
    through a link inherited in that same directory, is refused; resolving
    it needs the directory's exact bytes."""
    w = odd_world
    etc = _blob(w["src"], w["home"], b"/etc")
    f = _blob(w["src"], w["home"], b"inside\n")
    sub = _tree(w["src"], w["home"], [("120000", "blob", etc, b"y"), ("100644", "blob", f, b"f")])
    f0 = _blob(w["src"], w["home"], b"0\n")
    entries = [("100644", "blob", f0, b"f0.txt"), ("040000", "tree", sub, name)]
    work, base, _ = _base_commit(w, entries[1:])
    home, src = w["home"], w["src"]
    for target, accepted in ((b"y/passwd", False), (b"f", True)):
        link = _blob(src, home, target)
        new_sub = _tree(src, home, [("120000", "blob", etc, b"y"), ("100644", "blob", f, b"f"),
                                    ("120000", "blob", link, b"x")])
        tree = _tree(src, home, [entries[0], ("040000", "tree", new_sub, name)])
        commit = git(src, "commit-tree", tree, "-p", base, "-m", "agent", home=home)
        git(src, "update-ref", f"refs/heads/{BRANCH}", commit, home=home)
        out = work.export / rd.BUNDLE_NAME
        out.unlink(missing_ok=True)
        git(src, "bundle", "create", "-q", str(out), f"refs/heads/{BRANCH}", f"^{base}",
            home=home)
        if accepted:
            assert _import(work, base).head == commit
        else:
            with pytest.raises(rd.RepoWorkError, match="link points outside"):
                _import(work, base)


# ── The private copy is removed on every exit path of an import attempt ──────


def _exported(world, content="work\n"):
    agent_commit(world, content=content)
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    return world["work"].job / rd.IMPORT_COPY_NAME


def test_a_size_refusal_while_copying_leaves_no_private_copy(world):
    copy = _exported(world, content="z" * 5000)
    with pytest.raises(rd.RepoWorkError, match="bundle is larger than accepted"):
        rd.import_agent_work(world["work"], branch=BRANCH, base=world["base"],
                             max_bundle_bytes=100)
    assert not os.path.lexists(copy)


def test_a_read_failure_mid_copy_leaves_no_private_copy(world, monkeypatch):
    copy = _exported(world)
    monkeypatch.setattr(rd, "os", _OsProxy({"read"}, when=lambda *a, **k: copy.exists()))
    with pytest.raises(rd.RepoWorkError) as exc:
        rd.import_agent_work(world["work"], branch=BRANCH, base=world["base"])
    assert exc.value.category == rd.IMPORT_FAILED
    _assert_chain_free(exc.value)
    monkeypatch.undo()
    assert not os.path.lexists(copy)


class _FailingWrites:
    """A copy file whose writes fail after it has been created."""

    def __init__(self, fd, *a, **k):
        self._fd = fd

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        os.close(self._fd)
        return False

    def write(self, data):
        raise _fs_error()


def test_a_write_failure_mid_copy_leaves_no_private_copy(world, monkeypatch):
    copy = _exported(world)
    proxy = _OsProxy(set())
    proxy.fdopen = _FailingWrites
    monkeypatch.setattr(rd, "os", proxy)
    with pytest.raises(rd.RepoWorkError) as exc:
        rd.import_agent_work(world["work"], branch=BRANCH, base=world["base"])
    assert exc.value.category == rd.IMPORT_FAILED
    _assert_chain_free(exc.value)
    monkeypatch.undo()
    assert not os.path.lexists(copy)


def test_an_interrupt_mid_copy_leaves_no_private_copy(world, monkeypatch):
    copy = _exported(world)
    real_read = os.read

    class Interrupting(_OsProxy):
        def __getattr__(self, name):
            if name == "read":
                def read(*a, **k):
                    if copy.exists():
                        raise KeyboardInterrupt
                    return real_read(*a, **k)
                return read
            return getattr(os, name)

    monkeypatch.setattr(rd, "os", Interrupting(set()))
    with pytest.raises(KeyboardInterrupt):
        rd.import_agent_work(world["work"], branch=BRANCH, base=world["base"])
    monkeypatch.undo()
    assert not os.path.lexists(copy)


def test_a_failed_removal_after_a_refusal_keeps_the_refusal(world, monkeypatch):
    _exported(world, content="z" * 5000)
    monkeypatch.setattr(rd, "os", _OsProxy(
        {"unlink"}, when=lambda path, *a, **k: str(path).endswith(rd.IMPORT_COPY_NAME)))
    with pytest.raises(rd.RepoWorkError) as exc:
        rd.import_agent_work(world["work"], branch=BRANCH, base=world["base"],
                             max_bundle_bytes=100)
    assert exc.value.category == rd.IMPORT_REFUSED
    assert exc.value.reason == "the bundle is larger than accepted"
    _assert_chain_free(exc.value)


def test_a_failed_removal_after_an_import_is_a_fixed_error(world, monkeypatch):
    _exported(world)
    monkeypatch.setattr(rd, "os", _OsProxy(
        {"unlink"}, when=lambda path, *a, **k: str(path).endswith(rd.IMPORT_COPY_NAME)))
    with pytest.raises(rd.RepoWorkError) as exc:
        rd.import_agent_work(world["work"], branch=BRANCH, base=world["base"])
    assert exc.value.category == rd.IMPORT_FAILED and "nothing may be pushed" in str(exc.value)
    _assert_chain_free(exc.value)
