"""
Tests for retrying a prepare agent that spawned but never produced an RC.

## The bug these cover

`spawn_prepare_agent` uses `Popen`, so it returns as soon as the child process
starts — its `True` means "the agent launched", never "the agent produced a
release candidate". The old code stamped the per-commit lock immediately after
that call, which is a claim about an outcome that has not happened yet.

That claim is unrecoverable. A stamped SHA suppresses every later run for the
same head, so an agent dying mid-flight left the release permanently unprepared
with nothing to retry it.

Observed 2026-08-08: the agent spawned for neotoma `78dbcbefe` died on
`API Error: Unable to connect to API (ENOTFOUND)`. Every run afterwards logged
"Already ran for origin/main 78dbcbefe" while no RC PR existed, and the merged
fix never reached a release.

## What replaces it

Spawning leaves the lock unstamped (`transient=True`), so a crashed agent is
retried. The real idempotency guard is the in-flight `release_result` check:
once an agent succeeds, that gate short-circuits and stamps the SHA itself.

Retrying is bounded by `MAX_SPAWNS_PER_HEAD` — an agent dying for a reason
retrying cannot fix (no credits, missing agent_grant, bad prompt) must surface
to the operator rather than respawn on every webhook.

Run: pytest execution/daemons/phoenicurus-release/test_prepare_retry_on_crash.py -v
"""

from __future__ import annotations

import pytest

import prepare


