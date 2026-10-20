"""The repository delivery's user separation, run for real: as root, with the
agent as an unprivileged user (uid 10001, as in the runner image).

Runs only as root on Linux, which is how CI runs it (in a container from the
image's base, see .github/workflows/ateles-tests.yml); elsewhere it is
skipped. It checks that the agent cannot read or write origin.git or write
the job directory, that the agent's export runs as the agent and the import
as root, that the push lands the branch, that no credential reaches the disk,
and that the work is removed afterwards.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from fly_cleanup_test_support import assert_cleanup

pytestmark = pytest.mark.skipif(
    sys.platform != "linux" or os.geteuid() != 0,
    reason="needs root on Linux (runs in a container in CI)",
)

_RUNNER_DIR = Path(__file__).resolve().parents[3] / "deploy" / "fly-runner"
AGENT_UID = 10001
TOKEN = "ghs_placeholder_container_token_0101"
BRANCH = "cicada/container-check-20261015T120000-a1b2c3"


def _load():
    spec = importlib.util.spec_from_file_location("fly_repo_delivery_c", _RUNNER_DIR / "repo_delivery.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["fly_repo_delivery_c"] = module
    spec.loader.exec_module(module)
    return module


rd = _load()
ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp", "GIT_CONFIG_NOSYSTEM": "1",
       "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def _agent_ident():
    import pwd

    try:
        entry = pwd.getpwuid(AGENT_UID)
    except KeyError:
        subprocess.run(["useradd", "--uid", str(AGENT_UID), "--create-home", "--shell",
                        "/usr/sbin/nologin", "agent"], check=True)
        entry = pwd.getpwuid(AGENT_UID)
    return entry.pw_uid, entry.pw_gid


def _as_agent(ident, *cmd, cwd=None):
    return subprocess.run(list(cmd), env={**ENV, "HOME": "/tmp"}, cwd=cwd, user=ident[0],
                          group=ident[1], extra_groups=[], capture_output=True)


def test_the_agent_and_root_sides_stay_separate(tmp_path, monkeypatch):
    ident = _agent_ident()
    src = tmp_path / "src"
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], env=ENV, check=True)
    (src / "README").write_text("base\n")
    subprocess.run(["git", "-C", str(src), "add", "."], env=ENV, check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-qm", "base"], env=ENV, check=True)
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(remote)], env=ENV, check=True)
    monkeypatch.setattr(rd, "GITHUB_REMOTE", f"file://{remote}")
    monkeypatch.setattr(rd, "GITHUB_REMOTE_TRANSPORT", "file")

    root = Path("/work-container-check")
    work = rd.prepare_work(root, run_id="fly-container")
    try:
        base = rd.clone_origin(work, url=f"file://{remote}", base_branch="main", token=TOKEN,
                               allow="file")
        rd.make_checkout(work, branch=BRANCH, base_branch="main", repository="owner/repo",
                         ident=ident, author=("cicada", "cicada@agents.example.invalid"))

        # The agent cannot read or write origin.git, nor write the job directory.
        assert _as_agent(ident, "ls", str(work.origin)).returncode != 0
        assert _as_agent(ident, "sh", "-c", f"echo x > {work.origin}/config").returncode != 0
        assert _as_agent(ident, "sh", "-c", f"echo x > {work.job}/planted").returncode != 0
        assert not (work.job / "planted").exists()

        # The agent works in its own checkout, as itself.
        assert _as_agent(ident, "sh", "-c", "echo work > change.txt && git add . && "
                         "git commit -qm work", cwd=work.checkout).returncode == 0

        assert rd.export_agent_work(work, branch=BRANCH, base=base, ident=ident)
        bundle = work.export / rd.BUNDLE_NAME
        assert os.stat(bundle).st_uid == AGENT_UID  # written as the agent
        imported = rd.import_agent_work(work, branch=BRANCH, base=base)
        rd.push_branch(work, repository="owner/repo", branch=BRANCH, default_branch="main",
                       head=imported.head, token=TOKEN)
        landed = subprocess.run(["git", "-C", str(remote), "rev-parse", f"refs/heads/{BRANCH}"],
                                env=ENV, capture_output=True, text=True).stdout.strip()
        assert landed == imported.head

        header = rd.auth_config(TOKEN)[0][1].encode()
        for dirpath, _, files in os.walk(root):
            for name in files:
                data = Path(dirpath, name).read_bytes()
                assert TOKEN.encode() not in data and header not in data, name
    finally:
        rd.remove_work(root)
    assert_cleanup(root, present=True)
