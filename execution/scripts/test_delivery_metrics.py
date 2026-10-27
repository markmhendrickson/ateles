"""Tests for delivery_metrics.py against a synthetic, in-memory GitHub.

No network and no `gh` process: the script's transport is replaced by
FakeGitHub, which serves the same JSON shapes (and the same page/per_page and
search-cap behaviour) as the REST endpoints the script reads. Repo, branch and
PR names are invented.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import delivery_metrics as dm  # noqa: E402

REPO = "acme/widgets"
SINCE = datetime(2026, 10, 13, tzinfo=timezone.utc)
UNTIL = datetime(2026, 10, 17, tzinfo=timezone.utc)


def ts(day: int, hour: int = 0, minute: int = 0) -> str:
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def pr(
    number,
    created,
    merged,
    *,
    base="main",
    title=None,
    body="",
    labels=(),
    merge_sha=None,
    head_sha=None,
    updated=None,
):
    return {
        "number": number,
        "title": title or f"Change {number}",
        "body": body,
        "labels": [{"name": n} for n in labels],
        "created_at": created,
        "merged_at": merged,
        "updated_at": updated or merged or created,
        "merge_commit_sha": merge_sha or f"m{number:03d}",
        "head": {"sha": head_sha or f"h{number:03d}"},
        "base": {"ref": base},
        "state": "closed",
    }


def run(
    rid,
    sha,
    start,
    end,
    *,
    event="pull_request",
    conclusion="success",
    name="CI",
    status="completed",
):
    return {
        "id": rid,
        "head_sha": sha,
        "event": event,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "created_at": start,
        "run_started_at": start,
        "updated_at": end,
    }


class FakeGitHub:
    """Serves the endpoints delivery_metrics reads; counts requests per path."""

    def __init__(self):
        self.pulls: list[dict] = []
        self.commits: dict[int, list[dict]] = {}
        self.runs: list[dict] = []
        self.workflows: list[dict] = []
        self.workflow_runs: dict[int, list[dict]] = {}
        self.deployments: list[dict] = []
        self.statuses: dict[int, list[dict]] = {}
        self.compare: dict[tuple[str, str], str] = {}
        self.required: list[str] | None = None
        self.default_branch = "main"
        self.requests: list[tuple[str, dict]] = []
        self.search_cap = dm.SEARCH_CAP

    @staticmethod
    def _page(items, params):
        per = int(params.get("per_page", 30))
        page = int(params.get("page", 1))
        return items[(page - 1) * per : page * per]

    def _runs_response(self, runs, params):
        lo, hi = params["created"].split("..")
        lo_dt, hi_dt = dm.parse_ts(lo), dm.parse_ts(hi)
        hits = [r for r in runs if lo_dt <= dm.parse_ts(r["created_at"]) <= hi_dt]
        capped = hits[: self.search_cap]
        return {"total_count": len(hits), "workflow_runs": self._page(capped, params)}

    def __call__(self, path, params):
        self.requests.append((path, params))
        base = f"/repos/{REPO}"
        if path == base:
            return {"default_branch": self.default_branch}
        if path == f"{base}/pulls":
            items = [
                p for p in self.pulls if p["state"] == params.get("state", "closed")
            ]
            if "base" in params:
                items = [p for p in items if p["base"]["ref"] == params["base"]]
            items = sorted(items, key=lambda p: p["updated_at"], reverse=True)
            return self._page(items, params)
        if path.startswith(f"{base}/pulls/") and path.endswith("/commits"):
            number = int(path.split("/")[-2])
            return self._page(self.commits.get(number, []), params)
        if path == f"{base}/actions/runs":
            return self._runs_response(self.runs, params)
        if path == f"{base}/branches/main/protection/required_status_checks":
            if self.required is None:
                raise dm.ApiError(404, "Not Found (HTTP 404)")
            return {"contexts": self.required, "checks": []}
        if path == f"{base}/actions/workflows":
            return {
                "total_count": len(self.workflows),
                "workflows": self._page(self.workflows, params),
            }
        if path.startswith(f"{base}/actions/workflows/") and path.endswith("/runs"):
            wid = int(path.split("/")[-2])
            return self._runs_response(self.workflow_runs.get(wid, []), params)
        if path == f"{base}/deployments":
            items = sorted(
                self.deployments, key=lambda d: d["created_at"], reverse=True
            )
            return self._page(items, params)
        if path.startswith(f"{base}/deployments/") and path.endswith("/statuses"):
            return self._page(self.statuses.get(int(path.split("/")[-2]), []), params)
        if path.startswith(f"{base}/compare/"):
            a, b = path.split("/compare/")[1].split("...")
            if (a, b) not in self.compare:
                raise dm.ApiError(404, "Not Found (HTTP 404)")
            return {"status": self.compare[(a, b)]}
        raise AssertionError(f"unexpected request {path} {params}")

    def calls_to(self, suffix):
        return [r for r in self.requests if r[0].endswith(suffix)]


def commit(sha, authored):
    return {"sha": sha, "commit": {"author": {"date": authored}}}


@pytest.fixture
def small_pages(monkeypatch):
    monkeypatch.setattr(dm, "PER_PAGE", 2)


def make_api(gh, **kw):
    kw.setdefault("per_page", 2)
    return dm.Api(gh, **kw)


def compute(gh, **kw):
    api = make_api(gh, max_calls=kw.pop("max_calls", 1000))
    return dm.compute(
        api, REPO, kw.pop("branch", "main"), SINCE, UNTIL, now=UNTIL, **kw
    )


def standard_repo():
    """Three PRs merged into main in the window plus noise outside it."""
    gh = FakeGitHub()
    gh.pulls = [
        pr(1, ts(13, 8, 0), ts(13, 9, 0)),  # 60 min open
        pr(2, ts(14, 8, 0), ts(14, 8, 30)),  # 30 min open
        pr(3, ts(15, 8, 0), ts(15, 10, 0)),  # 120 min open
        pr(4, ts(10, 8, 0), ts(10, 9, 0)),  # merged before window
        pr(5, ts(13, 8, 0), ts(13, 9, 0), base="integration"),  # other branch
        pr(6, ts(14, 8, 0), None),  # closed unmerged
    ]
    gh.commits = {
        1: [commit("c1a", ts(13, 7, 0)), commit("c1b", ts(13, 8, 30))],
        2: [commit("c2a", ts(14, 7, 30))],
        3: [commit("c3a", ts(15, 6, 0))],
    }
    return gh


# --------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------


def test_pagination_collects_every_page_of_prs_and_commits():
    gh = FakeGitHub()
    # 5 merged PRs, page size 2 -> 3 pages of PR listing; one PR with 5 commits -> 3 commit pages.
    gh.pulls = [pr(n, ts(13, n), ts(13, n, 30)) for n in range(1, 6)]
    gh.commits = {1: [commit(f"x{i}", ts(13, 0, i)) for i in range(5)]}
    report = compute(gh)
    assert report["metrics"]["merges_per_day"]["total_merges"] == 5
    assert len(gh.calls_to("/pulls")) == 3
    assert len(gh.calls_to("/pulls/1/commits")) == 3
    # the first commit came from the LAST page's data set, proving all pages were read
    gh2 = FakeGitHub()
    gh2.pulls = [pr(1, ts(13, 8), ts(13, 20))]
    gh2.commits = {
        1: [commit("a", ts(13, 9)), commit("b", ts(13, 10)), commit("c", ts(13, 6))]
    }
    lt = compute(gh2)["metrics"]["pr_lead_time"]["first_commit_to_merged"]
    assert lt["median"] == 14 * 60.0


def test_listing_stops_once_pages_are_older_than_window():
    gh = FakeGitHub()
    gh.pulls = [pr(1, ts(14, 1), ts(14, 2))] + [
        pr(n, ts(1, 1), ts(1, 2)) for n in range(10, 30)
    ]
    compute(gh)
    # 21 PRs / page size 2 would be 11 pages; sorted by updated desc, so it must stop after
    # the first page that ends before the window.
    assert len(gh.calls_to("/pulls")) <= 2


def test_search_cap_is_bisected_so_no_run_is_lost(monkeypatch):
    monkeypatch.setattr(dm, "SEARCH_CAP", 6)
    gh = FakeGitHub()
    gh.search_cap = 6
    gh.pulls = [pr(1, ts(13, 1), ts(16, 1), merge_sha="mm1")]
    gh.commits = {1: [commit("c1", ts(13, 0))]}
    # 20 runs on the PR's commit spread over the window: far above the (patched) cap of 6
    gh.runs = [
        run(
            100 + i,
            "c1",
            ts(13 + i // 8, (i % 8) * 3),
            ts(13 + i // 8, (i % 8) * 3, 10),
        )
        for i in range(20)
    ]
    report = compute(gh)
    ci = report["metrics"]["ci_minutes_per_pr"]
    assert ci["runs_attributed"] == 20
    assert ci["median"] == 200.0  # 20 runs x 10 min
    assert not report["truncated"]


def test_call_budget_truncation_is_reported_not_hidden():
    gh = FakeGitHub()
    gh.pulls = [pr(n, ts(13, n), ts(13, n, 30)) for n in range(1, 8)]
    report = compute(gh, max_calls=3)
    assert report["truncated"] is True
    assert any("budget" in n for n in report["truncation_notes"])
    assert report["api_calls"] <= 3


# --------------------------------------------------------------------------
# Change-failure (revert) detection
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title, body, labels, expected",
    [
        ('Revert "Add widget cache"', "", (), True),
        ("revert: bad migration", "", (), True),
        ("Add widget cache", "This reverts #41 which broke the build", (), True),
        ("Add widget cache", "Reverts acme/widgets#41", (), True),
        ("Fix cache", "", ("hotfix",), True),
        ("Fix cache", "", ("Incident",), True),
        ("Add widget cache", "Mentions #41 and revert-ability in passing", (), False),
        ("Reverse the order of widgets", "", (), False),
        ("Add widget cache", "", ("feature",), False),
    ],
)
def test_failure_reasons(title, body, labels, expected):
    p = {"title": title, "body": body, "labels": [lbl.lower() for lbl in labels]}
    assert bool(dm.failure_reasons(p)) is expected


def test_change_failure_rate_counts_over_merged_prs_into_branch():
    gh = FakeGitHub()
    gh.pulls = [
        pr(1, ts(13, 1), ts(13, 2)),
        pr(2, ts(14, 1), ts(14, 2), title='Revert "Change 1"'),
        pr(3, ts(14, 3), ts(14, 4), body="reverts #1"),
        pr(4, ts(15, 1), ts(15, 2), labels=("hotfix",)),
        pr(5, ts(15, 3), ts(15, 4)),
        pr(
            6, ts(15, 3), ts(15, 4), base="integration", title="Revert something"
        ),  # other branch: excluded
    ]
    cf = compute(gh)["metrics"]["change_failure_rate"]
    assert cf["n"] == 5 and cf["failures"] == 3
    assert cf["rate"] == 0.6
    assert cf["failing_prs"] == [2, 3, 4]
    assert "Revert" in cf["definition"]


# --------------------------------------------------------------------------
# CI attribution
# --------------------------------------------------------------------------


def ci_repo():
    gh = standard_repo()
    gh.runs = [
        # PR 1: two runs on its commits (10 + 20 min), one push run on its merge commit (5 min)
        run(1, "c1a", ts(13, 8, 0), ts(13, 8, 10)),
        run(2, "c1b", ts(13, 8, 40), ts(13, 9, 0)),
        run(3, "m001", ts(13, 9, 1), ts(13, 9, 6), event="push"),
        # PR 2: one 40 min run, one failed 10 min run on the same head
        run(4, "c2a", ts(14, 8, 0), ts(14, 8, 40)),
        run(5, "c2a", ts(14, 8, 5), ts(14, 8, 15), conclusion="failure"),
        # PR 3: a 90 min run
        run(6, "c3a", ts(15, 8, 0), ts(15, 9, 30)),
        # unrelated: a huge run on a SHA that belongs to no merged PR in the window
        run(7, "zzz", ts(14, 0, 0), ts(14, 23, 0)),
        # unrelated event type on a matching SHA must be ignored (schedule)
        run(8, "c3a", ts(15, 0, 0), ts(15, 20, 0), event="schedule"),
    ]
    return gh


def test_ci_minutes_attributed_to_right_pr_and_unrelated_sha_excluded():
    report = compute(ci_repo())
    ci = report["metrics"]["ci_minutes_per_pr"]
    # per-PR totals: PR1 = 10+20+5 = 35, PR2 = 40+10 = 50, PR3 = 90
    assert ci["status"] == "ok" and ci["n"] == 3
    assert ci["median"] == 50.0
    assert ci["runs_attributed"] == 6
    assert ci["runs_on_unrelated_shas"] == 1
    assert ci["prs_without_matched_runs"] == 0
    assert ci["p90"] == pytest.approx(82.0)  # linear interpolation between 50 and 90


def test_attribute_runs_maps_each_run_to_its_own_pr():
    prs = [
        {
            "number": 1,
            "merged_at": SINCE,
            "merge_commit_sha": "m1",
            "head_sha": "h1",
            "commits": [{"sha": "a"}],
        },
        {
            "number": 2,
            "merged_at": SINCE,
            "merge_commit_sha": "m2",
            "head_sha": "h2",
            "commits": [{"sha": "b"}],
        },
    ]
    runs = [
        run(1, "a", ts(13), ts(13, 1)),
        run(2, "m2", ts(13), ts(13, 1), event="push"),
        run(3, "nope", ts(13), ts(13, 1)),
    ]
    got = dm.attribute_runs(prs, runs)
    assert [r["id"] for r in got["by_pr"][1]] == [1]
    assert [r["id"] for r in got["by_pr"][2]] == [2]
    assert got["unmatched"] == 1


def test_stacked_sha_goes_to_pr_that_owns_it_directly():
    shared = {"sha": "shared"}
    prs = [
        {
            "number": 1,
            "merged_at": SINCE + timedelta(hours=2),
            "merge_commit_sha": "m1",
            "head_sha": "shared",
            "commits": [shared],
        },
        {
            "number": 2,
            "merged_at": SINCE + timedelta(hours=1),
            "merge_commit_sha": "m2",
            "head_sha": "h2",
            "commits": [shared, {"sha": "h2"}],
        },
    ]
    got = dm.attribute_runs(prs, [run(1, "shared", ts(13), ts(13, 1))])
    assert list(got["by_pr"]) == [1]  # head of PR 1, even though PR 2 merged first
    assert got["ambiguous"] == 1


def test_ci_failure_rate_excludes_cancelled_and_counts_failures():
    gh = ci_repo()
    gh.runs.append(run(9, "c1a", ts(13, 8, 0), ts(13, 8, 1), conclusion="cancelled"))
    cf = compute(gh)["metrics"]["ci_failure_rate"]
    assert cf["n"] == 6 and cf["failed"] == 1 and cf["excluded_runs"] == 1
    assert cf["rate"] == pytest.approx(1 / 6, abs=1e-4)


def test_required_run_wall_clock_identified_from_required_checks():
    gh = ci_repo()
    # add a summary workflow with one 7 min run per PR
    gh.runs += [
        run(20, "c1b", ts(13, 8, 50), ts(13, 8, 57), name="Required Checks"),
        run(21, "c2a", ts(14, 8, 50), ts(14, 8, 57), name="Required Checks"),
        run(22, "c3a", ts(15, 8, 50), ts(15, 8, 57), name="Required Checks"),
    ]
    gh.required = ["required checks"]
    ci = compute(gh)["metrics"]["ci_minutes_per_pr"]
    rr = ci["required_run_wall_clock"]
    assert rr["status"] == "ok" and rr["median"] == 7.0 and rr["n"] == 3
    assert rr["identified_by"] == "required_status_check"


def test_required_run_unavailable_when_no_workflow_identifiable():
    gh = ci_repo()
    for r in gh.runs:
        r["name"] = (
            "Build"  # matches neither a required check nor the summary-name heuristic
        )
    ci = compute(gh)["metrics"]["ci_minutes_per_pr"]
    assert ci["required_run_wall_clock"]["status"] == "unavailable"
    # while the default name "CI" is picked up by the heuristic
    assert compute(ci_repo())["metrics"]["ci_minutes_per_pr"][
        "required_run_wall_clock"
    ]["identified_by"].startswith("name heuristic")


# --------------------------------------------------------------------------
# Unavailable versus zero
# --------------------------------------------------------------------------


def test_ci_unavailable_not_zero_when_runs_exist_but_none_match():
    gh = standard_repo()
    gh.runs = [
        run(1, "unrelated1", ts(14, 1), ts(14, 2)),
        run(2, "unrelated2", ts(14, 3), ts(14, 4)),
    ]
    report = compute(gh)
    ci = report["metrics"]["ci_minutes_per_pr"]
    assert ci["status"] == "unavailable"
    assert "none matched" in ci["reason"]
    assert "median" not in ci  # no number at all, in particular not 0
    assert report["metrics"]["ci_failure_rate"]["status"] == "unavailable"


def test_ci_unavailable_when_no_runs_at_all():
    ci = compute(standard_repo())["metrics"]["ci_minutes_per_pr"]
    assert ci["status"] == "unavailable" and "no workflow runs" in ci["reason"]


def test_ci_unavailable_when_every_matched_run_has_zero_duration():
    gh = standard_repo()
    gh.runs = [
        run(1, "c1a", ts(13, 8, 0), ts(13, 8, 0)),
        run(2, "c2a", ts(14, 8, 0), ts(14, 8, 0)),
    ]
    ci = compute(gh)["metrics"]["ci_minutes_per_pr"]
    assert ci["status"] == "unavailable" and "zero duration" in ci["reason"]


def test_incomplete_runs_do_not_count_as_zero_minutes():
    gh = standard_repo()
    gh.runs = [
        run(
            1, "c1a", ts(13, 8, 0), ts(13, 8, 30), status="in_progress", conclusion=None
        )
    ]
    ci = compute(gh)["metrics"]["ci_minutes_per_pr"]
    assert ci["status"] == "unavailable"


def test_zero_merges_is_flagged_suspicious_and_names_the_branch_that_moved():
    gh = FakeGitHub()
    gh.pulls = [
        pr(n, ts(14, n), ts(14, n, 30), base="integration") for n in range(1, 4)
    ]
    report = compute(gh)
    merges = report["metrics"]["merges_per_day"]
    assert merges["suspicious_zero"] is True
    assert "integration (3)" in merges["note"]
    assert report["metrics"]["pr_lead_time"]["status"] == "unavailable"
    assert report["metrics"]["change_failure_rate"]["status"] == "unavailable"
    assert report["metrics"]["deploy_lead_time"]["status"] == "unavailable"
    assert any("integration" in w for w in report["warnings"])


def test_lead_time_values():
    report = compute(standard_repo())
    lt = report["metrics"]["pr_lead_time"]
    assert lt["created_to_merged"]["n"] == 3
    assert lt["created_to_merged"]["median"] == 60.0  # 30, 60, 120
    assert lt["created_to_merged"]["p90"] == pytest.approx(108.0)
    # first commit -> merged: 120, 30+60=... PR1 07:00->09:00=120, PR2 07:30->08:30=60, PR3 06:00->10:00=240
    assert lt["first_commit_to_merged"]["median"] == 120.0
    merges = report["metrics"]["merges_per_day"]
    assert merges["total_merges"] == 3 and merges["mean_per_day"] == 0.75
    assert [s["merges"] for s in merges["series"]] == [1, 1, 1, 0]


def test_deploy_lead_time_unavailable_without_deploy_source():
    dl = compute(standard_repo())["metrics"]["deploy_lead_time"]
    assert dl["status"] == "unavailable" and "no GitHub Deployments" in dl["reason"]


def test_deploy_lead_time_exact_and_descendant_and_branch_share():
    gh = standard_repo()
    gh.deployments = [
        {
            "id": 11,
            "sha": "m001",
            "created_at": ts(13, 9, 5),
            "environment": "production",
        },
        {
            "id": 12,
            "sha": "d-after-2",
            "created_at": ts(14, 9, 0),
            "environment": "production",
        },
        {
            "id": 13,
            "sha": "stray",
            "created_at": ts(14, 12, 0),
            "environment": "production",
        },
    ]
    gh.statuses = {
        11: [
            {"state": "in_progress", "created_at": ts(13, 9, 6)},
            {"state": "success", "created_at": ts(13, 9, 30)},
        ],
        12: [{"state": "success", "created_at": ts(14, 9, 20)}],
        13: [{"state": "failure", "created_at": ts(14, 12, 5)}],
    }
    # PR 2 (merge m002) is an ancestor of the deploy sha d-after-2; PR 3 never deployed.
    gh.compare = {
        ("m002", "d-after-2"): "ahead",
        ("m003", "d-after-2"): "behind",
        ("main", "m001"): "behind",
        ("main", "d-after-2"): "diverged",
    }
    dl = compute(gh)["metrics"]["deploy_lead_time"]
    assert dl["status"] == "ok"
    lead = dl["merge_to_deploy"]
    # PR1: merged 09:00 -> deployed 09:30 = 30; PR2: merged 08:30 -> deployed 09:20 = 50
    assert lead["n"] == 2 and lead["median"] == 40.0
    assert lead["prs_without_matching_deploy"] == 1
    share = dl["deploy_sha_on_branch"]
    assert (
        share["on_branch"] == 1
        and share["off_branch"] == 1
        and share["share_on_branch"] == 0.5
    )


def test_deploy_workflow_runs_count_but_pull_request_previews_do_not():
    gh = standard_repo()
    gh.workflows = [{"id": 7, "name": "Deploy production"}, {"id": 8, "name": "Lint"}]
    gh.workflow_runs = {
        7: [
            run(
                1,
                "m001",
                ts(13, 9, 5),
                ts(13, 9, 25),
                event="push",
                name="Deploy production",
            ),
            run(
                2,
                "c2a",
                ts(14, 8, 5),
                ts(14, 8, 9),
                event="pull_request",
                name="Deploy production",
            ),
        ]
    }
    gh.compare = {("main", "m001"): "identical"}
    dl = compute(gh)["metrics"]["deploy_lead_time"]
    assert dl["status"] == "ok" and dl["merge_to_deploy"]["n"] == 1
    assert dl["merge_to_deploy"]["median"] == 25.0
    assert dl["deploy_sha_on_branch"]["share_on_branch"] == 1.0


# --------------------------------------------------------------------------
# Interventions file
# --------------------------------------------------------------------------


def test_interventions_unavailable_without_file():
    oi = compute(standard_repo())["metrics"]["operator_interventions_per_day"]
    assert oi["status"] == "unavailable" and "interventions-file" in oi["reason"]


def test_interventions_file_both_shapes_and_window(tmp_path):
    f = tmp_path / "iv.json"
    f.write_text(
        json.dumps(
            [
                {"date": "2026-10-13", "count": 3},
                {"date": "2026-10-14", "count": 1},
                {"at": "2026-10-14T09:30:00Z", "note": "approved a gate"},
                {"at": "2026-10-16T23:59:00+00:00", "note": "late"},
                {"date": "2026-10-20", "count": 9},  # outside the window
            ]
        )
    )
    entries = dm.load_interventions(str(f))
    oi = compute(standard_repo(), interventions=entries)["metrics"][
        "operator_interventions_per_day"
    ]
    assert oi["total"] == 6 and oi["entries_outside_window"] == 1
    assert oi["mean_per_day"] == 1.5  # 6 over the 4-day window
    assert [s["interventions"] for s in oi["series"]] == [3, 2, 0, 1]
    assert "suspicious_zero" not in oi


def test_interventions_empty_in_window_is_flagged_not_trusted():
    oi = compute(standard_repo(), interventions=[{"date": "2026-11-01", "count": 2}])[
        "metrics"
    ]["operator_interventions_per_day"]
    assert oi["status"] == "ok" and oi["total"] == 0 and oi["suspicious_zero"] is True


@pytest.mark.parametrize(
    "bad",
    [
        {"not": "a list"},
        [1],
        [{"date": "2026-10-13"}],
        [{"date": "2026-10-13", "count": -1}],
        [{"date": "x", "count": 1}],
        [{"at": "soon"}],
        [{"note": "n"}],
        [{"date": "2026-10-13", "count": True}],
    ],
)
def test_interventions_rejects_malformed_entries(bad):
    with pytest.raises(ValueError):
        dm.normalise_interventions(bad)


# --------------------------------------------------------------------------
# Output and CLI
# --------------------------------------------------------------------------


def test_every_ok_metric_carries_n_and_unavailable_carries_reason():
    report = compute(ci_repo())

    def walk(node, path=""):
        if isinstance(node, dict):
            if node.get("status") == "ok":
                assert "n" in node, f"{path} lacks n"
            if node.get("status") == "unavailable":
                assert node.get("reason"), f"{path} lacks reason"
            for k, v in node.items():
                walk(v, f"{path}.{k}")

    walk(report["metrics"])


def test_cli_json_and_summary(capsys, tmp_path):
    gh = ci_repo()
    argv = [
        "--repo",
        REPO,
        "--since",
        "2026-10-13",
        "--until",
        "2026-10-17",
        "--branch",
        "main",
    ]
    assert dm.main(argv + ["--json"], transport=gh) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["metrics"]["ci_minutes_per_pr"]["median"] == 50.0
    assert data["definitions"]["change_failure_rate"]

    assert dm.main(argv, transport=FakeGitHub.__call__.__get__(gh)) == 0
    text = capsys.readouterr().out
    assert (
        "Merges per day" in text
        and "unavailable" in text
        and "Change-failure definition" in text
    )


def test_cli_default_branch_is_looked_up(capsys):
    gh = standard_repo()
    gh.default_branch = "main"
    assert (
        dm.main(
            [
                "--repo",
                REPO,
                "--since",
                "2026-10-13",
                "--until",
                "2026-10-17",
                "--json",
            ],
            transport=gh,
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["branch"] == "main"
    assert any(p == f"/repos/{REPO}" for p, _ in gh.requests)


def test_cli_rejects_bad_repo_and_bad_interventions_file(capsys, tmp_path):
    assert (
        dm.main(["--repo", "nonsense", "--since", "2026-10-13"], transport=FakeGitHub())
        == 2
    )
    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert (
        dm.main(
            ["--repo", REPO, "--since", "2026-10-13", "--interventions-file", str(bad)],
            transport=FakeGitHub(),
        )
        == 2
    )
    capsys.readouterr()


def test_percentile():
    assert dm.percentile([5], 0.9) == 5
    assert dm.percentile([1, 2, 3, 4], 0.5) == 2.5
    assert dm.percentile([0, 100], 0.9) == pytest.approx(90)
