"""Scenario eval: a recovered checkpoint can be resolved, and released once.

Continues the recovery scenario through the operator's decision.  The real
recovery command files the checkpoints; the real MCP ``resolve_checkpoint``
implementation then resolves them with RFC 9421 headers signed by a test
resolver key whose thumbprint is pinned in each checkpoint's authority, and the
real Apis consumer (the same one the SSE stream drives) releases or declines the
task.  Nothing between the signature and the dispatch boundary is stubbed:
resolver authentication, attribution read-back, tenant, task and policy revision
checks, and the replay claim all run.

Resolution surfaces in the tree: the MCP ``resolve_checkpoint`` tool and the SSE
consumer of ``checkpoint_brief`` events (``handle_event``).  No Telegram path
resolves a checkpoint (notifications only), so there is nothing to exercise
there.
"""

from __future__ import annotations

import asyncio

from harness import (  # noqa: F401
    FINISHED,
    HELD,
    apis,
    gating,
    outcomes,
    resolvable,
    run,
    server,
)
from lib.daemon_runtime.sse_client import NeotomaEvent


class _Notifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message, priority=None, handler=None, **kwargs):
        self.sent.append(message)

    def clear_dedupe(self, key):
        pass


def recover(world, monkeypatch, capsys) -> dict[str, str]:
    """Run the real recovery command; return {task_id: its one checkpoint id}."""
    code, lines = run(monkeypatch, capsys, "--apply")
    assert code == 0, lines
    return {task: world.pending_checkpoints(task)[0] for task in HELD}


async def mcp_resolve(world, checkpoint_id: str, action: str) -> dict:
    return await server._resolve_checkpoint(
        checkpoint_id,
        action,
        resolver_aauth_headers=world.resolver_headers(checkpoint_id, action),
    )


async def sse_deliver(world, checkpoint_id: str, *, stale_status: str) -> None:
    """An SSE redelivery whose embedded snapshot is stale by construction."""
    event = NeotomaEvent(
        event_type="entity_updated",
        entity_type="checkpoint_" + "brief",
        entity_id=checkpoint_id,
        action="updated",
        snapshot={
            **world.entities[checkpoint_id]["fields"],
            "status": stale_status,
            "resolved_dispatched": False,
        },
        hydrated=True,
    )
    await apis.handle_event(event, _Notifier())


def task_fields(world, task_id):
    return world.entities[task_id]["fields"]


def test_mcp_approval_of_a_recovered_checkpoint_releases_the_task_exactly_once(
    world, monkeypatch, capsys
):
    checkpoints = recover(world, monkeypatch, capsys)
    cp = checkpoints["ent_task_held_plain"]

    result = asyncio.run(mcp_resolve(world, cp, "approve"))

    assert result["action_taken"] == "approved — task re-dispatched", result
    assert world.dispatches.calls == [("ent_task_held_plain", "approved", True)]
    assert task_fields(world, "ent_task_held_plain")["status"] == "routed"
    assert task_fields(world, "ent_task_held_plain")["blocked_reason"] == ""
    assert world.entities[cp]["fields"]["resolved_dispatched"] is True

    # Repeated delivery, on every surface, cannot release it again.
    replay = asyncio.run(mcp_resolve(world, cp, "approve"))
    assert "not 'awaiting_operator'" in replay["error"]

    async def redeliver():
        for _ in range(3):
            await sse_deliver(world, cp, stale_status="approved")
            await apis.handle_checkpoint_brief(
                cp, world.entities[cp]["fields"], _Notifier()
            )

    asyncio.run(redeliver())
    assert len(world.dispatches.calls) == 1, "exactly one dispatch"


