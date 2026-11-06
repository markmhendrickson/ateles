"""Fix rounds for a Fly-delivered PR run as Fly corrections, never on this host.

A review fix round, a CI-fix round and a resumed fix round (the revision
sweep) for a PR the Fly delivery ledger names are filed as one
``existing_pr_correction`` task on the PR's own branch, and no builder runs
here. The round limit still holds, a PR a readable ledger does not name keeps
the local round, a PR whose origin cannot be established (missing or
unreadable ledger) stops for attention, and a correction that cannot be filed
or does not read back as filed stops the PR for attention with no local commit
and no fix round used up. Offline: no Fly, GitHub or Neotoma calls.
"""

from __future__ import annotations

import asyncio
import json

import pytest

import build_delivery as delivery
from swarm_dispatch import SwarmDispatcher
from test_swarm_dispatch import _trigger as _swarm_trigger
from test_tiering_call_sites import (  # noqa: F401 (world is a fixture)
    _Recorder,
    _dispatcher,
    world,
)

REPO = "owner/repo"
PR = 87
BRANCH = "cicada/fly-delivered-1234"
HEAD = "a" * 40  # what the `world` fixture's `_pr_head_sha` returns
UX_FINDING = "[BLOCKING] naming: the flag is undiscoverable\ndetail"
GUIDANCE = "Rename the flag to --dry-run in cli.py and update its help text."


@pytest.fixture(autouse=True)
def isolated_ledger(monkeypatch, tmp_path):
    monkeypatch.setenv(delivery.FLY_DELIVERY_REGISTRY_ENV, str(tmp_path / "ledger.json"))
    monkeypatch.setenv("APIS_FLY_RUNNER_CLASSES", "open_or_merge_pr")


@pytest.fixture
def stored(monkeypatch):
    """Capture every Neotoma store the dispatcher makes; answer with an id, and
    read the stored task and its edges back as written. A test changes
    ``readback`` (fields overridden on read) or sets ``read_fails``."""
    box = {"calls": [], "answer": {"entities": [{"entity_id": "ent_synthetic_fix"}]},
           "readback": {}, "read_fails": False, "reads": []}

    async def store(self, entities, idempotency_key, relationships=None):
        box["calls"].append((entities, idempotency_key, relationships))
        return box["answer"]

    async def get(self, path):
        box["reads"].append(path)
        if box["read_fails"] or not box["calls"]:
            return None
        entities, _, rels = box["calls"][-1]
        if path.endswith("/relationships"):
            return {"relationships": [
                {"relationship_type": r["relationship_type"],
                 "source_entity_id": "ent_synthetic_fix",
                 "target_entity_id": r["target_entity_id"]}
                for r in rels or []]}
        return {"entity_id": "ent_synthetic_fix",
                "snapshot": {**entities[0], **box["readback"]}}

    monkeypatch.setattr(SwarmDispatcher, "_store_entities", store)
    monkeypatch.setattr(SwarmDispatcher, "_neotoma_get", get)
    return box


@pytest.fixture
def rounds_recorded(monkeypatch, world):
    """Count fix-round markers the way `_fix_round_count` reads them."""
    recorded = []

    async def record(self, t, n):
        recorded.append(n)
        world.fix_rounds += 1

    monkeypatch.setattr(SwarmDispatcher, "_record_fix_round", record)
    return recorded


async def _failing_checks(self, t):
    return [{"name": "unit-tests", "html_url": "https://example.invalid/run/1"}]


def ci_round(monkeypatch, d=None):
    monkeypatch.setattr(SwarmDispatcher, "_failing_check_runs", _failing_checks)
    d = d or _dispatcher()
    asyncio.run(d._route_ci_failure(trigger(), parent=80))
    return d


def seed_fly_delivery(number=PR, branch=BRANCH, key=f"{REPO}#task:ent_synthetic_src"):
    """A settled first delivery, as the Fly runner leaves it."""
    assert delivery._delivery_ledger().reserve_delivery(key, {
        "status": "opened", "branch": branch, "run_id": "run-1", "token": "seeded",
        "commit": HEAD, "pr_number": number, "readback": "passed"})


def trigger(**over):
    base = dict(kind="pr_synchronize", repository=REPO, number=PR,
                html_url=f"https://github.com/{REPO}/pull/{PR}", action="synchronize",
                head_ref=BRANCH, base_ref="main")
    base.update(over)
    return _swarm_trigger(**base)


def review_round(d=None, t=None):
    d = d or _dispatcher()
    asyncio.run(d._route_blocking_findings(
        t or trigger(), parent=80, reviews=[("ux", UX_FINDING)],
        verdict="request_changes"))
    return d


def corrections(box):
    return [e for entities, _, _ in box["calls"] for e in entities
            if e.get("task_type") == "existing_pr_correction"]


