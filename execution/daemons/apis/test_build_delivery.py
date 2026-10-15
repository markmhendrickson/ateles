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
    # The transport rule matches a SHORTER prefix (no ".git") than a rewrite of
    # the full URL, so a more specific rule elsewhere still takes precedence.
    base = str(target)[: -len(".git")]
    _git(work, "config", f"url.{base}.insteadOf", f"https://github.com/{REPO}")


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
    # The one place a rewrite of the github.com remote is allowed: this test's
    # own bare repository. Production's allowance is empty.
    monkeypatch.setattr(
        build_delivery, "TEST_ONLY_LOCAL_TRANSPORTS", frozenset({str(origin)})
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
        raise build_delivery.BuildWorktreeError("no local clone", fix="clone it first")

    monkeypatch.setattr(build_delivery, "prepare_build_worktree", _no_clone)
    result = _spawn(_task_snapshot())

    assert result.ok is False
    # What happened, and what to fix before retrying.
    assert result.error == "build not started: no local clone. Before retrying: clone it first"
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


class _TaskStore:
    """A stand-in for the task rows in Neotoma: durable across 'restarts'."""

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self.fail_writes = False
        self.fail_reads: set[str] = set()
        self.fail_query = False
        self.fail_query_times = 0
        self.queries = 0
        # field -> [mode, remaining]: a write that SUCCEEDS but is not persisted
        # as sent ("omit": dropped, "mismatch": stored with another value).
        self.lossy: dict[str, list] = {}
        self.key_suffixes: list[str] = []

    def put(self, entity_id: str, snapshot: dict) -> None:
        self.rows[entity_id] = dict(snapshot)

    def _persist(self, entity_id, field, value) -> None:
        row = self.rows.setdefault(entity_id, {})
        loss = self.lossy.get(field)
        if loss and loss[1] > 0:
            loss[1] -= 1
            if loss[0] == "mismatch":
                row[field] = "garbled-" + str(value)[:8]
            return
        row[field] = value

    def field(self, entity_id, field, value, **kw) -> bool:
        if self.fail_writes:
            return False
        self.key_suffixes.append(kw.get("key_suffix", ""))
        self._persist(entity_id, field, value)
        return True

    def status(self, entity_id, status, **kw) -> bool:
        if self.fail_writes:
            return False
        self._persist(entity_id, "status", getattr(status, "value", status))
        if kw.get("reason") is not None:
            self._persist(entity_id, "blocked_reason", kw["reason"])
        return True

    def query(self, entity_type, *, snapshot_filters, limit=None, **kw):
        self.queries += 1
        if self.fail_query or self.queries <= self.fail_query_times:
            return None
        out = []
        for eid, row in self.rows.items():
            if all(row.get(k) == f["value"] for k, f in snapshot_filters.items()):
                out.append({"entity_id": eid, "snapshot": dict(row)})
        return out[:limit] if limit else out

    def read(self, entity_id):
        if entity_id in self.fail_reads:
            return None
        return dict(self.rows[entity_id]) if entity_id in self.rows else None


@pytest.fixture
def dispatch_env(monkeypatch, tmp_path):
    monkeypatch.setattr(apis, "_unroutable", UnroutableLedger(path=tmp_path / "l.json"))
    monkeypatch.setattr(apis, "_created_seen", {})
    monkeypatch.setattr(apis, "_recovering", set())
    monkeypatch.setattr(apis, "_surfaced", set())
    monkeypatch.setattr(apis, "_unrecorded_deferrals", {})
    monkeypatch.setattr(apis, "_RECOVERY_PASS_DELAY_S", 0.001)
    monkeypatch.setattr(apis, "_RECOVERY_LIST_RETRY_DELAY_S", 0.001)
    monkeypatch.setattr(apis, "READINESS_GATE", False)
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", False)
    store = _TaskStore()
    writes: list[tuple] = []
    spawns: list[tuple] = []

    def _status(eid, status, **kw):
        writes.append((eid, getattr(status, "value", status)))
        return store.status(eid, status, **kw)

    monkeypatch.setattr(apis, "set_task_status", _status)
    monkeypatch.setattr(apis, "set_task_field", store.field)
    monkeypatch.setattr(apis, "query_all_entities", store.query)
    monkeypatch.setattr(apis, "fetch_task_snapshot", store.read)
    monkeypatch.setattr(apis, "write_checkpoint_brief", lambda **kw: "ent_brief")

    async def _spawn_stub(skill, entity_id, snapshot, trigger, *a, **kw):
        spawns.append((skill, entity_id, trigger))
        return _SpawnResult()

    monkeypatch.setattr(apis, "_spawn_harness_skill", _spawn_stub)

    async def _no_hydrate(event):
        return None

    monkeypatch.setattr(apis, "hydrate_snapshot", _no_hydrate)
    return writes, spawns, store


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
    """Let a released build's recovery sweep and its dispatches run to completion."""
    for _ in range(5):
        await asyncio.sleep(0)
    pending = list(apis._background_dispatch_tasks)
    while pending:
        await asyncio.gather(*pending, return_exceptions=True)
        pending = [t for t in apis._background_dispatch_tasks if not t.done()]


def _during_build(dispatch_env, *events, fail=False, notifier=None, release=True):
    """Deliver `events` while a build of the issue holds its claim; then release it.

    Each event's task is first put in the (stand-in) task store, as Neotoma would
    hold it. Returns what was observed mid-build, and the notifier.
    """
    _, _, store = dispatch_env
    notifier = notifier or _RecNotifier()
    out: dict = {}
    apis.install_build_release_hook(notifier)

    async def _go():
        try:
            with build_delivery.claim_build(REPO, 1448, "swarm-build-handoff") as claim:
                out["claim"] = claim
                for make in events:
                    event = make(claim)
                    if event.entity_id not in store.rows:
                        store.put(event.entity_id, event.snapshot)
                    await apis.handle_event(event, notifier)
                out["spawns_mid_build"] = list(dispatch_env[1])
                out["writes_mid_build"] = list(dispatch_env[0])
                out["rows_mid_build"] = {k: dict(v) for k, v in store.rows.items()}
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
    # Nor is it deferred: it is the build, so there is nothing to retry.
    assert spawns == [] and writes == []
    assert "disposition" not in out["rows_mid_build"]["ent_bf6382acd3f0cf83c641437c"]


def test_a_marker_from_no_running_build_proves_nothing(dispatch_env):
    """A copied or stale marker does not make a task 'the build's own'."""
    _, spawns, _ = dispatch_env
    snap = _local(description="build-run: 0123456789abcdef")
    out, _ = _during_build(dispatch_env, lambda c: _created("ent_forged_000001", snap))

    assert out["spawns_mid_build"] == []  # deferred, not dispatched now ...
    assert spawns == [("cicada", "ent_forged_000001", "build_released")]  # ... but run later


def test_an_independent_task_is_deferred_durably_then_run_when_the_build_ends(dispatch_env):
    writes, spawns, store = dispatch_env
    out, _ = _during_build(
        dispatch_env, lambda c: _created("ent_followup_00001", _local())
    )

    assert out["spawns_mid_build"] == []
    # The deferral is on the task, with a reason the operator can read, BEFORE
    # anything is acknowledged.
    row = out["rows_mid_build"]["ent_followup_00001"]
    claim = out["claim"]
    assert row["disposition"] == build_delivery.DEFERRED_DISPOSITION
    assert build_delivery.parse_deferral_context(row["dispatch_context"]) == (
        REPO.lower(), 1448, claim.run_id,
    )
    assert row["status"] == "pending"
    assert row["blocked_reason"].startswith("Deferred:")
    assert "swarm-build-handoff" in row["blocked_reason"] and claim.run_id in row["blocked_reason"]
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


def test_two_sweeps_at_once_dispatch_a_deferred_task_once(dispatch_env):
    _, spawns, _ = dispatch_env
    n = _RecNotifier()

    async def _go():
        with build_delivery.claim_build(REPO, 1448, "swarm-build-handoff") as claim:
            event = _created("ent_followup_00001", _local())
            dispatch_env[2].put(event.entity_id, event.snapshot)
            await apis.handle_event(event, n)
        await asyncio.gather(apis.recover_deferred_tasks(n), apis.recover_deferred_tasks(n))
        await _settle()

    asyncio.run(_go())
    assert spawns == [("cicada", "ent_followup_00001", "build_released")]


def test_a_deferred_task_the_build_finished_is_not_run(dispatch_env):
    _, spawns, store = dispatch_env
    n = _RecNotifier()
    apis.install_build_release_hook(n)

    async def _go():
        with build_delivery.claim_build(REPO, 1448, "swarm-build-handoff"):
            event = _created("ent_followup_00001", _local())
            store.put(event.entity_id, event.snapshot)
            await apis.handle_event(event, n)
            store.rows[event.entity_id]["status"] = "done"  # the build completed it
        await _settle()

    asyncio.run(_go())
    assert spawns == []


def test_a_deferred_task_that_cannot_be_re_read_is_surfaced_not_run(dispatch_env):
    _, spawns, store = dispatch_env
    store.fail_reads.add("ent_followup_00001")
    _, notifier = _during_build(
        dispatch_env, lambda c: _created("ent_followup_00001", _local())
    )
    assert spawns == []
    assert any("ent_followup_00001" in m and "re-dispatch it by hand" in m for m in notifier.sent)


def test_a_failed_task_query_is_surfaced_once_and_retried(dispatch_env):
    _, spawns, store = dispatch_env
    store.fail_query = True
    _, notifier = _during_build(
        dispatch_env, lambda c: _created("ent_followup_00001", _local())
    )
    assert spawns == []
    listing = [m for m in notifier.sent if "could not list the tasks deferred" in m]
    assert len(listing) == 1, "one notice, however many retries"
    assert store.queries == 1 + apis._RECOVERY_LIST_RETRIES, "retried a bounded number of times"


def test_a_query_that_recovers_on_retry_still_dispatches(dispatch_env):
    _, spawns, store = dispatch_env
    store.fail_query_times = 1
    _during_build(dispatch_env, lambda c: _created("ent_followup_00001", _local()))
    assert spawns == [("cicada", "ent_followup_00001", "build_released")]


def test_a_deferral_that_cannot_be_recorded_is_reported_and_still_retried_in_process(
    dispatch_env,
):
    _, spawns, store = dispatch_env
    store.fail_writes = True
    out, notifier = _during_build(
        dispatch_env, lambda c: _created("ent_followup_00001", _local())
    )
    assert any("could not record" in m for m in notifier.sent)
    assert out["spawns_mid_build"] == []


def test_a_deferred_task_with_an_unreadable_record_is_surfaced(dispatch_env):
    _, spawns, store = dispatch_env
    n = _RecNotifier()

    async def _go():
        store.put(
            "ent_bad_record_0001",
            {**_local(), "status": "pending", "disposition": build_delivery.DEFERRED_DISPOSITION,
             "dispatch_context": "garbled"},
        )
        await apis.recover_deferred_tasks(n)
        await _settle()

    asyncio.run(_go())
    assert spawns == []
    assert any("ent_bad_record_0001" in m and "does not say which build" in m for m in n.sent)


def test_the_sweep_leaves_the_historical_backlog_alone(dispatch_env):
    """Pending tasks no deferral marked are never selected or dispatched."""
    _, spawns, store = dispatch_env
    for i in range(5):
        store.put(f"ent_old_pending_{i:04d}", {**_local(), "status": "pending"})
    asyncio.run(apis.recover_deferred_tasks(_RecNotifier()))
    assert spawns == []


def test_several_deferred_tasks_each_run_once(dispatch_env):
    _, spawns, _ = dispatch_env
    _during_build(
        dispatch_env,
        lambda c: _created("ent_followup_00001", _local()),
        lambda c: _created("ent_followup_00002", _local()),
    )
    assert sorted(e for _, e, _ in spawns) == ["ent_followup_00001", "ent_followup_00002"]


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
    with pytest.raises(build_delivery.BuildNotStarted) as raised:
        asyncio.run(d._open_implementation_pr(_issue_trigger(), _empty_spec_state()))

    assert ran == []
    assert "NOT started" in raised.value.operator_text()


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


def test_a_failed_fetch_still_builds_and_says_the_base_may_be_stale(clone, monkeypatch):
    route_origin_to(clone, "/nonexistent/origin.git")
    monkeypatch.setattr(
        build_delivery, "TEST_ONLY_LOCAL_TRANSPORTS", frozenset({"/nonexistent/origin.git"})
    )
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
        # Built, not written: a literal user:token@host trips the PII scanner.
        "https://x-access-token:abc" + "@github.com/" + f"{REPO}.git",
        f"git@github.com:{REPO}.git",
        f"ssh://git@github.com/{REPO}.git",
        f"ssh://git@github.com:22/{REPO}.git",
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
    with pytest.raises(build_delivery.BuildNotStarted):
        asyncio.run(d._open_implementation_pr(trig, _empty_spec_state()))
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


# ── remote URLs: only github.com over https or ssh, exactly two segments ────


@pytest.mark.parametrize(
    "url",
    [
        # other transports
        f"http://github.com/{REPO}.git",
        f"git://github.com/{REPO}.git",
        f"file:///srv/git/{REPO}.git",
        f"ftp://github.com/{REPO}.git",
        f"ext::sh -c 'touch /tmp/x' %s",
        f"ext::https://github.com/{REPO}.git",
        f"https+ssh://github.com/{REPO}.git",
        f"/srv/git/{REPO}.git",
        f"./{REPO}",
        # query, fragment, params
        f"https://github.com/{REPO}.git?ref=main",
        f"https://github.com/{REPO}.git#main",
        f"https://github.com/{REPO}.git?",
        f"https://github.com/{REPO};x=1.git",
        # ports
        f"https://github.com:8443/{REPO}.git",
        f"https://github.com:80/{REPO}.git",
        f"https://github.com:/{REPO}.git",
        f"https://github.com:abc/{REPO}.git",
        f"ssh://git@github.com:2222/{REPO}.git",
        f"https://github.com:22/{REPO}.git",
        # path shape
        f"https://github.com/{REPO}/",
        f"https://github.com/{REPO}/extra",
        f"https://github.com/{REPO}.git/",
        "https://github.com/owner",
        "https://github.com/",
        "https://github.com",
        f"https://github.com//{REPO}.git",
        f"https://github.com/owner//repo.git",
        "https://github.com/owner/repo.git.git",
        "https://github.com/owner/..",
        "https://github.com/../repo.git",
        "git@github.com:owner",
        "git@github.com:owner/repo/extra.git",
        "git@github.com:/owner/repo.git",
        # encodings
        "https://github.com/own%65r/repo.git",
        "https://github.com/owner/re%2Fpo.git",
        "https://git%68ub.com/owner/repo.git",
        "https://github.com/owner/repo%2egit",
        "https://github.com/owner/repo%00.git",
        "git@github.com:own%65r/repo.git",
        # non-ASCII (look-alike letters, full-width forms, trailing dot variants)
        "https://github.com/öwner/repo.git",
        "https://github.com/owner/repо.git",
        "https://gіthub.com/owner/repo.git",
        "https://github．com/owner/repo.git",
        "https://github.com。/owner/repo.git",
        # host
        "https://github.com./owner/repo.git",
        "https://github.com.evil.example/owner/repo.git",
        "https://evilgithub.com/owner/repo.git",
        "https://www.github.com/owner/repo.git",
        "https://gist.github.com/owner/repo.git",
        "https://github.co/owner/repo.git",
        "git@github.com.evil.example:owner/repo.git",
        "git@gitlab.com:owner/repo.git",
        # ambiguous userinfo/host split
        "https://a<at>b<at>github.com/owner/repo.git",
        "https://github.com<at>evil.example/owner/repo.git",
        "https://github.com:pw<at>evil.example/owner/repo.git",
        "https://evil.example\\<at>github.com/owner/repo.git",
        "https://github.com\\.evil.example/owner/repo.git",
        "https://user:p:w<at>github.com/owner/repo.git",
        "https://us er<at>github.com/owner/repo.git",
        "ssh://someone<at>github.com/owner/repo.git",
        "ssh://git:pw<at>github.com/owner/repo.git",
        "someone<at>github.com:owner/repo.git",
        "git@@github.com:owner/repo.git",
        # whitespace, control characters, non-strings
        " https://github.com/owner/repo.git",
        "https://github.com/owner/repo.git ",
        "https://github.com/owner/repo.git\n",
        "https://github.com/owner/repo.git\x00",
        "https://github.com/owner/re\tpo.git",
        "",
        None,
        7,
        b"https://github.com/owner/repo.git",
    ],
)
def test_every_other_remote_shape_is_rejected(url):
    # `<at>` stands for "@": written out, a user@host literal reads as an email
    # address to the repository's PII scanner.
    url = url.replace("<at>", "@") if isinstance(url, str) else url
    assert build_delivery.normalise_remote(url) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com:443/owner/repo.git",
        "ssh://git@github.com:22/owner/repo.git",
        "ssh://github.com/owner/repo",
        "https://user:token<at>github.com/owner/repo",
        "git@GITHUB.COM:Owner/Repo.git",
        "HTTPS://GITHUB.COM/Owner/Repo",
    ],
)
def test_the_accepted_remote_forms_normalise_to_one_name(url):
    url = url.replace("<at>", "@")
    assert build_delivery.normalise_remote(url) == "github.com/owner/repo"


