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


def route_origin_to(work: Path, target: Path | str) -> None:
    """Send the clone's configured github.com origin to ``target`` (insteadOf)."""
    rc = subprocess.run(
        ["git", "-C", str(work), "config", "--get-regexp", r"^url\..*\.insteadof$"],
        capture_output=True,
        text=True,
    )
    for line in rc.stdout.splitlines():
        section = line.split()[0].rsplit(".insteadof", 1)[0]
        _git(work, "config", "--remove-section", section)
    _git(work, "config", f"url.{target}.insteadOf", f"https://github.com/{REPO}.git")


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
    # The clone says it is the real repository; only the transport is local.
    _git(work, "remote", "set-url", "origin", f"https://github.com/{REPO}.git")
    route_origin_to(work, origin)
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
        claim = build_delivery.active_build(REPO, 1448)
        seen["holder"] = claim.holder if claim else None
        seen["prompt"] = prompt
        return SkillResult(skill, True, 0, "ok", "")

    monkeypatch.setattr(apis, "run_skill", _fake)
    monkeypatch.setenv("CICADA_AGENT_PAT", "test-token-not-real")
    _spawn(_task_snapshot(), entity_id="ent_task_claimed1")

    assert seen["holder"] == "task:ent_task_claimed1"
    # The build is told how to mark the task it files for itself.
    assert build_delivery.RUN_MARKER_PREFIX in seen["prompt"]
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


# ── tasks about an issue that is being built ─────────────────────────────────


class _SpawnResult:
    ok = True
    pr_url = None
    detail = "stubbed"


class _RecNotifier(_Notifier):
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message, *a, **kw):
        self.sent.append(message)


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
    monkeypatch.setattr(apis, "write_checkpoint_brief", lambda **kw: "ent_brief")

    async def _spawn_stub(skill, entity_id, snapshot, trigger, *a, **kw):
        spawns.append((skill, entity_id, trigger))
        return _SpawnResult()

    monkeypatch.setattr(apis, "_spawn_harness_skill", _spawn_stub)

    async def _no_hydrate(event):
        return None

    monkeypatch.setattr(apis, "hydrate_snapshot", _no_hydrate)
    # What the task looks like when re-read after the build ends.
    fresh: dict = {"status": "pending"}
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda _id: fresh)
    return writes, spawns, fresh


def _local(**over) -> dict:
    return _task_snapshot(action_type="local_edit", **over)


def _created(entity_id: str, snapshot: dict):
    from lib.daemon_runtime.sse_client import NeotomaEvent

    return NeotomaEvent(
        event_type="entity_created",
        entity_type="task",
        entity_id=entity_id,
        action="created",
        snapshot=snapshot,
        hydrated=True,
    )


async def _settle():
    """Let a released build's deferred retries run to completion."""
    for _ in range(5):
        await asyncio.sleep(0)
    if build_delivery._background:
        await asyncio.gather(*list(build_delivery._background))


def _during_build(dispatch_env, *events, fail=False, notifier=None):
    """Deliver `events` while a build of the issue holds its claim; then release it.

    Returns the claim and the notifier.
    """
    notifier = notifier or _RecNotifier()
    out: dict = {}

    async def _go():
        try:
            with build_delivery.claim_build(REPO, 1448, "swarm-build-handoff") as claim:
                out["claim"] = claim
                for make in events:
                    await apis.handle_event(make(claim), notifier)
                out["spawns_mid_build"] = list(dispatch_env[1])
                out["writes_mid_build"] = list(dispatch_env[0])
                if fail:
                    raise RuntimeError("build crashed")
        except RuntimeError:
            out["raised"] = True
        await _settle()

    asyncio.run(_go())
    return out, notifier


def test_the_builds_own_tracking_task_is_not_dispatched(dispatch_env):
    """The second Cicada on #1448: the build's own tracking task, mid-build."""
    writes, spawns, _ = dispatch_env
    marker = lambda claim: _created(  # noqa: E731
        "ent_bf6382acd3f0cf83c641437c",
        _local(description=f"Track the build.\n{build_delivery.RUN_MARKER_PREFIX} {claim.run_id}"),
    )
    out, _ = _during_build(dispatch_env, marker)

    assert out["spawns_mid_build"] == [], "a duplicate builder was dispatched"
    assert out["writes_mid_build"] == [], "the build's own task was rewritten"
    # Nor is it picked up again once the build ends: it is not deferred.
    assert spawns == [] and writes == []


def test_a_marker_from_no_running_build_proves_nothing(dispatch_env):
    """A copied or stale marker does not make a task 'the build's own'."""
    _, spawns, _ = dispatch_env
    snap = _local(description="build-run: 0123456789abcdef")
    out, _ = _during_build(dispatch_env, lambda c: _created("ent_forged_000001", snap))

    assert out["spawns_mid_build"] == []  # deferred, not dispatched now ...
    assert spawns == [("cicada", "ent_forged_000001", "build_released")]  # ... but run later


