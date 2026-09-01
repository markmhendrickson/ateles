"""
The carrier that takes a PR PAST a verdict.

## The failure these cover

Measured on the live ateles repo, 2026-08-31: 47 open PRs. 23 at
CHANGES_REQUESTED (median 33 days, oldest 67). Four APPROVED and unmerged for
12, 33, 47 and 53 days — three of which had rotted from CLEAN to CONFLICTING
purely through elapsed time.

Review capacity was never the constraint. Over half that queue was reviewed
successfully. What was missing is a mechanism:

* `resume_deferred_reviews`, `resume_stalled_reviews` and
  `resume_missing_lens_reviews` all carry a PR TOWARD a verdict. Every one of
  them stops the moment a verdict exists.
* `resume_stalled_reviews` explicitly requires ZERO reviews. A PR at
  CHANGES_REQUESTED has one, so it is out of scope by design — and that design
  is right, but it left "reviewed, feedback addressed by a push, nothing
  re-armed it" with no watcher anywhere in the swarm.
* Nothing at all observed an APPROVED PR sitting unmerged, because an approved
  PR presents as done.

`resume_unactioned_revisions` had ZERO grep hits on origin/main before this
change: it was filed as task ent_4f11684cbf315fcd03900b10 and never built.

## What these tests assert

Observable effects — the re-dispatch happening, the notifier firing at a given
priority, the returned report's values — not that a string was logged. Per
policy `fixed_means_behavior_verified_not_contract_accepted`
(ent_db0b7855d47012084477fb00), a suite that accepts a contract without
exercising the behaviour is how PR #602's feature stayed 31/31 green while
never once working.

The candidate-selection tests drive the REAL `_prs_with_unactioned_revisions`
and `_approved_unmerged_prs` against a faked GitHub HTTP layer, so the
review-decision and push-ordering logic is genuinely under test rather than
stubbed past.

Run: pytest execution/daemons/apis/test_merge_carrier.py -v
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import swarm_dispatch as sd


class _Notifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, object]] = []

    def send(self, msg: str, priority=None, handler=None) -> None:  # noqa: ANN001
        self.sent.append((msg, priority))

    def at(self, priority) -> list[str]:  # noqa: ANN001
        return [m for m, p in self.sent if p == priority]


def _dispatcher() -> sd.SwarmDispatcher:
    return sd.SwarmDispatcher(notifier=_Notifier())


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)


def _freeze_dispatcher_now(monkeypatch) -> None:  # noqa: ANN001
    class _FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            if tz is None:
                return NOW.replace(tzinfo=None)
            return NOW.astimezone(tz)

    monkeypatch.setattr(sd, "datetime", _FrozenDateTime)


def _pr(number: int, *, sha: str = "abc1234", draft: bool = False) -> dict:
    return {
        "number": number,
        "title": f"PR {number}",
        "body": "",
        "draft": draft,
        "user": {"login": "cicada"},
        "html_url": f"https://github.com/o/r/pull/{number}",
        "head": {"ref": "feature", "sha": sha},
        "base": {"ref": "main"},
    }


def _review(state: str, when: datetime, login: str = "vanellus") -> dict:
    return {"state": state, "submitted_at": _iso(when), "user": {"login": login}}


class _Resp:
    def __init__(self, payload) -> None:  # noqa: ANN001
        self._payload = payload
        self.content = b"x"
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self) -> None:
        return None


def _install_github(
    monkeypatch,
    *,
    prs: list[dict],
    reviews: dict[int, list[dict]],
    commit_times: dict[str, datetime] | None = None,
    pr_detail: dict[int, dict] | None = None,
):
    """Fake the GitHub REST surface the candidate finders actually call."""
    commit_times = commit_times or {}
    pr_detail = pr_detail or {}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):  # noqa: ANN002
            return False

        async def get(self, url, **kw):  # noqa: ANN001
            if url.endswith("/pulls"):
                return _Resp(prs)
            if "/reviews" in url:
                number = int(url.split("/pulls/")[1].split("/")[0])
                return _Resp(reviews.get(number, []))
            if "/commits/" in url:
                sha = url.rsplit("/", 1)[1]
                when = commit_times.get(sha)
                if when is None:
                    raise RuntimeError("no such commit")
                return _Resp({"commit": {"committer": {"date": _iso(when)}}})
            # single-PR detail endpoint
            number = int(url.rsplit("/", 1)[1])
            return _Resp(pr_detail.get(number, {"mergeable_state": "clean"}))

    monkeypatch.setattr(sd.httpx, "AsyncClient", lambda **kw: _Client())


# ---------------------------------------------------------------------------
# Candidate selection: the push-after-review signal
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_changes_requested_with_later_push_is_a_candidate(monkeypatch):
    """The #511 shape: reviewed, blocked, author pushed the fix, silence."""
    d = _dispatcher()
    reviewed = NOW - timedelta(days=30)
    pushed = NOW - timedelta(days=29)
    _install_github(
        monkeypatch,
        prs=[_pr(163)],
        reviews={163: [_review("CHANGES_REQUESTED", reviewed)]},
        commit_times={"abc1234": pushed},
    )
    found = await d._prs_with_unactioned_revisions("o/r", NOW, 2700)
    assert [p["number"] for p in found] == [163]
    assert found[0]["_revision_pushed_at"] == pushed.isoformat()


