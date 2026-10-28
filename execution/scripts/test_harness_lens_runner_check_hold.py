"""The bootstrap lens runner seats no lens on a head whose checks are not green.

`harness_lens_runner.run_one` shares `panel_check_gate` with the Apis review
panel. A red, pending or unreadable head is refused BEFORE any worktree,
sandbox or model call exists; a green head proceeds to the worktree step.

Run: pytest execution/scripts/test_harness_lens_runner_check_hold.py -v
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _SCRIPTS_DIR.parent.parent
_DAEMON_DIR = _REPO_ROOT / "execution" / "daemons" / "apis"
for _p in (str(_REPO_ROOT), str(_DAEMON_DIR), str(_SCRIPTS_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import harness_lens_runner as hlr  # noqa: E402
import panel_check_gate as pcg  # noqa: E402

HEAD = "a" * 40
REPO = "owner/repo"


def _outcome(name, state, passed):
    return type(
        "O", (), {"name": name, "state": state, "passed": passed, "superseded": False}
    )()


RED = pcg.describe_gate(HEAD, [_outcome("pytest", "failing (failure)", False)], False)
PENDING = pcg.describe_gate(HEAD, [_outcome("pytest", "pending (queued)", False)], False)
GREEN = pcg.describe_gate(HEAD, [_outcome("pytest", "success", True)], True)
UNREADABLE = pcg.unreadable_gate(HEAD, "HTTP 502")


class _Reached(Exception):
    """Raised by the stubbed Worktree.create: the runner got past the hold."""


@pytest.fixture
def runner(monkeypatch, tmp_path):
    state = {
        "gate": RED, "worktrees": 0, "sandboxes": 0, "dispatches": 0,
        "pr": {"state": "open", "head": {"sha": HEAD}, "base": {"ref": "main"}},
    }

    async def read_gate(repository, head_sha, base_ref):
        state["asked"] = (repository, head_sha, base_ref)
        return state["gate"]

    def metadata(*, repo, pr):
        state["metadata_asked"] = (repo, pr)
        return state["pr"]

    def create(self, *, head):
        state["worktrees"] += 1
        raise _Reached()

    def sandbox(*args, **kwargs):
        state["sandboxes"] += 1
        raise _Reached()

    async def dispatch(*args, **kwargs):
        state["dispatches"] += 1
        raise _Reached()

    monkeypatch.setattr(hlr.panel_check_gate, "read_check_gate", read_gate)
    # Keep the old edge readable for the red-before proof: an implementation
    # that reads only the base and historical checks must still fail these tests.
    monkeypatch.setattr(hlr, "current_pr_base_ref", lambda **kw: "main", raising=False)
    monkeypatch.setattr(hlr, "current_pr_metadata", metadata, raising=False)
    monkeypatch.setattr(hlr, "check_headroom", lambda provider: None)
    monkeypatch.setattr(hlr.Worktree, "create", create)
    monkeypatch.setattr(hlr.HarnessSandbox, "build", sandbox)
    monkeypatch.setattr(hlr.dispatch_role, "dispatch", dispatch)
    brief = tmp_path / "brief.md"
    brief.write_text("brief\n", encoding="utf-8")
    target = hlr.LensTarget(repo=REPO, pr=7, head=HEAD, lens="pm", agent="pavo")

    def go(dry_run=False, compare=False):
        kwargs = dict(
            dry_run=dry_run, post=False, repo_worktree_name="repo",
            scratch_root=tmp_path, brief_path=brief, timeout=None,
        )
        call = (
            hlr.run_compare(target, providers=["claude", "codex"], **kwargs)
            if compare else hlr.run_one(target, provider="claude", **kwargs)
        )
        return asyncio.run(call)

    return state, go


@pytest.mark.parametrize("gate", [RED, PENDING, UNREADABLE], ids=["red", "pending", "unreadable"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_a_head_that_is_not_green_is_refused_before_any_worktree(runner, gate, dry_run):
    state, go = runner
    state["gate"] = gate
    report = go(dry_run=dry_run)
    assert report["ok"] is False
    assert report["held_for_checks"] is True
    assert report["no_model_call_made"] is True and report["posted"] is False
    assert HEAD[:9] in report["reason"]
    assert "held until the head's checks are green" in report["reason"]
    assert state["worktrees"] == 0, "no worktree may exist for a held lens"
    assert state["asked"] == (REPO, HEAD, "main")


def test_a_green_head_proceeds_to_the_worktree(runner):
    state, go = runner
    state["gate"] = GREEN
    with pytest.raises(_Reached):
        go()
    assert state["worktrees"] == 1


@pytest.mark.parametrize("metadata", [
    None, [], {},
    {"head": {"sha": HEAD}, "base": {"ref": "main"}},
    *({"state": state, "head": {"sha": HEAD}, "base": {"ref": "main"}}
      for state in (None, "closed", "merged", "unknown", "OPEN", " open ", 1)),
    *({"state": "open", "head": head, "base": {"ref": "main"}}
      for head in (None, [], {}, {"sha": None}, {"sha": 1}, {"sha": "a" * 39},
                   {"sha": "b" * 40}, {"sha": HEAD + "."}, {"sha": "%61" * 40})),
    *({"state": "open", "head": {"sha": HEAD}, "base": base}
      for base in (None, [], {}, {"ref": None}, {"ref": 1}, {"ref": ""})),
])
@pytest.mark.parametrize("mode", ["run", "dry_run", "compare"])
def test_green_historical_checks_cannot_release_invalid_live_pr(runner, metadata, mode):
    state, go = runner
    state.update(pr=metadata, gate=GREEN)
    report = go(dry_run=mode == "dry_run", compare=mode == "compare")
    reports = report["results"].values() if mode == "compare" else [report]
    for held in reports:
        assert held["ok"] is False and held["held_for_checks"] is True
        assert held["no_model_call_made"] is True and held["posted"] is False
        assert "rerun" in held["reason"].lower()
    assert state["worktrees"] == state["sandboxes"] == state["dispatches"] == 0
    assert "asked" not in state, "no historical check read may substitute for a live PR"


def test_live_head_uses_the_existing_sha_normalizer(runner):
    state, go = runner
    state["gate"] = GREEN
    state["pr"]["head"]["sha"] = "  " + HEAD.upper() + "  "
    with pytest.raises(_Reached):
        go()
    assert state["asked"] == (REPO, HEAD, "main")


def test_the_real_hold_names_failing_checks(runner):
    state, _ = runner
    target = hlr.LensTarget(repo=REPO, pr=7, head=HEAD, lens="pm", agent="pavo")
    state["gate"] = RED
    reason = asyncio.run(hlr.head_checks_hold(target))
    assert reason and "pytest" in reason
    state["gate"] = GREEN
    assert asyncio.run(hlr.head_checks_hold(target)) is None


@pytest.mark.parametrize("compare", [False, True])
def test_cli_keeps_the_natural_live_head_hold(runner, capsys, compare):
    state, _ = runner
    state.update(gate=GREEN, pr={"state": "closed", "head": {"sha": HEAD}})
    mode = ["--compare", "claude,codex"] if compare else ["--provider", "claude"]
    hlr.main(["--repo", REPO, "--pr", "7", "--head", HEAD,
              "--lens", "pm", "--agent", "pavo", "--brief", "fixture.md",
              "--json", *mode])
    report = json.loads(capsys.readouterr().out)
    reports = report["results"].values() if compare else [report]
    assert all(item["held_for_checks"] and item["no_model_call_made"] for item in reports)
    assert state["worktrees"] == state["sandboxes"] == state["dispatches"] == 0


@pytest.mark.parametrize("returncode,stdout", [
    (1, '{"state":"open"}'), (0, ""), (0, "not json"),
    (0, "null"), (0, "[]"), (0, "3"),
])
def test_live_pr_reader_rejects_failed_or_malformed_output(monkeypatch, returncode, stdout):
    env = {"fixture": "repository scoped"}
    monkeypatch.setattr(hlr, "repo_scoped_env", lambda repo: env)

    def read(command, **kwargs):
        assert command[:3] == ["gh", "api", f"repos/{REPO}/pulls/7"]
        assert kwargs["env"] is env and kwargs["timeout"] == 30
        return SimpleNamespace(returncode=returncode, stdout=stdout)

    monkeypatch.setattr(hlr.subprocess, "run", read)
    assert hlr.current_pr_metadata(repo=REPO, pr=7) is None


@pytest.mark.parametrize("error", [OSError("unavailable"),
    subprocess.TimeoutExpired("gh", 30), hlr.RepoAccessError("unreadable")])
def test_live_pr_reader_converts_read_failures_to_a_hold_input(monkeypatch, error):
    def unavailable(repo):
        raise error

    monkeypatch.setattr(hlr, "repo_scoped_env", unavailable)
    assert hlr.current_pr_metadata(repo=REPO, pr=7) is None


def test_live_pr_reader_returns_one_snapshot(monkeypatch):
    metadata = {"state": "open", "head": {"sha": HEAD}, "base": {"ref": "main"}}
    monkeypatch.setattr(hlr, "repo_scoped_env", lambda repo: {})
    monkeypatch.setattr(hlr.subprocess, "run", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout=json.dumps(metadata)))
    assert hlr.current_pr_metadata(repo=REPO, pr=7) == metadata
