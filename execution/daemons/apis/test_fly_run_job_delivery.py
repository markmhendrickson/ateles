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
import errno
import json
import os
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from fly_cleanup_test_support import assert_cleanup, cleanup_details, cleanup_error

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
    cleanup_errors = []
    real_clear, real_remove = rd._clear, rd.remove_work

    def capture_clear(root):
        try:
            return real_clear(root)
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            cleanup_errors.append(cleanup_error(exc, boundary_error=rd.RepoWorkError))
            raise

    def capture_remove(root):
        try:
            return real_remove(root)
        except rd.RepoWorkError as exc:
            if not cleanup_errors:
                cleanup_errors.append(cleanup_error(exc, boundary_error=rd.RepoWorkError))
            raise

    monkeypatch.setattr(rd, "_clear", capture_clear)
    monkeypatch.setattr(rd, "remove_work", capture_remove)

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
    return {"home": home, "remote": remote, "work_root": work_root, "api": api, "tmp": tmp_path, "cleanup_errors": cleanup_errors}


def _pr(number):
    return {"number": number, "html_url": f"https://github.com/{REPO}/pull/{number}",
            "head": {"ref": BRANCH, "repo": {"full_name": REPO}}, "base": {"ref": "main"}}


def _job(tmp_path, *, delivery=DELIVERY, secrets=None, argv=None, stdin=None, timeout=600):
    job_dir = tmp_path / "job"
    (job_dir / "secrets").mkdir(parents=True)
    secrets = {"LANIUS_NEOTOMA_TOKEN": ROLE_TOKEN, run_job.DELIVERY_TOKEN_NAME: TOKEN} \
        if secrets is None else secrets
    spec = {"run_id": "fly-deliver-1", "role": "lanius", "provider": "codex",
            "repository": REPO, "timeout_seconds": timeout, "credential_names": sorted(secrets),
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
        assert os.path.isabs(marker), "the prompt must carry the absolute text path"
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


def _cleanup_details(world):
    return cleanup_details(world["work_root"], cleanup_errors=world["cleanup_errors"])


def _assert_cleanup(world, *, absent=False, present=False):
    assert_cleanup(world["work_root"], cleanup_errors=world["cleanup_errors"],
                   absent=absent, present=present)


def _no_secret_anywhere(record, world, *, disk=True):
    text = json.dumps(record)
    assert TOKEN not in text and HEADER not in text
    if disk:
        assert not any(world["api"]["disk"])  # never on disk while delivering
    _assert_cleanup(world)


# ── No delivery: unchanged ───────────────────────────────────────────────────


def test_a_job_without_a_delivery_is_unchanged(world):
    job_dir = _job(world["tmp"], delivery=None, secrets={"LANIUS_NEOTOMA_TOKEN": ROLE_TOKEN})
    seen = {}

    def runner(argv, env, stdin, timeout, cwd, ident=None, collect_by=None):
        seen.update(cwd=cwd, argv=argv)
        return 0, b"ok\n", b"", False

    rc, record = _run(job_dir, runner)
    assert rc == 0 and not any(k.startswith(("delivery_", "pr_")) for k in record)
    _assert_cleanup(world, absent=True)
    assert world["api"]["calls"] == []
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


RESERVES = run_job.DELIVERY_RESERVE_SECONDS + run_job.RECORD_RESERVE_SECONDS
APIS_GRACE = 120  # fly_runner.APIS_DEADLINE_GRACE_SECONDS


class _Clock:
    """The time module for one module under test, with a movable offset."""

    def __init__(self):
        import time as real

        self._real, self.offset = real, 0.0

    def time(self):
        return self._real.time() + self.offset

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.mark.parametrize("timeout", [60, 300, 600])
def test_with_ample_headroom_the_cli_gets_exactly_its_timeout(world, monkeypatch, timeout):
    # The deadline Apis sets for a delivering job: timeout + delivery reserve + grace.
    monkeypatch.setenv(run_job.DEADLINE_ENV,
                       _deadline_in(timeout + run_job.DELIVERY_RESERVE_SECONDS + APIS_GRACE))
    seen = {}
    rc, record = _run(_job(world["tmp"], timeout=timeout), _agent(world, seen=seen))
    assert seen["timeout"] == timeout
    assert record["delivery_status"] == "opened"
    assert _remote_ref(world) == record["delivery_commit"]


def test_a_tight_deadline_reduces_the_cli_budget_instead_of_refusing(world, monkeypatch):
    monkeypatch.setenv(run_job.DEADLINE_ENV, _deadline_in(600 + RESERVES - 100))
    seen = {}
    rc, record = _run(_job(world["tmp"], timeout=600), _agent(world, seen=seen))
    assert 495 <= seen["timeout"] <= 500
    assert record["delivery_status"] == "opened"


def test_a_deadline_too_short_for_any_cli_refuses_without_cloning(world, monkeypatch):
    monkeypatch.setenv(run_job.DEADLINE_ENV, _deadline_in(RESERVES + 0.5))
    rc, record = _run(_job(world["tmp"], timeout=600),
                      lambda *a, **k: pytest.fail("the CLI must not run"))
    assert record["delivery_status"] == "no_time"
    _assert_cleanup(world, absent=True)
    assert world["api"]["calls"] == []
    assert _remote_ref(world) is None


def test_a_slow_clone_shrinks_the_cli_budget(world, monkeypatch):
    monkeypatch.setenv(run_job.DEADLINE_ENV, _deadline_in(600 + RESERVES + 50))
    clock = _Clock()
    monkeypatch.setattr(run_job, "time", clock)
    real_clone = rd.clone_origin

    def slow_clone(*a, **k):
        clock.offset += 200  # the clone took 200 s
        return real_clone(*a, **k)

    monkeypatch.setattr(rd, "clone_origin", slow_clone)
    seen = {}
    rc, record = _run(_job(world["tmp"], timeout=600), _agent(world, seen=seen))
    assert 445 <= seen["timeout"] <= 450
    assert record["delivery_status"] == "opened"


# ── An exhausted deadline starts nothing further ─────────────────────────────


def _expire_after(monkeypatch, word):
    """Move repo_delivery's clock past every deadline right after the first
    git call whose argv holds *word*; record every git call started after."""
    clock = _Clock()
    monkeypatch.setattr(rd, "time", clock)
    real = subprocess.run
    after = []

    def spy(args, *a, **k):
        if clock.offset and args[0] == rd.GIT:  # only the runner's own git calls
            after.append(args)
        done = real(args, *a, **k)
        if word in args and not clock.offset:
            clock.offset = 10_000.0
        return done

    monkeypatch.setattr(rd.subprocess, "run", spy)
    return after


def test_a_deadline_that_expires_during_the_import_pushes_nothing(world, monkeypatch):
    after = _expire_after(monkeypatch, "list-heads")
    rc, record = _run(_job(world["tmp"]), _agent(world))
    assert record["delivery_status"] == "no_time"
    assert record["delivery_reason"] == f"no_time: {rd.NO_TIME_REASON}"
    assert after == []  # no git process started after the deadline
    assert world["api"]["calls"] == [] and _remote_ref(world) is None
    _assert_cleanup(world, present=True)


def test_a_deadline_that_expires_after_the_push_opens_no_pull_request(world, monkeypatch):
    after = _expire_after(monkeypatch, "push")
    rc, record = _run(_job(world["tmp"]), _agent(world))
    assert record["delivery_status"] == "no_time"
    assert record["delivery_reason"] == "the branch was pushed; no time was left for the pull request"
    assert _remote_ref(world) == record["delivery_commit"]  # the pushed branch stays visible
    assert "pr_number" not in record and record["pr_url"] == ""
    assert world["api"]["calls"] == [] and after == []


def test_a_deadline_spent_before_the_clone_never_starts_the_cli(world, monkeypatch):
    clock = _Clock()
    clock.offset = 10_000.0
    monkeypatch.setattr(rd, "time", clock)
    rc, record = _run(_job(world["tmp"]), lambda *a, **k: pytest.fail("the CLI must not run"))
    assert record["delivery_status"] == "no_time"
    assert rc == run_job.EXIT_DELIVERY_FAILED and world["api"]["calls"] == []
    _assert_cleanup(world, present=True)


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
    assert _remote_ref(world) == record["delivery_commit"] and "pr_number" not in record


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
    _assert_cleanup(world, present=True)


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
    _assert_cleanup(world, absent=True)


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


def test_the_runner_keeps_the_same_delivery_reserve_the_token_life_assumes():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import fly_runner

    assert run_job.DELIVERY_RESERVE_SECONDS == fly_runner.DELIVERY_RESERVE_SECONDS


def test_the_header_form_of_the_token_is_a_job_secret(world):
    job = run_job.load_job(_job(world["tmp"]))
    assert job.secrets[run_job.DELIVERY_HEADER_SECRET_NAME] == HEADER
    assert run_job.DELIVERY_TOKEN_NAME not in job.child_credential_names


def test_failed_cleanup_diagnostics_are_bound_to_the_real_entrypoint_and_value_free(world, monkeypatch):
    real_remove = rd.shutil.rmtree

    def cannot_remove(path, *args, **kwargs):
        if Path(path) == world["work_root"] / "job-fly-deliver-1":
            (Path(path) / TOKEN).mkdir(exist_ok=True)
            raise OSError(errno.ENOTEMPTY, f"sensitive error {TOKEN}", str(path))
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(rd.shutil, "rmtree", cannot_remove)
    rc, record = _run(_job(world["tmp"]), _agent(world))
    assert rc == 0 and record["delivery_status"] == "opened"
    with pytest.raises((AssertionError, pytest.fail.Exception)) as caught:
        _no_secret_anywhere(record, world)
    message = str(caught.value)
    assert message.startswith("delivery cleanup failed: ")
    details = json.loads(message.removeprefix("delivery cleanup failed: "))
    assert details["errors"] == [{"type": "OSError", "errno": errno.ENOTEMPTY}]
    # prepare_work respects the caller's umask; verify the actual planted mode.
    job_mode = stat.S_IMODE((world["work_root"] / "job-fly-deliver-1").lstat().st_mode)
    assert any(row["relative"] == "job-fly-deliver-1" and row["mode"] == f"{job_mode:04o}"
               and row["kind"] == "directory" for row in details["leftovers"])
    assert any("<entry>" in row["relative"] for row in details["leftovers"])
    assert all(secret not in message for secret in (TOKEN, HEADER, ROLE_TOKEN, str(world["tmp"])))


def test_cleanup_diagnostics_do_not_follow_a_link_at_the_work_root(world):
    outside = world["tmp"] / "outside"
    outside.mkdir()
    (outside / TOKEN).write_text(ROLE_TOKEN)
    world["work_root"].symlink_to(outside, target_is_directory=True)
    details = _cleanup_details(world)
    link_mode = stat.S_IMODE(world["work_root"].lstat().st_mode)
    assert details["leftovers"] == [{"relative": ".", "mode": f"{link_mode:04o}", "kind": "link"}]


# These controls drive the same assertion paths as delivery tests, not only the
# formatter. Synthetic filenames and error text must never reach their output.
def _assert_safe_cleanup_failure(call, world):
    with pytest.raises((OSError, AssertionError, pytest.fail.Exception)) as caught:
        call()
    message = str(caught.value)
    assert message.startswith("delivery cleanup failed: ")
    assert all(value not in message for value in (TOKEN, HEADER, ROLE_TOKEN, str(world["tmp"])))
    return json.loads(message.removeprefix("delivery cleanup failed: "))


@pytest.mark.parametrize("target_state", ["empty", "populated", "dangling"])
def test_cleanup_assertion_never_probes_a_linked_root(world, monkeypatch, target_state):
    outside = world["tmp"] / "outside"
    if target_state != "dangling":
        outside.mkdir()
        if target_state == "populated":
            (outside / TOKEN).write_text(ROLE_TOKEN)
    world["work_root"].symlink_to(outside, target_is_directory=True)
    real_listdir = os.listdir
    probes = []

    def listdir(path):
        if Path(path) == world["work_root"]:
            probes.append(True)
            raise OSError(errno.EACCES, TOKEN, str(path))
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", listdir)
    details = _assert_safe_cleanup_failure(lambda: _no_secret_anywhere({}, world), world)
    assert probes == []
    link_mode = stat.S_IMODE(world["work_root"].lstat().st_mode)
    assert details["leftovers"] == [{"relative": ".", "mode": f"{link_mode:04o}", "kind": "link"}]


@pytest.mark.parametrize("failure_site", ["root_lstat", "listdir", "entry_lstat", "walk", "walk_raise", "scandir"])
def test_cleanup_assertion_sanitizes_probe_and_traversal_errors(world, monkeypatch, failure_site):
    root = world["work_root"]
    root.mkdir()
    (root / TOKEN).touch()
    real_lstat, real_listdir, real_walk, real_scandir = Path.lstat, os.listdir, os.walk, os.scandir
    if failure_site == "scandir":
        (root / TOKEN).unlink()
        (root / TOKEN).mkdir()

    def fail(path):
        raise OSError(errno.EACCES, TOKEN + HEADER + ROLE_TOKEN, str(path))

    def lstat(path, *args, **kwargs):
        if (failure_site == "root_lstat" and path == root or
                failure_site == "entry_lstat" and path == root / TOKEN):
            return fail(path)
        return real_lstat(path, *args, **kwargs)

    def listdir(path):
        if Path(path) == root and failure_site == "listdir":
            return fail(path)
        return real_listdir(path)

    def walk(path, *args, **kwargs):
        if Path(path) == root and failure_site == "walk_raise":
            return fail(root / TOKEN)
        if Path(path) == root and failure_site == "walk":
            error = OSError(errno.EACCES, TOKEN, str(root / HEADER))
            if kwargs.get("onerror"):
                kwargs["onerror"](error)
            return iter(())
        return real_walk(path, *args, **kwargs)

    def scandir(path):
        if Path(path) == root / TOKEN and failure_site == "scandir":
            return fail(path)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(os, "listdir", listdir)
    monkeypatch.setattr(os, "walk", walk)
    details = _assert_safe_cleanup_failure(lambda: _no_secret_anywhere({}, world), world)
    assert details["incomplete"] is True
    errors = details["errors"] + details["leftovers"]
    assert any(row.get("type") == "PermissionError" and row.get("errno") == errno.EACCES
               for row in errors)


@pytest.mark.parametrize("test_name", [
    "test_a_deadline_that_expires_during_the_import_pushes_nothing",
    "test_a_deadline_spent_before_the_clone_never_starts_the_cli",
    "test_the_work_directory_is_removed_when_the_cli_raises",
])
def test_all_delivery_cleanup_assertion_sinks_use_safe_evidence(world, monkeypatch, test_name):
    real_main = run_job.main

    def leave_after_main(*args, **kwargs):
        try:
            return real_main(*args, **kwargs)
        finally:
            world["work_root"].mkdir(exist_ok=True)
            (world["work_root"] / TOKEN).touch()

    monkeypatch.setattr(run_job, "main", leave_after_main)
    test = globals()[test_name]
    def call():
        if "monkeypatch" in test.__code__.co_varnames[:test.__code__.co_argcount]:
            test(world, monkeypatch)
        else:
            test(world)
    details = _assert_safe_cleanup_failure(call, world)
    planted_mode = stat.S_IMODE((world["work_root"] / TOKEN).lstat().st_mode)
    assert details["leftovers"] == [{"relative": "<entry>", "mode": f"{planted_mode:04o}", "kind": "file"}]


@pytest.mark.parametrize("count", [64, 65, 200])
def test_cleanup_assertion_bounds_and_redacts_more_than_64_rows(world, count):
    root = world["work_root"]
    root.mkdir()
    for i in range(count):
        (root / f"{TOKEN}-{i:03}").touch()
    details = _assert_safe_cleanup_failure(lambda: _no_secret_anywhere({}, world), world)
    assert len(details["leftovers"]) == 64
    assert all(row["relative"] == "<entry>" for row in details["leftovers"])
    assert details["truncated"] is (count > 64)


def test_cleanup_assertion_preserves_missing_empty_and_absent_root_contracts(world):
    _assert_cleanup(world, absent=True)
    missing = _assert_safe_cleanup_failure(lambda: _assert_cleanup(world, present=True), world)
    assert missing["root"] is None
    world["work_root"].mkdir()
    _assert_cleanup(world)
    details = _assert_safe_cleanup_failure(lambda: _assert_cleanup(world, absent=True), world)
    assert details["root"]["kind"] == "directory" and details["leftovers"] == []


def test_cleanup_assertion_records_nested_links_without_reading_targets(world, monkeypatch):
    root = world["work_root"]
    root.mkdir()
    outside = world["tmp"] / TOKEN
    outside.mkdir()
    (outside / HEADER).write_text(ROLE_TOKEN)
    (root / "checkout").symlink_to(outside, target_is_directory=True)
    real_scandir = os.scandir
    def scandir(path):
        if Path(path) in (outside, root / "checkout"):
            pytest.fail("nested target was traversed")
        return real_scandir(path)
    monkeypatch.setattr(os, "scandir", scandir)
    details = _assert_safe_cleanup_failure(lambda: _no_secret_anywhere({}, world), world)
    assert len(details["leftovers"]) == 1 and details["leftovers"][0]["kind"] == "link"
    assert details["incomplete"] is False and details["truncated"] is False