@pytest.mark.asyncio
async def test_changes_requested_without_a_push_is_not_a_candidate(monkeypatch):
    """No revision since the block: the ball is legitimately with the author.

    This is the line between "answered and ignored" and "abandoned". Re-arming
    an abandoned PR would bury the fact that it is abandoned under a fresh
    review round, which is why the sweep is keyed on the push and not on age.
    """
    d = _dispatcher()
    pushed = NOW - timedelta(days=40)
    reviewed = NOW - timedelta(days=30)
    _install_github(
        monkeypatch,
        prs=[_pr(173)],
        reviews={173: [_review("CHANGES_REQUESTED", reviewed)]},
        commit_times={"abc1234": pushed},
    )
    assert await d._prs_with_unactioned_revisions("o/r", NOW, 2700) == []


@pytest.mark.asyncio
async def test_later_approval_by_same_reviewer_clears_the_block(monkeypatch):
    """A reviewer's own APPROVED supersedes their earlier CHANGES_REQUESTED.

    Without latest-review-per-reviewer semantics an approved-then-merged PR
    would be re-armed forever on the strength of a stale block.
    """
    d = _dispatcher()
    _install_github(
        monkeypatch,
        prs=[_pr(383)],
        reviews={
            383: [
                _review("CHANGES_REQUESTED", NOW - timedelta(days=10)),
                _review("APPROVED", NOW - timedelta(days=5)),
            ]
        },
        commit_times={"abc1234": NOW - timedelta(days=2)},
    )
    assert await d._prs_with_unactioned_revisions("o/r", NOW, 2700) == []


@pytest.mark.asyncio
async def test_comment_review_does_not_clear_a_block(monkeypatch):
    """A drive-by COMMENT must not dismiss a real blocking finding."""
    d = _dispatcher()
    _install_github(
        monkeypatch,
        prs=[_pr(201)],
        reviews={
            201: [
                _review("CHANGES_REQUESTED", NOW - timedelta(days=10), "vanellus"),
                _review("COMMENT", NOW - timedelta(days=9), "vanellus"),
            ]
        },
        commit_times={"abc1234": NOW - timedelta(days=2)},
    )
    assert [p["number"] for p in
            await d._prs_with_unactioned_revisions("o/r", NOW, 2700)] == [201]


