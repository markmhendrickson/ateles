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
import hashlib
import inspect
import importlib.util
import json
import plistlib
import sys
import types
from pathlib import Path

import pytest
import yaml

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
from lib.daemon_runtime import gating as gating_module  # noqa: E402
from lib.daemon_runtime.gating import (  # noqa: E402
    ExecutionPolicy,
    build_checkpoint_authorization_envelope,
    evaluate_gate,
)

TEST_RESOLVER_JKT = "A" * 43

CHECKPOINT_TYPE = "checkpoint_" + "brief"
REAL_READ_AUTHENTICATED_RESOLUTION = (
    gating_module.read_authenticated_checkpoint_resolution
)


class _Notifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message, priority=None, handler=None, **kwargs):
        self.sent.append(message)

    def clear_dedupe(self, key):
        pass


async def _resolve(
    checkpoint_id: str,
    action: str,
    resolver_aauth_headers: dict[str, str] | None = None,
) -> dict:
    """Accept both the old sync shape and the fixed async implementation."""
    if resolver_aauth_headers is None:
        resolver_aauth_headers = {
            "signature-key": "caller-proof",
            "signature-input": "caller-proof",
            "signature": "caller-proof",
            "content-digest": "caller-proof",
            "content-type": "application/json",
        }
    result = server._resolve_checkpoint(
        checkpoint_id, action, resolver_aauth_headers=resolver_aauth_headers
    )
    if inspect.isawaitable(result):
        return await result
    return result


