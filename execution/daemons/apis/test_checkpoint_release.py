"""Effect tests for releasing an operator-approved checkpoint (ateles#1141).

The MCP tool is the public surface, so these tests enter through
``server._resolve_checkpoint``.  The release itself must still travel through
Apis's existing ``handle_checkpoint_brief`` consumer; the test never invokes
that consumer directly.

Before the fix, ``test_approve_releases_task_and_marks_brief_consumed`` failed
because ``dispatch_task`` was never called and the task stayed
``awaiting_approval`` with its hold reason intact.  A test that asserted only
the brief's new ``approved`` status passed on the broken implementation.
"""

from __future__ import annotations

import asyncio
import inspect
import importlib.util
import sys
import types
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
_MCP_DIR = _HERE.parent.parent / "mcp" / "ateles"
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_MCP_DIR))

# The daemon test lane intentionally does not install the MCP SDK.  The release
# logic exercised here is a plain function in server.py, so provide only the
# import-time shapes that module needs.  The dedicated MCP lane still runs the
# real SDK and its stdio smoke tests.
if importlib.util.find_spec("mcp") is None:
    mcp_module = types.ModuleType("mcp")
    server_module = types.ModuleType("mcp.server")
    stdio_module = types.ModuleType("mcp.server.stdio")
    types_module = types.ModuleType("mcp.types")

    class _Shape:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    server_module.Server = _Shape
    stdio_module.stdio_server = None
    types_module.TextContent = _Shape
    types_module.Tool = _Shape
    sys.modules.update(
        {
            "mcp": mcp_module,
            "mcp.server": server_module,
            "mcp.server.stdio": stdio_module,
            "mcp.types": types_module,
        }
    )

import apis  # noqa: E402
import server  # noqa: E402
from lib.daemon_runtime.gating import ExecutionPolicy, evaluate_gate  # noqa: E402

CHECKPOINT_TYPE = "checkpoint_" + "brief"


class _Notifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message, priority=None, handler=None):
        self.sent.append(message)


async def _resolve(checkpoint_id: str, action: str) -> dict:
    """Accept both the old sync shape and the fixed async implementation."""
    result = server._resolve_checkpoint(checkpoint_id, action)
    if inspect.isawaitable(result):
        return await result
    return result


@pytest.fixture
def release_store(monkeypatch):
    brief_id = "ent_cp1"
    task_id = "ent_task_1"
    apis._nonreleasable_checkpoint_quarantine.discard(brief_id)
    records = {
        brief_id: {
            "entity_type": CHECKPOINT_TYPE,
            "snapshot": {
                "status": "awaiting_operator",
                "resolved_dispatched": False,
                "task_entity_id": task_id,
                "gate_action": "checkpoint_plan_approval",
                "blast_radius": "low",
                "title": "Approve the bounded implementation",
                "user_id": "tenant-a",
            },
        },
        task_id: {
            "entity_type": "task",
            "snapshot": {
                "status": "awaiting_approval",
                "blocked_reason": "waiting for operator approval",
                "assigned_to": "cicada",
                "title": "Implement the bounded change",
                "body": "Engineering work in the dispatcher.",
                "user_id": "tenant-a",
            },
        },
    }

    def get(path, params=None):
        return records.get(path.rsplit("/", 1)[-1])

    def correct(entity_id, entity_type, field, value, idempotency_key):
        record = records.get(entity_id)
        if record is None or record["entity_type"] != entity_type:
            return False
        record["snapshot"][field] = value
        return True

    def fetch_task(task_entity_id):
        record = records.get(task_entity_id)
        if record is None or record["entity_type"] != "task":
            return None
        return record["snapshot"]

    def fetch_checkpoint(checkpoint_entity_id):
        record = records.get(checkpoint_entity_id)
        if record is None or record["entity_type"] != CHECKPOINT_TYPE:
            return None
        return record["snapshot"]

    def set_status(task_entity_id, status, *, reason=None, **kwargs):
        value = status.value if hasattr(status, "value") else str(status)
        records[task_entity_id]["snapshot"]["status"] = value
        if reason is not None:
            records[task_entity_id]["snapshot"]["blocked_reason"] = reason
        return True

    def stamp(checkpoint_entity_id, *, handler):
        records[checkpoint_entity_id]["snapshot"]["resolved_dispatched"] = True
        return True

    def close_without_release(checkpoint_entity_id, *, handler, reason):
        records[checkpoint_entity_id]["snapshot"]["status"] = "approved_no_release"
        return True

    monkeypatch.setattr(server, "_get", get)
    monkeypatch.setattr(server, "_correct", correct)
    monkeypatch.setattr(apis, "fetch_task_snapshot", fetch_task)
    monkeypatch.setattr(apis, "fetch_checkpoint_snapshot", fetch_checkpoint)
    monkeypatch.setattr(apis, "set_task_status", set_status)
    monkeypatch.setattr(apis, "stamp_checkpoint_dispatched", stamp)
    monkeypatch.setattr(
        apis,
        "close_checkpoint_without_release",
        close_without_release,
        raising=False,
    )
    monkeypatch.setattr(apis, "DRY_RUN", True)
    monkeypatch.setattr(apis.Notifier, "from_neotoma", lambda: _Notifier())

    # Keep the effect test hermetic: ActivityLogger is observability, not the
    # release mechanism under test.
    class _Job:
        def finished(self, message):
            return None

    monkeypatch.setattr(apis._activity, "started", lambda message: _Job())

    return records, brief_id, task_id


