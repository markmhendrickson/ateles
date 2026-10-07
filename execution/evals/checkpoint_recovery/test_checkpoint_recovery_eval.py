"""Scenario eval: recovering held tasks into resolvable checkpoints.

See ``harness.py`` for the stand-in and fixtures.  Each test names the behavior
whose removal turns it red.
"""

from __future__ import annotations

import pytest  # noqa: F401

from harness import (  # noqa: F401
    CHECKPOINT,
    FINISHED,
    HELD,
    SCENARIO,
    apis,
    cli,
    gating,
    outcomes,
    resolvable,
    run,
    server,
)


def test_dry_run_performs_zero_writes(world, monkeypatch, capsys):
    before = {i: len(e["observations"]) for i, e in world.entities.items()}

    code, lines = run(monkeypatch, capsys)

    assert code == 0
    assert world.write_log == []
    assert {i: len(e["observations"]) for i, e in world.entities.items()} == before
    got = outcomes(lines)
    assert all(got[t] == "would_reissue" for t in HELD)
    assert not set(FINISHED) & set(got), "finished tasks are not even enumerated"


def test_apply_gives_each_held_task_exactly_one_authenticated_checkpoint(
    world, monkeypatch, capsys
):
    code, lines = run(monkeypatch, capsys, "--apply")

    assert code == 0, lines
    summary = lines[-1]
    assert summary["examined"] == len(HELD), "paging reached every held task"
    for task_id in HELD:
        assert len(world.pending_checkpoints(task_id)) == 1, task_id
        assert len(resolvable(world, task_id)) == 1, task_id
        assert world.entities[task_id]["fields"]["status"] == "awaiting_approval"
    # The pre-signing brief of a held task is retired, never left beside the new one.
    assert world.entities["ent_legacy_held"]["fields"]["status"] == "superseded"
    # Two tasks that share a title still get two checkpoints.
    assert world.pending_checkpoints("ent_task_twin_a") != world.pending_checkpoints(
        "ent_task_twin_b"
    )
    # The relationship write stays within the producer's grant: none is made.
    assert world.relationship_requests == []


def test_finished_tasks_are_untouched(world, monkeypatch, capsys):
    snapshot = {t: dict(world.entities[t]["fields"]) for t in FINISHED}
    legacy_done = dict(world.entities["ent_legacy_done"]["fields"])

    run(monkeypatch, capsys, "--apply")
    # Even when a finished task is named explicitly, it is skipped and untouched.
    code, lines = run(
        monkeypatch,
        capsys,
        "--apply",
        *[a for t in FINISHED for a in ("--task", t)],
    )
    assert code == 0
    assert all(v == "skipped" for v in outcomes(lines).values())

    for task_id in FINISHED:
        assert world.entities[task_id]["fields"] == snapshot[task_id]
        assert world.pending_checkpoints(task_id) == (
            ["ent_legacy_done"] if task_id == "ent_task_done" else []
        )
    assert world.entities["ent_legacy_done"]["fields"] == legacy_done


def test_exact_replay_creates_nothing(world, monkeypatch, capsys):
    run(monkeypatch, capsys, "--apply")
    entities = set(world.entities)
    writes = list(world.write_log)

    code, lines = run(monkeypatch, capsys, "--apply")

    assert code == 0
    assert set(world.entities) == entities
    assert world.write_log == writes
    got = outcomes(lines)
    expected = {
        "ent_task_operator_only": "needs_operator",
        "ent_task_unclassified": "needs_classification",
    }
    assert all(got[t] == expected.get(t, "skipped") for t in HELD)


def test_changed_task_keeps_one_resolvable_current_checkpoint(
    world, monkeypatch, capsys
):
    run(monkeypatch, capsys, "--apply")
    first = world.pending_checkpoints("ent_task_held_plain")
    world.touch_task("ent_task_held_plain", blocked_reason="re-held after a change")

    code, lines = run(monkeypatch, capsys, "--apply")

    assert code == 0, lines
    current = world.pending_checkpoints("ent_task_held_plain")
    assert len(current) == 1 and current != first, "a distinct, current checkpoint"
    assert resolvable(world, "ent_task_held_plain") == current
    assert world.entities[first[0]]["fields"]["status"] == "superseded"


def test_failed_persistence_retires_nothing_and_exits_nonzero(
    world, monkeypatch, capsys
):
    world.fail_store = True

    code, lines = run(monkeypatch, capsys, "--apply")

    assert code == 1
    assert all(outcomes(lines)[t] == "failed" for t in HELD)
    assert world.entities["ent_legacy_held"]["fields"]["status"] == "awaiting_operator"
    assert [
        e
        for e in world.entities.values()
        if e["type"] == CHECKPOINT
        and e["fields"].get("status") == "awaiting_operator"
        and e["observations"][-1]["provenance"]
    ] == []


