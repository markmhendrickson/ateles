"""Tests for deterministic-first mechanical work (mechanical_first.py) and its
dispatch_role wiring.

Every git test runs against a real repository in tmp_path: the point of the
deterministic path is that git, not a model, decides the outcome, so a mock of
git would test nothing. The dispatch tests replace only ``run_skill`` (the model
path) and assert whether it was reached.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

_DAEMON_DIR = Path(__file__).resolve().parent
if str(_DAEMON_DIR) not in sys.path:
    sys.path.insert(0, str(_DAEMON_DIR))

import dispatch_role  # noqa: E402
import mechanical_first as mf  # noqa: E402
from skill_runner import SkillResult  # noqa: E402

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test",
    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
}


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch):
    for key, value in _GIT_ENV.items():
        monkeypatch.setenv(key, value)
    # Hooks and signing belong to the developer's global config, not this test.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "commit.gpgsign")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    monkeypatch.setenv("GIT_CONFIG_KEY_1", "init.defaultBranch")
    monkeypatch.setenv("GIT_CONFIG_VALUE_1", "main")


def git(cwd, *args) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


def commit(cwd: Path, name: str, text: str, message: str) -> None:
    (cwd / name).write_text(text, encoding="utf-8")
    git(cwd, "add", name)
    git(cwd, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path):
    """main has advanced past the point where `feature` branched."""
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    commit(root, "shared.txt", "line1\nline2\nline3\n", "base")
    git(root, "checkout", "-q", "-b", "feature")
    commit(root, "feature.txt", "feature work\n", "feature change")
    git(root, "checkout", "-q", "main")
    commit(root, "other.txt", "main work\n", "main change")
    git(root, "checkout", "-q", "feature")
    return root


def add_conflict(repo: Path) -> None:
    """main and feature both rewrite shared.txt line2 differently."""
    git(repo, "checkout", "-q", "main")
    commit(repo, "shared.txt", "line1\nMAIN line2\nline3\n", "main edits shared")
    git(repo, "checkout", "-q", "feature")
    commit(repo, "shared.txt", "line1\nFEATURE line2\nline3\n", "feature edits shared")


# ── clean integration: no model ─────────────────────────────────────────────


def test_clean_rebase_finishes_and_verifies_in_git(repo):
    outcome = mf.attempt_integration(str(repo), "main", mf.MODE_REBASE)
    assert outcome.status == mf.DONE, outcome.summary
    assert mf.verify_integration(str(repo), outcome.base_sha) is None
    # feature now sits on top of main, and history is linear.
    assert git(repo, "rev-parse", "HEAD~1") == git(repo, "rev-parse", "main")
    assert (repo / "other.txt").exists() and (repo / "feature.txt").exists()


def test_branch_that_already_contains_the_base_is_a_no_op(repo):
    mf.attempt_integration(str(repo), "main")
    head = git(repo, "rev-parse", "HEAD")
    outcome = mf.attempt_integration(str(repo), "main")
    assert outcome.status == mf.DONE and "already contains" in outcome.summary
    assert git(repo, "rev-parse", "HEAD") == head


def test_merge_mode_keeps_the_pushed_history_and_adds_a_merge_commit(repo):
    before = git(repo, "rev-parse", "HEAD")
    outcome = mf.attempt_integration(str(repo), "main", mf.MODE_MERGE)
    assert outcome.status == mf.DONE, outcome.summary
    parents = git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()[1:]
    assert len(parents) == 2, "expected a merge commit"
    # The published commit is untouched: an ancestor, not rewritten.
    assert git(repo, "merge-base", "--is-ancestor", before, "HEAD") == ""
    assert mf.verify_integration(str(repo), outcome.base_sha, mf.MODE_MERGE) is None


def test_rebase_rewrites_history_where_merge_does_not(repo):
    """Why the merge mode exists: the rebase changes the branch's own commit."""
    before = git(repo, "rev-parse", "HEAD")
    mf.attempt_integration(str(repo), "main", mf.MODE_REBASE)
    assert subprocess.run(
        ["git", "merge-base", "--is-ancestor", before, "HEAD"], cwd=repo
    ).returncode != 0


# ── refusals change nothing ─────────────────────────────────────────────────


@pytest.mark.parametrize("mutate,needle", [
    (lambda r: (r / "feature.txt").write_text("dirty\n"), "uncommitted tracked changes"),
    (lambda r: git(r, "checkout", "-q", "--detach"), "detached"),
])
def test_unsafe_worktrees_are_refused_untouched(repo, mutate, needle):
    mutate(repo)
    head = git(repo, "rev-parse", "HEAD")
    outcome = mf.attempt_integration(str(repo), "main")
    assert outcome.status == mf.REFUSED and needle in outcome.summary
    assert git(repo, "rev-parse", "HEAD") == head