@pytest.mark.asyncio
async def test_approve_releases_task_and_marks_brief_consumed(
    monkeypatch, release_store
):
    records, brief_id, task_id = release_store
    original_dispatch = apis.dispatch_task
    dispatches: list[dict] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append({"args": args, "kwargs": kwargs})
        await original_dispatch(*args, **kwargs)

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)

    result = await _resolve(brief_id, "approve")

    assert len(dispatches) == 1
    assert dispatches[0]["args"][:2] == (
        task_id,
        records[task_id]["snapshot"],
    )
    assert dispatches[0]["kwargs"]["trigger"] == "approved"
    assert dispatches[0]["kwargs"]["gate_override"] is True
    assert records[task_id]["snapshot"]["status"] == "routed"
    assert records[task_id]["snapshot"]["blocked_reason"] == ""
    assert records[brief_id]["snapshot"]["resolved_dispatched"] is True
    assert "re-dispatched" in result["action_taken"]

    replay = await _resolve(brief_id, "approve")
    assert "not 'awaiting_operator'" in replay["error"]
    assert len(dispatches) == 1

    # The consumer's independent replay guard also remains binding.
    await apis.handle_checkpoint_brief(
        brief_id, records[brief_id]["snapshot"], _Notifier()
    )
    assert len(dispatches) == 1


@pytest.mark.asyncio
async def test_stale_sse_snapshot_cannot_dispatch_after_inline_consumer(
    monkeypatch, release_store
):
    records, brief_id, _task_id = release_store
    stale_approved_snapshot = {
        **records[brief_id]["snapshot"],
        "status": "approved",
        "resolved_dispatched": False,
    }
    original_dispatch = apis.dispatch_task
    dispatches: list[str] = []

    async def spy_dispatch(entity_id, *args, **kwargs):
        dispatches.append(entity_id)
        await original_dispatch(entity_id, *args, **kwargs)

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)

    await _resolve(brief_id, "approve")
    await apis.handle_checkpoint_brief(brief_id, stale_approved_snapshot, _Notifier())

    assert len(dispatches) == 1


@pytest.mark.asyncio
async def test_failed_stamp_does_not_dispatch(monkeypatch, release_store):
    records, brief_id, task_id = release_store
    dispatches: list[tuple] = []
    notifier = _Notifier()

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)
    monkeypatch.setattr(
        apis, "stamp_checkpoint_dispatched", lambda checkpoint_id, *, handler: False
    )
    monkeypatch.setattr(apis.Notifier, "from_neotoma", lambda: notifier)

    result = await _resolve(brief_id, "approve")

    assert dispatches == []
    assert records[task_id]["snapshot"]["status"] == "awaiting_approval"
    assert records[brief_id]["snapshot"]["resolved_dispatched"] is False
    assert "re-dispatched" not in result["action_taken"]
    assert not any("re-dispatch" in message for message in notifier.sent)


@pytest.mark.asyncio
async def test_resolve_acknowledges_before_slow_agent_completion(
    monkeypatch, release_store
):
    records, brief_id, task_id = release_store
    finish_spawn = asyncio.Event()
    spawn_started = asyncio.Event()
    monkeypatch.setattr(apis, "DRY_RUN", False)
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", False)
    monkeypatch.setattr(apis, "RUN_EMAIL", False)

    class _Success:
        ok = True
        error = None
        returncode = 0

    async def slow_spawn(*args, **kwargs):
        spawn_started.set()
        await finish_spawn.wait()
        return _Success()

    monkeypatch.setattr(apis, "_spawn_harness_skill", slow_spawn)

    try:
        result = await asyncio.wait_for(_resolve(brief_id, "approve"), timeout=0.02)
        await asyncio.wait_for(spawn_started.wait(), timeout=0.02)
    finally:
        finish_spawn.set()

    assert result["action_taken"] == "approved — task re-dispatched"
    assert records[brief_id]["snapshot"]["resolved_dispatched"] is True
    assert records[task_id]["snapshot"]["status"] == "executing"

    background = list(getattr(apis, "_background_dispatch_tasks", ()))
    if background:
        await asyncio.gather(*background)


