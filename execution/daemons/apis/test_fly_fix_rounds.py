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


# --- A cleared or superseded receipt still means the PR came from Fly -------
#
# `fly_delivery_receipt.py clear` removes the receipt but first appends its
# identity to `<ledger>.audit.jsonl`; the automatic supersede does the same.
# Those lines carry no PR number, so they are matched on the repository in the
# key and on the PR's head branch (Fly branches are unique to a task).

SRC_KEY = f"{REPO}#task:ent_synthetic_src"


def readable_empty_ledger():
    ledger = delivery._delivery_ledger()
    with ledger._locked():
        ledger._save(ledger._load())
    assert ledger.delivery_receipts() == {}
    return ledger


def audit_file(tmp_path):
    return tmp_path / "ledger.json.audit.jsonl"  # next to the isolated_ledger path


def write_audit(tmp_path, *lines, raw=""):
    readable_empty_ledger()
    text = "".join(json.dumps(line) + "\n" for line in lines) + raw
    audit_file(tmp_path).write_text(text, encoding="utf-8")


def audit_line(action="clear_delivery_receipt", key=SRC_KEY, branch=BRANCH, **extra):
    return {"by": "operator", "action": action, "stage": "removed", "key": key,
            "status": "opened", "run_id": "run-1", "branch": branch, "commit": HEAD,
            **extra}


def run_route(monkeypatch, route, d=None):
    d = d or _dispatcher()
    summary = None
    if route == "review":
        review_round(d)
    elif route == "ci":
        ci_round(monkeypatch, d)
    else:
        _resume_scan(monkeypatch, d)
        summary = asyncio.run(d.resume_unactioned_revisions([REPO]))
    return d, summary


@pytest.mark.parametrize("route", ["review", "ci", "resumed"])
def test_a_cleared_receipt_still_routes_to_fly(monkeypatch, world, stored, tmp_path, route):
    import io

    import fly_delivery_receipt

    seed_fly_delivery()
    out, err = io.StringIO(), io.StringIO()
    assert fly_delivery_receipt.main(
        ["clear", SRC_KEY, "--confirm", SRC_KEY],
        ledger=delivery._delivery_ledger(), out=out, err=err) == 0, err.getvalue()
    assert delivery._delivery_ledger().delivery_receipts() == {}
    assert audit_file(tmp_path).exists()
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed", "accipiter": GUIDANCE})
    run_route(monkeypatch, route)
    assert rec.of("cicada") == []
    [task] = corrections(stored)
    assert task["pr_number"] == PR
    assert_filed_part_of_source(stored)


def assert_filed_part_of_source(stored):
    """The correction was filed, and read back, PART_OF the source task."""
    rels = stored["calls"][-1][2]
    assert rels == [{"relationship_type": "PART_OF", "source_index": 0,
                     "target_entity_id": "ent_synthetic_src"}]
    assert "entities/ent_synthetic_fix/relationships" in stored["reads"]


@pytest.mark.parametrize("route", ["review", "ci", "resumed"])
def test_a_superseded_receipt_still_routes_to_fly(monkeypatch, world, stored, tmp_path, route):
    write_audit(tmp_path, audit_line(
        action="supersede_delivery_receipt", stage="superseding", status="updated",
        source_task="ent_synthetic_src",
        existing_pr={"pr_number": PR, "branch": BRANCH, "base_branch": "main"}))
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed", "accipiter": GUIDANCE})
    run_route(monkeypatch, route)
    assert rec.of("cicada") == []
    assert len(corrections(stored)) == 1
    assert_filed_part_of_source(stored)


@pytest.mark.parametrize("line", [
    audit_line(),  # written before audit lines carried a source task
    audit_line(source_task=""),
    audit_line(action="supersede_delivery_receipt", stage="superseding", status="updated",
               existing_pr={"pr_number": PR, "branch": BRANCH, "base_branch": "main"}),
], ids=["clear-no-field", "clear-empty", "supersede-no-field"])
@pytest.mark.parametrize("route", ["review", "ci", "resumed"])
def test_an_audit_line_with_no_source_task_holds(
    monkeypatch, world, stored, rounds_recorded, tmp_path, line, route
):
    write_audit(tmp_path, line)
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed", "accipiter": GUIDANCE})
    d, summary = run_route(monkeypatch, route)
    if summary is not None:
        assert summary["resumed"] == 0
    assert rec.calls == []  # no builder, no guidance lenses, no local commit
    assert corrections(stored) == [] and stored["calls"] == []
    assert rounds_recorded == [] and world.fix_rounds == 0
    assert any("names no single source task" in m and "Needs your attention" in m
               for m in d.notifier.sent)


