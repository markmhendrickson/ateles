"""Tests for the task lifecycle state machine + its status-write I/O contract."""

from __future__ import annotations

from lib.daemon_runtime import task_lifecycle as tl
from lib.daemon_runtime.task_lifecycle import (
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
    assert not can_transition("verified", "failed")  # effect is not retryable


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

_HEADER = "[cicada] pull_request_link: https://github.com/markmhendrickson/ateles/pull/999"


class _FakeTaskStore:
    """A stand-in Neotoma task entity: `/correct` writes here, snapshots read here.

    `drop` names fields whose writes return 2xx but are silently NOT applied
    (the undeclared-field / idempotency-replay failure mode), and `mangle`
    maps a field to the value actually stored instead of the one written.
    """

    def __init__(self, *, drop=(), mangle=None):
        self.fields: dict = {"status": "executing", "result": ""}
        self.drop = set(drop)
        self.mangle = dict(mangle or {})
        self.writes: list[tuple[str, object]] = []

    def post(self, url, headers=None, json=None, timeout=None):
        field, value = json["field"], json["value"]
        self.writes.append((field, value))
        if field not in self.drop:
            self.fields[field] = self.mangle.get(field, value)
        return _Resp()

    def snapshot(self, _entity_id):
        return dict(self.fields)


def _use_store(monkeypatch, store):
    monkeypatch.setattr(tl, "NEOTOMA_BEARER_TOKEN", "test-token")
    monkeypatch.setattr(tl.httpx, "post", store.post)


def test_complete_writes_result_before_status_and_reads_both_back(monkeypatch):
    """Order is the point, and the outcome is only truthy once state read back.

    `set_task_status` writes `status` first and its companions after, so a
    process killed between the two leaves a task reading DONE with no artifact
    reference. This path writes the reference first.
    """
    store = _FakeTaskStore()
    _use_store(monkeypatch, store)
    outcome = tl.complete_task_with_result(
        "ent_t", handler="apis", result=_HEADER, fetch_snapshot=store.snapshot,
        from_status="executing",
    )
    assert outcome and outcome.stage == "done"
    assert [f for f, _ in store.writes] == ["result", "status"]
    assert store.fields == {"status": "done", "result": _HEADER}


def test_complete_never_writes_done_when_result_does_not_read_back(monkeypatch):
    """NEGATIVE effect test: goes RED if the result read-back is removed.

    The `/correct` call for `result` returns 2xx but the entity keeps the old
    value. Without the read-back the status write still happens and the task
    reads DONE carrying no (or the wrong) artifact reference.
    """
    store = _FakeTaskStore(drop={"result"})
    _use_store(monkeypatch, store)
    outcome = tl.complete_task_with_result(
        "ent_t", handler="apis", result=_HEADER, fetch_snapshot=store.snapshot,
    )
    assert not outcome and outcome.stage == "result_readback"
    assert "status" not in [f for f, _ in store.writes], "wrote DONE unproven"
    assert store.fields["status"] == "executing"


def test_complete_rejects_a_result_that_reads_back_as_something_else(monkeypatch):
    """Equality, not presence: a stale or truncated earlier value is not the header."""
    store = _FakeTaskStore(mangle={"result": "cicada completed (trigger=created)"})
    _use_store(monkeypatch, store)
    outcome = tl.complete_task_with_result(
        "ent_t", handler="apis", result=_HEADER, fetch_snapshot=store.snapshot,
    )
    assert not outcome and outcome.stage == "result_readback"
    assert "status" not in [f for f, _ in store.writes]


def test_complete_is_not_ok_when_terminal_status_does_not_read_back(monkeypatch):
    """NEGATIVE effect test: goes RED if the status read-back is removed.

    The `status` write returns 2xx but the entity is not DONE. The caller must
    be told, because it is about to claim `job.finished`.
    """
    store = _FakeTaskStore(drop={"status"})
    _use_store(monkeypatch, store)
    outcome = tl.complete_task_with_result(
        "ent_t", handler="apis", result=_HEADER, fetch_snapshot=store.snapshot,
    )
    assert not outcome and outcome.stage == "status_readback"
    assert outcome.detail
    assert store.fields["status"] == "executing"


def test_complete_fails_closed_when_the_snapshot_read_raises_or_is_missing(monkeypatch):
    store = _FakeTaskStore()
    _use_store(monkeypatch, store)

    def boom(_id):
        raise RuntimeError("neotoma down")

    for reader in (boom, lambda _id: None):
        store.fields.update(status="executing", result="")
        outcome = tl.complete_task_with_result(
            "ent_t", handler="apis", result=_HEADER, fetch_snapshot=reader,
        )
        assert not outcome and outcome.stage == "result_readback"
        assert "status" not in [f for f, _ in store.writes]


def test_complete_idempotency_key_carries_artifact_identity(monkeypatch):
    """Two attempts naming different artifacts must not dedupe into one write."""
    calls = _capture(monkeypatch)
    for ref in ("owner/repo#1", "owner/repo#2"):
        tl.complete_task_with_result(
            "ent_t", handler="apis", result=ref, artifact_identity=ref,
            fetch_snapshot=lambda _id, ref=ref: {"status": "done", "result": ref},
        )
    keys = [c["json"]["idempotency_key"] for c in calls]
    assert len(set(keys)) == len(keys), keys
    assert all("owner/repo#" in k for k in keys)


def test_complete_fail_open_without_token(monkeypatch):
    monkeypatch.setattr(tl, "NEOTOMA_BEARER_TOKEN", "")
    outcome = tl.complete_task_with_result(
        "ent_t", handler="apis", result="x", fetch_snapshot=lambda _id: {},
    )
    assert not outcome and outcome.stage == "result_write"