@pytest.fixture
def release_store(monkeypatch, tmp_path):
    brief_id = "ent_cp1"
    task_id = "ent_task_1"
    records = {
        brief_id: {
            "entity_id": brief_id,
            "entity_type": CHECKPOINT_TYPE,
            "observation_count": 1,
            "last_observation_at": "2026-09-21T00:00:00Z",
            "snapshot": {
                "status": "awaiting_operator",
                "resolved_dispatched": False,
                "task_entity_id": task_id,
                "gate_action": "checkpoint_plan_approval",
                "blast_radius": "low",
                "policy_entity_id": "default",
                "title": "Approve the bounded implementation",
                "user_id": "tenant-a",
            },
        },
        task_id: {
            "entity_id": task_id,
            "entity_type": "task",
            "observation_count": 1,
            "last_observation_at": "2026-09-21T00:00:00Z",
            "snapshot": {
                "status": "awaiting_approval",
                "blocked_reason": "waiting for operator approval",
                "assigned_to": "cicada",
                "title": "Implement the bounded change",
                "body": "Engineering work in the dispatcher.",
                "user_id": "tenant-a",
                "action_type": "local_edit",
                "confidence": 0.3,
            },
        },
    }

    def get(path, params=None):
        return records.get(path.rsplit("/", 1)[-1])

    def correct(entity_id, entity_type, field, value, idempotency_key, **_kwargs):
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

    def close_without_release(checkpoint_entity_id, *, handler, reason):
        records[checkpoint_entity_id]["snapshot"]["status"] = "approved_no_release"
        return True

    def require_fresh(checkpoint_entity_id, *, handler, reason):
        records[checkpoint_entity_id]["snapshot"]["status"] = (
            "approved_requires_fresh_approval"
        )
        return True

    monkeypatch.setattr(server, "_get", get)
    monkeypatch.setattr(server, "_correct", correct)
    monkeypatch.setattr(
        server,
        "_checkpoint_resolver_authority",
        lambda checkpoint_id, record: (
            "ateles@ateles-swarm",
            TEST_RESOLVER_JKT,
            "tenant-a",
        ),
    )
    monkeypatch.setattr(
        server,
        "_authenticate_checkpoint_resolver",
        lambda *args, **kwargs: {"sub": "ateles@ateles-swarm"},
    )
    monkeypatch.setattr(apis, "fetch_task_snapshot", fetch_task)
    monkeypatch.setattr(
        apis,
        "fetch_task_record",
        lambda entity_id: (
            records.get(entity_id)
            if records.get(entity_id, {}).get("entity_type") == "task"
            else None
        ),
    )
    monkeypatch.setattr(
        apis,
        "fetch_checkpoint_record",
        lambda entity_id: (
            records.get(entity_id)
            if records.get(entity_id, {}).get("entity_type") == CHECKPOINT_TYPE
            else None
        ),
    )
    monkeypatch.setattr(
        apis,
        "fetch_entity_user_id",
        lambda entity_id: (records.get(entity_id, {}).get("snapshot") or {}).get(
            "user_id"
        ),
    )
    policy = ExecutionPolicy(
        entity_id="default",
        low_blast_action_types=frozenset({"local_edit"}),
        high_blast_action_types=frozenset(),
        loaded=True,
    )
    decision = evaluate_gate(confidence=0.3, action_type="local_edit", policy=policy)
    encoded_authority = build_checkpoint_authorization_envelope(
        task_record=records[task_id],
        policy=policy,
        decision=decision,
        action_type="local_edit",
        user_id="tenant-a",
        required_approver_jkt=TEST_RESOLVER_JKT,
    )
    authority = json.loads(encoded_authority)
    records[brief_id]["snapshot"]["body"] = encoded_authority
    monkeypatch.setattr(
        apis,
        "read_authenticated_checkpoint_authorization",
        lambda checkpoint_id, record: (
            dict(authority)
            if (record.get("snapshot") or {}).get("body") == encoded_authority
            else None
        ),
    )

    def read_matching_approval(
        checkpoint_id,
        record,
        *,
        required_approver_sub,
        required_approver_jkt,
        expected_user_id,
        expected_resolution="approved",
    ):
        assert checkpoint_id == brief_id
        assert required_approver_sub == "ateles@ateles-swarm"
        assert required_approver_jkt == TEST_RESOLVER_JKT
        assert expected_user_id == "tenant-a"
        return {
            "principal_sub": "ateles@ateles-swarm",
            "observation_id": "obs-approval",
        }

    monkeypatch.setattr(
        gating_module,
        "read_authenticated_checkpoint_resolution",
        read_matching_approval,
    )
    monkeypatch.setattr(
        apis,
        "read_authenticated_checkpoint_resolution",
        read_matching_approval,
        raising=False,
    )
    monkeypatch.setattr(apis, "resolve_policy_for_agent", lambda skill: policy)
    monkeypatch.setattr(apis, "set_task_status", set_status)
    monkeypatch.setattr(
        apis,
        "mark_task_declined",
        lambda entity_id, **kwargs: set_status(entity_id, "declined"),
    )
    monkeypatch.setattr(apis, "stamp_checkpoint_dispatched", stamp)
    monkeypatch.setattr(
        apis,
        "close_checkpoint_without_release",
        close_without_release,
        raising=False,
    )
    monkeypatch.setattr(apis, "require_fresh_checkpoint_approval", require_fresh)
    monkeypatch.setattr(
        apis, "_file_fresh_checkpoint", lambda **kwargs: "ent_fresh_checkpoint"
    )
    monkeypatch.setattr(apis, "DRY_RUN", True)
    monkeypatch.setenv(
        "APIS_CHECKPOINT_DENIAL_DIR", str(tmp_path / "checkpoint-denials")
    )
    monkeypatch.setattr(apis.Notifier, "from_neotoma", lambda: _Notifier())

    # Keep the effect test hermetic: ActivityLogger is observability, not the
    # release mechanism under test.
    class _Job:
        def finished(self, message):
            return None

    monkeypatch.setattr(apis._activity, "started", lambda message: _Job())

    return records, brief_id, task_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "approval_failure",
    ["missing", "unreadable", "mismatched"],
)
async def test_unattributed_approval_never_reaches_gate_override(
    approval_failure, monkeypatch, release_store
):
    """The actual override sink must fail closed on approval attribution."""
    records, brief_id, task_id = release_store
    brief = records[brief_id]["snapshot"]
    brief["status"] = "approved"
    records[brief_id]["provenance"] = {"status": "obs-approval"}
    observations = [
        {
            "id": "obs-approval",
            "fields": {"status": "approved"},
            "user_id": "tenant-a",
            "provenance": {
                "agent_sub": "ateles@ateles-swarm",
                    "agent_thumbprint": TEST_RESOLVER_JKT,
                "attribution_tier": "software",
            },
        }
    ]
    if approval_failure == "missing":
        records[brief_id]["provenance"] = {}
    elif approval_failure == "unreadable":
        observations = []
    else:
        observations[0]["provenance"]["agent_sub"] = "other@ateles-swarm"
    dispatches: list[tuple] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)
    monkeypatch.setattr(
        apis,
        "read_authenticated_checkpoint_resolution",
        REAL_READ_AUTHENTICATED_RESOLUTION,
    )
    monkeypatch.setattr(
        gating_module,
        "_fetch_entity_observations",
        lambda checkpoint_id: observations,
    )

    released = await apis.handle_checkpoint_brief(brief_id, brief, _Notifier())

    assert released is False
    assert dispatches == []
    assert records[task_id]["snapshot"]["status"] == "awaiting_approval"
    assert brief["resolved_dispatched"] is False
    assert brief["status"] == "approved_requires_fresh_approval"