@pytest.mark.asyncio
async def test_cancelled_mcp_waiter_does_not_cancel_release_consumer(
    monkeypatch, release_store
):
    _records, brief_id, _task_id = release_store
    consumer_started = asyncio.Event()
    let_consumer_finish = asyncio.Event()
    consumer_finished = asyncio.Event()
    consumer_cancelled = False

    async def cancellable_consumer(checkpoint_id, snapshot):
        nonlocal consumer_cancelled
        consumer_started.set()
        try:
            await let_consumer_finish.wait()
        except asyncio.CancelledError:
            consumer_cancelled = True
            raise
        consumer_finished.set()
        return True

    monkeypatch.setattr(server, "_consume_checkpoint_resolution", cancellable_consumer)

    waiter = asyncio.create_task(_resolve(brief_id, "approve"))
    await consumer_started.wait()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    let_consumer_finish.set()
    await asyncio.wait_for(consumer_finished.wait(), timeout=0.02)
    assert consumer_cancelled is False


@pytest.mark.asyncio
async def test_unmaterialized_stamp_never_claims_release(monkeypatch, release_store):
    """A 2xx-like True without a read-back is not a replay claim."""
    records, brief_id, task_id = release_store
    monkeypatch.setattr(
        apis, "stamp_checkpoint_dispatched", lambda checkpoint_id, *, handler: True
    )

    result = await _resolve(brief_id, "approve")

    assert records[task_id]["snapshot"]["status"] == "routed"
    assert records[brief_id]["snapshot"]["resolved_dispatched"] is False
    assert "re-dispatched" not in result["action_taken"]


@pytest.mark.asyncio
async def test_fast_successful_run_is_confirmed_after_reaching_done(
    monkeypatch, release_store
):
    records, brief_id, task_id = release_store
    monkeypatch.setattr(apis, "DRY_RUN", False)
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", False)
    monkeypatch.setattr(apis, "RUN_EMAIL", False)

    class _Success:
        ok = True
        error = None
        returncode = 0

    async def succeed(*args, **kwargs):
        return _Success()

    monkeypatch.setattr(apis, "_spawn_harness_skill", succeed)

    result = await _resolve(brief_id, "approve")

    assert records[task_id]["snapshot"]["status"] == "done"
    assert records[task_id]["snapshot"]["blocked_reason"] == ""
    assert records[brief_id]["snapshot"]["resolved_dispatched"] is True
    assert "re-dispatched" in result["action_taken"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_write", ["routed", "blocked_reason", "executing"])
async def test_release_does_not_spawn_until_lifecycle_writes_are_proven(
    failed_write, monkeypatch, release_store
):
    records, brief_id, task_id = release_store
    task = records[task_id]["snapshot"]
    spawned: list[str] = []

    def selectively_write(task_entity_id, status, *, reason=None, **kwargs):
        value = status.value if hasattr(status, "value") else str(status)
        if failed_write == "routed" and value == "routed":
            return False
        if value == "routed":
            task["status"] = value
            if failed_write != "blocked_reason" and reason is not None:
                task["blocked_reason"] = reason
            return True
        if failed_write == "executing" and value == "executing":
            return False
        task["status"] = value
        if reason is not None:
            task["blocked_reason"] = reason
        return True

    class _Success:
        ok = True
        error = None
        returncode = 0

    async def capture_spawn(skill, entity_id, *args, **kwargs):
        spawned.append(entity_id)
        return _Success()

    monkeypatch.setattr(apis, "set_task_status", selectively_write)
    monkeypatch.setattr(apis, "DRY_RUN", False)
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", False)
    monkeypatch.setattr(apis, "RUN_EMAIL", False)
    monkeypatch.setattr(apis, "_spawn_harness_skill", capture_spawn)

    result = await _resolve(brief_id, "approve")

    assert spawned == []
    assert "re-dispatched" not in result["action_taken"]


@pytest.mark.asyncio
@pytest.mark.parametrize("gate_action", ["operator_only", "", "unrecognized"])
async def test_non_swarm_gate_actions_never_dispatch(
    gate_action, monkeypatch, release_store
):
    records, brief_id, task_id = release_store
    records[brief_id]["snapshot"]["gate_action"] = gate_action
    dispatches: list[tuple] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)

    result = await _resolve(brief_id, "approve")

    assert result["new_status"] == "approved"
    assert dispatches == []
    assert records[task_id]["snapshot"]["status"] == "awaiting_approval"
    assert records[brief_id]["snapshot"]["resolved_dispatched"] is False
    assert "re-dispatched" not in result["action_taken"]


