"""Tests for the release-review publish gate (release_review_approval.py).

All GitHub, Neotoma, panel, notifier and publish calls are fakes: no network
and no real publish. Each refusal condition has its own test.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
import release_review_approval as rra
import swarm_dispatch
from github_gateway import SwarmTrigger
from swarm_dispatch import DispatchConfig, SwarmDispatcher

REPO = "example-org/example-product"
OPERATOR = "example-operator"
APP = "example-swarm[bot]"
HEAD = "a" * 40
OTHER = "b" * 40
PR = 42
PR_URL = f"https://github.com/{REPO}/pull/{PR}"
REVIEW_ID = 1000
VERSION = "v1.2.3"


def _pr(**over):
    data = {
        "number": PR,
        "state": "open",
        "merged": False,
        "html_url": PR_URL,
        "head": {"ref": f"release/{VERSION}", "sha": HEAD, "repo": {"full_name": REPO}},
        "base": {"ref": "main", "repo": {"full_name": REPO}},
    }
    data.update(over)
    return data


def _review(**over):
    data = {
        "id": REVIEW_ID,
        "user": {"login": OPERATOR, "type": "User"},
        "state": "APPROVED",
        "commit_id": HEAD,
        "body": "",
        "submitted_at": "2026-01-01T10:00:00Z",
    }
    data.update(over)
    return data


def _app_review(**over):
    data = {
        "id": 900,
        "user": {"login": APP, "type": "Bot"},
        "state": "APPROVED",
        "commit_id": HEAD,
        "body": "panel approval",
        "submitted_at": "2026-01-01T09:00:00Z",
    }
    data.update(over)
    return data


def _record(**over):
    snap = {
        "version": VERSION,
        "status": "pending_approval",
        "rc_pr_url": PR_URL,
        "rc_head_sha": HEAD,
    }
    snap.update(over)
    return {"entity_id": "ent_example", "snapshot": snap}


class Fake:
    """Holds the fake world; builds Deps over it."""

    def __init__(self):
        self.pr = _pr()
        self.pr_after = None  # what a second PR read returns, when set
        self.review = _review()
        self.reviews = [_app_review(), _review()]
        self.comments: list[dict] = []
        self.inline: list[dict] = []
        self.behind = 0
        self.records = [_record()]
        self.complete = True
        self.panel = (True, "")
        self.app_login = APP
        self.fail: set[str] = set()
        self.calls: list[str] = []
        self.publishes: list[list[str]] = []
        self.notices: list[str] = []
        self.posted: list[str] = []
        self.publish_result = (0, "published")
        self.pr_reads = 0
        self.notify_raises = False
        self.comment_raises = False

    async def github_get(self, path, params):
        self.calls.append(path)
        if path.endswith(f"/pulls/{PR}"):
            self.pr_reads += 1
            if "pr" in self.fail:
                return 500, None, {}
            if self.pr_reads > 1 and self.pr_after is not None:
                return 200, self.pr_after, {}
            return 200, self.pr, {}
        if path.endswith(f"/reviews/{REVIEW_ID}"):
            if "review" in self.fail:
                return 404, None, {}
            return 200, self.review, {}
        if path.endswith("/reviews"):
            if "reviews" in self.fail:
                return 200, {"not": "a list"}, {}
            return 200, self.reviews if params.get("page") == 1 else [], {}
        if path.endswith("/issues/42/comments"):
            return 200, self.comments if params.get("page") == 1 else [], {}
        if path.endswith("/pulls/42/comments"):
            return 200, self.inline if params.get("page") == 1 else [], {}
        if "/compare/" in path:
            if "compare" in self.fail:
                return 200, {}, {}
            return 200, {"behind_by": self.behind}, {}
        raise AssertionError(f"unexpected path {path}")

    async def release_results(self):
        if "records" in self.fail:
            raise RuntimeError("down")
        return self.records, self.complete

    async def panel_gate(self, repo, pr, head):
        if "panel" in self.fail:
            raise RuntimeError("panel down")
        return self.panel

    async def app_bot_login(self):
        return self.app_login

    def notify(self, text):
        self.notices.append(text)
        if self.notify_raises:
            raise RuntimeError("notifier down")

    async def post_comment(self, repo, pr, body):
        self.posted.append(body)
        if self.comment_raises:
            raise RuntimeError("comment down")
        return True

    async def run_publish(self, argv):
        self.publishes.append(argv)
        if self.publish_result == "timeout":
            raise TimeoutError()
        return self.publish_result

    def deps(self, script):
        return rra.Deps(
            github_get=self.github_get,
            release_results=self.release_results,
            panel_gate=self.panel_gate,
            app_bot_login=self.app_bot_login,
            notify=self.notify,
            post_comment=self.post_comment,
            run_publish=self.run_publish,
            operator_login=OPERATOR,
            release_repo=REPO,
            release_branch="main",
            publish_script=script,
            python="python3",
        )


@pytest.fixture
def world(tmp_path):
    script = tmp_path / "publish.py"
    script.write_text("# fake\n")
    fake = Fake()
    gate = rra.ReleaseReviewApproval(fake.deps(str(script)))
    return fake, gate


def _trigger(review_id=REVIEW_ID, **over):
    base = dict(  # noqa: C408 — keyword form mirrors SwarmTrigger's fields
        kind="pr_review",
        repository=REPO,
        number=PR,
        title="Release",
        body="",
        author="example-author",
        html_url=PR_URL,
        delivery_id="d-1",
        action="submitted",
        head_ref=f"release/{VERSION}",
        base_ref="main",
        head_sha=HEAD,
        review_state="approved",
        review_author=OPERATOR,
        raw={"review": {"id": review_id}},
    )
    base.update(over)
    return SwarmTrigger(**base)


def _run(gate, trigger=None):
    return asyncio.run(gate.handle_review(trigger or _trigger()))


def _assert_refused(fake, words):
    assert fake.publishes == [], "a refusal must never start publish"
    assert fake.notices and words in fake.notices[-1], fake.notices
    assert len(fake.posted) == 1 and words in fake.posted[0], fake.posted


# ── flag ─────────────────────────────────────────────────────────────────────


def test_flag_only_exact_one_enables():
    assert rra.enabled({rra.FLAG_ENV: "1"})
    for value in ("", "0", "true", "yes", " 1", "1 ", "01"):
        assert not rra.enabled({rra.FLAG_ENV: value}), value
    assert not rra.enabled({})


# ── passing case and replay ─────────────────────────────────────────────────


def test_pass_starts_publish_once_without_force(world):
    fake, gate = world
    assert _run(gate) is True
    assert len(fake.publishes) == 1
    argv = fake.publishes[0]
    assert "--force" not in argv
    assert argv[2:] == [
        "--version", VERSION, "--entity-id", "ent_example",
        "--from-github-review", str(REVIEW_ID),
    ]
    assert fake.posted == []


def test_repeated_delivery_publishes_once(world):
    fake, gate = world
    _run(gate)
    _run(gate)
    assert len(fake.publishes) == 1
    assert "already handled" in fake.posted[-1]


def test_concurrent_deliveries_publish_once(world):
    fake, gate = world

    async def both():
        await asyncio.gather(gate.handle_review(_trigger()), gate.handle_review(_trigger()))

    asyncio.run(both())
    assert len(fake.publishes) == 1


def test_publish_failure_and_timeout_are_reported(world, tmp_path):
    fake, gate = world
    fake.publish_result = (1, "step failed")
    _run(gate)
    assert "FAILED" in fake.notices[-1]
    fake2 = Fake()
    fake2.publish_result = "timeout"
    gate2 = rra.ReleaseReviewApproval(fake2.deps(gate.deps.publish_script))
    _run(gate2)
    assert "TIMED OUT" in fake2.notices[-1]


def test_not_release_branch_returns_to_normal_path(world):
    fake, gate = world
    fake.pr = _pr(head={"ref": "feature/x", "sha": HEAD, "repo": {"full_name": REPO}})
    assert _run(gate) is False
    assert fake.publishes == [] and fake.posted == []


# ── one test per refusal condition ──────────────────────────────────────────


def test_refuses_unreadable_pr(world):
    fake, gate = world
    fake.fail.add("pr")
    assert _run(gate) is True
    _assert_refused(fake, "pull request could not be read")


def test_refuses_missing_review_id(world):
    fake, gate = world
    _run(gate, _trigger(raw={}))
    _assert_refused(fake, "review id")


def test_refuses_unreadable_review(world):
    fake, gate = world
    fake.fail.add("review")
    _run(gate)
    _assert_refused(fake, "the review could not be read")


def test_refuses_non_operator_reviewer(world):
    fake, gate = world
    fake.review = _review(user={"login": "someone-else", "type": "User"})
    _run(gate)
    _assert_refused(fake, "not by the operator")


def test_refuses_bot_reviewer(world):
    fake, gate = world
    fake.review = _review(user={"login": OPERATOR, "type": "Bot"})
    _run(gate)
    _assert_refused(fake, "not by a person")


def test_payload_author_is_not_trusted(world):
    # The event says the operator approved; the live review says otherwise.
    fake, gate = world
    fake.review = _review(user={"login": "someone-else", "type": "User"})
    _run(gate, _trigger(review_author=OPERATOR))
    _assert_refused(fake, "not by the operator")


def test_refuses_review_not_approved(world):
    fake, gate = world
    fake.review = _review(state="COMMENTED")
    _run(gate)
    _assert_refused(fake, "not an approval")


def test_refuses_review_on_old_head(world):
    fake, gate = world
    fake.review = _review(commit_id=OTHER)
    _run(gate)
    _assert_refused(fake, "current head")


def test_refuses_later_changes_requested(world):
    fake, gate = world
    fake.reviews.append(_review(id=REVIEW_ID + 1, state="CHANGES_REQUESTED",
                                submitted_at="2026-01-01T11:00:00Z"))
    _run(gate)
    _assert_refused(fake, "requested changes or dismissed")


def test_refuses_later_dismissed(world):
    fake, gate = world
    fake.reviews.append(_review(id=REVIEW_ID + 1, state="DISMISSED",
                                submitted_at="2026-01-01T11:00:00Z"))
    _run(gate)
    _assert_refused(fake, "requested changes or dismissed")


@pytest.mark.parametrize("body", ["hold please", "Skip this one", "STOP", "do not publish yet"])
def test_refuses_later_hold_comment(world, body):
    fake, gate = world
    fake.comments.append({"user": {"login": OPERATOR}, "body": body,
                          "created_at": "2026-01-01T11:00:00Z"})
    _run(gate)
    _assert_refused(fake, "later comment by the operator asks to hold")


def test_refuses_later_hold_review_body(world):
    fake, gate = world
    fake.reviews.append(_review(id=REVIEW_ID + 1, state="COMMENTED", body="hold",
                                submitted_at="2026-01-01T11:00:00Z"))
    _run(gate)
    _assert_refused(fake, "later review by the operator asks to hold")


def test_earlier_hold_comment_does_not_refuse(world):
    fake, gate = world
    fake.comments.append({"user": {"login": OPERATOR}, "body": "hold",
                          "created_at": "2026-01-01T08:00:00Z"})
    _run(gate)
    assert len(fake.publishes) == 1


def test_refuses_malformed_comment_timestamp(world):
    fake, gate = world
    fake.comments.append({"user": {"login": OPERATOR}, "body": "fine", "created_at": None})
    _run(gate)
    _assert_refused(fake, "timestamp could not be read")


def test_refuses_unreadable_review_list(world):
    fake, gate = world
    fake.fail.add("reviews")
    _run(gate)
    _assert_refused(fake, "unexpected shape")


def test_refuses_too_many_pages(world):
    fake, gate = world
    full = [{"user": {"login": "x"}, "body": "", "created_at": "2026-01-01T08:00:00Z"}] * 100

    async def endless(path, params):
        if path.endswith("/issues/42/comments"):
            return 200, full, {}
        return await Fake.github_get(fake, path, params)

    gate.deps.github_get = endless
    _run(gate)
    _assert_refused(fake, "more pages")


def test_refuses_closed_pr(world):
    fake, gate = world
    fake.pr = _pr(state="closed")
    _run(gate)
    _assert_refused(fake, "not open")


def test_refuses_wrong_base(world):
    fake, gate = world
    fake.pr = _pr(base={"ref": "develop", "repo": {"full_name": REPO}})
    _run(gate)
    _assert_refused(fake, "not based on main")


def test_refuses_fork_head(world):
    fake, gate = world
    fake.pr = _pr(head={"ref": f"release/{VERSION}", "sha": HEAD,
                        "repo": {"full_name": "someone/fork"}})
    _run(gate)
    _assert_refused(fake, "not in the release repository")


def test_refuses_behind_base(world):
    fake, gate = world
    fake.behind = 2
    _run(gate)
    _assert_refused(fake, "behind main")


def test_refuses_unreadable_compare(world):
    fake, gate = world
    fake.fail.add("compare")
    _run(gate)
    _assert_refused(fake, "behind could not be read")


def test_refuses_no_record(world):
    fake, gate = world
    fake.records = []
    _run(gate)
    _assert_refused(fake, "0 release records")


def test_refuses_two_records(world):
    fake, gate = world
    fake.records = [_record(), {**_record(), "entity_id": "ent_other"}]
    _run(gate)
    _assert_refused(fake, "2 release records")


def test_matches_release_url_field(world):
    fake, gate = world
    fake.records = [_record(rc_pr_url="", release_url=PR_URL)]
    _run(gate)
    assert len(fake.publishes) == 1


def test_refuses_incomplete_record_list(world):
    fake, gate = world
    fake.complete = False
    _run(gate)
    _assert_refused(fake, "may be incomplete")


def test_refuses_unreadable_records(world):
    fake, gate = world
    fake.fail.add("records")
    _run(gate)
    _assert_refused(fake, "release records could not be read")


@pytest.mark.parametrize("status", ["approved", "publishing", "published", "failed", ""])
def test_refuses_record_not_pending(world, status):
    fake, gate = world
    fake.records = [_record(status=status)]
    _run(gate)
    _assert_refused(fake, "not pending_approval")


def test_refuses_record_other_head(world):
    fake, gate = world
    fake.records = [_record(rc_head_sha=OTHER)]
    _run(gate)
    _assert_refused(fake, "different head")


def test_refuses_panel_gate_fail(world):
    fake, gate = world
    fake.panel = (False, "lenses: FAIL")
    _run(gate)
    _assert_refused(fake, "panel gate did not pass")


def test_refuses_panel_gate_error(world):
    fake, gate = world
    fake.fail.add("panel")
    _run(gate)
    _assert_refused(fake, "could not be evaluated")


def test_refuses_panel_gate_base_exception(world):
    fake, gate = world

    class Limited(BaseException):
        pass

    async def limited(*_a):
        raise Limited()

    gate.deps.panel_gate = limited
    _run(gate)
    _assert_refused(fake, "could not be evaluated")


def test_refuses_missing_app_approval(world):
    fake, gate = world
    fake.reviews = [_review()]
    _run(gate)
    _assert_refused(fake, "panel approval is not on this head")


def test_refuses_app_approval_on_old_head(world):
    fake, gate = world
    fake.reviews = [_app_review(commit_id=OTHER), _review()]
    _run(gate)
    _assert_refused(fake, "panel approval is not on this head")


def test_refuses_unreadable_app_login(world):
    fake, gate = world
    fake.app_login = ""
    _run(gate)
    _assert_refused(fake, "App's login could not be read")


def test_refuses_head_changed_before_publish(world):
    fake, gate = world
    fake.pr_after = _pr(head={"ref": f"release/{VERSION}", "sha": OTHER,
                              "repo": {"full_name": REPO}})
    _run(gate)
    _assert_refused(fake, "head changed before publishing")


def test_refuses_missing_publish_script(world):
    fake, gate = world
    gate.deps.publish_script = "/nonexistent/publish.py"
    _run(gate)
    _assert_refused(fake, "publish script is missing")


def test_notice_failures_never_publish(world):
    fake, gate = world
    fake.review = _review(state="COMMENTED")
    fake.notify_raises = True
    fake.comment_raises = True
    assert _run(gate) is True
    assert fake.publishes == []


def test_other_entrance_closed_for_release_pr(world):
    fake, gate = world
    assert asyncio.run(gate.guard_other_entrance(_trigger(kind="issue_comment"), "approve comment"))
    _assert_refused(fake, "approve comment was not acted on")


def test_other_entrance_open_for_non_release_pr(world):
    fake, gate = world
    fake.pr = _pr(head={"ref": "feature/x", "sha": HEAD, "repo": {"full_name": REPO}})
    assert not asyncio.run(gate.guard_other_entrance(_trigger(), "approve comment"))
    assert fake.posted == []


# ── dispatcher wiring ───────────────────────────────────────────────────────


def _dispatcher():
    class Notifier:
        def __init__(self):
            self.sent = []

        def send(self, message, **_kw):
            self.sent.append(message)

    return SwarmDispatcher(Notifier(), DispatchConfig(neotoma_token="", github_token=""))


def _wire(monkeypatch, d):
    merged = []

    async def fake_merge(self, trigger, source):
        merged.append(source)

    monkeypatch.setattr(SwarmDispatcher, "_approve_and_maybe_merge", fake_merge)
    monkeypatch.setattr(swarm_dispatch, "PHOENICURUS_RELEASE_REPO", REPO)
    monkeypatch.setattr(swarm_dispatch, "_OPERATOR_LOGIN", OPERATOR)
    return merged


def test_flag_off_unchanged_and_no_network(monkeypatch):
    monkeypatch.delenv(rra.FLAG_ENV, raising=False)
    d = _dispatcher()
    merged = _wire(monkeypatch, d)

    def no_network(**_kw):
        raise AssertionError("flag off must make no new network call")

    monkeypatch.setattr(httpx, "AsyncClient", no_network)
    monkeypatch.setattr(rra, "build_for_dispatcher",
                        lambda *a: (_ for _ in ()).throw(AssertionError("gate built")))
    asyncio.run(d._handle_pr_review(_trigger()))
    asyncio.run(d._handle_email_approve(_trigger(kind="email_approve")))
    asyncio.run(d._handle_approve(_trigger(kind="issue_comment")))
    assert merged == ["GitHub review", "email reply", "approve comment"]


@pytest.mark.parametrize("value", ["0", "true", "", "yes"])
def test_flag_other_values_unchanged(monkeypatch, value):
    monkeypatch.setenv(rra.FLAG_ENV, value)
    d = _dispatcher()
    merged = _wire(monkeypatch, d)
    monkeypatch.setattr(rra, "build_for_dispatcher",
                        lambda *a: (_ for _ in ()).throw(AssertionError("gate built")))
    asyncio.run(d._handle_pr_review(_trigger()))
    assert merged == ["GitHub review"]


def test_flag_on_release_pr_never_reaches_merge_path(monkeypatch, tmp_path):
    """With the flag on, a release PR review is handled by the gate, not merged."""
    monkeypatch.setenv(rra.FLAG_ENV, "1")
    d = _dispatcher()
    merged = _wire(monkeypatch, d)
    script = tmp_path / "publish.py"
    script.write_text("# fake\n")
    fake = Fake()
    monkeypatch.setattr(rra, "build_for_dispatcher",
                        lambda *a: rra.ReleaseReviewApproval(fake.deps(str(script))))
    asyncio.run(d._handle_pr_review(_trigger()))
    assert merged == [], "a release PR review must never reach the merge path"
    assert len(fake.publishes) == 1
    # A refused review does not fall back to merge either.
    fake2 = Fake()
    fake2.review = _review(state="COMMENTED")
    monkeypatch.setattr(rra, "build_for_dispatcher",
                        lambda *a: rra.ReleaseReviewApproval(fake2.deps(str(script))))
    d2 = _dispatcher()
    asyncio.run(d2._handle_pr_review(_trigger()))
    assert merged == []
    assert fake2.publishes == []


def test_flag_on_comment_and_email_closed_for_release_pr(monkeypatch, tmp_path):
    monkeypatch.setenv(rra.FLAG_ENV, "1")
    d = _dispatcher()
    merged = _wire(monkeypatch, d)
    fake = Fake()
    monkeypatch.setattr(rra, "build_for_dispatcher",
                        lambda *a: rra.ReleaseReviewApproval(fake.deps(str(tmp_path / "p.py"))))
    asyncio.run(d._handle_email_approve(_trigger(kind="email_approve")))
    asyncio.run(d._handle_approve(_trigger(kind="issue_comment")))
    assert merged == []
    assert fake.publishes == []
    assert len(fake.posted) == 2


def test_flag_on_other_repo_unchanged(monkeypatch):
    monkeypatch.setenv(rra.FLAG_ENV, "1")
    d = _dispatcher()
    merged = _wire(monkeypatch, d)
    monkeypatch.setattr(rra, "build_for_dispatcher",
                        lambda *a: (_ for _ in ()).throw(AssertionError("gate built")))
    asyncio.run(d._handle_pr_review(_trigger(repository="example-org/other")))
    assert merged == ["GitHub review"]


def test_flag_on_non_release_pr_in_release_repo_merges_as_before(monkeypatch, tmp_path):
    monkeypatch.setenv(rra.FLAG_ENV, "1")
    d = _dispatcher()
    merged = _wire(monkeypatch, d)
    fake = Fake()
    fake.pr = _pr(head={"ref": "feature/x", "sha": HEAD, "repo": {"full_name": REPO}})
    monkeypatch.setattr(rra, "build_for_dispatcher",
                        lambda *a: rra.ReleaseReviewApproval(fake.deps(str(tmp_path / "p.py"))))
    asyncio.run(d._handle_pr_review(_trigger()))
    assert merged == ["GitHub review"]


def test_refuses_timestamp_without_zone(world):
    fake, gate = world
    fake.review = _review(submitted_at="2026-01-01T10:00:00")
    _run(gate)
    _assert_refused(fake, "no time zone")


def test_after_timeout_a_second_delivery_does_not_publish_again(world):
    fake, gate = world
    fake.publish_result = "timeout"
    _run(gate)
    _run(gate, _trigger(review_id=REVIEW_ID))
    assert len(fake.publishes) == 1
    assert "may still be running" in fake.notices[0]
