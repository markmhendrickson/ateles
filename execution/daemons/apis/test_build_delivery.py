"""A Cicada dispatch that is meant to deliver a PR gets a worktree, the git-root
grants and network on Codex — whichever path launched it — and a task an
in-flight build filed to track its own work is not dispatched a second time.

Observed on the canary markmhendrickson/ateles#1448, 2026-10-14. Two Cicada runs
for one issue:

  * the swarm build handoff (network on, no cwd) built and pushed a branch, and
    was killed by its 1800s timeout before opening the PR;
  * ten minutes in, that run filed a Neotoma task to track its work. Apis
    auto-dispatched the task as a SECOND Cicada. The task path never passed
    `include_github_contract`, so the child ran `codex exec --sandbox
    workspace-write` with no network and no cwd, wrote a real diff, and failed
    with "sandbox denied network access — the child could not push or reach the
    GitHub API".

What these looked like RED, on origin/main:

    test_delivery_task_gets_worktree_network_and_token
        AssertionError: assert None == '<worktree>'   (run_skill got no cwd)
    test_task_for_an_issue_under_build_is_not_dispatched
        AssertionError: a duplicate builder was dispatched: [('cicada', ...)]
    test_build_handoff_runs_in_a_worktree_and_claims_the_issue
        AssertionError: assert None == '<worktree>'

Run: pytest execution/daemons/apis/test_build_delivery.py -v
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
import build_delivery  # noqa: E402
import skill_runner  # noqa: E402
from skill_runner import SkillResult  # noqa: E402
from unroutable_ledger import UnroutableLedger  # noqa: E402

REPO = "markmhendrickson/ateles"


# ── helpers ──────────────────────────────────────────────────────────────────


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(cwd),
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    ).stdout.strip()


@pytest.fixture
def clone(tmp_path, monkeypatch) -> Path:
    """A real clone of a throwaway origin, registered as the repo's build clone."""
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    origin.mkdir()
    _git(origin, "init", "--bare", "-b", "main")
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    (seed / "a.txt").write_text("a\n")
    _git(seed, "add", "a.txt")
    _git(seed, "commit", "-m", "seed")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "origin", "main")

    repos = tmp_path / "repos"
    repos.mkdir()
    work = repos / "ateles"
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(work)], check=True, capture_output=True
    )
    monkeypatch.setenv(build_delivery.BUILD_CLONES_ENV, f"{REPO}={work}")
    # conftest fakes worktree provisioning for every test; these use the real one.
    monkeypatch.setattr(
        build_delivery,
        "prepare_build_worktree",
        build_delivery.prepare_build_worktree.real,
    )
    return work


class _Notifier:
    def send(self, *a, **kw):
        pass

    def clear_dedupe(self, key):
        pass


def _task_snapshot(**over) -> dict:
    snap = {
        "title": "Implement snapshot-scoped design-basis checks for ateles issue 1448",
        "description": f"Implement the approved spec in {REPO}#1448. Open a PR.",
        "assigned_to": "cicada",
        "repository": REPO,
        "issue_number": 1448,
        "action_type": "open_or_merge_pr",
        "confidence": 0.95,
        "status": "pending",
    }
    snap.update(over)
    return snap


@pytest.fixture
def run_skill_calls(monkeypatch):
    """Record every run_skill call made by the task path."""
    calls: list[tuple[str, str, dict]] = []

    async def _fake(skill, prompt, **kw):
        calls.append((skill, prompt, kw))
        return SkillResult(skill, True, 0, "ok", "")

    monkeypatch.setattr(apis, "run_skill", _fake)
    monkeypatch.setenv("CICADA_AGENT_PAT", "test-token-not-real")
    return calls


def _spawn(snapshot, entity_id="ent_task_00001448"):
    return asyncio.run(
        apis._spawn_harness_skill(
            "cicada", entity_id, snapshot, "created", _Notifier(), role="cicada"
        )
    )


# ── the decision ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "skill,action,expected",
    [
        ("cicada", "open_or_merge_pr", True),
        ("Cicada", "OPEN_OR_MERGE_PR", True),
        ("cicada", "local_edit", False),
        ("cicada", "compute_only_analysis", False),
        ("cicada", "", False),
        ("cicada", None, False),
        # A reviewer shares the class but authors no build.
        ("vanellus", "open_or_merge_pr", False),
        ("pavo", "open_or_merge_pr", False),
    ],
)
def test_only_a_builder_cleared_to_deliver_is_a_delivery_dispatch(
    skill, action, expected
):
    assert build_delivery.is_delivery_dispatch(skill, action) is expected


# ── the task path ────────────────────────────────────────────────────────────


