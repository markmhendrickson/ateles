"""A PR-body-only check failure spends no CI-fix round, and a head's CI is
judged on the newest run of each check only.

Incident shape: the only failing check was "design basis line", which reads
the PR body; Apis routed CI-fix rounds that no code push could satisfy. After
the body edit the head carried both the old failed run and a newer passing run
of that check, and the stale failure still read as "CI completed failing".
"""

from __future__ import annotations

import asyncio

import httpx

from execution.daemons.apis import swarm_dispatch
from execution.daemons.apis.swarm_dispatch import (
    CI_STATE_PR_BODY_ONLY,
    SwarmDispatcher,
    _latest_check_runs,
)
from execution.daemons.apis.test_swarm_dispatch import _StubNotifier, _config

HEAD = "a" * 40
BODY_CHECK = "design basis line"


def _run(name, conclusion, run_id, *, app_id=15368, completed_at=""):
    return {
        "id": run_id,
        "name": name,
        "status": "completed",
        "conclusion": conclusion,
        "app": {"id": app_id},
        "completed_at": completed_at,
        "html_url": f"https://github.com/owner/repo/runs/{run_id}",
    }


class _CommentClient:
    """Fake httpx client for the PR-comment list/post calls."""

    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return None

    async def get(self, url, **kwargs):
        comments = list(self.store["comments"])

        class _Resp:
            def raise_for_status(self_inner):
                return None

            def json(self_inner):
                return comments

        return _Resp()

    async def post(self, url, json=None, **kwargs):
        self.store["posts"].append(json["body"])
        self.store["comments"].append({"body": json["body"]})

        class _Resp:
            def raise_for_status(self_inner):
                return None

        return _Resp()


def _dispatcher(monkeypatch, runs, *, status_state="success"):
    """Dispatcher whose head carries `runs`; records fix-round side effects."""
    rec = {"routed": [], "recorded": [], "counted": 0, "comments": [], "posts": []}

    async def snapshot(self, trigger):
        # Production applies the latest-run filter inside the snapshot fetch.
        return HEAD, status_state, _latest_check_runs(runs)

    async def route(self, trigger, parent):
        rec["routed"].append(trigger.number)

    async def record(self, trigger, n):
        rec["recorded"].append(n)

    async def count(self, trigger):
        rec["counted"] += 1
        return 0

    async def allowed(self, trigger, *, source="webhook"):
        return True

    async def write_ok(self, repo, what, **kw):
        return True

    async def review_not_clear(self, repository, number, head):
        # A green head stops at "no clear panel verdict yet": no network.
        return False

    monkeypatch.setattr(SwarmDispatcher, "_fetch_head_ci_snapshot", snapshot)
    monkeypatch.setattr(SwarmDispatcher, "_route_ci_failure", route)
    monkeypatch.setattr(SwarmDispatcher, "_record_fix_round", record)
    monkeypatch.setattr(SwarmDispatcher, "_fix_round_count", count)
    monkeypatch.setattr(SwarmDispatcher, "_label_gate_allows", allowed)
    monkeypatch.setattr(SwarmDispatcher, "_require_github_write_credential", write_ok)
    monkeypatch.setattr(SwarmDispatcher, "_pr_review_is_clear", review_not_clear)
    monkeypatch.setattr(
        swarm_dispatch.httpx, "AsyncClient", lambda **k: _CommentClient(rec)
    )
    return SwarmDispatcher(_StubNotifier(), _config()), rec


def _pr():
    return {
        "number": 87,
        "title": "A pull request",
        "body": "Closes #80.",
        "html_url": "https://github.com/owner/repo/pull/87",
        "user": {"login": "someone"},
        "head": {"sha": HEAD, "ref": "feature"},
        "base": {"ref": "main"},
    }


def _ci_completed(d):
    from execution.daemons.apis.test_swarm_dispatch import _trigger

    return asyncio.run(
        d._handle_ci_status_for_current_head(_trigger(head_sha=HEAD), _pr(), HEAD)
    )


# ── body-only failure: no round, no budget, one comment ───────────────────────


def test_body_only_failure_files_no_fix_round_and_spends_no_budget(monkeypatch):
    runs = [_run("pytest", "success", 10), _run(BODY_CHECK, "failure", 11)]
    d, rec = _dispatcher(monkeypatch, runs)

    assert asyncio.run(d._required_ci_state(_pr_trigger())) == CI_STATE_PR_BODY_ONLY
    assert _ci_completed(d) is True

    assert rec["routed"] == []
    assert rec["recorded"] == []
    assert rec["counted"] == 0
    assert len(rec["posts"]) == 1
    assert "apis-fix-round" not in rec["posts"][0]
    assert "Design basis:" in rec["posts"][0]
    assert "re-runs the check" in rec["posts"][0]


