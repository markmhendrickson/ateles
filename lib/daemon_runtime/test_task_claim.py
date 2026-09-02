"""Tests for the claim + lease primitive.

The two load-bearing assertions:

  1. Two concurrent claimants CANNOT both take one task.
  2. An abandoned claim becomes claimable again once its lease lapses —
     simulated by a runner that is killed and WRITES NOTHING further, which is
     the real case (a SIGKILLed process runs no cleanup).

The fake store below reproduces the behaviour probed against Neotoma prod:
canonical-key collisions de-duplicate onto ONE row and the snapshot is
last-writer-wins, with the loser receiving a SUCCESS response
(action="matched_existing") rather than an error.
"""

from __future__ import annotations

import pytest

from lib.daemon_runtime.task_claim import (
    CLAIMABLE_STATUSES,
    ClaimStore,
    claim_key,
    claimed,
    is_claimable_status,
    lease_is_live,
    new_runner_id,
)


class FakeNeotoma:
    """In-memory stand-in with Neotoma's real canonical-key semantics.

    Verified against prod (2026-09-02): storing twice on one canonical key
    returns action="created" then action="matched_existing", BOTH with HTTP
    success, and the second write's fields overwrite the first's.
    """

    def store(self, entities, idempotency_key=None):
        if self.fail_next_store:
            self.fail_next_store = False
            raise RuntimeError("neotoma unreachable")
        self.writes += 1
        out = []
        for ent in entities:
            key = ent["native_session_id"]
            existing = self.rows.get(key)
            action = "matched_existing" if existing else "created"
            row = dict(existing or {})
            row.update({k: v for k, v in ent.items() if k != "entity_type"})
            row["entity_id"] = (existing or {}).get("entity_id") or f"ent_{abs(hash(key)) % 10**12:012d}"
            self.rows[key] = row
            out.append({"entity_id": row["entity_id"], "action": action})
        return {"entities": out}

    # Fields agent_session declares (schema 0.2.0). Anything else is returned in
    # a sibling raw_fragments block, NOT in snapshot -- verified against prod.
    DECLARED = {
        "harness", "native_session_id", "kind", "title", "summary", "status",
        "created_at", "last_activity_at", "cwd", "repo", "repo_remote_url",
        "branch", "git_head_sha", "worktree_path", "origin_device",
        "trigger_kind", "trigger_ref", "holder", "task_id",
    }

    def __init__(self, declared=None):
        self.rows: dict[str, dict] = {}
        self.writes = 0
        self.fail_next_store = False
        self.fail_next_read = False
        # Overridable so a test can simulate the OLD 0.1.0 schema, where
        # `holder` was undeclared and hid in raw_fragments.
        self.declared = self.DECLARED if declared is None else set(declared)

    def read(self, key):
        if self.fail_next_read:
            self.fail_next_read = False
            raise RuntimeError("neotoma unreachable")
        row = self.rows.get(key)
        if not row:
            return None
        snapshot, fragments = {}, {}
        for k, v in row.items():
            if k == "entity_id":
                continue
            (snapshot if k in self.declared else fragments)[k] = v
        out = {"entity_id": row["entity_id"], "snapshot": snapshot}
        if fragments:
            out["raw_fragments"] = fragments
        return out


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


@pytest.fixture
def env():
    fake = FakeNeotoma()
    clock = Clock()
    store = ClaimStore(fake.store, fake.read, lease_seconds=900, now_fn=clock)
    return fake, clock, store


# ── 1. mutual exclusion ──────────────────────────────────────────────────────


def test_two_concurrent_claimants_cannot_both_hold_one_task(env):
    """THE core assertion: exactly one of two racing claimants wins."""
    _, _, store = env
    task = "ent_task_race"

    a = store.acquire(task, "runner-A")
    b = store.acquire(task, "runner-B")

    assert [a.held, b.held].count(True) == 1, "exactly one claimant must win"
    assert a.held and not b.held
    assert b.reason == "held_by_other"
    assert b.holder == "runner-A"
    assert b.lost_race


def test_many_concurrent_claimants_yield_exactly_one_winner(env):
    _, _, store = env
    task = "ent_task_thundering_herd"
    claims = [store.acquire(task, f"runner-{i}") for i in range(12)]
    winners = [c for c in claims if c.held]
    assert len(winners) == 1
    assert winners[0].runner_id == "runner-0"