@pytest.mark.asyncio
@pytest.mark.parametrize("resolver_failure", ["missing", "mismatched"])
async def test_unattributed_rejection_never_declines_task(
    resolver_failure, monkeypatch, release_store
):
    """The reject consumer is a task-decline sink and authenticates its caller."""
    records, brief_id, task_id = release_store
    brief = records[brief_id]["snapshot"]
    brief["status"] = "rejected"
    records[brief_id]["provenance"] = {"status": "obs-rejection"}
    observations = [
        {
            "id": "obs-rejection",
            "fields": {"status": "rejected"},
            "user_id": "tenant-a",
            "provenance": {
                "agent_sub": "ateles@ateles-swarm",
                    "agent_thumbprint": TEST_RESOLVER_JKT,
                "attribution_tier": "software",
            },
        }
    ]
    if resolver_failure == "missing":
        records[brief_id]["provenance"] = {}
    else:
        observations[0]["provenance"]["agent_sub"] = "other@ateles-swarm"
    declines: list[str] = []

    monkeypatch.setattr(
        apis,
        "read_authenticated_checkpoint_resolution",
        REAL_READ_AUTHENTICATED_RESOLUTION,
    )
    monkeypatch.setattr(
        gating_module,
        "_fetch_entity_observations",
        lambda checkpoint_id: observations,
    )
    monkeypatch.setattr(
        apis,
        "mark_task_declined",
        lambda entity_id, **kwargs: declines.append(entity_id) or True,
    )

    consumed = await apis.handle_checkpoint_brief(brief_id, brief, _Notifier())

    assert consumed is False
    assert declines == []
    assert records[task_id]["snapshot"]["status"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_authenticated_rejection_declines_task(
    monkeypatch, release_store
):
    records, brief_id, task_id = release_store
    brief = records[brief_id]["snapshot"]
    brief["status"] = "rejected"
    records[brief_id]["provenance"] = {"status": "obs-rejection"}
    observations = [
        {
            "id": "obs-rejection",
            "fields": {"status": "rejected"},
            "user_id": "tenant-a",
            "provenance": {
                "agent_sub": "ateles@ateles-swarm",
                    "agent_thumbprint": TEST_RESOLVER_JKT,
                "attribution_tier": "software",
            },
        }
    ]

    monkeypatch.setattr(
        apis,
        "read_authenticated_checkpoint_resolution",
        REAL_READ_AUTHENTICATED_RESOLUTION,
    )
    monkeypatch.setattr(
        gating_module,
        "_fetch_entity_observations",
        lambda checkpoint_id: observations,
    )

    def decline(entity_id, **kwargs):
        records[entity_id]["snapshot"]["status"] = "declined"
        return True

    monkeypatch.setattr(apis, "mark_task_declined", decline)

    consumed = await apis.handle_checkpoint_brief(brief_id, brief, _Notifier())

    assert consumed is True
    assert records[task_id]["snapshot"]["status"] == "declined"


@pytest.mark.asyncio
async def test_unloaded_current_policy_never_reaches_gate_override(
    monkeypatch, release_store
):
    """A fallback policy cannot authorize the take even after approval."""
    records, brief_id, task_id = release_store
    fallback = ExecutionPolicy(
        entity_id="default",
        low_blast_action_types=frozenset({"local_edit"}),
        high_blast_action_types=frozenset(),
        loaded=False,
    )
    _bind_v2_authorization(
        monkeypatch,
        records,
        brief_id,
        task_id,
        action_type="local_edit",
        policy=fallback,
    )
    brief = records[brief_id]["snapshot"]
    brief["status"] = "approved"
    dispatches: list[tuple] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)

    released = await apis.handle_checkpoint_brief(brief_id, brief, _Notifier())

    assert released is False
    assert dispatches == []
    assert records[task_id]["snapshot"]["status"] == "awaiting_approval"
    assert brief["resolved_dispatched"] is False