def test_two_audit_lines_naming_different_source_tasks_hold(
    monkeypatch, world, stored, rounds_recorded, tmp_path
):
    write_audit(tmp_path, audit_line(source_task="ent_synthetic_src"),
                audit_line(source_task="ent_synthetic_other"))
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed"})
    d = ci_round(monkeypatch)
    assert rec.calls == [] and stored["calls"] == [] and rounds_recorded == []
    assert any("names no single source task" in m for m in d.notifier.sent)


@pytest.mark.parametrize("line", [
    audit_line(key=f"other/repo#task:ent_synthetic_src"),
    audit_line(branch="cicada/another-task-5678"),
    audit_line(key=f"lens:{REPO}#{PR}@{HEAD}:ux", kind="lens", lens="ux"),
    audit_line(kind="lens"),
], ids=["other-repository", "other-branch", "lens-key", "lens-kind"])
def test_an_audit_line_for_something_else_keeps_the_local_round(
    monkeypatch, world, stored, rounds_recorded, tmp_path, line
):
    write_audit(tmp_path, line)
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed"})
    ci_round(monkeypatch)
    assert rec.only("cicada").get("agent_git_identity") is True
    assert corrections(stored) == [] and rounds_recorded == [1]


@pytest.mark.parametrize("corrupt", ["unparseable-line", "not-an-object", "undecodable"])
@pytest.mark.parametrize("route", ["review", "ci", "resumed"])
def test_a_corrupt_audit_file_holds_the_round(
    monkeypatch, world, stored, rounds_recorded, tmp_path, corrupt, route
):
    if corrupt == "unparseable-line":
        write_audit(tmp_path, audit_line(branch="cicada/another-task-5678"), raw="{not json\n")
    elif corrupt == "not-an-object":
        write_audit(tmp_path, raw="[1, 2]\n")
    else:
        readable_empty_ledger()
        audit_file(tmp_path).write_bytes(b"\xff\xfe\x00 not utf-8\n")
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed"})
    d, summary = run_route(monkeypatch, route)
    if summary is not None:
        assert summary["resumed"] == 0
    assert rec.calls == []
    assert corrections(stored) == [] and rounds_recorded == []
    assert any("could not be read" in m and "Needs your attention" in m
               for m in d.notifier.sent)


# Every kind of bad line this reader can meet. The whole file is validated
# before any match counts, so each one holds the round wherever it sits.
BAD_AUDIT_LINES = {
    "unparseable-line": "{not json\n",
    "not-an-object": "[1, 2]\n",
    "removal-key-not-a-string": json.dumps(audit_line(key=7)) + "\n",
    "removal-branch-not-a-string": json.dumps(audit_line(branch=["x"])) + "\n",
    "removal-source-task-not-a-string": json.dumps(audit_line(source_task=["x"])) + "\n",
    "removal-existing-pr-not-an-object": json.dumps(audit_line(existing_pr="87")) + "\n",
    "removal-existing-pr-branch-not-a-string": json.dumps(
        audit_line(existing_pr={"pr_number": PR, "branch": 5})) + "\n",
}


def assert_held(d, summary, stored, rounds_recorded, rec):
    if summary is not None:
        assert summary["resumed"] == 0
    assert rec.calls == []  # no builder, no guidance lenses, no local commit
    assert corrections(stored) == [] and rounds_recorded == []
    assert any("could not be read" in m and "Needs your attention" in m
               for m in d.notifier.sent)