@pytest.mark.parametrize("base", ["no-such-ref", "--upload-pack=x", ""])
def test_bad_base_refs_are_refused(repo, base):
    assert mf.attempt_integration(str(repo), base).status == mf.REFUSED


def test_a_worktree_mid_rebase_is_refused(repo):
    add_conflict(repo)
    assert mf.attempt_integration(str(repo), "main").status == mf.CONFLICTS
    again = mf.attempt_integration(str(repo), "main")
    assert again.status == mf.REFUSED and "already in progress" in again.summary


def test_a_non_repository_is_refused(tmp_path):
    assert mf.attempt_integration(str(tmp_path), "main").status == mf.REFUSED


# ── conflicts: scoped brief, verification, abort ────────────────────────────


def test_conflicts_leave_the_operation_open_with_a_brief_scoped_to_the_hunks(repo):
    add_conflict(repo)
    outcome = mf.attempt_integration(str(repo), "main")
    assert outcome.status == mf.CONFLICTS
    assert outcome.conflicted_files == ["shared.txt"]
    assert mf.operation_in_progress(str(repo)) == "rebase"
    brief = outcome.brief
    assert "shared.txt" in brief and "MAIN line2" in brief and "FEATURE line2" in brief
    # Scoped: files git integrated cleanly are not in the brief at all.
    assert "feature work" not in brief and "main work" not in brief
    assert "rebase --continue" in brief and "IN PROGRESS" in brief


def test_merge_conflicts_brief_names_the_merge_finish_command(repo):
    add_conflict(repo)
    outcome = mf.attempt_integration(str(repo), "main", mf.MODE_MERGE)
    assert outcome.status == mf.CONFLICTS
    assert mf.operation_in_progress(str(repo)) == "merge"
    assert "git commit --no-edit" in outcome.brief


def test_brief_is_bounded_by_its_budget(repo):
    git(repo, "checkout", "-q", "main")
    commit(repo, "big.txt", "".join(f"main {i}\n" for i in range(400)), "main big")
    git(repo, "checkout", "-q", "feature")
    commit(repo, "big.txt", "".join(f"feat {i}\n" for i in range(400)), "feature big")
    outcome = mf.attempt_integration(str(repo), "main", brief_budget=1500)
    assert outcome.status == mf.CONFLICTS
    assert len(outcome.brief) < 1500 + 1500, "brief must not carry the whole file"
    assert "not shown" in outcome.brief or "main 399" not in outcome.brief


def test_verification_catches_unfinished_and_unresolved_integrations(repo):
    add_conflict(repo)
    outcome = mf.attempt_integration(str(repo), "main")
    problem = mf.verify_integration(str(repo), outcome.base_sha)
    assert problem and ("in progress" in problem or "unmerged" in problem)
    # Resolve for real and continue: only now does it verify.
    (repo / "shared.txt").write_text("line1\nBOTH line2\nline3\n")
    git(repo, "add", "shared.txt")
    proc = subprocess.run(
        ["git", "rebase", "--continue"], cwd=repo, capture_output=True, text=True,
        env={**__import__("os").environ, "GIT_EDITOR": "true"},
    )
    assert proc.returncode == 0, proc.stderr
    assert mf.verify_integration(str(repo), outcome.base_sha) is None


def test_verification_fails_when_head_lacks_the_base(repo):
    base_sha = git(repo, "rev-parse", "main")
    assert "does not contain the base" in (mf.verify_integration(str(repo), base_sha) or "")


def test_abort_restores_the_worktree_as_found(repo):
    add_conflict(repo)
    head = git(repo, "rev-parse", "HEAD")
    assert mf.attempt_integration(str(repo), "main").status == mf.CONFLICTS
    assert mf.abort_integration(str(repo)) == "rebase"
    assert git(repo, "rev-parse", "HEAD") == head
    assert mf.operation_in_progress(str(repo)) == "" and git(repo, "status", "--porcelain") == ""


# ── generators ──────────────────────────────────────────────────────────────


def test_generator_runs_directly_and_lists_changed_files(repo):
    script = "import pathlib; pathlib.Path('gen.out').write_text('x')"
    outcome = mf.run_generators(str(repo), [f"{sys.executable} -c \"{script}\""])
    assert outcome.status == mf.DONE and outcome.changed_files == ["gen.out"]
    assert (repo / "gen.out").read_text() == "x"


def test_generators_run_in_order_and_stop_at_the_first_failure_verbatim(repo):
    fail = "import sys; print('boom detail'); sys.exit(3)"
    ok = "import pathlib; pathlib.Path('never.out').write_text('x')"
    outcome = mf.run_generators(
        str(repo), [f"{sys.executable} -c \"{fail}\"", f"{sys.executable} -c \"{ok}\""]
    )
    assert outcome.status == mf.ERROR
    assert "exited 3" in outcome.summary and "boom detail" in outcome.summary
    assert not (repo / "never.out").exists(), "later generators must not run after a failure"