SHA = "b" * 40
OTHER_SHA = "c" * 40


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Point every lock/counter file at a temp dir so tests never touch real state."""
    monkeypatch.setattr(prepare, "MERGE_STATE_FILE", tmp_path / ".sha")
    monkeypatch.setattr(prepare, "STATE_FILE", tmp_path / ".day")
    monkeypatch.setattr(prepare, "SPAWN_COUNT_FILE", tmp_path / ".spawns")
    monkeypatch.setattr(prepare, "SPAWN_PID_FILE", tmp_path / ".pid")
    monkeypatch.setattr(prepare, "STALE_ESCALATION_FILE", tmp_path / ".stale")
    return tmp_path


# ---------------------------------------------------------------------------
# Spawn counter
# ---------------------------------------------------------------------------


def test_spawn_count_starts_at_zero(isolated_state):
    assert prepare._spawn_count(SHA) == 0


def test_record_spawn_increments_for_same_head(isolated_state):
    assert prepare._record_spawn(SHA) == 1
    assert prepare._record_spawn(SHA) == 2
    assert prepare._spawn_count(SHA) == 2


def test_spawn_count_is_per_head(isolated_state):
    prepare._record_spawn(SHA)
    prepare._record_spawn(SHA)
    # A different head starts fresh: attempts are not global, or one flaky
    # release would poison the budget for every release after it.
    assert prepare._spawn_count(OTHER_SHA) == 0


def test_record_spawn_resets_when_head_moves(isolated_state):
    prepare._record_spawn(SHA)
    prepare._record_spawn(SHA)
    assert prepare._record_spawn(OTHER_SHA) == 1


def test_spawn_count_survives_unreadable_file(isolated_state, monkeypatch):
    # A corrupt counter must not wedge releases — degrade to "no attempts yet"
    # and let the run proceed rather than raising.
    (isolated_state / ".spawns").write_text("not-a-count")
    assert prepare._spawn_count(SHA) == 0


# ---------------------------------------------------------------------------
# The lock itself — the actual #78dbcbefe regression
# ---------------------------------------------------------------------------


@pytest.fixture
def ready_to_spawn(isolated_state, monkeypatch):
    """
    Drive `run_prepare` to the point where it spawns, with every gate green.

    This must go through `run_prepare` rather than calling `_mark_ran` directly:
    the defect was never in `_mark_ran`, it was in the CALL SITE choosing to
    stamp unconditionally after a fire-and-forget spawn. A test that asserts on
    `_mark_ran(transient=True)` passes against the buggy code, because it is
    testing the helper the buggy code declined to use.
    """
    monkeypatch.setattr(prepare, "_head_sha", lambda: SHA)
    monkeypatch.setattr(prepare, "latest_tag", lambda: "v1.0.0")
    monkeypatch.setattr(prepare, "unreleased_commit_count", lambda _tag: 5)
    monkeypatch.setattr(prepare, "existing_release_status", lambda *_a: None)
    monkeypatch.setattr(prepare, "main_ci_green", lambda: True)
    monkeypatch.setattr(prepare, "notify_operator", lambda *_a, **_k: None)
    monkeypatch.setattr(prepare, "subprocess", _NoopSubprocess())

    spawns: list[str] = []

    def _fake_spawn(tag, count, dry_run, head=""):
        spawns.append(tag)
        return True  # Popen succeeded — says nothing about the agent's outcome

    monkeypatch.setattr(prepare, "spawn_prepare_agent", _fake_spawn)
    # Default: no peer running, so existing tests exercise the spawn path.
    monkeypatch.setattr(prepare, "_live_peer_pid", lambda _h: None)
    monkeypatch.setattr(
        prepare, "NEOTOMA_REPO_ROOT", _FakeRepoRoot(isolated_state), raising=False
    )
    return spawns


class _NoopSubprocess:
    """Swallow the `git fetch` run_prepare does before checking the head."""

    @staticmethod
    def run(*_a, **_k):
        return None


class _FakeRepoRoot:
    """A repo root whose package.json always 'exists'."""

    def __init__(self, tmp_path):
        self._tmp = tmp_path
        (tmp_path / "package.json").write_text("{}")

    def __truediv__(self, other):
        return self._tmp / other


def test_spawning_does_not_lock_the_head(ready_to_spawn):
    """
    The regression, driven through `run_prepare`.

    A spawn must leave the head retryable: at spawn time we do not know whether
    the agent will produce anything, and if it dies the head must still be
    preparable. Against the pre-fix code this fails — it stamped the lock right
    after the spawn call, so `78dbcbefe` became permanently unpreparable.
    """
    rc = prepare.run_prepare(dry_run=False, force=False, on_merge=True)
    assert rc == 0
    assert ready_to_spawn == ["v1.0.0"], "expected exactly one spawn"
    assert not prepare._already_ran_for_sha(SHA), (
        "spawning stamped the per-commit lock; a crashed agent would never be "
        "retried (the 78dbcbefe failure)"
    )


def test_crashed_agent_is_retried_on_the_next_run(ready_to_spawn):
    """
    Two runs over the same head with no release_result between them must spawn
    twice. This is the behaviour the outage needed and did not have.
    """
    prepare.run_prepare(dry_run=False, force=False, on_merge=True)
    prepare.run_prepare(dry_run=False, force=False, on_merge=True)
    assert len(ready_to_spawn) == 2, (
        "a head with no release_result must be retried, not treated as done"
    )


def test_successful_agent_stops_further_spawns(ready_to_spawn, monkeypatch):
    """
    Once an agent succeeds, the in-flight `release_result` gate short-circuits
    and stamps the SHA — so the unstamped lock does not mean "spawn forever".
    """
    prepare.run_prepare(dry_run=False, force=False, on_merge=True)
    assert len(ready_to_spawn) == 1

    monkeypatch.setattr(
        prepare,
        "existing_release_status",
        lambda *_a: ("pending_approval", "ent_fake", "v1.0.0", None),
    )
    prepare.run_prepare(dry_run=False, force=False, on_merge=True)

    assert len(ready_to_spawn) == 1, "an RC already exists; must not spawn another"
    assert prepare._already_ran_for_sha(SHA), (
        "the in-flight gate is the terminal outcome and must stamp the lock"
    )


def test_spawning_stops_after_the_budget_is_spent(ready_to_spawn, monkeypatch):
    """An agent that keeps dying must surface to the operator, not respawn forever."""
    monkeypatch.setattr(prepare, "MAX_SPAWNS_PER_HEAD", 2)
    notices: list[str] = []
    monkeypatch.setattr(prepare, "notify_operator", lambda msg: notices.append(msg))

    for _ in range(4):
        prepare.run_prepare(dry_run=False, force=False, on_merge=True)

    assert len(ready_to_spawn) == 2, "must stop spawning once the budget is spent"
    assert notices, "exhausting the budget must tell the operator"
    assert prepare._already_ran_for_sha(SHA), "give up cleanly rather than looping"


def test_dry_run_on_exhausted_budget_has_no_side_effects(ready_to_spawn, monkeypatch):
    """
    A dry run must be observable-only, even on the give-up path.

    Loxia flagged this on ateles#401: the budget-exhaustion branch originally
    ran `notify_operator` and `_mark_ran` outside the `not dry_run` guard that
    every other mutation in `run_prepare` sits behind. So a diagnostic
    `--dry-run` would page the operator and stamp the lock — silently retiring a
    head that still needed preparing, which is the same class of bug this fix
    exists to remove.
    """
    monkeypatch.setattr(prepare, "MAX_SPAWNS_PER_HEAD", 1)
    notices: list[str] = []
    monkeypatch.setattr(prepare, "notify_operator", lambda msg: notices.append(msg))

    # Burn the budget for real, so the next call takes the give-up branch.
    prepare.run_prepare(dry_run=False, force=False, on_merge=True)
    notices.clear()

    prepare.run_prepare(dry_run=True, force=False, on_merge=True)

    assert notices == [], "a dry run must not page the operator"
    assert not prepare._already_ran_for_sha(SHA), (
        "a dry run must not stamp the per-commit lock — that would retire a head "
        "that still needs preparing"
    )


def test_terminal_outcome_still_locks_the_head(isolated_state):
    """The non-transient path must still stamp, or every run would re-spawn."""
    prepare._mark_ran(on_merge=True, head=SHA)
    assert prepare._already_ran_for_sha(SHA)


def test_scheduled_path_unaffected_by_transient(isolated_state):
    """
    The daily lock deliberately stamps even on a deferral — a same-day retry is
    not wanted there. Only the on-merge lock changes.
    """
    prepare._mark_ran(on_merge=False, head="", transient=True)
    assert prepare._already_ran_today()


# ---------------------------------------------------------------------------
# Bound
# ---------------------------------------------------------------------------


def test_budget_is_exhausted_after_max_spawns(isolated_state, monkeypatch):
    monkeypatch.setattr(prepare, "MAX_SPAWNS_PER_HEAD", 3)
    for _ in range(3):
        prepare._record_spawn(SHA)
    assert prepare._spawn_count(SHA) >= prepare.MAX_SPAWNS_PER_HEAD, (
        "a head that burned its attempts must be detectable, so the daemon can "
        "stop and notify instead of respawning on every webhook"
    )


def test_budget_not_exhausted_below_max(isolated_state, monkeypatch):
    monkeypatch.setattr(prepare, "MAX_SPAWNS_PER_HEAD", 3)
    prepare._record_spawn(SHA)
    assert prepare._spawn_count(SHA) < prepare.MAX_SPAWNS_PER_HEAD

# ---------------------------------------------------------------------------
# Deferral to a live peer must not spend a retry attempt
# ---------------------------------------------------------------------------


def test_deferring_to_a_live_peer_does_not_spend_an_attempt(ready_to_spawn, monkeypatch):
    """
    A spawned agent runs for minutes. When the daemon fires again in that window,
    the new run must defer to the live peer — and deferring is NOT an attempt.

    Observed 2026-08-09: attempts 2/3 and 3/3 were both deferrals (agents that
    correctly stood down rather than race a competing RC PR), the original agent
    then died, and the head was left with a spent budget and no release. Three
    "attempts", zero tries.
    """
    monkeypatch.setattr(prepare, "MAX_SPAWNS_PER_HEAD", 3)
    prepare.run_prepare(dry_run=False, force=False, on_merge=True)
    assert len(ready_to_spawn) == 1
    assert prepare._spawn_count(SHA) == 1

    # A peer is alive for this head (our own pid always is).
    monkeypatch.setattr(prepare, "_live_peer_pid", lambda h: 4242)

    prepare.run_prepare(dry_run=False, force=False, on_merge=True)
    prepare.run_prepare(dry_run=False, force=False, on_merge=True)

    assert len(ready_to_spawn) == 1, "must not spawn while a peer is working"
    assert prepare._spawn_count(SHA) == 1, (
        "deferring to a peer charged a retry attempt; three deferrals would "
        "exhaust the budget without a single agent having tried"
    )
    assert not prepare._already_ran_for_sha(SHA), (
        "deferral must leave the head retryable — the peer may still die"
    )


def test_dead_peer_does_not_block_a_retry(ready_to_spawn, monkeypatch):
    """Once the peer is gone, the head must become spawnable again."""
    monkeypatch.setattr(prepare, "MAX_SPAWNS_PER_HEAD", 3)
    prepare.run_prepare(dry_run=False, force=False, on_merge=True)

    monkeypatch.setattr(prepare, "_live_peer_pid", lambda h: None)
    prepare.run_prepare(dry_run=False, force=False, on_merge=True)

    assert len(ready_to_spawn) == 2, "a dead peer must not suppress the retry"


def test_live_peer_probe_reports_none_for_a_dead_pid(isolated_state, monkeypatch):
    """The liveness probe must not treat a stale pid file as a running agent."""
    monkeypatch.setattr(prepare, "SPAWN_PID_FILE", isolated_state / ".pid")
    (isolated_state / ".pid").write_text(f"{SHA} 999999")
    assert prepare._live_peer_pid(SHA) is None


def test_live_peer_probe_is_per_head(isolated_state, monkeypatch):
    """A peer working a DIFFERENT head must not block this one."""
    import os
    monkeypatch.setattr(prepare, "SPAWN_PID_FILE", isolated_state / ".pid")
    (isolated_state / ".pid").write_text(f"{OTHER_SHA} {os.getpid()}")
    assert prepare._live_peer_pid(SHA) is None
    assert prepare._live_peer_pid(OTHER_SHA) == os.getpid()


def test_scheduled_sweep_does_not_consult_the_peer_gate(ready_to_spawn, monkeypatch):
    """
    Pins the scope Loxia asked about on ateles#408.

    The sweep is rate-limited per calendar day, not per commit, so it passes
    head="" and has no key for a peer lookup. It therefore does NOT defer via
    this path — but it also cannot miscount, because `_record_spawn` is equally
    merge-only. Documenting the asymmetry so a future reader does not "fix" the
    docstring by wiring the sweep in without giving it a head to key on.
    """
    monkeypatch.setattr(prepare, "_live_peer_pid", lambda _h: 4242)
    monkeypatch.setattr(prepare, "_already_ran_today", lambda: False)

    prepare.run_prepare(dry_run=False, force=False, on_merge=False)

    assert len(ready_to_spawn) == 1, (
        "the sweep does not consult the peer gate — it has no head to key on"
    )
    assert prepare._spawn_count(SHA) == 0, (
        "and it must not touch the per-head counter either, or a sweep would "
        "silently spend a merge-path attempt"
    )
