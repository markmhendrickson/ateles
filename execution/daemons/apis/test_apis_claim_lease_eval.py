"""Eval id: apis_claim_lease_fail_closed_and_release

Named QA artifact for ateles#733 claim+lease. Ateles has no neotoma
`agentic_evals` lane for Apis Python daemons — the reproducible surface is
pytest under ateles-tests.yml. Thin wrappers below call through to the three
ship behaviours (no fourth fake):

  1. fail-closed: CLAIM_ENABLED + store unavailable → no EXECUTING / spawn
  2. RELEASE→PENDING + attempt accounting → BLOCKED after MAX_ATTEMPTS
  3. raw_fragments holder still wins; second claimant → held_by_other

CI: `pytest execution/daemons/apis/` + `pytest lib/daemon_runtime/`.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

EVAL_ID = "apis_claim_lease_fail_closed_and_release"

SHIP_NODE_IDS = (
    "execution/daemons/apis/test_claim_dispatch_gate.py::"
    "test_dispatch_fails_closed_when_claim_enabled_and_store_unavailable",
    "execution/daemons/apis/test_task_watchdog.py::"
    "test_sweep_releases_lapsed_claim_back_to_pending",
    "execution/daemons/apis/test_task_watchdog.py::"
    "test_sweep_escalates_after_max_lapsed_lease_releases",
    "lib/daemon_runtime/test_task_claim.py::"
    "test_claim_works_when_holder_arrives_in_raw_fragments",
    "lib/daemon_runtime/test_task_claim.py::"
    "test_two_concurrent_claimants_cannot_both_hold_one_task",
)


def test_eval_id_is_stable():
    """Anchor so the QA report can grep a stable string in this file."""
    assert EVAL_ID == "apis_claim_lease_fail_closed_and_release"
    assert len(SHIP_NODE_IDS) == 5


def test_eval_fail_closed_when_claim_store_unavailable(monkeypatch, tmp_path):
    """Ship behaviour 1 — call through the owning dispatch-gate test."""
    import test_claim_dispatch_gate as gate
    from unroutable_ledger import UnroutableLedger

    # Inline the owning module's `_isolated` fixture (cannot call fixtures).
    monkeypatch.setattr(
        gate.apis, "_unroutable", UnroutableLedger(path=tmp_path / "l.json"),
    )
    monkeypatch.setattr(gate.apis, "_created_seen", {})
    monkeypatch.setattr(gate.apis, "DRY_RUN", False)
    monkeypatch.setattr(gate.apis, "READINESS_GATE", False)
    monkeypatch.setattr(gate.apis, "RUN_CONVERSATIONS", False)
    monkeypatch.setattr(gate.apis, "RUN_EMAIL", False)
    gate.test_dispatch_fails_closed_when_claim_enabled_and_store_unavailable(
        monkeypatch,
    )


def test_eval_sweep_releases_then_escalates(monkeypatch):
    """Ship behaviour 2 — RELEASE→PENDING, then BLOCKED after MAX_ATTEMPTS."""
    import test_task_watchdog as tw_tests

    tw_tests.test_sweep_releases_lapsed_claim_back_to_pending(monkeypatch)
    tw_tests.test_sweep_escalates_after_max_lapsed_lease_releases(monkeypatch)


def test_eval_raw_fragments_holder_and_mutual_exclusion():
    """Ship behaviour 3 — raw_fragments holder + second claimant loses."""
    from lib.daemon_runtime import test_task_claim as claim_tests
    from lib.daemon_runtime.task_claim import ClaimStore

    claim_tests.test_claim_works_when_holder_arrives_in_raw_fragments()

    fake = claim_tests.FakeNeotoma()
    clock = claim_tests.Clock()
    store = ClaimStore(fake.store, fake.read, lease_seconds=900, now_fn=clock)
    claim_tests.test_two_concurrent_claimants_cannot_both_hold_one_task(
        (fake, clock, store),
    )
