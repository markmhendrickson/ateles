"""A Fly job that delivers a branch and a pull request (deploy/fly-runner/run_job.py).

The runner clones one repository into a root-owned work directory, runs the
CLI in the agent's checkout, imports the agent's commits, pushes exactly the
job's branch, and finds or opens the pull request. These tests drive the real
entrypoint with a stand-in CLI, a local bare repository in GitHub's place and
a stand-in GitHub API. The job's token is a placeholder; nothing leaves the
machine.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

_RUNNER_DIR = Path(__file__).resolve().parents[3] / "deploy" / "fly-runner"


def _load_run_job():
    spec = importlib.util.spec_from_file_location("fly_run_job_delivery", _RUNNER_DIR / "run_job.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["fly_run_job_delivery"] = module
    spec.loader.exec_module(module)
    return module


run_job = _load_run_job()
rd = run_job._repo_delivery()

TOKEN = "ghs_placeholder_delivery_token_0077"
HEADER = rd.auth_config(TOKEN)[0][1]
REPO = "owner/repo"
BRANCH = "cicada/fix-the-gate-20261015T120000-a1b2c3"
DELIVERY = {"branch": BRANCH, "base_branch": "main", "author_name": "cicada",
            "author_email": "cicada@agents.example.invalid"}
ROLE_TOKEN = "lanius-own-neotoma-token-0099"


def _genv(home):
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "LC_ALL": "C"}


def git(cwd, *args, home):
    done = subprocess.run(["git", *args], cwd=cwd, env=_genv(home), capture_output=True)
    assert done.returncode == 0, done.stderr.decode()
    return done.stdout.decode().strip()


def _deadline_in(seconds: float) -> str:
    return datetime.fromtimestamp(time.time() + seconds, tz=timezone.utc).isoformat()


@pytest.fixture
def world(tmp_path, monkeypatch):
    """GitHub's place taken by a local bare repository and a stand-in API."""
    home = tmp_path / "home"
    home.mkdir()
    src = tmp_path / "src"
    git(tmp_path, "init", "-q", "-b", "main", str(src), home=home)
    (src / "README").write_text("base\n")
    git(src, "add", ".", home=home)
    git(src, "commit", "-qm", "base", home=home)
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "-q", "--bare", str(src), str(remote), home=home)
    work_root = tmp_path / "work"
    monkeypatch.setattr(run_job, "WORK_ROOT", work_root)
    monkeypatch.setattr(rd, "GITHUB_REMOTE", f"file://{remote}")
    monkeypatch.setattr(rd, "GITHUB_REMOTE_TRANSPORT", "file")
    monkeypatch.setattr(run_job, "probe_paths_default",
                        lambda: (str(tmp_path / "operator.env"),))
    monkeypatch.setenv(run_job.DEADLINE_ENV, _deadline_in(900))
    api = {"calls": [], "open": [], "fail": {}, "disk": []}

    def fake_api(method, path, token, body=None):
        api["calls"].append((method, path, token, body))
        # What is on disk while the delivery runs: no token, no header.
        for dirpath, _, files in os.walk(work_root):
            for name in files:
                data = Path(dirpath, name).read_bytes()
                api["disk"].append(TOKEN.encode() in data or HEADER.encode() in data)
        if method in api["fail"]:
            return api["fail"][method], None
        if method == "GET":
            return 200, api["open"]
        number = 7 if method == "POST" else int(path.rsplit("/", 1)[1])
        return (201 if method == "POST" else 200), _pr(number)

    monkeypatch.setattr(rd, "_api", fake_api)
    return {"home": home, "remote": remote, "work_root": work_root, "api": api, "tmp": tmp_path}


def _pr(number):
    return {"number": number, "html_url": f"https://github.com/{REPO}/pull/{number}",
            "head": {"ref": BRANCH, "repo": {"full_name": REPO}}, "base": {"ref": "main"}}


