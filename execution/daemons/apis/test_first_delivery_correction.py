"""A settled first delivery does not make its own PR's first correction impossible.

A first delivery for issue #34 settles ``opened`` with PR #12 under the issue
key. The first correction of PR #12 (carrying issue #34 and PR #12) reserves
the PR key with the issue key as its association, which collides with that
receipt. When the receipt is exactly the delivery that opened this PR at the
correction's admitted head, the correction takes it over once, audited, under
the ledger lock; every other receipt still refuses before any job or model.
All data is synthetic; GitHub and the Fly job are mocked.
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import threading

import pytest

import apis
import build_delivery as delivery
from skill_runner import SkillResult
from test_existing_pr_delivery import snapshot
from test_fly_delivery_dispatch import _StubNotifier, dispatcher, trigger
from test_swarm_dispatch import _empty_spec_state

REPO = "owner/repo"
SOURCE, PR = REPO + "#34", REPO + "#12"
OPENED, NEXT, STALE = "a" * 40, "b" * 40, "c" * 40
BRANCH = "cicada/task-source-34"


@pytest.fixture(autouse=True)
def private_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv("APIS_FLY_LEDGER_FILE", str((tmp_path / "first-delivery-ledger.json").resolve()))
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")


def _ledger():
    return delivery._delivery_ledger()


def _audit():
    import fly_delivery_receipt
    path = fly_delivery_receipt.audit_path(_ledger())
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def _opened_receipt(**over):
    entry = {"status": "opened", "token": "source-token", "branch": BRANCH, "run_id": "source-run",
             "recorded_at": 1.0, "commit": OPENED, "pr_number": 12,
             "pr_url": f"https://github.com/{REPO}/pull/12", "readback": "passed",
             "readback_reason": "", "reason": "", "task_id": "source-task"}
    entry.update(over)
    return {k: v for k, v in entry.items() if v is not None}


def _seed(**over):
    entry = _opened_receipt(**over)
    assert _ledger().reserve_delivery(SOURCE, entry)
    return entry


def _correction_row(head=OPENED, branch=BRANCH, base="main", issue=34, pr=12):
    return snapshot(issue_number=issue, pr_number=pr, head_sha=head,
                    context=json.dumps({"branch": branch, "base_branch": base}))


class _World:
    """Mocked verified GitHub and Fly job for the natural entrances."""

    def __init__(self, monkeypatch, *, preflight_reason=None):
        self.launched, self.preflight, self.local = [], [], []
        self.preflight_reason = preflight_reason
        self.on_preflight = None
        world = self

        async def verify(repository, target, *a, **kw):
            world.preflight.append(target)
            if world.on_preflight:
                await world.on_preflight()
            return world.preflight_reason

        async def verify_delivery(result, plan):
            return None

        async def run(skill, prompt, **kw):
            spec = kw["fly_delivery"]
            world.launched.append(spec)
            if "pr_number" in spec:
                return SkillResult(skill, True, 0, "", "", fly_delivery_status="updated",
                                   remote_run_id=f"correction-run-{len(world.launched)}",
                                   fly_delivery_commit=world.next_commit, fly_pr_number=12,
                                   fly_pr_url=f"https://github.com/{REPO}/pull/12")
            return SkillResult(skill, True, 0, "", "", fly_delivery_status="opened",
                               remote_run_id="source-run", fly_delivery_commit=OPENED,
                               fly_pr_number=12, fly_pr_url=f"https://github.com/{REPO}/pull/12")

        async def worktree(*a, **kw):
            world.local.append(a)
            raise AssertionError("no local fallback")

        self.next_commit = NEXT
        monkeypatch.setattr(delivery, "verify_existing_pr_target", verify)
        monkeypatch.setattr(delivery, "verify_fly_delivery", verify_delivery)
        monkeypatch.setattr(delivery, "prepare_build_worktree", worktree)
        monkeypatch.setattr(apis, "run_skill", run)
        monkeypatch.setattr("swarm_dispatch.run_skill", run)


async def _source(pipeline):
    if pipeline:
        try:
            return await dispatcher()._open_implementation_pr(
                dataclasses.replace(trigger(), number=34), _empty_spec_state())
        except (delivery.FlyDeliveryEnded, delivery.BuildNotStarted, delivery.BuildWorktreeError):
            return None
    return await apis._spawn_harness_skill(
        "cicada", "ent_inert_source", dict(repository=REPO, issue_number=34,
        action_type="open_or_merge_pr", assigned_to="cicada"), "approved", _StubNotifier())


def _correct(row, task="ent_inert_correction"):
    return asyncio.run(apis._spawn_harness_skill("cicada", task, row, "approved", _StubNotifier()))


# ── The complete path through the natural entrances ─────────────────────────

@pytest.mark.parametrize("pipeline", [False, True])
def test_settled_first_delivery_is_corrected_once_through_the_natural_entrances(monkeypatch, pipeline):
    world = _World(monkeypatch)
    asyncio.run(_source(pipeline))
    assert len(world.launched) == 1 and "pr_number" not in world.launched[0]
    branch = world.launched[0]["branch"]
    (source_key,) = _ledger().delivery_receipts()
    assert delivery.canonical_key(source_key) == SOURCE
    assert _ledger().delivery_receipts()[source_key]["status"] == "opened"

    got = _correct(_correction_row(branch=branch))
    assert got.ok, got.error
    assert len(world.launched) == 2 and world.local == []
    spec = world.launched[1]
    assert spec["pr_number"] == "12" and spec["head_sha"] == OPENED and spec["branch"] == branch
    receipts = _ledger().delivery_receipts()
    assert list(receipts) == [PR]
    held = receipts[PR]
    assert held["status"] == "updated" and held["commit"] == NEXT
    assert held["associated_keys"] == [SOURCE]
    assert held["existing_pr"]["pr_number"] == "12" and held["existing_pr"]["head_sha"] == OPENED
    (line,) = _audit()
    assert line["action"] == "supersede_delivery_receipt" and line["key"] == source_key
    assert "correction" not in line and line["status"] == "opened" and line["commit"] == OPENED
    assert line["branch"] == branch and line["first_delivery"]["pr_number"] == 12
    assert line["superseded_by"] == {"key": PR, "head_sha": OPENED}
    assert delivery.active_build(REPO, 12) is None and delivery.active_build(REPO, 34) is None

    # The original source stays refused: its key is now the PR receipt's association.
    asyncio.run(_source(pipeline))
    assert len(world.launched) == 2
    assert "already recorded" in delivery.fly_delivery_blocker(SOURCE)
    # The same first-delivery takeover cannot happen twice.
    assert not delivery.supersedable_first_delivery(
        delivery.FlyDeliveryPlan(REPO, branch, "main", PR, 34, delivery.ExistingPRTarget(12, branch, "main", OPENED),
                                 source_key=SOURCE), PR, held)

    # A later legitimate correction, declared on the pushed commit, still works.
    world.next_commit = "e" * 40
    got = _correct(_correction_row(head=NEXT, branch=branch), task="ent_inert_correction_2")
    assert got.ok, got.error
    assert len(world.launched) == 3 and world.launched[2]["head_sha"] == NEXT
    receipts = _ledger().delivery_receipts()
    assert list(receipts) == [PR] and receipts[PR]["associated_keys"] == [SOURCE]
    assert [x.get("correction") for x in _audit()] == [None, True]

    # The PR's origin and parent read back from the ledger and its audit. A
    # task-routed source names its task; a pipeline source never recorded one
    # (its first-delivery receipt named no task either), so it stays unknown.
    origin = delivery.fly_delivered_pr(REPO, 12, branch)
    if pipeline:
        assert origin.delivered is None and "single source task" in origin.unknown_reason
    else:
        assert origin.delivered is True and origin.branch == branch
        assert origin.source_task == "ent_inert_source"


def test_pipeline_source_parent_was_already_unknown_before_any_correction(monkeypatch):
    world = _World(monkeypatch)
    asyncio.run(_source(True))
    origin = delivery.fly_delivered_pr(REPO, 12, world.launched[0]["branch"])
    assert origin.delivered is None and _audit() == []


# ── Hard negatives: refused before any preflight, job or model ──────────────

_REFUSED = {
    "launching": dict(status="launching", commit=None, pr_number=None, readback=None),
    "unknown": dict(status="unknown"),
    "unreported": dict(status="unreported"),
    "updated_first": dict(status="updated"),
    "no_readback": dict(readback="failed"),
    "missing_readback": dict(readback=None),
    "stale_commit": dict(commit=STALE),
    "short_commit": dict(commit="a" * 12),
    "wrong_pr": dict(pr_number=13),
    "missing_pr": dict(pr_number=None),
    "wrong_branch": dict(branch="cicada/other"),
    "already_associated": dict(associated_keys=[REPO + "#99"]),
    "lens": dict(kind="lens"),
    "no_run": dict(run_id=""),
}


@pytest.mark.parametrize("case", sorted(_REFUSED))
def test_unqualified_source_receipt_refuses_at_the_entrance(monkeypatch, case):
    held = _seed(**_REFUSED[case])
    world = _World(monkeypatch)
    got = _correct(_correction_row())
    assert not got.ok and "already recorded" in (got.error or "")
    assert world.preflight == [] and world.launched == [] and world.local == []
    assert _ledger().delivery_receipts() == {SOURCE: held} and _audit() == []


@pytest.mark.parametrize("row", [
    dict(head=STALE), dict(branch="cicada/other"), dict(base="develop-x"), dict(pr=13)])
def test_wrong_target_refuses_at_the_entrance(monkeypatch, row):
    held = _seed()
    world = _World(monkeypatch)
    got = _correct(_correction_row(**row))
    assert not got.ok and world.preflight == [] and world.launched == []
    assert _ledger().delivery_receipts() == {SOURCE: held} and _audit() == []


def test_run_still_in_flight_refuses(monkeypatch):
    held = _seed()
    monkeypatch.setattr(type(_ledger()), "entry", lambda self, run_id: {"run_id": run_id})
    world = _World(monkeypatch)
    assert not _correct(_correction_row()).ok and world.launched == []
    assert _ledger().delivery_receipts() == {SOURCE: held}


def test_unreadable_run_state_refuses(monkeypatch):
    import fly_runner
    held = _seed()

    def broken(self, run_id):
        raise fly_runner.FlyLedgerError("inert unreadable")
    monkeypatch.setattr(type(_ledger()), "entry", broken)
    world = _World(monkeypatch)
    assert not _correct(_correction_row()).ok and world.launched == []
    assert _ledger().delivery_receipts() == {SOURCE: held}


def test_a_second_receipt_on_either_key_refuses(monkeypatch):
    held = _seed()
    ledger = _ledger()
    data = ledger._load()
    other = {"status": "opened", "token": "t", "branch": "b", "run_id": "r", "associated_keys": [SOURCE]}
    data["deliveries"][REPO + "#77"] = other  # a legacy shape the reserve rule would refuse
    ledger._save(data)
    world = _World(monkeypatch)
    assert not _correct(_correction_row()).ok and world.launched == []
    assert ledger.delivery_receipts() == {SOURCE: held, REPO + "#77": other}


def test_unreadable_ledger_refuses(monkeypatch):
    _seed()
    path = _ledger().path
    path.write_text("{not json")
    world = _World(monkeypatch)
    got = _correct(_correction_row())
    assert not got.ok and world.launched == [] and world.preflight == []


def test_failed_preflight_changes_nothing(monkeypatch):
    held = _seed()
    world = _World(monkeypatch, preflight_reason="inert target divergence")
    got = _correct(_correction_row())
    assert not got.ok and len(world.preflight) == 1 and world.launched == []
    assert _ledger().delivery_receipts() == {SOURCE: held} and _audit() == []


def test_receipt_changed_during_awaited_preflight_launches_nothing(monkeypatch):
    held = _seed()
    world = _World(monkeypatch)

    async def change():
        ledger = _ledger()
        data = ledger._load()
        data["deliveries"][SOURCE] = {**held, "commit": STALE}
        ledger._save(data)
    world.on_preflight = change
    got = _correct(_correction_row())
    assert not got.ok and world.launched == []
    assert _ledger().delivery_receipts()[SOURCE]["commit"] == STALE and _audit() == []


def test_audit_failure_takes_over_nothing(monkeypatch):
    import fly_delivery_receipt
    held = _seed()
    world = _World(monkeypatch)

    def fail(*a, **kw):
        raise OSError("inert audit failure")
    monkeypatch.setattr(fly_delivery_receipt, "_append_audit", fail)
    got = _correct(_correction_row())
    assert not got.ok and world.launched == []
    assert _ledger().delivery_receipts() == {SOURCE: held}


# ── Contenders across the actual ledger lock ───────────────────────────────

def _plan():
    target = delivery.ExistingPRTarget(12, BRANCH, "main", OPENED)
    return delivery.FlyDeliveryPlan(repository=REPO, branch=BRANCH, base_branch="main", key=PR,
                                    issue_number=34, target=target, source_key=SOURCE, task_id="corr")


def test_two_corrections_racing_take_over_once():
    _seed()
    plan = _plan()
    barrier = threading.Barrier(4)
    tokens = []

    def contend():
        barrier.wait()
        tokens.append(delivery._supersede_settled_first_delivery(plan))
    threads = [threading.Thread(target=contend) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(1 for t in tokens if t) == 1 and all(t in ("", None) for t in tokens if not t)
    receipts = _ledger().delivery_receipts()
    assert list(receipts) == [PR] and receipts[PR]["status"] == "launching"
    assert len(_audit()) == 1


@pytest.mark.parametrize("source_first", [True, False])
def test_source_rerun_and_correction_in_both_orders(source_first):
    ledger = _ledger()
    _seed()
    plan = _plan()
    rerun = {"status": "launching", "token": "rerun"}
    if source_first:
        assert not ledger.reserve_delivery(SOURCE, rerun)  # the settled source refuses its rerun
        assert delivery._supersede_settled_first_delivery(plan)
    else:
        assert delivery._supersede_settled_first_delivery(plan)
        assert not ledger.reserve_delivery(SOURCE, rerun)  # still refused: now an association
    assert list(ledger.delivery_receipts()) == [PR]


def test_restart_after_takeover_keeps_the_reservation_owned():
    _seed()
    plan = _plan()
    token = delivery._supersede_settled_first_delivery(plan)
    # A fresh ledger object (a restarted process) sees the same owned reservation.
    fresh = delivery._delivery_ledger()
    held = fresh.delivery_receipts()[PR]
    assert held["token"] == token and held["associated_keys"] == [SOURCE]
    assert not fresh.release_delivery(PR, "other-token")
    assert not fresh.settle_delivery(PR, "other-token", {"status": "updated", "token": "other-token"})
    assert delivery._supersede_settled_first_delivery(plan) is None  # never a second takeover
    assert "already recorded" in delivery.fly_delivery_blocker(SOURCE)


def test_first_delivery_through_run_fly_delivery_settles_and_reads_back():
    _seed()
    launched, refused = [], []

    async def launch():
        launched.append(True)
        return SkillResult("cicada", True, 0, "", "", fly_delivery_status="no_changes",
                           remote_run_id="correction-run")

    asyncio.run(delivery.run_fly_delivery(_plan(), launch, lambda r: refused.append(r)))
    assert launched == [True] and refused == []
    held = _ledger().delivery_receipts()[PR]
    assert held["status"] == "no_changes" and held["associated_keys"] == [SOURCE]
    assert held["task_id"] == "corr"


# ── Nothing reached Fly: the superseded receipt goes back ──────────────────

def _not_launched(launched):
    async def launch():
        launched.append(True)
        return SkillResult("cicada", False, None, "", "", error="inert prelaunch refusal")
    return launch


def test_prelaunch_outcome_restores_the_first_delivery_and_a_retry_still_works():
    held = _seed()
    launched, refused = [], []
    got = asyncio.run(delivery.run_fly_delivery(_plan(), _not_launched(launched), refused.append))
    assert launched == [True] and refused == []
    assert got.fly_delivery_not_launched and got.fly_delivery_cleanup == "released"
    # The source issue is still refused by its own original receipt, unchanged.
    assert _ledger().delivery_receipts() == {SOURCE: held}
    assert "already recorded" in delivery.fly_delivery_blocker(SOURCE)
    assert [x["stage"] for x in _audit()] == ["superseding", "restored"]
    assert delivery.fly_delivered_pr(REPO, 12, BRANCH).source_task == "source-task"
    # A same-task re-entry (a fresh dispatch of the same correction) qualifies again.
    assert _test_launch_once() == [True]
    assert list(_ledger().delivery_receipts()) == [PR]


def _test_launch_once():
    launched = []

    async def launch():
        launched.append(True)
        return SkillResult("cicada", True, 0, "", "", fly_delivery_status="no_changes",
                           remote_run_id="correction-run")
    asyncio.run(delivery.run_fly_delivery(_plan(), launch, lambda r: pytest.fail(r)))
    return launched


def test_prelaunch_outcome_restores_a_superseded_correction():
    entry = {"status": "updated", "token": "earlier", "branch": BRANCH, "run_id": "earlier-run",
             "recorded_at": 1.0, "commit": OPENED, "associated_keys": [SOURCE],
             "existing_pr": {"branch": BRANCH, "base_branch": "main", "pr_number": "12", "head_sha": STALE}}
    assert _ledger().reserve_delivery(PR, entry)
    launched = []
    got = asyncio.run(delivery.run_fly_delivery(_plan(), _not_launched(launched), pytest.fail))
    assert launched == [True] and got.fly_delivery_cleanup == "released"
    assert _ledger().delivery_receipts() == {PR: entry}
    assert "already recorded" in delivery.fly_delivery_blocker(SOURCE)


def test_restore_refuses_a_reservation_this_dispatch_does_not_own():
    held = _seed()
    plan, taken = _plan(), []
    assert delivery._supersede_settled_first_delivery(plan, taken)
    assert not delivery._restore_superseded(plan, "other-token", *taken[0])
    assert list(_ledger().delivery_receipts()) == [PR]
    assert held == taken[0][1]