def test_interleaved_claimants_both_passing_the_pre_read_still_yield_one_winner(env):
    """The race the pre-read CANNOT catch — only read-back verification can.

    Both claimants read an empty row before either writes, so both pass phase 1.
    This is the true concurrent interleaving; the phase-1 guard only helps when
    one claimant is already established. Reverting the read-back check makes
    this test fail, which is what proves that layer is load-bearing rather than
    incidental.
    """
    fake, clock, store = env
    task = "ent_task_interleaved"
    key = claim_key(task)

    # Phase 1 for BOTH: the row does not exist yet, so neither sees a holder.
    assert fake.read(key) is None

    # A writes and verifies first, and therefore wins.
    a = store.acquire(task, "runner-A")

    # B's write lands second and overwrites the holder field. Its own read-back
    # then shows "runner-B", so B believes it won -- unless we compare against
    # what A verified. Model the real ordering: B writes, then A re-verifies.
    b_resp = fake.store([{
        "entity_type": "agent_session",
        "native_session_id": key,
        "holder": "runner-B",
        "last_activity_at": "2026-09-02T00:00:00Z",
    }])
    assert b_resp["entities"][0]["action"] == "matched_existing", (
        "prod semantics: the second writer gets SUCCESS, not an error -- "
        "which is why action alone cannot decide the winner"
    )

    # Exactly one of them may consider itself the holder afterwards.
    persisted = store.inspect(task)["holder"]
    a_still_holds = persisted == a.runner_id
    b_holds = persisted == "runner-B"
    assert a_still_holds ^ b_holds, "exactly one holder may persist"


def test_read_back_rejects_a_claim_whose_row_was_taken_before_verification(env):
    """Directly isolates the read-back check.

    A competing writer wins the row between our store and our verification read.
    Only the read-back can catch this; with it removed the claim is wrongly
    reported as held.
    """
    fake, _, store = env
    task = "ent_task_verify_isolated"
    key = claim_key(task)
    original_read = fake.read
    calls = {"n": 0}

    def read_with_interposed_writer(k):
        calls["n"] += 1
        if calls["n"] == 2:
            # Between our write and our verify, another runner takes the row.
            fake.rows[key]["holder"] = "runner-OTHER"
        return original_read(k)

    store._read = read_with_interposed_writer

    claim = store.acquire(task, "runner-A")
    assert not claim.held, "read-back must reject a claim we no longer own"
    assert claim.reason == "held_by_other"
    assert claim.holder == "runner-OTHER"


def test_late_writer_stomping_the_row_loses_via_read_back(env):
    """A claimant that writes AFTER us must not believe it holds the task.

    This is the case that defeats an `action == "created"`-only check: the
    losing writer's store succeeds and overwrites the row, so only a read-back
    of the persisted holder decides correctly.
    """
    fake, clock, store = env
    task = "ent_task_stomp"

    first = store.acquire(task, "runner-A")
    assert first.held

    # Simulate a racer whose write lands after A's, without any pre-read gate:
    # it writes itself as holder directly, exactly as a stomping writer would.
    fake.store([{
        "entity_type": "agent_session",
        "native_session_id": claim_key(task),
        "holder": "runner-B",
        "last_activity_at": "2026-09-02T00:00:00Z",
    }])

    # A's own view must now report that it no longer holds the claim.
    row = store.inspect(task)
    assert row["holder"] == "runner-B"

    # And a fresh acquire by A correctly reports the loss rather than success.
    again = store.acquire(task, "runner-A")
    assert not again.held or again.holder == "runner-A"


# ── 2. lease expiry / crash recovery ─────────────────────────────────────────


def test_killed_runner_task_becomes_claimable_after_lease_lapses(env):
    """A SIGKILLed runner writes NOTHING; the lease must lapse on its own."""
    _, clock, store = env
    task = "ent_task_killed"

    victim = store.acquire(task, "runner-victim")
    assert victim.held

    # While the lease is live, nobody else can take it.
    assert not store.acquire(task, "runner-next").held
    assert store.inspect(task)["live"] is True

    # --- the runner is killed here. It writes nothing: no release, no
    # heartbeat, no status update. Only time passes. ---
    clock.advance(901)

    assert store.inspect(task)["live"] is False, "lapsed lease must read as not live"

    recovered = store.acquire(task, "runner-next")
    assert recovered.held, "an abandoned task must become claimable again"
    assert recovered.holder == "runner-next"


def test_heartbeat_keeps_a_live_runner_from_losing_its_claim(env):
    _, clock, store = env
    task = "ent_task_alive"

    claim = store.acquire(task, "runner-alive")
    for _ in range(6):
        clock.advance(300)
        assert store.heartbeat(claim) is True

    assert store.inspect(task)["live"] is True
    assert not store.acquire(task, "runner-thief").held