def _job(tmp_path, *, delivery=DELIVERY, secrets=None, argv=None, stdin=None):
    job_dir = tmp_path / "job"
    (job_dir / "secrets").mkdir(parents=True)
    secrets = {"LANIUS_NEOTOMA_TOKEN": ROLE_TOKEN, run_job.DELIVERY_TOKEN_NAME: TOKEN} \
        if secrets is None else secrets
    spec = {"run_id": "fly-deliver-1", "role": "lanius", "provider": "codex",
            "repository": REPO, "timeout_seconds": 600, "credential_names": sorted(secrets),
            "child_credential_names": [], "neotoma_token_name": "LANIUS_NEOTOMA_TOKEN",
            "neotoma_jwk_name": "", "neotoma_base_url": "https://neotoma.example.test",
            "probe_env_names": [], "probe_paths": [], "child_user": ""}
    if delivery is not None:
        spec["delivery"] = delivery
    (job_dir / "job.json").write_text(json.dumps(spec))
    if argv is None:
        argv = ["codex", "exec", "--cd", run_job.WORKSPACE_PLACEHOLDER, "-"] if delivery \
            else ["codex", "exec", "-"]
    (job_dir / "argv.json").write_text(json.dumps(argv))
    (job_dir / "stdin").write_bytes(stdin if stdin is not None else
                                    b"Do the task. Write the PR text to @@ATELES_JOB_PR_FILE@@.")
    for name, value in secrets.items():
        (job_dir / "secrets" / name).write_text(value)
    return job_dir


def _agent(world, *, commits=1, rc=0, pr_text="# Fix the gate\n\nWhat changed.\n", files=None,
           out=b"done\n", seen=None):
    """A stand-in CLI: commits in its working directory and writes the PR text
    where the prompt said."""
    def runner(argv, env, stdin, timeout, cwd, ident=None, collect_by=None):
        if seen is not None:
            seen.update(argv=argv, env=env, stdin=stdin, timeout=timeout, cwd=cwd)
        for i in range(commits):
            for name, content in (files or {f"change-{i}.txt": "work\n"}).items():
                Path(cwd, name).write_text(content)
            git(cwd, "add", "-A", home=world["home"])
            git(cwd, "commit", "-qm", f"agent {i}", home=world["home"])
        marker = stdin.split(b"to ", 1)[1].rstrip(b".")
        if pr_text is not None:
            Path(marker.decode()).write_text(pr_text)
        return rc, out, b"", False
    return runner


def _run(job_dir, runner):
    stored = {}
    rc = run_job.main(job_dir, writer=lambda b, a, r: stored.setdefault("record", r) is not None,
                      runner=runner, hard_exit=lambda code: None)
    return rc, stored.get("record")


def _remote_ref(world, ref=f"refs/heads/{BRANCH}"):
    done = subprocess.run(["git", "-C", str(world["remote"]), "rev-parse", "--verify", "-q", ref],
                          env=_genv(world["home"]), capture_output=True)
    return done.stdout.decode().strip() or None


def _no_secret_anywhere(record, world, *, disk=True):
    text = json.dumps(record)
    assert TOKEN not in text and HEADER not in text
    if disk:
        assert not any(world["api"]["disk"])  # never on disk while delivering
    assert not world["work_root"].exists() or os.listdir(world["work_root"]) == []


# ── No delivery: unchanged ───────────────────────────────────────────────────


def test_a_job_without_a_delivery_is_unchanged(world):
    job_dir = _job(world["tmp"], delivery=None, secrets={"LANIUS_NEOTOMA_TOKEN": ROLE_TOKEN})
    seen = {}

    def runner(argv, env, stdin, timeout, cwd, ident=None, collect_by=None):
        seen.update(cwd=cwd, argv=argv)
        return 0, b"ok\n", b"", False

    rc, record = _run(job_dir, runner)
    assert rc == 0 and not any(k.startswith(("delivery_", "pr_")) for k in record)
    assert not world["work_root"].exists() and world["api"]["calls"] == []
    assert seen["argv"] == ["codex", "exec", "-"]


# ── Delivered ────────────────────────────────────────────────────────────────


