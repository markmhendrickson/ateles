"""
test_noowner_escalation.py — the no-owner escalation path, through dispatch_task.

Driven through the REAL entrypoint rather than the ledger's helpers. The guard
being tested here is precisely the kind that a unit test of the helper would
pass while the wired-up path still escalated: `snapshot_hydrated` has to be
threaded from the SSE handler all the way into the `skill is None` branch, and
only an end-to-end call proves that.

The measured defect (2026-09-01, apis.log): 123 escalations from 35 distinct
tasks; 218 `task.created` events for those 35; and
ent_c192afd8760fd9f3fbd3c08c — which has a title, a description and five tags —
escalated three times, its real tags logged at 16:22:05 and `tags=[]` at
16:25:25 straight after a 502 on the hydration GET.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import apis  # noqa: E402
from unroutable_ledger import UnroutableLedger  # noqa: E402


class _Notifier:
    """Captures sends so the test can count PAGES, not log lines."""

    def __init__(self):
        self.sent: list[str] = []

    def send(self, message, priority=None, handler=None):
        self.sent.append(message)


@pytest.fixture(autouse=True)
def _isolated_ledger(monkeypatch, tmp_path):
    """Every test gets its own on-disk ledger — never the operator's real one."""
    monkeypatch.setattr(apis, "_unroutable", UnroutableLedger(path=tmp_path / "l.json"))
    monkeypatch.setattr(apis, "_created_seen", {})


@pytest.fixture(autouse=True)
def _no_neotoma_writes(monkeypatch):
    """dispatch_task writes task status; keep that off the network."""
    monkeypatch.setattr(apis, "set_task_status", lambda *a, **k: True)


def _dispatch(entity_id, snapshot, notifier, hydrated=True):
    asyncio.run(
        apis.dispatch_task(
            entity_id, snapshot, trigger="created",
            notifier=notifier, snapshot_hydrated=hydrated,
        )
    )


# ── the hydration guard ──────────────────────────────────────────────────────


def test_unhydrated_snapshot_does_not_escalate():
    """A failed read must never be reported as 'this task has no owner'."""
    n = _Notifier()
    _dispatch("ent_c192afd8760fd9f3fbd3c08c", {}, n, hydrated=False)
    assert n.sent == [], f"escalated on an unread snapshot: {n.sent}"


def test_unhydrated_snapshot_does_not_mark_the_task_blocked(monkeypatch):
    """Blocking on a failed read would strand a routable task for the operator."""
    calls: list = []
    monkeypatch.setattr(apis, "set_task_status", lambda *a, **k: calls.append(a))
    _dispatch("ent_x", {}, _Notifier(), hydrated=False)
    assert calls == []


def test_genuinely_unroutable_task_still_escalates():
    """The guard must not become a blanket silence — read OK, no owner, page."""
    n = _Notifier()
    _dispatch("ent_x", {"title": "Something with no domain words", "tags": []}, n)
    assert len(n.sent) == 1
    assert "unroutable" in n.sent[0]
    assert "ent_x" in n.sent[0]


def test_a_routable_task_is_never_escalated(monkeypatch):
    """Sanity: routing itself works. Regression guard on the tables."""
    monkeypatch.setattr(apis, "_activity", _FakeActivity())
    n = _Notifier()
    # Routes to cicada via the ops/engineering patterns.
    snapshot = {"title": "Fix the flaky CI pipeline", "tags": ["ops"]}
    with pytest.raises(_Stop):
        _dispatch("ent_ok", snapshot, n)
    assert not any("unroutable" in m for m in n.sent)


class _Stop(Exception):
    """Halts dispatch once routing has demonstrably succeeded."""


class _FakeActivity:
    def started(self, *a, **k):
        raise _Stop()


# ── dedup through the real dispatcher ────────────────────────────────────────


def test_same_task_escalates_once_across_redelivered_events():
    n = _Notifier()
    snapshot = {"title": "No owner here", "tags": []}
    for _ in range(6):  # the measured ~6.2x redelivery
        _dispatch("ent_dup", snapshot, n)
    assert len(n.sent) == 1, f"expected one page, got {len(n.sent)}: {n.sent}"


def test_measured_burst_collapses_from_123_pages_to_a_handful():
    """35 distinct tasks x ~6 redeliveries — the actual observed shape.

    The live defect produced 123 pages for this. Aggregation means the whole
    burst arrives as one opening report plus whatever the flush loop emits, and
    every task must still be NAMED — reduced volume, not reduced information.
    """
    n = _Notifier()
    for task in range(35):
        for _ in range(6):
            _dispatch(f"ent_{task}", {"title": f"task {task}", "tags": []}, n)
    # Whatever is still buffered is delivered by the periodic flush.
    tail = apis._unroutable.drain(force=True)
    if tail:
        n.sent.append(tail)

    assert len(n.sent) < 35, f"expected aggregation, got {len(n.sent)} pages"
    combined = "\n".join(n.sent)
    reported = set(re.findall(r"ent_\d+", combined))
    missing = {f"ent_{i}" for i in range(35)} - reported
    assert not missing, f"unroutable tasks went unreported: {sorted(missing)}"


def test_a_new_unroutable_task_is_still_reported():
    """Dedup must not swallow a genuinely NEW task — the #583/#636 failure."""
    n = _Notifier()
    for _ in range(5):
        _dispatch("ent_old", {"title": "old", "tags": []}, n)
    _dispatch("ent_brand_new", {"title": "brand new", "tags": []}, n)
    tail = apis._unroutable.drain(force=True)
    if tail:
        n.sent.append(tail)
    combined = "\n".join(n.sent)
    assert "ent_brand_new" in combined, "a new unroutable task went unreported"


def test_escalation_dedup_survives_a_restart(tmp_path, monkeypatch):
    """A restart must not re-page the operator about the standing backlog."""
    path = tmp_path / "ledger.json"
    snapshot = {"title": "No owner", "tags": []}

    monkeypatch.setattr(apis, "_unroutable", UnroutableLedger(path=path))
    n1 = _Notifier()
    _dispatch("ent_a", snapshot, n1)
    assert len(n1.sent) == 1

    # Restart: fresh ledger object, fresh created-seen set, same file.
    monkeypatch.setattr(apis, "_unroutable", UnroutableLedger(path=path))
    monkeypatch.setattr(apis, "_created_seen", {})
    n2 = _Notifier()
    _dispatch("ent_a", snapshot, n2)
    assert n2.sent == [], "re-paged the whole backlog after a restart"