@pytest.mark.parametrize("bad", sorted(BAD_AUDIT_LINES))
@pytest.mark.parametrize("route", ["review", "ci", "resumed"])
def test_a_matching_audit_line_followed_by_a_bad_line_holds(
    monkeypatch, world, stored, rounds_recorded, tmp_path, bad, route
):
    # The first line alone would route to Fly; the bad line after it must
    # still be read, and makes the whole file unknown.
    write_audit(tmp_path, audit_line(), raw=BAD_AUDIT_LINES[bad])
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed", "accipiter": GUIDANCE})
    d, summary = run_route(monkeypatch, route)
    assert_held(d, summary, stored, rounds_recorded, rec)


@pytest.mark.parametrize("bad", sorted(BAD_AUDIT_LINES) + ["undecodable"])
@pytest.mark.parametrize("route", ["review", "ci", "resumed"])
def test_an_active_receipt_with_a_corrupt_audit_file_holds(
    monkeypatch, world, stored, rounds_recorded, tmp_path, bad, route
):
    seed_fly_delivery()  # an active receipt names this PR
    if bad == "undecodable":
        audit_file(tmp_path).write_bytes(b"\xff\xfe\x00 not utf-8\n")
    else:
        audit_file(tmp_path).write_text(BAD_AUDIT_LINES[bad], encoding="utf-8")
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed", "accipiter": GUIDANCE})
    d, summary = run_route(monkeypatch, route)
    assert_held(d, summary, stored, rounds_recorded, rec)


@pytest.mark.parametrize("route", ["review", "ci", "resumed"])
def test_an_active_receipt_with_a_clean_audit_file_still_routes_to_fly(
    monkeypatch, world, stored, tmp_path, route
):
    seed_fly_delivery()
    audit_file(tmp_path).write_text(
        json.dumps(audit_line(branch="cicada/another-task-5678")) + "\n", encoding="utf-8")
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed", "accipiter": GUIDANCE})
    run_route(monkeypatch, route)
    assert rec.of("cicada") == []
    assert len(corrections(stored)) == 1


def test_no_audit_file_and_no_receipt_keeps_the_local_round(
    monkeypatch, world, stored, rounds_recorded, tmp_path
):
    readable_empty_ledger()
    assert not audit_file(tmp_path).exists()
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed"})
    ci_round(monkeypatch)
    assert rec.only("cicada").get("agent_git_identity") is True
    assert corrections(stored) == [] and rounds_recorded == [1]


@pytest.mark.parametrize("route", ["review", "ci", "resumed"])
def test_a_fly_pr_with_fly_delivery_off_holds_for_the_operator(
    monkeypatch, world, stored, rounds_recorded, route
):
    seed_fly_delivery()  # a readable receipt names this PR
    monkeypatch.delenv("APIS_FLY_RUNNER_CLASSES", raising=False)
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed"})
    d, summary = run_route(monkeypatch, route)
    if summary is not None:
        assert summary["resumed"] == 0
    assert rec.of("cicada") == []
    assert corrections(stored) == [] and rounds_recorded == [] and world.fix_rounds == 0
    assert any("no longer opted onto Fly" in m and "Needs your attention" in m
               for m in d.notifier.sent)


# --- Only a correction receipt names the PR ---------------------------------
#
# The first delivery's receipt was cleared while a correction receipt (keyed
# by the PR, carrying `existing_pr`) is still active. The correction receipt
# records no task, so the parent comes from the removal's audit line; with
# none, or several, the round holds and nothing is stored.

def seed_correction_receipt(tmp_path, *lines):
    write_audit(tmp_path, *lines)
    assert delivery._delivery_ledger().reserve_delivery(f"{REPO}#{PR}", {
        "status": "updated", "run_id": "run-2", "token": "seeded", "commit": HEAD,
        "existing_pr": {"pr_number": PR, "branch": BRANCH, "base_branch": "main"}})


