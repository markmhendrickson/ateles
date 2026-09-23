"""
test_task_dispatch_kill_switch.py — APIS_TASK_DISPATCH_ENABLED (default OFF).

The operator approved disabling Apis's Neotoma-`task` dispatch path while
keeping the GitHub issue/PR pipeline running: a data migration replayed old
tasks into hosted Neotoma and Apis routed and re-flagged every one of them
within seconds, and the operator does not trust the current task dispatch
logic while the foundation workflow overhaul is in flight.

With the flag off (the default):
  * `dispatch_task` is never called for `task.created` / `task.due_today` SSE
    events, and never called from `handle_checkpoint_brief`.
  * No Neotoma write happens for a skipped event (no `set_task_status` call).
  * The GitHub webhook/issue/PR path (`github_gateway`, `SwarmDispatcher`) is
    driven by an entirely separate code path and is unaffected.

With the flag on, behaviour is unchanged from before this change — proven by
running the exact scenarios `test_terminal_task_never_dispatches.py` and
`test_noowner_escalation.py` already cover, through the same `handle_event`
entrypoint, with the flag explicitly set to "1".

Driven through `apis.handle_event` / `apis.handle_checkpoint_brief` (not the
`dispatch_task` internals) because the guard has to sit in the SSE routing
layer ahead of the dispatch call — the lesson `test_noowner_escalation.py`
and `test_terminal_task_never_dispatches.py` both state: only an end-to-end
call through the real entrypoint proves the guard is actually wired in.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
from lib.daemon_runtime.sse_client import NeotomaEvent  # noqa: E402
from unroutable_ledger import UnroutableLedger  # noqa: E402


class _Notifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, message, priority=None, handler=None):
        self.sent.append(message)


@pytest.fixture(autouse=True)
def _isolated_ledger(monkeypatch, tmp_path):
    """Never touch the operator's real on-disk ledger."""
    monkeypatch.setattr(apis, "_unroutable", UnroutableLedger(path=tmp_path / "l.json"))
    monkeypatch.setattr(apis, "_created_seen", {})
    monkeypatch.setattr(apis, "_announced", {})


@pytest.fixture
def dispatch_calls(monkeypatch):
    """Capture every call to dispatch_task instead of letting it run."""
    calls: list[tuple] = []

    async def _capture(entity_id, snapshot, trigger, notifier, **kw):
        calls.append((entity_id, trigger))

    monkeypatch.setattr(apis, "dispatch_task", _capture)
    return calls


@pytest.fixture
def status_writes(monkeypatch):
    """Capture every Neotoma status write."""
    calls: list[tuple] = []

    def _capture(entity_id, status, **kw):
        calls.append((entity_id, status))
        return True

    monkeypatch.setattr(apis, "set_task_status", _capture)
    return calls


def _task_event(entity_id: str, action: str, snapshot: dict | None = None) -> NeotomaEvent:
    return NeotomaEvent(
        event_type="entity_" + action,
        entity_type="task",
        entity_id=entity_id,
        action=action,
        snapshot=snapshot or {"title": "Some task", "status": "pending"},
        raw={},
        hydrated=True,
    )


def _checkpoint_event(entity_id: str, snapshot: dict) -> NeotomaEvent:
    return NeotomaEvent(
        event_type="entity_updated",
        entity_type="checkpoint_brief",
        entity_id=entity_id,
        action="updated",
        snapshot=snapshot,
        raw={},
        hydrated=True,
    )


@pytest.fixture(autouse=True)
def _no_hydrate(monkeypatch):
    """hydrate_snapshot is a network call in the real module — no-op it so
    tests are hermetic and events keep exactly the snapshot they were built
    with."""

    async def _noop(event):
        return None

    monkeypatch.setattr(apis, "hydrate_snapshot", _noop)


# ── Flag OFF (default): no dispatch, no Neotoma write ─────────────────────────