def test_review_round_for_a_fly_pr_is_filed_as_a_fly_correction(monkeypatch, world, stored):
    seed_fly_delivery()
    rec = _Recorder(monkeypatch, stdout={"accipiter": GUIDANCE})
    review_round()
    # No builder ran on this host: the lens wrote guidance, nothing committed.
    assert rec.of("cicada") == []
    assert len(rec.of("accipiter")) == 1
    [task] = corrections(stored)
    assert task["pr_number"] == PR and task["head_sha"] == HEAD
    assert task["assigned_to"] == "cicada"
    assert task["action_type"] == "open_or_merge_pr"
    assert json.loads(task["context"])["branch"] == BRANCH
    assert json.loads(task["context"])["base_branch"] == "main"
    assert GUIDANCE in task["description"]
    # The correction route itself admits it, on the PR's existing branch.
    target = delivery.existing_pr_target(task, REPO)
    assert (target.number, target.branch, target.head_sha) == (PR, BRANCH, HEAD)
    # The task hangs off the task the PR was delivered from.
    [(_, _, rels)] = stored["calls"]
    assert rels == [{"relationship_type": "PART_OF", "source_index": 0,
                     "target_entity_id": "ent_synthetic_src"}]


def test_ci_fix_round_for_a_fly_pr_is_filed_as_a_fly_correction(monkeypatch, world, stored):
    seed_fly_delivery()

    async def failing(self, t):
        return [{"name": "unit-tests", "html_url": "https://example.invalid/run/1"}]

    monkeypatch.setattr(SwarmDispatcher, "_failing_check_runs", failing)
    rec = _Recorder(monkeypatch)
    asyncio.run(_dispatcher()._route_ci_failure(trigger(), parent=80))
    assert rec.of("cicada") == []
    [task] = corrections(stored)
    assert "unit-tests" in task["description"]
    assert json.loads(task["context"])["fix_kind"] == "CI-fix"
    assert delivery.existing_pr_target(task, REPO).branch == BRANCH


def test_round_limit_still_holds_for_a_fly_pr(monkeypatch, world, stored):
    seed_fly_delivery()
    world.fix_rounds = 2  # the configured default cap
    rec = _Recorder(monkeypatch)
    d = review_round()
    asyncio.run(d._route_ci_failure(trigger(), parent=80))
    assert rec.calls == []
    assert corrections(stored) == []
    assert any("auto-fix rounds did not clear review" in m for m in d.notifier.sent)
    assert any("required CI is failing after" in m for m in d.notifier.sent)


def test_a_pr_fly_did_not_deliver_keeps_the_local_round(monkeypatch, world, stored):
    seed_fly_delivery(number=PR + 1)  # another PR's receipt names nothing here
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed"})
    review_round()
    assert rec.only("cicada").get("agent_git_identity") is True
    assert corrections(stored) == []


@pytest.mark.parametrize("route", ["review", "ci"])
def test_failed_correction_filing_stops_for_attention_with_no_local_commit(
    monkeypatch, world, stored, rounds_recorded, route
):
    seed_fly_delivery()
    stored["answer"] = None  # the store did not land
    rec = _Recorder(monkeypatch)
    d = review_round() if route == "review" else ci_round(monkeypatch)
    assert rec.of("cicada") == []
    assert any("was not started" in m and "Needs your attention" in m
               for m in d.notifier.sent)
    # The failed filing used up no fix round.
    assert rounds_recorded == [] and world.fix_rounds == 0


@pytest.mark.parametrize("route", ["review", "ci"])
def test_a_confirmed_filing_records_its_round(
    monkeypatch, world, stored, rounds_recorded, route
):
    seed_fly_delivery()
    _Recorder(monkeypatch, stdout={"accipiter": GUIDANCE})
    review_round() if route == "review" else ci_round(monkeypatch)
    assert len(corrections(stored)) == 1
    assert rounds_recorded == [1]
    # Read back by id, fields and PART_OF edge both.
    assert stored["reads"] == ["entities/ent_synthetic_fix",
                               "entities/ent_synthetic_fix/relationships"]


@pytest.mark.parametrize("mismatch", [
    {"repository": "owner/other"},
    {"pr_number": PR + 1},
    {"head_sha": "b" * 40},
    {"context": json.dumps({"branch": "someone/else"})},
    {"action_type": "code_edit"},
])
def test_a_filing_that_reads_back_wrong_is_no_filing(
    monkeypatch, world, stored, rounds_recorded, mismatch
):
    seed_fly_delivery()
    stored["readback"] = mismatch
    rec = _Recorder(monkeypatch)
    d = ci_round(monkeypatch)
    assert rec.of("cicada") == []
    assert rounds_recorded == []
    assert any("did not read back as filed" in m and "Needs your attention" in m
               for m in d.notifier.sent)