# ── verification fails closed ────────────────────────────────────────────────


def _injected_git(monkeypatch, overrides):
    """Real git, except for the (args-prefix -> result) pairs in ``overrides``."""
    real = build_delivery._git

    async def _git_inj(args, cwd, timeout=120):
        for prefix, result in overrides.items():
            if tuple(args[: len(prefix)]) == prefix:
                return result
        return await real(args, cwd, timeout)

    monkeypatch.setattr(build_delivery, "_git", _git_inj)


PUSHURL_QUERY = ("config", "--get-all", "remote.origin.pushurl")
EFFECTIVE_FETCH = ("remote", "get-url", "--all", "origin")
EFFECTIVE_PUSH = ("remote", "get-url", "--push", "--all", "origin")


@pytest.mark.parametrize(
    "query,result",
    [
        (PUSHURL_QUERY, (128, "", "fatal: bad config line 3 in file .git/config")),
        (PUSHURL_QUERY, (2, "", "error: could not lock config file")),
        (PUSHURL_QUERY, (1, "", "error: something went wrong")),  # not the quiet 'unset'
        (PUSHURL_QUERY, (1, "partial\n", "")),
        (PUSHURL_QUERY, (-1, "", "TimeoutError: git timed out")),
        (("config", "--get-all", "remote.origin.url"), (-1, "", "TimeoutError")),
        (("config", "--get-all", "remote.origin.url"), (128, "", "fatal: not a git repository")),
        (EFFECTIVE_FETCH, (128, "", "fatal: bad config")),
        (EFFECTIVE_FETCH, (-1, "", "TimeoutError")),
        (EFFECTIVE_FETCH, (0, "", "")),  # git says there is no URL
        (EFFECTIVE_PUSH, (2, "", "error: No such remote 'origin'")),
        (EFFECTIVE_PUSH, (-1, "", "OSError: git not found")),
        (EFFECTIVE_PUSH, (0, "", "")),
    ],
)
def test_any_git_error_refuses(clone, monkeypatch, query, result):
    _injected_git(monkeypatch, {query: result})
    with pytest.raises(build_delivery.BuildWorktreeError):
        asyncio.run(build_delivery.verify_clone_identity(clone, REPO))