@pytest.mark.asyncio
async def test_fresh_push_is_left_alone(monkeypatch):
    """A push whose re-review may still be in flight is never double-handled."""
    d = _dispatcher()
    _install_github(
        monkeypatch,
        prs=[_pr(495)],
        reviews={495: [_review("CHANGES_REQUESTED", NOW - timedelta(hours=2))]},
        commit_times={"abc1234": NOW - timedelta(minutes=5)},
    )
    assert await d._prs_with_unactioned_revisions("o/r", NOW, 2700) == []


@pytest.mark.asyncio
async def test_draft_prs_are_skipped(monkeypatch):
    d = _dispatcher()
    _install_github(
        monkeypatch,
        prs=[_pr(500, draft=True)],
        reviews={500: [_review("CHANGES_REQUESTED", NOW - timedelta(days=10))]},
        commit_times={"abc1234": NOW - timedelta(days=2)},
    )
    assert await d._prs_with_unactioned_revisions("o/r", NOW, 2700) == []


# ---------------------------------------------------------------------------
# The sweep: re-arm, bound, escalate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sweep_redispatches_cicada_and_never_repanels(monkeypatch):
    """Effect: Cicada is dispatched with the outstanding findings, and the
    panel is NOT re-run.

    #511 rules the re-panel out explicitly ("do not call `_handle_pr` or
    re-panel"; re-paneling CR PRs is listed as out of scope). The PR already
    carries a verdict with actionable findings — what is missing is someone
    acting on them. Asserted as an effect on `run_skill`'s arguments, not on a
    non-empty candidate list.
    """
    d = _dispatcher()
    calls: list[tuple] = []
    repaneled: list[str] = []

    async def fake_candidates(repo, now, stale):  # noqa: ANN001
        return [
            dict(
                _pr(163),
                _blocking_review_at="x",
                _revision_pushed_at="y",
                _blocking_review_body="[BLOCKING] the age parse drops the stamp",
            )
        ]

    async def fake_run_skill(skill, prompt, **kw):  # noqa: ANN001
        calls.append((skill, prompt, kw))
        return SimpleNamespace(ok=True, error="", stdout="", stderr="")

    async def fake_handle(trigger):  # noqa: ANN001
        repaneled.append(trigger.delivery_id)

    monkeypatch.setattr(d, "_prs_with_unactioned_revisions", fake_candidates)
    monkeypatch.setattr(d, "_handle_pr", fake_handle)
    monkeypatch.setattr(sd, "run_skill", fake_run_skill)

    summary = await d.resume_unactioned_revisions(["o/r"])

    assert summary["resumed"] == 1
    assert repaneled == [], "the panel must not be re-run (#511)"
    assert calls, "cicada was never dispatched"
    skill, prompt, kw = calls[0]
    assert skill == "cicada"
    assert "163" in prompt, "the fix prompt must name the PR"
    assert "the age parse drops the stamp" in prompt, (
        "the outstanding review findings must reach Cicada as guidance"
    )
    assert kw.get("include_github_contract") is True, "Cicada has to push"


@pytest.mark.asyncio
async def test_sweep_escalates_once_after_max_retries(monkeypatch):
    """A panel that cannot finish pages the operator instead of looping."""
    d = _dispatcher()
    monkeypatch.setenv("APIS_REVISION_MAX_RETRIES", "2")

    async def fake_candidates(repo, now, stale):  # noqa: ANN001
        return [_pr(163)]

    async def boom(skill, prompt, **kw):  # noqa: ANN001
        raise RuntimeError("cicada down")

    monkeypatch.setattr(d, "_prs_with_unactioned_revisions", fake_candidates)
    monkeypatch.setattr(sd, "run_skill", boom)

    for _ in range(2):
        await d.resume_unactioned_revisions(["o/r"])
    assert d.notifier.at(sd.Priority.BLOCKER) == []

    summary = await d.resume_unactioned_revisions(["o/r"])
    assert summary["escalated"] == 1
    assert len(d.notifier.at(sd.Priority.BLOCKER)) == 1

    await d.resume_unactioned_revisions(["o/r"])
    assert len(d.notifier.at(sd.Priority.BLOCKER)) == 1, "escalate-once violated"


