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
    records = {
        brief_id: {
            "entity_type": CHECKPOINT_TYPE,
            "snapshot": {
                "status": "awaiting_operator",
                "resolved_dispatched": False,
                "task_entity_id": task_id,
                "gate_action": "checkpoint_plan_approval",
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

    def set_status(task_entity_id, status, *, reason=None, **kwargs):
        value = status.value if hasattr(status, "value") else str(status)
        records[task_entity_id]["snapshot"]["status"] = value
        if reason is not None:
            records[task_entity_id]["snapshot"]["blocked_reason"] = reason
        return True

    def stamp(checkpoint_entity_id, *, handler):
        records[checkpoint_entity_id]["snapshot"]["resolved_dispatched"] = True
        return True

    monkeypatch.setattr(server, "_get", get)
    monkeypatch.setattr(server, "_correct", correct)
    monkeypatch.setattr(apis, "fetch_task_snapshot", fetch_task)
    monkeypatch.setattr(apis, "set_task_status", set_status)
    monkeypatch.setattr(apis, "stamp_checkpoint_dispatched", stamp)
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
async def test_failed_stamp_does_not_dispatch(monkeypatch, release_store):
    records, brief_id, task_id = release_store
    dispatches: list[tuple] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)
    monkeypatch.setattr(
        apis, "stamp_checkpoint_dispatched", lambda checkpoint_id, *, handler: False
    )

    result = await _resolve(brief_id, "approve")

    assert dispatches == []
    assert records[task_id]["snapshot"]["status"] == "awaiting_approval"
    assert records[brief_id]["snapshot"]["resolved_dispatched"] is False
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
