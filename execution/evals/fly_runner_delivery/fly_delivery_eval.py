"""Agentic eval: a Fly job that delivers a branch and a pull request.

Each scenario drives the REAL job entrypoint (``deploy/fly-runner/run_job.py``
``main``) with a stand-in CLI that acts like an agent in its checkout, a local
bare repository in GitHub's place and a stand-in GitHub API. The trace keeps
the result record AND the external effects: every ref on the remote before
and after, every API call, whether the CLI ran, whether the job's token or
its header ever reached an output, the record or a file on disk, and whether
the work directory is gone. Offline: placeholder token, no network.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
_REPO = HERE.parents[2]
_RUNNER_DIR = _REPO / "deploy" / "fly-runner"


def _load_run_job():
    spec = importlib.util.spec_from_file_location("fly_run_job_eval", _RUNNER_DIR / "run_job.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["fly_run_job_eval"] = module
    spec.loader.exec_module(module)
    return module


run_job = _load_run_job()
rd = run_job._repo_delivery()

TOKEN = "ghs_eval_placeholder_delivery_token_4242"
HEADER = rd.auth_config(TOKEN)[0][1]
ROLE_TOKEN = "eval-lanius-own-neotoma-token"
REPO = "owner/repo"
BRANCH = "cicada/eval-delivery-20261016T090000-abc123"
DELIVERY = {"branch": BRANCH, "base_branch": "main", "author_name": "cicada",
            "author_email": "cicada@agents.example.invalid"}


def load_scenarios() -> list[dict]:
    return json.loads((HERE / "fixtures" / "scenarios.json").read_text())["scenarios"]


@dataclass
class Trace:
    rc: int
    record: dict | None
    cli_ran: bool
    remote_before: dict
    remote_after: dict
    api_calls: list = field(default_factory=list)
    secret_on_disk: bool = False
    output_text: str = ""
    work_left: list = field(default_factory=list)


def _genv(home: Path) -> dict:
    return {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t", "LC_ALL": "C"}


def _git(cwd, *args, home):
    done = subprocess.run(["git", *args], cwd=cwd, env=_genv(home), capture_output=True)
    if done.returncode != 0:
        raise RuntimeError(f"eval setup git {args[0]} failed")
    return done.stdout.decode().strip()


def _refs(remote: Path, home: Path) -> dict:
    out = _git(remote, "for-each-ref", "--format=%(refname) %(objectname)", home=home)
    return dict(line.split(" ", 1) for line in out.splitlines() if line)


def _pr(number: int) -> dict:
    return {"number": number, "html_url": f"https://github.com/{REPO}/pull/{number}",
            "head": {"ref": BRANCH, "repo": {"full_name": REPO}}, "base": {"ref": "main"}}


def run_scenario(scenario: dict, tmp_path: Path, monkeypatch) -> Trace:
    home = tmp_path / "home"
    home.mkdir()
    src = tmp_path / "src"
    _git(tmp_path, "init", "-q", "-b", "main", str(src), home=home)
    (src / "README").write_text("base\n")
    _git(src, "add", ".", home=home)
    _git(src, "commit", "-qm", "base", home=home)
    _git(src, "tag", "v1.0.0", home=home)
    remote = tmp_path / "remote.git"
    _git(tmp_path, "clone", "-q", "--bare", str(src), str(remote), home=home)
    if scenario.get("remote_branch_exists"):
        _git(src, "checkout", "-q", "-b", BRANCH, home=home)
        (src / "theirs.txt").write_text("someone else's\n")
        _git(src, "add", ".", home=home)
        _git(src, "commit", "-qm", "theirs", home=home)
        _git(src, "push", "-q", str(remote), BRANCH, home=home)

    work_root = tmp_path / "work"
    monkeypatch.setattr(run_job, "WORK_ROOT", work_root)
    remote_url = f"file://{tmp_path / 'missing.git'}" if scenario.get("clone_fails") \
        else f"file://{remote}"
    monkeypatch.setattr(rd, "GITHUB_REMOTE", remote_url)
    monkeypatch.setattr(rd, "GITHUB_REMOTE_TRANSPORT", "file")
    monkeypatch.setattr(run_job, "probe_paths_default", lambda: (str(tmp_path / "none.env"),))
    deadline = datetime.fromtimestamp(time.time() + 900, tz=timezone.utc).isoformat()
    monkeypatch.setenv(run_job.DEADLINE_ENV, deadline)
    if scenario.get("no_time"):
        monkeypatch.setattr(run_job, "DELIVERY_MIN_SECONDS", 10_000.0)

    calls: list = []
    disk_hits: list[bool] = []
    api = scenario.get("api", {})

    def fake_api(method, path, token, body=None):
        calls.append({"method": method, "path": path, "body": body,
                      "token_ok": token == TOKEN})
        disk_hits.append(_secret_on_disk(work_root))
        if method in api.get("fail", {}):
            return api["fail"][method], None
        if method == "GET":
            return 200, [_pr(n) for n in api.get("open", [])]
        number = 7 if method == "POST" else int(path.rsplit("/", 1)[1])
        return (201 if method == "POST" else 200), _pr(number)

    monkeypatch.setattr(rd, "_api", fake_api)

    agent = scenario.get("agent", {})
    ran = []

    def stand_in_cli(argv, env, stdin, timeout, cwd, ident=None, collect_by=None):
        ran.append(True)
        disk_hits.append(_secret_on_disk(work_root))
        for i in range(agent.get("commits", 1)):
            for name, content in agent.get("files", {f"change-{i}.txt": "work\n"}).items():
                Path(cwd, name).write_text(content)
            _git(cwd, "add", "-A", home=home)
            _git(cwd, "commit", "-qm", f"agent {i}", home=home)
        if agent.get("pr_text") is not None and b"@@" not in stdin:
            target = stdin.split(b"to ", 1)[1].rstrip(b".").decode()
            if os.path.isabs(target):
                Path(target).write_text(agent["pr_text"])
        out = agent.get("stdout", "done\n").replace("{TOKEN}", TOKEN).replace("{HEADER}", HEADER)
        return agent.get("rc", 0), out.encode(), b"", False

    job_dir = tmp_path / "job"
    (job_dir / "secrets").mkdir(parents=True)
    delivering = scenario.get("delivery", True)
    secrets = {"LANIUS_NEOTOMA_TOKEN": ROLE_TOKEN}
    if delivering:
        secrets[run_job.DELIVERY_TOKEN_NAME] = TOKEN
    spec = {"run_id": "fly-eval-delivery", "role": "lanius", "provider": "codex",
            "repository": REPO, "timeout_seconds": 300, "credential_names": sorted(secrets),
            "child_credential_names": [], "neotoma_token_name": "LANIUS_NEOTOMA_TOKEN",
            "neotoma_jwk_name": "", "neotoma_base_url": "https://neotoma.eval.invalid",
            "probe_env_names": [], "probe_paths": [], "child_user": ""}
    if delivering:
        spec["delivery"] = DELIVERY
    (job_dir / "job.json").write_text(json.dumps(spec))
    argv = ["codex", "exec", "--cd", run_job.WORKSPACE_PLACEHOLDER, "-"] if delivering \
        else ["codex", "exec", "-"]
    (job_dir / "argv.json").write_text(json.dumps(argv))
    (job_dir / "stdin").write_bytes(b"Do the task. Write the PR text to @@ATELES_JOB_PR_FILE@@.")
    for name, value in secrets.items():
        (job_dir / "secrets" / name).write_text(value)

    before = _refs(remote, home)
    stored: dict = {}
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = run_job.main(job_dir, writer=lambda b, a, r: stored.setdefault("r", r) is not None,
                          runner=stand_in_cli, hard_exit=lambda code: None)
    return Trace(
        rc=rc, record=stored.get("r"), cli_ran=bool(ran), remote_before=before,
        remote_after=_refs(remote, home), api_calls=calls,
        secret_on_disk=any(disk_hits), output_text=out.getvalue(),
        work_left=sorted(os.listdir(work_root)) if work_root.exists() else [],
    )


def _secret_on_disk(root: Path) -> bool:
    if not root.exists():
        return False
    for dirpath, _, files in os.walk(root):
        for name in files:
            try:
                data = Path(dirpath, name).read_bytes()
            except OSError:
                continue
            if TOKEN.encode() in data or HEADER.encode() in data:
                return True
    return False


def judge(scenario: dict, trace: Trace) -> list[str]:
    e = scenario["expect"]
    problems: list[str] = []
    record = trace.record or {}
    if "rc" in e and trace.rc != e["rc"]:
        problems.append(f"exit {trace.rc} != {e['rc']}")
    if "cli_ran" in e and trace.cli_ran != e["cli_ran"]:
        problems.append("the CLI ran" if trace.cli_ran else "the CLI did not run")
    if "status" in e and record.get("delivery_status") != e["status"]:
        problems.append(f"delivery_status {record.get('delivery_status')!r} != {e['status']!r}")
    if "reason_starts" in e and not str(record.get("delivery_reason", "")).startswith(e["reason_starts"]):
        problems.append("delivery_reason does not name the expected step")
    if e.get("no_delivery_fields") and any(k.startswith(("delivery_", "pr_")) for k in record):
        problems.append("a job without a delivery gained delivery fields")
    pushed = trace.remote_after.get(f"refs/heads/{BRANCH}")
    before = trace.remote_before.get(f"refs/heads/{BRANCH}")
    if e.get("pushed"):
        if not pushed or pushed != record.get("delivery_commit"):
            problems.append("the delivered commit is not the remote branch head")
    elif pushed != before:
        problems.append("the remote branch changed although nothing should have been pushed")
    if "api" in e and [c["method"] for c in trace.api_calls] != e["api"]:
        problems.append(f"API calls {[c['method'] for c in trace.api_calls]} != {e['api']}")
    if "pr_number" in e:
        if e["pr_number"] is None and "pr_number" in record:
            problems.append("a pr_number was recorded although no pull request exists")
        if e["pr_number"] is not None and record.get("pr_number") != e["pr_number"]:
            problems.append(f"pr_number {record.get('pr_number')} != {e['pr_number']}")
    # Always: the default branch and the tag are untouched, nothing but the
    # job's branch changed, no secret anywhere, and the work directory gone.
    for ref in ("refs/heads/main", "refs/tags/v1.0.0"):
        if trace.remote_after.get(ref) != trace.remote_before.get(ref):
            problems.append(f"{ref} changed on the remote")
    other = {r for r in set(trace.remote_before) | set(trace.remote_after)
             if r != f"refs/heads/{BRANCH}"
             and trace.remote_before.get(r) != trace.remote_after.get(r)}
    if other:
        problems.append(f"other remote refs changed: {sorted(other)}")
    everywhere = json.dumps(record) + trace.output_text + json.dumps(
        [c["body"] for c in trace.api_calls])
    if TOKEN in everywhere or HEADER in everywhere:
        problems.append("the token or its header reached an output, the record or a request")
    if trace.secret_on_disk:
        problems.append("the token or its header was found on disk")
    if any(not c["token_ok"] for c in trace.api_calls):
        problems.append("an API call did not use the job's token")
    if trace.work_left:
        problems.append("the work directory was left behind")
    return problems