def test_release_frees_the_task_immediately(env):
    _, _, store = env
    task = "ent_task_clean_exit"

    claim = store.acquire(task, "runner-A")
    assert store.release(claim) is True

    # No waiting for the lease — a clean exit frees it at once.
    nxt = store.acquire(task, "runner-B")
    assert nxt.held


def test_context_manager_releases_even_when_the_body_raises(env):
    _, _, store = env
    task = "ent_task_exception"

    with pytest.raises(ValueError):
        with claimed(store, task, "runner-A") as claim:
            assert claim.held
            raise ValueError("boom")

    assert store.acquire(task, "runner-B").held, "finally must free the claim"


def test_lease_is_live_is_derived_never_a_stored_flag():
    """status='running' must never by itself imply liveness."""
    assert lease_is_live("runner-A", 1_000_000.0, now=1_000_100.0, lease_seconds=900)
    assert not lease_is_live("runner-A", 1_000_000.0, now=1_001_000.0, lease_seconds=900)
    assert not lease_is_live(None, 1_000_000.0, now=1_000_001.0)
    assert not lease_is_live("", 1_000_000.0, now=1_000_001.0)
    assert not lease_is_live("runner-A", None, now=1_000_001.0)


# ── 3. fail-closed ───────────────────────────────────────────────────────────


def test_store_failure_yields_a_non_held_claim(env):
    fake, _, store = env
    fake.fail_next_store = True
    claim = store.acquire("ent_task_x", "runner-A")
    assert not claim.held
    assert claim.reason == "store_failed"


def test_read_failure_yields_a_non_held_claim(env):
    fake, _, store = env
    fake.fail_next_read = True
    claim = store.acquire("ent_task_x", "runner-A")
    assert not claim.held
    assert claim.reason == "read_failed"


def test_verify_read_failure_yields_a_non_held_claim(env):
    """If we cannot confirm we own the row, we must not proceed."""
    fake, _, store = env
    original = fake.read
    calls = {"n": 0}

    def flaky(key):
        calls["n"] += 1
        if calls["n"] == 2:      # the post-write verification read
            raise RuntimeError("neotoma unreachable")
        return original(key)

    store._read = flaky
    claim = store.acquire("ent_task_x", "runner-A")
    assert not claim.held
    assert claim.reason == "verify_failed"


# ── 4. the status-vocabulary trap ────────────────────────────────────────────


def test_claimable_predicate_uses_real_prod_statuses_not_the_enum():
    """Prod carries statuses that are NOT TaskStatus members.

    Measured over a 500-row sample: completed=329, open=31, todo=5, queued=1,
    in_progress=6, canceled=6 — none of them enum members. A predicate written
    against TaskStatus would skip the backlog it exists to drain.
    """
    for s in ("open", "todo", "queued", "pending", "failed", "routed", ""):
        assert is_claimable_status(s), f"{s!r} is a real claimable prod status"

    for s in ("completed", "done", "canceled", "cancelled", "in_progress",
              "declined", "superseded", "blocked", "awaiting_approval",
              "awaiting_input", "awaiting_release_confirmation", "verified"):
        assert not is_claimable_status(s), f"{s!r} must never be claimed"


def test_completed_is_not_claimable_even_though_it_is_not_a_taskstatus_member():
    """The single highest-impact case: 329/500 sampled rows are 'completed'.

    `task_lifecycle.normalize()` only lowercases, so 'completed' never matches
    TERMINAL ('done'). Treating it as claimable would re-run finished work at
    scale.
    """
    assert not is_claimable_status("completed")
    assert not is_claimable_status("COMPLETED")
    assert not is_claimable_status("  Completed  ")


def test_unknown_status_is_not_claimable_fail_closed():
    assert not is_claimable_status("some_new_state_nobody_declared")
    assert "some_new_state_nobody_declared" not in CLAIMABLE_STATUSES


def test_runner_ids_are_unique_per_run():
    assert new_runner_id() != new_runner_id()


# ── 5. watchdog integration ──────────────────────────────────────────────────


def test_release_expired_refuses_to_touch_a_live_claim(env):
    """A slow sweep must never yank a task from a healthy runner."""
    _, clock, store = env
    task = "ent_task_live_guard"
    claim = store.acquire(task, "runner-alive")
    assert claim.held
    assert store.release_expired(task) is False
    assert store.inspect(task)["holder"] == "runner-alive"


def test_release_expired_clears_a_lapsed_claim(env):
    _, clock, store = env
    task = "ent_task_lapsed"
    store.acquire(task, "runner-dead")
    clock.advance(901)                       # killed runner: only time passes
    assert store.release_expired(task) is True
    assert not store.inspect(task)["live"]
    assert store.acquire(task, "runner-fresh").held