def _authorization_digest(
    *,
    task_id: str,
    user_id: str,
    action_type: str,
    gate_action: str,
    blast_radius: str,
    policy_id: str,
) -> str:
    canonical = json.dumps(
        {
            "action_type": action_type,
            "blast_radius": blast_radius,
            "gate_action": gate_action,
            "policy_id": policy_id,
            "task_entity_id": task_id,
            "user_id": user_id,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _bind_v2_authorization(
    monkeypatch,
    records: dict,
    brief_id: str,
    task_id: str,
    *,
    action_type: str,
    policy: ExecutionPolicy,
) -> dict:
    """Model a creation-observation authority, frozen before later mutation."""
    task_record = records[task_id]
    task_record["snapshot"]["action_type"] = action_type
    decision = evaluate_gate(
        confidence=float(task_record["snapshot"].get("confidence", 0.5)),
        action_type=action_type,
        policy=policy,
    )
    encoded = build_checkpoint_authorization_envelope(
        task_record=task_record,
        policy=policy,
        decision=decision,
        action_type=action_type,
        user_id="tenant-a",
        required_approver_jkt=TEST_RESOLVER_JKT,
    )
    authority = json.loads(encoded)
    brief = records[brief_id]["snapshot"]
    brief.update(
        {
            "body": encoded,
            "gate_action": decision.action.value,
            "blast_radius": decision.blast_radius.value,
            "policy_entity_id": policy.entity_id,
        }
    )
    monkeypatch.setattr(
        apis,
        "read_authenticated_checkpoint_authorization",
        lambda checkpoint_id, record: dict(authority),
    )
    monkeypatch.setattr(apis, "resolve_policy_for_agent", lambda skill: policy)
    return authority


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
async def test_operator_only_producer_shape_never_releases(monkeypatch, release_store):
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
async def test_unknown_current_classification_requires_fresh_policy_brief(
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
    assert brief["status"] == "approved_requires_fresh_approval"


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
async def test_failed_close_and_stamp_denial_survives_consumer_restart(
    monkeypatch, release_store, tmp_path
):
    """A failed Neotoma terminal write still cannot make approval reusable."""
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
    stamp_attempts = 0

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    def fail_denial_stamp_then_allow_release(checkpoint_id, *, handler):
        nonlocal stamp_attempts
        stamp_attempts += 1
        if stamp_attempts == 1:
            return False
        brief["resolved_dispatched"] = True
        return True

    monkeypatch.setenv("APIS_CHECKPOINT_DENIAL_DIR", str(tmp_path / "denials"))
    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)
    monkeypatch.setattr(
        apis, "close_checkpoint_without_release", lambda *args, **kwargs: False
    )
    monkeypatch.setattr(
        apis, "stamp_checkpoint_dispatched", fail_denial_stamp_then_allow_release
    )

    first = await apis.handle_checkpoint_brief(brief_id, brief, _Notifier())

    # Model a different consumer process: module-local memory is absent, while
    # the host-persistent denial record remains.  Then mutate the old approval
    # into a superficially releasable shape.
    quarantine = getattr(apis, "_nonreleasable_checkpoint_quarantine", None)
    if quarantine is not None:
        quarantine.clear()
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
    assert brief["resolved_dispatched"] is False
    assert stamp_attempts == 1


@pytest.mark.asyncio
async def test_current_high_requires_fresh_approval_instead_of_stale_low_authority(
    monkeypatch, release_store
):
    records, brief_id, task_id = release_store
    brief = records[brief_id]["snapshot"]
    brief.update(
        {
            "status": "approved",
            "blast_radius": "low",
            "authorization_action_type": "local_edit",
            "authorization_context_version": 1,
            "policy_entity_id": "default",
            "authorization_context_digest": _authorization_digest(
                task_id=task_id,
                user_id="tenant-a",
                action_type="local_edit",
                gate_action="checkpoint_plan_approval",
                blast_radius="low",
                policy_id="default",
            ),
        }
    )
    records[task_id]["snapshot"]["action_type"] = "payment"
    dispatches: list[tuple] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)

    released = await apis.handle_checkpoint_brief(brief_id, brief, _Notifier())

    assert released is False
    assert dispatches == []
    assert brief["resolved_dispatched"] is False
    assert brief["status"] == "approved_requires_fresh_approval"


@pytest.mark.asyncio
async def test_bound_approval_rejects_same_action_task_payload_mutation(
    monkeypatch, release_store
):
    records, brief_id, task_id = release_store
    brief = records[brief_id]["snapshot"]
    policy = ExecutionPolicy(
        entity_id="default",
        confidence_threshold=0.95,
        high_blast_action_types=frozenset({"open_or_merge_pr"}),
        low_blast_action_types=frozenset(),
        loaded=True,
    )
    _bind_v2_authorization(
        monkeypatch,
        records,
        brief_id,
        task_id,
        action_type="open_or_merge_pr",
        policy=policy,
    )
    brief["status"] = "approved"
    records[task_id]["snapshot"]["body"] = "Changed recipient and amount"
    dispatches: list[tuple] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)

    released = await apis.handle_checkpoint_brief(brief_id, brief, _Notifier())

    assert released is False
    assert dispatches == []
    assert brief["status"] == "approved_requires_fresh_approval"


