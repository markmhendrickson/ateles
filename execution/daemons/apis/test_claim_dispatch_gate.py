"""Claim-gate behaviour through the real dispatch_task entrypoint.

Covers the fail-closed path when CLAIM_ENABLED=1 but the ClaimStore could not
be built (missing token / _claims is None). A helper-only unit would pass while
the wired guard still fell through to EXECUTING — so this drives the ship path.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
from lib.daemon_runtime.task_lifecycle import TaskStatus  # noqa: E402
from skill_runner import SkillResult  # noqa: E402
from unroutable_ledger import UnroutableLedger  # noqa: E402


class _Notifier:
    def send(self, message, priority=None, handler=None):
        pass


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(apis, "_unroutable", UnroutableLedger(path=tmp_path / "l.json"))
    monkeypatch.setattr(apis, "_created_seen", {})
    monkeypatch.setattr(apis, "DRY_RUN", False)
    monkeypatch.setattr(apis, "READINESS_GATE", False)
    monkeypatch.setattr(apis, "RUN_CONVERSATIONS", False)
    monkeypatch.setattr(apis, "RUN_EMAIL", False)


def _status_name(args) -> str | None:
    if not args:
        return None
    status = args[1] if len(args) > 1 else None
    if isinstance(status, TaskStatus):
        return status.value
    return str(status) if status is not None else None


def test_dispatch_fails_closed_when_claim_enabled_and_store_unavailable(monkeypatch):
    """CLAIM_ENABLED=1 + _claims=None must not reach EXECUTING (or spawn)."""
    status_calls: list[tuple] = []
    monkeypatch.setattr(
        apis, "set_task_status",
        lambda *a, **k: status_calls.append((a, k)),
    )
    monkeypatch.setattr(apis, "CLAIM_ENABLED", True)
    monkeypatch.setattr(apis, "_claims", None)

    spawned = {"n": 0}

    async def _boom(*a, **k):
        spawned["n"] += 1
        raise AssertionError("must not spawn when claim store unavailable")

    monkeypatch.setattr(apis, "_spawn_harness_skill", _boom)

    snapshot = {"title": "Fix the flaky CI pipeline", "tags": ["ops"]}
    asyncio.run(
        apis.dispatch_task(
            "ent_claim_gate",
            snapshot,
            trigger="created",
            notifier=_Notifier(),
            gate_override=True,
        )
    )

    executing = [c for c in status_calls if _status_name(c[0]) == TaskStatus.EXECUTING.value]
    assert executing == [], f"fail-closed violated: EXECUTING written ({status_calls})"
    assert spawned["n"] == 0


def test_dispatch_rollback_path_when_claims_disabled_reaches_executing(monkeypatch):
    """CLAIM_ENABLED=0 may still dispatch unclaimed (explicit rollback)."""
    status_calls: list[tuple] = []
    monkeypatch.setattr(
        apis, "set_task_status",
        lambda *a, **k: status_calls.append((a, k)),
    )
    monkeypatch.setattr(apis, "CLAIM_ENABLED", False)
    monkeypatch.setattr(apis, "_claims", None)

    async def _ok(*a, **k):
        return SkillResult("cicada", True, 0, "out", "", provider="claude")

    monkeypatch.setattr(apis, "_spawn_harness_skill", _ok)

    snapshot = {"title": "Fix the flaky CI pipeline", "tags": ["ops"]}
    asyncio.run(
        apis.dispatch_task(
            "ent_claim_off",
            snapshot,
            trigger="created",
            notifier=_Notifier(),
            gate_override=True,
        )
    )

    executing = [c for c in status_calls if _status_name(c[0]) == TaskStatus.EXECUTING.value]
    assert executing, f"rollback path must still EXECUTING; got {status_calls}"