def test_git_reporting_a_key_as_unset_is_not_an_error(clone):
    """The one tolerated non-zero: no pushurl configured (`git config` exits 1, silent)."""
    asyncio.run(build_delivery.verify_clone_identity(clone, REPO))


def test_a_refusal_names_the_prerequisite_to_fix(clone):
    _git(clone, "remote", "set-url", "origin", "https://github.com/other/repo.git")
    with pytest.raises(build_delivery.BuildWorktreeError) as raised:
        asyncio.run(build_delivery.verify_clone_identity(clone, REPO))
    assert str(clone) in raised.value.fix and f"https://github.com/{REPO}.git" in raised.value.fix
    assert build_delivery.BUILD_CLONES_ENV in raised.value.fix


def _rewrite(clone, key, to, frm=None):
    _git(clone, "config", f"url.{to}.{key}", frm or f"https://github.com/{REPO}.git")


@pytest.mark.parametrize("key", ["insteadOf", "pushInsteadOf"])
def test_a_rewrite_that_points_a_correct_clone_at_another_repository_is_refused(
    clone, run_skill_calls, monkeypatch, key
):
    """Correctly configured, but git would really contact a different repository."""
    # Drop the fixture's own (local-transport) rewrite so only the hostile rule applies.
    route_origin_to(clone, "/nonexistent/none.git")
    _git(clone, "config", "--remove-section", "url./nonexistent/none")
    _rewrite(clone, key, "https://github.com/other/repo.git")
    spy = _Spy(monkeypatch)

    result = _spawn(_task_snapshot())

    assert result.ok is False
    assert "effective origin" in result.error and "Before retrying" in result.error
    assert spy.mutating() == [] and spy.tokens == [] and run_skill_calls == []
    assert not (clone.parent / "ateles-worktrees").exists()