def test_generators_are_not_run_through_a_shell(repo):
    outcome = mf.run_generators(str(repo), ["echo hi; touch injected.out"])
    assert not (repo / "injected.out").exists()
    assert outcome.status == mf.DONE  # echo received the literal arguments


@pytest.mark.parametrize("command", ["git stash", "/usr/bin/git stash push", "git commit --no-verify -m x", ""])
def test_forbidden_or_empty_generator_commands_are_refused(repo, command):
    assert mf.run_generators(str(repo), [command]).status == mf.REFUSED


def test_missing_generator_is_an_error_not_a_crash(repo):
    outcome = mf.run_generators(str(repo), ["definitely-not-a-real-generator-binary"])
    assert outcome.status == mf.ERROR and "not found" in outcome.summary


# ── dispatch_role: which outcomes reach a model ─────────────────────────────


@pytest.fixture
def no_events(monkeypatch):
    events: list[dict] = []
    import skill_runner
    monkeypatch.setattr(skill_runner, "_write_harness_event", lambda **kw: events.append(kw))
    return events


def _model_must_not_run(monkeypatch):
    calls: list = []

    async def boom(skill, prompt, **kwargs):
        calls.append((skill, prompt))
        raise AssertionError("a model must not be called for a clean integration")

    monkeypatch.setattr(dispatch_role, "run_skill", boom)
    return calls


def test_clean_rebase_dispatch_makes_no_model_call(repo, monkeypatch, no_events):
    calls = _model_must_not_run(monkeypatch)
    result = asyncio.run(dispatch_role.dispatch(
        "cicada", "rebase onto main", cwd=str(repo), work_class="rebase", integration_base="main"))
    assert result.ok and result.provider == "deterministic" and calls == []
    assert result.attempted_providers == ("deterministic",)
    assert "finished cleanly" in result.stdout
    assert git(repo, "rev-parse", "HEAD~1") == git(repo, "rev-parse", "main")
    # The no-model run is recorded, so it is visible rather than silent.
    event, = no_events
    assert event["tool_name"] == "deterministic:rebase" and event["success"] == "true"
    assert event["output_summary"].startswith("no model call")