def test_rejecting_another_recovered_checkpoint_declines_its_task_with_no_dispatch(
    world, monkeypatch, capsys
):
    checkpoints = recover(world, monkeypatch, capsys)
    approved = checkpoints["ent_task_held_plain"]
    rejected = checkpoints["ent_task_reject"]
    asyncio.run(mcp_resolve(world, approved, "approve"))
    assert len(world.dispatches.calls) == 1

    result = asyncio.run(mcp_resolve(world, rejected, "reject"))

    assert result["action_taken"] == "rejected — task marked declined", result
    assert task_fields(world, "ent_task_reject")["status"] == "declined"
    assert len(world.dispatches.calls) == 1, "a rejection dispatches nothing"
    # The approved task is untouched by the other task's rejection.
    assert task_fields(world, "ent_task_held_plain")["status"] == "routed"

    async def replay():
        again = await mcp_resolve(world, rejected, "reject")
        assert "not 'awaiting_operator'" in again["error"]
        for _ in range(2):
            await sse_deliver(world, rejected, stale_status="rejected")

    asyncio.run(replay())
    assert len(world.dispatches.calls) == 1
    assert task_fields(world, "ent_task_reject")["status"] == "declined"


def test_a_held_task_the_gate_would_now_pass_is_still_releasable_by_its_recovered_checkpoint(
    world, monkeypatch, capsys
):
    """Creation turns an automatic pass into a checkpoint so the operator decides;
    release must then accept that approval rather than loop on a mismatch."""
    checkpoints = recover(world, monkeypatch, capsys)
    cp = checkpoints["ent_task_auto"]
    assert world.entities[cp]["fields"]["gate_action"] == "checkpoint_plan_approval"

    result = asyncio.run(mcp_resolve(world, cp, "approve"))

    assert result["action_taken"] == "approved — task re-dispatched", result
    assert world.dispatches.calls == [("ent_task_auto", "approved", True)]
    assert task_fields(world, "ent_task_auto")["status"] == "routed"


def test_sse_consumer_releases_a_signed_approval_once_across_redelivery(
    world, monkeypatch, capsys
):
    """The SSE surface on its own: the operator's signed approval reaches Neotoma
    directly, and the stream's consumer is what releases the task."""
    checkpoints = recover(world, monkeypatch, capsys)
    cp = checkpoints["ent_task_twin_a"]
    from lib.daemon_runtime.checkpoint_protocol import checkpoint_resolution_body

    body = server._canonical_body_bytes(checkpoint_resolution_body(cp, "approve"))
    response = world.post(
        "https://neotoma.test/correct",
        headers={
            "Authorization": "Bearer eval-token",
            **world.resolver_headers(cp, "approve"),
        },
        content=body,
    )
    assert response.status_code == 200

    async def deliver():
        for _ in range(3):
            await sse_deliver(world, cp, stale_status="approved")

    asyncio.run(deliver())

    assert world.dispatches.calls == [("ent_task_twin_a", "approved", True)]
    assert task_fields(world, "ent_task_twin_a")["status"] == "routed"


def test_an_approval_without_the_pinned_resolver_never_releases(
    world, monkeypatch, capsys
):
    """Approver checks are unchanged: a bare bearer approval, and an approval
    signed by some other key, release nothing."""
    checkpoints = recover(world, monkeypatch, capsys)
    bare = checkpoints["ent_task_twin_b"]
    world._put(bare, "checkpoint_" + "brief", {"status": "approved"}, signed=False)
    other = checkpoints["ent_task_held_last"]
    from harness import _signer

    stranger = _signer(sub="ateles@ateles-swarm", kid="not-the-pinned-key")
    from lib.daemon_runtime.checkpoint_protocol import checkpoint_resolution_body

    stranger_body = server._canonical_body_bytes(
        checkpoint_resolution_body(other, "approve")
    )
    world.post(
        "https://neotoma.test/correct",
        headers={
            "Authorization": "Bearer eval-token",
            **stranger.sign_headers(
                method="POST",
                url="https://neotoma.test/correct",
                body=stranger_body,
                content_type="application/json",
            ),
            "content-type": "application/json",
        },
        content=stranger_body,
    )

    async def deliver():
        await sse_deliver(world, bare, stale_status="approved")
        await sse_deliver(world, other, stale_status="approved")

    asyncio.run(deliver())

    assert world.dispatches.calls == []
    assert task_fields(world, "ent_task_twin_b")["status"] == "awaiting_approval"
    assert task_fields(world, "ent_task_held_last")["status"] == "awaiting_approval"