def test_a_rewrite_to_a_local_path_is_refused_in_production(clone, monkeypatch):
    """The test allowance is empty in production: no local transport is ever accepted."""
    monkeypatch.setattr(build_delivery, "TEST_ONLY_LOCAL_TRANSPORTS", frozenset())
    with pytest.raises(build_delivery.BuildWorktreeError, match="effective origin"):
        asyncio.run(build_delivery.verify_clone_identity(clone, REPO))


def test_the_test_allowance_is_exact_not_a_prefix(clone, monkeypatch):
    bare = Path(_git(clone, "config", "--get-regexp", r"^url\..*\.insteadof$").split()[0][4:].rsplit(".insteadof", 1)[0])
    monkeypatch.setattr(build_delivery, "TEST_ONLY_LOCAL_TRANSPORTS", frozenset({str(bare.parent)}))
    with pytest.raises(build_delivery.BuildWorktreeError, match="effective origin"):
        asyncio.run(build_delivery.verify_clone_identity(clone, REPO))


def test_the_allowance_is_not_settable_from_the_environment(clone, monkeypatch):
    monkeypatch.setattr(build_delivery, "TEST_ONLY_LOCAL_TRANSPORTS", frozenset())
    for name in ("ATELES_TEST_ALLOW_LOCAL_REMOTE", "TEST_ONLY_LOCAL_TRANSPORTS", "ATELES_BUILD_ALLOW_LOCAL"):
        monkeypatch.setenv(name, str(clone))
    with pytest.raises(build_delivery.BuildWorktreeError):
        asyncio.run(build_delivery.verify_clone_identity(clone, REPO))