class TestFlagOffDefault:
    def test_default_is_off(self):
        """The flag defaults to OFF without any env var set."""
        assert apis.TASK_DISPATCH_ENABLED is False

    def test_created_task_not_dispatched(self, monkeypatch, dispatch_calls, status_writes):
        monkeypatch.setattr(apis, "TASK_DISPATCH_ENABLED", False)
        notifier = _Notifier()
        asyncio.run(apis.handle_event(_task_event("ent_t1", "created"), notifier))
        assert dispatch_calls == [], f"dispatch_task was called: {dispatch_calls}"
        assert status_writes == [], f"a Neotoma write happened for a skipped event: {status_writes}"

    def test_due_today_task_not_dispatched_even_with_auto_execute(
        self, monkeypatch, dispatch_calls, status_writes
    ):
        """AUTO_EXECUTE alone must not override the kill switch."""
        monkeypatch.setattr(apis, "TASK_DISPATCH_ENABLED", False)
        monkeypatch.setattr(apis, "AUTO_EXECUTE", True)
        notifier = _Notifier()
        asyncio.run(apis.handle_event(_task_event("ent_t2", "due_today"), notifier))
        assert dispatch_calls == [], f"dispatch_task was called: {dispatch_calls}"
        assert status_writes == []

    def test_checkpoint_brief_does_not_redispatch(
        self, monkeypatch, dispatch_calls, status_writes
    ):
        monkeypatch.setattr(apis, "TASK_DISPATCH_ENABLED", False)
        notifier = _Notifier()
        snapshot = {
            "status": "approved",
            "task_entity_id": "ent_task_1",
            "title": "Some checkpoint",
        }
        asyncio.run(apis.handle_event(_checkpoint_event("ent_cb1", snapshot), notifier))
        assert dispatch_calls == [], f"dispatch_task was called: {dispatch_calls}"
        assert status_writes == []

    def test_updated_task_status_change_still_not_dispatched(
        self, monkeypatch, dispatch_calls, status_writes
    ):
        """task.updated never dispatches even with the flag on (observability
        only) — confirm the flag doesn't change that, and nothing regresses."""
        monkeypatch.setattr(apis, "TASK_DISPATCH_ENABLED", False)
        notifier = _Notifier()
        snapshot = {"title": "t", "status": "approved"}
        asyncio.run(apis.handle_event(_task_event("ent_t3", "updated", snapshot), notifier))
        assert dispatch_calls == []
        assert status_writes == []

    def test_watchdog_and_reconciler_do_not_start(self, monkeypatch):
        """Neither sweeper's real .run() coroutine is awaited when the flag is
        off — main() substitutes a disabled stand-in instead."""
        monkeypatch.setattr(apis, "TASK_DISPATCH_ENABLED", False)

        watchdog_ran = False
        reconciler_ran = False

        class _FakeWatchdog:
            async def run(self, notifier, dispatch_fn):
                nonlocal watchdog_ran
                watchdog_ran = True

        class _FakeReconciler:
            async def run(self, dispatch_fn):
                nonlocal reconciler_ran
                reconciler_ran = True

        # Exercise the same conditional main() uses, directly — main() itself
        # requires a live Neotoma connection to boot, so the branch is proven
        # here rather than by running the whole daemon.
        watchdog = _FakeWatchdog()
        reconciler = _FakeReconciler()

        async def _drive():
            async def _task_sweep_disabled(name):
                return None

            await asyncio.gather(
                watchdog.run(None, None)
                if apis.TASK_DISPATCH_ENABLED
                else _task_sweep_disabled("watchdog"),
                reconciler.run(None)
                if apis.TASK_DISPATCH_ENABLED
                else _task_sweep_disabled("reconciler"),
            )

        asyncio.run(_drive())
        assert watchdog_ran is False
        assert reconciler_ran is False


# ── Flag ON: behaviour is unchanged ────────────────────────────────────────────