@pytest.mark.parametrize("route", ["review", "ci", "resumed"])
def test_a_correction_receipt_takes_its_parent_from_the_audit_line(
    monkeypatch, world, stored, tmp_path, route
):
    # Red without the fix (seen on all three routes): `fly_delivered_pr`
    # returned delivered=True with source_task="", so the correction was
    # stored with `relationships=None` and its edge never read back:
    #   assert None == [{'relationship_type': 'PART_OF', 'source_index': 0,
    #                    'target_entity_id': 'ent_synthetic_src'}]
    seed_correction_receipt(tmp_path, audit_line(source_task="ent_synthetic_src"))
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed", "accipiter": GUIDANCE})
    run_route(monkeypatch, route)
    assert rec.of("cicada") == []
    [task] = corrections(stored)
    assert task["pr_number"] == PR
    assert_filed_part_of_source(stored)


@pytest.mark.parametrize("lines", [
    [audit_line()],
    [audit_line(source_task="ent_synthetic_src"),
     audit_line(source_task="ent_synthetic_other")],
], ids=["no-task", "two-tasks"])
@pytest.mark.parametrize("route", ["review", "ci", "resumed"])
def test_a_correction_receipt_with_no_single_parent_holds(
    monkeypatch, world, stored, rounds_recorded, tmp_path, lines, route
):
    seed_correction_receipt(tmp_path, *lines)
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed", "accipiter": GUIDANCE})
    d, summary = run_route(monkeypatch, route)
    if summary is not None:
        assert summary["resumed"] == 0
    assert rec.calls == []  # no builder, no guidance lenses, no local commit
    assert stored["calls"] == []
    assert rounds_recorded == [] and world.fix_rounds == 0
    assert any("names a single source task" in m and "Needs your attention" in m
               for m in d.notifier.sent)


def test_a_correction_with_no_parent_is_never_stored(monkeypatch, world, stored):
    # By construction: even if routing ever hands it no source task.
    d = _dispatcher()
    facts = delivery.FlyDeliveredPR(True, branch=BRANCH, base_branch="main")
    ok = asyncio.run(d._file_fly_correction(
        trigger(), kind="review", round_n=1, body=UX_FINDING, facts=facts))
    assert ok is False and stored["calls"] == []
    assert any("no parent task" in m for m in d.notifier.sent)


# --- A receipt for this PR with a status the reader does not recognize ------
#
# Skipping it would leave a readable ledger that names the PR nowhere, and the
# round would run locally: the very thing this module exists to stop. A
# recognized receipt for the same PR does not outweigh it, and a recognized
# non-delivery status such as `not_delivered` behaves as before.

def seed_first_delivery_receipt(key=SRC_KEY, **entry):
    """Write one receipt straight into the ledger file, whatever its status
    (``reserve_delivery`` refuses a non-string one)."""
    ledger = delivery._delivery_ledger()
    with ledger._locked():
        ledger._save(ledger._load())  # a valid ledger, keeping any receipts
    raw = json.loads(ledger.path.read_text(encoding="utf-8"))
    raw.setdefault("deliveries", {})[key] = {
        "branch": BRANCH, "run_id": "run-1", "token": "seeded", "commit": HEAD,
        "readback": "passed", **entry}
    ledger.path.write_text(json.dumps(raw), encoding="utf-8")


def assert_held_unrecognized(d, summary, stored, rounds_recorded, rec, text="does not recognize"):
    if summary is not None:
        assert summary["resumed"] == 0
    assert rec.calls == []  # no builder, no guidance lenses, no local commit
    assert corrections(stored) == [] and rounds_recorded == [] and stored["calls"] == []
    assert any(text in m and "Needs your attention" in m for m in d.notifier.sent)


@pytest.mark.parametrize("named_by", ["pr_number", "key"])
@pytest.mark.parametrize("route", ["review", "ci", "resumed"])
def test_a_receipt_for_this_pr_with_an_unrecognized_status_holds(
    monkeypatch, world, stored, rounds_recorded, route, named_by
):
    # Red without the fix (run against the unfixed `fly_delivered_pr`; the
    # output kept below is from the review and CI routes): the receipt was
    # skipped, the route came back "local", and the round ran here. Review:
    #   AssertionError: assert [('accipiter'...nosed', ...})] == []
    #   Left contains 2 more items, first extra item: ('accipiter', {...})
    # CI:
    #   AssertionError: assert [('cicada', {...riage', ...})] == []
    #   Left contains one more item: ('cicada', {... 'work_class': 'ci_log_triage', ...})
    if named_by == "pr_number":
        seed_first_delivery_receipt(status="exhausted", pr_number=PR)
    else:
        seed_first_delivery_receipt(key=f"{REPO}#{PR}", status="exhausted")
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed", "accipiter": GUIDANCE})
    d, summary = run_route(monkeypatch, route)
    assert_held_unrecognized(d, summary, stored, rounds_recorded, rec)