def test_approving_a_never_tier_recovered_checkpoint_releases_nothing(
    world, monkeypatch, capsys
):
    """What the honest summary says is what happens: approval records the
    decision and dispatches nothing."""
    checkpoints = recover(world, monkeypatch, capsys)

    for task_id in ("ent_task_operator_only", "ent_task_unclassified"):
        result = asyncio.run(mcp_resolve(world, checkpoints[task_id], "approve"))
        assert "no agent dispatch" in result["action_taken"], result
        assert task_fields(world, task_id)["status"] == "awaiting_approval"

    assert world.dispatches.calls == []


# ── Policy-only classification repair, as the published instructions say ────

UNCLASSIFIED = "ent_task_unclassified"
CLASSIFIED_LOW = frozenset({"local_edit", "mystery_action"})


def _recover_one(world, monkeypatch, capsys, task_id, *extra):
    code, lines = run(monkeypatch, capsys, "--apply", "--task", task_id, *extra)
    assert code == 0, lines
    return lines[0]


def _repair_policy_only(world, monkeypatch, capsys):
    """Recover the unclassified task, then follow the instructions it carries:
    add its action to the policy's low-blast set (the task is not touched) and
    re-run recovery."""
    first = _recover_one(world, monkeypatch, capsys, UNCLASSIFIED)
    assert first["outcome"] == "needs_classification", first
    old = first["checkpoint_id"]
    assert world.entities[old]["fields"]["blast_radius"] == "never"
    task_before = dict(task_fields(world, UNCLASSIFIED))

    world.edit_policy(low_blast_action_types=CLASSIFIED_LOW)

    assert task_fields(world, UNCLASSIFIED) == task_before, "the task is unchanged"
    return old


def test_policy_only_repair_replaces_the_obsolete_checkpoint(
    world, monkeypatch, capsys
):
    old = _repair_policy_only(world, monkeypatch, capsys)

    # A dry run reports the replacement it would make and writes nothing.
    writes = list(world.write_log)
    code, lines = run(monkeypatch, capsys, "--task", UNCLASSIFIED)
    assert code == 0 and lines[0]["outcome"] == "would_reissue", lines
    assert world.write_log == writes

    result = _recover_one(world, monkeypatch, capsys, UNCLASSIFIED)

    assert result["outcome"] == "reissued", result
    new = result["checkpoint_id"]
    assert new != old, "a distinct checkpoint bound to the current policy"
    assert world.pending_checkpoints(UNCLASSIFIED) == [new]
    assert resolvable(world, UNCLASSIFIED) == [new]
    assert world.entities[old]["fields"]["status"] == "superseded"
    assert world.entities[new]["fields"]["blast_radius"] == "low"
    assert "Approval releases the task" in world.entities[new]["fields"]["plan_summary"]
    # The old checkpoint is retired only after the replacement is stored.
    stored = world.write_log.index(f"store {new}")
    retired = world.write_log.index(f"correct {old}.status=superseded")
    assert stored < retired
    # Recovery itself never releases the task.
    assert task_fields(world, UNCLASSIFIED)["status"] == "awaiting_approval"
    assert world.dispatches.calls == []

    # A rerun is a no-op: one checkpoint per task state.
    writes = list(world.write_log)
    again = _recover_one(world, monkeypatch, capsys, UNCLASSIFIED)
    assert again["outcome"] == "skipped" and again["checkpoint_id"] == new
    assert world.write_log == writes