def test_an_independent_task_is_deferred_and_run_when_the_build_ends(dispatch_env):
    writes, spawns, _ = dispatch_env
    out, _ = _during_build(
        dispatch_env, lambda c: _created("ent_followup_00001", _local())
    )

    assert out["spawns_mid_build"] == [] and out["writes_mid_build"] == []
    assert spawns == [("cicada", "ent_followup_00001", "build_released")]


def test_the_retry_happens_when_the_build_fails(dispatch_env):
    _, spawns, _ = dispatch_env
    out, _ = _during_build(
        dispatch_env, lambda c: _created("ent_followup_00001", _local()), fail=True
    )

    assert out["raised"] is True
    assert spawns == [("cicada", "ent_followup_00001", "build_released")]
    assert build_delivery.active_build(REPO, 1448) is None, "claim leaked"


def test_redelivery_neither_defers_twice_nor_redispatches(dispatch_env):
    """`_seen_created` consumed the event once; copies are ignored."""
    _, spawns, _ = dispatch_env
    make = lambda c: _created("ent_followup_00001", _local())  # noqa: E731
    out, _ = _during_build(dispatch_env, make, make, make)

    assert spawns == [("cicada", "ent_followup_00001", "build_released")]
    # And a copy delivered after the retry is still ignored.
    asyncio.run(apis.handle_event(make(None), _RecNotifier()))
    assert spawns == [("cicada", "ent_followup_00001", "build_released")]


def test_a_deferred_task_the_build_finished_is_not_run(dispatch_env):
    _, spawns, fresh = dispatch_env
    fresh["status"] = "done"
    _during_build(dispatch_env, lambda c: _created("ent_followup_00001", _local()))
    assert spawns == []


def test_a_deferred_task_that_cannot_be_re_read_is_reported_not_run(
    dispatch_env, monkeypatch
):
    _, spawns, _ = dispatch_env
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda _id: None)
    _, notifier = _during_build(
        dispatch_env, lambda c: _created("ent_followup_00001", _local())
    )
    assert spawns == []
    assert any("ent_followup_00001" in m and "not dispatched" in m for m in notifier.sent)


def test_several_deferred_tasks_each_run_once_in_order(dispatch_env):
    _, spawns, _ = dispatch_env
    _during_build(
        dispatch_env,
        lambda c: _created("ent_followup_00001", _local()),
        lambda c: _created("ent_followup_00002", _local()),
    )
    assert [e for _, e, _ in spawns] == ["ent_followup_00001", "ent_followup_00002"]


def test_task_for_a_different_issue_is_dispatched_at_once(dispatch_env):
    _, spawns, _ = dispatch_env
    other = _local(issue_number=1449, description="Implement the spec in %s#1449." % REPO)
    out, _ = _during_build(dispatch_env, lambda c: _created("ent_other_issue_001", other))
    assert out["spawns_mid_build"] == [("cicada", "ent_other_issue_001", "created")]


def test_task_for_a_different_repository_is_dispatched_at_once(dispatch_env):
    other = _local(repository="someone/else")
    out, _ = _during_build(dispatch_env, lambda c: _created("ent_other_repo_0001", other))
    assert out["spawns_mid_build"] == [("cicada", "ent_other_repo_0001", "created")]


def test_task_is_dispatched_at_once_when_no_build_is_running(dispatch_env):
    _, spawns, _ = dispatch_env
    asyncio.run(apis.handle_event(_created("ent_after_build_001", _local()), _RecNotifier()))
    assert spawns == [("cicada", "ent_after_build_001", "created")]


def test_operator_approved_release_is_not_second_guessed(dispatch_env, monkeypatch):
    """`gate_override` is the operator's explicit decision to run this task."""
    _, spawns, _ = dispatch_env
    monkeypatch.setattr(apis, "_release_lifecycle_proven", lambda *a, **k: True)

    async def _go():
        with build_delivery.claim_build(REPO, 1448, "swarm-build-handoff"):
            await apis.dispatch_task(
                "ent_approved_00001",
                _local(),
                trigger="approved",
                notifier=_RecNotifier(),
                snapshot_hydrated=True,
                gate_override=True,
            )

    asyncio.run(_go())
    assert spawns == [("cicada", "ent_approved_00001", "approved")]


def test_only_builders_are_deferred(dispatch_env):
    """A different role's task about the same issue is a different piece of work."""
    other = _local(assigned_to="fringilla")
    out, _ = _during_build(dispatch_env, lambda c: _created("ent_other_role_0001", other))
    assert [s for s, _, _ in out["spawns_mid_build"]] == ["fringilla"]


