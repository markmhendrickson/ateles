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
NOFORBIDDEN: tuple[str, ...] = ()


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
    work = rd.prepare_work(root, run_id="fly-test-0001", forbidden=NOFORBIDDEN)
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
    with pytest.raises(rd.RepoWorkError, match=match):
        rd.import_agent_work(world["work"], branch=BRANCH, base=world["base"], **limits)
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
    work = rd.prepare_work(tmp_path / "other-root", run_id="r2", forbidden=NOFORBIDDEN)
    with pytest.raises(rd.RepoWorkError, match="larger than 10 bytes"):
        rd.clone_origin(work, url=f"file://{world['remote']}", base_branch="main", token=TOKEN,
                        allow="file", max_bytes=10)


def test_a_failed_clone_names_no_credential(tmp_path, world):
    work = rd.prepare_work(tmp_path / "other-root", run_id="r3", forbidden=NOFORBIDDEN)
    with pytest.raises(rd.RepoWorkError) as exc:
        rd.clone_origin(work, url=f"file://{world['remote']}", base_branch="no-such-branch",
                        token=TOKEN, allow="file")
    assert TOKEN not in str(exc.value)


def test_the_clone_uses_only_the_allowed_transport(tmp_path, world):
    work = rd.prepare_work(tmp_path / "other-root", run_id="r4", forbidden=NOFORBIDDEN)
    with pytest.raises(rd.RepoWorkError, match="the clone failed"):
        rd.clone_origin(work, url=f"file://{world['remote']}", base_branch="main", token=TOKEN)


@pytest.mark.parametrize("base", ["", "-main", "a..b", "refs/heads/main", "main branch", "x" * 101])
def test_an_unacceptable_base_branch_is_refused(tmp_path, base):
    work = rd.prepare_work(tmp_path / "r", run_id="r", forbidden=NOFORBIDDEN)
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
        rd.prepare_work(tmp_path / "link" / "work", run_id="r", forbidden=NOFORBIDDEN)


def test_leftovers_are_cleared_without_following_links(tmp_path):
    root = tmp_path / "root"
    (root / "job-old" / "origin.git").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep")
    (root / "escape").symlink_to(outside)
    (root / "job-old" / "escape").symlink_to(outside)
    rd.prepare_work(root, run_id="new", forbidden=NOFORBIDDEN)
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
    refused(world, "importing the agent's bundle failed|does not descend")


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
    refused(world, "adds 4 commits; at most 3", max_commits=3)


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
    refused(world, "file larger than 1000 bytes", max_blob_bytes=1000)


def test_an_oversized_bundle_is_refused_before_it_is_read(world):
    agent_commit(world, content="y" * 5000)
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    refused(world, "bundle is larger than 100 bytes", max_bundle_bytes=100)


@pytest.mark.parametrize("target", ["/etc/passwd", "../../outside", "../.git/config", "a/../../x"])
def test_a_link_pointing_outside_the_repository_is_refused(world, target):
    co = world["work"].checkout
    (co / "sub").mkdir()
    os.symlink(target, co / "sub" / "link")
    git(co, "add", ".", home=world["home"])
    git(co, "commit", "-qm", "link", home=world["home"])
    rd.export_agent_work(world["work"], branch=BRANCH, base=world["base"], ident=None)
    refused(world, "link that points outside")


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
    refused(world, "importing the agent's bundle failed|leaves the repository")


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
    refused(world, "reading the agent's bundle failed")


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