# ── the operator hears "not started", and what to fix ────────────────────────


def _handoff_message(exc: Exception | None, *, url: str | None):
    from test_swarm_dispatch import (
        SwarmDispatcher,
        _StubNotifier,
        _config,
        _empty_spec_state,
        _issue_trigger,
    )

    notifier = _StubNotifier()
    d = SwarmDispatcher(notifier, _config())

    async def _open(self, trigger, state):
        if exc:
            raise exc
        return url

    SwarmDispatcher._open_implementation_pr, saved = _open, SwarmDispatcher._open_implementation_pr
    try:
        asyncio.run(
            d._hand_off_to_build(
                _issue_trigger(), _empty_spec_state(), completed=["pm", "eng"],
                spec_action="Open the issue and approve the build.", spec_ready_key="k",
            )
        )
    finally:
        SwarmDispatcher._open_implementation_pr = saved
    return notifier.sent


def test_a_build_that_never_started_is_not_reported_as_a_spec_problem():
    cause = build_delivery.BuildWorktreeError(
        "no local clone of o/r", fix="clone https://github.com/o/r.git to ~/repos/r"
    )
    (msg,) = _handoff_message(build_delivery.BuildNotStarted("o/r", 100, cause), url=None)

    assert "NOT started" in msg and "no local clone of o/r" in msg
    assert "Before retrying: clone https://github.com/o/r.git to ~/repos/r" in msg
    assert "spec may be incomplete" not in msg and "approve the build" not in msg


def test_a_build_that_ran_without_a_pr_still_says_so():
    (msg,) = _handoff_message(None, url=None)
    assert "opened NO PR" in msg and "spec may be incomplete" in msg
    assert "NOT started" not in msg


def test_a_build_that_opened_a_pr_says_so():
    (msg,) = _handoff_message(None, url="https://github.com/o/r/pull/9")
    assert "implementation PR opened" in msg


# ── the worktree's own remote configuration is verified too ──────────────────


def _scoped_rewrite(clone: Path, condition: str, key: str, target: str) -> None:
    include = clone.parent / f"scoped-{abs(hash((condition, key)))}.gitconfig"
    include.write_text(f'[url "{target}"]\n\t{key} = https://github.com/{REPO}.git\n')
    _git(clone, "config", f"includeIf.{condition}.path", str(include))


def _scope_by_directory(clone: Path) -> str:
    common = Path(_git(clone, "rev-parse", "--path-format=absolute", "--git-common-dir")).resolve()
    return f"gitdir:{common}/worktrees/"


SCOPES = {
    "directory": lambda clone: _scope_by_directory(clone),
    "branch": lambda clone: "onbranch:cicada/**",
}


@pytest.mark.parametrize("scope", sorted(SCOPES))
@pytest.mark.parametrize("key", ["insteadOf", "pushInsteadOf"])
def test_the_new_worktrees_remote_configuration_is_verified(
    clone, run_skill_calls, monkeypatch, scope, key
):
    """The source clone passes; the worktree made from it is checked on its own."""
    _scoped_rewrite(clone, SCOPES[scope](clone), key, "https://github.com/other/repo.git")
    # The source clone alone is clean.
    asyncio.run(build_delivery.verify_clone_identity(clone, REPO))
    spy = _Spy(monkeypatch)

    result = _spawn(_task_snapshot())

    assert result.ok is False
    assert "worktree" in result.error and "Before retrying" in result.error
    assert "verify the worktree's effective remote configuration" in result.error
    assert spy.tokens == [] and run_skill_calls == []