@pytest.mark.asyncio
async def test_new_push_resets_the_retry_budget(monkeypatch):
    """A new revision is a new attempt.

    Keying the budget on the ref alone would let three failures against one
    revision permanently suppress every later revision of the same PR — the PR
    would be silently dead while its author kept pushing fixes.
    """
    d = _dispatcher()
    monkeypatch.setenv("APIS_REVISION_MAX_RETRIES", "1")
    sha = {"v": "sha_one"}
    handled: list[str] = []

    async def fake_candidates(repo, now, stale):  # noqa: ANN001
        return [_pr(163, sha=sha["v"])]

    async def fake_run_skill(skill, prompt, **kw):  # noqa: ANN001
        handled.append(skill)
        return SimpleNamespace(ok=True, error="", stdout="", stderr="")

    monkeypatch.setattr(d, "_prs_with_unactioned_revisions", fake_candidates)
    monkeypatch.setattr(sd, "run_skill", fake_run_skill)

    await d.resume_unactioned_revisions(["o/r"])
    await d.resume_unactioned_revisions(["o/r"])  # budget exhausted for sha_one
    assert len(handled) == 1

    sha["v"] = "sha_two"  # author pushed again
    await d.resume_unactioned_revisions(["o/r"])
    assert len(handled) == 2, "a new head SHA must re-arm the budget"


@pytest.mark.asyncio
async def test_sweep_is_fail_open_on_scan_error(monkeypatch):
    d = _dispatcher()

    async def boom(repo, now, stale):  # noqa: ANN001
        raise RuntimeError("github down")

    monkeypatch.setattr(d, "_prs_with_unactioned_revisions", boom)
    summary = await d.resume_unactioned_revisions(["o/r"])  # must not raise
    assert summary["scanned"] == 0


# ---------------------------------------------------------------------------
# Approved-unmerged visibility (ateles#565)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approved_clean_pr_is_found_with_age_and_state(monkeypatch):
    """The #452 shape: APPROVED, CLEAN, mergeable, and nothing merging it."""
    _freeze_dispatcher_now(monkeypatch)
    d = _dispatcher()
    _install_github(
        monkeypatch,
        prs=[_pr(452)],
        reviews={452: [_review("APPROVED", NOW - timedelta(days=12))]},
        pr_detail={452: {"mergeable_state": "clean", "merged": False}},
    )
    report = await d.report_pr_review_queue(["o/r"])
    assert report["approved_unmerged"] == 1
    entry = report["prs"][0]
    assert entry["approved_unmerged_age_days"] == 12
    assert entry["mergeable_state"] == "clean"
    assert entry["blocker"] == "merge_pending_operator"


@pytest.mark.asyncio
async def test_stale_approved_clean_pr_escalates_to_blocker(monkeypatch):
    """Mergeable, approved, nobody merging it — the operator is the only fix."""
    _freeze_dispatcher_now(monkeypatch)
    d = _dispatcher()
    _install_github(
        monkeypatch,
        prs=[_pr(452)],
        reviews={452: [_review("APPROVED", NOW - timedelta(days=12))]},
        pr_detail={452: {"mergeable_state": "clean", "merged": False}},
    )
    report = await d.report_pr_review_queue(["o/r"])
    assert report["escalated"] == 1
    blockers = d.notifier.at(sd.Priority.BLOCKER)
    assert len(blockers) == 1 and "o/r#452" in blockers[0]