def test_a_reference_in_prose_does_not_make_a_key_without_a_repository(dispatch_env):
    """No resolved repository, no key: nothing to defer behind."""
    snap = _local(repository="", issue_number="")
    out, _ = _during_build(dispatch_env, lambda c: _created("ent_prose_only_0001", snap))
    assert out["spawns_mid_build"] == [("cicada", "ent_prose_only_0001", "created")]


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
        claim = build_delivery.active_build(trig.repository, trig.number)
        seen["holder"] = claim.holder if claim else None
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
    assert build_delivery.RUN_MARKER_PREFIX in seen["prompt"]
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
    route_origin_to(clone, "/nonexistent/origin.git")
    wt = asyncio.run(build_delivery.prepare_build_worktree(REPO, "issue-1448"))

    assert wt.base_fresh is False
    assert "git fetch origin" in build_delivery.worktree_instructions(wt)


def test_an_unknown_repository_is_refused_with_the_reason(clone):
    with pytest.raises(build_delivery.BuildWorktreeError, match="no local clone"):
        asyncio.run(
            build_delivery.prepare_build_worktree("someone/does-not-exist-9z", "x")
        )


# ── which issue is a task about ──────────────────────────────────────────────


def test_the_repo_field_names_the_repository_like_every_other_field():
    """One resolver, four keys, first present: `repo` is the one Cicada fixtures use."""
    for key in ("repo", "dispatch_repo", "github_repo", "repository"):
        assert apis._dispatch_repo_from_snapshot({key: REPO}) == REPO


def test_an_explicit_repository_always_beats_a_reference_in_prose(run_skill_calls):
    snap = _task_snapshot(
        repo="owner/target",
        repository="",
        issue_number=7,
        description="Follow the pattern in other/reference#9",
    )
    assert apis._dispatch_repo_from_snapshot(snap) == "owner/target"
    assert build_delivery.issue_key(snap, "owner/target") == ("owner/target", 7)


def test_a_reference_to_another_repository_is_not_this_issue():
    snap = {"description": "Follow the pattern in other/reference#9"}
    assert build_delivery.issue_key(snap, "owner/target") is None


def test_the_worktree_and_token_follow_the_explicit_repository(run_skill_calls, monkeypatch):
    asked: list = []

    async def _prepare(repository, label):
        asked.append(("worktree", repository))
        return build_delivery.BuildWorktree("/wt", "cicada/x", "0" * 40, "/clone", True)

    monkeypatch.setattr(build_delivery, "prepare_build_worktree", _prepare)
    monkeypatch.setattr(
        apis, "_token_for_agent_on_repo", lambda skill, repo: asked.append(("token", repo)) or "t"
    )
    _spawn(
        _task_snapshot(
            repo="owner/target",
            repository="",
            description="Follow the pattern in other/reference#9",
        )
    )
    assert asked == [("worktree", "owner/target"), ("token", "owner/target")]


def test_the_issue_number_comes_from_text_only_for_the_resolved_repository():
    snap = {"description": f"Implement {REPO}#1448 following other/reference#9"}
    assert build_delivery.issue_key(snap, REPO) == (REPO.lower(), 1448)


def test_two_different_references_are_ambiguous_and_match_nothing():
    snap = {"description": f"Follow-up to {REPO}#1 for {REPO}#2"}
    assert build_delivery.issue_key(snap, REPO) is None


def test_an_issue_url_names_the_issue():
    snap = {"description": f"https://github.com/{REPO}/issues/12"}
    assert build_delivery.issue_key(snap, REPO) == (REPO.lower(), 12)


def test_claim_is_released_on_error_and_only_by_its_holder():
    with pytest.raises(RuntimeError):
        with build_delivery.claim_build("x/y", 1, "first"):
            with build_delivery.claim_build("x/y", 1, "second"):
                pass  # not the holder: must not release "first"
            assert build_delivery.active_build("x/y", 1).holder == "first"
            raise RuntimeError("boom")
    assert build_delivery.active_build("x/y", 1) is None


# ── the clone must be the repository ─────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [
        f"https://github.com/{REPO}.git",
        f"https://github.com/{REPO}",
        f"HTTPS://GitHub.com/{REPO.upper()}.git",
        f"https://x-access-token:abc@github.com/{REPO}.git",
        f"git@github.com:{REPO}.git",
        f"ssh://git@github.com/{REPO}.git",
        f"ssh://git@github.com:22/{REPO}.git",
        f"https://github.com/{REPO}/",
    ],
)
def test_every_remote_form_of_the_repository_matches(url):
    assert build_delivery.normalise_remote(url) == f"github.com/{REPO}".lower()


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/other/repo.git",
        f"https://github.com.evil.example/{REPO}.git",
        f"https://evil.example/github.com/{REPO}.git",
        f"https://gitlab.com/{REPO}.git",
        f"/srv/git/{REPO}.git",
        f"file:///srv/git/{REPO}.git",
        f"https://github.com/{REPO}-fork.git",
        "",
    ],
)
def test_other_remotes_do_not_match(url):
    assert build_delivery.normalise_remote(url) != f"github.com/{REPO}".lower()