def test_delivery_task_gets_worktree_network_and_token(run_skill_calls):
    _spawn(_task_snapshot())

    (skill, prompt, kw), = run_skill_calls
    assert skill == "cicada"
    # The worktree is the child's cwd — skill_runner derives the git roots from it.
    assert kw["cwd"]
    # Network is granted through the one switch skill_runner reads, with the
    # scoped identity it requires (fail-closed without one).
    assert kw["include_github_contract"] is True
    assert kw["github_token"] == "test-token-not-real"
    assert "YOUR WORKTREE" in prompt and "do NOT create another worktree" in prompt


@pytest.mark.parametrize("action", ["local_edit", "compute_only_analysis"])
def test_non_delivery_task_gets_no_worktree_and_no_network(run_skill_calls, action):
    """#590: network is for the dispatches that deliver, and for no others."""
    _spawn(_task_snapshot(action_type=action))

    (_, prompt, kw), = run_skill_calls
    assert "cwd" not in kw
    assert "include_github_contract" not in kw
    assert "github_token" not in kw
    assert "YOUR WORKTREE" not in prompt


def test_delivery_task_naming_no_repository_runs_as_before(run_skill_calls):
    snap = _task_snapshot(
        repository="", issue_number="", title="Tidy", description="No repo here."
    )
    _spawn(snap)

    (_, _, kw), = run_skill_calls
    assert "cwd" not in kw and "include_github_contract" not in kw


def test_delivery_task_with_no_worktree_fails_loudly_without_running(
    run_skill_calls, monkeypatch
):
    async def _no_clone(repository, label):
        raise build_delivery.BuildWorktreeError("no local clone")

    monkeypatch.setattr(build_delivery, "prepare_build_worktree", _no_clone)
    result = _spawn(_task_snapshot())

    assert result.ok is False
    assert "worktree could not be provisioned" in result.error
    assert run_skill_calls == [], "a builder ran with nowhere to deliver from"


def test_delivery_task_claims_its_issue_while_it_runs(monkeypatch):
    seen: dict = {}

    async def _fake(skill, prompt, **kw):
        seen["holder"] = build_delivery.active_build(REPO, 1448)
        return SkillResult(skill, True, 0, "ok", "")

    monkeypatch.setattr(apis, "run_skill", _fake)
    monkeypatch.setenv("CICADA_AGENT_PAT", "test-token-not-real")
    _spawn(_task_snapshot(), entity_id="ent_task_claimed1")

    assert seen["holder"] == "task:ent_task_claimed1"
    assert build_delivery.active_build(REPO, 1448) is None, "claim leaked"


# ── what Codex actually receives ─────────────────────────────────────────────


def test_codex_command_for_a_delivery_dispatch_has_network_and_git_roots(clone):
    """The task path's kwargs, taken through the real codex command builder."""
    wt = asyncio.run(build_delivery.prepare_build_worktree(REPO, "issue-1448"))
    cmd, _ = skill_runner._provider_command(
        "codex", "codex", "sys", "work", cwd=wt.path, network=True
    )

    assert ["-c", "sandbox_workspace_write.network_access=true"] == cmd[
        cmd.index("-c") : cmd.index("-c") + 2
    ]
    add_dirs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--add-dir"]
    gitdir = Path(wt.clone, ".git", "worktrees").resolve()
    common = Path(wt.clone, ".git").resolve()
    assert str(common) in add_dirs
    assert any(Path(d).parent == gitdir for d in add_dirs)
    assert cmd[cmd.index("--cd") + 1] == wt.path
    # Scoped, not blanket: the sandbox is still workspace-write.
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert "danger-full-access" not in cmd


def test_codex_command_for_a_non_delivery_dispatch_has_no_network(clone):
    cmd, _ = skill_runner._provider_command(
        "codex", "codex", "sys", "work", cwd=None, network=False
    )
    assert "-c" not in cmd and "--add-dir" not in cmd
    assert "danger-full-access" not in cmd


# ── the duplicate ────────────────────────────────────────────────────────────


class _SpawnResult:
    ok = True
    pr_url = None
    detail = "stubbed"


@pytest.fixture
def dispatch_env(monkeypatch, tmp_path):
    monkeypatch.setattr(apis, "_unroutable", UnroutableLedger(path=tmp_path / "l.json"))
    monkeypatch.setattr(apis, "_created_seen", {})
    monkeypatch.setattr(apis, "READINESS_GATE", False)
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", False)
    writes: list[tuple] = []
    spawns: list[tuple] = []
    monkeypatch.setattr(
        apis, "set_task_status", lambda eid, status, **kw: writes.append((eid, status)) or True
    )
    monkeypatch.setattr(
        apis, "write_checkpoint_brief", lambda **kw: "ent_brief"
    )

    async def _spawn_stub(skill, entity_id, *a, **kw):
        spawns.append((skill, entity_id))
        return _SpawnResult()

    monkeypatch.setattr(apis, "_spawn_harness_skill", _spawn_stub)
    return writes, spawns