def test_partial_retirement_is_reported_then_a_rerun_finishes_without_a_second_replacement(
    world, monkeypatch, capsys
):
    world.fail_correct_ids = {"ent_legacy_held"}

    code, lines = run(monkeypatch, capsys, "--apply")

    assert code == 1
    assert outcomes(lines)["ent_task_held_legacy"] == "incomplete"
    replacement = [
        c
        for c in world.pending_checkpoints("ent_task_held_legacy")
        if c != "ent_legacy_held"
    ]
    assert len(replacement) == 1
    world.fail_correct_ids = set()

    code, lines = run(monkeypatch, capsys, "--apply")

    assert code == 0
    assert outcomes(lines)["ent_task_held_legacy"] == "retired_stale"
    assert world.pending_checkpoints("ent_task_held_legacy") == replacement


def test_read_failures_are_reported_distinctly_from_skips(world, monkeypatch, capsys):
    world.fail_query_types = {CHECKPOINT}

    code, lines = run(monkeypatch, capsys, "--apply")

    assert code == 1
    got = outcomes(lines)
    assert all(got[t] == "failed" for t in HELD)
    assert world.write_log == []
    # A confirmed skip stays a skip, distinct from a failed read.
    code, lines = run(monkeypatch, capsys, "--apply", "--task", "ent_task_done")
    assert code == 0 and outcomes(lines) == {"ent_task_done": "skipped"}

    world.fail_query_types = {"task"}
    code, lines = run(monkeypatch, capsys, "--apply")
    assert code == 1
    assert any("enumeration" in line.get("detail", "") for line in lines)


def test_recovered_checkpoint_passes_the_resolver_authority_gate_and_a_legacy_one_does_not(
    world, monkeypatch, capsys
):
    run(monkeypatch, capsys, "--apply")
    new_id = world.pending_checkpoints("ent_task_held_plain")[0]
    monkeypatch.setattr(
        server, "_get", lambda path, params=None: world.record(path.rsplit("/", 1)[-1])
    )
    headers = {
        "signature": "x",
        "signature-input": "x",
        "signature-key": "x",
        "content-digest": "x",
        "content-type": "application/json",
    }

    # The surface the operator uses: the checkpoint's authority is readable ...
    assert (
        server._checkpoint_resolver_authority(new_id, world.record(new_id)) is not None
    )
    # ... whereas the pre-signing brief has none, which is why it could never be resolved.
    legacy = world.record("ent_legacy_done")
    assert server._checkpoint_resolver_authority("ent_legacy_done", legacy) is None
    import asyncio

    refused = asyncio.run(
        server._resolve_checkpoint("ent_legacy_done", "approve", headers)
    )
    assert "no authenticated required-resolver authority" in refused["error"]
    # The recovered checkpoint gets past that gate (and is stopped only by the
    # caller-supplied proof, which this scenario does not have).
    passed = asyncio.run(server._resolve_checkpoint(new_id, "approve", headers))
    assert "no authenticated required-resolver authority" not in passed["error"]
    # Exactly one resolvable checkpoint stands for the task: one possible release.
    assert resolvable(world, "ent_task_held_plain") == [new_id]


# ── Recovery-state contract: a failed read is not evidence ──────────────────


def _checkpoint_states(world, task_id):
    return {
        i: world.entities[i]["fields"]["status"]
        for i, e in world.entities.items()
        if e["type"] == CHECKPOINT and e["fields"].get("task_entity_id") == task_id
    }


def test_unreadable_existing_checkpoint_is_a_failure_and_nothing_is_retired(
    world, monkeypatch, capsys
):
    """A signed brief whose record or authorization observations cannot be read is
    neither replaced nor retired: failure to read is not evidence that the
    authority is stale."""
    run(monkeypatch, capsys, "--apply")  # every held task now has a signed checkpoint
    signed = world.pending_checkpoints("ent_task_held_plain")[0]
    before = _checkpoint_states(world, "ent_task_held_plain")
    writes = list(world.write_log)

    world.fail_get = {signed}  # the checkpoint itself is unreadable
    code, lines = run(monkeypatch, capsys, "--apply", "--task", "ent_task_held_plain")
    assert code == 1
    assert outcomes(lines) == {"ent_task_held_plain": "failed"}
    assert world.write_log == writes, "nothing created or retired"
    assert _checkpoint_states(world, "ent_task_held_plain") == before

    world.fail_get = set()
    world.fail_observations = {signed}  # its authorization observations are not
    code, lines = run(monkeypatch, capsys, "--apply", "--task", "ent_task_held_plain")
    assert code == 1
    assert outcomes(lines) == {"ent_task_held_plain": "failed"}
    assert world.write_log == writes
    assert _checkpoint_states(world, "ent_task_held_plain") == before

    world.fail_observations = set()

    # Once reads recover, the same checkpoint is simply still current.
    world.fail_get = set()
    code, lines = run(monkeypatch, capsys, "--apply", "--task", "ent_task_held_plain")
    assert code == 0
    assert outcomes(lines) == {"ent_task_held_plain": "skipped"}
    assert world.pending_checkpoints("ent_task_held_plain") == [signed]