@pytest.mark.asyncio
async def test_bound_approval_rejects_same_policy_id_content_reclassification(
    monkeypatch, release_store
):
    records, brief_id, task_id = release_store
    brief = records[brief_id]["snapshot"]
    original_policy = ExecutionPolicy(
        entity_id="default",
        confidence_threshold=1.0,
        high_blast_action_types=frozenset(),
        low_blast_action_types=frozenset({"local_edit"}),
        loaded=True,
    )
    _bind_v2_authorization(
        monkeypatch,
        records,
        brief_id,
        task_id,
        action_type="local_edit",
        policy=original_policy,
    )
    brief["status"] = "approved"
    changed_policy = ExecutionPolicy(
        entity_id="default",
        high_blast_action_types=frozenset({"local_edit"}),
        low_blast_action_types=frozenset(),
        loaded=True,
    )
    monkeypatch.setattr(apis, "resolve_policy_for_agent", lambda skill: changed_policy)
    dispatches: list[tuple] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)

    released = await apis.handle_checkpoint_brief(brief_id, brief, _Notifier())

    assert released is False
    assert dispatches == []
    assert brief["status"] == "approved_requires_fresh_approval"


@pytest.mark.asyncio
async def test_recomputed_sibling_digest_cannot_rewrite_approved_authority(
    monkeypatch, release_store
):
    records, brief_id, task_id = release_store
    brief = records[brief_id]["snapshot"]
    policy = ExecutionPolicy(
        entity_id="default",
        confidence_threshold=1.0,
        high_blast_action_types=frozenset({"payment"}),
        low_blast_action_types=frozenset({"local_edit"}),
        loaded=True,
    )
    _bind_v2_authorization(
        monkeypatch,
        records,
        brief_id,
        task_id,
        action_type="local_edit",
        policy=policy,
    )
    brief.update(
        {
            "status": "approved",
            "blast_radius": "high",
            "authorization_action_type": "payment",
            "authorization_context_version": 1,
            "authorization_context_digest": "attacker-recomputed",
        }
    )
    records[task_id]["snapshot"]["action_type"] = "payment"
    dispatches: list[tuple] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)

    released = await apis.handle_checkpoint_brief(brief_id, brief, _Notifier())

    assert released is False
    assert dispatches == []
    assert brief["status"] == "approved_requires_fresh_approval"