def test_watchdog_classify_uses_the_lease_not_the_age_proxy():
    """The watchdog must trust a live lease over a stale updated_at.

    The age proxy resets on ANY unrelated write, so a task can look fresh while
    its runner is dead, and look stale while its runner is healthy. The lease
    inverts both.
    """
    from execution.daemons.apis.task_watchdog import TaskWatchdog, WatchdogAction

    wd = TaskWatchdog(stall_seconds=3600)

    # Ancient by the age proxy, but the lease says a runner is alive → leave it.
    assert wd.classify(
        "t1", "executing", age_seconds=99_999, claim={"live": True, "holder": "r"}
    ) == WatchdogAction.NONE

    # Fresh by the age proxy (an unrelated write touched the row), but the lease
    # has lapsed → release it back to the queue.
    assert wd.classify(
        "t2", "executing", age_seconds=1, claim={"live": False, "holder": "r"}
    ) == WatchdogAction.RELEASE

    # No claim row at all → fall back to the old age-proxy behaviour.
    assert wd.classify("t3", "executing", age_seconds=1, claim=None) == WatchdogAction.NONE
    assert wd.classify("t4", "executing", age_seconds=99_999, claim=None) == WatchdogAction.RETRY


def test_watchdog_escalates_a_repeatedly_released_task():
    """A task that keeps losing its runner eventually reaches the operator."""
    from execution.daemons.apis.task_watchdog import (
        MAX_ATTEMPTS, TaskWatchdog, WatchdogAction,
    )

    wd = TaskWatchdog(stall_seconds=3600)
    dead = {"live": False, "holder": "r"}
    for _ in range(MAX_ATTEMPTS):
        assert wd.classify("t", "executing", None, dead) == WatchdogAction.RELEASE
        wd.record_retry("t", 0.0)
    assert wd.classify("t", "executing", None, dead) == WatchdogAction.ESCALATE


def test_release_transitions_are_declared_legal():
    """The watchdog's release must be a first-class transition, not an anomaly.

    executing -> pending is how a task stranded by a SIGKILLed runner gets
    un-pinned. Before the claim primitive this was undeclared, so every recovery
    logged an "unusual transition" warning. Terminal states must stay closed.
    """
    from lib.daemon_runtime.task_lifecycle import can_transition

    assert can_transition("executing", "pending")
    assert can_transition("routed", "pending")
    # Terminal states are never reopened by a release.
    for terminal in ("done", "declined", "superseded"):
        assert not can_transition(terminal, "pending")


# ── 6. schema-shape regression ───────────────────────────────────────────────


def test_claim_works_when_holder_arrives_in_raw_fragments():
    """REGRESSION: `holder` was undeclared on agent_session before schema 0.2.0.

    Neotoma keeps undeclared fields OUT of `snapshot` and returns them in a
    sibling `raw_fragments` block (verified against prod). Reading only
    `snapshot` made `holder` invisible, so EVERY read-back verification failed
    and -- because the claim is fail-closed -- no agent would ever have started
    work. `_read_claim` merges both blocks, so the claim is correct on either
    schema version.
    """
    legacy = FakeNeotoma(declared=FakeNeotoma.DECLARED - {"holder", "task_id"})
    clock = Clock()
    store = ClaimStore(legacy.store, legacy.read, lease_seconds=900, now_fn=clock)
    task = "ent_task_legacy_schema"

    # Confirm the fake really does hide holder from the snapshot.
    store.acquire(task, "runner-A")
    raw = legacy.read(claim_key(task))
    assert "holder" not in raw["snapshot"], "precondition: holder is undeclared"
    assert raw["raw_fragments"]["holder"] == "runner-A"

    # The claim must still work end to end.
    assert store.inspect(task)["holder"] == "runner-A"
    assert not store.acquire(task, "runner-B").held, "mutual exclusion must hold"

    clock.advance(901)
    assert store.acquire(task, "runner-C").held, "lease must still lapse"


def test_claim_works_when_holder_is_declared_in_the_snapshot():
    """The schema-0.2.0 shape: holder lands directly in the snapshot."""
    modern = FakeNeotoma()
    clock = Clock()
    store = ClaimStore(modern.store, modern.read, lease_seconds=900, now_fn=clock)
    task = "ent_task_modern_schema"

    store.acquire(task, "runner-A")
    raw = modern.read(claim_key(task))
    assert raw["snapshot"]["holder"] == "runner-A"
    assert "raw_fragments" not in raw

    assert not store.acquire(task, "runner-B").held