class _Spy:
    """Record every git command and every token request; none may happen."""

    def __init__(self, monkeypatch):
        self.git: list[list[str]] = []
        self.tokens: list = []
        real = build_delivery._git

        async def _git_spy(args, cwd, timeout=120):
            self.git.append(args)
            return await real(args, cwd, timeout)

        monkeypatch.setattr(build_delivery, "_git", _git_spy)
        monkeypatch.setattr(
            apis, "_token_for_agent_on_repo", lambda *a: self.tokens.append(a) or "t"
        )

    def mutating(self) -> list[list[str]]:
        return [a for a in self.git if a[0] in {"fetch", "worktree"}]


def test_a_clone_whose_origin_is_elsewhere_is_refused_before_anything_happens(
    clone, run_skill_calls, monkeypatch
):
    _git(clone, "remote", "set-url", "origin", "https://github.com/other/repo.git")
    spy = _Spy(monkeypatch)

    result = _spawn(_task_snapshot())

    assert result.ok is False
    assert "origin that is not" in result.error
    assert spy.mutating() == [], f"git was run against the wrong clone: {spy.git}"
    assert spy.tokens == [], "a token was resolved for the wrong clone"
    assert run_skill_calls == [], "a child was launched"
    assert not (clone.parent / "ateles-worktrees").exists(), "a worktree was made"


def test_a_push_url_that_points_elsewhere_is_refused_too(clone, run_skill_calls, monkeypatch):
    _git(clone, "config", "remote.origin.pushurl", "https://github.com/other/repo.git")
    spy = _Spy(monkeypatch)

    result = _spawn(_task_snapshot())

    assert result.ok is False and spy.mutating() == [] and spy.tokens == []


def test_a_clone_with_no_origin_is_refused(clone, run_skill_calls):
    _git(clone, "remote", "remove", "origin")
    result = _spawn(_task_snapshot())
    assert result.ok is False and "no origin remote" in result.error


def test_the_handoff_refuses_a_mismatched_clone_too(clone, monkeypatch):
    from test_swarm_dispatch import (
        SwarmDispatcher,
        _StubNotifier,
        _config,
        _empty_spec_state,
        _issue_trigger,
    )

    trig = _issue_trigger()
    trig.repository = REPO
    _git(clone, "remote", "set-url", "origin", "https://github.com/other/repo.git")
    ran: list = []

    async def _fake_run_skill(skill, prompt, **kw):
        ran.append(skill)
        return SkillResult(skill, True, 0, "", "")

    monkeypatch.setattr("swarm_dispatch.run_skill", _fake_run_skill)
    spy = _Spy(monkeypatch)
    monkeypatch.setattr("swarm_dispatch._token_for_agent_on_repo", lambda *a: spy.tokens.append(a) or "t")

    d = SwarmDispatcher(_StubNotifier(), _config())
    assert asyncio.run(d._open_implementation_pr(trig, _empty_spec_state())) is None
    assert ran == [] and spy.mutating() == [] and spy.tokens == []


@pytest.mark.parametrize(
    "name",
    [
        "",
        "owner",
        "owner/",
        "/repo",
        "a/b/c",
        "../etc",
        "owner/..",
        "owner/.",
        "own er/repo",
        "owner/re po",
        "owner/repo\n",
        "owner/repo\x00",
        "owner/re%2Fpo",
        "owner/..%2F..",
        "owner/a..b",
        "owner\\repo",
        "owner/repo.git",
        "-owner/repo",
        "own\u00e9r/repo",
        "owner/rep\u0585",
        "o" * 50 + "/repo",
        "owner/" + "r" * 101,
        None,
        7,
    ],
)
def test_malformed_repository_names_are_refused(name, monkeypatch):
    spy = _Spy(monkeypatch)
    with pytest.raises(build_delivery.BuildWorktreeError):
        asyncio.run(build_delivery.prepare_build_worktree.real(name, "x"))
    assert spy.git == [] and spy.tokens == []


@pytest.mark.parametrize(
    "name", ["markmhendrickson/ateles", "a/b", "Owner-1/re.po_sitory-2", "o/.github"]
)
def test_ordinary_repository_names_are_accepted(name):
    assert build_delivery.validate_repository(name) == name
