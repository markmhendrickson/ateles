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


# ── Listing larger than a page, under the server's own query rules ──────────


def _production_page(monkeypatch):
    monkeypatch.setattr(gating, "QUERY_PAGE_SIZE", 100)
    return 100


def test_stand_in_refuses_what_the_server_refuses(world):
    """Guards the guard: a cursor beside filters, an offset past the bound, and an
    oversized snapshot page are each a 400, as in production."""
    url = "https://neotoma.test/entities/query"
    filters = {"status": {"op": "eq", "value": "awaiting_approval"}}

    def post(**body):
        return world.post(url, json={"entity_type": "task", **body})

    combo = post(snapshot_filters=filters, cursor="ent_a")
    assert combo.status_code == 400
    assert combo.json()["details"]["code"] == "ERR_CURSOR_COMBINATION"
    assert post(cursor="ent_a", offset=5).status_code == 400
    assert post(offset=2001).status_code == 400
    assert post(limit=501, include_snapshots=True).status_code == 400
    assert post(snapshot_filters=filters, offset=2000, limit=500).status_code == 200
    assert post(cursor="ent_a", limit=5).status_code == 200  # unfiltered keyset is fine


def test_every_held_task_is_reached_across_production_sized_pages(
    world, monkeypatch, capsys
):
    page = _production_page(monkeypatch)
    bulk = world.add_held_tasks(2 * page + 37)
    expected = set(HELD) | set(bulk)

    code, lines = run(monkeypatch, capsys)

    assert code == 0, lines[-3:]
    assert set(outcomes(lines)) == expected, "every held task, none missed or repeated"
    assert lines[-1]["examined"] == len(expected)
    held_reads = [q for q in world.query_log if q["type"] == "task"]
    assert [q["offset"] for q in held_reads] == [0, page, 2 * page]
    assert all(q["filtered"] for q in held_reads)


def test_a_held_list_that_ends_on_a_full_page_is_still_complete(
    world, monkeypatch, capsys
):
    page = _production_page(monkeypatch)
    bulk = world.add_held_tasks(2 * page - len(HELD))  # exactly two full pages

    code, lines = run(monkeypatch, capsys)

    assert code == 0, lines[-3:]
    assert set(outcomes(lines)) == set(HELD) | set(bulk)


def test_applying_to_a_multi_page_population_gives_each_task_one_checkpoint(
    world, monkeypatch, capsys
):
    page = _production_page(monkeypatch)
    bulk = world.add_held_tasks(page + 20)

    code, lines = run(monkeypatch, capsys, "--apply")

    assert code == 0, lines[-3:]
    assert lines[-1]["examined"] == len(HELD) + len(bulk)
    for task_id in bulk:
        assert len(world.pending_checkpoints(task_id)) == 1, task_id


def test_a_listing_deeper_than_the_server_allows_fails_loudly_not_short(
    world, monkeypatch, capsys
):
    page = _production_page(monkeypatch)
    world.add_held_tasks(gating.MAX_QUERY_OFFSET + page + 5)

    code, lines = run(monkeypatch, capsys)

    assert code == 1
    assert any("enumeration" in line.get("detail", "") for line in lines), lines[-3:]
    assert world.write_log == []


def test_one_tasks_checkpoints_are_all_read_across_pages(world, monkeypatch):
    _production_page(monkeypatch)
    task = "ent_task_done"
    for n in range(230):
        world._put(
            f"ent_many_{n:04d}",
            CHECKPOINT,
            {"task_entity_id": task, "status": "superseded"},
            signed=False,
        )

    found = gating.checkpoints_for_task(task)

    assert found is not None
    assert len({e["entity_id"] for e in found}) == 231  # 230 + the scenario's one


# ── --limit bounds the enumeration, not just what is examined ───────────────


def _task_reads(world):
    return [q for q in world.query_log if q["type"] == "task"]


def _oversized(world, monkeypatch):
    """A held backlog beyond the server's offset bound, at production page size."""
    page = _production_page(monkeypatch)
    bulk = world.add_held_tasks(gating.MAX_QUERY_OFFSET + 110)
    assert len(bulk) + len(HELD) > gating.MAX_QUERY_OFFSET + page
    return sorted(set(bulk) | set(HELD))


def test_limit_one_on_an_oversized_backlog_dry_run_examines_the_first_task_only(
    world, monkeypatch, capsys
):
    first = _oversized(world, monkeypatch)[0]

    code, lines = run(monkeypatch, capsys, "--limit", "1")

    assert code == 0, lines
    assert outcomes(lines) == {first: "would_reissue"}
    assert lines[-1]["examined"] == 1
    assert len(_task_reads(world)) == 1, "nothing past the limit is read"
    assert world.write_log == []


def test_limit_one_on_an_oversized_backlog_apply_recovers_only_the_first_task(
    world, monkeypatch, capsys
):
    ordered = _oversized(world, monkeypatch)
    first = ordered[0]
    others = {i: dict(world.entities[i]["fields"]) for i in ordered[1:]}

    code, lines = run(monkeypatch, capsys, "--apply", "--limit", "1")

    assert code == 0, lines
    assert outcomes(lines) == {first: "reissued"}
    assert lines[-1]["examined"] == 1
    assert len(_task_reads(world)) == 1
    assert len(world.pending_checkpoints(first)) == 1
    assert resolvable(world, first) == world.pending_checkpoints(first)
    assert world.entities[first]["fields"]["status"] == "awaiting_approval"
    assert world.dispatches.calls == [], "recovery releases nothing"
    # No other task was touched and none gained a checkpoint.
    assert {i: world.entities[i]["fields"] for i in others} == others
    assert all(
        world.pending_checkpoints(i) == [] for i in others if i.startswith("ent_bulk")
    )


def test_a_limit_spanning_pages_reads_only_the_pages_it_needs(
    world, monkeypatch, capsys
):
    page = _production_page(monkeypatch)
    _oversized(world, monkeypatch)

    code, lines = run(monkeypatch, capsys, "--limit", str(page + 50))

    assert code == 0, lines[-2:]
    assert lines[-1]["examined"] == page + 50
    assert [q["offset"] for q in _task_reads(world)] == [0, page]


def test_unlimited_run_on_an_oversized_backlog_is_a_failure_with_nothing_written(
    world, monkeypatch, capsys
):
    """The whole list is read before any task is recovered, so an enumeration that
    cannot complete changes nothing and is reported as a failure, not as work."""
    _oversized(world, monkeypatch)

    code, lines = run(monkeypatch, capsys, "--apply")

    assert code == 1
    assert any("enumeration" in line.get("detail", "") for line in lines)
    assert lines[-1]["examined"] == 0, "no task was examined"
    assert world.write_log == []
    assert world.dispatches.calls == []