@pytest.mark.asyncio
async def test_live_policy_custom_low_authorization_is_not_guessed_never(
    monkeypatch, release_store
):
    records, brief_id, task_id = release_store
    action_type = "tenant_bounded_local_transform"
    policy_id = "ent_custom_policy"
    brief = records[brief_id]["snapshot"]
    policy = ExecutionPolicy(
        entity_id=policy_id,
        confidence_threshold=1.0,
        low_blast_action_types=frozenset({action_type}),
        high_blast_action_types=frozenset(),
        loaded=True,
    )
    _bind_v2_authorization(
        monkeypatch,
        records,
        brief_id,
        task_id,
        action_type=action_type,
        policy=policy,
    )
    brief["status"] = "approved"
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
    assert brief["status"] == "approved"


@pytest.mark.asyncio
async def test_legacy_unknown_classification_requests_fresh_approval_non_destructively(
    monkeypatch, release_store
):
    records, brief_id, task_id = release_store
    brief = records[brief_id]["snapshot"]
    brief.update({"status": "approved", "blast_radius": "low"})
    records[task_id]["snapshot"]["action_type"] = "tenant_bounded_local_transform"
    dispatches: list[tuple] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)

    released = await apis.handle_checkpoint_brief(brief_id, brief, _Notifier())

    assert released is False
    assert dispatches == []
    assert brief["resolved_dispatched"] is False
    assert brief["status"] == "approved_requires_fresh_approval"


def test_replacement_checkpoint_uses_current_live_policy(monkeypatch):
    action_type = "tenant_bounded_local_transform"
    task = {
        "title": "Bounded transform",
        "assigned_to": "cicada",
        "action_type": action_type,
        "confidence": 0.99,
        "user_id": "tenant-a",
    }
    policy = ExecutionPolicy(
        entity_id="ent_current_policy",
        low_blast_action_types=frozenset({action_type}),
        high_blast_action_types=frozenset(),
        loaded=True,
    )
    writes: list[dict] = []
    notifier = _Notifier()
    task_record = {
        "entity_id": "ent_task",
        "entity_type": "task",
        "observation_count": 1,
        "last_observation_at": "2026-09-21T00:00:00Z",
        "snapshot": task,
    }

    monkeypatch.setattr(apis, "resolve_policy_for_agent", lambda skill: policy)
    monkeypatch.setattr(apis, "fetch_task_record", lambda task_id: task_record)
    monkeypatch.setattr(apis, "fetch_entity_user_id", lambda task_id: "tenant-a")
    monkeypatch.setattr(
        apis,
        "write_checkpoint_brief",
        lambda **kwargs: writes.append(kwargs) or "ent_fresh",
    )

    apis._file_fresh_checkpoint(
        prior_checkpoint_id="ent_old",
        task_id="ent_task",
        task_snapshot=task,
        notifier=notifier,
    )

    assert len(writes) == 1
    assert writes[0]["action_type"] == action_type
    assert writes[0]["decision"].blast_radius.value == "low"
    assert writes[0]["decision"].action.value == "checkpoint_plan_approval"
    assert writes[0]["decision"].policy_id == "ent_current_policy"
    assert writes[0]["idempotency_context"].startswith("fresh-")
    assert any("Fresh approval required" in message for message in notifier.sent)


def test_failed_replacement_write_leaves_old_approval_retriable_and_visible(
    monkeypatch, release_store
):
    records, brief_id, task_id = release_store
    brief = records[brief_id]["snapshot"]
    brief["status"] = "approved"
    notifier = _Notifier()
    monkeypatch.setattr(apis, "_file_fresh_checkpoint", lambda **kwargs: None)

    apis._require_fresh_release_authority(
        brief_id,
        task_id=task_id,
        task_snapshot=records[task_id]["snapshot"],
        notifier=notifier,
        reason="task changed",
    )

    assert brief["status"] == "approved"
    assert any("retry" in message.lower() for message in notifier.sent)