def test_unreadable_legacy_checkpoint_is_preserved_not_replaced(
    world, monkeypatch, capsys
):
    world.fail_get = {"ent_legacy_held"}

    code, lines = run(monkeypatch, capsys, "--apply", "--task", "ent_task_held_legacy")

    assert code == 1
    assert outcomes(lines) == {"ent_task_held_legacy": "failed"}
    assert world.write_log == []
    assert world.pending_checkpoints("ent_task_held_legacy") == ["ent_legacy_held"]


def test_failed_verification_read_after_replacement_is_incomplete_not_skipped(
    world, monkeypatch, capsys
):
    task = "ent_task_held_legacy"
    world.fail_get_after_store = {task}

    code, lines = run(monkeypatch, capsys, "--apply", "--task", task)

    assert code == 1, lines
    assert outcomes(lines) == {task: "incomplete"}
    record = lines[0]
    replacement = record["checkpoint_id"]
    assert replacement and replacement != "ent_legacy_held"
    assert record["remaining"] == ["ent_legacy_held"]
    # The replacement stands; the stale brief is still pending, not retired.
    assert world.entities[replacement]["fields"]["status"] == "awaiting_operator"
    assert world.entities["ent_legacy_held"]["fields"]["status"] == "awaiting_operator"

    world.fail_get_after_store = set()
    stores = world.store_count
    code, lines = run(monkeypatch, capsys, "--apply", "--task", task)

    assert code == 0
    assert outcomes(lines) == {task: "retired_stale"}
    assert lines[0]["checkpoint_id"] == replacement, "the replacement is reused"
    assert world.store_count == stores, "no second replacement is created"
    assert world.pending_checkpoints(task) == [replacement]
    assert world.entities["ent_legacy_held"]["fields"]["status"] == "superseded"


# ── Tasks whose approval can never release them ─────────────────────────────


@pytest.mark.parametrize(
    "task_id,outcome,needle",
    [
        ("ent_task_operator_only", "needs_operator", "reserved to the operator"),
        ("ent_task_unclassified", "needs_classification", "Classification repair"),
    ],
)
def test_tasks_approval_cannot_release_get_an_honest_outcome_and_summary(
    world, monkeypatch, capsys, task_id, outcome, needle
):
    code, lines = run(monkeypatch, capsys, "--apply", "--task", task_id)

    assert code == 0, lines
    assert outcomes(lines) == {task_id: outcome}
    checkpoint = world.pending_checkpoints(task_id)
    assert len(checkpoint) == 1 and resolvable(world, task_id) == checkpoint
    summary = world.entities[checkpoint[0]]["fields"]["plan_summary"]
    assert needle in summary
    assert "will NOT release" in summary
    assert "Approval releases the task" not in summary
    assert world.entities[task_id]["fields"]["status"] == "awaiting_approval"


def test_ordinary_held_tasks_still_say_approval_releases_them(
    world, monkeypatch, capsys
):
    run(monkeypatch, capsys, "--apply", "--task", "ent_task_held_plain")

    checkpoint = world.pending_checkpoints("ent_task_held_plain")[0]
    assert (
        "Approval releases the task"
        in world.entities[checkpoint]["fields"]["plan_summary"]
    )


def test_dry_run_flags_the_tasks_approval_cannot_release(world, monkeypatch, capsys):
    code, lines = run(monkeypatch, capsys, "--task", "ent_task_operator_only")

    assert code == 0
    assert "will NOT release" in lines[0]["detail"]
    assert world.write_log == []


@pytest.mark.parametrize(
    "task_id", ["ent_task_operator_only", "ent_task_unclassified"]
)
def test_summaries_never_offer_rejection_as_a_way_to_close_the_work(
    world, monkeypatch, capsys, task_id
):
    """Rejecting declines the task, so it cannot be how finished work is closed.

    The summary says what rejection does, and points to the task's own status as
    the place to record the outcome."""
    code, lines = run(monkeypatch, capsys, "--apply", "--task", task_id)
    assert code == 0, lines
    checkpoint = world.pending_checkpoints(task_id)[0]
    texts = [
        world.entities[checkpoint]["fields"]["plan_summary"],
        lines[0]["detail"],
    ]

    for text in texts:
        assert "reject this checkpoint to close" not in text
        assert "rejecting declines the task" in text.lower()
        assert "status in Neotoma to done (or cancelled" in text
        assert "no checkpoint action records that outcome" in text
        assert "approving only closes the checkpoint" in text


def test_ordinary_held_task_summary_states_what_rejecting_does(
    world, monkeypatch, capsys
):
    run(monkeypatch, capsys, "--apply", "--task", "ent_task_held_plain")

    checkpoint = world.pending_checkpoints("ent_task_held_plain")[0]
    summary = world.entities[checkpoint]["fields"]["plan_summary"]
    assert "Rejecting this checkpoint declines the task" in summary