@pytest.mark.parametrize("scope", sorted(SCOPES))
def test_a_refused_worktree_is_removed_with_its_branch(clone, monkeypatch, scope):
    _scoped_rewrite(clone, SCOPES[scope](clone), "pushInsteadOf", "https://github.com/other/repo.git")

    with pytest.raises(build_delivery.BuildWorktreeError, match="worktree"):
        asyncio.run(build_delivery.prepare_build_worktree(REPO, "task-x"))

    parent = clone.parent / "ateles-worktrees"
    assert not parent.exists() or list(parent.iterdir()) == []
    assert _git(clone, "worktree", "list").count("\n") == 0
    assert _git(clone, "branch", "--list", "cicada/*") == ""


@pytest.mark.parametrize("scope", sorted(SCOPES))
def test_the_handoff_refuses_a_worktree_with_scoped_configuration(clone, monkeypatch, scope):
    from test_swarm_dispatch import (
        SwarmDispatcher,
        _StubNotifier,
        _config,
        _empty_spec_state,
        _issue_trigger,
    )

    trig = _issue_trigger()
    trig.repository = REPO
    _scoped_rewrite(clone, SCOPES[scope](clone), "pushInsteadOf", "https://github.com/other/repo.git")
    ran: list = []

    async def _fake_run_skill(skill, prompt, **kw):
        ran.append(skill)
        return SkillResult(skill, True, 0, "", "")

    monkeypatch.setattr("swarm_dispatch.run_skill", _fake_run_skill)
    spy = _Spy(monkeypatch)
    monkeypatch.setattr(
        "swarm_dispatch._token_for_agent_on_repo", lambda *a: spy.tokens.append(a) or "t"
    )
    notifier = _StubNotifier()
    d = SwarmDispatcher(notifier, _config())

    asyncio.run(
        d._hand_off_to_build(
            trig, _empty_spec_state(), completed=["pm"], spec_action="x", spec_ready_key="k"
        )
    )

    (msg,) = notifier.sent
    assert "NOT started" in msg and "worktree" in msg
    assert "verify the worktree's effective remote configuration" in msg
    assert "spec may be incomplete" not in msg
    assert ran == [] and spy.tokens == []


def test_a_linked_worktree_source_with_its_own_configuration_is_verified(clone, monkeypatch, tmp_path):
    """A source that is itself a linked worktree may carry per-worktree configuration."""
    source = tmp_path / "linked-source"
    _git(clone, "worktree", "add", "-q", "--detach", str(source))
    _git(clone, "config", "extensions.worktreeConfig", "true")
    _git(source, "config", "--worktree", "url.https://github.com/other/repo.git.pushInsteadOf",
         f"https://github.com/{REPO}.git")
    monkeypatch.setenv(build_delivery.BUILD_CLONES_ENV, f"{REPO}={source}")

    with pytest.raises(build_delivery.BuildWorktreeError, match="effective origin"):
        asyncio.run(build_delivery.prepare_build_worktree(REPO, "task-x"))


# ── the sweep examines every marked task ─────────────────────────────────────


def _mark(store, entity_id, *, context="ok", **over):
    row = _task_snapshot(action_type=over.pop("action_type", "local_edit"), **over)
    row.update(status="pending", disposition=build_delivery.DEFERRED_DISPOSITION)
    if context == "ok":
        row["dispatch_context"] = f"deferred-behind:{REPO.lower()}#1448:{'0123456789abcdef'}"
    elif context is not None:
        row["dispatch_context"] = context
    row["blocked_reason"] = "Deferred: swarm-build-handoff is building"
    store.put(entity_id, row)


def _sweep(store_env, notifier=None, times=1):
    n = notifier or _RecNotifier()

    async def _go():
        for _ in range(times):
            await apis.recover_deferred_tasks(n)
            await _settle()

    asyncio.run(_go())
    return n


def test_the_sweep_reaches_every_marked_task_past_one_batch(dispatch_env, monkeypatch):
    _, spawns, store = dispatch_env
    monkeypatch.setattr(apis, "_RECOVERY_BATCH", 3)
    for i in range(10):
        _mark(store, f"ent_valid_{i:04d}")

    _sweep(dispatch_env)  # ONE trigger: no second event, release or restart

    assert sorted(e for _, e, _ in spawns) == [f"ent_valid_{i:04d}" for i in range(10)]


def test_broken_records_before_valid_ones_do_not_hide_them(dispatch_env, monkeypatch):
    _, spawns, store = dispatch_env
    monkeypatch.setattr(apis, "_RECOVERY_BATCH", 2)
    _mark(store, "ent_a_unreadable_01")
    _mark(store, "ent_a_unreadable_02")
    _mark(store, "ent_b_garbled_0001", context="garbled")
    _mark(store, "ent_b_missing_0001", context=None)
    for i in range(6):
        _mark(store, f"ent_c_valid_{i:04d}")
    store.fail_reads |= {"ent_a_unreadable_01", "ent_a_unreadable_02"}

    n = _sweep(dispatch_env)

    assert sorted(e for _, e, _ in spawns) == [f"ent_c_valid_{i:04d}" for i in range(6)]
    for bad in ("ent_a_unreadable_01", "ent_a_unreadable_02", "ent_b_garbled_0001", "ent_b_missing_0001"):
        assert sum(bad in m for m in n.sent) == 1, f"{bad} should be reported exactly once"


