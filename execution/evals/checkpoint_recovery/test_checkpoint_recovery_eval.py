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
    assert all(outcomes(lines)[t] == "skipped" for t in HELD)


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