@pytest.mark.asyncio
async def test_rotted_approved_pr_reports_rebase_not_merge(monkeypatch):
    """A DIRTY approved PR needs a rebase; telling the operator to merge lies."""
    _freeze_dispatcher_now(monkeypatch)
    d = _dispatcher()
    _install_github(
        monkeypatch,
        prs=[_pr(210)],
        reviews={210: [_review("APPROVED", NOW - timedelta(days=53))]},
        pr_detail={210: {"mergeable_state": "dirty", "merged": False}},
    )
    report = await d.report_pr_review_queue(["o/r"])
    assert report["rotted"] == 1
    assert report["prs"][0]["blocker"] == "rebase_required"
    assert "rebase" in d.notifier.at(sd.Priority.BLOCKER)[0].lower()


@pytest.mark.asyncio
async def test_freshly_approved_pr_does_not_page(monkeypatch):
    """Below the stale threshold the report stays quiet — noise trains people
    to ignore the channel, which is the failure this sweep exists to fix."""
    _freeze_dispatcher_now(monkeypatch)
    d = _dispatcher()
    _install_github(
        monkeypatch,
        prs=[_pr(600)],
        reviews={600: [_review("APPROVED", NOW - timedelta(hours=6))]},
        pr_detail={600: {"mergeable_state": "clean", "merged": False}},
    )
    report = await d.report_pr_review_queue(["o/r"])
    assert report["approved_unmerged"] == 1
    assert report["escalated"] == 0
    assert d.notifier.at(sd.Priority.BLOCKER) == []


@pytest.mark.asyncio
async def test_standing_block_outranks_an_approval(monkeypatch):
    """One reviewer's standing CHANGES_REQUESTED means the PR is NOT approved."""
    _freeze_dispatcher_now(monkeypatch)
    d = _dispatcher()
    _install_github(
        monkeypatch,
        prs=[_pr(226)],
        reviews={
            226: [
                _review("APPROVED", NOW - timedelta(days=10), "vanellus"),
                _review("CHANGES_REQUESTED", NOW - timedelta(days=9), "lanius"),
            ]
        },
        pr_detail={226: {"mergeable_state": "clean", "merged": False}},
    )
    report = await d.report_pr_review_queue(["o/r"])
    assert report["approved_unmerged"] == 0


@pytest.mark.asyncio
async def test_state_transition_pages_again(monkeypatch):
    """CLEAN → DIRTY changes what the operator can do, so it must re-page."""
    d = _dispatcher()
    state = {"v": "clean"}

    async def fake_candidates(repo, now):  # noqa: ANN001
        return [
            dict(
                _pr(335),
                _approved_at="x",
                _approved_age_days=33,
                _mergeable_state=state["v"],
            )
        ]

    monkeypatch.setattr(d, "_approved_unmerged_prs", fake_candidates)

    await d.report_pr_review_queue(["o/r"])
    await d.report_pr_review_queue(["o/r"])
    assert len(d.notifier.at(sd.Priority.BLOCKER)) == 1, "escalate-once violated"

    state["v"] = "dirty"  # it rotted
    await d.report_pr_review_queue(["o/r"])
    assert len(d.notifier.at(sd.Priority.BLOCKER)) == 2


@pytest.mark.asyncio
async def test_report_never_merges(monkeypatch):
    """The hard boundary: this carrier reports, it does not land anything.

    Agents do not merge autonomously. If `report_pr_review_queue` ever grows a
    merge call this test fails, which is the point.
    """
    _freeze_dispatcher_now(monkeypatch)
    d = _dispatcher()
    merged: list[int] = []

    async def spy_merge(repo, number, method="squash"):  # noqa: ANN001
        merged.append(number)
        return True, "deadbeef"

    monkeypatch.setattr(d, "_merge_pr", spy_merge)
    _install_github(
        monkeypatch,
        prs=[_pr(452)],
        reviews={452: [_review("APPROVED", NOW - timedelta(days=12))]},
        pr_detail={452: {"mergeable_state": "clean", "merged": False}},
    )
    await d.report_pr_review_queue(["o/r"])
    assert merged == [], "the queue report must never merge a PR"