def test_a_filing_missing_its_part_of_edge_is_no_filing(
    monkeypatch, world, stored, rounds_recorded
):
    seed_fly_delivery()
    real_get = SwarmDispatcher._neotoma_get

    async def no_edges(self, path):
        if path.endswith("/relationships"):
            return {"relationships": []}
        return await real_get(self, path)

    monkeypatch.setattr(SwarmDispatcher, "_neotoma_get", no_edges)
    _Recorder(monkeypatch)
    d = ci_round(monkeypatch)
    assert rounds_recorded == []
    assert any("missing its PART_OF edge" in m for m in d.notifier.sent)


def test_a_filing_that_cannot_be_read_back_is_no_filing(
    monkeypatch, world, stored, rounds_recorded
):
    seed_fly_delivery()
    stored["read_fails"] = True
    rec = _Recorder(monkeypatch)
    d = review_round()
    assert rec.of("cicada") == []
    assert rounds_recorded == []
    assert any("could not be read back" in m and "Needs your attention" in m
               for m in d.notifier.sent)


def test_unreadable_ledger_while_on_fly_stops_for_attention(monkeypatch, world, stored, tmp_path):
    bad = tmp_path / "ledger.json"
    bad.write_text("{not json", encoding="utf-8")
    rec = _Recorder(monkeypatch)
    d = review_round()
    assert rec.calls == []  # not even the guidance lenses: the round never started
    assert corrections(stored) == []
    assert any("could not be read" in m and "Needs your attention" in m
               for m in d.notifier.sent)


def _resume_scan(monkeypatch, d):
    async def scan(repo, now, stale):
        return [{
            "number": PR, "title": "t", "body": "Closes #80.",
            "html_url": f"https://github.com/{REPO}/pull/{PR}",
            "user": {"login": "someone"},
            "head": {"ref": BRANCH, "sha": HEAD}, "base": {"ref": "main"},
            "labels": [], "_blocking_review_at": "x", "_revision_pushed_at": "y",
            "_blocking_review_body": UX_FINDING,
        }]

    monkeypatch.setattr(d, "_prs_with_unactioned_revisions", scan)


@pytest.mark.parametrize("ledger", ["missing", "unreadable"])
@pytest.mark.parametrize("opted_in", [True, False])
@pytest.mark.parametrize("route", ["review", "ci", "resumed"])
def test_unknown_origin_holds_every_fix_round(
    monkeypatch, world, stored, rounds_recorded, tmp_path, ledger, opted_in, route
):
    if not opted_in:
        monkeypatch.delenv("APIS_FLY_RUNNER_CLASSES", raising=False)
    path = tmp_path / "ledger.json"  # the isolated_ledger path
    if ledger == "unreadable":
        path.write_text("{not json", encoding="utf-8")
    assert path.exists() is (ledger == "unreadable")
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed"})
    d = _dispatcher()
    if route == "review":
        review_round(d)
    elif route == "ci":
        ci_round(monkeypatch, d)
    else:
        _resume_scan(monkeypatch, d)
        summary = asyncio.run(d.resume_unactioned_revisions([REPO]))
        assert summary["resumed"] == 0
    assert rec.calls == []  # no builder, not even the guidance lenses
    assert corrections(stored) == [] and rounds_recorded == []
    assert any("missing or could not be read" in m and "Needs your attention" in m
               for m in d.notifier.sent)


@pytest.mark.parametrize("opted_in", [True, False])
def test_a_readable_ledger_with_no_receipt_keeps_the_local_round(
    monkeypatch, world, stored, rounds_recorded, opted_in
):
    if not opted_in:
        monkeypatch.delenv("APIS_FLY_RUNNER_CLASSES", raising=False)
    ledger = delivery._delivery_ledger()
    with ledger._locked():
        ledger._save(ledger._load())  # readable, no receipts at all
    assert delivery._delivery_ledger().delivery_receipts() == {}
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed"})
    ci_round(monkeypatch)
    assert rec.only("cicada").get("agent_git_identity") is True
    assert corrections(stored) == [] and rounds_recorded == [1]


def test_resumed_fix_round_for_a_fly_pr_is_filed_as_a_fly_correction(monkeypatch, world, stored):
    seed_fly_delivery()
    d = _dispatcher()
    rec = _Recorder(monkeypatch)

    async def scan(repo, now, stale):
        return [{
            "number": PR, "title": "t", "body": "Closes #80.",
            "html_url": f"https://github.com/{REPO}/pull/{PR}",
            "user": {"login": "someone"},
            "head": {"ref": BRANCH, "sha": HEAD}, "base": {"ref": "main"},
            "labels": [], "_blocking_review_at": "x", "_revision_pushed_at": "y",
            "_blocking_review_body": UX_FINDING,
        }]

    monkeypatch.setattr(d, "_prs_with_unactioned_revisions", scan)
    summary = asyncio.run(d.resume_unactioned_revisions([REPO]))
    assert rec.of("cicada") == []
    [task] = corrections(stored)
    assert UX_FINDING.splitlines()[0] in task["description"]
    assert summary["resumed"] == 1