@pytest.mark.asyncio
async def test_operator_only_producer_shape_never_releases(
    monkeypatch, release_store
):
    records, brief_id, task_id = release_store
    decision = evaluate_gate(
        confidence=1.0,
        action_type="operator_only",
        policy=ExecutionPolicy(entity_id="default", loaded=False),
    )
    brief = records[brief_id]["snapshot"]
    brief["gate_action"] = decision.action.value
    brief["blast_radius"] = decision.blast_radius.value
    brief["reason"] = decision.reason
    records[task_id]["snapshot"]["action_type"] = "operator_only"
    brief["status"] = "approved"
    dispatches: list[tuple] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)

    released = await apis.handle_checkpoint_brief(brief_id, brief, _Notifier())

    assert decision.action.value == "checkpoint_plan_approval"
    assert decision.blast_radius.value == "never"
    assert released is False
    assert dispatches == []
    assert records[brief_id]["snapshot"]["resolved_dispatched"] is False
    assert records[brief_id]["snapshot"]["status"] == "approved_no_release"
    assert records[task_id]["snapshot"]["status"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_current_never_classification_overrides_stale_low_brief(
    monkeypatch, release_store
):
    records, brief_id, task_id = release_store
    policy = ExecutionPolicy(entity_id="default", loaded=False)
    task = records[task_id]["snapshot"]
    task["action_type"] = "future_external_action"
    decision = evaluate_gate(
        confidence=1.0,
        action_type=task["action_type"],
        policy=policy,
    )
    brief = records[brief_id]["snapshot"]
    brief.update(
        {
            "status": "approved",
            "gate_action": "checkpoint_plan_approval",
            "blast_radius": "low",
        }
    )
    dispatches: list[tuple] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)

    released = await apis.handle_checkpoint_brief(brief_id, brief, _Notifier())

    assert decision.blast_radius.value == "never"
    assert released is False
    assert dispatches == []
    assert brief["resolved_dispatched"] is False
    assert brief["status"] == "approved_no_release"


@pytest.mark.asyncio
async def test_failed_nonrelease_close_cannot_make_approval_reusable(
    monkeypatch, release_store
):
    records, brief_id, task_id = release_store
    brief = records[brief_id]["snapshot"]
    brief.update(
        {
            "status": "approved",
            "gate_action": "operator_only",
            "resolved_dispatched": False,
        }
    )
    dispatches: list[tuple] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)
    monkeypatch.setattr(
        apis,
        "close_checkpoint_without_release",
        lambda *args, **kwargs: False,
    )

    first = await apis.handle_checkpoint_brief(brief_id, brief, _Notifier())
    brief.update(
        {
            "gate_action": "checkpoint_plan_approval",
            "blast_radius": "low",
        }
    )
    records[task_id]["snapshot"]["action_type"] = "local_edit"
    second = await apis.handle_checkpoint_brief(brief_id, brief, _Notifier())

    assert first is False
    assert second is False
    assert dispatches == []
    assert brief["resolved_dispatched"] is True


@pytest.mark.asyncio
async def test_already_approved_unstamped_checkpoint_releases_task(
    monkeypatch, release_store
):
    """Legacy recovery: an approved event may predate the inline MCP consumer."""
    records, brief_id, task_id = release_store
    brief = records[brief_id]["snapshot"]
    brief.update({"status": "approved", "resolved_dispatched": False})
    original_dispatch = apis.dispatch_task
    dispatches: list[str] = []

    async def spy_dispatch(entity_id, *args, **kwargs):
        dispatches.append(entity_id)
        await original_dispatch(entity_id, *args, **kwargs)

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)

    released = await apis.handle_checkpoint_brief(brief_id, brief, _Notifier())

    assert released is True
    assert dispatches == [task_id]
    assert brief["resolved_dispatched"] is True
    assert records[task_id]["snapshot"]["status"] == "routed"


@pytest.mark.asyncio
@pytest.mark.parametrize("blast_radius", [None, "", "never", "critical"])
async def test_absent_never_or_unknown_blast_radius_never_releases(
    blast_radius, monkeypatch, release_store
):
    records, brief_id, task_id = release_store
    if blast_radius is None:
        records[brief_id]["snapshot"].pop("blast_radius")
    else:
        records[brief_id]["snapshot"]["blast_radius"] = blast_radius
    dispatches: list[tuple] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)

    result = await _resolve(brief_id, "approve")

    assert dispatches == []
    assert records[brief_id]["snapshot"]["resolved_dispatched"] is False
    assert records[task_id]["snapshot"]["status"] == "awaiting_approval"
    assert "re-dispatched" not in result["action_taken"]