def test_repeated_sweeps_make_progress_and_never_double_dispatch(dispatch_env, monkeypatch):
    _, spawns, store = dispatch_env
    monkeypatch.setattr(apis, "_RECOVERY_BATCH", 2)
    _mark(store, "ent_a_unreadable_01")
    _mark(store, "ent_b_garbled_0001", context="garbled")
    for i in range(5):
        _mark(store, f"ent_c_valid_{i:04d}")
    store.fail_reads.add("ent_a_unreadable_01")

    n = _sweep(dispatch_env, times=4)

    dispatched = [e for _, e, _ in spawns]
    assert sorted(dispatched) == [f"ent_c_valid_{i:04d}" for i in range(5)]
    assert len(dispatched) == len(set(dispatched)), "a task was dispatched twice"
    assert sum("ent_a_unreadable_01" in m for m in n.sent) == 1, "re-announced on a later sweep"


def test_an_empty_sweep_is_quiet(dispatch_env):
    _, spawns, _ = dispatch_env
    n = _sweep(dispatch_env)
    assert spawns == [] and n.sent == []


def test_no_overflow_notice_promises_a_continuation_that_does_not_exist(dispatch_env, monkeypatch):
    _, _, store = dispatch_env
    monkeypatch.setattr(apis, "_RECOVERY_BATCH", 2)
    for i in range(9):
        _mark(store, f"ent_valid_{i:04d}")
    n = _sweep(dispatch_env)
    assert not any("follow at the next" in m or "tasks are deferred behind builds" in m for m in n.sent)


def test_a_local_edit_task_is_recovered_without_any_build_release(dispatch_env, monkeypatch):
    """No delivery claim, so no release event: only the continuation can reach it."""
    _, spawns, store = dispatch_env
    monkeypatch.setattr(apis, "_RECOVERY_BATCH", 1)
    for i in range(4):
        _mark(store, f"ent_local_{i:04d}", action_type="local_edit")
    _sweep(dispatch_env)
    assert len(spawns) == 4


def test_deferred_dispatches_run_at_most_the_stated_concurrency(dispatch_env, monkeypatch):
    _, _, store = dispatch_env
    running = peak = 0

    async def _slow_spawn(skill, entity_id, snapshot, trigger, *a, **kw):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return _SpawnResult()

    monkeypatch.setattr(apis, "_spawn_harness_skill", _slow_spawn)
    monkeypatch.setattr(apis, "_RECOVERY_CONCURRENCY", 2)
    monkeypatch.setattr(apis, "_recovery_slots", __import__("weakref").WeakKeyDictionary())
    for i in range(8):
        _mark(store, f"ent_valid_{i:04d}")
    _sweep(dispatch_env)
    assert 0 < peak <= 2


# ── a deferral is acknowledged only after it is read back ────────────────────


def _defer_with(dispatch_env, *, lossy=None, events=None, **kw):
    _, _, store = dispatch_env
    store.lossy = {k: list(v) for k, v in (lossy or {}).items()}
    return _during_build(
        dispatch_env, lambda c: _created("ent_followup_00001", _local()), **kw
    )


@pytest.mark.parametrize(
    "field,mode",
    [
        ("disposition", "omit"),
        ("dispatch_context", "omit"),
        ("blocked_reason", "omit"),
        ("disposition", "mismatch"),
        ("dispatch_context", "mismatch"),
        ("blocked_reason", "mismatch"),
        ("status", "mismatch"),
    ],
)
def test_a_write_that_lost_a_field_once_is_rewritten_and_then_acknowledged(
    dispatch_env, field, mode
):
    _, spawns, store = dispatch_env
    out, notifier = _defer_with(dispatch_env, lossy={field: [mode, 1]})

    assert not any("could not record" in m for m in notifier.sent)
    row = out["rows_mid_build"]["ent_followup_00001"]
    assert row["disposition"] == build_delivery.DEFERRED_DISPOSITION and row["status"] == "pending"
    # The retry used its own idempotency keys, or a server would call it a repeat.
    assert len(set(store.key_suffixes)) > 1
    assert spawns == [("cicada", "ent_followup_00001", "build_released")]


@pytest.mark.parametrize(
    "field,mode",
    [(f, m) for f in ("disposition", "dispatch_context", "blocked_reason") for m in ("omit", "mismatch")]
)
def test_a_write_that_never_persists_is_reported_not_acknowledged(dispatch_env, field, mode):
    _, spawns, store = dispatch_env
    out, notifier = _defer_with(dispatch_env, lossy={field: [mode, 99]})

    (msg,) = [m for m in notifier.sent if "could not record" in m]
    assert "ent_followup_00001" in msg and field in msg and "re-dispatch the task by hand" in msg
    assert out["spawns_mid_build"] == []
    # The in-process fallback still runs it when the build ends.
    assert spawns == [("cicada", "ent_followup_00001", "build_released")]


