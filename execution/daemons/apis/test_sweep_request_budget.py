"""
The periodic review sweep must not spend the App's quota on PRs it will skip.

## The failure these cover

Every step of `deferred_review_sweep_pass` listed every open PR and read each
one's comments and reviews; only afterwards did the label gate
(`ATELES_SWARM_REQUIRE_LABEL`, the `swarm-canary` lane) drop nearly all of them.
Measured on a quiet night: about 4,100 of the installation's 5,000 hourly calls,
until approvals and lens posting failed with 403.

These tests use a mocked GitHub (an `httpx.MockTransport` that records every
request path) and cover:

* gate set, N unlabelled PRs: no per-PR read at all;
* gate set, a labelled PR: still fully processed;
* gate set, an unlabelled PR whose parent issue is labelled: still processed,
  and the parent read is made once per pass, not once per step;
* gate unset: every PR is read and processed as before;
* the open-PR list is read once per repository per pass (plus the supersession
  step's own paged read), not once per step;
* low `X-RateLimit-Remaining`: the pass stops and says when the quota resets;
* a CI-completion webhook for a PR outside the lane stops before the check and
  review reads (auto-merge mode, where every later outcome is gated anyway).

Run: pytest execution/daemons/apis/test_sweep_request_budget.py -v
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import apis
import swarm_dispatch as sd
from github_gateway import SwarmTrigger

CANARY = "swarm-canary"
REPOS = ["o/a", "o/b", "o/c"]
_REAL_CLIENT = httpx.AsyncClient


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, msg, priority=None, handler=None, **kwargs):  # noqa: ANN001
        self.sent.append(msg)

    def clear_dedupe(self, key):  # noqa: ANN001
        return None


def _dispatcher(require_label: str, auto_merge: bool = False) -> sd.SwarmDispatcher:
    config = sd.DispatchConfig(
        neotoma_token="",
        github_token="",
        auto_merge=auto_merge,
        auto_rereview_on_push=False,
        require_label=require_label,
        resume_repositories=tuple(REPOS),
    )
    return sd.SwarmDispatcher(_Notifier(), config)


def _pr(number: int, labels=(), body: str = "no parent") -> dict:
    old = datetime.now(timezone.utc) - timedelta(hours=5)
    return {
        "number": number,
        "title": f"PR {number}",
        "body": body,
        "draft": False,
        "updated_at": old.isoformat().replace("+00:00", "Z"),
        "user": {"login": "someone"},
        "html_url": f"https://github.com/o/r/pull/{number}",
        "head": {"ref": "feature", "sha": "c" * 40},
        "base": {"ref": "main"},
        "labels": [{"name": n} for n in labels],
    }


class FakeGitHub:
    """Records every request; serves open-PR lists, empty comments/reviews and
    parent-issue labels. `remaining` is echoed as X-RateLimit-Remaining."""

    def __init__(self, prs: dict[str, list[dict]], *, remaining=None, issues=None):
        self.prs = prs
        self.remaining = remaining
        self.issues = issues or {}
        self.paths: list[str] = []
        self.reset = int(time.time()) + 1800

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.paths.append(path)
        headers = {}
        if self.remaining is not None:
            headers = {
                "X-RateLimit-Remaining": str(self.remaining),
                "X-RateLimit-Reset": str(self.reset),
            }
        parts = path.strip("/").split("/")  # repos/o/r/<kind>/...
        repo = "/".join(parts[1:3])
        if len(parts) == 4 and parts[3] == "pulls":
            return httpx.Response(200, json=self.prs.get(repo, []), headers=headers)
        if len(parts) == 5 and parts[3] == "issues":
            number = int(parts[4])
            labels = self.issues.get((repo, number), [])
            return httpx.Response(
                200, json={"number": number, "labels": [{"name": n} for n in labels]}
            )
        return httpx.Response(200, json=[])

    def list_reads(self) -> int:
        return sum(1 for p in self.paths if p.endswith("/pulls"))

    def per_pr_reads(self) -> int:
        return sum(1 for p in self.paths if not p.endswith("/pulls"))

    def issue_reads(self) -> int:
        return sum(
            1 for p in self.paths if p.strip("/").split("/")[3:4] == ["issues"]
            and len(p.strip("/").split("/")) == 5
        )


@pytest.fixture
def wire(monkeypatch):
    """Route every httpx client through a FakeGitHub and stub the dispatches."""
    handled: list[tuple[str, int]] = []

    def install(d: sd.SwarmDispatcher, gh: FakeGitHub) -> None:
        monkeypatch.setattr(
            sd.httpx,
            "AsyncClient",
            lambda *a, **k: _REAL_CLIENT(transport=httpx.MockTransport(gh.handler)),
        )

        async def handle_pr(trigger):  # noqa: ANN001
            handled.append((trigger.repository, trigger.number))

        async def not_held(trigger, source=""):  # noqa: ANN001
            return False

        async def ready(repositories):  # noqa: ANN001
            return None

        monkeypatch.setattr(d, "_handle_pr", handle_pr)
        monkeypatch.setattr(d, "_hold_panel_until_checks_green", not_held)
        monkeypatch.setattr(d, "_await_github_app_ready", ready)
        monkeypatch.setattr(d, "_github_headers", lambda repo="": {})

    install.handled = handled
    return install


def _all_unlabelled(n: int) -> dict[str, list[dict]]:
    return {repo: [_pr(i) for i in range(1, n + 1)] for repo in REPOS}


@pytest.mark.asyncio
async def test_gate_set_unlabelled_prs_cost_no_per_pr_reads(wire):
    gh = FakeGitHub(_all_unlabelled(30))
    d = _dispatcher(CANARY)
    wire(d, gh)

    await apis.deferred_review_sweep_pass(d)

    assert gh.per_pr_reads() == 0, (
        f"90 unlabelled PRs made {gh.per_pr_reads()} per-PR reads; "
        f"first few: {[p for p in gh.paths if not p.endswith('/pulls')][:4]}"
    )
    assert wire.handled == []


@pytest.mark.asyncio
async def test_gate_set_labelled_pr_is_still_fully_processed(wire):
    prs = _all_unlabelled(30)
    prs["o/a"][4] = _pr(5, labels=[CANARY])
    gh = FakeGitHub(prs)
    d = _dispatcher(CANARY)
    wire(d, gh)

    await apis.deferred_review_sweep_pass(d)

    assert ("o/a", 5) in wire.handled, "the labelled PR must still be re-driven"
    read = {p for p in gh.paths if not p.endswith("/pulls")}
    assert any("/pulls/5/reviews" in p for p in read)
    assert any("/issues/5/comments" in p for p in read)
    assert not any("/pulls/6/" in p or "/issues/6/" in p for p in read), (
        "an unlabelled neighbour must not be read"
    )


@pytest.mark.asyncio
async def test_gate_set_parent_labelled_pr_processed_and_parent_read_once(wire):
    prs = {repo: [] for repo in REPOS}
    # five unlabelled PRs share one parent that carries the label; five others
    # share an unlabelled parent.
    prs["o/a"] = [_pr(i, body="Closes #900") for i in range(1, 6)] + [
        _pr(i, body="Closes #901") for i in range(6, 11)
    ]
    gh = FakeGitHub(prs, issues={("o/a", 900): [CANARY], ("o/a", 901): ["bug"]})
    d = _dispatcher(CANARY)
    wire(d, gh)

    await apis.deferred_review_sweep_pass(d)

    handled = {n for r, n in wire.handled if r == "o/a"}
    assert handled == {1, 2, 3, 4, 5}, "PRs under the labelled parent are in the lane"
    parent_reads = [p for p in gh.paths if p in ("/repos/o/a/issues/900", "/repos/o/a/issues/901")]
    assert sorted(parent_reads) == ["/repos/o/a/issues/900", "/repos/o/a/issues/901"], (
        f"each parent must be read once per pass, got {parent_reads}"
    )


@pytest.mark.asyncio
async def test_gate_unset_every_pr_is_still_read_and_processed(wire):
    gh = FakeGitHub(_all_unlabelled(4))
    d = _dispatcher("")
    wire(d, gh)

    await apis.deferred_review_sweep_pass(d)

    for repo in REPOS:
        for n in range(1, 5):
            assert any(f"/repos/{repo}/pulls/{n}/reviews" in p for p in gh.paths)
            assert (repo, n) in wire.handled, "gate unset: stalled PRs re-driven as before"
    assert not any(
        p.endswith(("/issues/1", "/issues/2")) for p in gh.paths
    ), "no parent-issue read without the gate"


@pytest.mark.asyncio
async def test_open_pr_list_is_shared_between_steps(wire):
    gh = FakeGitHub(_all_unlabelled(3))
    d = _dispatcher("")
    wire(d, gh)

    await apis.deferred_review_sweep_pass(d)

    per_repo = gh.list_reads() / len(REPOS)
    assert per_repo <= 2, (
        f"{per_repo} open-PR list reads per repository per pass; one shared "
        "read plus the supersession step's own paged read is the most allowed"
    )


@pytest.mark.asyncio
async def test_low_quota_stops_the_pass_and_logs_the_reset(wire, monkeypatch, caplog):
    monkeypatch.setenv("APIS_SWEEP_MIN_REMAINING", "1000")
    prs = _all_unlabelled(5)
    prs["o/a"][0] = _pr(1, labels=[CANARY])
    gh = FakeGitHub(prs, remaining=120)
    d = _dispatcher(CANARY)
    wire(d, gh)

    with caplog.at_level(logging.WARNING):
        await apis.deferred_review_sweep_pass(d)

    assert gh.per_pr_reads() == 0, "below the floor, no per-PR read may follow"
    assert gh.list_reads() <= 1, "after the first reading the pass stops listing"
    lines = [r.getMessage() for r in caplog.records if "quota" in r.getMessage()]
    assert len(lines) == 1, f"exactly one stop line, got {lines}"
    reset = datetime.fromtimestamp(gh.reset, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert reset in lines[0], "the line must say when the quota resets"


@pytest.mark.asyncio
async def test_quota_above_floor_does_not_stop_the_pass(wire, monkeypatch):
    monkeypatch.setenv("APIS_SWEEP_MIN_REMAINING", "1000")
    prs = _all_unlabelled(3)
    prs["o/a"][0] = _pr(1, labels=[CANARY])
    gh = FakeGitHub(prs, remaining=4000)
    d = _dispatcher(CANARY)
    wire(d, gh)

    await apis.deferred_review_sweep_pass(d)

    assert ("o/a", 1) in wire.handled


def _ci_trigger() -> SwarmTrigger:
    return SwarmTrigger(
        kind="ci_status",
        repository="o/a",
        number=0,
        title="",
        body="",
        author="",
        html_url="",
        delivery_id="d",
        action="completed",
        ci_head_sha="c" * 40,
        ci_conclusion="success",
        ci_pr_numbers=[7],
    )


@pytest.mark.parametrize(
    "labels,expect_reads", [([], False), ([CANARY], True)], ids=["unlabelled", "labelled"]
)
@pytest.mark.asyncio
async def test_ci_webhook_gates_before_check_reads(monkeypatch, labels, expect_reads):
    d = _dispatcher(CANARY, auto_merge=True)
    pr = _pr(7, labels=labels)
    pr["state"] = "open"
    read: list[str] = []

    async def fetch_pr(repository, number):  # noqa: ANN001
        return pr

    async def ci_state(trigger):  # noqa: ANN001
        read.append("checks")
        return "pending"

    monkeypatch.setattr(d, "_fetch_pr", fetch_pr)
    monkeypatch.setattr(d, "_required_ci_state", ci_state)

    await d._handle_ci_status(_ci_trigger())

    assert bool(read) is expect_reads


@pytest.mark.asyncio
async def test_ci_webhook_gate_unset_reads_checks_as_before(monkeypatch):
    d = _dispatcher("", auto_merge=True)
    pr = _pr(7)
    pr["state"] = "open"
    read: list[str] = []

    async def fetch_pr(repository, number):  # noqa: ANN001
        return pr

    async def ci_state(trigger):  # noqa: ANN001
        read.append("checks")
        return "pending"

    monkeypatch.setattr(d, "_fetch_pr", fetch_pr)
    monkeypatch.setattr(d, "_required_ci_state", ci_state)

    await d._handle_ci_status(_ci_trigger())

    assert read == ["checks"]