def _dispatch(entity_id, snapshot, **kw):
    asyncio.run(
        apis.dispatch_task(
            entity_id,
            snapshot,
            trigger="created",
            notifier=_Notifier(),
            snapshot_hydrated=True,
            **kw,
        )
    )


def test_task_for_an_issue_under_build_is_not_dispatched(dispatch_env):
    """The second Cicada on #1448: the build's own tracking task."""
    writes, spawns = dispatch_env
    with build_delivery.claim_build(REPO, 1448, "swarm-build-handoff"):
        _dispatch("ent_bf6382acd3f0cf83c641437c", _task_snapshot(action_type="local_edit"))

    assert spawns == [], f"a duplicate builder was dispatched: {spawns}"
    assert writes == [], f"the in-flight build's task was rewritten: {writes}"


def test_task_naming_the_issue_only_in_text_is_matched_too(dispatch_env):
    writes, spawns = dispatch_env
    snap = _task_snapshot(action_type="local_edit", repository="", issue_number="")
    with build_delivery.claim_build(REPO, 1448, "swarm-build-handoff"):
        _dispatch("ent_text_only_0001", snap)
    assert spawns == []


def test_task_for_a_different_issue_is_still_dispatched(dispatch_env):
    _, spawns = dispatch_env
    with build_delivery.claim_build(REPO, 1448, "swarm-build-handoff"):
        _dispatch("ent_other_issue_001", _task_snapshot(action_type="local_edit", issue_number=1449,
                  description="Implement the spec in %s#1449." % REPO))
    assert spawns == [("cicada", "ent_other_issue_001")]


def test_task_is_dispatched_once_the_build_has_finished(dispatch_env):
    _, spawns = dispatch_env
    with build_delivery.claim_build(REPO, 1448, "swarm-build-handoff"):
        pass
    _dispatch("ent_after_build_001", _task_snapshot(action_type="local_edit"))
    assert spawns == [("cicada", "ent_after_build_001")]


def test_operator_approved_release_is_not_second_guessed(dispatch_env, monkeypatch):
    """`gate_override` is the operator's explicit decision to run this task."""
    _, spawns = dispatch_env
    monkeypatch.setattr(apis, "_release_lifecycle_proven", lambda *a, **k: True)
    with build_delivery.claim_build(REPO, 1448, "swarm-build-handoff"):
        _dispatch("ent_approved_00001", _task_snapshot(action_type="local_edit"), gate_override=True)
    assert spawns == [("cicada", "ent_approved_00001")]


def test_only_builders_are_deduplicated(dispatch_env):
    """A different role's task about the same issue is a different piece of work."""
    _, spawns = dispatch_env
    with build_delivery.claim_build(REPO, 1448, "swarm-build-handoff"):
        _dispatch(
            "ent_other_role_0001",
            _task_snapshot(assigned_to="fringilla", action_type="local_edit"),
        )
    assert [s for s, _ in spawns] == ["fringilla"]


# ── the swarm build handoff ──────────────────────────────────────────────────


def test_build_handoff_runs_in_a_worktree_and_claims_the_issue(monkeypatch):
    from test_swarm_dispatch import (
        SwarmDispatcher,
        _StubNotifier,
        _config,
        _empty_spec_state,
        _issue_trigger,
    )

    trig = _issue_trigger()
    seen: dict = {}

    async def _fake_run_skill(skill, prompt, **kw):
        seen["kw"] = kw
        seen["prompt"] = prompt
        seen["holder"] = build_delivery.active_build(trig.repository, trig.number)
        return SkillResult(skill, True, 0, f"opened {trig.html_url}/../pull/9", "")

    monkeypatch.setattr("swarm_dispatch.run_skill", _fake_run_skill)

    async def _no_pr(self, trigger):
        return None

    monkeypatch.setattr(SwarmDispatcher, "_find_open_pr_for_issue", _no_pr)
    monkeypatch.setenv("CICADA_AGENT_PAT", "test-token-not-real")

    d = SwarmDispatcher(_StubNotifier(), _config())
    asyncio.run(d._open_implementation_pr(trig, _empty_spec_state()))

    assert seen["kw"]["cwd"]
    assert seen["kw"]["include_github_contract"] is True
    assert seen["holder"] == "swarm-build-handoff"
    assert "YOUR WORKTREE" in seen["prompt"]
    assert build_delivery.active_build(trig.repository, trig.number) is None