@pytest.mark.asyncio
async def test_report_is_fail_open(monkeypatch):
    d = _dispatcher()

    async def boom(repo, now):  # noqa: ANN001
        raise RuntimeError("github down")

    monkeypatch.setattr(d, "_approved_unmerged_prs", boom)
    report = await d.report_pr_review_queue(["o/r"])  # must not raise
    assert report["approved_unmerged"] == 0


# ---------------------------------------------------------------------------
# Wiring: an unwired carrier is the same as no carrier
# ---------------------------------------------------------------------------


def test_both_sweeps_are_wired_into_the_daemon_loop():
    """PR #602's lesson: a suite can be green while the feature never runs.

    These sweeps only exist if the daemon calls them, so assert the call sites.
    """
    from pathlib import Path

    source = Path(__file__).with_name("apis.py").read_text()
    assert "dispatcher.resume_unactioned_revisions(" in source
    assert "dispatcher.report_pr_review_queue(" in source


# ---------------------------------------------------------------------------
# Unreviewed PRs: absence of a blocking finding is not approval (#599, #607)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unreviewed_pr_is_reported_as_its_own_state(monkeypatch):
    """The inverse hazard, measured 2026-08-31.

    4 of 13 same-day PRs (ateles#599, #607, neotoma#2270, #2271) had no parent
    issue, so gate inheritance blocked and no panel ever seated. Those PRs have
    no blocking review — not because they are clean, but because nobody looked.
    To any check of the form "are there unresolved blocking findings?" they
    present as the SAFEST PRs in the queue. ateles#599 touches the payment path.
    """
    d = _dispatcher()
    _install_github(
        monkeypatch,
        prs=[_pr(599)],
        reviews={599: []},
        pr_detail={599: {"mergeable_state": "clean", "merged": False}},
    )
    report = await d.report_pr_review_queue(["o/r"])
    assert report["unreviewed"] == 1
    assert report["approved_unmerged"] == 0, (
        "an unreviewed PR must NEVER be counted as approved"
    )
    assert report["prs"][0]["blocker"] == "never_reviewed"


@pytest.mark.asyncio
async def test_unreviewed_pr_pages_the_operator(monkeypatch):
    """Unknown must not collapse into pass — same tri-state rule as #560."""
    d = _dispatcher()
    _install_github(
        monkeypatch,
        prs=[_pr(607)],
        reviews={607: []},
        pr_detail={607: {"mergeable_state": "clean", "merged": False}},
    )
    await d.report_pr_review_queue(["o/r"])
    blockers = d.notifier.at(sd.Priority.BLOCKER)
    assert len(blockers) == 1
    assert "no formal review" in blockers[0].lower()
    # It must report the observation, not assert a single cause: #596 and
    # #602 have a Closes keyword AND zero formal reviews (a lens posted
    # [BLOCKING] findings as a plain comment). Naming one cause as "most
    # likely" was wrong for those, so the message names both and tells the
    # operator to read the comments.
    assert "absence of evidence" in blockers[0].lower()


def test_parent_issue_requires_a_closing_keyword():
    """Root cause of the bypass: a BARE `#597` reference does not seat a panel.

    ateles#607's body references #597 in prose; ateles#599's title carries
    "(#553)". Neither matches `_PARENT_ISSUE`, so `_parent_issue_number`
    returns None, gate inheritance blocks, and no review ever happens. The fix
    is a one-line convention — write `Closes #N` — not a code change, which is
    worth stating plainly rather than patching the regex to accept bare refs
    (that would silently adopt every incidentally-mentioned issue as a parent).
    """
    assert sd.SwarmDispatcher._parent_issue_number("Implements #597.") is None
    assert sd.SwarmDispatcher._parent_issue_number("fix(x): thing (#553)") is None
    assert sd.SwarmDispatcher._parent_issue_number("Closes #597") == 597
    assert sd.SwarmDispatcher._parent_issue_number("Fixes #553\nmore") == 553