@pytest.mark.parametrize("status", [None, 7, ["opened"], {"s": "opened"}],
                         ids=["none", "int", "list", "dict"])
@pytest.mark.parametrize("route", ["review", "ci", "resumed"])
def test_a_receipt_for_this_pr_with_a_non_string_status_holds(
    monkeypatch, world, stored, rounds_recorded, route, status
):
    # The ledger reader itself rejects a receipt with no string status, so the
    # whole ledger reads as unknown and the round holds on that ground.
    seed_first_delivery_receipt(status=status, pr_number=PR)
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed", "accipiter": GUIDANCE})
    d, summary = run_route(monkeypatch, route)
    assert_held_unrecognized(d, summary, stored, rounds_recorded, rec, text="could not be read")


@pytest.mark.parametrize("status", [None, 7, ["opened"]], ids=["none", "int", "list"])
def test_fly_delivered_pr_reads_a_non_string_status_as_unknown(monkeypatch, status):
    # Belt and braces behind the ledger's own validation: should a receipt
    # with a non-string status ever reach `fly_delivered_pr`, it is unknown.
    readable_empty_ledger()

    class Ledger:
        path = delivery._delivery_ledger().path

        def delivery_receipts(self):
            return {SRC_KEY: {"status": status, "pr_number": PR, "branch": BRANCH}}

    monkeypatch.setattr(delivery, "_delivery_ledger", Ledger)
    facts = delivery.fly_delivered_pr(REPO, PR, BRANCH)
    assert facts.delivered is None and "does not recognize" in facts.unknown_reason


@pytest.mark.parametrize("route", ["review", "ci", "resumed"])
def test_a_recognized_receipt_does_not_hide_an_unrecognized_one(
    monkeypatch, world, stored, rounds_recorded, route
):
    seed_fly_delivery()  # alone, this routes to Fly
    seed_first_delivery_receipt(key=f"{REPO}#task:ent_synthetic_other",
                                status="exhausted", pr_number=PR)
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed", "accipiter": GUIDANCE})
    d, summary = run_route(monkeypatch, route)
    assert_held_unrecognized(d, summary, stored, rounds_recorded, rec)


@pytest.mark.parametrize("route", ["review", "ci", "resumed"])
def test_a_not_delivered_receipt_for_this_pr_keeps_its_existing_handling(
    monkeypatch, world, stored, rounds_recorded, route
):
    # `not_delivered` is a recognized status naming no delivered PR: it adds
    # nothing, so with nothing else naming the PR the local round runs, as
    # it did before this check.
    seed_first_delivery_receipt(status="not_delivered", pr_number=PR)
    assert delivery.fly_delivered_pr(REPO, PR, BRANCH) == delivery.FlyDeliveredPR(False)
    rec = _Recorder(monkeypatch, stdout={"cicada": "fixed", "accipiter": GUIDANCE})
    d, summary = run_route(monkeypatch, route)
    assert rec.of("cicada") != []
    assert corrections(stored) == []
    assert not any("does not recognize" in m for m in d.notifier.sent)


def test_an_unrecognized_receipt_that_cannot_be_linked_to_this_pr_is_ignored(
    monkeypatch, world, stored, rounds_recorded
):
    seed_first_delivery_receipt(status="exhausted")  # no pr_number; key names a task
    assert delivery.fly_delivered_pr(REPO, PR, BRANCH) == delivery.FlyDeliveredPR(False)
    seed_first_delivery_receipt(key=f"{REPO}#task:ent_synthetic_x", status="exhausted",
                                pr_number=PR + 1)
    assert delivery.fly_delivered_pr(REPO, PR, BRANCH) == delivery.FlyDeliveredPR(False)