def test_denial_marker_rejects_relative_state_path(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("APIS_CHECKPOINT_DENIAL_DIR", "relative/denials")

    assert apis._persist_checkpoint_denial("ent_cp") is False


@pytest.mark.parametrize("entry_kind", ["directory", "symlink"])
def test_denial_marker_rejects_non_regular_existing_entry(
    entry_kind, monkeypatch, tmp_path
):
    root = tmp_path / "denials"
    monkeypatch.setenv("APIS_CHECKPOINT_DENIAL_DIR", str(root))
    marker = apis._checkpoint_denial_marker("ent_cp")
    marker.parent.mkdir(parents=True)
    if entry_kind == "directory":
        marker.mkdir()
    else:
        target = tmp_path / "target"
        target.write_text("denied\n")
        marker.symlink_to(target)

    assert apis._persist_checkpoint_denial("ent_cp") is False
    assert apis._checkpoint_denial_persisted("ent_cp") is False


@pytest.mark.asyncio
async def test_already_approved_unstamped_checkpoint_releases_task(
    monkeypatch, release_store
):
    """A v2-authorized approval may recover after its original consumer stopped."""
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
async def test_unsigned_generic_approved_checkpoint_never_dispatches(
    monkeypatch, release_store
):
    records, brief_id, _task_id = release_store
    brief = records[brief_id]["snapshot"]
    brief.update({"status": "approved", "resolved_dispatched": False})
    brief.pop("body", None)
    monkeypatch.setattr(
        apis, "read_authenticated_checkpoint_authorization", lambda *args: None
    )
    dispatches: list[tuple] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)

    released = await apis.handle_checkpoint_brief(brief_id, brief, _Notifier())

    assert released is False
    assert dispatches == []
    assert brief["resolved_dispatched"] is False
    assert brief["status"] == "approved_requires_fresh_approval"


@pytest.mark.asyncio
async def test_unsigned_legacy_approval_cannot_authorize_mutated_task_revision(
    monkeypatch, release_store
):
    records, brief_id, task_id = release_store
    brief = records[brief_id]["snapshot"]
    brief.update({"status": "approved", "resolved_dispatched": False})
    brief.pop("body", None)
    records[task_id]["snapshot"]["body"] = "changed after operator approval"
    monkeypatch.setattr(
        apis, "read_authenticated_checkpoint_authorization", lambda *args: None
    )
    dispatches: list[tuple] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)

    released = await apis.handle_checkpoint_brief(brief_id, brief, _Notifier())

    assert released is False
    assert dispatches == []
    assert brief["status"] == "approved_requires_fresh_approval"


def test_legacy_migration_creates_fresh_v2_approval_without_dispatch(
    monkeypatch, release_store
):
    records, brief_id, _task_id = release_store
    brief = records[brief_id]["snapshot"]
    brief.update({"status": "approved", "resolved_dispatched": False})
    brief.pop("body", None)
    monkeypatch.setattr(
        apis, "read_authenticated_checkpoint_authorization", lambda *args: None
    )
    dispatches: list[tuple] = []

    async def spy_dispatch(*args, **kwargs):
        dispatches.append((args, kwargs))

    monkeypatch.setattr(apis, "dispatch_task", spy_dispatch)

    replacement_id = apis.migrate_checkpoint_authority(brief_id, notifier=_Notifier())

    assert replacement_id == "ent_fresh_checkpoint"
    assert brief["status"] == "approved_requires_fresh_approval"
    assert brief["resolved_dispatched"] is False
    assert dispatches == []


def test_automatic_release_has_no_operator_specific_legacy_allowlist():
    assert not hasattr(apis, "_LEGACY_CHECKPOINT_RELEASES")