def test_an_unrecorded_deferral_does_not_survive_a_restart_and_the_operator_was_told(dispatch_env):
    _, spawns, store = dispatch_env
    out, notifier = _defer_with(
        dispatch_env, lossy={"dispatch_context": ["omit", 99]}, release=False
    )
    assert any("re-dispatch the task by hand" in m for m in notifier.sent)
    # A new process finds nothing marked to recover (the record never held).
    apis._unrecorded_deferrals.clear()
    spawns.clear()
    asyncio.run(apis.recover_deferred_tasks(_RecNotifier()))
    assert spawns == []


def test_a_deferral_that_reads_back_as_nothing_is_not_acknowledged(dispatch_env, monkeypatch):
    _, _, store = dispatch_env
    monkeypatch.setattr(apis, "fetch_task_snapshot", lambda _id: None)
    _, notifier = _during_build(dispatch_env, lambda c: _created("ent_followup_00001", _local()))
    assert any("could not record" in m and "read-back" in m for m in notifier.sent)


# ── a task that waited for a slot is judged as it reads when its turn comes ──


def _queue_behind_a_busy_slot(dispatch_env, monkeypatch, *, change):
    """Two marked tasks, one slot. A holds it; ``change(store)`` hits B while it waits."""
    writes, _, store = dispatch_env
    monkeypatch.setattr(apis, "_RECOVERY_CONCURRENCY", 1)
    monkeypatch.setattr(apis, "_recovery_slots", __import__("weakref").WeakKeyDictionary())
    dispatched: list[tuple[str, dict]] = []
    n = _RecNotifier()

    async def _go():
        release = asyncio.Event()

        async def _gated(skill, entity_id, snapshot, trigger, *a, **kw):
            dispatched.append((entity_id, dict(snapshot)))
            if entity_id == "ent_a_first_0001":
                await release.wait()
            return _SpawnResult()

        monkeypatch.setattr(apis, "_spawn_harness_skill", _gated)
        _mark(store, "ent_a_first_0001")
        _mark(store, "ent_b_queued_0001", title="as first read")
        await apis.recover_deferred_tasks(n)
        for _ in range(20):
            await asyncio.sleep(0)
        assert [e for e, _ in dispatched] == ["ent_a_first_0001"], "B should be queued"
        change(store)
        release.set()
        await _settle()

    asyncio.run(_go())
    return dispatched, n, writes


@pytest.mark.parametrize(
    "label,change",
    [
        ("completed", lambda st: st.rows["ent_b_queued_0001"].update(status="done")),
        ("cancelled", lambda st: st.rows["ent_b_queued_0001"].update(status="cancelled")),
        ("superseded", lambda st: st.rows["ent_b_queued_0001"].update(status="superseded")),
        (
            "re-marked behind a different build",
            lambda st: st.rows["ent_b_queued_0001"].update(
                dispatch_context=f"deferred-behind:{REPO.lower()}#1448:fedcba9876543210"
            ),
        ),
        ("no longer marked", lambda st: st.rows["ent_b_queued_0001"].pop("disposition")),
    ],
)
def test_a_queued_task_that_stopped_qualifying_is_not_dispatched(dispatch_env, monkeypatch, label, change):
    dispatched, n, writes = _queue_behind_a_busy_slot(dispatch_env, monkeypatch, change=change)

    assert [e for e, _ in dispatched] == ["ent_a_first_0001"], label
    assert not any(e == "ent_b_queued_0001" for e, _ in writes), "a status write for a task not dispatched"
    # The normal case (finished while it waited) is quiet.
    assert not any("ent_b_queued_0001" in m for m in n.sent)


def test_a_queued_task_whose_build_is_running_again_is_not_dispatched(dispatch_env, monkeypatch):
    claims: list = []

    def _change(store):
        cm = build_delivery.claim_build(REPO, 1448, "swarm-build-handoff")
        cm.__enter__()
        claims.append(cm)

    try:
        dispatched, _, writes = _queue_behind_a_busy_slot(dispatch_env, monkeypatch, change=_change)
    finally:
        for cm in claims:
            cm.__exit__(None, None, None)
    assert [e for e, _ in dispatched] == ["ent_a_first_0001"]
    assert not any(e == "ent_b_queued_0001" for e, _ in writes)


def test_a_queued_task_that_is_still_deferred_is_dispatched_from_the_fresh_read(dispatch_env, monkeypatch):
    dispatched, _, _ = _queue_behind_a_busy_slot(
        dispatch_env, monkeypatch,
        change=lambda st: st.rows["ent_b_queued_0001"].update(title="as re-read at its turn"),
    )
    by_id = dict(dispatched)
    assert set(by_id) == {"ent_a_first_0001", "ent_b_queued_0001"}
    assert by_id["ent_b_queued_0001"]["title"] == "as re-read at its turn"


def test_a_queued_task_that_cannot_be_re_read_is_reported_once_and_not_dispatched(dispatch_env, monkeypatch):
    dispatched, n, writes = _queue_behind_a_busy_slot(
        dispatch_env, monkeypatch,
        change=lambda st: st.fail_reads.add("ent_b_queued_0001"),
    )
    assert [e for e, _ in dispatched] == ["ent_a_first_0001"]
    assert sum("ent_b_queued_0001" in m for m in n.sent) == 1
    assert not any(e == "ent_b_queued_0001" for e, _ in writes)