def test_build_handoff_with_no_worktree_does_not_dispatch(monkeypatch):
    from test_swarm_dispatch import (
        SwarmDispatcher,
        _StubNotifier,
        _config,
        _empty_spec_state,
        _issue_trigger,
    )

    ran: list = []

    async def _fake_run_skill(skill, prompt, **kw):
        ran.append(skill)
        return SkillResult(skill, True, 0, "", "")

    async def _no_clone(repository, label):
        raise build_delivery.BuildWorktreeError("no local clone")

    monkeypatch.setattr("swarm_dispatch.run_skill", _fake_run_skill)
    monkeypatch.setattr(build_delivery, "prepare_build_worktree", _no_clone)

    d = SwarmDispatcher(_StubNotifier(), _config())
    url = asyncio.run(d._open_implementation_pr(_issue_trigger(), _empty_spec_state()))

    assert url is None and ran == []


# ── the worktree itself, against a real repository ───────────────────────────


def test_worktree_is_linked_off_origin_main_and_leaves_the_clone_alone(clone):
    head_before = _git(clone, "rev-parse", "HEAD")
    branch_before = _git(clone, "branch", "--show-current")

    wt = asyncio.run(build_delivery.prepare_build_worktree(REPO, "issue-1448"))

    assert Path(wt.path).is_dir()
    assert wt.path != str(clone)
    # Linked: its git dir differs from the common dir.
    assert _git(Path(wt.path), "rev-parse", "--git-dir") != _git(
        Path(wt.path), "rev-parse", "--git-common-dir"
    )
    assert _git(Path(wt.path), "rev-parse", "HEAD") == wt.base_sha
    assert _git(Path(wt.path), "branch", "--show-current") == wt.branch
    assert wt.branch.startswith("cicada/issue-1448-")
    assert wt.base_fresh is True
    # The shared clone's checkout is untouched.
    assert _git(clone, "rev-parse", "HEAD") == head_before
    assert _git(clone, "branch", "--show-current") == branch_before
    assert _git(clone, "status", "--porcelain") == ""


def test_the_sandbox_grants_exactly_the_two_roots_git_needs(clone):
    wt = asyncio.run(build_delivery.prepare_build_worktree(REPO, "issue-1448"))
    roots = skill_runner._git_roots_for_sandbox(wt.path)

    assert len(roots) == 2
    assert str(Path(wt.clone, ".git").resolve()) in roots
    assert all(Path(r).is_relative_to(Path(wt.clone).resolve()) for r in roots)


def test_each_dispatch_gets_its_own_worktree(clone):
    a = asyncio.run(build_delivery.prepare_build_worktree(REPO, "issue-1448"))
    b = asyncio.run(build_delivery.prepare_build_worktree(REPO, "issue-1448"))
    assert a.path != b.path and a.branch != b.branch


def test_branches_from_the_fetched_main_not_a_stale_one(clone, tmp_path):
    seed = tmp_path / "seed"
    (seed / "b.txt").write_text("b\n")
    _git(seed, "add", "b.txt")
    _git(seed, "commit", "-m", "moves main")
    _git(seed, "push", "origin", "main")
    new_head = _git(seed, "rev-parse", "HEAD")

    wt = asyncio.run(build_delivery.prepare_build_worktree(REPO, "issue-1448"))
    assert wt.base_sha == new_head


def test_a_failed_fetch_still_builds_and_says_the_base_may_be_stale(clone):
    _git(clone, "remote", "set-url", "origin", "/nonexistent/origin.git")
    wt = asyncio.run(build_delivery.prepare_build_worktree(REPO, "issue-1448"))

    assert wt.base_fresh is False
    assert "git fetch origin" in build_delivery.worktree_instructions(wt)


def test_an_unknown_repository_is_refused_with_the_reason(clone):
    with pytest.raises(build_delivery.BuildWorktreeError, match="no local clone"):
        asyncio.run(
            build_delivery.prepare_build_worktree("someone/does-not-exist-9z", "x")
        )


# ── which issue is a task about ──────────────────────────────────────────────


def test_structured_fields_win_over_text():
    snap = {"repository": "A/B", "issue_number": "7", "description": "see c/d#9"}
    assert build_delivery.issue_key(snap) == ("a/b", 7)


def test_two_different_references_are_ambiguous_and_match_nothing():
    snap = {"description": "Follow-up to x/y#1 for x/y#2"}
    assert build_delivery.issue_key(snap) is None


def test_an_issue_url_names_the_issue():
    snap = {"description": "https://github.com/x/y/issues/12"}
    assert build_delivery.issue_key(snap) == ("x/y", 12)


def test_claim_is_released_on_error_and_only_by_its_holder():
    with pytest.raises(RuntimeError):
        with build_delivery.claim_build("x/y", 1, "first"):
            with build_delivery.claim_build("x/y", 1, "second"):
                pass  # not the holder: must not release "first"
            assert build_delivery.active_build("x/y", 1) == "first"
            raise RuntimeError("boom")
    assert build_delivery.active_build("x/y", 1) is None