def test_a_delivery_pushes_the_branch_and_opens_the_pull_request(world):
    seen = {}
    rc, record = _run(_job(world["tmp"]), _agent(world, commits=2, seen=seen))
    assert rc == 0
    assert record["delivery_status"] == "opened" and record["delivery_reason"] == ""
    assert record["delivery_branch"] == BRANCH and record["pr_number"] == 7
    assert record["pr_url"] == f"https://github.com/{REPO}/pull/7"
    assert _remote_ref(world) == record["delivery_commit"]
    assert _remote_ref(world, "refs/heads/main") == git(world["tmp"] / "src", "rev-parse", "HEAD",
                                                         home=world["home"])
    methods = [c[0] for c in world["api"]["calls"]]
    assert methods == ["GET", "POST"]
    post = world["api"]["calls"][1][3]
    assert post == {"title": "Fix the gate", "head": BRANCH, "base": "main", "body": "What changed."}
    # The CLI ran in its checkout, was told it, and never held the token.
    assert seen["cwd"].endswith("/checkout") and seen["argv"][3] == seen["cwd"]
    assert b"@@ATELES_JOB_PR_FILE@@" not in seen["stdin"]
    assert TOKEN not in json.dumps(seen["env"]) and HEADER not in json.dumps(seen["env"])
    _no_secret_anywhere(record, world)


def test_an_open_pull_request_for_the_branch_is_updated_not_duplicated(world):
    world["api"]["open"] = [_pr(12)]
    rc, record = _run(_job(world["tmp"]), _agent(world))
    assert record["delivery_status"] == "updated" and record["pr_number"] == 12
    assert [c[0] for c in world["api"]["calls"]] == ["GET", "PATCH"]


def test_the_cli_budget_keeps_the_delivery_and_record_reserves(world):
    seen = {}
    _run(_job(world["tmp"]), _agent(world, seen=seen))
    assert seen["timeout"] <= 900 - run_job.DELIVERY_RESERVE_SECONDS - run_job.RECORD_RESERVE_SECONDS


# ── Not delivered ────────────────────────────────────────────────────────────


def test_no_commits_means_no_push_and_no_pull_request(world):
    rc, record = _run(_job(world["tmp"]), _agent(world, commits=0))
    assert record["delivery_status"] == "no_changes"
    assert _remote_ref(world) is None and world["api"]["calls"] == []
    _no_secret_anywhere(record, world)


def test_a_failed_cli_delivers_nothing(world):
    rc, record = _run(_job(world["tmp"]), _agent(world, rc=1))
    assert rc == 1 and record["delivery_status"] == "not_delivered"
    assert _remote_ref(world) is None and world["api"]["calls"] == []


def test_a_failed_clone_never_runs_the_cli(world, monkeypatch):
    monkeypatch.setattr(rd, "GITHUB_REMOTE", f"file://{world['tmp']}/missing.git")
    rc, record = _run(_job(world["tmp"]),
                      lambda *a, **k: pytest.fail("the CLI must not run without its clone"))
    assert rc == run_job.EXIT_DELIVERY_FAILED
    assert record["delivery_status"] == "clone_failed"
    assert record["delivery_reason"].startswith("clone_failed: ")
    _no_secret_anywhere(record, world)


def test_a_rejected_push_never_forces_and_opens_no_pull_request(world):
    # The branch already exists on the remote with other history.
    src = world["tmp"] / "src"
    git(src, "checkout", "-q", "-b", BRANCH, home=world["home"])
    (src / "other.txt").write_text("someone else\n")
    git(src, "add", ".", home=world["home"])
    git(src, "commit", "-qm", "other", home=world["home"])
    git(src, "push", "-q", str(world["remote"]), BRANCH, home=world["home"])
    theirs = _remote_ref(world)
    rc, record = _run(_job(world["tmp"]), _agent(world))
    assert record["delivery_status"] == "push_failed"
    assert record["delivery_reason"].startswith("push_failed: ")
    assert _remote_ref(world) == theirs and world["api"]["calls"] == []


def test_a_failed_pull_request_step_keeps_the_pushed_branch(world):
    world["api"]["fail"] = {"GET": 500}
    rc, record = _run(_job(world["tmp"]), _agent(world))
    assert record["delivery_status"] == "pr_failed"
    assert record["delivery_reason"] == ("pr_failed: the open pull requests could not be read "
                                         "[http_500]")
    assert _remote_ref(world) == record["delivery_commit"] and record["pr_number"] is None