def test_runtime_configs_bind_absolute_persistent_denial_store():
    plist_path = _HERE / "com.ateles.apis.plist"
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["EnvironmentVariables"]["APIS_CHECKPOINT_DENIAL_DIR"] == (
        "/var/tmp/ateles/checkpoint-denials"
    )

    compose_path = (
        _HERE.parent.parent.parent / "deploy" / "cloud" / "docker-compose.yml"
    )
    compose = yaml.safe_load(compose_path.read_text())
    apis_service = compose["services"]["apis"]
    assert apis_service["environment"]["APIS_CHECKPOINT_DENIAL_DIR"] == (
        "/var/lib/ateles/checkpoint-denials"
    )
    assert (
        "apis-checkpoint-denials:/var/lib/ateles/checkpoint-denials"
        in apis_service["volumes"]
    )
    assert "apis-checkpoint-denials" in compose["volumes"]

    mcp_wrapper = (
        _HERE.parent.parent / "mcp" / "ateles" / "run_ateles_mcp.sh"
    ).read_text()
    assert (
        'APIS_CHECKPOINT_DENIAL_DIR="${APIS_CHECKPOINT_DENIAL_DIR:-'
        '/var/tmp/ateles/checkpoint-denials}"'
    ) in mcp_wrapper
    assert "export APIS_CHECKPOINT_DENIAL_DIR" in mcp_wrapper


def test_mcp_startup_proves_checkpoint_denial_store(monkeypatch, tmp_path):
    root = tmp_path / "mcp-checkpoint-denials"
    monkeypatch.setenv("APIS_CHECKPOINT_DENIAL_DIR", str(root))
    monkeypatch.setattr(
        server,
        "_load_apis_daemon",
        lambda: pytest.fail(
            "MCP startup imported the full Apis daemon dependency tree"
        ),
    )

    assert server._require_checkpoint_release_state() == root
    assert root.is_dir()


def test_denial_store_startup_fails_closed_when_unconfigured(monkeypatch):
    monkeypatch.delenv("APIS_CHECKPOINT_DENIAL_DIR", raising=False)

    with pytest.raises(RuntimeError, match="APIS_CHECKPOINT_DENIAL_DIR"):
        apis._require_checkpoint_denial_store()


@pytest.mark.asyncio
async def test_main_refuses_to_start_without_denial_store(monkeypatch):
    monkeypatch.delenv("APIS_CHECKPOINT_DENIAL_DIR", raising=False)
    monkeypatch.setattr(
        apis.AgentLoader,
        "load",
        lambda self: pytest.fail("startup advanced past denial-store validation"),
    )

    with pytest.raises(RuntimeError, match="APIS_CHECKPOINT_DENIAL_DIR"):
        await apis.main()


def test_denial_store_startup_proves_writable_directory(monkeypatch, tmp_path):
    root = tmp_path / "checkpoint-denials"
    monkeypatch.setenv("APIS_CHECKPOINT_DENIAL_DIR", str(root))

    assert apis._require_checkpoint_denial_store() == root
    assert root.is_dir()
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("bad_kind", ["relative", "file", "symlink"])
def test_denial_store_startup_rejects_unusable_binding(bad_kind, monkeypatch, tmp_path):
    if bad_kind == "relative":
        configured = "relative/checkpoint-denials"
    elif bad_kind == "file":
        configured_path = tmp_path / "not-a-directory"
        configured_path.write_text("no\n")
        configured = str(configured_path)
    else:
        target = tmp_path / "target"
        target.mkdir()
        configured_path = tmp_path / "linked"
        configured_path.symlink_to(target, target_is_directory=True)
        configured = str(configured_path)
    monkeypatch.setenv("APIS_CHECKPOINT_DENIAL_DIR", configured)

    with pytest.raises(RuntimeError):
        apis._require_checkpoint_denial_store()


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
    policy = ExecutionPolicy(
        entity_id="default",
        low_blast_action_types=frozenset({"local_edit"}),
        high_blast_action_types=frozenset(),
        loaded=True,
    )
    _bind_v2_authorization(
        monkeypatch,
        records,
        brief_id,
        task_id,
        action_type="local_edit",
        policy=policy,
    )
    monkeypatch.setattr(
        apis._activity,
        "started",
        lambda message: pytest.fail(
            "unroutable task must not start a dispatch activity"
        ),
    )

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