@pytest.mark.asyncio
async def test_cross_tenant_task_is_not_dispatched(monkeypatch, release_store):
    records, brief_id, task_id = release_store
    records[task_id]["snapshot"]["user_id"] = "tenant-b"
    dispatches: list[tuple] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)

    result = await _resolve(brief_id, "approve")

    assert dispatches == []
    assert records[task_id]["snapshot"]["status"] == "awaiting_approval"
    assert records[brief_id]["snapshot"]["resolved_dispatched"] is False
    assert "re-dispatched" not in result["action_taken"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("brief_user_id", "task_user_id", "should_dispatch"),
    [
        (None, None, False),
        (None, "tenant-a", False),
        ("tenant-a", None, False),
        ("tenant-a", "tenant-b", False),
        ("tenant-a", "tenant-a", True),
    ],
)
async def test_release_requires_matching_present_tenant_provenance(
    brief_user_id,
    task_user_id,
    should_dispatch,
    monkeypatch,
    release_store,
):
    records, brief_id, task_id = release_store
    brief = records[brief_id]["snapshot"]
    task = records[task_id]["snapshot"]
    if brief_user_id is None:
        brief.pop("user_id")
    else:
        brief["user_id"] = brief_user_id
    if task_user_id is None:
        task.pop("user_id")
    else:
        task["user_id"] = task_user_id
    dispatches: list[tuple] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)

    await _resolve(brief_id, "approve")

    assert bool(dispatches) is should_dispatch
    if not should_dispatch:
        assert records[brief_id]["snapshot"]["resolved_dispatched"] is False
        assert records[task_id]["snapshot"]["status"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_non_releasable_approval_cannot_be_reclassified_and_replayed(
    monkeypatch, release_store
):
    records, brief_id, _task_id = release_store
    brief = records[brief_id]["snapshot"]
    brief.update(
        {
            "status": "approved",
            "gate_action": "operator_only",
            "resolved_dispatched": False,
        }
    )
    dispatches: list[tuple] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)

    first = await apis.handle_checkpoint_brief(brief_id, brief, _Notifier())
    brief["gate_action"] = "checkpoint_plan_approval"
    second = await apis.handle_checkpoint_brief(brief_id, brief, _Notifier())

    assert first is False
    assert second is False
    assert dispatches == []
    assert brief["resolved_dispatched"] is False
    assert brief["status"] == "approved_no_release"


@pytest.mark.asyncio
async def test_missing_task_does_not_claim_release(monkeypatch, release_store):
    records, brief_id, task_id = release_store
    del records[task_id]
    dispatches: list[tuple] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)

    result = await _resolve(brief_id, "approve")

    assert dispatches == []
    assert records[brief_id]["snapshot"]["resolved_dispatched"] is False
    assert "re-dispatched" not in result["action_taken"]


@pytest.mark.asyncio
async def test_task_without_route_blocks_without_claiming_release(
    monkeypatch, release_store
):
    records, brief_id, task_id = release_store
    task = records[task_id]["snapshot"]
    task.update(
        {
            "title": "Unclassified work",
            "body": "No domain signal is present.",
            "assigned_to": "",
            "tags": [],
        }
    )
    monkeypatch.setattr(apis._activity, "started", lambda message: pytest.fail(
        "unroutable task must not start a dispatch activity"
    ))

    result = await _resolve(brief_id, "approve")

    assert records[task_id]["snapshot"]["status"] == "blocked"
    assert records[brief_id]["snapshot"]["resolved_dispatched"] is True
    assert "re-dispatched" not in result["action_taken"]


@pytest.mark.asyncio
async def test_non_checkpoint_entity_fails_closed(monkeypatch, release_store):
    records, brief_id, _task_id = release_store
    records[brief_id]["entity_type"] = "task"

    result = await _resolve(brief_id, "approve")

    assert "not a checkpoint_brief" in result["error"]


@pytest.mark.asyncio
async def test_reject_still_declines_without_dispatch(monkeypatch, release_store):
    records, brief_id, task_id = release_store
    dispatches: list[tuple] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)

    result = await _resolve(brief_id, "reject")

    assert result["new_status"] == "rejected"
    assert records[task_id]["snapshot"]["status"] == "declined"
    assert dispatches == []