def test_no_time_left_means_no_push(world, monkeypatch):
    monkeypatch.setattr(run_job, "DELIVERY_MIN_SECONDS", 10_000.0)
    rc, record = _run(_job(world["tmp"]), _agent(world))
    assert record["delivery_status"] == "no_time"
    assert _remote_ref(world) is None and world["api"]["calls"] == []


def test_a_refused_import_pushes_nothing(world):
    rc, record = _run(_job(world["tmp"]), _agent(world, files={
        ".gitmodules": '[submodule "x"]\n\tpath = x\n\turl = https://example.invalid/x\n'}))
    assert record["delivery_status"] == "not_delivered"
    assert record["delivery_reason"].startswith("import_refused: ")
    assert _remote_ref(world) is None and world["api"]["calls"] == []


# ── Secrets ──────────────────────────────────────────────────────────────────


def test_the_token_and_its_header_are_scrubbed_from_output_and_pr_text(world):
    rc, record = _run(_job(world["tmp"]), _agent(
        world, out=f"leak {TOKEN} and {HEADER}\n".encode(),
        pr_text=f"# Title {TOKEN}\n\nBody {HEADER}\n"))
    assert TOKEN not in record["stdout"] and HEADER not in record["stdout"]
    post = world["api"]["calls"][1][3]
    assert TOKEN not in json.dumps(post) and HEADER not in json.dumps(post)
    # The stand-in agent wrote the values into its own text file on purpose;
    # the record, the request and the cleanup are what this checks.
    _no_secret_anywhere(record, world, disk=False)


def test_the_work_directory_is_removed_when_the_cli_raises(world):
    def runner(*a, **k):
        assert os.listdir(world["work_root"])  # the clone exists while the CLI runs
        raise RuntimeError("stand-in crash")

    with pytest.raises(RuntimeError):
        run_job.main(_job(world["tmp"]), writer=lambda *a: True, runner=runner,
                     hard_exit=lambda code: None)
    assert os.listdir(world["work_root"]) == []


# ── Job validation ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("change", [
    {"branch": "main"}, {"branch": "release/1.2"}, {"branch": "cicada/x"},
    {"branch": "cicada/main-20261015T120000-a1b2c3", "base_branch": "cicada/main-20261015T120000-a1b2c3"},
    {"base_branch": "../main"}, {"author_name": "a\nb"}, {"extra": "x"},
])
def test_an_unacceptable_delivery_is_refused_before_anything_runs(world, change):
    delivery = {**DELIVERY, **change}
    rc, record = _run(_job(world["tmp"], delivery=delivery),
                      lambda *a, **k: pytest.fail("nothing may run"))
    assert rc == run_job.EXIT_JOB_INVALID and record is None
    assert not world["work_root"].exists()


def test_a_delivery_token_without_a_delivery_is_refused(world):
    job_dir = _job(world["tmp"], delivery=None)
    rc, record = _run(job_dir, lambda *a, **k: pytest.fail("nothing may run"))
    assert rc == run_job.EXIT_JOB_INVALID


def test_a_workspace_placeholder_without_a_delivery_is_refused(world):
    job_dir = _job(world["tmp"], delivery=None, secrets={"LANIUS_NEOTOMA_TOKEN": ROLE_TOKEN},
                   argv=["codex", "exec", "--cd", run_job.WORKSPACE_PLACEHOLDER, "-"])
    rc, record = _run(job_dir, lambda *a, **k: pytest.fail("nothing may run"))
    assert rc == run_job.EXIT_JOB_INVALID


def test_a_delivery_without_its_token_is_refused(world):
    job_dir = _job(world["tmp"], secrets={"LANIUS_NEOTOMA_TOKEN": ROLE_TOKEN})
    rc, record = _run(job_dir, lambda *a, **k: pytest.fail("nothing may run"))
    assert rc == run_job.EXIT_JOB_INVALID