def test_merge_dispatch_makes_a_merge_commit_without_a_model(repo, monkeypatch, no_events):
    calls = _model_must_not_run(monkeypatch)
    result = asyncio.run(dispatch_role.dispatch(
        "cicada", "merge main in", cwd=str(repo), work_class="rebase",
        integration_base="main", integration_mode="merge"))
    assert result.ok and calls == []
    assert len(git(repo, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3


def test_refused_integration_fails_without_a_model(repo, monkeypatch, no_events):
    calls = _model_must_not_run(monkeypatch)
    (repo / "feature.txt").write_text("dirty\n")
    result = asyncio.run(dispatch_role.dispatch(
        "cicada", "x", cwd=str(repo), work_class="rebase", integration_base="main"))
    assert not result.ok and calls == [] and "refused" in result.error


def _resolving_model(repo: Path, seen: list):
    async def model(skill, prompt, **kwargs):
        seen.append((prompt, kwargs))
        (repo / "shared.txt").write_text("line1\nBOTH line2\nline3\n")
        git(repo, "add", "shared.txt")
        subprocess.run(["git", "rebase", "--continue"], cwd=repo, capture_output=True,
                       env={**__import__("os").environ, "GIT_EDITOR": "true"})
        return SkillResult(skill, True, 0, "done", "", provider="claude-local",
                           attempted_providers=("claude-local",))
    return model


def test_conflicts_invoke_a_model_with_only_the_conflicted_hunks(repo, monkeypatch, no_events):
    add_conflict(repo)
    seen: list = []
    monkeypatch.setattr(dispatch_role, "run_skill", _resolving_model(repo, seen))
    result = asyncio.run(dispatch_role.dispatch(
        "cicada", "please rebase", cwd=str(repo), work_class="rebase", integration_base="main"))
    assert result.ok, result.error
    assert result.attempted_providers == ("deterministic", "claude-local")
    (prompt, kwargs), = seen
    assert "MAIN line2" in prompt and "FEATURE line2" in prompt
    assert "feature work" not in prompt and "main work" not in prompt
    assert kwargs["work_class"] == "rebase" and kwargs["cwd"] == str(repo)
    assert "please rebase" in prompt  # caller's task kept as context
    assert mf.operation_in_progress(str(repo)) == ""


def test_a_model_that_claims_success_without_finishing_is_not_believed(repo, monkeypatch, no_events):
    add_conflict(repo)
    head = git(repo, "rev-parse", "HEAD")

    async def liar(skill, prompt, **kwargs):
        return SkillResult(skill, True, 0, "done, all resolved", "", provider="claude-local")

    monkeypatch.setattr(dispatch_role, "run_skill", liar)
    result = asyncio.run(dispatch_role.dispatch(
        "cicada", "x", cwd=str(repo), work_class="rebase", integration_base="main"))
    assert result.ok is False
    assert "did not verify" in result.error and "aborted" in result.error
    assert git(repo, "rev-parse", "HEAD") == head and mf.operation_in_progress(str(repo)) == ""


def test_a_failed_model_run_aborts_and_leaves_the_worktree_as_found(repo, monkeypatch, no_events):
    add_conflict(repo)
    head = git(repo, "rev-parse", "HEAD")

    async def failing(skill, prompt, **kwargs):
        return SkillResult(skill, False, 1, "", "", error="claude-local failed (x)",
                           provider="claude-local", local_failure="local_run_failed")

    monkeypatch.setattr(dispatch_role, "run_skill", failing)
    result = asyncio.run(dispatch_role.dispatch(
        "cicada", "x", cwd=str(repo), work_class="rebase", integration_base="main"))
    assert not result.ok and result.local_failure == "local_run_failed"
    assert git(repo, "rev-parse", "HEAD") == head and mf.operation_in_progress(str(repo)) == ""


def test_rebase_without_a_base_stays_on_the_model_path(repo, monkeypatch, no_events):
    seen: list = []

    async def model(skill, prompt, **kwargs):
        seen.append(prompt)
        return SkillResult(skill, True, 0, "ok", "", provider="claude-local")

    monkeypatch.setattr(dispatch_role, "run_skill", model)
    result = asyncio.run(dispatch_role.dispatch("cicada", "rebase it", cwd=str(repo), work_class="rebase"))
    assert result.ok and seen == ["rebase it"]


def test_regenerate_dispatch_runs_the_generator_with_no_model(repo, monkeypatch, no_events):
    calls = _model_must_not_run(monkeypatch)
    script = "import pathlib; pathlib.Path('catalog.md').write_text('x')"
    result = asyncio.run(dispatch_role.dispatch(
        "cicada", "regenerate", cwd=str(repo), work_class="regenerate_generated_files",
        regenerate_commands=[f"{sys.executable} -c \"{script}\""]))
    assert result.ok and calls == [] and "catalog.md" in result.stdout


def test_a_failing_generator_fails_the_dispatch_without_a_model(repo, monkeypatch, no_events):
    calls = _model_must_not_run(monkeypatch)
    result = asyncio.run(dispatch_role.dispatch(
        "cicada", "regenerate", cwd=str(repo), work_class="regenerate_generated_files",
        regenerate_commands=[f"{sys.executable} -c \"import sys; sys.exit(2)\""]))
    assert not result.ok and calls == [] and "exited 2" in result.error


# ── CLI surface ─────────────────────────────────────────────────────────────


@pytest.fixture
def cli(tmp_path, monkeypatch, capsys):
    skills = tmp_path / "skills-repo" / ".claude" / "skills" / "cicada"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# cicada\n", encoding="utf-8")
    monkeypatch.setattr(dispatch_role, "ATELES_REPO", tmp_path / "skills-repo")
    monkeypatch.setattr(dispatch_role, "_load_agent_def",
                        lambda role: (_ for _ in ()).throw(RuntimeError("no neotoma in tests")))

    def run(*argv):
        rc = dispatch_role.main(["--role", "cicada", "--task", "t", "--json", *argv])
        out = capsys.readouterr().out
        return rc, json.loads(out)
    return run


@pytest.mark.parametrize("argv,needle", [
    (["--rebase-onto", "main"], "requires --work-class rebase"),
    (["--work-class", "rebase", "--rebase-onto", "main"], "requires --cwd"),
    (["--work-class", "rebase", "--regenerate-cmd", "x", "--cwd", "."], "requires --work-class regenerate"),
    (["--work-class", "regenerate_generated_files", "--regenerate-cmd", "x"], "requires --cwd"),
])
def test_cli_rejects_incoherent_mechanical_flags(cli, argv, needle):
    rc, envelope = cli(*argv)
    assert rc != 0 and envelope["ok"] is False and needle in envelope["reason"]


def test_cli_clean_rebase_reports_the_deterministic_provider(cli, repo, monkeypatch, no_events):
    _model_must_not_run(monkeypatch)
    rc, envelope = cli("--work-class", "rebase", "--rebase-onto", "main", "--cwd", str(repo))
    assert rc == 0 and envelope["ok"] is True
    assert envelope["provider"] == "deterministic"
    assert envelope["attempted_providers"] == ["deterministic"]
    assert envelope["local_failure"] == ""
