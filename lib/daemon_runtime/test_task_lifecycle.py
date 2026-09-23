"""Tests for the task lifecycle state machine + its status-write I/O contract."""

from __future__ import annotations

import task_lifecycle as tl
from task_lifecycle import (
    MAX_ATTEMPTS,
    TaskStatus,
    attempts_exhausted,
    backoff_seconds,
    can_transition,
)


class _Resp:
    def raise_for_status(self):
        pass

    def json(self):
        return {}


def _capture(monkeypatch):
    """Patch the module's bearer token + httpx.post; return the captured calls."""
    calls: list[dict] = []

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return _Resp()

    monkeypatch.setattr(tl, "NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(tl.httpx, "post", fake_post)
    return calls


# ── transition graph ────────────────────────────────────────────────────────


def test_happy_path_transitions():
    assert can_transition("pending", "routed")
    assert can_transition("routed", "executing")
    assert can_transition("executing", "done")
    assert can_transition("verified", "done")


def test_failure_and_recovery_transitions():
    assert can_transition("executing", "failed")
    assert can_transition("failed", "routed")     # retry
    assert can_transition("failed", "blocked")    # give up
    assert can_transition("blocked", "routed")    # operator remediation


def test_terminal_states_are_locked():
    assert not can_transition("done", "executing")
    assert not can_transition("declined", "routed")
    assert not can_transition("superseded", "routed")
    # same-state re-entry is always allowed (idempotent replay)
    assert can_transition("done", "done")


def test_guards():
    assert not can_transition("pending", "done")          # no skipping
    assert can_transition("weird_legacy", "done")          # unknown origin permissive
    assert can_transition("PENDING", "Routed")             # case-insensitive


def test_retry_policy():
    assert backoff_seconds(1) < backoff_seconds(2) < backoff_seconds(3)
    assert backoff_seconds(99) <= tl._BACKOFF_CAP
    assert attempts_exhausted(MAX_ATTEMPTS)
    assert not attempts_exhausted(0)


# ── status-write I/O contract ───────────────────────────────────────────────


def test_set_status_writes_status_and_reason(monkeypatch):
    calls = _capture(monkeypatch)
    ok = tl.set_task_status(
        "ent_t", TaskStatus.FAILED, handler="apis",
        from_status="executing", reason="boom", key_suffix="created",
    )
    assert ok
    by_field = {c["json"]["field"]: c["json"] for c in calls}
    assert set(by_field) == {"status", "blocked_reason"}
    assert by_field["status"]["value"] == "failed"
    assert by_field["status"]["entity_type"] == "task"
    assert by_field["status"]["entity_id"] == "ent_t"
    # idempotency key folds in handler + status + suffix
    assert by_field["status"]["idempotency_key"] == "taskstatus-apis-ent_t-failed-created"
    assert all(c["headers"]["Authorization"] == "Bearer test-token" for c in calls)


def test_set_status_done_writes_result(monkeypatch):
    calls = _capture(monkeypatch)
    tl.set_task_status("ent_t", TaskStatus.DONE, handler="apis", result="ok", key_suffix="created")
    fields = {c["json"]["field"] for c in calls}
    assert fields == {"status", "result"}


def test_set_status_remediation(monkeypatch):
    calls = _capture(monkeypatch)
    tl.set_task_status("ent_t", TaskStatus.ROUTED, handler="apis", remediation_id="ent_fix")
    fields = {c["json"]["field"] for c in calls}
    assert fields == {"status", "remediation_id"}


def test_set_status_fail_open_without_token(monkeypatch):
    monkeypatch.setattr(tl, "NEOTOMA_BEARER_TOKEN", "")
    # No token → returns False, never raises.
    assert tl.set_task_status("ent_t", TaskStatus.ROUTED, handler="apis") is False


def test_set_status_fail_open_on_http_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("network down")

    monkeypatch.setattr(tl, "NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(tl.httpx, "post", boom)
    assert tl.set_task_status("ent_t", TaskStatus.ROUTED, handler="apis") is False


# ── complete_task_with_result (ateles#1155) ─────────────────────────────────


def _lifecycle_under_test():
    """Load the worktree module by path.

    An editable install of ``ateles`` can shadow ``lib.daemon_runtime`` with a
    checkout that lacks ``complete_task_with_result``. Bare ``import
    task_lifecycle`` then aliases the installed module. File-path load keeps
    these tests honest against the code under review.
    """
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parent / "task_lifecycle.py"
    spec = importlib.util.spec_from_file_location(
        "task_lifecycle_under_test", path
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_complete_writes_result_before_status(monkeypatch):
    """Order is the point, not a coincidence of the implementation.

    `set_task_status` writes `status` first and its companions after, so a
    process killed between the two leaves a task reading DONE with no artifact
    reference — terminal, and silent about what finished it. The deliverable
    gate in Apis completes through this function instead, so the reference the
    completion rests on is durable before the task is closed.
    """
    tl_local = _lifecycle_under_test()
    calls = _capture(monkeypatch)
    # Re-bind capture onto the file-loaded module.
    monkeypatch.setattr(tl_local, "NEOTOMA_BEARER_TOKEN", "test-token")

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return _Resp()

    monkeypatch.setattr(tl_local.httpx, "post", fake_post)
    ok = tl_local.complete_task_with_result(
        "ent_t",
        handler="apis",
        result="[cicada] pull_request_link: markmhendrickson/ateles#999",
        from_status="executing",
    )
    assert ok is True
    assert [c["json"]["field"] for c in calls] == ["result", "status"]
    assert calls[1]["json"]["value"] == tl_local.TaskStatus.DONE.value


def test_complete_idempotency_key_carries_artifact_identity(monkeypatch):
    """Two attempts naming different artifacts must not dedupe into one write."""
    tl_local = _lifecycle_under_test()
    calls: list[dict] = []
    monkeypatch.setattr(tl_local, "NEOTOMA_BEARER_TOKEN", "test-token")

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({"url": url, "json": json, "headers": headers})
        return _Resp()

    monkeypatch.setattr(tl_local.httpx, "post", fake_post)
    for ref in ("owner/repo#1", "owner/repo#2"):
        tl_local.complete_task_with_result(
            "ent_t", handler="apis", result=ref, artifact_identity=ref,
        )
    keys = [c["json"]["idempotency_key"] for c in calls]
    assert len(set(keys)) == len(keys), keys
    assert all("owner/repo#" in k for k in keys)


def test_complete_fail_open_without_token(monkeypatch):
    tl_local = _lifecycle_under_test()
    monkeypatch.setattr(tl_local, "NEOTOMA_BEARER_TOKEN", "")
    assert (
        tl_local.complete_task_with_result("ent_t", handler="apis", result="x")
        is False
    )