def test_body_only_comment_is_posted_once_across_reruns(monkeypatch):
    runs = [_run("pytest", "success", 10), _run(BODY_CHECK, "failure", 11)]
    d, rec = _dispatcher(monkeypatch, runs)

    _ci_completed(d)
    _ci_completed(d)  # the check re-ran and failed again
    _ci_completed(d)

    assert len(rec["posts"]) == 1
    assert rec["routed"] == []


def test_mixed_body_and_code_failure_still_routes_a_fix_round(monkeypatch):
    runs = [_run("pytest", "failure", 10), _run(BODY_CHECK, "failure", 11)]
    d, rec = _dispatcher(monkeypatch, runs)

    assert _ci_completed(d) is True

    assert rec["routed"] == [87]
    assert rec["posts"] == []


def test_combined_status_failure_with_body_check_still_routes(monkeypatch):
    # A failing commit status is a code-side signal the body check cannot explain.
    runs = [_run(BODY_CHECK, "failure", 11)]
    d, rec = _dispatcher(monkeypatch, runs, status_state="failure")

    _ci_completed(d)

    assert rec["routed"] == [87]


def test_body_only_state_does_not_reach_merge_readiness(monkeypatch):
    runs = [_run("pytest", "success", 10), _run(BODY_CHECK, "failure", 11)]
    d, rec = _dispatcher(monkeypatch, runs)
    checkpoints = []

    async def fake_checkpoint(self, trigger, parent, lenses):
        checkpoints.append(trigger.number)

    monkeypatch.setattr(SwarmDispatcher, "_store_merge_checkpoint", fake_checkpoint)
    asyncio.run(d._gate_merge_readiness(_pr_trigger(), parent=80, panel=[]))

    assert checkpoints == []
    assert rec["routed"] == []
    assert len(rec["posts"]) == 1
    assert not any("READY TO MERGE" in m for m in d.notifier.sent)


# ── latest run per check name ─────────────────────────────────────────────────


def test_superseded_failed_run_with_newer_success_reads_green(monkeypatch):
    runs = [
        _run(BODY_CHECK, "failure", 11, completed_at="2026-11-16T10:00:00Z"),
        _run("pytest", "success", 12),
        _run(BODY_CHECK, "success", 13, completed_at="2026-11-16T10:05:00Z"),
    ]
    d, rec = _dispatcher(monkeypatch, runs)

    assert asyncio.run(d._required_ci_state(_pr_trigger())) == "green"
    _ci_completed(d)
    assert rec["routed"] == []
    assert rec["posts"] == []


def test_newest_run_failing_still_reads_failing(monkeypatch):
    runs = [
        _run("pytest", "success", 11, completed_at="2026-11-16T10:00:00Z"),
        _run("pytest", "failure", 14, completed_at="2026-11-16T10:30:00Z"),
    ]
    d, rec = _dispatcher(monkeypatch, runs)

    assert asyncio.run(d._required_ci_state(_pr_trigger())) == "failing"
    _ci_completed(d)
    assert rec["routed"] == [87]


def test_latest_check_runs_keeps_runs_of_other_apps_and_unnamed_runs():
    other_app = _run(BODY_CHECK, "failure", 5, app_id=99)
    unnamed = {"status": "completed", "conclusion": "failure"}
    runs = [
        _run(BODY_CHECK, "failure", 11),
        _run(BODY_CHECK, "success", 13),
        other_app,
        unnamed,
    ]
    kept = _latest_check_runs(runs)
    assert [r.get("id") for r in kept if r.get("name")] == [13, 5]
    assert unnamed in kept


def test_snapshot_fetch_applies_the_latest_run_filter(monkeypatch):
    """The real `_fetch_head_ci_snapshot` drops a superseded run."""
    runs = [_run(BODY_CHECK, "failure", 11), _run(BODY_CHECK, "success", 13)]

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url, **kwargs):
            if "/pulls/" in url:
                payload = {"head": {"sha": HEAD}}
            elif url.endswith("/status"):
                payload = {"state": "success"}
            else:
                payload = {"check_runs": runs}

            class _Resp:
                def raise_for_status(self_inner):
                    return None

                def json(self_inner):
                    return payload

            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **k: _Client())
    d = SwarmDispatcher(_StubNotifier(), _config())
    head, state, kept = asyncio.run(d._fetch_head_ci_snapshot(_pr_trigger()))
    assert [r["id"] for r in kept] == [13]


def _pr_trigger():
    return SwarmDispatcher._pr_trigger_from_api("owner/repo", _pr())