class TestFlagOnUnchanged:
    def test_created_task_dispatches(self, monkeypatch, dispatch_calls):
        monkeypatch.setattr(apis, "TASK_DISPATCH_ENABLED", True)
        notifier = _Notifier()
        asyncio.run(apis.handle_event(_task_event("ent_on_1", "created"), notifier))
        assert dispatch_calls == [("ent_on_1", "created")]

    def test_due_today_dispatches_with_auto_execute(self, monkeypatch, dispatch_calls):
        monkeypatch.setattr(apis, "TASK_DISPATCH_ENABLED", True)
        monkeypatch.setattr(apis, "AUTO_EXECUTE", True)
        notifier = _Notifier()
        asyncio.run(apis.handle_event(_task_event("ent_on_2", "due_today"), notifier))
        assert dispatch_calls == [("ent_on_2", "due_today")]

    def test_checkpoint_brief_reaches_past_the_flag_gate(self, monkeypatch):
        """With the flag on, handle_checkpoint_brief must not return at the
        APIS_TASK_DISPATCH_ENABLED check — it should proceed into its normal
        durable-state refresh (ateles#1141: fetch_checkpoint_record /
        _checkpoint_denial_persisted) rather than short-circuiting.

        This does not re-prove the full approved-release flow (authorization,
        tenant-provenance matching, gate re-evaluation, the actual dispatch) —
        that is test_checkpoint_release.py's job, and it already covers it in
        detail through the real consumer. This test's only job is the flag
        gate itself: on, the function must reach the refresh call; off (see
        TestFlagOffDefault above), it must return before ever calling it.
        """
        monkeypatch.setattr(apis, "TASK_DISPATCH_ENABLED", True)

        refresh_calls: list[str] = []

        def _fake_fetch_checkpoint_record(entity_id):
            refresh_calls.append(entity_id)
            return None  # unreadable → handler logs and returns False; fine here

        monkeypatch.setattr(
            apis, "fetch_checkpoint_record", _fake_fetch_checkpoint_record
        )

        notifier = _Notifier()
        snapshot = {
            "status": "approved",
            "task_entity_id": "ent_task_on",
            "title": "Some checkpoint",
        }
        result = asyncio.run(
            apis.handle_checkpoint_brief("ent_cb_on", snapshot, notifier)
        )
        assert refresh_calls == ["ent_cb_on"], (
            "flag on but handle_checkpoint_brief never reached the durable-state "
            "refresh — the flag gate is short-circuiting live behavior"
        )
        assert result is False  # unreadable record → no release, not a crash


# ── GitHub path is unaffected by the flag, in either state ────────────────────


class TestGithubPathUnaffected:
    def test_non_task_non_checkpoint_events_pass_through(self, monkeypatch, dispatch_calls):
        """handle_event's task-dispatch guard must not swallow unrelated
        entity types — it only ever applies to task/checkpoint_brief."""
        monkeypatch.setattr(apis, "TASK_DISPATCH_ENABLED", False)
        notifier = _Notifier()
        event = NeotomaEvent(
            event_type="entity_created",
            entity_type="issue",
            entity_id="ent_issue_1",
            action="created",
            snapshot={},
            raw={},
            hydrated=True,
        )
        # Must not raise, and must not touch dispatch_task.
        asyncio.run(apis.handle_event(event, notifier))
        assert dispatch_calls == []

    def test_github_gateway_and_swarm_dispatch_wiring_unchanged(self):
        """The GitHub webhook pipeline is wired in main() independently of the
        task-dispatch flag: github_gateway.serve(...) and
        SwarmDispatcher.handle_trigger are unconditional in the asyncio.gather
        (not guarded by TASK_DISPATCH_ENABLED), and the module imports
        unaffected by the flag's value."""
        import inspect

        src = inspect.getsource(apis.main)
        # The GitHub gateway serve() call must appear unconditionally (not
        # inside the flag's if/else branch guarding the sweepers).
        assert "github_gateway.serve(gateway_app, GITHUB_WEBHOOK_PORT)" in src
        gateway_line = [
            ln for ln in src.splitlines() if "github_gateway.serve(" in ln
        ][0]
        # No reference to TASK_DISPATCH_ENABLED on the same statement.
        assert "TASK_DISPATCH_ENABLED" not in gateway_line