def test_repaired_checkpoint_releases_exactly_once_through_mcp(
    world, monkeypatch, capsys
):
    old = _repair_policy_only(world, monkeypatch, capsys)
    new = _recover_one(world, monkeypatch, capsys, UNCLASSIFIED)["checkpoint_id"]

    # The retired checkpoint can no longer be resolved at all.
    stale = asyncio.run(mcp_resolve(world, old, "approve"))
    assert "not 'awaiting_operator'" in stale["error"], stale
    assert world.dispatches.calls == []

    result = asyncio.run(mcp_resolve(world, new, "approve"))

    assert result["action_taken"] == "approved — task re-dispatched", result
    assert world.dispatches.calls == [(UNCLASSIFIED, "approved", True)]
    assert task_fields(world, UNCLASSIFIED)["status"] == "routed"

    replay = asyncio.run(mcp_resolve(world, new, "approve"))
    assert "not 'awaiting_operator'" in replay["error"]

    async def redeliver():
        for _ in range(3):
            await sse_deliver(world, new, stale_status="approved")
            await apis.handle_checkpoint_brief(
                new, world.entities[new]["fields"], _Notifier()
            )

    asyncio.run(redeliver())
    assert len(world.dispatches.calls) == 1, "exactly one dispatch"


def test_repaired_checkpoint_releases_once_through_a_signed_sse_resolution(
    world, monkeypatch, capsys
):
    _repair_policy_only(world, monkeypatch, capsys)
    new = _recover_one(world, monkeypatch, capsys, UNCLASSIFIED)["checkpoint_id"]
    from lib.daemon_runtime.checkpoint_protocol import checkpoint_resolution_body

    body = server._canonical_body_bytes(checkpoint_resolution_body(new, "approve"))
    response = world.post(
        "https://neotoma.test/correct",
        headers={
            "Authorization": "Bearer eval-token",
            **world.resolver_headers(new, "approve"),
        },
        content=body,
    )
    assert response.status_code == 200
    assert world.dispatches.calls == [], "the resolution alone releases nothing"

    async def deliver():
        for _ in range(3):
            await sse_deliver(world, new, stale_status="approved")

    asyncio.run(deliver())

    assert world.dispatches.calls == [(UNCLASSIFIED, "approved", True)]
    assert task_fields(world, UNCLASSIFIED)["status"] == "routed"


def test_an_unchanged_policy_keeps_the_never_tier_checkpoint(
    world, monkeypatch, capsys
):
    """No repair, no replacement: the same checkpoint stands and still says so."""
    first = _recover_one(world, monkeypatch, capsys, UNCLASSIFIED)
    writes = list(world.write_log)

    again = _recover_one(world, monkeypatch, capsys, UNCLASSIFIED)

    assert again["outcome"] == "needs_classification"
    assert again["checkpoint_id"] == first["checkpoint_id"]
    assert world.write_log == writes


def test_an_unreadable_policy_replaces_and_retires_nothing(
    world, monkeypatch, capsys
):
    """If the current policy cannot be established the checkpoint is not judged
    stale: the task reports a failure and nothing is written."""
    first = _recover_one(world, monkeypatch, capsys, UNCLASSIFIED)
    writes = list(world.write_log)
    world.edit_policy(loaded=False)

    code, lines = run(monkeypatch, capsys, "--apply", "--task", UNCLASSIFIED)

    assert code == 1, lines
    assert lines[0]["outcome"] == "failed"
    assert world.write_log == writes
    assert world.pending_checkpoints(UNCLASSIFIED) == [first["checkpoint_id"]]


def test_closing_operator_work_by_task_status_keeps_its_outcome(
    world, monkeypatch, capsys
):
    """The closure the summaries describe: the operator records the outcome on the
    task, and approving the checkpoint then only closes it, never re-writes the
    task.  Rejecting instead declines the task, as the summaries say."""
    checkpoints = recover(world, monkeypatch, capsys)

    for task_id in ("ent_task_operator_only", UNCLASSIFIED):
        world.touch_task(task_id, status="done")
        result = asyncio.run(mcp_resolve(world, checkpoints[task_id], "approve"))
        assert "no agent dispatch" in result["action_taken"], result
        assert task_fields(world, task_id)["status"] == "done"
        assert world.entities[checkpoints[task_id]]["fields"]["status"] != (
            "awaiting_operator"
        ), "the checkpoint is closed"

    assert world.dispatches.calls == []
